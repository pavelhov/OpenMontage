"""Typed canonical native inputs bound through jobs on the retained profile form receipt."""
import pytest

from lib import openart_jobs as jobs
from lib import openart_qualification as qual
from tools import _openart_cli as cli
from tools._openart_cli import OpenArtCLIError
from tests.lib.test_openart_jobs import env, calls, receipt, fixture_profile, store_profile  # noqa: F401

FORM = {"model": "m-turbo", "mode": "text2video", "media": "video",
        "jsonSchema": {"type": "object", "required": ["prompt"], "properties": {
            "prompt": {"type": "string"},
            "duration": {"type": "integer", "minimum": 5, "maximum": 15},
            "aspectRatio": {"type": "string", "enum": ["16:9", "9:16"]},
            "resolution": {"type": "string", "enum": ["480P", "768P"]},
            "seed": {"type": "integer"}}}}


def typed_profile(form=FORM, mode="text2video"):
    rid, rsha = receipt(form, cli.model_form_argv("m-turbo", mode) + cli.GLOBAL_FLAGS)
    return store_profile(fixture_profile(form_sha256=qual._hash(form),
                                         captured_receipts=[{"kind": "form", "receipt_id": rid,
                                                             "receipt_sha256": rsha}]))


def test_native_params_bind_through_retained_form(env):
    profile = typed_profile()
    native = jobs.native_request({"prompt": "a", "model": "m-turbo", "duration": 10,
                                  "native_params": {"aspectRatio": "9:16", "resolution": "768P"}}, profile)
    assert native["native_controls"] == {"prompt": "a", "model": "m-turbo", "mode": "text2video",
                                         "duration": 10, "aspect_ratio": "9:16", "resolution": "768P"}
    assert native["native_params"] == {"aspectRatio": "9:16", "resolution": "768P", "duration": 10}
    assert native["input_assets"] == []
    assert "--aspect-ratio" in native["argv"] or "9:16" in native["argv"]
    assert jobs.native_reference_digest({"prompt": "a", "model": "m-turbo", "duration": 10,
                                         "native_params": {"aspectRatio": "9:16", "resolution": "768P"}},
                                        profile, native=native) is None
    assert calls(env) == []


def test_legacy_syntax_shape_unchanged(env):
    profile = typed_profile()
    native = jobs.native_request({"prompt": "a", "model": "m-turbo", "duration": 10}, profile)
    assert "native_params" not in native and "input_assets" not in native


@pytest.mark.parametrize("extra,kind", [
    ({"native_params": {"resolution": "1080P"}}, "control_invalid"),
    ({"native_params": {"duration": 4}}, "control_invalid"),
    ({"native_params": {"fps": 24}}, "control_not_in_form"),
    ({"native_params": {"seed": 3}}, "transport_unsupported"),
    ({"duration": 5, "native_params": {"duration": 6}}, "ambiguous_role"),
    ({"last_image_path": "/tmp/x.png", "end_image_upload_id": "u"}, "control_not_in_form"),
    ({"input_assets": [{"role": "reference_audio", "upload_id": "u", "source_sha256": "a" * 64}]},
     "control_not_in_form"),
])
def test_typed_failures_before_any_call(env, extra, kind):
    profile = typed_profile()
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.native_request({"prompt": "a", "model": "m-turbo", **extra}, profile)
    assert exc.value.kind == kind
    assert calls(env) == []


def test_typed_inputs_need_retained_form_receipt(env):
    profile = store_profile(fixture_profile())
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.native_request({"prompt": "a", "model": "m-turbo", "native_params": {"duration": 5}}, profile)
    assert exc.value.kind == "generation_unqualified"


def test_tampered_form_receipt_rejected(env):
    profile = typed_profile()
    profile = dict(profile, form_sha256="0" * 64)
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.native_request({"prompt": "a", "model": "m-turbo", "native_params": {"duration": 5}}, profile)
    assert exc.value.kind == "generation_unqualified"


def test_h3_start_frame_requiring_id_stays_blocked(env):
    form = {"model": "m-turbo", "mode": "image2video", "media": "video",
            "jsonSchema": {"type": "object", "required": ["prompt", "startFrame"], "properties": {
                "prompt": {"type": "string"},
                "startFrame": {"type": "object", "required": ["id", "url"], "properties": {
                    "id": {"type": "string"}, "url": {"type": "string"}, "type": {"type": "string"},
                    "label": {"type": "string"}}},
                "endFrame": {"type": "object"}}}}
    rid, rsha = receipt(form, cli.model_form_argv("m-turbo", "image2video") + cli.GLOBAL_FLAGS)
    prof = fixture_profile(mode="image2video", form_sha256=qual._hash(form),
                           captured_receipts=[{"kind": "form", "receipt_id": rid, "receipt_sha256": rsha}])
    with pytest.raises(OpenArtCLIError) as exc:
        jobs._bind_typed_inputs({"prompt": "a", "model": "m-turbo", "operation": "image_to_video",
                                 "input_assets": [{"role": "first_frame", "upload_id": "u",
                                                   "source_path": "/tmp/x.png"}]}, prof)
    assert "CLI_omits_required_startFrame_id" in exc.value.message
    assert calls(env) == []


# ---------------------------------------------------------------- freeze regression coverage
import hashlib
import json

from tests.lib.test_openart_jobs import captured, approve, current_ok  # noqa: F401,E402

I2V_FORM = {"model": "m-turbo", "mode": "image2video", "media": "video",
            "jsonSchema": {"type": "object", "required": ["prompt", "startFrame"], "properties": {
                "prompt": {"type": "string"},
                "startFrame": {"type": "object", "additionalProperties": False, "required": ["url"],
                               "properties": {"label": {"type": "string", "maxLength": 12},
                                              "type": {"const": "image"},
                                              "url": {"type": "string", "pattern": "^https://up[.]openart[.]test/"}}},
                "duration": {"type": "integer", "minimum": 5, "maximum": 15}}}}
UP_URL = "https://up.openart.test/r.png"


def i2v_profile(form=I2V_FORM):
    rid, rsha = receipt(form, cli.model_form_argv("m-turbo", "image2video") + cli.GLOBAL_FLAGS)
    raw = {"contract": {"nonspending": True, "no_delayed_charge": True}, "url": UP_URL}
    prof = fixture_profile(mode="image2video", form_sha256=qual._hash(form),
                           captured_receipts=[{"kind": "form", "receipt_id": rid, "receipt_sha256": rsha}],
                           upload={"json_paths": {"upload_url": "url"}, "url_hosts": ["up.openart.test"],
                                   "guarantee": {"argv": ["account"],
                                                 "nonspending": {"path": "contract.nonspending", "expected": True},
                                                 "no_delayed_charge": {"path": "contract.no_delayed_charge",
                                                                       "expected": True}},
                                   "receipts": [captured("nonspending_guarantee", ["account"], raw),
                                                captured("upload", ["upload", "add", "/synthetic/ref.png"], raw)]})
    return store_profile(prof)


@pytest.fixture
def uploaded(env, monkeypatch, tmp_path, current_ok):
    profile = i2v_profile()
    monkeypatch.setattr(jobs, "_ALLOW_FIXTURE_UPLOAD", True)
    src = tmp_path / "start.png"
    src.write_bytes(b"start-frame-bytes")
    sha = hashlib.sha256(b"start-frame-bytes").hexdigest()
    jobs.register_upload_approval_lookup(approve(sha))
    jobs.upload_reference(tmp_path, "up-1", src, model="m-turbo", mode="image2video")
    return {"profile": profile, "src": src, "sha": sha}


def start_preview(profile, start=None, prompt="a", duration=None, image_key="startFrame", wire_duration=None):
    creative = cli.native_video_argv(prompt, model="m-turbo", mode="image2video", duration=duration,
                                     image_url=UP_URL)
    params = {"prompt": prompt}
    if duration is not None:
        params["duration"] = duration if wire_duration is None else wire_duration
    params[image_key] = UP_URL if image_key == "image" else (
        start or {"label": "start.png", "type": "image", "url": UP_URL})
    body = {"model": "m-turbo", "media": "video", "mode": "image2video", "params": params}
    return receipt({"endpoint": profile["dry_run_endpoint"], "body": body}, creative + ["--dry-run"] + cli.GLOBAL_FLAGS)


def typed_inputs(st, **extra):
    return {"prompt": "a", "model": "m-turbo",
            "input_assets": [{"role": "first_frame", "source_path": str(st["src"]), "upload_id": "up-1",
                              "source_sha256": st["sha"]}], **extra}


def legacy_inputs(st, **extra):
    return {"prompt": "a", "model": "m-turbo", "operation": "image_to_video",
            "image_path": str(st["src"]), "image_upload_id": "up-1", **extra}


def with_preview(inputs, rid, rsha):
    return dict(inputs, native_dry_run_receipt_id=rid, native_dry_run_receipt_sha256=rsha)


def test_typed_first_frame_end_to_end_request_and_digest(uploaded, env):
    st = uploaded
    rid, rsha = start_preview(st["profile"])
    inputs = with_preview(typed_inputs(st), rid, rsha)
    native = jobs.prepare_native_request(inputs, st["profile"])
    assert native["input_assets"] == [{"role": "first_frame", "native": "startFrame",
                                       "body_pointer": "params.startFrame", "upload_id": "up-1",
                                       "source_sha256": st["sha"], "url_sha256": jobs._sha(UP_URL)}]
    assert "--image" in native["argv"]
    digest = jobs.native_reference_digest(inputs, st["profile"], native=native)
    assert digest and digest == jobs.native_reference_digest(inputs, st["profile"], native=native)
    # Legacy syntax for the same bytes keeps the old shape (no native_params/input_assets) and
    # the same role-bound digest.
    legacy = with_preview(legacy_inputs(st), rid, rsha)
    old = jobs.prepare_native_request(legacy, st["profile"])
    assert "input_assets" not in old and "native_params" not in old
    assert jobs.native_reference_digest(legacy, st["profile"], native=old) == digest
    assert not [c for c in calls(env) if "--async" in c]


@pytest.mark.parametrize("tamper", ["path_bytes", "claimed_sha", "upload_id", "account", "native_pointer",
                                    "native_assets", "native_controls_type"])
def test_typed_first_frame_tamper_rejected(uploaded, env, tmp_path, tamper):
    st = uploaded
    rid, rsha = start_preview(st["profile"], duration=5)
    inputs = with_preview(typed_inputs(st, native_params={"duration": 5}), rid, rsha)
    native = jobs.prepare_native_request(inputs, st["profile"])
    profile = st["profile"]
    if tamper == "path_bytes":
        st["src"].write_bytes(b"swapped")
    elif tamper == "claimed_sha":
        inputs["input_assets"][0]["source_sha256"] = "0" * 64
    elif tamper == "upload_id":
        inputs["input_assets"][0]["upload_id"] = "up-2"
    elif tamper == "account":
        profile = dict(profile, account_id_sha256="0" * 64)
    elif tamper == "native_pointer":
        inputs["input_assets"][0]["body_pointer"] = "params.image"
    elif tamper == "native_assets":
        native = json.loads(json.dumps(native))
        native["input_assets"][0]["source_sha256"] = "1" * 64
    else:
        native = json.loads(json.dumps(native))
        native["native_params"]["duration"] = 5.0
    with pytest.raises(OpenArtCLIError):
        jobs.native_reference_digest(inputs, profile, native=native)
    assert not [c for c in calls(env) if "--async" in c or c[:1] == ["upload"] and c[2:3] != []
                and "up-1.source" not in c[2]]


def test_native_controls_bool_vs_int_drift_rejected(uploaded):
    st = uploaded
    rid, rsha = start_preview(st["profile"], duration=5)
    inputs = with_preview(typed_inputs(st, native_params={"duration": 5}), rid, rsha)
    native = json.loads(json.dumps(jobs.prepare_native_request(inputs, st["profile"])))
    native["native_controls"]["duration"] = 5.0
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.native_reference_digest(inputs, st["profile"], native=native)
    assert exc.value.kind in ("control_invalid", "upload_mismatch")


@pytest.mark.parametrize("syntax", ["typed", "legacy"])
@pytest.mark.parametrize("start", [
    {"label": "x" * 40, "type": "image", "url": UP_URL},   # label maxLength
    {"label": "start.png", "type": "image", "url": UP_URL, "id": "guess"},  # extra field / invented id
])
def test_start_frame_full_form_violation_fails_for_any_syntax(uploaded, syntax, start):
    st = uploaded
    rid, rsha = start_preview(st["profile"], start=start)
    inputs = typed_inputs(st) if syntax == "typed" else legacy_inputs(st)
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.prepare_native_request(with_preview(inputs, rid, rsha), st["profile"])
    assert exc.value.kind == "dry_run_mismatch"


def test_typed_request_never_accepts_legacy_params_image_wire(uploaded):
    st = uploaded
    rid, rsha = start_preview(st["profile"], image_key="image")
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.prepare_native_request(with_preview(typed_inputs(st), rid, rsha), st["profile"])
    assert exc.value.kind == "dry_run_mismatch"


def test_body_type_exact_duration_compare(uploaded):
    st = uploaded
    rid, rsha = start_preview(st["profile"], duration=5, wire_duration=5.0)  # wire 5.0 vs requested int 5
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.prepare_native_request(with_preview(legacy_inputs(st, duration=5), rid, rsha), st["profile"])
    assert exc.value.kind == "dry_run_mismatch"


def test_t2v_receipt_backed_preview_full_form_range(env):
    profile = typed_profile()
    creative = cli.native_video_argv("a", model="m-turbo", mode="text2video", duration=20)
    body = {"model": "m-turbo", "media": "video", "mode": "text2video", "params": {"prompt": "a", "duration": 20}}
    rid, rsha = receipt({"endpoint": profile["dry_run_endpoint"], "body": body},
                        creative + ["--dry-run"] + cli.GLOBAL_FLAGS)
    with pytest.raises(OpenArtCLIError) as exc:  # legacy syntax, receipt-backed, out of retained range
        jobs.prepare_native_request({"prompt": "a", "model": "m-turbo", "duration": 20,
                                     "native_dry_run_receipt_id": rid, "native_dry_run_receipt_sha256": rsha},
                                    profile)
    assert exc.value.kind == "dry_run_mismatch"
    assert calls(env) == []


@pytest.mark.parametrize("marker", ["schema_only", "unqualified_for_dispatch", "evidence_kind"])
def test_schema_only_probe_receipt_is_never_authority(env, marker):
    profile = store_profile(fixture_profile())
    creative = cli.native_video_argv("a", model="m-turbo", mode="text2video", duration=5)
    body = {"model": "m-turbo", "media": "video", "mode": "text2video", "params": {"prompt": "a", "duration": 5}}
    argv = creative + ["--dry-run"] + cli.GLOBAL_FLAGS
    stem = f"2026-10-05T000000-{hashlib.sha256(marker.encode()).hexdigest()[:8]}"
    data = json.dumps({"argv": argv, "parsed": {"endpoint": profile["dry_run_endpoint"], "body": body},
                       marker: True}, sort_keys=True).encode()
    cli.write_private(cli.receipt_path(stem), data)
    rsha = hashlib.sha256(data).hexdigest()
    with pytest.raises(OpenArtCLIError) as exc:
        jobs._load_receipt(stem, rsha, "native_preview_invalid")
    assert exc.value.kind == "native_preview_invalid"
    with pytest.raises(OpenArtCLIError) as exc:
        jobs.prepare_native_request({"prompt": "a", "model": "m-turbo", "duration": 5,
                                     "native_dry_run_receipt_id": stem, "native_dry_run_receipt_sha256": rsha},
                                    profile)
    assert exc.value.kind == "native_preview_invalid"
    # Qualification seam: a fully valid captured record (streams/returncode/started_at intact) is
    # accepted, and the SAME record differing only by the probe marker is rejected.
    entry = captured("dry_run", creative + ["--dry-run"], {"endpoint": profile["dry_run_endpoint"], "body": body})
    assert qual._record(entry)["returncode"] == 0
    path = cli.receipt_path(entry["receipt_id"])
    record = json.loads(path.read_bytes())
    record[marker] = True
    raw = json.dumps(record).encode()
    path.chmod(0o600)
    path.write_bytes(raw)
    with pytest.raises(qual.OpenArtQualificationError):
        qual._record(dict(entry, receipt_sha256=hashlib.sha256(raw).hexdigest()))
    assert calls(env) == []


UNION_FORM = {"model": "m-turbo", "mode": "text2video", "media": "video", "jsonSchema": {"anyOf": [
    {"type": "object", "required": ["prompt"], "properties": {
        "prompt": {"type": "string"}, "duration": {"const": -1}}},
    {"type": "object", "required": ["prompt", "duration"], "properties": {
        "prompt": {"type": "string"}, "duration": {"type": "integer", "minimum": 2, "maximum": 30}}}]}}


def test_union_form_view_keeps_branches_and_no_collapsed_defaults():
    defaults, has_prompt = qual.form_view(UNION_FORM, model="m-turbo", mode="text2video")
    assert defaults == {} and has_prompt is True


@pytest.mark.parametrize("duration,kind", [(10, None), (0, "control_invalid"), (31, "control_invalid"),
                                           # Form-valid sentinel, but CLI 0.1.1 --duration cannot carry it:
                                           # the transport gate fails closed (no unverified sentinel support).
                                           (-1, "invalid_argument")])
def test_union_typed_binding_validates_full_branches(env, duration, kind):
    profile = typed_profile(form=UNION_FORM)
    inputs = {"prompt": "a", "model": "m-turbo", "native_params": {"duration": duration}}
    if kind is None:
        assert jobs.native_request(inputs, profile)["native_params"]["duration"] == duration
    else:
        with pytest.raises(OpenArtCLIError) as exc:
            jobs.native_request(inputs, profile)
        assert exc.value.kind == kind
    assert calls(env) == []
