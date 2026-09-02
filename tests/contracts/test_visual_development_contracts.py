"""Offline contract coverage for the Visual Skills-derived Layer-2 integration."""

from __future__ import annotations

import json
from pathlib import Path



ROOT = Path(__file__).resolve().parents[2]
SOURCE_URL = "https://github.com/smixs/visual-skills/tree/3c554715b5eb30f54de78fac3c0df4a7105e4955"
CC_BY_URL = "https://creativecommons.org/licenses/by/4.0/"
PIPELINES = ("animated-explainer", "cinematic", "animation", "hybrid")


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_shared_skills_are_attributed_and_discoverable() -> None:
    visual = _read("skills/creative/visual-development.md")
    audit = _read("skills/meta/prompt-audit.md")
    index = _read("skills/INDEX.md")

    for text in (visual, audit):
        assert "Serge Shima" in text
        assert SOURCE_URL in text
        assert "CC BY 4.0" in text
        assert "not endorsed" in text

    assert "creative/visual-development.md" in index
    assert "meta/prompt-audit.md" in index


def test_visual_development_sidecars_use_existing_top_level_metadata() -> None:
    visual = _read("skills/creative/visual-development.md")
    audit = _read("skills/meta/prompt-audit.md")
    normalized_visual = " ".join(visual.split())

    assert "scene_plan.metadata.visual_development.shot_cards[scene_id]" in visual
    assert "continuity_ledger" in visual
    assert "animatic_keyframes" in visual
    assert "asset_manifest.metadata.reference_assets[asset_id]" in visual
    assert "asset_manifest.metadata.motion_handoffs[keyframe_asset_id]" in visual
    assert "Change / Preserve / Constraints" in visual
    assert "reference_requirements" in visual
    assert "keyframe_requirements" in visual
    assert "Do not invent future asset IDs" in normalized_visual
    assert "asset_manifest.metadata.edit_contracts[output_asset_id]" in visual
    assert "asset_manifest.metadata.edit_attempts[edit_attempt_id]" in visual
    assert "one concrete" in visual
    assert "source_master_asset_id" in visual
    assert "asset_manifest.metadata.prompt_audits[asset_id]" in audit
    assert "asset_manifest.metadata.prompt_attempts[attempt_id]" in audit
    assert "dramaturgical function" in audit.lower()
    assert "provider capacity and syntax" in audit.lower()

    scene_schema = json.loads(_read("schemas/artifacts/scene_plan.schema.json"))
    asset_schema = json.loads(_read("schemas/artifacts/asset_manifest.schema.json"))
    assert scene_schema["properties"]["scenes"]["items"]["additionalProperties"] is False
    assert asset_schema["properties"]["assets"]["items"]["additionalProperties"] is False
    assert scene_schema["properties"]["metadata"]["type"] == "object"
    assert asset_schema["properties"]["metadata"]["type"] == "object"


def test_metadata_sidecar_examples_fit_closed_schema_without_new_fields() -> None:
    scene_schema = json.loads(_read("schemas/artifacts/scene_plan.schema.json"))
    asset_schema = json.loads(_read("schemas/artifacts/asset_manifest.schema.json"))

    scene_item_fields = scene_schema["properties"]["scenes"]["items"]["properties"]
    asset_item_fields = asset_schema["properties"]["assets"]["items"]["properties"]
    assert "metadata" not in scene_item_fields
    assert "metadata" not in asset_item_fields
    assert "visual_development" not in scene_item_fields
    assert "prompt_audits" not in asset_item_fields

    # The rich maps belong to the existing, open top-level metadata objects.
    scene_sidecar = {"visual_development": {"shot_cards": {"scene-03": {"scene_id": "scene-03"}}}}
    asset_sidecar = {"prompt_audits": {"asset-17": {"asset_id": "asset-17", "pre": "draft", "post": "rewrite"}}}
    assert scene_schema["properties"]["metadata"]["type"] == "object"
    assert asset_schema["properties"]["metadata"]["type"] == "object"
    assert "scene-03" in scene_sidecar["visual_development"]["shot_cards"]
    assert "asset-17" in asset_sidecar["prompt_audits"]


def test_target_pipelines_review_sidecar_guidance_without_contract_changes() -> None:
    for name in PIPELINES:
        text = _read(f"pipeline_defs/{name}.yaml")
        assert "- name: scene_plan" in text
        assert "- name: assets" in text
        assert "metadata" in text
        assert "existing scene IDs" in text
        assert "metadata" in text
        assert "existing asset IDs" in text


def test_directors_route_to_shared_skills_and_do_not_reopen_item_schemas() -> None:
    pairs = (
        ("explainer", "scene-director.md"),
        ("explainer", "asset-director.md"),
        ("cinematic", "scene-director.md"),
        ("cinematic", "asset-director.md"),
        ("animation", "scene-director.md"),
        ("animation", "asset-director.md"),
        ("hybrid", "scene-director.md"),
        ("hybrid", "asset-director.md"),
    )
    for pipeline, filename in pairs:
        text = _read(f"skills/pipelines/{pipeline}/{filename}")
        assert "visual-development.md" in text
        assert "closed" in text.lower()
        if filename == "asset-director.md":
            assert "prompt-audit.md" in text
            assert "prompt_attempts" in text
            assert "prompt_audits" in text


def test_attribution_notice_and_license_text_are_complete() -> None:
    notice = _read("THIRD_PARTY_NOTICES.md")
    license_text = _read("licenses/visual-skills-CC-BY-4.0.txt")

    for required in (
        "Serge Shima — <https://github.com/smixs/visual-skills>",
        "Copyright (c) 2026 Serge Shima",
        "skills/creative/storytelling.md",
        "skills/meta/reviewer.md",
        SOURCE_URL,
        CC_BY_URL,
        "2026-09-01",
        "does not endorse",
    ):
        assert required in notice
    assert "Creative Commons Attribution 4.0 International Public License" in license_text
    assert "Section 3 -- License Conditions." in license_text
    assert "No endorsement." in license_text
