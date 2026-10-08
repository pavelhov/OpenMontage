"""Agent-facing U2 account controls; all provider boundaries are synthetic."""
from __future__ import annotations

import json

from tools import _openart_cli as cli
from tools.openart_account import OpenArtAccount


def _execute(action, **extra):
    return OpenArtAccount().execute({"action": action, "read_only": True, **extra})


def test_status_collect_and_verify_target_original_attempt(tmp_path, monkeypatch):
    from lib import openart_jobs as jobs, production_execution as execution

    attempt = "attempt-123"
    request_digest = "a" * 64
    synthetic_status = {"attempt_id": attempt, "launched": True, "state": "open",
                        "events_sha256": "b" * 64,
                        "collection_job_record_sha256": None, "binding": {},
                        "job_id_sha256": "c" * 64, "output": None,
                        "billing": "unknown", "release_authorized": False}
    monkeypatch.setattr(jobs, "reconcile_job", lambda aid: synthetic_status)
    monkeypatch.setattr(execution, "collect_openart_attempt",
                        lambda project_dir, aid, *, request_sha256, timeout:
                        {"status": "pending", "attempt_id": aid,
                         "request_sha256": request_sha256})
    monkeypatch.setattr(jobs, "load_frozen_request", lambda aid: {"profile": {"source": "fixture"}}, raising=False)
    monkeypatch.setattr(jobs, "verify_collection_receipt",
                        lambda aid, profile: {"attempt_id": aid, "verified": True}, raising=False)

    status = _execute("status", attempt_id=attempt)
    collected = _execute("collect", attempt_id=attempt, project_dir=str(tmp_path),
                         request_sha256=request_digest)
    verified = _execute("verify", attempt_id=attempt)
    assert status.success and status.data["evidence"] == synthetic_status
    assert collected.success and collected.data["evidence"]["status"] == "pending"
    assert verified.success and verified.data["evidence"]["verified"] is True


def test_public_argv_and_results_redact_secrets_recursively(monkeypatch):
    monkeypatch.setattr(cli, "model_cost_argv", lambda model, mode:
                        ["model", "cost", "--model", "sk-secretsecretsecret", "--mode", mode])
    monkeypatch.setattr(cli, "run_readonly", lambda argv, timeout: {
        "argv": list(argv) + ["https://example.test/x?X-Amz-Signature=secret"], "public": {},
        "receipt_id": "receipt", "receipt_sha256": "d" * 64,
        "stdout_sha256": "e" * 64, "parsed": {"nested": ["https://example.test/x?X-Amz-Signature=secret",
                                                                    "sk-secretsecretsecret"]}})
    result = _execute("quote", model="synthetic-model", mode="text2video")
    public = json.dumps(result.data)
    assert result.success
    assert "sk-secretsecretsecret" not in public
    assert "X-Amz-Signature=secret" not in public
    assert "[redacted]" in public


def test_error_messages_redact_secret_argv_and_signed_url(monkeypatch):
    monkeypatch.setattr(cli, "model_cost_argv", lambda model, mode:
                        ["model", "cost", "--model", "sk-secretsecretsecret", "--mode", mode])

    def fail(argv, timeout):
        raise cli.OpenArtCLIError("synthetic_failure", "failed https://example.test/x?X-Amz-Signature=secret "
                                 "token=sk-secretsecretsecret")

    monkeypatch.setattr(cli, "run_readonly", fail)
    result = _execute("quote", model="synthetic", mode="text2video")
    public = json.dumps({"error": result.error, "data": result.data})
    assert not result.success
    assert "X-Amz-Signature=secret" not in public
    assert "sk-secretsecretsecret" not in public


def test_reference_upload_is_qualified_by_jobs_helper(monkeypatch, tmp_path):
    from lib import openart_jobs as jobs

    calls = []
    monkeypatch.setattr(jobs, "upload_reference", lambda root, upload_id, source_path, *, model, mode, timeout:
                        calls.append((root, upload_id, source_path, model, mode, timeout)) or {
                            "upload_id": upload_id, "source_sha256": "f" * 64,
                            "source_size": 3, "url_sha256": "1" * 64,
                            "receipt_id": "opaque", "receipt_sha256": "2" * 64,
                            "account_id_sha256": "3" * 64, "approval_sha256": "4" * 64}, raising=False)
    source = tmp_path / "reference.png"
    source.write_bytes(b"img")
    result = _execute("upload", upload_id="ref-1", source_path=str(source),
                      project_root=str(tmp_path), model="synthetic-model", mode="image2video")
    assert result.success
    assert calls == [(tmp_path, "ref-1", source, "synthetic-model", "image2video", 30.0)]


def test_unqualified_upload_fails_without_cli_or_reservation(monkeypatch, tmp_path):
    from lib import openart_jobs as jobs

    def refuse(*args, **kwargs):
        raise cli.OpenArtCLIError("upload_unqualified", "synthetic qualification absent")

    monkeypatch.setattr(jobs, "upload_reference", refuse, raising=False)
    source = tmp_path / "ref.png"
    source.write_bytes(b"synthetic")
    result = _execute("upload", upload_id="ref-1", source_path=str(source),
                      project_root=str(tmp_path), model="synthetic-model", mode="image2video")
    assert not result.success
    assert "upload_unqualified" in result.error
    assert result.data["reservations"] == 0


def test_list_qualifications_is_pure(monkeypatch):
    from lib import openart_jobs as jobs

    called = []
    monkeypatch.setattr(jobs, "list_qualifications", lambda: called.append(True) or [{"source": "fixture"}], raising=False)
    result = _execute("qualifications")
    assert result.success and result.data["evidence"] == [{"source": "fixture"}]
    assert called == [True]


def test_native_image_dry_run_resolves_private_url_inside_guard_and_redacts(monkeypatch):
    from lib import openart_jobs as jobs

    signed_url = "https://cdn.example.test/private/ref.png?X-Amz-Signature=secret"
    observed = {}
    profile = {"source": "fixture", "model": "synthetic-model", "mode": "image2video",
               "account_id_sha256": "c" * 64}
    monkeypatch.setattr(jobs, "load_qualification", lambda *, model, mode, require:
                        profile if (model, mode) == ("synthetic-model", "image2video") else None,
                        raising=False)

    def retained(upload_id, *, profile, account_id_sha256):
        observed["upload_binding"] = (upload_id, profile, account_id_sha256)
        return signed_url

    monkeypatch.setattr(jobs, "upload_url_for", retained, raising=False)

    def fake_cli(argv, timeout):
        observed["argv"] = list(argv)
        observed["guarded"] = cli._IMAGE_FLAG_ALLOWED.get()
        return {"argv": list(argv) + cli.GLOBAL_FLAGS, "public": {},
                "receipt_id": "opaque", "receipt_sha256": "a" * 64,
                "stdout_sha256": "b" * 64,
                "parsed": {"endpoint": "POST /api/cli/v1/generate",
                           "body": {"model": "synthetic-model", "media": "video",
                                    "mode": "image2video", "params": {"prompt": "synthetic",
                                        "image": signed_url}}}}

    monkeypatch.setattr(cli, "run_readonly", fake_cli)
    result = _execute("native_dry_run", prompt="synthetic", model="synthetic-model",
                      mode="image2video", image_upload_id="opaque-upload")
    public = json.dumps(result.data)
    assert result.success
    assert observed["guarded"] is True
    assert observed["upload_binding"] == ("opaque-upload", profile, "c" * 64)
    assert observed["argv"][-3:] == ["--image", signed_url, "--dry-run"]
    assert "opaque-upload" not in observed["argv"]
    assert "X-Amz-Signature=secret" not in public
    assert "[redacted]" in public


def test_native_image_dry_run_missing_upload_record_makes_zero_cli_calls(monkeypatch):
    from lib import openart_jobs as jobs

    calls = []

    profile = {"source": "fixture", "model": "synthetic-model", "mode": "image2video",
               "account_id_sha256": "c" * 64}
    monkeypatch.setattr(jobs, "load_qualification", lambda *, model, mode, require: profile, raising=False)

    def missing(upload_id, *, profile, account_id_sha256):
        raise cli.OpenArtCLIError("upload_unqualified", "synthetic retained upload absent")

    monkeypatch.setattr(jobs, "upload_url_for", missing, raising=False)
    monkeypatch.setattr(cli, "run_readonly", lambda *args, **kwargs: calls.append(args))
    result = _execute("native_dry_run", prompt="synthetic", model="synthetic-model",
                      mode="image2video", image_upload_id="missing-upload")
    assert not result.success
    assert "upload_unqualified" in result.error
    assert result.data["reservations"] == 0
    assert calls == []


def test_native_image_dry_run_rejects_wrong_profile_before_upload_lookup_or_cli(monkeypatch):
    from lib import openart_jobs as jobs

    calls = []

    def wrong_profile(*, model, mode, require):
        assert (model, mode) == ("requested-model", "image2video")
        raise cli.OpenArtCLIError("generation_unqualified", "synthetic model profile mismatch")

    monkeypatch.setattr(jobs, "load_qualification", wrong_profile, raising=False)
    monkeypatch.setattr(jobs, "upload_url_for", lambda *args, **kwargs: calls.append(("upload", args, kwargs)),
                        raising=False)
    monkeypatch.setattr(cli, "run_readonly", lambda *args, **kwargs: calls.append(("cli", args, kwargs)))
    result = _execute("native_dry_run", prompt="synthetic", model="requested-model",
                      mode="image2video", image_upload_id="upload-for-another-model")
    assert not result.success
    assert result.data["reservations"] == 0
    assert calls == []


def test_setup_actions_dispatch_and_schema_match(monkeypatch):
    from lib import openart_setup as setup, openart_jobs as jobs
    from pathlib import Path
    import jsonschema
    schema=json.loads((Path(__file__).resolve().parents[2]/'schemas/tools/openart_account.schema.json').read_text())
    assert OpenArtAccount.input_schema['properties']==schema['properties']
    calls=[]
    for action, helper, extra in [
        ('qualify_inspection','inspect_qualification',{'json_paths':{}}),
        ('qualify_upload','qualify_upload_guarantee',{'json_paths':{},'guarantee':{},'url_hosts':[]}),
        ('qualify_preview','qualify_preview',{'prompt':'private prompt'}),
    ]:
        monkeypatch.setattr(setup,helper,lambda *args,**kwargs:calls.append((args,kwargs)) or {'profile_sha256':'a'*64,'level':'inspected'})
        inputs={'action':action,'read_only':True,'model':'m1','mode':'image2video',**extra}
        jsonschema.validate(inputs,schema)
        assert OpenArtAccount().execute(inputs).success
        assert calls[-1][0]==('m1','image2video')
    monkeypatch.setattr(jobs,'promote_result_contract',lambda aid,**kwargs:calls.append((aid,kwargs)) or {'result_proof_id':'opaque'},raising=False)
    assert _execute('qualify_result',attempt_id='attempt-1',json_paths={}).success
    assert calls[-1][0]=='attempt-1'
