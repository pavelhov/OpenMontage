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
import copy
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


def shot_local(contract: dict) -> bool:
    """Only an explicitly authored prospective contract opts into local validity."""
    return contract.get("planning_mode") == "shot_local_v1"


def source_upstream_ids(contract, shot, project_dir, *, factory=None) -> list:
    """Upstream shots whose selected outgoing frame feeds this shot.

    In the one-shot factory an upstream without a bound ``upstream_source``
    asset is an ordering note; generic manifests keep every declared upstream.
    Historical readers pass ``factory`` from the attempt's frozen authority."""
    declared = [u['shot_id'] for u in shot.get('upstream', [])]
    if not (factory_project(project_dir) if factory is None else factory):
        return declared
    bound = {(a.get('upstream_source') or {}).get('shot_id') for a in contract.get('assets', [])
             if a.get('id') in shot.get('asset_ids', [])}
    return [sid for sid in declared if sid in bound]


def factory_project(project_dir) -> bool:
    """True only for an explicitly activated standing make_video project.

    The one-shot factory treats semantic planning reviews as report-only
    observations. Generic manifest pipelines keep strict review gating. Both
    existing retained authorities must hold: the board pack's exact package
    binding and a current user-approved autonomy policy whose activation basis
    binds an enabled standing setup and an actual ``make_video`` run command
    for this project/story. A package binding alone never relaxes review.
    Original collection uses the attempt's frozen authority instead
    (:func:`frozen_factory_authority`).
    """
    from lib import production_boards as boards
    root = Path(project_dir).resolve()
    if not (root / boards.ARTIFACT).is_file():
        return False
    try:
        pack = boards.load_board_pack(root)
        binding = pack.get("package_binding")
        if not isinstance(binding, dict) or not binding.get("path") or not binding.get("sha256"):
            return False
        path = boards._inside(root, binding["path"])
        if not (path.is_file() and file_sha256(path) == binding["sha256"]):
            return False
        from lib import production_autonomy as autonomy
        policy_path = root / autonomy.POLICY_ARTIFACT
        if not policy_path.is_file():
            return False
        policy = json.loads(policy_path.read_text())
        if autonomy.schema_errors(policy) or not _standing_make_video(root, policy):
            return False
        sha = autonomy.policy_sha256(policy, autonomy.retained_baselines(root, policy))
        decision = autonomy.activation_decision(root)
        return bool(decision) and decision.get("selected") == f"auto_continue:{sha}" \
            and decision.get("user_approved") is True
    except Exception:
        return False


def _standing_make_video(root, policy) -> bool:
    """Exact retained standing setup + make_video run command for the current project/story."""
    from lib import production_autonomy as autonomy
    basis = policy.get("activation_basis") or {}
    if basis.get("kind") != "standing_run" or policy.get("planning_mode") != "shot_local_v1":
        return False
    setup = autonomy._bound_document(root, basis.get("standing_setup"), "standing setup")
    command = autonomy._bound_document(root, basis.get("run_command"), "run command")
    marker = json.loads((Path(root) / "project.json").read_text())
    identity = basis.get("identity_id")
    return (isinstance(setup, dict) and setup.get("enabled") is True and setup.get("identity_id") == identity
            and isinstance(command, dict) and command.get("command") == "make_video"
            and command.get("identity_id") == identity
            and command.get("standing_setup_sha256") == basis["standing_setup"]["sha256"]
            and all(command.get(k) == marker.get(k) == policy.get(k) for k in ("project_id", "story_revision")))


def frozen_factory_authority(project_dir, scope) -> bool:
    """Whether an already-reserved attempt was dispatched under standing make_video.

    Reads the attempt's frozen ``derived_from_policy`` authority (retained policy
    bytes + its approval decision), independent of the current package/approval.
    Generic or ambiguous historical scopes stay strict."""
    if not isinstance(scope, dict) or not isinstance(scope.get("derived_from_policy"), dict):
        return False
    from lib import production_autonomy as autonomy
    try:
        policy, _, _ = autonomy.load_historical_policy(project_dir, scope)
        return _standing_make_video(Path(project_dir).resolve(), policy)
    except Exception:
        return False


def frame_conditioned_start(contract: dict, shot: dict, project_dir) -> dict:
    """Resolve one real board in its exact authored/current observed starting role.

    Canonical primary roles stay intact: a payoff board can also be the authored
    starting composition without making a duplicate asset or an implicit alias.
    """
    from lib import production_boards as boards
    if not shot_local(contract):
        raise ValueError("frame-conditioned treatment requires shot_local_v1")
    frame = shot.get("frame_input")
    if not isinstance(frame, dict) or set(frame) != {"asset_id", "role"} or frame["role"] != "start_frame":
        raise ValueError("frame-conditioned treatment needs exact starting frame_input")
    aid = frame["asset_id"]
    assets = [a for a in contract["assets"] if a["id"] == aid and aid in shot["asset_ids"]]
    if len(assets) != 1:
        raise ValueError("frame_input must name one canonical consumer start/payoff asset")
    asset = assets[0]
    if set(asset["cast_ids"]) != set(shot["cast_ids"]):
        raise ValueError("frame_input composition differs from exact declared shot cast")
    root = Path(project_dir).resolve()
    pack = boards.load_board_pack(root)
    declarations = [r for s in pack["specification"]["shots"] if s["shot_id"] == shot["id"] for r in s["roles"]]
    if {"asset_id": aid, "role": "start_frame"} not in declarations:
        raise ValueError("frame_input lacks actual consumer starting-role declaration")
    binding = pack["board_bindings"].get(aid)
    if (not binding or not boards._binding_current(root, pack, aid)
            or binding["sha256"] != asset.get("sha256")
            or boards._inside(root, binding["path"]) != boards._inside(root, asset.get("path", ""))):
        raise ValueError("frame_input differs from exact current board bytes")
    observation = boards._observation(pack, aid, "start_frame")
    if factory_project(root):
        # One-shot factory: the retained observation is report-only evidence.
        if not observation:
            raise ValueError("frame_input lacks a retained current starting-role observation")
        review = observation["review"]
        if review["story_revision"] != contract["story_revision"]:
            raise ValueError("frame_input observation belongs to another story revision")
    else:
        if not observation or boards._review_blockers(pack, aid, "start_frame"):
            raise ValueError("frame_input lacks passing current starting-role observation")
        review = observation["review"]
        predicates = review["predicates"]
        if (review["story_revision"] != contract["story_revision"]
                or any(len([p for p in predicates if p["name"] == name]) != 1
                       or len([p for p in predicates if p["name"] == name and p["status"] == "pass"
                               and p.get("severity", "critical") == "critical"]) != 1
                       for name in ("cast_identity", "cast_count"))):
            raise ValueError("frame_input lacks exact current cast composition proof")
    return {"asset_id": aid, "role": "start_frame", "sha256": asset["sha256"],
            "review_sha256": review_digest(review), "path": str(boards._inside(root, asset["path"]))}


def protected_planning(contract: dict) -> dict:
    """Story and ordered shot identities remain shared protected planning."""
    return {**{key: copy.deepcopy(value) for key, value in contract.items()
               if key not in {"assets", "shots", "project_review"}},
            "shot_order": [shot["id"] for shot in contract["shots"]]}


def required_asset_ids(contract: dict, shot: dict) -> set:
    reference_free = contract.get("reference_mode") == "reference_free" or shot.get("reference_mode") == "reference_free"
    cast = set(shot["cast_ids"])
    required = set(shot["asset_ids"])
    if not shot_local(contract):
        cast |= set(contract["late_cast_ids"]) | set(contract["payoff_speaker_ids"])
        if contract.get("reference_mode") != "reference_free":
            required.add(contract.get("payoff_asset_id"))
    required.update(asset["id"] for asset in contract["assets"]
                    if asset["role"] == "identity_reference" and cast.intersection(asset["cast_ids"]))
    return required


def shot_planning_subject(contract: dict, shot_id: str, *, approval=False) -> dict:
    """Bind only this consumer's planning, sources and actual upstream evidence.

    Full snapshots still use contract_digest. Approval omits observed serial
    handoff facts, matching the existing exact retained authority convention.
    """
    shots = [shot for shot in contract["shots"] if shot["id"] == shot_id]
    if len(shots) != 1:
        raise ValueError("shot must occur exactly once")
    shot = copy.deepcopy(shots[0]); shot.pop("review", None)
    needed = required_asset_ids(contract, shot)
    assets = [{key: copy.deepcopy(value) for key, value in asset.items() if key != "review"}
              for asset in contract["assets"] if asset["id"] in needed]
    if shot_local(contract):
        assets.sort(key=lambda asset: asset['id'])
    if approval:
        shot["upstream"] = [{"shot_id": item["shot_id"]} for item in shot.get("upstream", [])]
        for asset in assets:
            if asset.get("upstream_source"):
                asset.pop("path", None); asset.pop("sha256", None)
    return {"protected": protected_planning(contract), "shot": shot, "assets": assets,
            "required_asset_ids": sorted(needed)}


def project_review_subject(contract: dict) -> str:
    return _digest(protected_planning(contract)) if shot_local(contract) else contract_digest(contract)


def shot_review_subject(contract: dict, shot_id: str) -> str:
    return _digest(shot_planning_subject(contract, shot_id)) if shot_local(contract) else contract_digest(contract)


def review_digest(review: dict) -> str:
    """Hash the entire review, including its subject binding and provenance."""
    return _digest(review)


def _successor_require(condition, message):
    if not condition:
        raise ValueError("review successor: " + message)


def _successor_bound(root, binding, *, return_bytes=False):
    _successor_require(isinstance(binding, dict) and set(binding) == {"path", "sha256"}, "exact evidence path/hash required")
    _successor_require(isinstance(binding["path"], str) and bool(binding["path"].strip()), "evidence path required")
    path = (root / binding["path"]).resolve()
    _successor_require(path.is_relative_to(root), "evidence missing, changed or outside project")
    try:
        raw = path.read_bytes() if return_bytes else None
        digest = hashlib.sha256(raw).hexdigest() if return_bytes else file_sha256(path)
    except OSError as exc:
        raise ValueError("review successor: evidence missing or unreadable") from exc
    _successor_require(digest == binding["sha256"], "evidence missing, changed or outside project")
    return raw if return_bytes else path


def _successor_context(root, shot_id, selection):
    from lib import production_execution as execution
    from lib.production_provenance import shot_planning_digest
    marker = execution._read(root / "project.json")
    contract = execution.load_shot_contract(root)
    _successor_require(marker.get("governance", {}).get("mode") == "strict", "strict project required")
    _successor_require((contract.get("project_id"), contract.get("story_revision")) ==
                       (marker.get("project_id"), marker.get("story_revision")), "project/story differs")
    for key in ("output", "outgoing_frame"):
        _successor_bound(root, selection.get(key))
    _successor_require(selection.get("review", {}).get("subject_sha256") == selection_digest(selection)
                       and selection["review"].get("story_revision") == marker["story_revision"],
                       "selection review subject differs")
    return {"project_id": marker["project_id"], "story_revision": marker["story_revision"],
            "shot_id": shot_id, "selection_sha256": review_digest(selection),
            "subject_sha256": selection_digest(selection),
            "planning_sha256": shot_planning_digest(contract, shot_id)}


def _successor_listening(root, evidence, context, selection, review):
    value = json.loads(_successor_bound(root, evidence, return_bytes=True))
    expected = {k: context[k] for k in ("project_id", "story_revision", "shot_id", "subject_sha256")}
    expected.update(version="1.0", kind="synchronized_audio_review", attempt_id=selection["attempt_id"],
                    output_sha256=selection["output"]["sha256"], reviewer=review["reviewer"],
                    coverage="complete_clip", synchronized=True, listened=True, status="pass")
    _successor_require(review_digest(value) == review_digest(expected),
                       "complete synchronized listening attestation differs from exact selected clip/reviewer")


def _successor_transition(old, new):
    schema = load_schema("shot_contract")
    validator = Draft202012Validator({"$defs": schema["$defs"], "$ref": "#/$defs/review"})
    _successor_require(validator.is_valid(old) and validator.is_valid(new), "valid old/new named reviews required")
    _successor_require(old["status"] == "provisional", "original provisional review required")
    _successor_require(old["review_id"] != new["review_id"], "new review identity required")
    _successor_require(old["story_revision"] == new["story_revision"] and old["subject_sha256"] == new["subject_sha256"],
                       "review subject/story differs")
    before = {p["name"]: p for p in old["predicates"]}
    after = {p["name"]: p for p in new["predicates"]}
    _successor_require(len(before) == len(old["predicates"]) and len(after) == len(new["predicates"])
                       and set(before) == set(after) and UPSTREAM_PREDICATES.issubset(before),
                       "complete unique unchanged predicate set required")
    _successor_require(before["speaker_source"]["status"] == "unknown" and after["speaker_source"]["status"] == "pass"
                       and bool(after["speaker_source"]["evidence"].strip())
                       and before["speaker_source"]["evidence"] != after["speaker_source"]["evidence"]
                       and before["speaker_source"].get("severity") == after["speaker_source"].get("severity"),
                       "only substantiated audio unknown-to-pass permitted")
    _successor_require(all(before[name] == after[name] for name in before if name != "speaker_source"),
                       "visual facts and failed predicates must remain unchanged")
    accepted = "creator_draft_exception" in old
    if not accepted:
        _successor_require(all(p["status"] == "pass" for name, p in before.items()
                               if name != "speaker_source" and (name in CRITICAL_PREDICATES or p.get("severity", "critical") == "critical")),
                           "critical visual failure or uncertainty cannot advance to pass")
    _successor_require(new["status"] == ("provisional" if accepted else "pass"), "accepted critical defect remains provisional")
    allowed = {"review_id", "reviewer", "status", "predicates", "draft_policy_sha256"}
    _successor_require({k: v for k, v in old.items() if k not in allowed} ==
                       {k: v for k, v in new.items() if k not in allowed}, "review authority differs")
    if accepted:
        _successor_require(new.get("draft_policy_sha256") == old.get("draft_policy_sha256"), "draft authority must remain bound")
    else:
        _successor_require("draft_policy_sha256" not in new, "passing successor cannot retain draft policy")


def resolve_selection_review(project_dir, shot_id, selection, *, expected_review_sha256=None):
    """Resolve the selected review, reading one retained same-media successor.

    Read-only: retained ``production_review_successors`` records written before
    the writer was retired still apply when content-addressed, uniquely bound to
    this unchanged original/subject/media/planning context and backed by their
    bound synchronized listening attestation. No record means the original
    review. ``expected_review_sha256`` must equal the current original review.
    """
    root = Path(project_dir).resolve()
    original = selection.get("review") if isinstance(selection, dict) else None
    if not isinstance(original, dict):
        raise ValueError("original review missing")
    predecessor_sha256 = review_digest(original)
    if expected_review_sha256 is not None and predecessor_sha256 != expected_review_sha256:
        raise ValueError("historical original review differs")
    directory = root / "production_review_successors"
    if not directory.exists() and not directory.is_symlink():
        return copy.deepcopy(original)
    _successor_require(not directory.is_symlink(), "history directory cannot be a symlink")
    subject_sha256 = selection_digest(selection)
    matches = []
    for path in directory.glob("*.json"):
        raw = path.read_bytes()
        record = json.loads(raw)
        _successor_require(isinstance(record, dict), "malformed history")
        if (record.get("shot_id") == shot_id and record.get("attempt_id") == selection.get("attempt_id")
                and record.get("predecessor_sha256") == predecessor_sha256
                and record.get("subject_sha256") == subject_sha256):
            matches.append((path, record, hashlib.sha256(raw).hexdigest()))
    if not matches:
        return copy.deepcopy(original)
    _successor_require(len(matches) == 1, "conflicting successors")
    path, record, record_sha256 = matches[0]
    _successor_require(path.parent.resolve() == directory.resolve() and not path.is_symlink()
                       and path.name == record_sha256 + ".json", "content-addressed immutable history required")
    context = _successor_context(root, shot_id, selection)
    _successor_require(set(record) == set(context) | {"version", "attempt_id", "predecessor_sha256", "review", "evidence"},
                       "unsupported record fields")
    _successor_require(record["version"] == "1.0" and all(record[k] == v for k, v in context.items())
                       and record["predecessor_sha256"] == predecessor_sha256, "stale or forged chain binding")
    _successor_listening(root, record["evidence"], context, selection, record["review"])
    _successor_transition(original, record["review"])
    return copy.deepcopy(record["review"])


def selection_digest(selection: dict) -> str:
    """Bind a review to the exact upstream attempt/output/outgoing-frame tuple."""
    subject = {key: selection.get(key) for key in ("attempt_id", "output", "outgoing_frame")}
    # Revised choreography needs a new review of the same footage against the
    # authorized current plan. Keep historical selection subjects unchanged.
    if "planning_revision" in selection:
        subject["planning_revision"] = selection["planning_revision"]
    return _digest(subject)


def audio_review_disabled(project_dir: str | Path) -> bool:
    """Future identity projects only; missing or damaged authority fails closed.

    This changes required review scope, never native media or old observations.
    Version 1.0 keeps its historical identity binding. Version 1.1 is the
    neutral explicit project setting: same strict, evidence-bound shape, no
    identity prefix.
    """
    try:
        root = Path(project_dir).resolve()
        marker = json.loads((root / "project.json").read_text())
        governance = marker.get("governance", {})
        policy = governance.get("audio_review", {})
        version = policy.get("version")
        legacy = version == "1.0"
        if (governance.get("mode") != "strict"
                or version not in {"1.0", "1.1"}
                or (legacy and not str(marker.get("project_id", "")).startswith("thisaccountisawarcrime-"))
                or set(policy) != {"version", "mode", "project_id", "story_revision", "evidence"}
                or policy["mode"] != "disabled"
                or policy["project_id"] != marker["project_id"]
                or not isinstance(policy["story_revision"], str) or not policy["story_revision"].strip()
                or (not legacy and policy["story_revision"] != marker.get("story_revision"))):
            return False
        evidence = policy["evidence"]
        if set(evidence) != {"path", "sha256"}:
            return False
        path = (root / evidence["path"]).resolve()
        if not path.is_relative_to(root) or file_sha256(path) != evidence["sha256"]:
            return False
        approval = json.loads(path.read_text())
        return ((not legacy or approval.get("identity") == "thisaccountisawarcrime")
                and approval.get("audio_review") == "disabled"
                and approval.get("scope") == "future_projects_only")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def required_review_predicates(project_dir, required):
    return required - {"speaker_source"} if audio_review_disabled(project_dir) else required


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
        story_revision=story_revision, selected_upstream=selected_upstream)


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
        story_revision=story_revision, selected_upstream=selected_upstream)


def _validate_shot_contract(
    contract, *, project_dir, shot_id, story_revision, selected_upstream,
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
    # Factory semantic planning reviews are disclosed observations; generic
    # manifests keep strict review gating.
    report_only = factory_project(root)

    def check_file(record, label):
        try:
            path = Path(record["path"])
            if not path.is_absolute():
                path = root / path
            if file_sha256(path) != record["sha256"]:
                errors.append(f"{label}.sha256: input bytes changed")
        except (KeyError, TypeError, ValueError, OSError) as exc:
            errors.append(f"{label}: unreadable bound asset ({exc})")

    def check_review(review, subject, required, label, *, allow_draft=False, selection=None, upstream_id=None,
                     gating=True):
        # Semantic planning reviews are observations in the one-shot factory:
        # their findings are disclosed as warnings and never gate motion.
        review_errors = errors if gating else warnings
        # Upstream review records enter from outside the schema-validated plan.
        review_schema = {"$defs": schema["$defs"], "$ref": "#/$defs/review"}
        malformed = list(Draft202012Validator(review_schema).iter_errors(review))
        if malformed:
            review_errors.append(f"{label}: invalid/missing review: {malformed[0].message}")
            return
        if review["story_revision"] != revision or review["subject_sha256"] != subject:
            review_errors.append(f"{label}: stale review binding (story revision or subject hash)")
        from lib.production_draft import accepted_draft_predicate
        accepted = accepted_draft_predicate(selection, root, shot_id=upstream_id) if allow_draft and selection else None
        audio_provisional = allow_draft and provisional_audio_review(review, root)
        provisional = audio_provisional or accepted is not None
        if review["status"] != "pass" and not provisional:
            review_errors.append(f"{label}: review is {review['status']}")
        required = required_review_predicates(root, required)
        seen = set()
        for predicate in review["predicates"]:
            name = predicate["name"]
            if name in seen:
                review_errors.append(f"{label}.{name}: duplicate/conflicting predicate")
            seen.add(name)
            if name == "speaker_source" and audio_review_disabled(root):
                continue
            critical = name in CRITICAL_PREDICATES or predicate.get("severity", "critical") == "critical"
            if name in CRITICAL_PREDICATES and predicate.get("severity") == "cosmetic":
                review_errors.append(f"{label}.{name}: required critical predicate cannot be cosmetic")
            deferred = (audio_provisional and name == "speaker_source" and predicate["status"] == "unknown") or (name == accepted and predicate["status"] == "fail")
            if predicate["status"] != "pass":
                (review_errors if critical and not deferred else warnings).append(f"{label}.{name}: {predicate['status']}: {predicate['evidence']}")
        for name in sorted(required - seen):
            review_errors.append(f"{label}.{name}: missing critical review predicate")

    try:
        digest = contract_digest(contract)
    except (ValueError, TypeError) as exc:
        errors.append(f"$: invalid JSON planning value ({exc})")
        return result()
    check_review(contract["project_review"], project_review_subject(contract), PROJECT_PREDICATES, "project_review", gating=not report_only)
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
    reference_guided = shot.get("reference_mode", contract.get("reference_mode")) == "reference_guided"
    frame_conditioned = shot.get("reference_mode", contract.get("reference_mode")) == "frame_conditioned"
    if frame_conditioned:
        try:
            frame_conditioned_start(contract, shot, root)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            errors.append(f"shots.{shot_id}.frame_input: {exc}")
    elif "frame_input" in shot:
        errors.append(f"shots.{shot_id}.frame_input: requires explicit frame_conditioned treatment")
    # Prospective unknown is frozen authored planning, not a footage verdict.
    # Current bound completed_action reviews below still supply completion proof.
    allowed_endpoints = {"completed", "unknown"} if shot_local(contract) else {"completed"}
    # Legacy board-backed contracts retain the global payoff gate.
    payoff_id = contract.get("payoff_asset_id")
    payoff = assets.get(payoff_id)
    payoff_required = not project_reference_free and (not shot_local(contract) or payoff_id in shot["asset_ids"])
    if payoff_required and (not payoff or payoff["role"] != "payoff_board"):
        errors.append("payoff_asset_id: missing payoff_board role")
    elif payoff_required and not set(contract["payoff_speaker_ids"]).issubset(payoff["cast_ids"]):
        errors.append("payoff_speaker_ids: missing payoff speaker in board cast")
    required_assets = required_asset_ids(contract, shot)
    required_cast = set(shot["cast_ids"])
    if not shot_local(contract):
        required_cast |= set(contract["late_cast_ids"]) | set(contract["payoff_speaker_ids"])
    for cast_id in required_cast:
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
        check_review(asset.get("review"), asset.get("sha256"), ASSET_PREDICATES, f"assets.{asset_id}.review", gating=not report_only)
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
        if not reference_free and not (shot_local(contract) and (reference_guided or frame_conditioned)) and sum(a["role"] == role for a in local_assets) != 1:
            errors.append(f"shots.{shot_id}.asset_ids: require exactly one {role}")
    timed_consumers = []
    if any(asset['role'] == 'timed_keyframe' for asset in local_assets) and (root / 'artifacts/board_preparation.json').exists():
        from lib import production_boards as boards
        timed_consumers = [role for row in boards.load_board_pack(root)['specification']['shots']
                           if row['shot_id'] == shot_id for role in row['roles']]
    for asset in local_assets:
        if asset['role'] != 'timed_keyframe':
            continue
        consumers = [role for role in timed_consumers if role['asset_id'] == asset['id']]
        times = [role.get('time_seconds', asset.get('time_seconds', -1)) for role in consumers if role['role'] == 'timed_keyframe']
        if not consumers:
            times = [asset.get('time_seconds', -1)]
        if any(not 0 < timestamp < shot['duration_seconds'] for timestamp in times):
            errors.append(f"assets.{asset['id']}.time_seconds: keyframe must be inside shot duration")
    if shot["endpoint_completion"] not in allowed_endpoints:
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
            message = f"shots.{shot_id}.allowed_transformations: must preserve prop/body invariants"
            if report_only:
                warnings.append(message + " (report-only wording mismatch; semantic preservation not verified)")
            else:
                errors.append(message)
    check_review(shot.get("review"), shot_review_subject(contract, shot_id), SHOT_PREDICATES, f"shots.{shot_id}.review", gating=not report_only)

    for binding in shot["upstream"]:
        upstream_id = binding["shot_id"]
        label = f"shots.{shot_id}.upstream.{upstream_id}"
        ordered_ids = list(shots)
        if upstream_id not in shots or ordered_ids.index(upstream_id) >= ordered_ids.index(shot_id):
            errors.append(f"{label}: upstream must precede dependent shot (no self/cyclic dependencies)")
        elif report_only and not any((a.get("upstream_source") or {}).get("shot_id") == upstream_id
                                     for a in (assets.get(i) for i in shot["asset_ids"]) if a):
            # One-shot factory: every clip uses its own prepared frame. An
            # upstream without a bound outgoing-frame source is an ordering note.
            if not isinstance((selected_upstream or {}).get(upstream_id), dict):
                warnings.append(f"{label}: upstream not selected (ordering note only)")
            continue
        elif not report_only and shots[upstream_id]["endpoint_completion"] not in allowed_endpoints:
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
            review = resolve_selection_review(root, upstream_id, selection,
                expected_review_sha256=binding["review_sha256"])
        except (OSError, ValueError, TypeError, KeyError) as exc:
            errors.append(f"{label}: selected review changed or missing ({exc})")
            continue
        check_review(review, selection_digest(selection), UPSTREAM_PREDICATES, f"{label}.review", allow_draft=True, selection=selection, upstream_id=upstream_id, gating=not report_only)
    return result()
