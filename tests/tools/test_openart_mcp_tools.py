"""Wrapper contract checks; all connector responses here are synthetic."""

from __future__ import annotations

from pathlib import Path

import json

from lib import openart_mcp as mcp
from lib import openart_mcp_dispatch as dispatch
from tools.openart_mcp_account import OpenArtMCPAccount
from tools.video.openart_mcp_video import OpenArtMCPVideo


def test_video_prepare_forwards_the_complete_native_generation_input(monkeypatch):
    source = {
        "project_dir": "/tmp/project", "governance": {"scope_id": "s", "shot_id": "a", "stage": "generate"},
        "operation": "reference_to_video", "model": "model-x", "mode": "ref2video",
        "prompt": "prompt", "native_params": {"duration": 8, "audio": True},
        "input_assets": [{"role": "last_frame", "source_path": "/tmp/end.png", "source_sha256": "a" * 64, "upload_id": "end"}],
    }
    seen = []
    monkeypatch.setattr(dispatch, "invoke", lambda inputs: seen.append(inputs) or {
        "attempt_id": "original", "status": "prepared", "native_body_sha256": "b" * 64,
    })

    result = OpenArtMCPVideo().execute(source)

    assert result.success
    assert seen == [source]
    assert result.data["attempt_id"] == "original"
    assert result.data["next"] == {"action": "begin", "tool": "openart_mcp_account"}
    assert result.data["prepared_for_connector_handoff"] is True
    assert result.data["generation_enabled"] is False
    assert result.cost_usd is None
    assert OpenArtMCPVideo.retry_policy.max_retries == 0


def test_video_reports_governance_error_without_retry(monkeypatch):
    def reject(_inputs):
        raise mcp.OpenArtMCPError("approval_required", "no current approval")

    monkeypatch.setattr(dispatch, "invoke", reject)
    result = OpenArtMCPVideo().execute({})
    assert not result.success
    assert result.data["error"] == {"kind": "approval_required", "message": "no current approval"}
    assert OpenArtMCPVideo.retry_policy.max_retries == 0


def test_video_get_info_and_selector_hook_share_exact_native_catalog(monkeypatch):
    catalog = {
        "model-x": {"display_name": "Model X", "modes": {
            "text2video": {"production_ready": False, "profile_status": "candidate",
                "operation": "text_to_video", "native_capabilities": {
                    "roles": {"first_frame": {"supported": False}, "last_frame": {"supported": False}},
                    "params": {"audio": {"binding": "native_param"}}, "unreachable": [],
                }},
        }},
    }
    monkeypatch.setattr(mcp, "model_catalog", lambda: catalog)
    monkeypatch.setattr(mcp, "video_catalog", lambda: [{"model": "model-x", "mode": "text2video"}])
    monkeypatch.setattr(mcp, "account_summary", lambda: {"credits_observed": None})

    tool = OpenArtMCPVideo()
    info = tool.get_info()

    assert tool._model_catalog() is catalog
    assert info["model_catalog"] is catalog
    assert tool.supports["explicit_selection_only"] is True
    assert tool.supports["native_audio"] is False
    assert info["supports"]["native_audio"] is False
    assert info["supports"]["conditional_model_modes"]["native_audio"] == []


def test_video_catalog_is_unavailable_without_leaking_fixture_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENMONTAGE_OPENART_STATE_DIR", str(tmp_path / "cli-only-state"))
    monkeypatch.delenv("OPENMONTAGE_OPENART_MCP_DISCOVERY_DIR", raising=False)

    def isolated_error():
        raise mcp.OpenArtMCPError("fixture_not_isolated", "synthetic observations require isolated discovery and state directories")

    monkeypatch.setattr(mcp, "model_catalog", isolated_error)
    info = OpenArtMCPVideo().get_info()

    assert info["status"] == "unavailable"
    assert info["model_catalog"] == {}
    assert info["generation_enabled"] is False
    assert info["catalog_status"] == "unavailable"
    assert info["catalog_unavailable_reason"].startswith("fixture_not_isolated:")


def test_account_records_original_connector_structured_content_once(monkeypatch):
    raw = {"historyId": "provider-original", "status": "PENDING", "opaque": "keep exact object"}
    calls = []

    def record(action, project_dir, attempt_id, **kwargs):
        calls.append((action, project_dir, attempt_id, kwargs))
        return {"attempt_id": attempt_id, "status": "submitted"}

    monkeypatch.setattr(dispatch, "invoke_action", record)
    result = OpenArtMCPAccount().execute({
        "action": "receive", "project_dir": "/tmp/project", "attempt_id": "original", "outcome": raw,
    })

    assert result.success
    assert len(calls) == 1
    assert calls[0] == ("receive", "/tmp/project", "original", {"outcome": raw})


def test_account_download_requests_only_the_retained_original_attempt(monkeypatch):
    receipt = {"attempt_id": "original", "downloaded_path": "/tmp/private/original.mp4",
               "history_id": "provider-history", "resource_id": "resource-1",
               "resource_url_sha256": "a" * 64, "file_sha256": "b" * 64}
    calls = []
    monkeypatch.setattr(dispatch, "invoke_action", lambda *args, **kwargs: calls.append((args, kwargs)) or receipt)

    result = OpenArtMCPAccount().execute({
        "action": "download", "project_dir": "/tmp/project", "attempt_id": "original",
    })

    assert result.success
    assert calls == [(("download", "/tmp/project", "original"), {})]
    assert result.data["downloaded_path"] == receipt["downloaded_path"]
    assert OpenArtMCPAccount.resource_profile.network_required is True


def test_account_lists_attempts_without_inventing_an_attempt_id(monkeypatch):
    calls = []
    monkeypatch.setattr(dispatch, "invoke_action", lambda *args, **kwargs: calls.append((args, kwargs)) or [])
    result = OpenArtMCPAccount().execute({"action": "list", "project_dir": "/tmp/project"})
    assert result.success
    assert calls == [(('list', '/tmp/project', None), {})]


def test_account_rejects_unknown_or_ignored_fields():
    tool = OpenArtMCPAccount()
    unknown = tool.execute({"action": "receive", "project_dir": "/tmp/p", "attempt_id": "a",
                            "outcome": {}, "retry": True})
    assert not unknown.success
    assert unknown.data["error"]["kind"] == "invalid_argument"

    ignored = tool.execute({"action": "catalog", "attempt_id": "a"})
    assert not ignored.success
    assert ignored.data["error"]["kind"] == "invalid_argument"


def test_json_schemas_are_strict_and_document_the_connector_handoff():
    root = Path(__file__).resolve().parents[2]
    video = json.loads((root / "schemas/tools/openart_mcp_video.schema.json").read_text())
    account = json.loads((root / "schemas/tools/openart_mcp_account.schema.json").read_text())
    assert video["additionalProperties"] is False
    assert account["additionalProperties"] is False
    assert "authority" not in video["properties"]
    assert "billing_authority" not in account["properties"]
    receive = next(rule["then"] for rule in account["allOf"]
                   if rule.get("if", {}).get("properties", {}).get("action", {}).get("const") == "receive")
    assert "oneOf" in receive
    assert video == OpenArtMCPVideo.input_schema
    assert account == OpenArtMCPAccount.input_schema
