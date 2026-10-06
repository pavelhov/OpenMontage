"""Tool registry with status, stability, and support-envelope reporting.

The registry discovers all registered tools, reports their availability,
and lets the orchestrator/agents query capabilities by tier, status, etc.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from types import ModuleType
from typing import Any, Optional

from tools.base_tool import BaseTool, ToolStatus, ToolTier, ToolStability


# Unicode punctuation that breaks on Windows cp1252 stdout. Map each to an
# ASCII equivalent. This only touches strings rendered by registry helpers
# that an agent is likely to print to the user at preflight — not docstrings,
# comments, or markdown.
_UNICODE_DASH_REPLACEMENTS = {
    "\u2014": "--",   # em dash
    "\u2013": "-",    # en dash
    "\u2212": "-",    # minus sign
    "\u2018": "'",    # left single quote
    "\u2019": "'",    # right single quote
    "\u201c": '"',    # left double quote
    "\u201d": '"',    # right double quote
    "\u2026": "...",  # ellipsis
}


def _scrub_unicode_dashes(value: Any) -> Any:
    """Recursively normalize unicode punctuation in str leaves to ASCII.

    Used to keep `provider_menu_summary()` output readable on Windows cp1252
    stdout. Does NOT modify dict/list structure or non-string values.
    """
    if isinstance(value, str):
        out = value
        for needle, repl in _UNICODE_DASH_REPLACEMENTS.items():
            if needle in out:
                out = out.replace(needle, repl)
        return out
    if isinstance(value, list):
        return [_scrub_unicode_dashes(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_unicode_dashes(item) for item in value)
    if isinstance(value, dict):
        return {k: _scrub_unicode_dashes(v) for k, v in value.items()}
    return value


_OPENART_HELD_DEBIT = frozenset({"reserved", "unresolved"})
_OPENART_PENDING_SLOTS = frozenset({"prepared", "ready", "submitting", "submitted", "uncertain"})


def _openart_route(tool: BaseTool) -> dict[str, Any]:
    """Read-only OpenArt menu row: qualification files + existing ledger snapshot.

    Never calls the CLI, never constructs CreditLedger, never creates state.
    Only FULL real-result-qualified profiles count as available models;
    inspected/pre_submit rows are qualification candidates; fixtures never live.
    """
    supports = dict(getattr(tool, "supports", {}))
    errors: list[str] = []
    try:
        status = tool.get_status().value
    except Exception as exc:  # pragma: no cover - defensive
        status = "unavailable"
        errors.append(f"status_error:{type(exc).__name__}")
    available_models: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    try:
        from lib import openart_jobs
        rows = openart_jobs.list_qualifications()
    except Exception as exc:
        rows = []
        errors.append(f"qualification_read_error:{type(exc).__name__}")
    try:
        catalog = tool._model_catalog() if hasattr(tool, "_model_catalog") else {}
    except Exception as exc:  # pragma: no cover - adapter catalog never raises
        catalog = {}
        errors.append(f"model_catalog_error:{type(exc).__name__}")
    for row in rows:
        compact = {
            "model": row.get("model"),
            "mode": row.get("mode"),
            "level": row.get("level"),
            "cli_version": row.get("cli_version"),
            "profile_sha256": row.get("profile_sha256"),
        }
        if row.get("source") != "real" or row.get("error") == "fixture_profile":
            rejected.append({**compact, "reason": "fixture_profile_never_live"})
        elif row.get("valid") is True and row.get("level") == "full":
            entry = ((catalog.get(row.get("model")) or {}).get("modes") or {}).get(row.get("mode"))
            if entry and entry.get("profile_sha256") == row.get("profile_sha256"):
                controls = {"native_controls": entry["native_controls"],
                            "native_controls_source": "retained_form_and_exact_preview_receipts",
                            "required_unqualified": entry["required_unqualified"],
                            "argv_flag_without_effective_preview":
                                entry["argv_flag_without_effective_preview"]}
                account = entry.get("account_id_sha256")
            else:
                # Full row whose retained receipts could not be verified: never ready.
                controls = {"native_controls": "unverified_receipts",
                            "native_controls_source": "unavailable", "required_unqualified": None,
                            "argv_flag_without_effective_preview": []}
                account = None
                errors.append("model_catalog_unverified")
            available_models.append({**compact,
                                     "result_contract_sha256": row.get("result_contract_sha256"),
                                     "result_proof_id": row.get("result_proof_id"),
                                     "account_id_sha256": account,
                                     "catalog_verified": account is not None,
                                     "controls": controls})
        elif row.get("level") in ("inspected", "pre_submit"):
            candidates.append({**compact,
                               "purpose": "qualification_candidate",
                               "production_available": False,
                               "next_stage": "pre_submit" if row.get("level") == "inspected"
                               else "first_original_result_qualification",
                               "error": row.get("error")})
        else:
            rejected.append({**compact, "reason": row.get("error") or "unqualified"})
    profile_accounts = sorted({m["account_id_sha256"] for m in available_models if m["account_id_sha256"]})
    ledger: dict[str, Any] = {"initialized": False, "scope": "provider:openart_cli",
                              "accounts": [], "pending": [], "holds": [],
                              "quarantine": [], "unacknowledged_outbox": 0, "error": None}
    try:
        from lib.provider_credit_ledger import read_existing_snapshot
        snap = read_existing_snapshot()
        ledger["initialized"] = bool(snap.get("initialized"))
        attempts: set[str] = set()
        for res in snap.get("reservations", []):
            account = _openart_account_key(res.get("account_key"))
            if account is None or not _in_scope(account, profile_accounts):
                continue  # other providers'/accounts' facts never enter the OpenArt row
            attempts.add(str(res.get("attempt_id")))
            item = {"attempt_id": res.get("attempt_id"), "account": account,
                    "slot_state": res.get("slot_state"),
                    "debit_state": res.get("debit_state"),
                    "reserved_units": res.get("reserved_units"),
                    "charged_units": res.get("charged_units")}
            if res.get("slot_state") in _OPENART_PENDING_SLOTS:
                ledger["pending"].append(item)
            if res.get("debit_state") in _OPENART_HELD_DEBIT:
                # Debit state (billing) and slot state (job acceptance) are separate
                # facts. An unresolved debit on a terminal/closed slot is a retained
                # billing hold only: economics are incomplete, but it does not make
                # the route ineligible. Unknown job acceptance is the pending-slot
                # blocker below.
                ledger["holds"].append({**item, "hold": "unknown_billing"
                                        if res.get("debit_state") == "unresolved"
                                        else "pending_reservation",
                                        "job_acceptance_unknown":
                                        res.get("slot_state") in _OPENART_PENDING_SLOTS})
        for acct in snap.get("accounts", []):
            account = _openart_account_key(acct.get("account_key"))
            if account is None or not _in_scope(account, profile_accounts):
                continue
            ledger["accounts"].append({**account, "quarantined": bool(acct.get("quarantined")),
                                       "observed_units": acct.get("observed_units"),
                                       "balance_units": acct.get("balance_units")})
            if acct.get("quarantined"):
                ledger["quarantine"].append({"account": account, "reason": "account_quarantined"})
        for q in snap.get("account_quarantine", []):
            claim = _openart_claim_key(q.get("claim_key"))
            if claim is not None and _in_scope(claim, profile_accounts):
                ledger["quarantine"].append({"account": claim, "reason": q.get("reason")})
        ledger["unacknowledged_outbox"] = sum(
            1 for o in snap.get("outbox", []) if str(o.get("attempt_id")) in attempts)
    except Exception as exc:
        ledger["error"] = f"{type(exc).__name__}: {exc}"
        errors.append("ledger_snapshot_error")
    levels = {r.get("level") for r in available_models} | {c.get("level") for c in candidates}
    if "full" in levels:
        discovery = "full_profile_retained"
    elif "pre_submit" in levels:
        discovery = "pre_submit_profile_retained"
    elif "inspected" in levels:
        discovery = "inspected_profile_retained"
    else:
        discovery = "pending"
    blockers: list[str] = []
    if status != "available":
        blockers.append(f"tool_status:{status}")
    verified_models = [m for m in available_models if m["catalog_verified"]]
    if not available_models:
        blockers.append("no_full_real_result_qualified_profile")
    elif not verified_models:
        blockers.append("full_profile_receipts_unverified")
    if ledger["error"]:
        blockers.append("ledger_unreadable")
    if ledger["pending"]:
        blockers.append("pending_openart_attempts_need_reconciliation")
    if any(h["job_acceptance_unknown"] for h in ledger["holds"]):
        blockers.append("unknown_job_acceptance_unresolved")
    billing_holds = [h for h in ledger["holds"] if h["hold"] == "unknown_billing"]
    if ledger["quarantine"]:
        blockers.append("openart_account_quarantined")
    if ledger["unacknowledged_outbox"]:
        blockers.append("unacknowledged_openart_outbox")
    production_available = status == "available" and bool(verified_models)
    return {
        "tool": tool.name,
        "provider": tool.provider,
        "status": status,
        "production_available": production_available,
        "selection": "explicit_singleton_pin_only",
        "operations": [op for op in ("text_to_video", "image_to_video") if supports.get(op)],
        "models": available_models,
        "qualification_candidates": candidates,
        "not_live": rejected,
        "controls": {
            "first_last_frame": supports.get("first_last_frame") is True,
            "native_audio": supports.get("native_audio") is True,
            "multiple_reference_images": supports.get("multiple_reference_images") is True,
            # Native controls are per model/mode (models[].controls), read from
            # retained form + exact preview receipts. Nothing route-wide is guessed.
            "native_controls": "per_model" if verified_models else "not_yet_qualified",
            "native_controls_source": "models[].controls" if verified_models
            else "no_verified_full_profile",
            "required_unqualified": ["resolution"] if not verified_models else sorted(
                {n for m in verified_models for n in m["controls"]["required_unqualified"]}),
        },
        "limitations": [
            "no end-frame pin",
            "no native audio",
            "single reference image only",
            "required resolution stays unqualified until an observed form and exact preview exist; defaults never substitute",
            "models and controls come only from observed qualification profiles",
        ],
        "account_stages": {
            "account_discovery": discovery,
            "fresh_refresh_required_before_dispatch": True,
            "qualification_stages": ["inspected", "pre_submit", "full"],
        },
        "dispatch_readiness": {
            # A qualified model is not an available account.
            "ready": not blockers,
            "blockers": blockers,
            "account_scope": ({"kind": "verified_profile_accounts",
                               "account_id_sha256": profile_accounts} if profile_accounts
                              else "all_openart_accounts_until_verified_profile_account"),
            "fresh_prelaunch_refresh_required": True,
            "requires": ["fresh_account_refresh", "current_quote", "approved_credit_authorization"],
        },
        "billing": {
            "kind": "credits",
            "billing_unit": "credits",
            "current_quote_required": True,
            "usd_cost_status": "unknown",
            "estimated_cost_usd": None,
            "holds": {
                "pending_reservation": [h for h in ledger["holds"] if h["hold"] == "pending_reservation"],
                "unknown_billing": billing_holds,
            },
            # Retained unknown-billing holds keep the route eligible but the
            # economics incomplete: remaining allowance + a current quote decide.
            "economics": "incomplete_unknown_billing_hold" if billing_holds else "quote_required",
            "remaining_allowance_required": bool(billing_holds),
            "remaining_allowance": "unknown_until_current_quote",
        },
        "ledger": ledger,
        "errors": errors,
        "recommendation": None,
    }


def _grok_route(tool: BaseTool) -> dict[str, Any]:
    """Grok menu row. Unlike OpenArt metadata, get_status() runs the existing
    read-only `grok --version` / `--help` compatibility probes (never a
    generation). The CLI manages and does not report the media model: the pinned
    agent model is not a video model, so no models are listed or selectable.
    """
    supports = dict(getattr(tool, "supports", {}))
    try:
        status = tool.get_status().value
    except Exception:  # pragma: no cover - defensive
        status = "unavailable"
    return {
        "tool": tool.name,
        "provider": tool.provider,
        "status": status,
        "status_probe": "readonly_cli_version_and_help",
        "selection": "explicit_singleton_pin_only",
        "operations": list(getattr(tool, "capabilities", [])),
        "models": [],
        "model_policy": "cli_managed_media_unreported",
        "model_selection": "not_supported",
        "controls": {
            "first_last_frame": supports.get("first_last_frame") is True,
            "native_audio": supports.get("native_audio") is True,
            "preset_voices": supports.get("preset_voices") is True,
            "multiple_reference_images": supports.get("multiple_reference_images") is True,
            "text_to_video": supports.get("text_to_video") is True,
            "operation_specific": True,
        },
        "billing": {
            "kind": "subscription_quota_unknown",
            "billing_unit": None,
            "usd_cost_status": "unknown",
            "estimated_cost_usd": None,
        },
        "recommendation": None,
    }


def _in_scope(account: dict[str, Any], profile_accounts: list[str]) -> bool:
    """Until a verified full profile names its account, every OpenArt account counts."""
    return not profile_accounts or account.get("account_id_sha256") in profile_accounts


def _openart_account_key(raw: Any) -> Optional[dict[str, Any]]:
    import json
    try:
        parts = json.loads(raw)
    except Exception:
        return None
    if isinstance(parts, list) and len(parts) == 3 and parts[0] == "openart_cli":
        return {"account_id_sha256": parts[1], "workspace": parts[2]}
    return None


def _openart_claim_key(raw: Any) -> Optional[dict[str, Any]]:
    import json
    try:
        parts = json.loads(raw)
    except Exception:
        return None
    if isinstance(parts, list) and len(parts) == 2 and parts[0] == "openart_cli":
        return {"account_id_sha256": parts[1], "workspace": None}
    return None


class ToolRegistry:
    """Central registry of all OpenMontage tools."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._discovered_packages: set[str] = set()

    def register(self, tool: BaseTool) -> None:
        """Register a tool instance."""
        if not tool.name:
            raise ValueError("Tool must have a non-empty name")
        self._tools[tool.name] = tool

    def clear(self) -> None:
        """Clear registered tools and discovery state."""
        self._tools.clear()
        self._discovered_packages.clear()

    def register_module(self, module: ModuleType) -> list[str]:
        """Register all concrete BaseTool subclasses defined in a module."""
        registered: list[str] = []
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls is BaseTool or not issubclass(cls, BaseTool):
                continue
            if cls.__module__ != module.__name__ or inspect.isabstract(cls):
                continue
            tool = cls()
            self.register(tool)
            registered.append(tool.name)
        return registered

    @staticmethod
    def _load_dotenv() -> None:
        """Load .env file into os.environ if present, so tools can find API keys."""
        from pathlib import Path
        import os
        env_path = Path(__file__).resolve().parent.parent / ".env"
        if not env_path.is_file():
            return
        import re
        with open(env_path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # Quoted value: take the content inside the quotes verbatim.
                if value[:1] in ("'", '"'):
                    quote = value[0]
                    end = value.find(quote, 1)
                    value = value[1:end] if end != -1 else value[1:]
                else:
                    # Strip an inline comment ('#' at line start or after
                    # whitespace) so "KEY=   # note" yields "" not "# note".
                    match = re.search(r"(^|\s)#", value)
                    if match:
                        value = value[: match.start()]
                    value = value.strip()
                if key and key not in os.environ:
                    os.environ[key] = value

    def discover(self, package_name: str = "tools") -> list[str]:
        """Import a package tree and register any concrete tools it defines."""
        self._load_dotenv()
        package = importlib.import_module(package_name)
        discovered: list[str] = []
        package_paths = getattr(package, "__path__", None)
        if package_paths is None:
            return self.register_module(package)

        for module_info in pkgutil.walk_packages(package_paths, f"{package.__name__}."):
            if module_info.name.endswith(".base_tool") or module_info.name.endswith(".tool_registry"):
                continue
            module = importlib.import_module(module_info.name)
            discovered.extend(self.register_module(module))

        self._discovered_packages.add(package_name)
        return discovered

    def ensure_discovered(self, package_name: str = "tools") -> None:
        """Load tool modules once before reporting capabilities."""
        if package_name not in self._discovered_packages:
            self.discover(package_name)

    def get(self, name: str) -> Optional[BaseTool]:
        """Get a tool by name."""
        return self._tools.get(name)

    def list_all(self) -> list[str]:
        """List all registered tool names."""
        return list(self._tools.keys())

    def get_by_tier(self, tier: ToolTier) -> list[BaseTool]:
        """Get all tools in a given tier."""
        return [t for t in self._tools.values() if t.tier == tier]

    def get_by_capability(self, capability: str) -> list[BaseTool]:
        """Get all tools registered for a top-level capability family."""
        return [t for t in self._tools.values() if t.capability == capability]

    def get_by_provider(self, provider: str) -> list[BaseTool]:
        """Get all tools backed by a specific provider."""
        return [t for t in self._tools.values() if t.provider == provider]

    def get_by_status(self, status: ToolStatus) -> list[BaseTool]:
        """Get all tools with a given status."""
        return [t for t in self._tools.values() if t.get_status() == status]

    def get_available(self) -> list[BaseTool]:
        """Get all tools that are currently available."""
        return self.get_by_status(ToolStatus.AVAILABLE)

    def get_unavailable(self) -> list[BaseTool]:
        """Get all tools that are currently unavailable."""
        return self.get_by_status(ToolStatus.UNAVAILABLE)

    def get_by_stability(self, stability: ToolStability) -> list[BaseTool]:
        """Get all tools at a given stability level."""
        return [t for t in self._tools.values() if t.stability == stability]

    def find_by_capability(self, capability: str) -> list[BaseTool]:
        """Find tools that declare a given capability."""
        return [
            t for t in self._tools.values()
            if capability in t.capabilities
        ]

    def find_fallback(self, tool_name: str) -> Optional[BaseTool]:
        """Find the fallback tool for a given tool, if declared and available."""
        tool = self.get(tool_name)
        if tool is None:
            return None
        candidates = list(tool.fallback_tools or [])
        if tool.fallback and tool.fallback not in candidates:
            candidates.append(tool.fallback)
        for name in candidates:
            fb = self.get(name)
            if fb and fb.get_status() == ToolStatus.AVAILABLE:
                return fb
        return None

    def support_envelope(self) -> dict[str, Any]:
        """Generate a full support-envelope report for all tools.

        Returns a dict mapping tool name to its contract info + live status.
        This is the primary report the orchestrator uses to understand
        what the system can and cannot do.
        """
        self.ensure_discovered()
        report: dict[str, Any] = {}
        for name, tool in self._tools.items():
            info = tool.get_info()
            report[name] = info
        return report

    def capability_catalog(self) -> dict[str, list[dict[str, Any]]]:
        """Group the support envelope by top-level capability."""
        self.ensure_discovered()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for tool in self._tools.values():
            grouped.setdefault(tool.capability, []).append(tool.get_info())
        for items in grouped.values():
            items.sort(key=lambda item: (item["provider"], item["name"]))
        return dict(sorted(grouped.items()))

    def provider_catalog(self) -> dict[str, list[dict[str, Any]]]:
        """Group the support envelope by provider."""
        self.ensure_discovered()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for tool in self._tools.values():
            grouped.setdefault(tool.provider, []).append(tool.get_info())
        for items in grouped.values():
            items.sort(key=lambda item: (item["capability"], item["name"]))
        return dict(sorted(grouped.items()))

    def tier_summary(self) -> dict[str, dict[str, int]]:
        """Summarize tool counts by tier and status.

        Returns:
            {"core": {"available": 5, "unavailable": 2, "degraded": 0}, ...}
        """
        summary: dict[str, dict[str, int]] = {}
        for tier in ToolTier:
            tier_tools = self.get_by_tier(tier)
            counts = {"available": 0, "unavailable": 0, "degraded": 0}
            for t in tier_tools:
                status = t.get_status().value
                counts[status] = counts.get(status, 0) + 1
            if tier_tools:
                summary[tier.value] = counts
        return summary

    def provider_menu(self) -> dict[str, dict[str, Any]]:
        """Generate a capability-grouped provider menu for user-facing display.

        Returns a dict like:
        {
            "video_generation": {
                "available": [{"name": ..., "provider": ..., "best_for": ...}],
                "unavailable": [{"name": ..., "provider": ..., "install_instructions": ...}],
                "total": 12,
                "configured": 2,
            },
            ...
        }

        This powers the agent's preflight provider menu — the agent reads this
        output and presents it to the user.  Adding a new tool to tools/ is
        enough; this method auto-discovers it.
        """
        self.ensure_discovered()
        menu: dict[str, dict[str, Any]] = {}

        # Skip selectors — they aggregate, they aren't providers themselves
        tools = [t for t in self._tools.values() if t.provider != "selector"]

        for tool in tools:
            cap = tool.capability
            if cap not in menu:
                menu[cap] = {"available": [], "unavailable": [], "total": 0, "configured": 0}

            info = tool.get_info()
            status = tool.get_status()
            entry = {
                "name": tool.name,
                "provider": tool.provider,
                "runtime": tool.runtime.value,
                "best_for": tool.best_for,
                "dependencies": info.get("dependencies", []),
                "install_instructions": tool.install_instructions,
                "status": status.value,
            }
            for extra_key in (
                "source_provider_menu",
                "source_provider_summary",
                "render_engines",
                "remotion_note",
                "provider_matrix",
                "setup_offer",
                "operation_statuses",
                "resource_profiles",
                "resource_profile_note",
                "pinned_final_frame",
            ):
                if extra_key in info:
                    entry[extra_key] = info[extra_key]

            if status == ToolStatus.AVAILABLE:
                menu[cap]["available"].append(entry)
                menu[cap]["configured"] += 1
            else:
                menu[cap]["unavailable"].append(entry)
            menu[cap]["total"] += 1

        for bucket in menu.values():
            bucket["available"].sort(key=lambda entry: (entry["provider"], entry["name"]))
            bucket["unavailable"].sort(key=lambda entry: (entry["provider"], entry["name"]))

        return dict(sorted(menu.items()))

    def provider_menu_summary(self) -> dict[str, Any]:
        """Compact, human-ready rollup of provider_menu() for onboarding/preflight.

        Returns a dict shaped for the "N of M configured" capability menu the
        agent is supposed to present to the user per AGENT_GUIDE.md → "Provider
        Menu (Mandatory at Preflight)". Collapses the firehose of
        support_envelope() into something the agent can paraphrase in plain
        language in a few lines.

        Example output (abbreviated):
        {
          "composition_runtimes": {
            "ffmpeg": True,
            "remotion": True,
            "hyperframes": True,
          },
          "capabilities": [
            {"capability": "video_generation", "configured": 10, "total": 16,
             "available_providers": ["fal", "heygen", ...],
             "unavailable_providers": ["openai", ...]},
            ...
          ],
          "setup_offers": [
             {"capability": "music_generation", "tool": "suno_music",
              "install_instructions": "Add SUNO_API_KEY to .env"},
             ...
          ],
          "runtime_warnings": [
             "hyperframes: npm package `hyperframes` not resolvable: ...",
             ...
          ],
        }

        Agents should use this as the source for the preflight capability
        menu rather than rendering `support_envelope()` or `provider_menu()`
        raw. See AGENT_GUIDE.md > "Provider Menu (Mandatory at Preflight)".
        """
        self.ensure_discovered()
        menu = self.provider_menu()

        # Composition runtimes — lift from video_compose.get_info() since
        # they're the signal the runtime-selection contract depends on.
        comp_runtimes: dict[str, bool] = {}
        runtime_warnings: list[str] = []
        vc = self._tools.get("video_compose")
        if vc is not None:
            info = vc.get_info()
            engines = info.get("render_engines") or {}
            comp_runtimes = {k: bool(v) for k, v in engines.items()}
        # If hyperframes_compose is registered, surface its npm-resolve reasons
        # explicitly — those are the "looks available but isn't" failures.
        hf = self._tools.get("hyperframes_compose")
        if hf is not None:
            hf_info = hf.get_info()
            rc = hf_info.get("hyperframes_runtime") or {}
            for reason in rc.get("reasons") or []:
                runtime_warnings.append(f"hyperframes: {reason}")

        # Capabilities rollup (configured/total + provider lists).
        # When a provider has multiple tools (e.g. seedance-fal and
        # seedance-replicate both reporting provider="seedance"), a
        # naive set-split shows the provider in BOTH available and
        # unavailable — confusing for users. Dedupe: if the provider has
        # any available tool, do NOT list it as unavailable.
        capabilities: list[dict[str, Any]] = []
        for cap, bucket in menu.items():
            available_providers = {
                e.get("provider") for e in bucket.get("available", [])
            } - {None}
            unavailable_providers = (
                {e.get("provider") for e in bucket.get("unavailable", [])}
                - {None}
                - available_providers  # provider with any available tool wins
            )
            capabilities.append(
                {
                    "capability": cap,
                    "configured": bucket.get("configured", 0),
                    "total": bucket.get("total", 0),
                    "available_providers": sorted(available_providers),
                    "unavailable_providers": sorted(unavailable_providers),
                }
            )

        # Setup offers — unavailable tools that would be 1-minute env-var fixes.
        # Filter for short install instructions referencing an env var so the
        # agent can lead with the easy wins.
        setup_offers: list[dict[str, Any]] = []
        for cap, bucket in menu.items():
            for entry in bucket.get("unavailable", []):
                offer = entry.get("setup_offer")
                if offer:
                    setup_offers.append(
                        {
                            "capability": cap,
                            "tool": entry.get("name"),
                            "provider": entry.get("provider"),
                            "runtime": entry.get("runtime"),
                            "install_instructions": entry.get("install_instructions") or "",
                            **offer,
                        }
                    )
                    continue

                env_vars = [
                    dep[4:]
                    for dep in entry.get("dependencies", [])
                    if isinstance(dep, str) and dep.startswith("env:")
                ]
                if env_vars:
                    setup_offers.append(
                        {
                            "capability": cap,
                            "tool": entry.get("name"),
                            "provider": entry.get("provider"),
                            "runtime": entry.get("runtime"),
                            "kind": "env_var",
                            "fix_complexity": "1-minute env-var",
                            "env_vars": env_vars,
                            "install_instructions": entry.get("install_instructions") or "",
                        }
                    )
                    continue

                hint = entry.get("install_instructions") or ""
                # Heuristic: 1-minute fixes mention an env var or API key.
                if any(k in hint.lower() for k in ["api key", "env", "_key=", "_api"]):
                    setup_offers.append(
                        {
                            "capability": cap,
                            "tool": entry.get("name"),
                            "provider": entry.get("provider"),
                            "runtime": entry.get("runtime"),
                            "install_instructions": hint,
                        }
                    )

            for entry in bucket.get("available", []) + bucket.get("unavailable", []):
                if entry.get("resource_profile_note"):
                    runtime_warnings.append(
                        f"{entry.get('name')}: {entry.get('resource_profile_note')}"
                    )

        result = {
            "composition_runtimes": comp_runtimes,
            "capabilities": capabilities,
            "setup_offers": setup_offers,
            "runtime_warnings": runtime_warnings,
            # Explicit-only CLI video routes (Grok subscription, OpenArt credits)
            # with observed controls and billing truth. OpenArt rows are pure
            # retained-metadata reads (zero OpenArt CLI calls, no ledger
            # construction, no reservation, no directory creation). Grok rows
            # reuse the existing read-only `--version`/`--help` compatibility
            # probe and never generate media.
            "qualified_cli_video_routes": self.qualified_cli_video_routes(),
            # Keep endpoint support visible even when a provider's ordinary
            # image-to-video route is available through another billing path.
            "pinned_final_frame_routes": [
                {
                    "tool": entry["name"],
                    "provider": entry["provider"],
                    "status": entry["status"],
                    "install_instructions": entry["install_instructions"],
                    **entry["pinned_final_frame"],
                }
                for bucket in menu.values()
                for entry in bucket["available"] + bucket["unavailable"]
                if "pinned_final_frame" in entry
            ],
        }
        # Normalize em-dashes and en-dashes to ASCII so preflight output prints
        # cleanly on Windows cp1252 stdout (the default on Git Bash / PowerShell
        # without PYTHONIOENCODING=utf-8). Agents paste this dict into chat; a
        # mojibake `�` in an install_instructions string looks like a bug.
        # Markdown docs keep their typographic dashes; this only touches the
        # runtime-reported strings.
        return _scrub_unicode_dashes(result)

    def qualified_cli_video_routes(self) -> list[dict[str, Any]]:
        """Truthful compact menu rows for the explicit-only Grok/OpenArt CLIs.

        No row is a recommendation or default; nothing here is benchmarked.
        """
        self.ensure_discovered()
        routes: list[dict[str, Any]] = []
        grok = self._tools.get("grok_cli_video")
        if grok is not None:
            supports = dict(getattr(grok, "supports", {}))
            routes.append(_grok_route(grok))
        openart = self._tools.get("openart_cli_video")
        if openart is not None:
            routes.append(_openart_route(openart))
        return routes

    # Post-hoc fix: narrow helper that keeps the registry output stdout-safe on
    # Windows cp1252 without imposing a new style rule on every tool author.

    def gpu_required_tools(self) -> list[str]:
        """List tools that require GPU (VRAM > 0)."""
        return [
            t.name for t in self._tools.values()
            if t.resource_profile.vram_mb > 0
        ]

    def network_required_tools(self) -> list[str]:
        """List tools that require network access."""
        return [
            t.name for t in self._tools.values()
            if t.resource_profile.network_required
        ]


# Singleton registry instance
registry = ToolRegistry()
