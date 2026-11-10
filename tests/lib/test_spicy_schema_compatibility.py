"""Pure compatibility regressions using sanitized schema-only provider evidence."""
import json
from pathlib import Path

import pytest

from lib import spicy_catalog as catalog
from lib import spicy_jobs as jobs
from lib import spicy_uploads as uploads

FIXTURE = Path(__file__).parents[1] / "fixtures/spicy/seedance_fast_schema_2026-10-22.json"


def live_schema():
    return json.loads(FIXTURE.read_text())["inputSchema"]


def test_observed_schema_admits_exact_existing_engine_envelope():
    schema = live_schema()
    assert schema["properties"]["duration_seconds"]["enum"] == [-1, *range(4, 16)]
    assert catalog.schema_compatible(catalog.VIDEO_MODEL, schema)
    assert catalog.VIDEO_ROUTES[catalog.VIDEO_MODEL]["properties"]["duration_seconds"]["enum"] == list(range(4, 16))


@pytest.mark.parametrize("field", ["image_url", "last_image_url"])
@pytest.mark.parametrize("missing", sorted(catalog.SOURCE_UPLOAD_MIMES))
def test_missing_any_upload_mime_blocks_compatibility(field, missing):
    schema = live_schema()
    declared = schema["properties"][field]["contentMediaType"]
    declared.remove(missing)
    assert not catalog.schema_compatible(catalog.VIDEO_MODEL, schema)


@pytest.mark.parametrize("field", ["image_url", "last_image_url"])
@pytest.mark.parametrize("declaration", [
    "image/png", "image/*", "video/mp4", "", [], None, True,
    ["image/png", "image/jpeg", "image/webp", "application/unknown"],
    ["image/png", "image/jpeg", "image/webp", {}],
    {"image/png": True, "image/jpeg": True, "image/webp": True},
])
def test_narrow_unknown_or_malformed_media_declaration_is_not_ignored(field, declaration):
    schema = live_schema()
    schema["properties"][field]["contentMediaType"] = declaration
    assert not catalog.schema_compatible(catalog.VIDEO_MODEL, schema)


def test_provider_may_admit_only_the_complete_engine_upload_subset():
    schema = live_schema()
    for field in ("image_url", "last_image_url"):
        schema["properties"][field]["contentMediaType"] = sorted(catalog.SOURCE_UPLOAD_MIMES)
    assert catalog.schema_compatible(catalog.VIDEO_MODEL, schema)


@pytest.mark.parametrize("keyword,value", [
    ("contentEncoding", "base64"), ("contentSchema", {"const": "unexpected"}),
    ("pattern", "^https://"), ("const", "spicy://fixed"),
    ("allOf", [{"maxLength": 1}]),
])
def test_other_unhandled_source_keywords_still_block(keyword, value):
    schema = live_schema()
    schema["properties"]["image_url"][keyword] = value
    assert not catalog.schema_compatible(catalog.VIDEO_MODEL, schema)


@pytest.mark.parametrize("where", ["root", "prompt", "duration_seconds"])
def test_media_keyword_is_handled_only_on_source_fields(where):
    schema = live_schema()
    target = schema if where == "root" else schema["properties"][where]
    target["contentMediaType"] = sorted(catalog.SOURCE_UPLOAD_MIMES)
    assert not catalog.schema_compatible(catalog.VIDEO_MODEL, schema)


@pytest.mark.parametrize("missing", range(4, 16))
def test_wider_duration_enum_with_a_hole_is_not_compatible(missing):
    schema = live_schema()
    schema["properties"]["duration_seconds"]["enum"].remove(missing)
    assert not catalog.schema_compatible(catalog.VIDEO_MODEL, schema)


@pytest.mark.parametrize("replacement", [True, "4", 4.5])
def test_duration_enum_requires_the_actual_supported_values(replacement):
    schema = live_schema()
    schema["properties"]["duration_seconds"]["enum"][1] = replacement
    assert not catalog.schema_compatible(catalog.VIDEO_MODEL, schema)


@pytest.mark.parametrize("constraint", [{"minimum": 5}, {"maximum": 14}])
def test_other_duration_bounds_still_cover_complete_local_subset(constraint):
    schema = live_schema()
    schema["properties"]["duration_seconds"].update(constraint)
    assert not catalog.schema_compatible(catalog.VIDEO_MODEL, schema)


@pytest.mark.parametrize("duration", [-1, 3, 16, True, 4.0])
def test_vendor_wider_duration_does_not_expand_native_requests(duration):
    with pytest.raises(catalog.SpicyCatalogError):
        catalog.validate_native_input(catalog.VIDEO_MODEL,
            {"prompt": "synthetic fixture", "image_url": "spicy://f/fixture", "duration_seconds": duration})


def test_uploader_mime_subset_matches_compatibility_policy_without_adding_gif():
    emitted = {uploads._mime(b"\x89PNG\r\n\x1a\n"), uploads._mime(b"\xff\xd8\xff"),
               uploads._mime(b"RIFF\x00\x00\x00\x00WEBP")}
    assert emitted == catalog.SOURCE_UPLOAD_MIMES
    with pytest.raises(jobs.SpicyError):
        uploads._mime(b"GIF89a")


def test_diagnostic_reports_fixed_route_compatibility_without_observation_or_authority(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SPICY_SCHEMA_FIXTURE", "synthetic-schema-test-only")
    calls = []
    def transport(method, url, headers, body, timeout=None):
        assert method == "GET" and body is None
        calls.append(url)
        data = ({"available": "0.00", "held": "0.00", "total": "0.00"}
                if url.endswith("/chat/credit") else {"items": [json.loads(FIXTURE.read_text())]})
        return 200, {}, json.dumps({"code": 200, "data": data}).encode()
    client = jobs.SpicyClient("env:SPICY_SCHEMA_FIXTURE", transport=transport)
    result = jobs.connection_check("env:SPICY_SCHEMA_FIXTURE", client=client)
    assert result["video_route"]["callable"] is True
    assert result["authority"] == "none" and result["production_observation_written"] is False
    assert calls == [jobs.API_BASE + "/chat/credit", jobs.API_BASE + "/models?includeSchema=1"]
    assert list(tmp_path.iterdir()) == []
