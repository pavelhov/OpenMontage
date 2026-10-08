"""Factual eligibility for motion; semantic judgments belong to named reviewers.

This is a motion prerequisite, not an image-generation gate: reference/payoff
boards must be authorable before their reviews exist. Draft/legacy artifacts may
be read without passing this validator, but never gain eligibility implicitly.
Paths resolve relative to the project root; hashes bind bytes, not file names.
Callers must load current story and upstream selections from authoritative disk
state, and snapshot/recheck inputs at dispatch to close the file mutation race.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from schemas.artifacts import load_schema

CURRENT_VERSION = "1.0"
PROJECT_PREDICATES = frozenset({"payoff", "late_cast", "cast_identity", "cast_count", "speaker_source"})
SHOT_PREDICATES = frozenset({"cast_identity", "cast_count", "completed_action", "speaker_source", "possession", "transformation"})
ASSET_PREDICATES = frozenset({"asset_quality", "cast_identity"})
UPSTREAM_PREDICATES = SHOT_PREDICATES | {"outgoing_frame"}
CRITICAL_PREDICATES = PROJECT_PREDICATES | SHOT_PREDICATES | ASSET_PREDICATES | UPSTREAM_PREDICATES


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def file_sha256(path: str | Path) -> str:
    """Hash actual bytes without loading large media into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def contract_digest(contract: dict) -> str:
    """Bind all planning fields, asset bytes and expected upstream evidence.

    Only the three explicitly designated review locations are excluded. Asset
    sha256 and upstream review_sha256 remain included; there is no self hash in
    this projection. Changing review prose never requires resigning the plan.
    """
    plan = {k: v for k, v in contract.items() if k not in {"project_review", "assets", "shots"}}
    plan["assets"] = [{k: v for k, v in asset.items() if k != "review"} for asset in contract.get("assets", [])]
    plan["shots"] = [{k: v for k, v in shot.items() if k != "review"} for shot in contract.get("shots", [])]
    return _digest(plan)


def review_digest(review: dict) -> str:
    """Hash the entire review, including its subject binding and provenance."""
    return _digest(review)


def selection_digest(selection: dict) -> str:
    """Bind a review to the exact upstream attempt/output/outgoing-frame tuple."""
    subject = {key: selection.get(key) for key in ("attempt_id", "output", "outgoing_frame")}
    # Revised choreography needs a new review of the same footage against the
    # authorized current plan. Keep historical selection subjects unchanged.
    if "planning_revision" in selection:
        subject["planning_revision"] = selection["planning_revision"]
    return _digest(subject)


def draft_audio_policy_digest(project_dir: str | Path) -> str:
    """Validate explicit draft-only authorization bound to local evidence bytes.

    This policy is separate from creative planning: changing review capability
    must not rewrite historical attempts or expand their approved media scope.
    """
    root = Path(project_dir).resolve()
    marker = json.loads((root / "project.json").read_text())
    if marker.get("governance", {}).get("mode") != "strict":
        raise ValueError("draft authorization requires strict governance")
    policy = marker.get("governance", {}).get("draft_review")
    if not isinstance(policy, dict) or set(policy) != {
        "version", "mode", "project_id", "story_revision", "evidence"
    }:
        raise ValueError("explicit audio-unavailable draft policy required")
    if (policy["version"] != "1.0" or policy["mode"] != "audio_unavailable_draft"
            or policy["project_id"] != marker.get("project_id")
            or policy["story_revision"] != marker.get("story_revision")):
        raise ValueError("draft policy differs from current project/story")
    evidence = policy["evidence"]
    if not isinstance(evidence, dict) or set(evidence) != {"path", "sha256"}:
        raise ValueError("draft policy requires hashed approval evidence")
    path = (root / evidence["path"]).resolve()
    if not path.is_relative_to(root) or file_sha256(path) != evidence["sha256"]:
        raise ValueError("draft authorization evidence missing or changed")
    return _digest(policy)


def provisional_audio_review(review: dict, project_dir: str | Path) -> bool:
    """Allow only unknown speaker-source evidence; callers check other evidence."""
    if review.get("status") != "provisional":
        return False
    try:
        bound = review.get("draft_policy_sha256") == draft_audio_policy_digest(project_dir)
    except (OSError, ValueError, TypeError, KeyError):
        return False
    speakers = [p for p in review.get("predicates", []) if p.get("name") == "speaker_source"]
    return bound and len(speakers) == 1 and speakers[0].get("status") == "unknown"


def validate_shot_contract(
    contract: dict, *, project_dir: str | Path, shot_id: str,
    story_revision: str | None = None, selected_upstream: dict | None = None,
) -> dict[str, Any]:
    """Return ``eligible``, field-level ``errors`` and nonblocking ``warnings``.

    There is deliberately no budget, attempt-count, or review-round override.
    Missing/failed critical evidence always blocks. Explicit draft authorization
    may defer only unknown upstream speaker_source evidence. ``selected_upstream``
    maps shot IDs to fresh selected records containing attempt_id, output,
    outgoing_frame (both path/sha256), and review. It must not be reconstructed
    from the expected bindings stored in this contract.
    """
    return _validate_shot_contract(contract, project_dir=project_dir, shot_id=shot_id,
        story_revision=story_revision, selected_upstream=selected_upstream,
        resolve_successors=True)


def _validate_original_shot_contract(
    contract: dict, *, project_dir: str | Path, shot_id: str,
    story_revision: str | None = None, selected_upstream: dict | None = None,
) -> dict[str, Any]:
    """Check original source facts for retained generation-authority replay only.

    Embedded original reviews, including explicitly authorized draft unknowns,
    retain their exact expected hashes. This does not establish current listening
    evidence or eligibility under an appended review successor.
    """
    return _validate_shot_contract(contract, project_dir=project_dir, shot_id=shot_id,
        story_revision=story_revision, selected_upstream=selected_upstream,
        resolve_successors=False)


def _validate_shot_contract(
    contract, *, project_dir, shot_id, story_revision, selected_upstream,
    resolve_successors,
):
    errors: list[str] = []
    warnings: list[str] = []

    def result():
        return {"eligible": not errors, "errors": errors, "warnings": warnings}

    schema = load_schema("shot_contract")
    for error in Draft202012Validator(schema).iter_errors(contract):
        path = ".".join(map(str, error.absolute_path)) or "$"
        errors.append(f"{path}: {error.message}")
    if errors:
        return result()
    revision = contract["story_revision"]
    if story_revision is not None and revision != story_revision:
        errors.append("story_revision: contract does not match current story")
    root = Path(project_dir).resolve()

    def check_file(record, label):
        try:
            path = Path(record["path"])
            if not path.is_absolute():
                path = root / path
            if file_sha256(path) != record["sha256"]:
                errors.append(f"{label}.sha256: input bytes changed")
        except (KeyError, TypeError, ValueError, OSError) as exc:
            errors.append(f"{label}: unreadable bound asset ({exc})")

    def check_review(review, subject, required, label, *, allow_draft=False, selection=None, upstream_id=None):
        # Upstream review records enter from outside the schema-validated plan.
        review_schema = {"$defs": schema["$defs"], "$ref": "#/$defs/review"}
        malformed = list(Draft202012Validator(review_schema).iter_errors(review))
        if malformed:
            errors.append(f"{label}: invalid/missing review: {malformed[0].message}")
            return
        if review["story_revision"] != revision or review["subject_sha256"] != subject:
            errors.append(f"{label}: stale review binding (story revision or subject hash)")
        from lib.production_draft import accepted_draft_predicate
        accepted = accepted_draft_predicate(selection, root, shot_id=upstream_id) if allow_draft and selection else None
        audio_provisional = allow_draft and provisional_audio_review(review, root)
        provisional = audio_provisional or accepted is not None
        if review["status"] != "pass" and not provisional:
            errors.append(f"{label}: review is {review['status']}")
        seen = set()
        for predicate in review["predicates"]:
            name = predicate["name"]
            if name in seen:
                errors.append(f"{label}.{name}: duplicate/conflicting predicate")
            seen.add(name)
            critical = name in CRITICAL_PREDICATES or predicate.get("severity", "critical") == "critical"
            if name in CRITICAL_PREDICATES and predicate.get("severity") == "cosmetic":
                errors.append(f"{label}.{name}: required critical predicate cannot be cosmetic")
            deferred = (audio_provisional and name == "speaker_source" and predicate["status"] == "unknown") or (name == accepted and predicate["status"] == "fail")
            if predicate["status"] != "pass":
                (errors if critical and not deferred else warnings).append(f"{label}.{name}: {predicate['status']}: {predicate['evidence']}")
        for name in sorted(required - seen):
            errors.append(f"{label}.{name}: missing critical review predicate")

    try:
        digest = contract_digest(contract)
    except (ValueError, TypeError) as exc:
        errors.append(f"$: invalid JSON planning value ({exc})")
        return result()
    check_review(contract["project_review"], digest, PROJECT_PREDICATES, "project_review")
    assets = {}
    for asset in contract["assets"]:
        if asset["id"] in assets:
            errors.append(f"assets.{asset['id']}: duplicate asset id")
        assets[asset["id"]] = asset
    shots = {}
    for shot in contract["shots"]:
        if shot["id"] in shots:
            errors.append(f"shots.{shot['id']}: duplicate shot id")
        shots[shot["id"]] = shot
    shot = shots.get(shot_id)
    if shot is None:
        errors.append(f"shots.{shot_id}: shot missing")
        return result()

    project_reference_free = contract.get("reference_mode") == "reference_free"
    reference_free = project_reference_free or shot.get("reference_mode") == "reference_free"
    # Legacy board-backed contracts retain the global payoff gate.
    payoff_id = contract.get("payoff_asset_id")
    payoff = assets.get(payoff_id)
    if not project_reference_free and (not payoff or payoff["role"] != "payoff_board"):
        errors.append("payoff_asset_id: missing payoff_board role")
    elif not project_reference_free and not set(contract["payoff_speaker_ids"]).issubset(payoff["cast_ids"]):
        errors.append("payoff_speaker_ids: missing payoff speaker in board cast")
    required_assets = set(shot["asset_ids"]) | ({payoff_id} if not project_reference_free else set())
    for cast_id in set(contract["late_cast_ids"]) | set(shot["cast_ids"]) | set(contract["payoff_speaker_ids"]):
        identity = [a for a in assets.values() if a["role"] == "identity_reference" and cast_id in a["cast_ids"]]
        if not identity:
            errors.append(f"cast_ids.{cast_id}: missing identity_reference asset")
        required_assets.update(a["id"] for a in identity)
    for asset_id in sorted(required_assets):
        asset = assets.get(asset_id)
        if not asset:
            errors.append(f"asset_ids.{asset_id}: unknown asset")
            continue
        check_file(asset, f"assets.{asset_id}")
        check_review(asset.get("review"), asset.get("sha256"), ASSET_PREDICATES, f"assets.{asset_id}.review")
        source = asset.get("upstream_source")
        if source:
            upstream_id = source["shot_id"]
            if upstream_id not in {binding["shot_id"] for binding in shot["upstream"]}:
                errors.append(f"assets.{asset_id}.upstream_source: missing reviewed shot.upstream binding")
            selection = (selected_upstream or {}).get(upstream_id, {})
            observed = selection.get(source["role"], {}) if isinstance(selection, dict) else {}
            if not isinstance(observed, dict) or not observed.get("path"):
                errors.append(f"assets.{asset_id}.upstream_source: missing selected outgoing frame")
            else:
                asset_path = (root / asset.get("path", "")).resolve()
                observed_path = (root / observed["path"]).resolve()
                if asset_path != observed_path or asset.get("sha256") != observed.get("sha256"):
                    errors.append(f"assets.{asset_id}.upstream_source: path/hash differ from current selected outgoing frame")
    local_assets = [assets[i] for i in shot["asset_ids"] if i in assets]
    for role in ("start_frame", "end_frame"):
        if not reference_free and sum(a["role"] == role for a in local_assets) != 1:
            errors.append(f"shots.{shot_id}.asset_ids: require exactly one {role}")
    for asset in local_assets:
        if asset["role"] == "timed_keyframe" and not (0 < asset.get("time_seconds", -1) < shot["duration_seconds"]):
            errors.append(f"assets.{asset['id']}.time_seconds: keyframe must be inside shot duration")
    if shot["endpoint_completion"] != "completed":
        errors.append(f"shots.{shot_id}.endpoint_completion: completed endpoint required")
    if not set(shot["required_visible_speakers"]).issubset(shot["cast_ids"]):
        errors.append(f"shots.{shot_id}.required_visible_speakers: speaker missing from cast_ids")
    visible_dialogue = set()
    for line in shot["dialogue"]:
        if line["speaker_id"] not in shot["cast_ids"]:
            errors.append(f"shots.{shot_id}.dialogue: undeclared speaker")
        if not (line["start_seconds"] < line["end_seconds"] <= shot["duration_seconds"]):
            errors.append(f"shots.{shot_id}.dialogue: invalid timing")
        if line["source"] == "visible":
            visible_dialogue.add(line["speaker_id"])
    if visible_dialogue != set(shot["required_visible_speakers"]):
        errors.append(f"shots.{shot_id}.required_visible_speakers: inconsistent visible dialogue coverage")
    transition = shot["transition"]
    transformations = {t["id"]: t for t in shot["allowed_transformations"]}
    if len(transformations) != len(shot["allowed_transformations"]):
        errors.append(f"shots.{shot_id}.allowed_transformations: duplicate id")
    if transition["type"] == "transformation":
        if transition.get("transformation_id") not in transformations:
            errors.append(f"shots.{shot_id}.transition: unapproved transformation")
    elif "transformation_id" in transition:
        errors.append(f"shots.{shot_id}.transition: transformation_id requires transformation type")
    for transformation in transformations.values():
        if not set(shot["prop_body_invariants"]).issubset(transformation["preserved_invariants"]):
            errors.append(f"shots.{shot_id}.allowed_transformations: must preserve prop/body invariants")
    check_review(shot.get("review"), digest, SHOT_PREDICATES, f"shots.{shot_id}.review")

    for binding in shot["upstream"]:
        upstream_id = binding["shot_id"]
        label = f"shots.{shot_id}.upstream.{upstream_id}"
        ordered_ids = list(shots)
        if upstream_id not in shots or ordered_ids.index(upstream_id) >= ordered_ids.index(shot_id):
            errors.append(f"{label}: upstream must precede dependent shot (no self/cyclic dependencies)")
        elif shots[upstream_id]["endpoint_completion"] != "completed":
            errors.append(f"{label}: upstream endpoint_completion is not completed")
        selection = (selected_upstream or {}).get(upstream_id)
        if not isinstance(selection, dict):
            errors.append(f"{label}: missing current selected upstream evidence")
            continue
        if not {"attempt_id", "output_sha256", "outgoing_frame_sha256", "review_sha256"}.issubset(binding):
            errors.append(f"{label}: pending upstream observation is not eligible for motion")
            continue
        if selection.get("attempt_id") != binding["attempt_id"]:
            errors.append(f"{label}: selected attempt changed")
        for key in ("output", "outgoing_frame"):
            record = selection.get(key)
            if not isinstance(record, dict) or record.get("sha256") != binding[f"{key}_sha256"]:
                errors.append(f"{label}.{key}: selected hash changed or missing")
            else:
                check_file(record, f"{label}.{key}")
        try:
            if resolve_successors:
                from lib.production_review_successors import resolve_selection_review
                review = resolve_selection_review(root, upstream_id, selection,
                    expected_review_sha256=binding["review_sha256"])
            else:
                review = selection.get("review")
                if review_digest(review) != binding["review_sha256"]:
                    raise ValueError("historical original review differs")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            errors.append(f"{label}: selected review changed or missing ({exc})")
            continue
        check_review(review, selection_digest(selection), UPSTREAM_PREDICATES, f"{label}.review", allow_draft=True, selection=selection, upstream_id=upstream_id)
    return result()
