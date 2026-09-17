"""Validate canonical endpoint bindings and adapt them to video-selector inputs.

This helper makes no provider choice and performs no generation. Callers supply
an already approved provider/model and add creative parameters after validation.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _object(value: Any, label: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _index(value: Any, label: str) -> dict[str, Mapping]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    indexed = {}
    for item in value:
        item = _object(item, label)
        identifier = _text(item.get("id"), f"{label} id")
        if identifier in indexed:
            raise ValueError(f"Duplicate {label} id: {identifier}")
        indexed[identifier] = item
    return indexed


def pinned_final_frame_params(
    scene_plan: Mapping,
    asset_manifest: Mapping,
    *,
    scene_id: str,
    keyframe_asset_id: str,
    provider: str,
    model: str,
    project_dir: str | Path,
) -> dict[str, Any]:
    """Resolve an approved ending-frame handoff to explicit selector parameters.

    Paths must be project-relative, contained within ``project_dir``, and refer
    to non-empty image files. Malformed, missing, ambiguous, or unapproved
    bindings raise ValueError before a provider can be called. Provider/model
    capability and credential checks remain the selector's responsibility.
    """
    provider = _text(provider, "Explicit provider")
    model = _text(model, "Explicit model")
    if provider.strip().lower() == "auto" or model.strip().lower() == "auto":
        raise ValueError("Pinned final frames require an explicit provider and model")
    scene_id = _text(scene_id, "scene_id")
    keyframe_asset_id = _text(keyframe_asset_id, "keyframe_asset_id")
    plan = _object(scene_plan, "scene_plan")
    manifest = _object(asset_manifest, "asset_manifest")
    if scene_id not in _index(plan.get("scenes"), "scene"):
        raise ValueError(f"Unknown scene: {scene_id}")
    metadata = _object(plan.get("metadata", {}), "scene_plan metadata")
    visual = _object(metadata.get("visual_development", {}), "visual_development")
    cards = _object(visual.get("shot_cards", {}), "shot_cards")
    card = _object(cards.get(scene_id, {}), "shot card")
    requirement = _object(card.get("pinned_final_frame"), "pinned_final_frame requirement")
    if requirement.get("required") is not True:
        raise ValueError("pinned_final_frame requirement must have required=true")
    requirement_id = _text(requirement.get("requirement_id"), "requirement_id")
    _text(requirement.get("end_state"), "end_state")
    metadata = _object(manifest.get("metadata", {}), "asset_manifest metadata")
    handoffs = _object(metadata.get("motion_handoffs", {}), "motion_handoffs")
    handoff = _object(handoffs.get(keyframe_asset_id, {}), "keyframe motion handoff")
    if handoff.get("approved") is not True:
        raise ValueError("Starting-keyframe motion handoff must be explicitly approved")
    for field, expected in (("asset_id", keyframe_asset_id), ("scene_id", scene_id)):
        if field in handoff and handoff[field] != expected:
            raise ValueError(f"Starting-keyframe handoff {field} does not match its canonical binding")
    binding = _object(handoff.get("pinned_final_frame"), "pinned_final_frame binding")
    if binding.get("requirement_id") != requirement_id:
        raise ValueError("Ending-frame requirement_id does not match the scene plan")
    if binding.get("approved") is not True:
        raise ValueError("Ending-frame binding must be explicitly approved")
    ending_asset_id = _text(binding.get("asset_id"), "Ending-frame asset_id")
    assets = _index(manifest.get("assets"), "asset")
    root = Path(project_dir).expanduser().resolve()

    def image_path(asset_id: str) -> str:
        asset = assets.get(asset_id)
        if asset is None:
            raise ValueError(f"Unknown image asset: {asset_id}")
        if asset.get("type") != "image" or asset.get("scene_id") != scene_id:
            raise ValueError(f"Asset {asset_id} must be an image bound to scene {scene_id}")
        relative = Path(_text(asset.get("path"), f"Asset {asset_id} path"))
        if relative.is_absolute():
            raise ValueError(f"Asset {asset_id} path must be project-relative")
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"Asset {asset_id} path escapes the project directory")
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"Asset {asset_id} must reference a non-empty image file")
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError(f"Asset {asset_id} has an unsupported image format")
        return str(path)

    return {
        "operation": "first_last_frame",
        "preferred_provider": provider,
        "allowed_providers": [provider],
        "model": model,
        "reference_image_path": image_path(keyframe_asset_id),
        "last_image_path": image_path(ending_asset_id),
        "endpoint_requirement_id": requirement_id,
    }
