"""Offline closeout regressions: truthful native-tier uncertainty and image MIME evidence."""
import copy
import hashlib
import io
import json
from pathlib import Path
import uuid

from PIL import Image
import pytest

from lib import spicy_catalog as catalog, spicy_jobs as jobs, spicy_lifecycle as lifecycle
from tests.lib.test_spicy_lifecycle_v2 import original, accept, collector, no_network

MODEL = "bytedance/seedance-2.0-fast/image-to-video"


def test_early_download_url_exception_is_pending_without_publication_or_new_task(original, monkeypatch):
    o = original; accept(o)
    o["http"].task.update(state="succeeded", output={"assets": [
        {"key": "ready.mp4", "mime": "video/mp4"}]})
    creates_before = [call for call in o["http"].calls if call[1] == "/jobs/createTask"]
    def unexpected_sdk_failure(*args, **kwargs):
        raise RuntimeError("Synthetic SDK failure before publication callback")
    monkeypatch.setattr(o["http"], "create_download_url", unexpected_sdk_failure)
    from lib import openart_download
    monkeypatch.setattr(openart_download, "collect_output", lambda *a, **k: pytest.fail("No download after SDK failure"))
    result = jobs.resume_original(o["root"], o["aid"])
    assert not result.success
    assert result.data["dispatch_status"] == "pending"
    assert result.data["error_category"] == "collection_unavailable"
    assert result.data["task_id"] == "synthetic-original"
    assert [call for call in o["http"].calls if call[1] == "/jobs/createTask"] == creates_before
    assert not Path(o["inputs"]["output_path"]).exists()
    record = json.loads(o["journal"].read_text())
    assert not record.get("collection_intents_v2") and not record.get("output")
    assert not [path for path in (o["root"] / "assets").rglob("*") if path.is_file()]


def native(**controls):
    return {"model": MODEL, "input": {"duration_seconds": 5, "resolution": "720p",
        "aspect_ratio": "adaptive", "generate_audio": False, **controls}}


def media(**overrides):
    return {"mime": "video/mp4", "width": 834, "height": 1112,
        "duration_seconds": 5.041667, "audio_streams": 0, **overrides}


def test_adaptive_native_resolution_label_is_explicitly_unverified():
    evidence = lifecycle._primary_controls_v2(native(), media())
    assert evidence["resolution"] == {"status": "unverified", "requested": "720p",
        "width": 834, "height": 1112, "model": MODEL,
        "reason": "provider_geometry_contract_unavailable"}
    # Even a 720 short edge does not prove the undocumented model tier.
    assert lifecycle._primary_controls_v2(native(), media(width=720, height=960))["resolution"]["status"] == "unverified"


@pytest.mark.parametrize("controls,actual", [
    ({"width": 832}, {}), ({"height": 1110}, {}),
    ({"aspect_ratio": "2:3"}, {"width": 720, "height": 960}),
    ({}, {"duration_seconds": 9}), ({}, {"duration_seconds": float("nan")}),
    ({}, {"audio_streams": 1}), ({"generate_audio": True}, {}),
    ({}, {"width": 0}), ({"resolution": "480p"}, {}),
])
def test_adaptive_unknown_never_relaxes_literal_controls_or_other_tiers(controls, actual):
    with pytest.raises(jobs.SpicyError):
        lifecycle._primary_controls_v2(native(**controls), media(**actual))


def test_other_model_retains_existing_resolution_rejection():
    value = native(); value["model"] = "synthetic/another-model"
    with pytest.raises(jobs.SpicyError, match="resolution"):
        lifecycle._primary_controls_v2(value, media())


def image_bytes(format="JPEG"):
    buffer = io.BytesIO(); Image.new("RGB", (64, 64), "blue").save(buffer, format=format)
    return buffer.getvalue()


def test_image_reconciliation_requires_signature_and_decode_agreement(tmp_path):
    path = tmp_path / "provider-declared.png"; path.write_bytes(image_bytes())
    asset = {"key": "provider-last.jpg", "mime": "image/png"}
    with pytest.raises(jobs.SpicyError, match="MIME"):
        lifecycle._probe_v2(path, native(), asset)
    result = lifecycle._probe_v2(path, native(), asset, reconcile_image_mime=True)
    assert result["mime"] == "image/jpeg"
    path.write_bytes(b"\xff\xd8\xffNOT-A-DECODABLE-IMAGE")
    with pytest.raises(jobs.SpicyError, match="decoding"):
        lifecycle._probe_v2(path, native(), asset, reconcile_image_mime=True)
    path.write_bytes(image_bytes())
    with pytest.raises(jobs.SpicyError, match="MIME"):
        lifecycle._probe_v2(path, native(), {"mime": "video/mp4"}, reconcile_image_mime=True)


@pytest.mark.parametrize("transport_mime", ["image/jpeg", "image/png", "application/octet-stream"])
def test_bonus_still_is_retained_with_actual_mime_and_immutable_declaration(original, monkeypatch, transport_mime):
    o = original; accept(o)
    real_probe = lifecycle._probe_v2
    def probe(path, native, asset, **kwargs):
        if asset["mime"].startswith("video/"):
            return {"mime": "video/mp4", "width": 64, "height": 64, "duration_seconds": 5}
        return real_probe(path, native, asset, **kwargs)
    monkeypatch.setattr(lifecycle, "_probe_v2", probe)
    raw = image_bytes()
    from lib import openart_download
    def collect(url, target, **kwargs):
        target = Path(target); temporary = target.parent / (".openart-download-" + uuid.uuid4().hex)
        image = "/last.jpg?" in url
        data = raw if image else b"synthetic-video"
        temporary.write_bytes(data)
        if image:
            assert kwargs["max_bytes"] == 10 * 1024 * 1024
            assert kwargs["expected_mime"] == "image/png"
            assert transport_mime in kwargs["accepted_mimes"]
        else:
            assert "accepted_mimes" not in kwargs
        kwargs["before_publish"]({"publication_source": str(temporary),
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            "mime": transport_mime if image else "video/mp4"})
        temporary.rename(target)
    monkeypatch.setattr(openart_download, "collect_output", collect)
    o["http"].task.update(state="succeeded", output={"assets": [
        {"key": "ready.mp4", "mime": "video/mp4"},
        {"key": "last.jpg", "mime": "image/png", "bytes": len(raw), "role": "last_frame"}]})
    result = jobs.resume_original(o["root"], o["aid"])
    assert result.success
    record = json.loads(o["journal"].read_text())
    intent = record["collection_intents_v2"]["last.jpg"]
    assert intent["asset"]["mime"] == "image/png"
    assert intent["mime"] == "image/jpeg"
    proof = intent["mime_reconciliation"]
    assert proof["transport_mime"] == transport_mime
    assert proof["provider_declared_mime"] == "image/png"
    assert proof["decoded_mime"] == proof["sniffed_mime"] == "image/jpeg"
    assert "provider_decoded_mime_mismatch" in proof["anomalies"]
    assert Path(intent["path"]).read_bytes() == raw
    assert len(result.data["outputs"]) == 2
    assert len([call for call in o["http"].calls if call[1] == "/jobs/createTask"]) == 2
    monkeypatch.setattr(o["http"], "request_json", lambda *a, **k: pytest.fail("Verified original must resume locally"))
    assert jobs.resume_original(o["root"], o["aid"]).success
    intent["mime_reconciliation"]["decoded_mime"] = "image/png"
    o["journal"].write_text(json.dumps(record))
    with pytest.raises(jobs.SpicyError, match="MIME evidence"):
        jobs.resume_original(o["root"], o["aid"])


@pytest.mark.parametrize("historical_read", [False, True])
def test_unverified_native_tier_is_bound_into_completion_receipt(original, monkeypatch, historical_read):
    import tests.lib.test_spicy_lifecycle_v2 as fixtures
    o = original
    monkeypatch.setattr(fixtures, "MODEL", MODEL)
    request = o["request"]; frozen = request["spicy_native"]
    frozen["model"] = MODEL
    frozen["input"].update(resolution="720p", aspect_ratio="adaptive", generate_audio=False)
    frozen["input_schema"]["properties"].update(resolution={"type": "string"},
        aspect_ratio={"type": "string"}, generate_audio={"type": "boolean"})
    frozen["schema_sha256"] = catalog.digest(frozen["input_schema"])
    frozen["request_sha256"] = catalog.digest({"model": MODEL, "input": frozen["input"]})
    request["spicy_authority"]["quote"].update(model=MODEL, native_request_sha256=frozen["request_sha256"])
    o["http"].task["model"] = MODEL
    catalog.atomic_write_json(o["directory"] / "request.json", request)
    accept(o); collector(monkeypatch)
    monkeypatch.setattr(lifecycle, "_probe_v2", lambda *args, **kwargs: media(width=720, height=960) if historical_read else media())
    o["http"].task.update(state="succeeded", output={"assets": [{"key": "ready.mp4", "mime": "video/mp4"}]})
    result = jobs.resume_original(o["root"], o["aid"])
    assert result.success and result.data["warnings"]
    record = json.loads(o["journal"].read_text())
    evidence = record["primary_control_verification_v2"]
    assert evidence["resolution"]["status"] == "unverified"
    assert result.data["primary_control_verification"] == result.data["conditioning_receipt"]["primary_control_verification"] == evidence
    monkeypatch.setattr(o["http"], "request_json", lambda *a, **k: pytest.fail("Local original only"))
    assert jobs.resume_original(o["root"], o["aid"]).success
    if historical_read:
        # Construct an old receipt shape, retaining the original byte and native
        # request proof, then prove a read derives truth without migration.
        del record["primary_control_verification_v2"]
        del record["conditioning_receipt"]["primary_control_verification"]
        o["journal"].write_text(json.dumps(record))
        original_journal = o["journal"].read_bytes()
        original_receipt = copy.deepcopy(record["conditioning_receipt"])
        resumed = jobs.resume_original(o["root"], o["aid"])
        assert resumed.success and resumed.data["warnings"]
        assert resumed.data["conditioning_receipt"] == original_receipt
        assert "primary_control_verification" not in original_receipt
        assert resumed.data["primary_control_verification"]["resolution"]["status"] == "unverified"
        assert resumed.data["primary_control_verification_provenance"] == {
            "scope": "derived_current_read", "source": "frozen_native_request_and_verified_local_media"}
        proof = lifecycle.validate_collected_original(o["root"], o["aid"])
        assert proof["receipt"] == original_receipt
        assert proof["primary_control_verification"] == resumed.data["primary_control_verification"]
        assert o["journal"].read_bytes() == original_journal
        return
    assert result.data["primary_control_verification_provenance"]["scope"] == "retained_completion"
    record["primary_control_verification_v2"]["resolution"]["status"] = "verified"
    o["journal"].write_text(json.dumps(record))
    with pytest.raises(jobs.SpicyError, match="control verification proof"):
        jobs.resume_original(o["root"], o["aid"])
