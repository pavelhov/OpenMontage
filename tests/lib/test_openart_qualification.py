"""Synthetic raw receipts test validator structure; never live account qualification."""
import copy
import hashlib
import json

import pytest

from tools import _openart_cli as cli
from lib import openart_qualification as qualification

Error = qualification.OpenArtQualificationError


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.fixture
def synthetic_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMONTAGE_OPENART_STATE_DIR", str(tmp_path / "private-state"))
    form = {"properties": {"prompt": {"type": "string"}, "duration": {"type": "integer", "default": 5}},
            "required": ["prompt"]}
    creative = ["generate", "video", "synthetic fox", "--model", "m-turbo", "--duration", "5"]
    bodies = {
        "version": (["version"], {"version": "0.1.1"}),
        "account": (["account"], {"user": {"id": "synthetic-account", "tier": "subscription"}}),
        "form": (cli.model_form_argv("m-turbo", "text2video"), form),
        "dry_run": (creative + ["--dry-run"], {"endpoint": "POST /api/cli/v1/generate", "body": {
            "model": "m-turbo", "media": "video", "mode": "text2video",
            "params": {"prompt": "synthetic fox", "duration": 5}}}),
        "submit": (creative + ["--async"], {"job": {"id": "synthetic-job"}}),
        "creation_get": (["creation", "get", "synthetic-job"], {"creation": {
            "id": "synthetic-job", "status": "done", "urls": ["https://cdn.openart.test/video.mp4?sig=synthetic"]}}),
    }
    profile = {"version": "1", "source": "real", "cli_version": "0.1.1",
        "account_id_sha256": hashlib.sha256(b"synthetic-account").hexdigest(), "tier": "subscription",
        "model": "m-turbo", "mode": "text2video", "form_sha256": digest(form), "form_defaults": {"duration": 5},
        "dry_run_endpoint": "POST /api/cli/v1/generate", "json_paths": {
            "account_id": "user.id", "account_tier": "user.tier", "submit_job_id": "job.id",
            "result_job_id": "creation.id", "status": "creation.status", "status_terminal_ok": "done",
            "status_terminal_fail": "failed", "urls": "creation.urls", "url_hosts": ["cdn.openart.test"]},
        "captured_receipts": []}
    counter = 0

    def capture(kind, argv, parsed):
        nonlocal counter
        counter += 1
        stem = f"2026-10-05T000000-{counter:08x}"
        raw = json.dumps(parsed).encode()
        cli.write_private(cli.private_dir("streams") / f"{stem}.stdout", raw)
        cli.write_private(cli.private_dir("streams") / f"{stem}.stderr", b"")
        record = {"argv": argv + cli.GLOBAL_FLAGS, "started_at": "2026-10-05T00:00:00+00:00",
            "returncode": 0, "stdout_sha256": hashlib.sha256(raw).hexdigest(), "parsed": parsed,
            "streams": {"stdout": f"{stem}.stdout", "stderr": f"{stem}.stderr"}}
        data = json.dumps(record).encode()
        cli.write_private(cli.receipt_path(stem), data)
        return {"kind": kind, "receipt_id": stem, "receipt_sha256": hashlib.sha256(data).hexdigest()}

    def refresh(kind, *, argv=None, parsed=None):
        oldargv, oldparsed = bodies[kind]
        bodies[kind] = (argv if argv is not None else oldargv, parsed if parsed is not None else oldparsed)
        entry = capture(kind, *bodies[kind])
        profile["captured_receipts"] = [entry if e["kind"] == kind else e for e in profile["captured_receipts"]]

    profile["captured_receipts"] = [capture(kind, *value) for kind, value in bodies.items()]
    return profile, bodies, refresh, capture


def test_synthetic_complete_structure_only(synthetic_contract):
    profile, *_ = synthetic_contract
    assert qualification.validate_profile(profile)["source"] == "real"


@pytest.mark.parametrize("field,value", [("cli_version", "9.9"), ("tier", "invented"),
    ("form_defaults", {"duration": 99}), ("model", "m-other"), ("mode", "image2video")])
def test_refreshed_profile_hash_does_not_hide_binding_changes(synthetic_contract, field, value):
    profile, *_ = synthetic_contract
    profile[field] = value
    with pytest.raises(Error):
        qualification.validate_profile(profile)


@pytest.mark.parametrize("kind,parsed", [
    ("creation_get", {"creation": {"id": "other-job", "status": "done", "urls": ["https://cdn.openart.test/v"]}}),
    ("creation_get", {"creation": {"id": "synthetic-job", "status": "running", "urls": []}}),
    ("creation_get", {"creation": {"id": "synthetic-job", "status": "done", "urls": ["https://other.test/v"]}}),
    ("version", {"version": "99"}),
])
def test_refreshed_receipt_sha_does_not_hide_result_contract_changes(synthetic_contract, kind, parsed):
    profile, _, refresh, _ = synthetic_contract
    refresh(kind, parsed=parsed)
    with pytest.raises(Error):
        qualification.validate_profile(profile)


def test_duplicate_kind_refused(synthetic_contract):
    profile, *_ = synthetic_contract
    profile["captured_receipts"].append(copy.deepcopy(profile["captured_receipts"][0]))
    with pytest.raises(Error):
        qualification.validate_profile(profile)


def test_missing_real_captures(synthetic_contract):
    profile, *_ = synthetic_contract
    profile.pop("captured_receipts")
    with pytest.raises(Error):
        qualification.validate_profile(profile)


@pytest.mark.parametrize("problem", ["sha", "permissions", "stdout", "unknown_kind", "unbound_metadata"])
def test_raw_private_evidence_refuses_corruption(synthetic_contract, problem):
    profile, *_ = synthetic_contract
    entry = profile["captured_receipts"][0]
    path = cli.receipt_path(entry["receipt_id"])
    if problem == "sha":
        entry["receipt_sha256"] = "0" * 64
    elif problem == "permissions":
        path.chmod(0o644)
    elif problem == "stdout":
        record = json.loads(path.read_bytes())
        (cli.state_dir() / "streams" / record["streams"]["stdout"]).write_text('{}')
    elif problem == "unknown_kind":
        entry["kind"] = "invented"
    else:
        entry["account"] = "unbound caller label"
    with pytest.raises(Error):
        qualification.validate_profile(profile)


@pytest.mark.parametrize("url", ["http://cdn.openart.test/v", "https://user@cdn.openart.test/v",
    "https://cdn.openart.test:444/v", "https://cdn.openart.test/v#fragment",
    "https://cdn.openart.test/v%0a", "https://cdn.openart.test/v\n", "https://cdn.openart.test\\evil/v"])
def test_unsafe_result_urls_refused(synthetic_contract, url):
    profile, _, refresh, _ = synthetic_contract
    refresh("creation_get", parsed={"creation": {"id": "synthetic-job", "status": "done", "urls": [url]}})
    with pytest.raises(Error):
        qualification.validate_profile(profile)


@pytest.mark.parametrize("host", ["*", "cdn.openart.test/path", "user@cdn.openart.test", "cdn.openart.test:443",
    "cdn.openart.test\n", "-bad.test", "a..test"])
def test_unsafe_qualified_hosts_refused(synthetic_contract, host):
    profile, *_ = synthetic_contract
    profile["json_paths"]["url_hosts"] = [host]
    with pytest.raises(Error):
        qualification.validate_profile(profile)


@pytest.mark.parametrize("kind,argv", [
    ("form", ["model", "form", "wrong-model", "text2video"]),
    ("dry_run", ["generate", "video", "other prompt", "--model", "m-turbo", "--duration", "5", "--dry-run"]),
    ("submit", ["generate", "video", "other prompt", "--model", "m-turbo", "--duration", "5", "--async"]),
    ("creation_get", ["creation", "get", "other-job"]),
])
def test_refreshed_argv_does_not_hide_wrong_contract(synthetic_contract, kind, argv):
    profile, _, refresh, _ = synthetic_contract
    refresh(kind, argv=argv)
    with pytest.raises(Error):
        qualification.validate_profile(profile)


def synthetic_upload(synthetic_contract):
    profile, _, _, capture = synthetic_contract
    raw = {"contract": {"nonspending": True, "no_delayed_charge": True}, "url": "https://upload.openart.test/r.png"}
    profile["upload"] = {"json_paths": {"upload_url": "url"}, "url_hosts": ["upload.openart.test"],
        "guarantee": {"argv": ["account"],
            "nonspending": {"path": "contract.nonspending", "expected": True},
            "no_delayed_charge": {"path": "contract.no_delayed_charge", "expected": True}},
        "receipts": [capture("nonspending_guarantee", ["account"], raw),
                     capture("upload", ["upload", "add", "/synthetic/private/ref.png"], raw)]}
    return profile, raw, capture


def test_synthetic_structured_upload_contract_only(synthetic_contract):
    profile, _, _ = synthetic_upload(synthetic_contract)
    qualification.validate_upload_contract(profile)


@pytest.mark.parametrize("problem", ["label_only", "wrong_value", "missing_delayed", "truthy_expected",
    "bool_zero", "wrong_argv", "duplicate", "receipt_sha", "immediate_balance", "user_attestation"])
def test_upload_guarantee_is_raw_typed_and_complete(synthetic_contract, problem):
    profile, raw, capture = synthetic_upload(synthetic_contract)
    upload = profile["upload"]
    if problem == "label_only":
        upload.pop("guarantee")
    elif problem == "truthy_expected":
        upload["guarantee"]["no_delayed_charge"]["expected"] = "yes"
    elif problem == "bool_zero":
        upload["guarantee"]["nonspending"]["expected"] = 0
    elif problem == "wrong_argv":
        upload["receipts"][1] = capture("upload", ["upload", "add", "relative.png"], raw)
    elif problem == "duplicate":
        upload["receipts"][1] = upload["receipts"][0]
    elif problem == "receipt_sha":
        upload["receipts"][0]["receipt_sha256"] = "0" * 64
    else:
        raw = copy.deepcopy(raw)
        if problem == "wrong_value":
            raw["contract"]["nonspending"] = False
        elif problem == "missing_delayed":
            raw["contract"].pop("no_delayed_charge")
        elif problem == "immediate_balance":
            raw = {"balance_before": 100, "balance_after": 100, "url": raw["url"]}
        else:
            raw = {"user_attestation": "no spending", "url": raw["url"]}
        upload["receipts"][1] = capture("upload", ["upload", "add", "/synthetic/private/ref.png"], raw)
    with pytest.raises(Error) as exc:
        qualification.validate_upload_contract(profile)
    assert exc.value.kind == "unsupported_gate"


def test_fixture_never_becomes_real_evidence(synthetic_contract):
    profile, *_ = synthetic_contract
    profile["source"] = "fixture"
    profile.pop("captured_receipts")
    profile["json_paths"].pop("account_tier")
    with pytest.raises(Error):
        qualification.validate_profile(profile)
    assert qualification.validate_profile(profile, allow_fixture=True)["source"] == "fixture"


def test_refreshed_form_hash_still_requires_observed_defaults(synthetic_contract):
    profile, bodies, refresh, _ = synthetic_contract
    form = copy.deepcopy(bodies["form"][1])
    form["properties"]["duration"]["default"] = 9
    refresh("form", parsed=form)
    profile["form_sha256"] = digest(form)
    with pytest.raises(Error):
        qualification.validate_profile(profile)


@pytest.mark.parametrize("field,value", [("mode", "image2video"), ("model", "other-model"), ("media", "image")])
def test_refreshed_native_body_sha_still_binds_model_mode(synthetic_contract, field, value):
    profile, bodies, refresh, _ = synthetic_contract
    dry = copy.deepcopy(bodies["dry_run"][1])
    dry["body"][field] = value
    refresh("dry_run", parsed=dry)
    with pytest.raises(Error):
        qualification.validate_profile(profile)


@pytest.mark.parametrize("field,value", [("returncode", 1), ("returncode", False),
    ("stdout_sha256", "0" * 64), ("parsed", {"version": "different"}),
    ("streams", {"stdout": "../escape", "stderr": "also"})])
def test_refreshed_record_sha_still_validates_transport_fields(synthetic_contract, field, value):
    profile, *_ = synthetic_contract
    entry = profile["captured_receipts"][0]
    record = json.loads(cli.receipt_path(entry["receipt_id"]).read_bytes())
    record[field] = value
    stem = "2026-10-05T000000-ffffffff"
    raw = json.dumps(record).encode()
    cli.write_private(cli.receipt_path(stem), raw)
    entry.update(receipt_id=stem, receipt_sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(Error):
        qualification.validate_profile(profile)


def test_failed_terminal_result_does_not_invent_output_success(synthetic_contract):
    profile, _, refresh, _ = synthetic_contract
    refresh("creation_get", parsed={"creation": {"id": "synthetic-job", "status": "failed", "urls": []}})
    with pytest.raises(Error):
        qualification.validate_profile(profile)


@pytest.mark.parametrize("host", ["2130706433", "0x7f.1", "0x7f000001", "127.01", "example.0x7f"])
def test_address_like_qualified_hosts_match_downloader_refusal(synthetic_contract, host):
    profile, *_ = synthetic_contract
    profile["json_paths"]["url_hosts"] = [host]
    profile["source"] = "fixture"
    with pytest.raises(Error):
        qualification.validate_profile(profile, allow_fixture=True)


@pytest.mark.parametrize("different_url", [False, True])
def test_image_preview_binds_exact_retained_upload_url(synthetic_contract, different_url):
    profile, bodies, refresh, _ = synthetic_contract
    synthetic_upload(synthetic_contract)
    profile["mode"] = "image2video"
    form = copy.deepcopy(bodies["form"][1])
    form["properties"]["image"] = {"type": "string"}
    refresh("form", argv=cli.model_form_argv("m-turbo", "image2video"), parsed=form)
    profile["form_sha256"] = digest(form)
    image_url = "https://upload.openart.test/other.png" if different_url else "https://upload.openart.test/r.png"
    creative = ["generate", "video", "synthetic fox", "--model", "m-turbo", "--duration", "5", "--image", image_url]
    dry = copy.deepcopy(bodies["dry_run"][1])
    dry["body"]["mode"] = "image2video"
    dry["body"]["params"]["image"] = image_url
    refresh("dry_run", argv=creative + ["--dry-run"], parsed=dry)
    refresh("submit", argv=creative + ["--async"])
    if different_url:
        with pytest.raises(Error):
            qualification.validate_profile(profile)
    else:
        assert qualification.validate_profile(profile)["mode"] == "image2video"


def test_oversized_retained_receipt_refused_without_unbounded_read(synthetic_contract, monkeypatch):
    profile, *_ = synthetic_contract
    entry = profile["captured_receipts"][0]
    raw = cli.receipt_path(entry["receipt_id"]).read_bytes()
    monkeypatch.setattr(cli, "MAX_STDOUT", len(raw) - 1)
    with pytest.raises(Error):
        qualification.validate_profile(profile)
