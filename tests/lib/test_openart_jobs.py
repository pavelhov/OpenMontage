"""U2 OpenArt async job lifecycle: submit-once, hold, collect, verify. No real provider calls."""
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from lib import openart_jobs as jobs
from tools import _openart_cli as cli
from tools._openart_cli import OpenArtCLIError

FAKE = r'''#!{python}
import json, os, sys
log = os.environ.get("FAKE_OPENART_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps(sys.argv[1:]) + "\n")
mode = os.environ.get("FAKE_OPENART_MODE", "ok")
args = sys.argv[1:]
if args[:1] == ["account"]:
    print(json.dumps({{"user": {{"id": os.environ.get("FAKE_ACCOUNT", "acct-1")}}}}))
elif args[:2] == ["creation", "get"]:
    print(json.dumps({{"creation": {{"id": os.environ.get("FAKE_RESULT_JOB", args[2]),
        "status": os.environ.get("FAKE_STATUS", "done"),
        "urls": ["https://cdn.openart.test/v.mp4?sig=1"]}}}}))
elif args[:2] == ["upload", "add"]:
    print(json.dumps({{"contract": {{"nonspending": True, "no_delayed_charge": True}},
                      "url": "https://up.openart.test/r.png"}}))
elif "--async" in args:
    if mode == "nojob":
        print("{{}}")
    else:
        print(json.dumps({{"job": {{"id": "job-123"}}}}))
else:
    print("{{}}")
'''

ACCOUNT_SHA = hashlib.sha256(b"acct-1").hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    binary = tmp_path / "bin" / "openart"
    binary.parent.mkdir()
    binary.write_text(FAKE.format(python=sys.executable))
    binary.chmod(0o755)
    monkeypatch.setenv("OPENART_CLI_PATH", str(binary))
    monkeypatch.setenv("OPENMONTAGE_OPENART_STATE_DIR", str(tmp_path / "state"))
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_OPENART_LOG", str(log))
    monkeypatch.delenv("OPENMONTAGE_OPENART_OFFLINE", raising=False)
    monkeypatch.setattr(jobs, "_ALLOW_FIXTURE_LAUNCH", False)
    jobs.register_reservation_lookup(None)
    jobs.register_account_check(None)
    jobs.register_upload_approval_lookup(None)
    yield {"tmp": tmp_path, "log": log}
    jobs.register_reservation_lookup(None)
    jobs.register_account_check(None)
    jobs.register_upload_approval_lookup(None)


def calls(env):
    return [json.loads(l) for l in env["log"].read_text().splitlines()] if env["log"].exists() else []


def receipt(parsed, argv):
    stem = f"2026-10-05T000000-{hashlib.sha256(json.dumps([parsed, argv]).encode()).hexdigest()[:8]}"
    data = json.dumps({"argv": argv, "parsed": parsed}, sort_keys=True).encode()
    cli.write_private(cli.state_dir() / "receipts" / f"{stem}.json", data)
    return stem, hashlib.sha256(data).hexdigest()


def fixture_profile(model="m-turbo", mode="text2video", **extra):
    prof = {"version": "1", "source": "fixture", "cli_version": "0.1.1", "account_id_sha256": ACCOUNT_SHA,
            "model": model, "mode": mode, "form_sha256": "f" * 64, "form_defaults": {"duration": 5},
            "tier": "subscription", "dry_run_endpoint": "POST /api/cli/v1/generate",
            "json_paths": {"account_id": "user.id", "submit_job_id": "job.id",
                           "result_job_id": "creation.id", "status": "creation.status",
                           "status_terminal_ok": "done", "status_terminal_fail": "failed",
                           "urls": "creation.urls", "url_hosts": ["cdn.openart.test"]}}
    prof.update(extra)
    return prof


def store_profile(prof):
    path = jobs.profile_path_for(prof["model"], prof["mode"])
    cli.write_private(path, json.dumps(prof).encode())
    return jobs.load_qualification(model=prof["model"], mode=prof["mode"], allow_fixture=True)


def preview(profile, controls):
    creative = cli.native_video_argv(controls["prompt"], model=profile["model"], mode=profile["mode"],
                                     duration=controls.get("duration"))
    params = {"prompt": controls["prompt"]}
    if controls.get("duration") is not None:
        params["duration"] = controls["duration"]
    body = {"model": profile["model"], "media": "video", "mode": profile["mode"], "params": params}
    rid, rsha = receipt({"endpoint": profile["dry_run_endpoint"], "body": body},
                        creative + ["--dry-run"] + cli.GLOBAL_FLAGS)
    return rid, rsha


def prepared(env, prompt="a fox", attempt="att-1", output="clip.mp4"):
    profile = store_profile(fixture_profile())
    rid, rsha = preview(profile, {"prompt": prompt, "duration": 5})
    project = env["tmp"] / "project"
    project.mkdir(exist_ok=True)
    inputs = {"prompt": prompt, "model": "m-turbo", "duration": 5, "output_path": str(project / output),
              "native_dry_run_receipt_id": rid, "native_dry_run_receipt_sha256": rsha}
    native = jobs.prepare_native_request(inputs, profile)
    snap = jobs.freeze_request(attempt, inputs, native, profile)
    binding = {"attempt_id": attempt, "request_sha256": "r" * 64, "reservation_id": "res-1",
               "snapshot_sha256": snap["snapshot_sha256"],
               **{k: native[k] for k in ("native_controls_sha256", "native_argv_sha256", "profile_sha256",
                                         "account_id_sha256", "native_body_sha256")}}
    return profile, inputs, native, binding


def active_ledger(project_root, attempt_id, request_sha256):
    return {"state": "active", "attempt_id": attempt_id, "request_sha256": request_sha256,
            "reservation_id": "res-1"}


# ------------------------------------------------------------ qualification

def test_no_profile_means_generation_unqualified(env):
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.load_qualification(model="m-turbo", mode="text2video")
    assert exc.value.kind == "generation_unqualified"


def test_fixture_profile_never_qualifies_without_seam(env):
    store_profile(fixture_profile())
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.load_qualification(model="m-turbo", mode="text2video")
    assert exc.value.kind == "generation_unqualified"


def test_real_source_alone_does_not_qualify(env):
    prof = fixture_profile(source="real")
    cli.write_private(jobs.profile_path_for("m-turbo", "text2video"), json.dumps(prof).encode())
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.load_qualification(model="m-turbo", mode="text2video")
    assert exc.value.kind == "generation_unqualified"


def test_per_model_profiles_coexist_and_list_is_pure(env):
    store_profile(fixture_profile(model="m-turbo"))
    store_profile(fixture_profile(model="m-max"))
    rows = jobs.list_qualifications()
    assert {r["model"] for r in rows} == {"m-turbo", "m-max"}
    assert all(r["valid"] is False and r["error"] == "fixture_profile" for r in rows)
    assert calls(env) == []


def test_image2video_profile_requires_upload_contract(env):
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.validate_profile(fixture_profile(mode="image2video"), allow_fixture=True)
    assert exc.value.kind == "unsupported_gate"


# ------------------------------------------------------------ native request

@pytest.mark.parametrize("key", ["first_frame", "reference_image_path", "reference_image_paths", "image",
                                 "image_url", "end_image_path", "audio_path", "keyframes", "voices",
                                 "rich_references", "bogus_control"])
def test_unsupported_aliases_and_controls_fail(env, key):
    profile = store_profile(fixture_profile())
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.native_request({"prompt": "x", "model": "m-turbo", key: "v"}, profile)
    assert exc.value.kind == "unsupported_gate"


def test_image_path_without_operation_fails(env):
    profile = store_profile(fixture_profile())
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.native_request({"prompt": "x", "model": "m-turbo", "image_path": "/tmp/a.png"}, profile)
    assert exc.value.kind == "unsupported_gate"


def test_prepare_requires_retained_preview(env):
    profile = store_profile(fixture_profile())
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.prepare_native_request({"prompt": "x", "model": "m-turbo"}, profile)
    assert exc.value.kind == "native_preview_required"


def test_prepare_rejects_tampered_or_mismatched_preview(env):
    profile = store_profile(fixture_profile())
    rid, rsha = preview(profile, {"prompt": "other prompt", "duration": 5})
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.prepare_native_request({"prompt": "a fox", "model": "m-turbo", "duration": 5,
                                     "native_dry_run_receipt_id": rid,
                                     "native_dry_run_receipt_sha256": rsha}, profile)
    assert exc.value.kind == "dry_run_mismatch"
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.prepare_native_request({"prompt": "other prompt", "model": "m-turbo", "duration": 5,
                                     "native_dry_run_receipt_id": rid,
                                     "native_dry_run_receipt_sha256": "0" * 64}, profile)
    assert exc.value.kind == "native_preview_invalid"


def test_native_request_is_deterministic_and_binds_preview(env):
    profile, inputs, native, _ = prepared(env)
    again = jobs.native_request(inputs, profile, dry_run=native["dry_run"])
    assert again == native
    assert native["native_body_sha256"] and native["argv"][-1] == "--async"
    assert calls(env) == []


# ------------------------------------------------------------ frozen snapshot

def test_freeze_is_immutable_and_tamper_detected(env):
    profile, inputs, native, binding = prepared(env)
    loaded = jobs.load_frozen_request("att-1")
    assert loaded["snapshot_sha256"] == binding["snapshot_sha256"]
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.freeze_request("att-1", dict(inputs, prompt="changed"), native, profile)
    assert exc.value.kind == "already_frozen"
    path = jobs.job_dir("att-1") / "frozen_request.json"
    os.chmod(path, 0o600)
    path.write_bytes(path.read_bytes().replace(b"a fox", b"a cat"))
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.load_frozen_request("att-1")
    assert exc.value.kind == "frozen_request_tampered"


# ------------------------------------------------------------ launch

@pytest.mark.parametrize("bad_timeout", [None, True, 0, -1, float("nan"), float("inf"), "1"])
def test_launch_rejects_explicit_invalid_wait_timeout_before_provider_calls(env, monkeypatch, bad_timeout):
    def unexpected_call(*args, **kwargs):
        pytest.fail("invalid launch timeout must fail before provider calls")

    monkeypatch.setattr(jobs, "_current_account", unexpected_call)
    monkeypatch.setattr(jobs, "_POPEN", unexpected_call)
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.launch_submit(env["tmp"] / "project", {}, {}, {}, wait_timeout=bad_timeout,
                           deadline=jobs.time.monotonic() + 30)
    assert exc.value.kind == "invalid_argument"
    assert calls(env) == []


def test_launch_without_reservation_never_spawns(env, monkeypatch):
    profile, _, native, binding = prepared(env)
    monkeypatch.setattr(jobs, "_ALLOW_FIXTURE_LAUNCH", True)
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.launch_submit(env["tmp"] / "project", binding, native, profile)
    assert exc.value.kind == "no_active_reservation"
    assert calls(env) == []


def test_fixture_profile_cannot_launch(env):
    profile, _, native, binding = prepared(env)
    jobs.register_reservation_lookup(active_ledger)
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.launch_submit(env["tmp"] / "project", binding, native, profile)
    assert exc.value.kind == "generation_unqualified"
    assert calls(env) == []


def test_launch_submits_exactly_once(env, monkeypatch):
    profile, _, native, binding = prepared(env)
    monkeypatch.setattr(jobs, "_ALLOW_FIXTURE_LAUNCH", True)
    jobs.register_reservation_lookup(active_ledger)
    out = jobs.launch_submit(env["tmp"] / "project", binding, native, profile, wait_timeout=20)
    assert out["status"] == "submitted"
    assert out["job_id_sha256"] == hashlib.sha256(b"job-123").hexdigest()
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.launch_submit(env["tmp"] / "project", binding, native, profile, wait_timeout=20)
    assert exc.value.kind == "already_launched"
    submits = [c for c in calls(env) if "--async" in c]
    assert len(submits) == 1


@pytest.mark.parametrize("stage", ["marker_dir", "streams_dir"])
def test_fsync_failure_before_spawn_never_submits(env, monkeypatch, stage):
    """Durable submit-once marker + raw stream entries precede Popen; fsync failure -> zero submits."""
    profile, _, native, binding = prepared(env)
    monkeypatch.setattr(jobs, "_ALLOW_FIXTURE_LAUNCH", True)
    jobs.register_reservation_lookup(active_ledger)
    order = []
    real_cli_fsync, real_jobs_fsync = cli.fsync_dir, jobs._fsync_dir

    def cli_fsync(path):
        order.append(("cli", Path(path).name))
        if stage == "marker_dir" and (Path(path) / "launch.json").exists():
            raise OSError(5, "EIO")
        return real_cli_fsync(path)

    def jobs_fsync(path):
        order.append(("jobs", Path(path).name))
        if stage == "streams_dir" and (Path(path) / "submit.stdout").exists():
            raise OSError(5, "EIO")
        return real_jobs_fsync(path)

    monkeypatch.setattr(cli, "fsync_dir", cli_fsync)
    monkeypatch.setattr(jobs, "_fsync_dir", jobs_fsync)
    spawned = []
    monkeypatch.setattr(jobs, "_popen", lambda *a, **k: spawned.append(a) or (_ for _ in ()).throw(AssertionError))
    with pytest.raises(OSError):
        jobs.launch_submit(env["tmp"] / "project", binding, native, profile, wait_timeout=20)
    assert spawned == [] and [c for c in calls(env) if "--async" in c] == []
    assert ("cli", "att-1") in order  # marker publication fsynced its parent directory
    with pytest.raises(OpenArtCLIError) as exc:  # marker persists: never a second attempt
        jobs.launch_submit(env["tmp"] / "project", binding, native, profile, wait_timeout=20)
    assert exc.value.kind == "already_launched"


def test_private_dir_and_write_fsync_parent(env, monkeypatch):
    seen = []
    real = cli.fsync_dir
    monkeypatch.setattr(cli, "fsync_dir", lambda p: seen.append(Path(p)) or real(p))
    target = cli.private_dir("jobs", "durable") / "marker"
    cli.write_private(target, b"x")
    assert cli.state_dir() / "jobs" in seen and target.parent in seen


def test_account_mismatch_quarantines_without_spawn(env, monkeypatch):
    profile, _, native, binding = prepared(env)
    monkeypatch.setattr(jobs, "_ALLOW_FIXTURE_LAUNCH", True)
    monkeypatch.setenv("FAKE_ACCOUNT", "someone-else")
    jobs.register_reservation_lookup(active_ledger)
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.launch_submit(env["tmp"] / "project", binding, native, profile, wait_timeout=20)
    assert exc.value.kind == "account_mismatch"
    assert not [c for c in calls(env) if "--async" in c]
    assert any(e["type"] == "quarantined" for e in jobs.read_events("att-1"))


def test_lost_job_id_holds_and_never_resubmits(env, monkeypatch):
    profile, _, native, binding = prepared(env)
    monkeypatch.setattr(jobs, "_ALLOW_FIXTURE_LAUNCH", True)
    monkeypatch.setenv("FAKE_OPENART_MODE", "nojob")
    jobs.register_reservation_lookup(active_ledger)
    out = jobs.launch_submit(env["tmp"] / "project", binding, native, profile, wait_timeout=20)
    assert out["status"] == "hold_unknown_job"
    assert jobs.recover_launch("att-1")["status"] == "hold_unknown_job"
    assert jobs.reconcile_job("att-1")["release_authorized"] is False


# ------------------------------------------------------------ uploads

def test_upload_unqualified_makes_zero_cli_calls(env, tmp_path):
    store_profile(fixture_profile())
    src = tmp_path / "ref.png"
    src.write_bytes(b"png")
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.upload_reference(tmp_path, "up-1", src, model="m-turbo", mode="text2video")
    assert exc.value.kind == "upload_unqualified"
    assert calls(env) == []


def test_upload_url_for_requires_retained_record(env):
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.upload_url_for("up-missing")
    assert exc.value.kind == "upload_mismatch"


def test_readonly_transport_refuses_upload(env):
    with pytest.raises(OpenArtCLIError):
        cli.run_readonly(["upload", "add", "/tmp/x.png"])
    assert calls(env) == []


# ------------------------------------------------------------ collection

import lib.openart_download as dl


def fake_download(monkeypatch, payload=b"video-bytes", fail=None, record=None, host="cdn.openart.test"):
    def collect_output(url, output_path, *, allowed_hosts, output_root, **kw):
        if record is not None:
            record.append(url)
        path = Path(output_path)
        if fail == "before":
            raise dl.OpenArtDownloadError("connection refused")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        os.write(fd, payload)
        os.close(fd)
        if fail == "after":
            raise dl.OpenArtDownloadError("fsync failed after publish")
        return {"path": str(path), "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                "source_url": url, "source_host": host}
    monkeypatch.setattr(dl, "collect_output", collect_output)


def launched(env, monkeypatch, attempt="att-1", output="clip.mp4"):
    profile, inputs, native, binding = prepared(env, attempt=attempt, output=output)
    monkeypatch.setattr(jobs, "_ALLOW_FIXTURE_LAUNCH", True)
    jobs.register_reservation_lookup(active_ledger)
    assert jobs.launch_submit(env["tmp"] / "project", binding, native, profile, wait_timeout=20)["status"] == "submitted"
    root = env["tmp"] / "project"
    root.mkdir(exist_ok=True)
    return profile, binding, root


def test_collect_happy_path_binds_original_job_and_bytes(env, monkeypatch):
    profile, binding, root = launched(env, monkeypatch)
    urls = []
    fake_download(monkeypatch, record=urls)
    out = jobs.collect_job("att-1", output_path=root / "clip.mp4", output_root=root, profile=profile)
    assert out["status"] == "collected"
    assert out["release_authorized"] is False and out["billing"] == "unknown"
    assert urls == ["https://cdn.openart.test/v.mp4?sig=1"]
    assert ["creation", "get", "job-123", "--json", "--no-input"] in calls(env)
    stable = jobs.verify_collection_receipt("att-1", profile)
    assert stable["output"] == out["output"]
    assert stable["binding"] == binding
    assert stable["job_id_sha256"] == hashlib.sha256(b"job-123").hexdigest()
    assert stable["evidence"]["snapshot_sha256"] == binding["snapshot_sha256"]
    rec = jobs.reconcile_job("att-1")
    assert rec["state"] == "collected" and rec["collection_job_record_sha256"] == stable["job_record_sha256"]
    assert rec["release_authorized"] is False and "events_sha256" in rec
    # Second collect makes no new status/download call and returns same bytes.
    before = len(calls(env))
    again = jobs.collect_job("att-1", output_path=root / "clip.mp4", output_root=root, profile=profile)
    assert again["output"] == out["output"] and len(calls(env)) == before
    assert not [c for c in calls(env) if "--async" in c][1:]


def test_verification_stable_under_appended_billing_events(env, monkeypatch):
    profile, _, root = launched(env, monkeypatch)
    fake_download(monkeypatch)
    jobs.collect_job("att-1", output_path=root / "clip.mp4", output_root=root, profile=profile)
    first = jobs.verify_collection_receipt("att-1", profile)
    for kind in ("status", "settlement", "refund", "status"):
        jobs.append_event("att-1", {"type": kind, "billing": "unknown"})
    assert jobs.verify_collection_receipt("att-1", profile) == first


def test_unknown_billing_still_valid_footage(env, monkeypatch):
    profile, _, root = launched(env, monkeypatch)
    fake_download(monkeypatch)
    out = jobs.collect_job("att-1", output_path=root / "clip.mp4", output_root=root, profile=profile)
    stable = jobs.verify_collection_receipt("att-1", profile)
    assert out["billing"] == stable["billing"] == "unknown"
    assert stable["output"]["sha256"] == hashlib.sha256(b"video-bytes").hexdigest()


def test_wrong_result_job_holds_without_download(env, monkeypatch):
    profile, _, root = launched(env, monkeypatch)
    monkeypatch.setenv("FAKE_RESULT_JOB", "job-other")
    urls = []
    fake_download(monkeypatch, record=urls)
    out = jobs.collect_job("att-1", output_path=root / "clip.mp4", output_root=root, profile=profile)
    assert out["status"] == "hold" and out["reason"] == "result_job_mismatch"
    assert urls == [] and not (root / "clip.mp4").exists()
    assert len([c for c in calls(env) if "--async" in c]) == 1


def test_account_change_at_collect_quarantines(env, monkeypatch):
    profile, _, root = launched(env, monkeypatch)
    monkeypatch.setenv("FAKE_ACCOUNT", "someone-else")
    urls = []
    fake_download(monkeypatch, record=urls)
    out = jobs.collect_job("att-1", output_path=root / "clip.mp4", output_root=root, profile=profile)
    assert out["status"] == "quarantined" and urls == []
    assert not [c for c in calls(env) if c[:2] == ["creation", "get"]]
    assert jobs.reconcile_job("att-1")["state"] == "quarantined"


def test_changed_profile_holds(env, monkeypatch):
    profile, _, root = launched(env, monkeypatch)
    other = dict(profile, form_defaults={"duration": 10})
    out = jobs.collect_job("att-1", output_path=root / "clip.mp4", output_root=root, profile=other)
    assert out["status"] == "hold" and out["reason"] == "profile_changed"


def test_terminal_failure_records_receipt_never_releases(env, monkeypatch):
    profile, _, root = launched(env, monkeypatch)
    monkeypatch.setenv("FAKE_STATUS", "failed")
    out = jobs.collect_job("att-1", output_path=root / "clip.mp4", output_root=root, profile=profile)
    assert out["status"] == "failed_terminal" and out["release_authorized"] is False
    assert out["receipt_id"] and out["receipt_sha256"]
    rec = jobs.reconcile_job("att-1")
    assert rec["state"] == "failed_terminal" and rec["release_authorized"] is False
    assert len([c for c in calls(env) if "--async" in c]) == 1
    with pytest.raises(OpenArtCLIError):
        jobs.verify_collection_receipt("att-1", profile)


def test_pending_status_keeps_holding(env, monkeypatch):
    profile, _, root = launched(env, monkeypatch)
    monkeypatch.setenv("FAKE_STATUS", "processing")
    out = jobs.collect_job("att-1", output_path=root / "clip.mp4", output_root=root, profile=profile)
    assert out["status"] == "pending" and out["release_authorized"] is False


def test_publish_then_crash_resumes_into_unique_path(env, monkeypatch):
    profile, _, root = launched(env, monkeypatch)
    fake_download(monkeypatch, fail="after")
    out = jobs.collect_job("att-1", output_path=root / "clip.mp4", output_root=root, profile=profile)
    assert out["status"] == "hold" and out["reason"] == "collection_failed_after_publish"
    stale = root / "clip.mp4"
    stale.write_bytes(b"tampered")  # old unverified file must never be trusted or overwritten
    fake_download(monkeypatch, payload=b"fresh-bytes")
    again = jobs.collect_job("att-1", output_path=root / "clip.mp4", output_root=root, profile=profile)
    assert again["status"] == "collected"
    assert again["output"]["path"] == str(root / "clip.r1.mp4")
    assert stale.read_bytes() == b"tampered"
    stable = jobs.verify_collection_receipt("att-1", profile)
    assert stable["evidence"]["recovered"] is True
    assert len([c for c in calls(env) if "--async" in c]) == 1


def test_download_failure_before_publish_is_pending_not_retry(env, monkeypatch):
    profile, _, root = launched(env, monkeypatch)
    fake_download(monkeypatch, fail="before")
    out = jobs.collect_job("att-1", output_path=root / "clip.mp4", output_root=root, profile=profile)
    assert out["status"] == "pending" and out["release_authorized"] is False
    assert len([c for c in calls(env) if "--async" in c]) == 1


def test_output_outside_reserved_root_holds(env, monkeypatch):
    profile, _, root = launched(env, monkeypatch)
    fake_download(monkeypatch)
    out = jobs.collect_job("att-1", output_path=env["tmp"] / "elsewhere.mp4", output_root=root,
                           profile=profile)
    assert out["status"] == "hold" and out["reason"] == "output_outside_root"


def test_symlink_ancestor_escape_fails_verification(env, monkeypatch):
    (env["tmp"] / "project" / "sub").mkdir(parents=True)
    profile, _, root = launched(env, monkeypatch, output="sub/clip.mp4")
    fake_download(monkeypatch)
    jobs.collect_job("att-1", output_path=root / "sub" / "clip.mp4", output_root=root, profile=profile)
    outside = env["tmp"] / "outside"
    (root / "sub").rename(outside)
    (root / "sub").symlink_to(outside)
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.verify_collection_receipt("att-1", profile)
    assert exc.value.kind == "collection_receipt_invalid"


@pytest.mark.parametrize("tamper", ["output", "evidence", "status_receipt", "account_receipt"])
def test_tampered_collection_fails_verification(env, monkeypatch, tamper):
    profile, _, root = launched(env, monkeypatch)
    fake_download(monkeypatch)
    jobs.collect_job("att-1", output_path=root / "clip.mp4", output_root=root, profile=profile)
    ev_path = jobs.job_dir("att-1") / "collection_evidence.json"
    ev = json.loads(ev_path.read_bytes())
    if tamper == "output":
        (root / "clip.mp4").write_bytes(b"swapped")
    elif tamper == "evidence":
        ev_path.chmod(0o600)
        ev_path.write_bytes(ev_path.read_bytes().replace(b"cdn.openart.test", b"evil.example"))
    else:
        rid = ev["status_receipt_id" if tamper == "status_receipt" else "account_receipt_id"]
        path = cli.receipt_path(rid)
        path.chmod(0o600)
        path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.verify_collection_receipt("att-1", profile)
    assert exc.value.kind == "collection_receipt_invalid"


def test_unknown_job_holds_collection(env, monkeypatch):
    profile, _, native, binding = prepared(env)
    monkeypatch.setattr(jobs, "_ALLOW_FIXTURE_LAUNCH", True)
    monkeypatch.setenv("FAKE_OPENART_MODE", "nojob")
    jobs.register_reservation_lookup(active_ledger)
    jobs.launch_submit(env["tmp"] / "project", binding, native, profile, wait_timeout=20)
    root = env["tmp"] / "project"
    out = jobs.collect_job("att-1", output_path=root / "clip.mp4", output_root=root, profile=profile)
    assert out["status"] == "hold" and out["reason"] == "unknown_job_id"
    assert not [c for c in calls(env) if c[:2] == ["creation", "get"]]


# ------------------------------------------------------------ parent death / liveness

def test_recover_after_parent_death_parses_surviving_private_stdout(env, monkeypatch):
    profile, _, native, binding = prepared(env)
    monkeypatch.setattr(jobs, "_ALLOW_FIXTURE_LAUNCH", True)
    jobs.register_reservation_lookup(active_ledger)

    class Killed(Exception):
        pass

    real = jobs._popen

    def popen_then_die(*a, **kw):
        proc = real(*a, **kw)
        raise_after.append(proc)
        return proc

    raise_after = []
    monkeypatch.setattr(jobs, "_POPEN", None)
    monkeypatch.setattr(jobs, "_popen", popen_then_die)
    orig_wait_parse = jobs._finish_parse
    monkeypatch.setattr(jobs, "_finish_parse", lambda *a, **k: (_ for _ in ()).throw(Killed()))
    with pytest.raises(Killed):
        jobs.launch_submit(env["tmp"] / "project", binding, native, profile, wait_timeout=20)
    monkeypatch.setattr(jobs, "_finish_parse", orig_wait_parse)
    stdout = jobs.job_dir("att-1") / "submit.stdout"
    assert stdout.stat().st_mode & 0o077 == 0 and b"job-123" in stdout.read_bytes()
    assert jobs.original_job_id("att-1") is None
    out = jobs.recover_launch("att-1")
    assert out["status"] == "submitted"
    assert out["job_id_sha256"] == hashlib.sha256(b"job-123").hexdigest()
    assert len([c for c in calls(env) if "--async" in c]) == 1


class _NoWaitProc:
    """Parent 'dies' before waitpid: no durable exited proof is ever recorded."""
    def __init__(self, proc):
        self._proc, self.pid = proc, proc.pid

    def wait(self, timeout=None):
        self._proc.wait()  # reap quietly so the test leaves no zombie
        raise RuntimeError("parent died before waitpid")


def _launch_without_wait(env, monkeypatch, birth=True):
    profile, _, native, binding = prepared(env)
    monkeypatch.setattr(jobs, "_ALLOW_FIXTURE_LAUNCH", True)
    jobs.register_reservation_lookup(active_ledger)
    real = jobs._popen
    monkeypatch.setattr(jobs, "_popen", lambda *a, **k: _NoWaitProc(real(*a, **k)))
    if not birth:
        monkeypatch.setattr(jobs, "process_identity", lambda pid: None)
    out = jobs.launch_submit(env["tmp"] / "project", binding, native, profile, wait_timeout=20)
    assert out["status"] == "uncertain"
    assert not any(e["type"] == "exited" for e in jobs.read_events("att-1"))
    return profile


def test_recover_with_unknown_liveness_keeps_holding(env, monkeypatch):
    _launch_without_wait(env, monkeypatch)
    monkeypatch.setattr(jobs, "process_alive", lambda identity: "unknown")
    out = jobs.recover_launch("att-1")
    assert out["status"] == "uncertain" and out["liveness"] == "unknown"
    assert jobs.original_job_id("att-1") is None
    assert jobs.original_process_state("att-1")["state"] == "unknown"


def test_recover_with_live_original_keeps_holding(env, monkeypatch):
    _launch_without_wait(env, monkeypatch)
    monkeypatch.setattr(jobs, "process_alive", lambda identity: "alive")
    out = jobs.recover_launch("att-1")
    assert out["status"] == "uncertain" and out["liveness"] == "alive"


def test_missing_birth_without_wait_proof_never_counts_as_exited(env, monkeypatch):
    _launch_without_wait(env, monkeypatch, birth=False)
    state = jobs.original_process_state("att-1")
    assert state["state"] not in ("exited", "dead")
    assert jobs.recover_launch("att-1")["status"] == "uncertain"


def test_recover_uses_waitpid_proof_when_birth_unavailable(env, monkeypatch):
    profile, _, native, binding = prepared(env)
    monkeypatch.setattr(jobs, "_ALLOW_FIXTURE_LAUNCH", True)
    jobs.register_reservation_lookup(active_ledger)
    monkeypatch.setattr(jobs, "process_identity", lambda pid: None)
    orig = jobs._finish_parse
    monkeypatch.setattr(jobs, "_finish_parse", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("die")))
    with pytest.raises(RuntimeError):
        jobs.launch_submit(env["tmp"] / "project", binding, native, profile, wait_timeout=20)
    monkeypatch.setattr(jobs, "_finish_parse", orig)
    monkeypatch.setattr(jobs, "process_alive", lambda identity: "unknown")
    state = jobs.original_process_state("att-1")
    assert state["state"] == "exited" and state["returncode"] == 0
    out = jobs.recover_launch("att-1")
    assert out["status"] == "submitted"
    assert len([c for c in calls(env) if "--async" in c]) == 1


# ------------------------------------------------------------ native controls / budgets

@pytest.mark.parametrize("operation", ["foo", "image_to_video", "text_to_image"])
def test_unknown_or_mismatched_operation_is_rejected(env, operation):
    profile, inputs, _, _ = prepared(env)
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.prepare_native_request(dict(inputs, operation=operation), profile)
    assert exc.value.kind == "unsupported_gate"


def test_explicit_text_to_video_operation_is_accepted(env):
    profile, inputs, native, _ = prepared(env)
    again = jobs.prepare_native_request(dict(inputs, operation="text_to_video"), profile)
    assert again["native_argv_sha256"] == native["native_argv_sha256"]


def test_exhausted_budget_during_account_check_never_spawns(env, monkeypatch):
    import time as _time
    profile, _, native, binding = prepared(env)
    monkeypatch.setattr(jobs, "_ALLOW_FIXTURE_LAUNCH", True)
    jobs.register_reservation_lookup(active_ledger)
    seen = []

    def slow_account(profile, timeout):
        seen.append(timeout)
        _time.sleep(timeout + 0.2)
        return ACCOUNT_SHA

    jobs.register_account_check(slow_account)
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.launch_submit(env["tmp"] / "project", binding, native, profile, wait_timeout=0.5)
    assert exc.value.kind == "timeout"
    assert seen and 0 < seen[0] <= 0.5
    assert not [c for c in calls(env) if "--async" in c]
    assert not (jobs.job_dir("att-1") / "submit.stdout").exists()


def test_collect_shares_one_budget_with_download(env, monkeypatch):
    profile, _, root = launched(env, monkeypatch)
    got = {}

    def collect_output(url, output_path, *, allowed_hosts, output_root, timeout=None, **kw):
        got["timeout"] = timeout
        raise dl.OpenArtDownloadError("connection refused")

    monkeypatch.setattr(dl, "collect_output", collect_output)
    out = jobs.collect_job("att-1", output_path=root / "clip.mp4", output_root=root, profile=profile,
                           timeout=20)
    assert out["status"] == "pending"
    assert got["timeout"] is not None and 0 < got["timeout"] < 20


# ------------------------------------------------------------ raw submit / root binding

def test_forged_job_id_contradicting_raw_submit_holds(env, monkeypatch):
    profile, _, root = launched(env, monkeypatch)
    urls = []
    fake_download(monkeypatch, record=urls)
    path = jobs.job_dir("att-1") / "job_id"
    path.chmod(0o600)
    path.write_bytes(b"job-999")  # fake status echoes the requested id, so status is "consistent"
    out = jobs.collect_job("att-1", output_path=root / "clip.mp4", output_root=root, profile=profile)
    assert out["status"] == "hold" and out["reason"] == "raw_submit_invalid"
    assert urls == [] and not [c for c in calls(env) if c[:2] == ["creation", "get"]]


@pytest.mark.parametrize("change", ["modify", "remove"])
def test_raw_submit_change_fails_verify_and_collect(env, monkeypatch, change):
    profile, _, root = launched(env, monkeypatch)
    fake_download(monkeypatch)
    assert jobs.collect_job("att-1", output_path=root / "clip.mp4", output_root=root,
                            profile=profile)["status"] == "collected"
    stdout = jobs.job_dir("att-1") / "submit.stdout"
    if change == "modify":
        stdout.chmod(0o600)
        stdout.write_bytes(stdout.read_bytes() + b"\n")
    else:
        stdout.unlink()
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.verify_collection_receipt("att-1", profile)
    assert exc.value.kind == "collection_receipt_invalid"
    again = jobs.collect_job("att-1", output_path=root / "clip.mp4", output_root=root, profile=profile)
    assert again["status"] == "hold"


def test_collect_wrong_output_root_holds(env, monkeypatch):
    profile, _, root = launched(env, monkeypatch)
    other = env["tmp"] / "other"
    other.mkdir()
    urls = []
    fake_download(monkeypatch, record=urls)
    out = jobs.collect_job("att-1", output_path=other / "clip.mp4", output_root=other, profile=profile)
    assert out["status"] == "hold" and out["reason"] == "output_root_not_reserved" and urls == []


def test_collect_unbound_output_path_holds(env, monkeypatch):
    profile, _, root = launched(env, monkeypatch)
    urls = []
    fake_download(monkeypatch, record=urls)
    out = jobs.collect_job("att-1", output_path=root / "other.mp4", output_root=root, profile=profile)
    assert out["status"] == "hold" and out["reason"] == "output_path_not_bound" and urls == []


def test_unqualified_download_source_host_is_not_collected(env, monkeypatch):
    profile, _, root = launched(env, monkeypatch)
    fake_download(monkeypatch, host="evil.example")
    out = jobs.collect_job("att-1", output_path=root / "clip.mp4", output_root=root, profile=profile)
    assert out["status"] == "hold" and out["status"] != "collected"
    with pytest.raises(OpenArtCLIError):
        jobs.verify_collection_receipt("att-1", profile)


def test_ancestor_swapped_to_symlink_at_open_is_rejected(env, monkeypatch):
    (env["tmp"] / "project" / "sub").mkdir(parents=True)
    profile, _, root = launched(env, monkeypatch, output="sub/clip.mp4")
    fake_download(monkeypatch)
    jobs.collect_job("att-1", output_path=root / "sub" / "clip.mp4", output_root=root, profile=profile)
    jobs.verify_collection_receipt("att-1", profile)  # sane before the race
    real = jobs._walk_dir
    outside = env["tmp"] / "outside"

    def swap_then_walk(path):
        if not outside.exists():  # identical bytes outside: only a no-follow walk can tell
            (root / "sub").rename(outside)
            (root / "sub").symlink_to(outside)
        return real(path)

    monkeypatch.setattr(jobs, "_walk_dir", swap_then_walk)
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.verify_collection_receipt("att-1", profile)
    assert exc.value.kind == "collection_receipt_invalid"


# ------------------------------------------------------------ terminal failure verifier

def test_verify_terminal_failure_happy_path(env, monkeypatch):
    profile, binding, root = launched(env, monkeypatch)
    monkeypatch.setenv("FAKE_STATUS", "failed")
    out = jobs.collect_job("att-1", output_path=root / "clip.mp4", output_root=root, profile=profile)
    tf = jobs.verify_terminal_failure("att-1", profile)
    assert tf["terminal_failure_sha256"] == out["terminal_failure_sha256"]
    assert tf["job_id_sha256"] == hashlib.sha256(b"job-123").hexdigest()
    assert tf["process_state"] in ("exited", "dead")
    assert tf["billing"] == "unknown" and tf["release_authorized"] is False
    assert tf["binding"]["reservation_id"] == binding["reservation_id"]
    # pure: no further CLI calls
    before = len(calls(env))
    jobs.verify_terminal_failure("att-1", profile)
    assert len(calls(env)) == before


@pytest.mark.parametrize("tamper", ["record", "status_receipt", "account_receipt", "stdout"])
def test_verify_terminal_failure_rejects_tampering(env, monkeypatch, tamper):
    profile, _, root = launched(env, monkeypatch)
    monkeypatch.setenv("FAKE_STATUS", "failed")
    jobs.collect_job("att-1", output_path=root / "clip.mp4", output_root=root, profile=profile)
    rec_path = jobs.job_dir("att-1") / "terminal_failure.json"
    rec = json.loads(rec_path.read_bytes())
    if tamper == "record":
        target = rec_path
    elif tamper == "stdout":
        target = jobs.job_dir("att-1") / "submit.stdout"
    else:
        target = cli.receipt_path(rec["status_receipt_id" if tamper == "status_receipt"
                                      else "account_receipt_id"])
    target.chmod(0o600)
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.verify_terminal_failure("att-1", profile)
    assert exc.value.kind == "terminal_failure_invalid"


# ------------------------------------------------------------ qualified fixture uploads

def captured(kind, argv, parsed):
    """U1-shaped private receipt with retained 0600 streams (as the qualification module requires)."""
    raw = json.dumps(parsed).encode()
    stem = f"2026-10-05T000000-{hashlib.sha256(raw + json.dumps(argv).encode()).hexdigest()[:8]}"
    cli.write_private(cli.private_dir("streams") / f"{stem}.stdout", raw)
    cli.write_private(cli.private_dir("streams") / f"{stem}.stderr", b"")
    record = {"argv": argv + cli.GLOBAL_FLAGS, "started_at": "2026-10-05T00:00:00+00:00",
              "returncode": 0, "stdout_sha256": hashlib.sha256(raw).hexdigest(), "parsed": parsed,
              "streams": {"stdout": f"{stem}.stdout", "stderr": f"{stem}.stderr"}}
    data = json.dumps(record).encode()
    cli.write_private(cli.receipt_path(stem), data)
    return {"kind": kind, "receipt_id": stem, "receipt_sha256": hashlib.sha256(data).hexdigest()}


def upload_profile(env, model="m-turbo"):
    raw = {"contract": {"nonspending": True, "no_delayed_charge": True},
           "url": "https://up.openart.test/x.png"}
    prof = fixture_profile(model=model, mode="image2video", upload={
        "json_paths": {"upload_url": "url"}, "url_hosts": ["up.openart.test"],
        "guarantee": {"argv": ["account"],
                      "nonspending": {"path": "contract.nonspending", "expected": True},
                      "no_delayed_charge": {"path": "contract.no_delayed_charge", "expected": True}},
        "receipts": [captured("nonspending_guarantee", ["account"], raw),
                     captured("upload", ["upload", "add", "/synthetic/ref.png"], raw)]})
    return store_profile(prof)


@pytest.fixture(autouse=False)
def current_ok(monkeypatch):
    """Unit seam: fixture profiles have no real inspection to recheck (real chain test covers it)."""
    seen = []
    monkeypatch.setattr(jobs, "_verify_current", lambda profile, timeout: seen.append(timeout))
    return seen


def approve(src_sha):
    def lookup(project_root, upload_id, source_sha256):
        if source_sha256 != src_sha:
            return None
        return {"state": "approved", "upload_id": upload_id, "source_sha256": source_sha256,
                "approval_sha256": "a" * 64}
    return lookup


def test_qualified_fixture_upload_uses_private_snapshot(env, monkeypatch, tmp_path, current_ok):
    profile = upload_profile(env)
    monkeypatch.setattr(jobs, "_ALLOW_FIXTURE_UPLOAD", True)
    src = tmp_path / "ref.png"
    src.write_bytes(b"png-bytes")
    jobs.register_upload_approval_lookup(approve(hashlib.sha256(b"png-bytes").hexdigest()))
    out = jobs.upload_reference(tmp_path, "up-1", src, model="m-turbo", mode="image2video")
    ups = [c for c in calls(env) if c[:2] == ["upload", "add"]]
    assert len(ups) == 1 and ups[0][2] != str(src) and ups[0][2].endswith("up-1.source.png")
    assert out["source_sha256"] == hashlib.sha256(b"png-bytes").hexdigest()
    src.write_bytes(b"changed-later")  # mutable original no longer matters
    url = jobs.upload_url_for("up-1", profile=profile, source_sha256=out["source_sha256"])
    assert url == "https://up.openart.test/r.png"
    # staged promotion (added dry_run receipt -> new profile SHA) keeps the stable upload binding valid
    promoted = dict(profile, receipts=list(profile.get("receipts") or []) + [{"kind": "dry_run"}],
                    profile_sha256="f" * 64)
    assert jobs.upload_url_for("up-1", profile=promoted) == url
    assert out["binding_sha256"] == jobs.sha256_json(jobs.upload_binding(promoted))


def test_upload_unapproved_source_makes_no_upload(env, monkeypatch, tmp_path):
    upload_profile(env)
    monkeypatch.setattr(jobs, "_ALLOW_FIXTURE_UPLOAD", True)
    src = tmp_path / "ref.png"
    src.write_bytes(b"png-bytes")
    jobs.register_upload_approval_lookup(approve("0" * 64))
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.upload_reference(tmp_path, "up-1", src, model="m-turbo", mode="image2video")
    assert exc.value.kind == "upload_unapproved"
    assert not [c for c in calls(env) if c[:1] == ["upload"]]


def test_source_changed_after_approval_is_not_uploaded(env, monkeypatch, tmp_path):
    upload_profile(env)
    monkeypatch.setattr(jobs, "_ALLOW_FIXTURE_UPLOAD", True)
    src = tmp_path / "ref.png"
    src.write_bytes(b"png-bytes")
    sha = hashlib.sha256(b"png-bytes").hexdigest()

    def lookup(project_root, upload_id, source_sha256):
        src.write_bytes(b"swapped-after-approval")
        return approve(sha)(project_root, upload_id, source_sha256)

    jobs.register_upload_approval_lookup(lookup)
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.upload_reference(tmp_path, "up-1", src, model="m-turbo", mode="image2video")
    assert exc.value.kind == "upload_mismatch"
    assert not [c for c in calls(env) if c[:1] == ["upload"]]


@pytest.mark.parametrize("tamper", ["snapshot", "profile", "guarantee_receipt", "no_profile", "receipt_argv"])
def test_upload_url_for_rejects_tampering(env, monkeypatch, tmp_path, tamper, current_ok):
    profile = upload_profile(env)
    monkeypatch.setattr(jobs, "_ALLOW_FIXTURE_UPLOAD", True)
    src = tmp_path / "ref.png"
    src.write_bytes(b"png-bytes")
    jobs.register_upload_approval_lookup(approve(hashlib.sha256(b"png-bytes").hexdigest()))
    out = jobs.upload_reference(tmp_path, "up-1", src, model="m-turbo", mode="image2video")
    kwargs = {"profile": profile}
    if tamper == "snapshot":
        snap = cli.state_dir() / "uploads" / "up-1.source.png"
        snap.chmod(0o600)
        snap.write_bytes(b"other")
    elif tamper == "profile":
        kwargs["profile"] = dict(profile, tier="other-tier")  # stable binding field, not profile SHA
    elif tamper == "guarantee_receipt":
        up = dict(profile["upload"])
        up["receipts"] = [dict(e, receipt_sha256="0" * 64) if e["kind"] == "nonspending_guarantee" else e
                          for e in up["receipts"]]
        kwargs["profile"] = dict(profile, upload=up)
    elif tamper == "no_profile":
        kwargs = {}
    else:
        path = cli.receipt_path(out["receipt_id"])
        data = json.loads(path.read_bytes())
        data["argv"] = ["upload", "add", str(src), "--json", "--no-input"]
        raw = json.dumps(data, sort_keys=True).encode()
        path.chmod(0o600)
        path.write_bytes(raw)
        rec_path = cli.state_dir() / "uploads" / "up-1.json"
        record = json.loads(rec_path.read_bytes())
        record["receipt_sha256"] = hashlib.sha256(raw).hexdigest()
        rec_path.chmod(0o600)
        rec_path.write_bytes(jobs._canon(record))
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.upload_url_for("up-1", **kwargs)
    assert exc.value.kind == "upload_mismatch"


def test_upload_current_contract_change_makes_zero_uploads(env, monkeypatch, tmp_path):
    upload_profile(env)
    monkeypatch.setattr(jobs, "_ALLOW_FIXTURE_UPLOAD", True)

    def changed(profile, timeout):
        raise OpenArtCLIError("qualification_changed", "tier differs")
    monkeypatch.setattr(jobs, "_verify_current", changed)
    src = tmp_path / "ref.png"
    src.write_bytes(b"png-bytes")
    jobs.register_upload_approval_lookup(approve(hashlib.sha256(b"png-bytes").hexdigest()))
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.upload_reference(tmp_path, "up-1", src, model="m-turbo", mode="image2video")
    assert exc.value.kind == "qualification_changed"
    assert not [c for c in calls(env) if c[:1] == ["upload"]]


def test_real_unqualified_upload_makes_zero_calls(env, tmp_path):
    src = tmp_path / "ref.png"
    src.write_bytes(b"png")
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.upload_reference(tmp_path, "up-1", src, model="m-turbo", mode="image2video")
    assert exc.value.kind == "upload_unqualified"
    assert calls(env) == []


# ------------------------------------------------- staged first-account chain (actual fake CLI)
# Red evidence (2026-10-05, before qual._url/_hosts/_record reuse in promotion and full replay):
# numeric/localhost hosts and fragment/control result URLs promoted to `full`, and a modified
# retained creation_get/account stdout stream with unchanged receipt JSON still loaded `full`.

STAGED_PATHS = {"account_id": "id", "account_tier": "tier", "submit_job_id": "job", "result_job_id": "job",
                "status": "status", "urls": "urls", "status_terminal_ok": "done",
                "status_terminal_fail": "failed", "url_hosts": ["cdn.example.test"]}
STAGED_GUARANTEE = {"argv": ["account"], "nonspending": {"path": "upload.free", "expected": True},
                    "no_delayed_charge": {"path": "upload.noDelayed", "expected": True}}
STAGED_FAKE = r'''#!PYTHON
import json, os, sys
args = sys.argv[1:-2]
with open(os.environ["FAKE_LOG"], "a") as f: f.write(json.dumps(args) + "\n")
url = "https://cdn.example.test/ref.png?X-Amz-Signature=private"
g = {"free": True, "noDelayed": True}
if args == ["version"]: out = {"version": "1"}
elif args == ["account"]: out = {"id": os.environ.get("ACCOUNT", "private-account"), "tier": "turbo", "upload": g}
elif args[:2] == ["model", "form"]:
    out = {"properties": {"prompt": {"type": "string"}, "image": {"type": "string"},
           "duration": {"type": "integer", "default": 5}}, "required": ["prompt"]}
elif args[:2] == ["upload", "add"]: out = {"url": url, "upload": g}
elif args[:2] == ["creation", "get"]:
    out = {"job": os.environ.get("RESULT_JOB", args[2]), "status": os.environ.get("STATUS", "done"),
           "urls": [os.environ.get("RESULT_URL", "https://cdn.example.test/out.mp4?sig=1")]}
elif args[:2] == ["generate", "video"] and "--async" in args: out = {"job": "job-1"}
elif args[:2] == ["generate", "video"]:
    params = {"prompt": args[2]}
    if "--image" in args: params["image"] = url
    out = {"endpoint": "POST /api/cli/v1/generate", "body": {"model": "m1", "media": "video",
           "mode": "image2video" if "--image" in args else "text2video", "params": params}}
else: raise SystemExit(7)
print(json.dumps(out))
'''


@pytest.fixture
def staged(tmp_path, monkeypatch):
    binary = tmp_path / "fake-openart"
    binary.write_text(STAGED_FAKE.replace("PYTHON", sys.executable))
    binary.chmod(0o755)
    monkeypatch.setenv("OPENART_CLI_PATH", str(binary))
    monkeypatch.setenv("OPENMONTAGE_OPENART_STATE_DIR", str(tmp_path / "state"))
    log = tmp_path / "calls"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.delenv("OPENMONTAGE_OPENART_OFFLINE", raising=False)
    monkeypatch.setattr(jobs, "_ALLOW_FIXTURE_LAUNCH", False)
    jobs.register_upload_approval_lookup(
        lambda root, uid, sha: {"state": "approved", "upload_id": uid, "source_sha256": sha,
                                "approval_sha256": "a" * 64})
    jobs.register_account_check(None)
    jobs.register_reservation_lookup(None)
    yield {"tmp": tmp_path, "log": log}
    jobs.register_upload_approval_lookup(None)
    jobs.register_reservation_lookup(None)


def staged_calls(st):
    return [json.loads(x) for x in st["log"].read_text().splitlines()] if st["log"].exists() else []


def staged_pre_submit(st):
    from lib import openart_setup as setup
    setup.inspect_qualification("m1", "image2video", json_paths=STAGED_PATHS)
    setup.qualify_upload_guarantee("m1", "image2video", guarantee=STAGED_GUARANTEE,
                                   json_paths={"upload_url": "url"}, url_hosts=["cdn.example.test"])
    src = st["tmp"] / "project" / "ref.png"
    src.parent.mkdir(exist_ok=True)
    src.write_bytes(b"approved image")
    jobs.upload_reference(st["tmp"] / "project", "ref1", src, model="m1", mode="image2video")
    setup.qualify_preview("m1", "image2video", prompt="a fox", image_upload_id="ref1")
    profile = jobs.load_qualification(model="m1", mode="image2video", require="pre_submit")
    return profile, src


def staged_launch(st, attempt="qa-1", occurrence="occ-1", reservation="res-q", purpose=jobs.QUALIFICATION_PURPOSE):
    profile, src = staged_pre_submit(st) if "profile" not in st else (st["profile"], st["src"])
    st["profile"], st["src"] = profile, src
    dry = next(e for e in profile["captured_receipts"] if e["kind"] == "dry_run")
    project = st["tmp"] / "project"
    inputs = {"prompt": "a fox", "model": "m1", "operation": "image_to_video", "image_path": str(src),
              "image_upload_id": "ref1", "output_path": str(project / f"{attempt}.mp4"),
              "native_dry_run_receipt_id": dry["receipt_id"], "native_dry_run_receipt_sha256": dry["receipt_sha256"]}
    native = jobs.prepare_native_request(inputs, profile)
    snap = jobs.freeze_request(attempt, inputs, native, profile)
    binding = {"attempt_id": attempt, "request_sha256": "r" * 64, "reservation_id": reservation,
               "snapshot_sha256": snap["snapshot_sha256"],
               **{k: native[k] for k in ("native_controls_sha256", "native_argv_sha256", "profile_sha256",
                                         "account_id_sha256", "native_body_sha256")}}
    trusted = {"state": "active", "attempt_id": attempt, "request_sha256": "r" * 64,
               "reservation_id": reservation, "purpose": purpose, "authorization_occurrence_id": occurrence}
    jobs.register_reservation_lookup(lambda root, aid, req: dict(trusted) if aid == attempt else None)
    out = jobs.launch_submit(project, binding, native, profile, wait_timeout=20)
    return profile, out, project


def _fake_download(monkeypatch):
    from lib import openart_download as dl

    def fake(url, output_path, *, allowed_hosts, output_root, **kw):
        data = b"generated video bytes"
        Path(output_path).write_bytes(data)
        return {"path": str(output_path), "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "source_url": url, "source_host": "cdn.example.test"}
    monkeypatch.setattr(dl, "collect_output", fake)


def test_staged_first_account_full_chain(staged, monkeypatch):
    profile, out, project = staged_launch(staged)
    assert out["status"] == "submitted"
    assert sum("--async" in c for c in staged_calls(staged)) == 1
    with pytest.raises(OpenArtCLIError):
        jobs.load_qualification(model="m1", mode="image2video", require="full")
    promoted = jobs.promote_result_contract("qa-1", timeout=20)
    assert promoted["level"] == "full" and promoted["profile_sha256"] == profile["profile_sha256"]
    full = jobs.load_qualification(model="m1", mode="image2video", require="full")
    assert full == profile  # creative profile identity unchanged by result promotion
    eff = jobs.effective_result_contract("qa-1")
    assert eff["result_contract_sha256"] == promoted["result_contract_sha256"]
    status = jobs.qualification_status("m1", "image2video")
    assert status["level"] == "full" and status["result_contract_sha256"] == promoted["result_contract_sha256"]
    _fake_download(monkeypatch)
    got = jobs.collect_job("qa-1", output_path=project / "qa-1.mp4", output_root=project, profile=profile,
                           timeout=20)
    assert got["status"] == "collected", got
    stable = jobs.verify_collection_receipt("qa-1", profile)
    assert stable["output"]["sha256"] == hashlib.sha256(b"generated video bytes").hexdigest()
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.promote_result_contract("qa-1", timeout=20)
    assert exc.value.kind == "result_contract_exists"


def test_staged_ordinary_purpose_and_missing_occurrence_never_spawn(staged):
    for kw in ({"purpose": "ordinary"}, {"occurrence": None}):
        attempt = "o-" + str(len(staged_calls(staged)))
        with pytest.raises(OpenArtCLIError):
            staged_launch(staged, attempt=attempt, **kw)
    assert not any("--async" in c for c in staged_calls(staged))


def test_staged_same_occurrence_cannot_qualify_twice(staged):
    staged_launch(staged)
    with pytest.raises(OpenArtCLIError) as exc:
        staged_launch(staged, attempt="qa-2", reservation="res-other")
    assert exc.value.kind == "qualification_authorization_consumed"
    assert sum("--async" in c for c in staged_calls(staged)) == 1


@pytest.mark.parametrize("env_key,value", [("RESULT_JOB", "job-other"), ("ACCOUNT", "other"),
                                           ("RESULT_URL", "https://evil.example.test/out.mp4"),
                                           ("STATUS", "failed")])
def test_staged_promotion_rejects_mismatch(staged, monkeypatch, env_key, value):
    staged_launch(staged)
    monkeypatch.setenv(env_key, value)
    with pytest.raises(OpenArtCLIError):
        jobs.promote_result_contract("qa-1", timeout=20)
    assert jobs.qualification_status("m1", "image2video")["level"] == "pre_submit"


@pytest.mark.parametrize("paths,url", [
    ({"url_hosts": ["127.0.0.1"]}, "https://127.0.0.1/out.mp4"),
    ({"url_hosts": ["localhost"]}, "https://localhost/out.mp4"),
    ({"url_hosts": ["cdn.example.test.local"]}, "https://cdn.example.test.local/out.mp4"),
    ({}, "https://cdn.example.test/out.mp4#frag"),
    ({}, "https://cdn.example.test/out%0a.mp4"),
    ({"status_terminal_ok": "do\x01ne"}, None),
    ({"urls": "urls..0"}, None),
])
def test_staged_promotion_reuses_qualified_path_host_url_validation(staged, monkeypatch, paths, url):
    staged_launch(staged)
    if url:
        monkeypatch.setenv("RESULT_URL", url)
    with pytest.raises(OpenArtCLIError):
        jobs.promote_result_contract("qa-1", json_paths=paths or None, timeout=20)
    assert jobs.qualification_status("m1", "image2video")["level"] == "pre_submit"


def _proof(profile):
    target = jobs.result_proof_path(profile["model"], profile["mode"], profile["profile_sha256"])
    return target, json.loads(target.read_bytes())


def _rewrite(target, data):
    target.unlink()
    cli.write_private(target, json.dumps(data, sort_keys=True).encode())


def _row(model="m1", mode="image2video"):
    rows = [r for r in jobs.list_qualifications() if r["model"] == model and r["mode"] == mode]
    assert len(rows) == 1
    return rows[0]


def test_list_qualifications_reports_truthful_staged_levels(staged, monkeypatch):
    from lib import openart_setup as setup
    setup.inspect_qualification("m1", "image2video", json_paths=STAGED_PATHS)
    before = len(staged_calls(staged))
    row = _row()
    assert row["level"] == "inspected" and row["valid"] is False and row["error"]
    assert row["image2video_qualified"] is False
    profile, _, _ = staged_launch(staged)
    row = _row()
    assert row["level"] == "pre_submit" and row["valid"] is False
    assert row["profile_sha256"] == profile["profile_sha256"]
    assert row["result_contract_sha256"] is None
    promoted = jobs.promote_result_contract("qa-1", timeout=20)
    count = len(staged_calls(staged))
    row = _row()
    assert row["level"] == "full" and row["valid"] is True and row["error"] is None
    assert row["image2video_qualified"] is True
    assert row["profile_sha256"] == profile["profile_sha256"]
    assert row["result_contract_sha256"] == promoted["result_contract_sha256"]
    assert len(staged_calls(staged)) == count  # listing is pure: no CLI calls
    assert len(staged_calls(staged)) > before
    target, proof = _proof(profile)
    proof["json_paths"]["url_hosts"] = ["localhost"]
    _rewrite(target, proof)
    row = _row()
    assert row["level"] == "pre_submit" and row["valid"] is False and row["error"]
    assert row["result_contract_sha256"] is None
    assert len(staged_calls(staged)) == count


@pytest.mark.parametrize("which", ["account_receipt", "creation_get_receipt"])
def test_full_load_fails_when_retained_stdout_stream_modified(staged, which):
    profile, _, _ = staged_launch(staged)
    jobs.promote_result_contract("qa-1", timeout=20)
    _, proof = _proof(profile)
    entry = proof["evidence"][which]
    rec = json.loads(cli.receipt_path(entry["receipt_id"]).read_bytes())
    stream = cli.state_dir() / "streams" / rec["streams"]["stdout"]
    stream.write_bytes(stream.read_bytes().replace(b"}", b',"x":1}', 1))
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.load_qualification(model="m1", mode="image2video", require="full")
    assert jobs.qualification_status("m1", "image2video")["level"] == "pre_submit"


@pytest.mark.parametrize("mutate", [
    lambda p: p["json_paths"].update(url_hosts=["127.0.0.1"]),
    lambda p: p["json_paths"].update(url_hosts=["localhost"]),
    lambda p: p["json_paths"].update(status_terminal_ok="do\nne"),
    lambda p: p["json_paths"].update(urls="urls..x"),
])
def test_full_replay_revalidates_proof_paths_and_hosts(staged, mutate):
    profile, _, _ = staged_launch(staged)
    jobs.promote_result_contract("qa-1", timeout=20)
    target, proof = _proof(profile)
    mutate(proof)
    _rewrite(target, proof)
    with pytest.raises(OpenArtCLIError):
        jobs.load_qualification(model="m1", mode="image2video", require="full")


def test_full_replay_rejects_tampered_raw_submit(staged):
    profile, _, _ = staged_launch(staged)
    jobs.promote_result_contract("qa-1", timeout=20)
    raw = jobs.job_dir("qa-1", create=False) / "submit.stdout"
    raw.write_bytes(b'{"job":"job-2"}\n')
    with pytest.raises(OpenArtCLIError):
        jobs.load_qualification(model="m1", mode="image2video", require="full")


def test_exhausted_budget_never_spawns_staged(staged, monkeypatch):
    staged_pre_submit(staged)
    real = cli.lock_remaining
    state = {"n": 0}

    def exhausted(deadline):
        state["n"] += 1
        if state["n"] >= 2:
            raise OpenArtCLIError("timeout", "budget exhausted")
        return real(deadline)
    monkeypatch.setattr(cli, "lock_remaining", exhausted)
    with pytest.raises(OpenArtCLIError):
        staged_launch(staged)
    assert not any("--async" in c for c in staged_calls(staged))


def _rewrite_frozen(aid, mutate):
    path = jobs.job_dir(aid, create=False) / "frozen_request.json"
    snap = json.loads(path.read_bytes())
    mutate(snap)
    if snap["profile"].get("profile_sha256"):
        snap["profile"]["profile_sha256"] = jobs.sha256_json(
            {k: v for k, v in snap["profile"].items() if k != "profile_sha256"})
    snap["native"]["native_argv_sha256"] = jobs.sha256_json(snap["native"]["argv"])
    path.chmod(0o600)
    path.write_bytes(jobs._canon(snap))


_FROZEN_TAMPERS = {
    "native_body": lambda s: s["native"].__setitem__("native_body_sha256", "1" * 64),
    "native_argv": lambda s: s["native"].__setitem__("argv", s["native"]["argv"] + ["--duration", "9"]),
    "native_controls": lambda s: s["native"].__setitem__("native_controls_sha256", "2" * 64),
    "inputs_snapshot": lambda s: s["inputs"].__setitem__("prompt", "changed after approval"),
    "profile": lambda s: s["profile"].__setitem__("tier", "other"),
}


@pytest.mark.parametrize("tamper", sorted(_FROZEN_TAMPERS))
def test_full_proof_requires_untampered_origin_frozen_request(staged, tamper):
    staged_launch(staged)
    jobs.promote_result_contract("qa-1", timeout=20)
    jobs.load_qualification(model="m1", mode="image2video", require="full")
    _rewrite_frozen("qa-1", _FROZEN_TAMPERS[tamper])
    with pytest.raises(OpenArtCLIError):
        jobs.load_qualification(model="m1", mode="image2video", require="full")


@pytest.mark.parametrize("tamper", ["native_body", "inputs_snapshot"])
def test_promotion_refuses_tampered_origin_frozen_request(staged, tamper):
    staged_launch(staged)
    _rewrite_frozen("qa-1", _FROZEN_TAMPERS[tamper])
    calls = len(staged_calls(staged))
    with pytest.raises(OpenArtCLIError):
        jobs.promote_result_contract("qa-1", timeout=20)
    assert len(staged_calls(staged)) == calls
