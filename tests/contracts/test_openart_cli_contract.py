"""U5 contract: truthful, read-only qualified CLI video menu (Grok + OpenArt)."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess

import pytest

from lib import openart_jobs
from lib import provider_credit_ledger as ledger_mod
from tools.tool_registry import ToolRegistry


@pytest.fixture()
def private_state(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "openart-state"
    monkeypatch.setenv("OPENMONTAGE_OPENART_STATE_DIR", str(root))
    monkeypatch.setenv("OPENART_CLI_PATH", str(tmp_path.resolve() / "missing-openart"))
    # Hermetic: a real local Grok install would run its non-generating --version probe.
    monkeypatch.setenv("GROK_CLI_PATH", str(tmp_path.resolve() / "missing-grok"))
    for name in ("OPENMONTAGE_ALLOW_NETWORK", "HYPERFRAMES_QA", "HYPERFRAMES_QA_RENDER"):
        monkeypatch.delenv(name, raising=False)
    return root


@pytest.fixture()
def no_side_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("menu must not spawn, construct the ledger or reserve")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(ledger_mod.CreditLedger, "__init__", forbidden)
    monkeypatch.setattr(openart_jobs.cli, "_run_checked", forbidden, raising=False)


def _routes(registry: ToolRegistry) -> dict[str, dict]:
    return {row["provider"]: row for row in registry.qualified_cli_video_routes()}


def test_summary_exposes_qualified_routes(private_state, monkeypatch):
    monkeypatch.setattr(ledger_mod.CreditLedger, "__init__",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ledger constructed")))
    summary = ToolRegistry().provider_menu_summary()
    providers = [row["provider"] for row in summary["qualified_cli_video_routes"]]
    assert providers == ["grok_cli", "openart_cli"]


_DEFAULT_ROWS = (("held", "oa", "submitted", "unresolved"), ("prep", "oa", "prepared", "reserved"),
                 ("done", "oa", "closed", "settled"), ("foreign", "other", "submitted", "unresolved"))


def _seed_ledger(root, rows=_DEFAULT_ROWS, quarantine=True, outbox=True):
    root.mkdir(mode=0o700)
    credits = root / "credits"
    credits.mkdir(mode=0o700)
    path = credits / "ledger.sqlite3"
    db = sqlite3.connect(path)
    for ddl in ledger_mod._SCHEMA:
        db.execute(ddl)
    db.execute(f"PRAGMA user_version={ledger_mod.SCHEMA_VERSION}")
    oa_acct = '["openart_cli","h","ws"]'
    other_acct = '["other_cli","x","ws"]'
    db.execute("INSERT INTO accounts VALUES(?,'1',100,100,100,?,1)", (oa_acct, "a" * 64))
    db.execute("INSERT INTO accounts VALUES(?,'1',100,100,100,?,1)", (other_acct, "a" * 64))
    accts = {"oa": oa_acct, "other": other_acct}
    for attempt, acct_name, slot, debit in rows:
        acct = accts[acct_name]
        db.execute(
            "INSERT INTO reservations(attempt_id,account_key,authorization_occurrence,allowance_id,binding_json,"
            "validation_sha256,reserved_units,ceiling_units,slot_state,debit_state) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (attempt, acct, attempt, "al", "{}", "b" * 64, 10, 10, slot, debit))
    if quarantine:
        db.execute("INSERT INTO account_quarantine VALUES('[\"openart_cli\",\"h\"]','drift')")
        db.execute("INSERT INTO account_quarantine VALUES('[\"other_cli\",\"x\"]','drift')")
    for event, attempt in ((("e1", "held"), ("e2", "foreign")) if outbox else ()):
        db.execute("INSERT INTO outbox(event_id,attempt_id,kind,evidence_sha256) VALUES(?,?,'charge',?)",
                   (event, attempt, "c" * 64))
    db.commit()
    db.close()
    os.chmod(path, 0o600)


def test_menu_lists_grok_and_openart_with_separate_billing(private_state, no_side_effects):
    routes = _routes(ToolRegistry())
    grok, openart = routes["grok_cli"], routes["openart_cli"]
    assert grok["billing"]["kind"] == "subscription_quota_unknown"
    assert grok["model_policy"] == "cli_managed_media_unreported"
    assert grok["models"] == [] and grok["model_selection"] == "not_supported"
    assert "grok-4.6" not in str(grok)  # pinned agent model is never a video model
    assert grok["billing"]["estimated_cost_usd"] is None
    assert grok["controls"]["first_last_frame"] and grok["controls"]["native_audio"]
    assert grok["controls"]["operation_specific"] is True
    assert grok["status_probe"] == "readonly_cli_version_and_help"
    billing = openart["billing"]
    assert (billing["kind"], billing["billing_unit"], billing["current_quote_required"],
            billing["usd_cost_status"], billing["estimated_cost_usd"]) == (
        "credits", "credits", True, "unknown", None)
    assert billing["remaining_allowance"] == "unknown_until_current_quote"
    # No advertised guess: controls stay unqualified until a verified full profile.
    assert openart["controls"]["native_controls"] == "not_yet_qualified"
    assert "resolution" in openart["controls"]["required_unqualified"]
    assert openart["controls"]["first_last_frame"] is False
    assert openart["controls"]["native_audio"] is False
    assert openart["controls"]["multiple_reference_images"] is False
    assert openart["account_stages"]["account_discovery"] == "pending"
    assert openart["account_stages"]["fresh_refresh_required_before_dispatch"] is True
    assert openart["dispatch_readiness"]["ready"] is False
    assert grok["recommendation"] is None and openart["recommendation"] is None


def test_missing_state_is_reported_empty_and_never_created(private_state, no_side_effects):
    openart = _routes(ToolRegistry())["openart_cli"]
    assert not private_state.exists()
    assert openart["models"] == [] and openart["production_available"] is False
    assert openart["ledger"]["initialized"] is False and openart["ledger"]["error"] is None


def test_only_full_real_profiles_are_available(private_state, no_side_effects, monkeypatch):
    base = {"cli_version": "0.1.1", "tier": "subscription", "profile_sha256": "p" * 64,
            "result_contract_sha256": None, "result_proof_id": None, "image2video_qualified": False}
    rows = [
        {**base, "model": "full-model", "mode": "text2video", "source": "real", "valid": True, "level": "full",
         "error": None, "result_contract_sha256": "r" * 64, "result_proof_id": "proof"},
        {**base, "model": "pre-model", "mode": "image2video", "source": "real", "valid": False,
         "level": "pre_submit", "error": "result_contract_unqualified"},
        {**base, "model": "insp-model", "mode": "text2video", "source": "real", "valid": False,
         "level": "inspected", "error": "result_contract_unqualified"},
        {**base, "model": "fixture-model", "mode": "text2video", "source": "fixture", "valid": False,
         "level": "none", "error": "fixture_profile"},
    ]
    monkeypatch.setattr(openart_jobs, "list_qualifications", lambda: [dict(r) for r in rows])
    openart = _routes(ToolRegistry())["openart_cli"]
    assert [m["model"] for m in openart["models"]] == ["full-model"]
    candidates = {c["model"]: c for c in openart["qualification_candidates"]}
    assert set(candidates) == {"pre-model", "insp-model"}
    assert all(c["production_available"] is False and c["purpose"] == "qualification_candidate"
               for c in candidates.values())
    assert candidates["pre-model"]["next_stage"] == "first_original_result_qualification"
    assert [r["model"] for r in openart["not_live"]] == ["fixture-model"]
    # Binary missing: a full profile alone does not make the route production-available.
    assert openart["status"] == "unavailable" and openart["production_available"] is False
    assert openart["account_stages"]["account_discovery"] == "full_profile_retained"
    assert "tool_status:unavailable" in openart["dispatch_readiness"]["blockers"]


def test_account_discovery_reflects_retained_staged_rows(private_state, no_side_effects, monkeypatch):
    row = {"model": "m", "mode": "text2video", "source": "real", "valid": False, "level": "inspected",
           "error": "result_contract_unqualified", "cli_version": "0.1.1", "profile_sha256": "p" * 64}
    monkeypatch.setattr(openart_jobs, "list_qualifications", lambda: [dict(row)])
    openart = _routes(ToolRegistry())["openart_cli"]
    assert openart["account_stages"]["account_discovery"] == "inspected_profile_retained"
    assert openart["models"] == []
    assert "no_full_real_result_qualified_profile" in openart["dispatch_readiness"]["blockers"]


def test_openart_ledger_facts_block_dispatch_readiness_even_with_full_profile(
        private_state, no_side_effects, monkeypatch):
    _seed_ledger(private_state)
    full = {"model": "m", "mode": "text2video", "source": "real", "valid": True, "level": "full",
            "error": None, "cli_version": "0.1.1", "profile_sha256": "p" * 64,
            "result_contract_sha256": "r" * 64, "result_proof_id": "proof"}
    monkeypatch.setattr(openart_jobs, "list_qualifications", lambda: [dict(full)])
    openart = _routes(ToolRegistry())["openart_cli"]
    assert [m["model"] for m in openart["models"]] == ["m"]
    blockers = openart["dispatch_readiness"]["blockers"]
    for expected in ("pending_openart_attempts_need_reconciliation", "unknown_job_acceptance_unresolved",
                     "openart_account_quarantined", "unacknowledged_openart_outbox"):
        assert expected in blockers
    holds = openart["billing"]["holds"]
    assert [h["attempt_id"] for h in holds["unknown_billing"]] == ["held"]
    assert [h["attempt_id"] for h in holds["pending_reservation"]] == ["prep"]


def test_terminal_slot_unknown_billing_hold_is_eligible_but_needs_quote(
        private_state, no_side_effects, monkeypatch):
    # Job accepted and terminal; only the debit is unresolved. That is a retained
    # billing hold (economics incomplete), not an unknown job-acceptance block.
    _seed_ledger(private_state, rows=(("billed", "oa", "terminal", "unresolved"),
                                      ("closedhold", "oa", "closed", "unresolved")),
                 quarantine=False, outbox=False)
    full = {"model": "m", "mode": "text2video", "source": "real", "valid": True, "level": "full",
            "error": None, "cli_version": "0.1.1", "profile_sha256": "p" * 64,
            "result_contract_sha256": "r" * 64, "result_proof_id": "proof"}
    monkeypatch.setattr(openart_jobs, "list_qualifications", lambda: [dict(full)])
    openart = _routes(ToolRegistry())["openart_cli"]
    blockers = openart["dispatch_readiness"]["blockers"]
    assert "unknown_job_acceptance_unresolved" not in blockers
    assert "pending_openart_attempts_need_reconciliation" not in blockers
    assert not any("billing" in b or "hold" in b for b in blockers)
    billing = openart["billing"]
    assert {h["attempt_id"] for h in billing["holds"]["unknown_billing"]} == {"billed", "closedhold"}
    assert all(h["job_acceptance_unknown"] is False for h in billing["holds"]["unknown_billing"])
    assert billing["economics"] == "incomplete_unknown_billing_hold"
    assert billing["remaining_allowance_required"] is True
    assert billing["current_quote_required"] is True
    assert "current_quote" in openart["dispatch_readiness"]["requires"]


def test_existing_ledger_snapshot_reports_pending_holds_quarantine_outbox(private_state, no_side_effects):
    _seed_ledger(private_state)
    openart = _routes(ToolRegistry())["openart_cli"]
    ledger = openart["ledger"]
    assert ledger["error"] is None and ledger["initialized"] is True
    # Only OpenArt-scoped facts; the foreign provider's rows never leak in.
    assert {p["attempt_id"] for p in ledger["pending"]} == {"held", "prep"}
    assert {h["attempt_id"] for h in ledger["holds"]} == {"held", "prep"}
    assert {q["account"]["account_id_sha256"] for q in ledger["quarantine"]} == {"h"}
    assert len(ledger["quarantine"]) == 2  # account flag + claim quarantine, both OpenArt
    assert [a["workspace"] for a in ledger["accounts"]] == ["ws"]
    assert ledger["unacknowledged_outbox"] == 1


def test_unsafe_ledger_state_is_reported_not_raised(private_state, no_side_effects):
    _seed_ledger(private_state)
    os.chmod(private_state / "credits" / "ledger.sqlite3", 0o644)
    openart = _routes(ToolRegistry())["openart_cli"]
    assert openart["ledger"]["error"] and "ledger_snapshot_error" in openart["errors"]
    assert openart["production_available"] is False


# --- Actual retained-receipt catalog (real-format qualified profile) ---------
from tools.video.openart_cli_video import OpenArtCLIVideo  # noqa: E402
from tests.integration.test_openart_dispatch_recovery import governed  # noqa: E402,F401


@pytest.mark.parametrize("name,value", [
    ("prompt", "secret"), ("prompt", "16:9"), ("image", "abc"),
    ("image", "https://up.openart.test/start.svg?sig=private"), ("unknownParam", "1"),
    ("duration", True), ("duration", float("nan")), ("duration", "8"),
    ("aspectRatio", "secret"), ("resolution", "https://a"), ("resolution", 720),
])
def test_safe_value_hashes_everything_outside_creative_allowlist(name, value):
    out = OpenArtCLIVideo._safe_value(name, value)
    assert out["value_redacted"] is True and "value" not in out and len(out["value_sha256"]) == 64


@pytest.mark.parametrize("name,value", [("duration", 8), ("aspectRatio", "16:9"), ("resolution", "720p")])
def test_safe_value_keeps_creative_controls(name, value):
    assert OpenArtCLIVideo._safe_value(name, value) == {"value": value}


def _tree(root):
    return sorted((str(p.relative_to(root)), p.stat().st_size) for p in root.rglob("*"))


def test_actual_full_profile_catalog_reads_reference_receipts(governed, monkeypatch):
    from lib import openart_dispatch as dispatch  # noqa: F401
    root, inputs, profile, tmp = governed
    # Real fake-CLI chain: original qualification job, then first-result promotion.
    result = OpenArtCLIVideo().execute(inputs)
    attempt = result.data["production_attempt_id"]
    monkeypatch.setenv("FAKE_STATUS", "done")
    openart_jobs.promote_result_contract(attempt, json_paths={"url_hosts": ["cdn.openart.test"]})
    full = openart_jobs.load_qualification(model="m1", mode="image2video", require="full")
    assert full["profile_sha256"] == profile["profile_sha256"]  # promoted unchanged
    # Profiles keep reference-only receipts, never parsed blobs.
    assert all(set(e) == {"kind", "receipt_id", "receipt_sha256"} for e in full["captured_receipts"])
    state = openart_jobs.cli.state_dir(create=False)
    before, calls_before = _tree(state), (tmp / "calls").read_text()

    def forbidden(*a, **k):
        raise AssertionError("catalog must not spawn/construct ledger")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(ledger_mod.CreditLedger, "__init__", forbidden)
    monkeypatch.setattr(openart_jobs.cli, "_run_checked", forbidden, raising=False)
    catalog = OpenArtCLIVideo._model_catalog()
    mode = catalog["m1"]["modes"]["image2video"]
    assert mode["profile_sha256"] == full["profile_sha256"] and mode["source"] == "real"
    assert mode["account_id_sha256"] == full["account_id_sha256"]
    native = mode["native_controls"]
    assert native["duration"]["preview"] == {"value": 8}
    assert native["duration"]["form_default"] == {"value": 8}
    assert native["aspectRatio"]["preview"] == {"value": "16:9"}
    assert native["resolution"]["preview"] == {"value": "720p"}
    assert native["prompt"]["preview"]["value_redacted"] is True
    assert native["image"]["preview"]["value_redacted"] is True
    assert mode["required_unqualified"] == [] and mode["argv_flag_without_effective_preview"] == []
    text = json.dumps(catalog)
    assert inputs["prompt"] not in text and "sig=private" not in text and "https://" not in text
    # Route row: per-model controls, profile-account scope, fresh refresh still required.
    row = _routes(ToolRegistry())["openart_cli"]
    model = row["models"][0]
    assert model["catalog_verified"] is True and model["controls"]["native_controls"] == native
    scope = row["dispatch_readiness"]["account_scope"]
    assert scope == {"kind": "verified_profile_accounts", "account_id_sha256": [full["account_id_sha256"]]}
    assert row["dispatch_readiness"]["fresh_prelaunch_refresh_required"] is True
    assert row["account_stages"]["fresh_refresh_required_before_dispatch"] is True
    assert inputs["prompt"] not in json.dumps(row)
    assert _tree(state) == before and (tmp / "calls").read_text() == calls_before


def test_tampered_reference_receipt_drops_catalog_and_blocks_readiness(governed, monkeypatch):
    root, inputs, profile, tmp = governed
    attempt = OpenArtCLIVideo().execute(inputs).data["production_attempt_id"]
    monkeypatch.setenv("FAKE_STATUS", "done")
    openart_jobs.promote_result_contract(attempt, json_paths={"url_hosts": ["cdn.openart.test"]})
    ref = next(e for e in profile["captured_receipts"] if e["kind"] == "form")
    path = openart_jobs.cli.receipt_path(ref["receipt_id"])
    path.write_bytes(path.read_bytes() + b" ")
    assert OpenArtCLIVideo._model_catalog() == {}
    row = _routes(ToolRegistry())["openart_cli"]
    if row["models"]:  # full row may still list, but never verified/ready
        assert row["models"][0]["catalog_verified"] is False
        assert "full_profile_receipts_unverified" in row["dispatch_readiness"]["blockers"]
        assert row["production_available"] is False


def test_argv_resolution_flag_without_effective_preview_is_unqualified(monkeypatch):
    """Unit seam: resolution in argv but absent from the effective preview body."""
    row = {"model": "m", "mode": "text2video", "source": "real", "valid": True, "level": "full",
           "profile_sha256": "p" * 64, "result_contract_sha256": "r" * 64, "result_proof_id": "x"}
    prof = {"profile_sha256": "p" * 64, "source": "real", "dry_run_endpoint": "POST /g",
            "account_id_sha256": "a" * 64,
            "captured_receipts": [{"kind": "form", "receipt_id": "f", "receipt_sha256": "1" * 64},
                                  {"kind": "dry_run", "receipt_id": "d", "receipt_sha256": "2" * 64}]}
    recs = {"form": {"parsed": {"type": "object", "properties": {
                "prompt": {"type": "string"}, "resolution": {"type": "string", "enum": ["720p", "1080p"]}},
                "required": ["prompt", "resolution"]}},
            "dry_run": {"argv": ["generate", "video", "x", "--resolution", "1080p", "--dry-run"],
                        "parsed": {"endpoint": "POST /g", "body": {"model": "m", "media": "video",
                                   "mode": "text2video", "params": {"prompt": "x"}}}}}
    monkeypatch.setattr(openart_jobs, "list_qualifications", lambda: [dict(row)])
    monkeypatch.setattr(openart_jobs, "load_qualification", lambda **k: dict(prof))
    monkeypatch.setattr(openart_jobs, "_qual_record", lambda entry, name, kind: recs[name])
    mode = OpenArtCLIVideo._model_catalog()["m"]["modes"]["text2video"]
    assert mode["argv_flag_without_effective_preview"] == ["resolution"]
    assert mode["required_unqualified"] == ["resolution"]
    assert "preview" not in mode["native_controls"]["resolution"]
    assert mode["native_controls"]["resolution"]["enum"] == [{"value": "720p"}, {"value": "1080p"}]


def test_malformed_enum_members_are_hashed_not_disclosed(monkeypatch):
    """A resolution enum carrying a signed URL/private string must not leak via the menu."""
    row = {"model": "m", "mode": "text2video", "source": "real", "valid": True, "level": "full",
           "profile_sha256": "p" * 64, "result_contract_sha256": "r" * 64, "result_proof_id": "x"}
    prof = {"profile_sha256": "p" * 64, "source": "real", "dry_run_endpoint": "POST /g",
            "captured_receipts": [{"kind": "form", "receipt_id": "f", "receipt_sha256": "1" * 64},
                                  {"kind": "dry_run", "receipt_id": "d", "receipt_sha256": "2" * 64}]}
    recs = {"form": {"parsed": {"type": "object", "properties": {
                "prompt": {"type": "string"},
                "duration": {"type": "integer", "enum": [5, 8]},
                "aspectRatio": {"type": "string", "enum": ["16:9", "9:16"]},
                "resolution": {"type": "string",
                               "enum": ["720p", "https://cdn.x/a?sig=SECRET", "private-token"]}},
                "required": ["prompt"]}},
            "dry_run": {"argv": ["generate", "video", "x", "--dry-run"],
                        "parsed": {"endpoint": "POST /g", "body": {"model": "m", "media": "video",
                                   "mode": "text2video", "params": {"prompt": "x", "resolution": "720p"}}}}}
    monkeypatch.setattr(openart_jobs, "list_qualifications", lambda: [dict(row)])
    monkeypatch.setattr(openart_jobs, "load_qualification", lambda **k: dict(prof))
    monkeypatch.setattr(openart_jobs, "_qual_record", lambda entry, name, kind: recs[name])
    catalog = OpenArtCLIVideo._model_catalog()
    text = json.dumps(catalog)
    assert "SECRET" not in text and "private-token" not in text and "https://" not in text
    native = catalog["m"]["modes"]["text2video"]["native_controls"]
    res = native["resolution"]["enum"]
    assert res[0] == {"value": "720p"}
    assert all(m.get("value_redacted") is True and len(m["value_sha256"]) == 64 for m in res[1:])
    assert native["duration"]["enum"] == [{"value": 5}, {"value": 8}]
    assert native["aspectRatio"]["enum"] == [{"value": "16:9"}, {"value": "9:16"}]
