"""Offline tests for generic OpenArt native capability discovery and typed input binding."""
import pytest

from lib import openart_controls as oc
from tools._openart_cli import OpenArtCLIError

FRAME = {"type": "object", "additionalProperties": False,
         "properties": {"label": {"type": "string"}, "type": {"const": "image"},
                        "url": {"type": "string"}}, "required": ["url"]}


def h3_t2v(turbo=True):
    return {"model": "fal-h3-max-turbo" if turbo else "fal-h3-max", "mode": "text2video", "media": "video",
            "jsonSchema": {"type": "object", "required": ["prompt"], "properties": {
                "prompt": {"type": "string"},
                "aspectRatio": {"type": "string", "enum": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"]},
                "autoEnhancePrompt": {"type": "boolean"},
                "duration": {"type": "integer", "minimum": 5, "maximum": 15},
                "promptExpansionMode": {"type": "string", "enum": ["balanced"]},
                "resolution": {"type": "string", "enum": ["480P", "768P"] + (["1080P"] if turbo else [])},
                "seed": {"type": "integer"}, "videoCount": {"type": "integer", "minimum": 1, "maximum": 8}}}}


def h3_i2v():
    return {"model": "fal-h3-max-turbo", "mode": "image2video", "media": "video",
            "jsonSchema": {"type": "object", "required": ["prompt", "startFrame"], "properties": {
                "prompt": {"type": "string"}, "startFrame": FRAME, "endFrame": {},
                "duration": {"type": "integer", "minimum": 5, "maximum": 15},
                "resolution": {"type": "string", "enum": ["480P", "768P", "1080P"]},
                "seed": {"type": "integer"}}}}


def caps(form):
    return oc.mode_capabilities(form, model=form["model"], mode=form["mode"])


def test_h3_turbo_t2v_flags_and_unsupported_controls_are_explicit():
    c = caps(h3_t2v())
    assert not c["unreachable"]
    assert c["params"]["resolution"]["binding"] == "flag"
    assert c["params"]["resolution"]["enum"][-1] == "1080P"
    for name in ("seed", "videoCount", "autoEnhancePrompt", "promptExpansionMode"):
        assert c["params"][name]["binding"] == "transport_unsupported"
    bound = oc.bind_native_inputs({"prompt": "a", "resolution": "1080P", "duration": 10}, c)
    assert bound["expected_params"] == {"prompt": "a", "resolution": "1080P", "duration": 10}
    with pytest.raises(OpenArtCLIError) as e:
        oc.bind_native_inputs({"prompt": "a", "native_params": {"seed": 3}}, c)
    assert e.value.kind == "transport_unsupported"


def test_exact_case_sensitive_enum_and_range():
    c = caps(h3_t2v(turbo=False))
    for bad in ({"resolution": "1080P"}, {"resolution": "768p"}, {"duration": 4}, {"duration": True}):
        with pytest.raises(OpenArtCLIError) as e:
            oc.bind_native_inputs({"prompt": "a", **bad}, c)
        assert e.value.kind == "control_invalid"


def test_h3_i2v_start_frame_bound_end_frame_transport_unsupported():
    c = caps(h3_i2v())
    assert c["roles"]["first_frame"]["supported"] and c["roles"]["first_frame"]["native"] == "startFrame"
    assert c["roles"]["last_frame"] == {"native": "endFrame", "supported": False, "reason": "transport_unsupported"}
    legacy = oc.bind_native_inputs({"prompt": "a", "image_path": "/x.png", "image_upload_id": "u1"}, c)
    canon = oc.bind_native_inputs({"prompt": "a", "input_assets": [
        {"role": "first_frame", "source_path": "/x.png", "upload_id": "u1"}]}, c)
    assert legacy["input_assets"] == canon["input_assets"]
    assert canon["input_assets"][0]["native"] == "startFrame"
    with pytest.raises(OpenArtCLIError) as e:
        oc.bind_native_inputs({"prompt": "a", "image_path": "/x.png", "image_upload_id": "u1",
                               "last_image_path": "/y.png", "end_image_upload_id": "u2"}, c)
    assert e.value.kind == "transport_unsupported"
    with pytest.raises(OpenArtCLIError) as e:
        oc.bind_native_inputs({"prompt": "a"}, c)
    assert e.value.kind == "control_invalid"  # required startFrame missing


def test_conflicting_legacy_and_canonical_assets_fail_closed():
    c = caps(h3_i2v())
    with pytest.raises(OpenArtCLIError) as e:
        oc.bind_native_inputs({"prompt": "a", "image_upload_id": "u9", "image_path": "/x.png",
                               "input_assets": [{"role": "first_frame", "source_path": "/x.png", "upload_id": "u1"}]}, c)
    assert e.value.kind == "ambiguous_role"
    with pytest.raises(OpenArtCLIError) as e:
        oc.bind_native_inputs({"prompt": "a", "input_assets": [
            {"role": "first_frame", "source_path": "/a", "upload_id": "1"},
            {"role": "first_frame", "source_path": "/b", "upload_id": "2"}]}, c)
    assert e.value.kind == "ambiguous_role"
    with pytest.raises(OpenArtCLIError) as e:
        oc.bind_native_inputs({"prompt": "a", "native_params": {"startFrame": {}}}, c)
    assert e.value.kind == "ambiguous_role"


def test_element2video_unreachable_and_unknown_controls_rejected():
    form = {"model": "fal-h3-max", "mode": "element2video", "jsonSchema": {
        "type": "object", "required": ["prompt", "visualReferences"],
        "properties": {"prompt": {"type": "string"}, "visualReferences": {"type": "array"}}}}
    c = caps(form)
    assert c["unreachable"] and "element2video" in c["unreachable_reason"]
    assert c["roles"]["reference_image"]["native"] == "visualReferences"
    with pytest.raises(OpenArtCLIError) as e:
        oc.bind_native_inputs({"prompt": "a"}, c)
    assert e.value.kind == "mode_unreachable"
    with pytest.raises(OpenArtCLIError) as e:
        oc.bind_native_inputs({"prompt": "a", "native_params": {"nope": 1}}, caps(h3_t2v()))
    assert e.value.kind == "control_not_in_form"


def test_i2v_without_start_property_is_unreachable():
    form = {"model": "kling-v3", "mode": "image2video", "jsonSchema": {"type": "object", "properties": {}}}
    assert caps(form)["unreachable"]


def test_root_union_branches_validate_and_disambiguate():
    def branch(q):
        return {"type": "object", "additionalProperties": False, "required": ["prompt", "startFrame"],
                "properties": {"prompt": {"type": "string"}, "startFrame": FRAME,
                               "endFrame": {"anyOf": [FRAME, {"type": "null"}]},
                               "resolution": {"type": "string", "const": q}}}
    form = {"model": "kling-v3", "mode": "image2video",
            "jsonSchema": {"oneOf": [branch("std"), branch("pro")]}}
    c = caps(form)
    assert c["union"] == "oneOf" and not c["unreachable"]
    assert c["params"]["endFrame"]["nullable"] is True
    asset = {"role": "first_frame", "source_sha256": "a" * 64, "upload_id": "u"}
    got = oc.bind_native_inputs({"prompt": "a", "resolution": "pro", "input_assets": [asset]}, c)
    assert got["union_branch"] == 1
    with pytest.raises(OpenArtCLIError) as e:
        oc.bind_native_inputs({"prompt": "a", "input_assets": [asset]}, c)
    assert e.value.kind == "ambiguous_role"
    with pytest.raises(OpenArtCLIError) as e:
        oc.bind_native_inputs({"prompt": "a", "resolution": "4k", "input_assets": [asset]}, c)
    assert e.value.kind == "control_invalid"


def test_capabilities_tamper_and_unknown_transport_rejected():
    c = caps(h3_t2v())
    c["params"]["seed"]["binding"] = "flag"
    with pytest.raises(OpenArtCLIError) as e:
        oc.bind_native_inputs({"prompt": "a", "native_params": {"seed": 1}}, c)
    assert e.value.kind == "control_invalid"
    with pytest.raises(OpenArtCLIError) as e:
        oc.mode_capabilities(h3_t2v(), model="fal-h3-max-turbo", mode="text2video", cli_version="9.9")
    assert e.value.kind == "transport_unknown"


def test_union_wrapper_rejected_by_strict_legacy_parser():
    from tools import _openart_cli as cli
    form = {"jsonSchema": {"anyOf": [{"properties": {}}]}}
    with pytest.raises(OpenArtCLIError):
        cli.form_schema(form)
    assert cli.form_root_schema(form)["union"] == "anyOf"
    with pytest.raises(OpenArtCLIError):
        cli.form_root_schema({"jsonSchema": {"anyOf": [{"type": "object"}]}})


def test_full_parameter_union_bounds_and_false_null_constants():
    from tools import _openart_cli as cli
    form = {"properties": {"prompt": {"type": "string", "minLength": 2}, "duration": {
        "anyOf": [{"type": "integer", "const": -1}, {"type": "integer", "minimum": 2, "maximum": 30}]},
        "disabled": {"const": False}, "empty": {"const": None}}, "required": ["prompt"]}
    c = oc.mode_capabilities(form, model="wan3", mode="text2video")
    for duration in (-1, 2, 30):
        assert oc.bind_native_inputs({"prompt": "ok", "duration": duration}, c)
    for duration in (0, 999, True, "2"):
        with pytest.raises(OpenArtCLIError):
            oc.bind_native_inputs({"prompt": "ok", "duration": duration}, c)
    for key, value in (("disabled", 0), ("disabled", True), ("empty", False)):
        with pytest.raises(OpenArtCLIError):
            cli.validate_form_params(form, {"prompt": "ok", key: value})
    with pytest.raises(OpenArtCLIError):
        oc.bind_native_inputs({"prompt": "x"}, c)


def test_strict_root_wrappers_siblings_and_unique_full_branch():
    from tools import _openart_cli as cli
    b = {"type": "object", "properties": {"duration": {"type": "integer", "const": 2}},
         "required": ["duration"]}
    for bad in ({"schema": None, "jsonSchema": b}, {"schema": b, "properties": {}},
                {"anyOf": [b], "allOf": []}, {"oneOf": [b], "not": {}},
                {"oneOf": [b], "additionalProperties": False}, {"anyOf": [{"type": "array"}]}):
        with pytest.raises(OpenArtCLIError):
            cli.form_root_schema(bad)
    assert cli.exact_form_schema({"anyOf": [b]}, params={"duration": 2}) == b
    with pytest.raises(OpenArtCLIError):
        cli.exact_form_schema({"anyOf": [b, b]}, params={"duration": 2})


def test_shared_visual_reference_types_bounds_and_public_privacy():
    form = {"properties": {"prompt": {"type": "string", "default": "PRIVATE PROMPT"},
        "visualReferences": {"type": "array", "minItems": 1, "maxItems": 4, "items": {
            "oneOf": [{"type": "object", "properties": {"type": {"const": kind},
                "url": {"type": "string", "default": "https://private.example/secret"}}}
                for kind in ("image", "video", "audio")]}}}}
    c = oc.mode_capabilities(form, model="h3", mode="element2video")
    for role in ("reference_image", "reference_video", "reference_audio"):
        row = c["roles"][role]
        assert row["native"] == "visualReferences" and row["supported"] is False
        assert row["min_items"] == 1 and row["max_items"] == 4
        assert row["accepted_types"] == ["audio", "image", "video"]
    import json
    public = json.dumps(oc.public_capabilities(c))
    assert "PRIVATE PROMPT" not in public and "private.example" not in public


def test_cli_image_wire_omits_form_required_id_and_never_binds():
    form = h3_i2v()
    form['jsonSchema']['properties']['startFrame'] = {
        'type': 'object', 'properties': {'id': {'type': 'string'}, 'url': {'type': 'string'}},
        'required': ['id', 'url']}
    c = caps(form)
    assert c['unreachable']
    assert not c['roles']['first_frame']['supported']
    assert not c['roles']['first_frame']['wire_schema_compatible']
    assert c['roles']['first_frame']['reason'] == 'CLI_omits_required_startFrame_id'
    assert 'CLI_omits_required_startFrame_id' in c['unreachable_reason']
    with pytest.raises(OpenArtCLIError) as err:
        oc.bind_native_inputs({'prompt': 'a', 'image_path': '/x.png', 'image_upload_id': 'u'}, c)
    assert err.value.kind == 'mode_unreachable'


@pytest.mark.parametrize('frame,reason', [
    ({'type': 'array'}, 'CLI_wire_type_incompatible_startFrame'),
    ({'type': 'object', 'properties': {'type': {'const': 'video'}}}, 'CLI_wire_value_incompatible_startFrame_type'),
    ({'type': 'object', 'properties': {'url': {'type': 'integer'}}}, 'CLI_wire_type_incompatible_startFrame_url'),
    ({'type': 'object', 'additionalProperties': False, 'properties': {'url': {}}}, 'CLI_sends_disallowed_startFrame_label'),
])
def test_known_image_wire_type_const_and_extra_field_mismatches(frame, reason):
    form = h3_i2v()
    form['jsonSchema']['properties']['startFrame'] = frame
    c = caps(form)
    assert c['unreachable'] and c['roles']['first_frame']['reason'] == reason


def test_image_wire_nullable_union_keeps_full_compatible_branch():
    form = h3_i2v()
    form['jsonSchema']['properties']['startFrame'] = {'anyOf': [FRAME, {'type': 'null'}]}
    c = caps(form)
    assert not c['unreachable'] and c['roles']['first_frame']['supported']


@pytest.mark.parametrize('native,alias', [(True, 5), (True, 1), (1, True), (5.0, 5), (5, 5.0)])
def test_duplicate_flag_alias_does_not_mask_json_type_conflict(native, alias):
    with pytest.raises(OpenArtCLIError) as err:
        oc.bind_native_inputs({'prompt': 'a', 'duration': alias, 'native_params': {'duration': native}},
                              caps(h3_t2v()))
    assert err.value.kind == 'ambiguous_role'


def test_identical_duplicate_flag_alias_values_bind_normally():
    bound = oc.bind_native_inputs({'prompt': 'a', 'duration': 5, 'resolution': '1080P',
                                  'native_params': {'duration': 5, 'resolution': '1080P'}}, caps(h3_t2v()))
    assert bound['native_params'] == {'duration': 5, 'resolution': '1080P'}


@pytest.mark.parametrize('left,right', [({'a': [True]}, {'a': [1]}), ({'a': [1]}, {'a': [1.0]}),
                                        ([False], [0]), ({'a': {'v': 1}}, {'a': {'v': True}})])
def test_recursive_alias_equality_preserves_json_types(left, right):
    assert not oc._same_json_value(left, right)
    assert oc._same_json_value(left, left)
