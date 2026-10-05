"""OpenArt subscription CLI account/control inspector (U1: nonspending only).

Explicit read-only actions: ``inspect`` (version + account), ``quote``
(``model cost --model --mode``), ``form`` (``model form``), ``native_dry_run``
(``generate video ... --dry-run``). No generation, upload, wait, status,
collect or attempt resolution is exposed here; those belong to later units.
"""
from __future__ import annotations

from typing import Any

from tools import _openart_cli as cli
from tools.base_tool import (BaseTool, Determinism, DependencyError, ExecutionMode,
                             ToolResult, ToolRuntime, ToolStability, ToolTier)

READ_ONLY_ACTIONS = ("inspect", "quote", "form", "native_dry_run", "readiness")
DELEGATED_ACTIONS = ("status", "collect", "resolve_attempt", "submit", "upload")


class OpenArtAccount(BaseTool):
    name = "openart_account"
    version = "0.1.0"
    tier = ToolTier.ANALYZE  # inspection; never governed as generation
    capability = "provider_account"
    provider = "openart"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC if hasattr(Determinism, "STOCHASTIC") else Determinism.DETERMINISTIC
    runtime = ToolRuntime.API
    dependencies = ["cmd:openart"]
    install_instructions = ("Install the official OpenArt CLI (github.com/OpenArt-AI/cli) to PATH or "
                            "~/.local/bin/openart, or set OPENART_CLI_PATH; authenticate with `openart` OAuth.")
    side_effects = ["runs read-only openart CLI commands", "writes private receipts outside the checkout"]
    best_for = ["inspecting OpenArt subscription CLI account, model forms and nonspending quotes"]
    not_good_for = ["generating media (not enabled in U1)"]
    input_schema = {
        "type": "object", "required": ["action", "read_only"],
        "properties": {
            "action": {"type": "string", "enum": [*READ_ONLY_ACTIONS, *DELEGATED_ACTIONS]},
            "read_only": {"const": True},
            "model": {"type": "string"}, "mode": {"type": "string"},
            "prompt": {"type": "string"}, "duration": {"type": "integer"},
            "aspect_ratio": {"type": "string"}, "resolution": {"type": "string"},
            "probe": {"type": "boolean"}, "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 300},
        },
    }

    def check_dependencies(self) -> None:
        try:
            cli.resolve_binary()
        except cli.OpenArtCLIError as exc:
            raise DependencyError(f"{exc.message}. {self.install_instructions}")

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    def _ok(self, action: str, evidence: dict) -> ToolResult:
        return ToolResult(success=True, cost_usd=0.0, data={
            "action": action, "provider": "openart", "reservations": 0, "paid_submission": False,
            "generation_enabled": False, "evidence": evidence})

    @staticmethod
    def _call(argv: list[str], inputs: dict) -> dict:
        out = cli.run_readonly(argv, timeout=cli.validate_timeout(inputs.get("timeout_seconds")))
        return {"argv": out["argv"], "public": out["public"], "receipt_id": out["receipt_id"],
                "receipt_sha256": out["receipt_sha256"], "stdout_sha256": out["stdout_sha256"],
                "_parsed": out["parsed"]}

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        action = inputs.get("action")
        if action in DELEGATED_ACTIONS:
            return ToolResult(success=False, error=f"openart_account action {action!r} is not available in U1")
        if action not in READ_ONLY_ACTIONS:
            return ToolResult(success=False, error=f"unknown openart_account action {action!r}")
        if inputs.get("read_only") is not True:
            return ToolResult(success=False, error="openart_account requires explicit read_only: true")
        try:
            return self._dispatch(action, inputs)
        except cli.OpenArtCLIError as exc:
            return ToolResult(success=False, error=f"{exc.kind}: {cli.redact_text(exc.message)}",
                              data={"error": exc.public(), "reservations": 0, "paid_submission": False})

    def _dispatch(self, action: str, inputs: dict) -> ToolResult:
        if action == "readiness":
            return self._ok(action, cli.readiness(probe=bool(inputs.get("probe"))))
        if action == "inspect":
            version = self._call(["version"], inputs)
            account = self._call(["account"], inputs)
            for part in (version, account):
                part.pop("_parsed")
            return self._ok(action, {"version": version, "account": account,
                                     "qualification": "unqualified_account_identity"})
        if action == "quote":
            call = self._call(cli.model_cost_argv(inputs.get("model"), inputs.get("mode")), inputs)
            call.pop("_parsed")
            return self._ok(action, {**call, "kind": "model_cost", "model": inputs["model"],
                                     "mode": inputs["mode"], "covers_settings": False,
                                     "qualification": "unqualified_for_dispatch"})
        if action == "form":
            call = self._call(cli.model_form_argv(inputs.get("model"), inputs.get("mode")), inputs)
            parsed = call.pop("_parsed")
            try:
                controls, shape = cli.form_controls(parsed), "json_schema"
            except cli.OpenArtCLIError:
                controls, shape = None, "unqualified"
            return self._ok(action, {**call, "controls": controls, "form_shape": shape,
                                     "qualification": "unqualified_for_dispatch"})
        # native_dry_run
        argv = cli.native_dry_run_argv(inputs.get("prompt"), model=inputs.get("model"), mode=inputs.get("mode"),
                                       duration=inputs.get("duration"), aspect_ratio=inputs.get("aspect_ratio"),
                                       resolution=inputs.get("resolution"))
        call = self._call(argv, inputs)
        parsed = call.pop("_parsed")
        try:
            request = cli.dry_run_request(parsed)
            request = {"endpoint": request["endpoint"], "body_sha256": request["body_sha256"],
                       "body": cli.redact(request["body"])}
        except cli.OpenArtCLIError as exc:
            request = {"error": exc.public()}
        return self._ok(action, {**call, "request": request, "qualification": "unqualified_for_dispatch"})
