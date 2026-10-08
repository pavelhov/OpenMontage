"""Offline contract coverage for all retained live OpenArt video form receipts.

The accompanying schemas retain only JSON Schema structure. They intentionally omit
defaults, examples, descriptions, and all sampled provider text.
"""
import json
import re
from pathlib import Path

import pytest

from lib.openart_controls import bind_native_inputs, mode_capabilities
from tools._openart_cli import OpenArtCLIError, validate_form_params


FIXTURE = Path(__file__).parents[1] / "fixtures" / "openart" / "live_form_schemas.json"
CATALOG = json.loads(FIXTURE.read_text())
FORMS = CATALOG["forms"]


def _form(row):
    root = row["schema"]
    schema = ({root["union"]: root["branches"]} if root["union"] else root["branches"][0])
    return {"model": row["model"], "mode": row["mode"], "media": "video", "schema": schema}


def _caps(row):
    return mode_capabilities(_form(row), model=row["model"], mode=row["mode"], cli_version="0.1.1")


def test_fixture_covers_all_17_catalog_models_and_45_live_form_receipts():
    assert len(FORMS) == 45
    assert len({row["model"] for row in FORMS}) == 17
    assert len({(row["model"], row["mode"]) for row in FORMS}) == 45
    probe = CATALOG["cli_011_image_probe"]
    assert probe["receipt_id"] == "2026-10-07T223851-592bce16"
    assert re.fullmatch(r"[a-f0-9]{64}", probe["receipt_sha256"])
    assert probe["observed_params"] == ["label", "type", "url"]
    assert probe["missing_from_observed_params"] == ["id"]
    for row in FORMS:
        assert re.fullmatch(r"2026-10-07T\d{6}-[a-f0-9]{8}", row["receipt_id"])
        assert re.fullmatch(r"[a-f0-9]{64}", row["receipt_sha256"])
        assert row["schema"]["union"] in (None, "anyOf", "oneOf")
        assert row["schema"]["branches"]


def test_fixture_contains_only_structural_json_schema_fields():
    schema_keywords = {
        "type", "properties", "required", "enum", "const", "minimum", "maximum",
        "exclusiveMinimum", "exclusiveMaximum", "minItems", "maxItems", "items",
        "anyOf", "oneOf", "additionalProperties",
    }

    def check_schema(node):
        assert isinstance(node, dict)
        assert set(node) <= schema_keywords
        for key, value in node.items():
            if key == "properties":
                assert isinstance(value, dict)
                for child in value.values():
                    check_schema(child)
            elif key in {"items", "additionalProperties"} and isinstance(value, dict):
                check_schema(value)
            elif key in {"anyOf", "oneOf"}:
                for child in value:
                    check_schema(child)

    for row in FORMS:
        for branch in row["schema"]["branches"]:
            check_schema(branch)


def test_every_retained_schema_is_analyzable_against_the_verified_cli_surface():
    for row in FORMS:
        caps = _caps(row)
        assert caps["model"] == row["model"]
        assert caps["mode"] == row["mode"]
        assert caps["cli_version"] == "0.1.1"
        assert re.fullmatch(r"[a-f0-9]{64}", caps["form_sha256"])
        assert re.fullmatch(r"[a-f0-9]{64}", caps["capabilities_sha256"])
        assert all(param["binding"] in {
            "prompt", "flag", "role", "transport_unsupported"
        } for param in caps["params"].values())
        assert caps["native_params_supported"] is False


def test_union_forms_are_preserved_and_evaluated_branch_by_branch():
    unions = [row for row in FORMS if row["schema"]["union"]]
    assert len(unions) == 6
    expected = {
        "kling-3-omni": 3,
        "kling-v3": 2,
        "smart-shot": 1,
    }
    assert {model: sum(row["model"] == model for row in unions) for model in expected} == expected
    for row in unions:
        caps = _caps(row)
        assert caps["union"] == row["schema"]["union"]
        assert len(caps["branches"]) == len(row["schema"]["branches"])


def test_h3_turbo_image_to_video_reports_exact_wire_form_mismatch():
    row = next(row for row in FORMS if row["model"] == "fal-h3-max-turbo" and row["mode"] == "image2video")
    caps = _caps(row)
    assert caps["roles"]["first_frame"]["native"] == "startFrame"
    assert caps["roles"]["first_frame"]["supported"] is False
    assert caps["roles"]["first_frame"]["reason"] == "CLI_omits_required_startFrame_id"
    assert caps["unreachable"] is True
    with pytest.raises(OpenArtCLIError) as exc:
        bind_native_inputs({
            "model": row["model"], "mode": row["mode"], "prompt": "sanitized test prompt",
            "input_assets": [{"role": "first_frame", "upload_id": "fixture-upload",
                              "source_sha256": "a" * 64}],
        }, caps)
    assert exc.value.kind == "mode_unreachable"


def test_h3_turbo_start_binding_does_not_hide_cli_form_id_mismatch():
    row = next(row for row in FORMS if row["model"] == "fal-h3-max-turbo" and row["mode"] == "image2video")
    # The guarded CLI preview carries label/type/url for startFrame, but no id.
    # The retained endpoint form requires id. Metadata and full native validation
    # both reject the mismatch; no synthetic id can repair the observed CLI wire.
    observed_preview_params = {"prompt": "sanitized test prompt", "startFrame": {
        "label": "first frame", "type": "image", "url": "https://cdn.example.invalid/frame.png"}}
    with pytest.raises(OpenArtCLIError) as exc:
        validate_form_params(_form(row), observed_preview_params, model=row["model"], mode=row["mode"])
    assert exc.value.kind == "control_invalid"
    compatible_params = {**observed_preview_params, "startFrame": {
        **observed_preview_params["startFrame"], "id": "fixture-asset"}}
    selected = validate_form_params(_form(row), compatible_params, model=row["model"], mode=row["mode"])
    assert "id" in selected["properties"]["startFrame"]["required"]


def test_cli_011_refuses_end_frame_and_multireference_forms_before_dispatch():
    image_row = next(row for row in FORMS if row["model"] == "fal-h3-max-turbo" and row["mode"] == "image2video")
    image_caps = _caps(image_row)
    assert image_caps["roles"]["last_frame"]["native"] == "endFrame"
    assert image_caps["roles"]["last_frame"]["supported"] is False
    with pytest.raises(OpenArtCLIError) as exc:
        bind_native_inputs({
            "model": image_row["model"], "mode": image_row["mode"], "prompt": "sanitized test prompt",
            "input_assets": [{"role": "last_frame", "upload_id": "fixture-upload",
                              "source_sha256": "b" * 64}],
        }, image_caps)
    assert exc.value.kind == "mode_unreachable"

    # Catalog element2video forms expose reference-image controls, but CLI 0.1.1
    # cannot select that mode, so binding refuses before any dispatch can occur.
    reference_rows = [row for row in FORMS if any("visualReferences" in b.get("properties", {})
                                                   for b in row["schema"]["branches"])]
    assert reference_rows
    for row in reference_rows:
        caps = _caps(row)
        assert caps["roles"]["reference_image"]["native"] == "visualReferences"
        assert caps["unreachable"] is True
        with pytest.raises(OpenArtCLIError) as exc:
            bind_native_inputs({"model": row["model"], "mode": row["mode"]}, caps)
        assert exc.value.kind == "mode_unreachable"


def test_native_audio_flags_are_visible_but_not_transportable():
    observed = 0
    for row in FORMS:
        caps = _caps(row)
        for name in ("generateAudio", "audio"):
            spec = caps["params"].get(name)
            if spec is None:
                continue
            observed += 1
            assert spec["binding"] == "transport_unsupported"
            if caps["unreachable"]:
                continue
            with pytest.raises(OpenArtCLIError) as exc:
                bind_native_inputs({"model": row["model"], "mode": row["mode"],
                                    "native_params": {name: False}}, caps)
            assert exc.value.kind == "transport_unsupported"
    assert observed
