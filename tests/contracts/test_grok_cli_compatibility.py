"""Offline guardrails for the audited Grok CLI compatibility boundary.

These tests intentionally do not invoke the local ``grok`` binary, make network
calls, or create subprocesses. They guard the separate REST and explicit-only
CLI provider contracts.
"""

from __future__ import annotations

from pathlib import Path
import re

from tools.base_tool import ToolRuntime
from tools.graphics.grok_cli_image import GrokCLIImage
from tools.graphics.grok_image import GrokImage
from tools.video.grok_cli_video import GrokCLIVideo
from tools.video.grok_video import GrokVideo


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
COMPATIBILITY_DOC = PROJECT_ROOT / "docs" / "GROK_CLI_COMPATIBILITY.md"
PROVIDERS_DOC = PROJECT_ROOT / "docs" / "PROVIDERS.md"
GROK_SKILL = PROJECT_ROOT / ".agents" / "skills" / "grok-media" / "SKILL.md"


def test_grok_rest_provider_identity_remains_api_based():
    """The existing provider is the xAI REST tool, not the local CLI."""
    assert GrokVideo.name == "grok_video"
    assert GrokVideo.provider == "grok"
    assert GrokVideo.runtime is ToolRuntime.API
    assert "grok_cli" not in {GrokVideo.name, GrokVideo.provider}


def test_grok_cli_tools_are_separate_explicit_only_providers():
    assert GrokImage.name == "grok_image"
    assert GrokImage.provider == "grok"
    assert GrokCLIImage.name == "grok_cli_image"
    assert GrokCLIVideo.name == "grok_cli_video"

    for tool in (GrokCLIImage(), GrokCLIVideo()):
        assert tool.provider == "grok_cli"
        assert tool.runtime is ToolRuntime.API
        assert tool.supports["explicit_selection_only"] is True
        assert tool.supports["api_key_required"] is False
        assert tool.fallback_tools == []

    assert GrokCLIVideo.supports["text_to_video"] is False
    assert GrokCLIVideo.supports["image_to_video"] is True
    assert GrokCLIVideo.supports["reference_to_video"] is True


def test_compatibility_doc_records_qualified_explicit_only_contract():
    document = re.sub(
        r"\s+", " ", COMPATIBILITY_DOC.read_text(encoding="utf-8").replace("**", " ")
    )

    for required_text in (
        "2026-09-02",
        "1.0.13",
        "minimum-version, explicit-only provider adapter",
        "No new paid media generation was run",
        "Direct text to video",
        "Not exposed as a primitive",
        "Missing cost data means",
        "never free",
        "ZDR",
        "There is no browser automation, provider hopping, REST retry, or silent fallback",
    ):
        assert required_text.lower() in document.lower()


def test_provider_docs_distinguish_rest_tools_from_explicit_cli():
    document = re.sub(
        r"\s+", " ", PROVIDERS_DOC.read_text(encoding="utf-8").replace("**", " ")
    )

    for required_text in (
        "registered xAI REST API tools",
        "use `XAI_API_KEY`",
        "`grok_cli_image`",
        "`grok_cli_video`",
        "cached Grok OAuth/subscription session",
        "explicit-only",
        "never enters automatic ranking or fallbacks",
        "unknown rather than free",
        "Grok CLI compatibility audit",
    ):
        assert required_text in document


def test_grok_skill_routes_rest_and_cli_contracts_separately():
    skill = GROK_SKILL.read_text(encoding="utf-8")
    for required_text in (
        'provider="grok"',
        'provider="grok_cli"',
        "does not use `XAI_API_KEY`",
        "allow_unknown_cost=true",
        "does not expose direct text-to-video",
        "never retries or falls back",
    ):
        assert required_text in skill
