"""Preflight must distinguish endpoint support from ordinary CLI availability."""

import json

import pytest

from tools.base_tool import ToolStatus
from tools.tool_registry import ToolRegistry
from tools.video.grok_cli_video import GrokCLIVideo
from tools.video.grok_video import GrokVideo


@pytest.mark.parametrize("credential_available", [False, True])
def test_preflight_exposes_endpoint_route_without_credentials(monkeypatch, credential_available):
    secret = "offline-secret-must-not-appear"
    if credential_available:
        monkeypatch.setenv("XAI_API_KEY", secret)
    else:
        monkeypatch.delenv("XAI_API_KEY", raising=False)
    registry = ToolRegistry()
    monkeypatch.setattr(registry, "ensure_discovered", lambda: None)
    rest, cli = GrokVideo(), GrokCLIVideo()
    monkeypatch.setattr(cli, "get_status", lambda: ToolStatus.AVAILABLE)
    registry.register(rest)
    registry.register(cli)

    menu = registry.provider_menu()["video_generation"]
    entries = {entry["name"]: entry for entry in menu["available"] + menu["unavailable"]}
    assert entries[rest.name]["pinned_final_frame"]["supported"] is True
    assert entries[cli.name]["pinned_final_frame"]["supported"] is False

    summary = registry.provider_menu_summary()
    routes = {entry["tool"]: entry for entry in summary["pinned_final_frame_routes"]}
    assert routes[rest.name]["credential_available"] is credential_available
    assert routes[rest.name]["billing"] == "separate_api"
    assert routes[rest.name]["status"] == ("available" if credential_available else "unavailable")
    assert routes[rest.name]["requires_explicit_route_approval"] is True
    assert routes[rest.name]["models"] == ["grok-imagine-video-1.5"]
    assert routes[cli.name]["billing"] == "subscription_or_usage_unknown"
    assert routes[cli.name]["status"] == "available"
    assert secret not in json.dumps({"menu": menu, "summary": summary})
