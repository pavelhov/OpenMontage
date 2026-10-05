"""Selector -> SchemaMedia execution path must not duplicate reference images.

ImageSelector copies canonical ``image_path(s)``/``image_url(s)`` into a native
``images`` array for providers that only accept ``images``. SchemaMedia
adapters (e.g. AtlasRefreshImage) also accept the canonical fields and encode
local paths as data URIs themselves, so copying would submit every reference
twice -- once as a raw filesystem path string.
"""

from __future__ import annotations

import base64

import pytest

import tools.schema_media as schema_media
from tools.base_tool import ToolResult, ToolStatus
from tools.graphics.atlas_refresh_image import AtlasRefreshImage
from tools.graphics.image_selector import ImageSelector


@pytest.fixture
def captured(monkeypatch):
    calls: list[tuple[str, str, dict]] = []
    media = "data:image/png;base64," + base64.b64encode(b"owned-output").decode()

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if method == "POST":
            return {"data": {"id": "offline-job"}}
        return {"data": {"status": "completed", "outputs": [media]}}

    monkeypatch.setattr(schema_media, "request_json", fake_request)
    monkeypatch.setenv("ATLASCLOUD_API_KEY", "offline-test-key")
    return calls


def test_selector_does_not_duplicate_canonical_refs_into_native_images(
    monkeypatch, captured, tmp_path
):
    source = tmp_path / "source.png"
    source.write_bytes(b"source-bytes")
    tool = AtlasRefreshImage()
    monkeypatch.setattr(tool, "get_status", lambda: ToolStatus.AVAILABLE)
    selector = ImageSelector()
    monkeypatch.setattr(selector, "_providers", lambda: [tool])

    result = selector.execute(
        {
            "prompt": "Keep this source",
            "preferred_tool": tool.name,
            "model": "gpt-image-2.5-flare",
            "image_path": str(source),
            "image_url": "https://owned.example/ref.png",
            "output_path": str(tmp_path / "out.png"),
            "poll_interval": 0.01,
        }
    )

    assert result.success, result.error
    posts = [kwargs["json"] for method, _, kwargs in captured if method == "POST"]
    assert len(posts) == 1
    expected_data_uri = (
        "data:image/png;base64," + base64.b64encode(b"source-bytes").decode()
    )
    assert posts[0]["images"] == ["https://owned.example/ref.png", expected_data_uri]
    assert str(source) not in posts[0]["images"]
    assert "preferred_tool" not in posts[0]
    assert "hosting_provider" not in posts[0]


def test_legacy_images_only_provider_still_gets_normalized_refs(monkeypatch):
    """Providers exposing only a native ``images`` array keep the old mapping."""

    class _LegacyImagesTool:
        name = "legacy_images_tool"
        provider = "legacy"
        capability = "image_generation"
        supports = {"text_to_image": True, "image_edit": True}
        best_for: list[str] = []
        input_schema = {"properties": {"prompt": {}, "images": {}}}

        def __init__(self):
            self.received = None

        def get_status(self):
            return ToolStatus.AVAILABLE

        def get_info(self):
            return {"name": self.name, "provider": self.provider, "supports": self.supports}

        def execute(self, inputs):
            self.received = dict(inputs)
            return ToolResult(success=True, data={})

    tool = _LegacyImagesTool()
    selector = ImageSelector()
    monkeypatch.setattr(selector, "_providers", lambda: [tool])
    result = selector.execute(
        {
            "prompt": "Edit",
            "preferred_tool": tool.name,
            "image_url": "https://owned.example/ref.png",
        }
    )
    assert result.success, result.error
    assert tool.received["images"] == ["https://owned.example/ref.png"]
