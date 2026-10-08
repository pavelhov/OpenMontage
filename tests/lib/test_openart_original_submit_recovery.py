"""Original-only staged submit recovery against fake CLI subprocesses; no live provider calls."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from lib import openart_dispatch as dispatch, openart_jobs as jobs
from tools import _openart_cli as cli
from tools.openart_account import OpenArtAccount
from tools.video.openart_cli_video import OpenArtCLIVideo
from tests.lib.test_openart_jobs import (staged, staged_launch, staged_calls, _fake_download)  # noqa: F401
from tests.integration.test_openart_dispatch_recovery import governed  # noqa: F401
from tests.integration.test_openart_unknown_cost_workflow import workflow, submissions  # noqa: F401


def lost_launch(st, monkeypatch, *, raw=None, rc=0, metadata=None, purpose=jobs.QUALIFICATION_PURPOSE):
    from tests.lib import test_openart_jobs as staged_tests
    monkeypatch.setitem(staged_tests.STAGED_PATHS, "submit_job_id", "creationId")
    binary = Path(cli.resolve_binary())
    text = binary.read_text().replace('out = {"job": "job-1"}',
                                     'out = json.loads(os.environ["SUBMIT_RESPONSE"])')
    text = text.replace('print(json.dumps(out))',
                        'print(os.environ.get("RAW_SUBMIT", json.dumps(out)) if "--async" in args else json.dumps(out))\n'
                        'if "--async" in args: raise SystemExit(int(os.environ.get("SUBMIT_RC", "0")))')
    binary.write_text(text)
    response = {"historyId": "job-1", "model": "m1", "mode": "image2video", "media": "video", "status": "PENDING"}
    response.update(metadata or {})
    monkeypatch.setenv("SUBMIT_RESPONSE", json.dumps(response))
    monkeypatch.setenv("SUBMIT_RC", str(rc))
    if raw is not None:
        monkeypatch.setenv("RAW_SUBMIT", raw)
    profile, out, project = staged_launch(st, purpose=purpose)
    assert out["status"] == "hold_unknown_job"
    return profile, project


def recover(**kwargs):
    return jobs.recover_original_submit("qa-1", json_paths={"submit_job_id": "historyId"}, timeout=20, **kwargs)


def observed_history_result():
    """Synthetic values with the safely observed official history/resources field structure."""
    return {"history": {"id": "job-1", "status": "completed"}, "resources": [{
        "createdAt": 1234567890, "generation": {"historyId": "job-1"}, "id": "resource-private-1",
        "resourceType": "video", "sourceType": "generation", "status": "completed",
        "thumbnailUrl": "https://cdn.openart.ai/private-thumbnail.jpg?sig=private-thumbnail",
        "url": "https://cdn.openart.ai/private-video.mp4?sig=private-video"}]}


def install_result_response(monkeypatch, response):
    binary = Path(cli.resolve_binary())
    text = binary.read_text().replace('print(os.environ.get("RAW_SUBMIT", json.dumps(out))',
        'if args[:2] == ["creation", "get"]: out = json.loads(os.environ["RESULT_RESPONSE"])\n'
        'print(os.environ.get("RAW_SUBMIT", json.dumps(out))')
    binary.write_text(text)
    monkeypatch.setenv("RESULT_RESPONSE", json.dumps(response))


def test_observed_history_result_recovers_original_once_and_keeps_resource_ids_private(staged, monkeypatch):
    profile, _ = lost_launch(staged, monkeypatch)
    install_result_response(monkeypatch, observed_history_result())
    result = OpenArtAccount().execute({"action": "recover_original_submit", "read_only": True,
        "attempt_id": "qa-1", "json_paths": {"submit_job_id": "historyId", "result_job_id": "history.id"}})
    assert result.success, result.error
    evidence = result.data["evidence"]
    assert evidence["status"] == "original_job_recovered"
    assert evidence["json_paths"] == {"submit_job_id": "historyId", "result_job_id": "history.id"}
    assert evidence["observed_statuses"] == [{"path": "history.status", "value": "completed"},
                                             {"path": "resources.0.status", "value": "completed"}]
    assert evidence["url_hosts"] == [{"path": "resources.0.thumbnailUrl", "host": "cdn.openart.ai"},
                                      {"path": "resources.0.url", "host": "cdn.openart.ai"}]
    assert {"path": "resources.0.createdAt", "type": "number"} in evidence["fields"]
    public = json.dumps(result.data)
    assert not any(private in public for private in ("job-1", "resource-private-1", "https://", "private-video", "private-thumbnail"))
    assert jobs.original_job_id("qa-1") == "job-1"
    assert evidence["billing"] == "unknown" and evidence["release_authorized"] is False
    promoted = jobs.promote_result_contract("qa-1", json_paths={"status": "history.status",
        "status_terminal_ok": "completed", "status_terminal_fail": "failed", "urls": "resources.0.url",
        "url_hosts": ["cdn.openart.ai"]})
    assert promoted["level"] == "full"
    assert jobs.load_qualification(model="m1", mode="image2video", require="full") == profile
    assert sum("--async" in c for c in staged_calls(staged)) == 1


@pytest.mark.parametrize("change,declaration", [("wrong_history", "history.id"),
    ("resource_echo_only", "history.id"), ("request_echo_only", "history.id"),
    ("multiple_eligible", "history.id"), ("resource_id_declaration", "resources.0.id"),
    ("resource_generation_declaration", "resources.0.generation.historyId"),
    ("resource_url_declaration", "resources.0.url")])
def test_observed_history_shape_requires_unique_original_history_id(staged, monkeypatch, change, declaration):
    lost_launch(staged, monkeypatch)
    response = observed_history_result()
    if change == "wrong_history":
        response["history"]["id"] = "foreign-job"
    elif change == "resource_echo_only":
        del response["history"]["id"]
    elif change == "request_echo_only":
        del response["history"]["id"]
        response["resources"] = []
        response["request"] = {"history": {"id": "job-1"}}
    elif change == "multiple_eligible":
        response["result"] = {"id": "job-1"}
    elif change == "resource_id_declaration":
        response["resources"][0]["id"] = "job-1"
    install_result_response(monkeypatch, response)
    result = OpenArtAccount().execute({"action": "recover_original_submit", "read_only": True,
        "attempt_id": "qa-1", "json_paths": {"submit_job_id": "historyId", "result_job_id": declaration}})
    assert result.success and result.data["evidence"]["status"] == "hold_unknown_job"
    assert jobs.original_job_id("qa-1") is None
    assert not (jobs.job_dir("qa-1") / "submit_recovery.json").exists()
    assert sum("--async" in c for c in staged_calls(staged)) == 1


def test_original_once_recovers_promotes_collects_without_changing_frozen_profile(staged, monkeypatch):
    profile, project = lost_launch(staged, monkeypatch)
    origin = {name: (jobs.job_dir("qa-1") / name).read_bytes()
              for name in ("launch.json", "frozen_request.json")}
    recovered = recover()
    assert recovered["status"] == "original_job_recovered"
    assert recovered["json_paths"] == {"submit_job_id": "historyId", "result_job_id": "job"}
    assert recovered["billing"] == "unknown" and recovered["release_authorized"] is False
    assert jobs.original_job_id("qa-1") == "job-1"
    assert jobs._verify_raw_submit("qa-1", jobs.launch_record("qa-1"), "job-1", "bad")
    with pytest.raises(cli.OpenArtCLIError):
        jobs.effective_result_contract("qa-1")
    promoted = jobs.promote_result_contract("qa-1", timeout=20)
    assert promoted["level"] == "full"
    assert jobs.load_qualification(model="m1", mode="image2video", require="full") == profile
    assert jobs.effective_result_contract("qa-1")["json_paths"]["submit_job_id"] == "historyId"
    _fake_download(monkeypatch)
    assert jobs.collect_job("qa-1", output_path=project / "qa-1.mp4", output_root=project,
                            profile=profile, timeout=20)["status"] == "collected"
    assert jobs.verify_collection_receipt("qa-1", profile)["output"]["sha256"]
    assert all((jobs.job_dir("qa-1") / name).read_bytes() == raw for name, raw in origin.items())
    assert sum("--async" in c for c in staged_calls(staged)) == 1


def test_registered_action_returns_only_hash_shape_enums_and_hosts(staged, monkeypatch):
    lost_launch(staged, monkeypatch)
    result = OpenArtAccount().execute({"action": "recover_original_submit", "read_only": True,
                                      "attempt_id": "qa-1", "json_paths": {"submit_job_id": "historyId"},
                                      "timeout_seconds": 20})
    assert result.success, result.error
    evidence = result.data["evidence"]
    assert evidence["observed_statuses"] == [{"path": "status", "value": "done"}]
    assert evidence["url_hosts"] == [{"path": "urls.0", "host": "cdn.example.test"}]
    public = json.dumps(result.data)
    assert not any(private in public for private in ("job-1", "private-account", "https://", "sig=1"))
    assert "recover_original_submit" in OpenArtAccount.input_schema["properties"]["action"]["enum"]


def test_registered_recovery_transport_errors_do_not_publish_raw_diagnostics(staged, monkeypatch):
    lost_launch(staged, monkeypatch)
    original = cli.run_readonly
    def fail(argv, **kw):
        if argv[:2] == ["creation", "get"]:
            raise cli.OpenArtCLIError("nonzero_exit", "provider rejected job-1 for private-account",
                                      {"stderr": "job-1 private-account https://cdn.example.test/private?sig=secret"})
        return original(argv, **kw)
    monkeypatch.setattr(cli, "run_readonly", fail)
    result = OpenArtAccount().execute({"action": "recover_original_submit", "read_only": True,
                                      "attempt_id": "qa-1", "json_paths": {"submit_job_id": "historyId"}})
    assert result.success is False
    assert not any(value in json.dumps(result.data) + result.error for value in
                   ("job-1", "private-account", "https://", "sig=secret"))
    assert jobs.original_job_id("qa-1") is None


def test_recovery_repeated_and_restart_publication_is_idempotent(staged, monkeypatch):
    lost_launch(staged, monkeypatch)
    original_write = cli.write_private
    def crash(path, data):
        if Path(path).name == "job_id":
            raise RuntimeError("synthetic crash after proof and parsed publication")
        return original_write(path, data)
    with monkeypatch.context() as patch:
        patch.setattr(cli, "write_private", crash)
        with pytest.raises(RuntimeError, match="synthetic crash"):
            recover()
    assert not (jobs.job_dir("qa-1") / "job_id").exists()
    proof = (jobs.job_dir("qa-1") / "submit_recovery.json").read_bytes()
    calls_before = staged_calls(staged)
    first, second = recover(), recover()
    assert first["submit_recovery_sha256"] == second["submit_recovery_sha256"]
    assert staged_calls(staged)[len(calls_before):] == [["account"], ["creation", "get", "job-1"]] * 2
    assert (jobs.job_dir("qa-1") / "submit_recovery.json").read_bytes() == proof
    assert len([e for e in jobs.read_events("qa-1") if e.get("type") == "parsed"]) == 1


def test_restart_before_parsed_publication_rechecks_current_account(staged, monkeypatch):
    lost_launch(staged, monkeypatch)
    original_append = jobs.append_event
    def crash(aid, event):
        if event.get("type") == "parsed":
            raise RuntimeError("synthetic crash before parsed publication")
        return original_append(aid, event)
    with monkeypatch.context() as patch:
        patch.setattr(jobs, "append_event", crash)
        with pytest.raises(RuntimeError, match="synthetic crash"):
            recover()
    assert (jobs.job_dir("qa-1") / "submit_recovery.json").is_file()
    assert jobs.original_job_id("qa-1") is None
    monkeypatch.setenv("ACCOUNT", "foreign-account")
    with pytest.raises(cli.OpenArtCLIError) as exc:
        recover()
    assert exc.value.kind == "account_mismatch"
    assert jobs.original_job_id("qa-1") is None
    monkeypatch.delenv("ACCOUNT")
    assert recover()["status"] == "original_job_recovered"
    assert len([e for e in jobs.read_events("qa-1") if e.get("type") == "parsed"]) == 1


def test_replay_observes_current_terminal_schema_without_rewriting_recovery(staged, monkeypatch):
    lost_launch(staged, monkeypatch)
    monkeypatch.setenv("STATUS", "PENDING")
    first = recover()
    proof_bytes = (jobs.job_dir("qa-1") / "submit_recovery.json").read_bytes()
    monkeypatch.setenv("STATUS", "COMPLETED")
    current = recover()
    assert first["observed_statuses"] == [{"path": "status", "value": "PENDING"}]
    assert current["observed_statuses"] == [{"path": "status", "value": "COMPLETED"}]
    assert current["submit_recovery_sha256"] == first["submit_recovery_sha256"]
    assert (jobs.job_dir("qa-1") / "submit_recovery.json").read_bytes() == proof_bytes


@pytest.mark.parametrize("change", ["job_file", "parsed_event", "nonzero", "duplicate_exit", "wrong_pid",
                                    "raw_hash", "stderr", "launch", "frozen", "marker"])
def test_forged_or_conflicting_original_evidence_refuses_before_readonly_probe(staged, monkeypatch, change):
    profile, _ = lost_launch(staged, monkeypatch)
    directory = jobs.job_dir("qa-1")
    if change == "job_file":
        cli.write_private(directory / "job_id", b"forged-job")
    elif change == "parsed_event":
        jobs.append_event("qa-1", {"type": "parsed", "job_id_sha256": "f" * 64})
    elif change in {"nonzero", "duplicate_exit", "wrong_pid"}:
        path = directory / "events.jsonl"
        events = jobs.read_events("qa-1")
        exited = next(e for e in events if e["type"] == "exited")
        if change == "nonzero":
            exited["returncode"] = 1
        elif change == "wrong_pid":
            exited["pid"] += 1
        else:
            events.append(exited)
        path.write_bytes(b"".join(jobs._canon(e) + b"\n" for e in events))
    elif change in {"raw_hash", "stderr"}:
        with (directory / ("submit.stdout" if change == "raw_hash" else "submit.stderr")).open("ab") as fh:
            fh.write(b" ")
    elif change in {"launch", "frozen"}:
        name = "launch.json" if change == "launch" else "frozen_request.json"
        path = directory / name
        value = json.loads(path.read_bytes())
        value["purpose" if change == "launch" else "version"] = "forged"
        path.write_bytes(jobs._canon(value))
    elif change == "marker":
        path = next((cli.state_dir() / "qualification" / "m1" / "image2video" / "attempts").glob("*.json"))
        value = json.loads(path.read_bytes()); value["reservation_id"] = "other"
        path.write_bytes(jobs._canon(value))
    calls_before = staged_calls(staged)
    with pytest.raises(cli.OpenArtCLIError):
        recover()
    assert staged_calls(staged) == calls_before
    assert not (directory / "submit_recovery.json").exists()


@pytest.mark.parametrize("raw", ['{"historyId":"job-1","historyId":"job-2"}',
                                  '{"historyId":["job-1","job-2"]}',
                                  '{"historyId":"job-1"}\n{"historyId":"job-2"}',
                                  '{"historyId":"job-1","value":NaN}',
                                  '{"historyId":"job-1","value":1e309}'])
def test_ambiguous_original_submit_never_probes_or_acknowledges(staged, monkeypatch, raw):
    lost_launch(staged, monkeypatch, raw=raw)
    before = staged_calls(staged)
    with pytest.raises(cli.OpenArtCLIError):
        recover()
    assert staged_calls(staged) == before
    assert jobs.original_job_id("qa-1") is None


@pytest.mark.parametrize("metadata", [{"model": "other"}, {"mode": "text2video"}, {"media": "image"}])
def test_original_protocol_must_match_frozen_native(staged, monkeypatch, metadata):
    lost_launch(staged, monkeypatch, metadata=metadata)
    before = staged_calls(staged)
    with pytest.raises(cli.OpenArtCLIError):
        recover()
    assert staged_calls(staged) == before


@pytest.mark.parametrize("path", ["model", "mode", "media", "status", "state", "request.historyId", "echo.id"])
def test_protocol_or_echo_submit_paths_cannot_become_job_identity(staged, monkeypatch, path):
    lost_launch(staged, monkeypatch)
    before = staged_calls(staged)
    with pytest.raises(cli.OpenArtCLIError):
        jobs.recover_original_submit("qa-1", json_paths={"submit_job_id": path})
    assert staged_calls(staged) == before
    assert jobs.original_job_id("qa-1") is None


@pytest.mark.parametrize("raw", ['{"historyId":"PENDING","status":"PENDING"}',
                                  '{"historyId":"m1"}',
                                  '{"historyId":"job-1","echo":{"id":"job-1"}}'])
def test_candidate_unique_and_distinct_from_native_protocol_values(staged, monkeypatch, raw):
    lost_launch(staged, monkeypatch, raw=raw)
    before = staged_calls(staged)
    with pytest.raises(cli.OpenArtCLIError):
        recover()
    assert staged_calls(staged) == before


@pytest.mark.parametrize("response,path", [({"model": "job-1"}, "model"),
    ({"status": "job-1"}, "status"), ({"request": {"historyId": "job-1"}}, "request.historyId"),
    ({"echo": {"id": "job-1"}}, "echo.id"), ({"error": {"id": "job-1"}}, "error.id"),
    ({"billing": {"job": "job-1"}}, "billing.job"),
    ({"id": "job-1", "creation": {"id": "job-1"}}, "id")])
def test_result_protocol_echo_and_ambiguous_identity_never_acknowledge(staged, monkeypatch, response, path):
    lost_launch(staged, monkeypatch)
    binary = Path(cli.resolve_binary())
    text = binary.read_text().replace('print(os.environ.get("RAW_SUBMIT", json.dumps(out))',
        'if args[:2] == ["creation", "get"]: out = json.loads(os.environ["RESULT_RESPONSE"])\n'
        'print(os.environ.get("RAW_SUBMIT", json.dumps(out))')
    binary.write_text(text)
    monkeypatch.setenv("RESULT_RESPONSE", json.dumps(response))
    if "model" in response:
        with pytest.raises(cli.OpenArtCLIError):
            jobs.recover_original_submit("qa-1", json_paths={"submit_job_id": "historyId", "result_job_id": path})
    else:
        out = jobs.recover_original_submit("qa-1", json_paths={"submit_job_id": "historyId", "result_job_id": path})
        assert out["status"] == "hold_unknown_job"
    assert jobs.original_job_id("qa-1") is None


def test_promoted_actual_submit_path_applies_to_new_separately_authorized_ordinary_launch(staged, monkeypatch):
    profile, project = lost_launch(staged, monkeypatch)
    recover(); jobs.promote_result_contract("qa-1")
    original_reservation = jobs.get_active_reservation(project, "qa-1", "r" * 64)
    inputs = dict(jobs.load_frozen_request("qa-1")["inputs"], output_path=str(project / "ordinary-2.mp4"))
    native = jobs.prepare_native_request(inputs, profile)
    snapshot = jobs.freeze_request("ordinary-2", inputs, native, profile)
    binding = dict(jobs.launch_record("qa-1")["binding"], attempt_id="ordinary-2", reservation_id="res-ordinary",
                   snapshot_sha256=snapshot["snapshot_sha256"])
    ordinary = {"state": "active", "attempt_id": "ordinary-2", "reservation_id": "res-ordinary",
                "request_sha256": "r" * 64, "purpose": "ordinary", "authorization_occurrence_id": "occ-ordinary"}
    jobs.register_reservation_lookup(lambda root, aid, req: original_reservation if aid == "qa-1"
                                    else ordinary if aid == "ordinary-2" else None)
    monkeypatch.setenv("SUBMIT_RESPONSE", json.dumps({"historyId": "job-2", "model": "m1",
                                                     "mode": "image2video", "media": "video", "status": "PENDING"}))
    out = jobs.launch_submit(project, binding, native, profile, wait_timeout=20)
    assert out["status"] == "submitted"
    assert jobs.original_job_id("ordinary-2") == "job-2"
    assert jobs._verify_raw_submit("ordinary-2", jobs.launch_record("ordinary-2"), "job-2", "bad")
    assert profile["json_paths"]["submit_job_id"] == "creationId"
    assert sum("--async" in c for c in staged_calls(staged)) == 2


def test_cross_account_never_reads_original_job(staged, monkeypatch):
    lost_launch(staged, monkeypatch)
    monkeypatch.setenv("ACCOUNT", "foreign-account")
    before = staged_calls(staged)
    with pytest.raises(cli.OpenArtCLIError) as exc:
        recover()
    assert exc.value.kind == "account_mismatch"
    assert staged_calls(staged)[len(before):] == [["account"]]
    assert jobs.original_job_id("qa-1") is None


def test_wrong_returned_job_keeps_hold_and_returns_safe_observed_shape(staged, monkeypatch):
    lost_launch(staged, monkeypatch)
    monkeypatch.setenv("RESULT_JOB", "foreign-job")
    out = recover()
    assert out["status"] == "hold_unknown_job"
    assert {"path": "job", "type": "string"} in out["fields"]
    assert jobs.original_job_id("qa-1") is None
    assert not (jobs.job_dir("qa-1") / "submit_recovery.json").exists()
    assert "foreign-job" not in json.dumps(out)
    monkeypatch.delenv("RESULT_JOB")
    assert recover()["status"] == "original_job_recovered"
    assert sum("--async" in c for c in staged_calls(staged)) == 1


def test_proof_and_promotion_refuse_conflicting_path_declarations(staged, monkeypatch):
    lost_launch(staged, monkeypatch)
    recover()
    before = staged_calls(staged)
    for paths in ({"submit_job_id": "creationId"}, {"submit_job_id": "historyId", "result_job_id": "id"}):
        with pytest.raises(cli.OpenArtCLIError):
            jobs.recover_original_submit("qa-1", json_paths=paths)
    with pytest.raises(cli.OpenArtCLIError):
        jobs.promote_result_contract("qa-1", json_paths={"submit_job_id": "creationId"})
    assert staged_calls(staged) == before


@pytest.mark.parametrize("file", ["submit.stdout", "submit_recovery.json", "job_id"])
def test_replay_and_promotion_reject_changed_original_bytes(staged, monkeypatch, file):
    lost_launch(staged, monkeypatch)
    recover()
    with (jobs.job_dir("qa-1") / file).open("ab") as fh:
        fh.write(b" ")
    with pytest.raises(cli.OpenArtCLIError):
        recover()
    with pytest.raises(cli.OpenArtCLIError):
        jobs.promote_result_contract("qa-1")


def test_full_proof_replay_does_not_recreate_a_missing_qualification_marker_directory(staged, monkeypatch):
    profile, _ = lost_launch(staged, monkeypatch)
    recover(); jobs.promote_result_contract("qa-1")
    folder = cli.state_dir() / "qualification" / "m1" / "image2video" / "attempts"
    for marker in folder.iterdir():
        marker.unlink()
    folder.rmdir()
    before = staged_calls(staged)
    with pytest.raises(cli.OpenArtCLIError):
        jobs.load_result_proof(profile)
    assert not folder.exists()
    assert staged_calls(staged) == before


def test_real_ledger_original_one_launch_recovery_leaves_billing_and_slot_held(governed, monkeypatch):
    root, inputs, profile, tmp = governed
    binary = Path(cli.resolve_binary())
    text = binary.read_text().replace("out={'job':{'id':job}}",
        "out={'historyId':job,'model':'m1','mode':'image2video','media':'video','status':'PENDING'}")
    binary.write_text(text)
    result = OpenArtCLIVideo().execute(inputs)
    aid = result.data["production_attempt_id"]
    row = dispatch.ledger().inspect(aid)
    assert row["slot_state"] == "uncertain" and not row["job_id"]
    before = {name: (jobs.job_dir(aid) / name).read_bytes() for name in ("launch.json", "frozen_request.json", "credit_dispatch.json")}
    # Billing echoes cannot become job identity; only the unique actual result ID is eligible.
    probe = jobs.recover_original_submit(aid, json_paths={"submit_job_id": "historyId"})
    assert probe["status"] == "original_job_recovered"
    assert probe["json_paths"]["result_job_id"] == "creation.id"
    recovered = jobs.recover_original_submit(aid, json_paths={"submit_job_id": "historyId", "result_job_id": "creation.id"})
    assert recovered["status"] == "original_job_recovered"
    assert dispatch.ledger().inspect(aid) == row  # recovery neither settles nor releases
    dispatch.record_launch_result(aid)
    acknowledged = dispatch.ledger().inspect(aid)
    assert acknowledged["slot_state"] == "submitted"
    assert acknowledged["debit_state"] == row["debit_state"]
    monkeypatch.setenv("FAKE_STATUS", "done")
    promoted = jobs.promote_result_contract(aid, json_paths={"url_hosts": ["cdn.openart.test"]})
    assert promoted["level"] == "full" and promoted["profile_sha256"] == profile["profile_sha256"]
    assert jobs.effective_result_contract(aid)["json_paths"]["submit_job_id"] == "historyId"
    assert all((jobs.job_dir(aid) / name).read_bytes() == raw for name, raw in before.items())
    assert len((tmp / "paid").read_text().splitlines()) == 1


def test_exact_original_pixverse_unknown_cost_registered_one_launch_recovery(workflow, monkeypatch):
    root, inputs, profile, tmp, tool, packet = workflow
    binary = Path(cli.resolve_binary())
    binary.write_text(binary.read_text().replace("out={'job':{'id':'fixture-original'}}",
        "out={'historyId':'fixture-original','model':'pixverseV6','media':'video','mode':'text2video','status':'PENDING'}"))
    assert packet["duration"] == 1 and packet["aspect_ratio"] == "16:9" and packet["resolution"] == "720p"
    result = tool.execute(inputs)
    aid = result.data["production_attempt_id"]
    row = dispatch.ledger().inspect_unpriced(aid)
    assert row["slot_state"] == "uncertain"
    response = OpenArtAccount().execute({"action": "recover_original_submit", "read_only": True,
                                        "attempt_id": aid, "json_paths": {"submit_job_id": "historyId"}})
    assert response.success, response.error
    assert response.data["evidence"]["json_paths"] == {"submit_job_id": "historyId", "result_job_id": "creation.id"}
    assert dispatch.ledger().inspect_unpriced(aid) == row
    monkeypatch.setenv("FAKE_STATUS", "done")
    refreshed = OpenArtAccount().execute({"action": "recover_original_submit", "read_only": True,
                                         "attempt_id": aid, "json_paths": {"submit_job_id": "historyId"}})
    assert refreshed.data["evidence"]["observed_statuses"] == [{"path": "creation.status", "value": "done"}]
    assert refreshed.data["evidence"]["url_hosts"] == [{"path": "creation.urls.0", "host": "cdn.openart.test"}]
    promoted = OpenArtAccount().execute({"action": "qualify_result", "read_only": True, "attempt_id": aid,
                                        "json_paths": {"url_hosts": ["cdn.openart.test"]}})
    assert promoted.success, promoted.error
    assert jobs.load_qualification(model="pixverseV6", mode="text2video", require="full") == profile
    assert submissions(tmp) == ["fixture-original"]
    assert dispatch.ledger().inspect_unpriced(aid)["billing_state"] == "unknown"


def _ordinary_proof_path(profile):
    return jobs.result_proof_path(profile["model"], profile["mode"], profile["profile_sha256"])


@pytest.mark.parametrize("stale_proof", [False, True])
def test_ordinary_lost_launch_recovers_and_collects_original_without_result_proof(staged, monkeypatch, stale_proof):
    profile, project = lost_launch(staged, monkeypatch, purpose="ordinary")
    assert jobs.launch_record("qa-1")["purpose"] == "ordinary"
    origin = {name: (jobs.job_dir("qa-1") / name).read_bytes()
              for name in ("launch.json", "frozen_request.json")}
    if stale_proof:  # Invalid optional evidence never disables ordinary original recovery.
        cli.write_private(_ordinary_proof_path(profile), b'{"stale": true}')
    recovered = recover()
    assert recovered["status"] == "original_job_recovered"
    assert recovered["billing"] == "unknown" and recovered["release_authorized"] is False
    assert jobs.original_job_id("qa-1") == "job-1"
    proof = json.loads((jobs.job_dir("qa-1") / "submit_recovery.json").read_bytes())
    assert proof["origin"]["purpose"] == "ordinary"
    assert proof["origin"]["qualification_marker_sha256"] is None
    effective = jobs.effective_result_contract("qa-1")
    assert effective["result_contract_sha256"] is None
    assert effective["json_paths"]["submit_job_id"] == "historyId"
    assert effective["json_paths"]["result_job_id"] == recovered["json_paths"]["result_job_id"]
    assert {k: v for k, v in effective["json_paths"].items() if k not in ("submit_job_id", "result_job_id")} \
        == {k: v for k, v in profile["json_paths"].items() if k not in ("submit_job_id", "result_job_id")}
    _fake_download(monkeypatch)
    out = jobs.collect_job("qa-1", output_path=project / "qa-1.mp4", output_root=project,
                           profile=profile, timeout=20)
    assert out["status"] == "collected", out
    assert jobs.verify_collection_receipt("qa-1", profile)["output"]["sha256"]
    again = jobs.collect_job("qa-1", output_path=project / "qa-1.mp4", output_root=project,
                             profile=profile, timeout=20)
    assert again["status"] == "collected"
    assert all((jobs.job_dir("qa-1") / name).read_bytes() == raw for name, raw in origin.items())
    assert profile["json_paths"]["submit_job_id"] == "creationId"
    assert jobs.launch_record("qa-1")["profile"] == profile
    with pytest.raises(cli.OpenArtCLIError):
        jobs.load_qualification(model="m1", mode="image2video", require="full")
    with pytest.raises(cli.OpenArtCLIError):
        jobs.promote_result_contract("qa-1", timeout=20)
    assert os.path.lexists(_ordinary_proof_path(profile)) is stale_proof
    assert sum("--async" in c for c in staged_calls(staged)) == 1


def test_ordinary_recovery_rejects_cross_account(staged, monkeypatch):
    lost_launch(staged, monkeypatch, purpose="ordinary")
    monkeypatch.setenv("ACCOUNT", "foreign-account")
    with pytest.raises(cli.OpenArtCLIError) as exc:
        recover()
    assert exc.value.kind == "account_mismatch"
    assert jobs.original_job_id("qa-1") is None


def test_ordinary_recovery_holds_foreign_job(staged, monkeypatch):
    lost_launch(staged, monkeypatch, purpose="ordinary")
    monkeypatch.setenv("RESULT_JOB", "foreign-job")
    assert recover()["status"] == "hold_unknown_job"
    assert jobs.original_job_id("qa-1") is None
    assert not (jobs.job_dir("qa-1") / "submit_recovery.json").exists()


@pytest.mark.parametrize("authority", [jobs.QUALIFICATION_PURPOSE, "forged"])
def test_ordinary_recovery_requires_reservation_purpose_equal_frozen_launch(staged, monkeypatch, authority):
    _, project = lost_launch(staged, monkeypatch, purpose="ordinary")
    reservation = dict(jobs.get_active_reservation(project, "qa-1", "r" * 64), purpose=authority)
    jobs.register_reservation_lookup(lambda root, aid, req: dict(reservation) if aid == "qa-1" else None)
    before = staged_calls(staged)
    with pytest.raises(cli.OpenArtCLIError):
        recover()
    assert staged_calls(staged) == before
    assert jobs.original_job_id("qa-1") is None


def test_qualification_launch_rejects_ordinary_reservation_authority(staged, monkeypatch):
    _, project = lost_launch(staged, monkeypatch)
    reservation = dict(jobs.get_active_reservation(project, "qa-1", "r" * 64), purpose="ordinary")
    jobs.register_reservation_lookup(lambda root, aid, req: dict(reservation) if aid == "qa-1" else None)
    with pytest.raises(cli.OpenArtCLIError):
        recover()
    assert jobs.original_job_id("qa-1") is None


def test_ordinary_tampered_recovery_holds_collection(staged, monkeypatch):
    profile, project = lost_launch(staged, monkeypatch, purpose="ordinary")
    recover()
    path = jobs.job_dir("qa-1") / "submit_recovery.json"
    value = json.loads(path.read_bytes())
    value["json_paths"]["result_job_id"] = "id"
    path.write_bytes(jobs._canon(value))
    _fake_download(monkeypatch)
    out = jobs.collect_job("qa-1", output_path=project / "qa-1.mp4", output_root=project,
                           profile=profile, timeout=20)
    assert out["status"] == "hold"
    with pytest.raises(cli.OpenArtCLIError):
        jobs.effective_result_contract("qa-1")
    assert sum("--async" in c for c in staged_calls(staged)) == 1


def test_diagnostic_recovery_origin_keeps_retained_v1_shape(staged, monkeypatch):
    """Retained v1 diagnostic recovery proofs (no purpose key) stay valid without rewrites."""
    lost_launch(staged, monkeypatch)
    recover()
    path = jobs.job_dir("qa-1") / "submit_recovery.json"
    raw = path.read_bytes()
    origin = json.loads(raw)["origin"]
    assert "purpose" not in origin and origin["qualification_marker_sha256"]
    assert set(origin) == {"attempt_id", "launch_sha256", "snapshot_sha256", "origin_profile_sha256",
                           "account_id_sha256", "submit_stdout_sha256", "submit_stderr_sha256",
                           "original_events_sha256", "reservation_binding_sha256", "dispatch_sha256",
                           "qualification_marker_sha256"}
    assert jobs._load_submit_recovery("qa-1", "bad")[1] == hashlib.sha256(raw).hexdigest()
    assert recover()["status"] == "original_job_recovered"
    assert path.read_bytes() == raw


def _ordinary_generation_authority(root):
    """Rewrite the fixture's synthetic credit authorization to ordinary generation (helper pattern)."""
    from lib import openart_credit as credit
    from tests.lib.test_production_request import write
    scope = json.loads((root / "production_scopes.json").read_text())["scopes"][0]
    auth = json.loads((root / "artifacts/credit_authorization-credit.json").read_text())
    auth.update(purpose="generation")
    auth["occurrences"][0].update(id="ordinary-generation-synthetic-approval")
    terms = {k: v for k, v in auth.items() if k != "evidence"}
    raw = json.dumps({"kind": "openart_credit_authorization", "terms": terms}, sort_keys=True).encode()
    (root / "credit-approval.json").write_bytes(raw)
    auth["evidence"] = {"path": "credit-approval.json", "sha256": hashlib.sha256(raw).hexdigest()}
    scope["credit_authorization_sha256"] = credit.credit_authorization_digest(auth)
    write(root / "artifacts/credit_authorization-credit.json", auth)
    write(root / "production_scopes.json", {"version": "1.0", "scopes": [scope]})


def test_real_ledger_ordinary_lost_launch_recovers_and_collects_original_without_result_proof(governed, monkeypatch):
    from lib import production_execution as execution
    root, inputs, profile, tmp = governed
    _ordinary_generation_authority(root)
    binary = Path(cli.resolve_binary())
    # historyId shift on submit; result URL stays on the frozen profile's declared host.
    binary.write_text(binary.read_text().replace("out={'job':{'id':job}}",
        "out={'historyId':job,'model':'m1','mode':'image2video','media':'video','status':'PENDING'}")
        .replace("https://cdn.openart.test/result.mp4", "https://cdn.example.test/result.mp4"))
    result = OpenArtCLIVideo().execute(inputs)
    aid = result.data["production_attempt_id"]
    assert jobs.launch_record(aid)["purpose"] == "ordinary"
    assert not os.path.lexists(_ordinary_proof_path(profile))
    row = dispatch.ledger().inspect(aid)
    assert row["slot_state"] == "uncertain" and not row["job_id"]
    before = {name: (jobs.job_dir(aid) / name).read_bytes()
              for name in ("launch.json", "frozen_request.json", "credit_dispatch.json")}
    probe = jobs.recover_original_submit(aid, json_paths={"submit_job_id": "historyId"})
    assert probe["status"] == "original_job_recovered"
    assert probe["json_paths"]["result_job_id"] == "creation.id"
    recovered = jobs.recover_original_submit(aid, json_paths={"submit_job_id": "historyId", "result_job_id": "creation.id"})
    assert recovered["status"] == "original_job_recovered"
    origin = json.loads((jobs.job_dir(aid) / "submit_recovery.json").read_bytes())["origin"]
    assert origin["purpose"] == "ordinary" and origin["qualification_marker_sha256"] is None
    assert dispatch.ledger().inspect(aid) == row  # recovery neither settles nor releases
    dispatch.record_launch_result(aid)
    acknowledged = dispatch.ledger().inspect(aid)
    assert acknowledged["slot_state"] == "submitted" and acknowledged["debit_state"] == row["debit_state"]
    effective = jobs.effective_result_contract(aid)
    assert effective["result_contract_sha256"] is None
    assert effective["json_paths"]["submit_job_id"] == "historyId"
    monkeypatch.setenv("FAKE_STATUS", "done")
    request_sha = dispatch._manifest(aid)[1].request_sha256
    _fake_download(monkeypatch)  # offline bytes; host matches the frozen profile's cdn.example.test
    collected = execution.collect_openart_attempt(root, aid, request_sha256=request_sha)
    assert collected["status"] == "generated", collected
    assert jobs.verify_collection_receipt(aid, profile)["output"]["sha256"]
    assert execution.collect_openart_attempt(root, aid, request_sha256=request_sha) == collected
    assert dispatch.ledger().inspect(aid)["debit_state"] == row["debit_state"]  # never spuriously released
    assert all((jobs.job_dir(aid) / name).read_bytes() == raw for name, raw in before.items())
    assert jobs.launch_record(aid)["profile"] == profile
    assert profile["json_paths"]["submit_job_id"] == "job.id"
    assert not os.path.lexists(_ordinary_proof_path(profile))
    with pytest.raises(cli.OpenArtCLIError):
        jobs.load_qualification(model="m1", mode="image2video", require="full")
    assert len((tmp / "paid").read_text().splitlines()) == 1
