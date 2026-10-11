"""Episode-local board queue; OpenMontage alone admits and accounts for calls.

Text prompts and inventory are authored once. Exact image packets materialize
when their source bytes exist. This helper never invokes an image/video tool,
grants motion or corrective generation authority. Newly authored opt-in packages
may project real reviewed bytes into unfrozen canonical planning artifacts.
"""
from copy import deepcopy
from datetime import datetime, timezone
from functools import wraps
import fcntl
import hashlib
import json
from pathlib import Path


ARTIFACT = "artifacts/board_preparation.json"
_NO_REPLACEMENT = object()
NODE_FIELDS = {"slot_id", "shot_id", "role", "prompt", "reference_ids", "image_dependencies",
               "output_path", "required_predicates", "transparent_background", "cast"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value):
    """Validate an optional review start time; the timestamp itself is not stored here."""
    if not isinstance(value, str):
        raise ValueError("event ts must be an ISO timestamp string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("event ts must include a timezone")
    return parsed


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _inside(root, path):
    value = Path(path)
    value = (value if value.is_absolute() else root / value).resolve()
    if not value.is_relative_to(root) or value == root:
        raise ValueError("board source/output must be a project-local file")
    return value


def _current(root, binding):
    path = _inside(root, binding["path"])
    return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == binding["sha256"]


def _binding_current(root, pack, asset_id, visiting=None):
    binding = pack["board_bindings"].get(asset_id)
    if not binding or not _current(root, binding):
        return False
    visiting = set(visiting or ())
    if asset_id in visiting:
        return False
    visiting.add(asset_id)
    for source_id, expected in binding.get("source_bindings", {}).items():
        source = pack["board_bindings"].get(source_id)
        if not source or any(source.get(key) != expected[key] for key in ("path", "sha256")):
            return False
        if not _binding_current(root, pack, source_id, visiting):
            return False
    return True


def load_board_pack(project_dir):
    root = Path(project_dir).resolve()
    pack = json.loads((root / ARTIFACT).read_text())
    marker = json.loads((root / "project.json").read_text())
    if pack["project_id"] != marker["project_id"] or pack["story_revision"] != marker["story_revision"]:
        raise ValueError("board pack has a stale project/story binding")
    if pack["prompt_pack"] != {node["slot_id"]: node for node in pack["specification"]["image_nodes"]}:
        raise ValueError("immutable board prompt pack changed")
    if _digest(pack["specification"]) != pack["specification_sha256"]:
        raise ValueError("immutable board prompt specification changed")
    for name in ("story", "approval"):
        if not _current(root, pack["specification"][name]):
            raise ValueError("board pack has stale " + name + " bytes")
    return pack


def _save(root, pack):
    target = root / ARTIFACT
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(pack, indent=2) + "\n")
    temporary.replace(target)


def _board_transaction(operation):
    """Serialize the pack's local load/modify/publish, never provider work."""
    @wraps(operation)
    def transaction(project_dir, *args, **kwargs):
        root = Path(project_dir).resolve()
        lock = root / "artifacts/.board_preparation.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                return operation(root, *args, **kwargs)
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return transaction


def _event(pack, kind, observed_at=None, **details):
    pack["events"].append({"kind": kind, "observed_at": observed_at or _now(), "details": details})


def _cast_errors(slot, node):
    """New nodes name their own cast; a global cast line must not contradict it."""
    cast = node["cast"]
    if not isinstance(cast, list) or not cast or any(not isinstance(member, dict)
            or set(member) != {"name", "anatomy_invariants"} or not str(member["name"]).strip()
            or not isinstance(member["anatomy_invariants"], list) or not member["anatomy_invariants"]
            or not all(isinstance(item, str) and item.strip() for item in member["anatomy_invariants"])
            for member in cast):
        return [slot + ": cast needs named members with nonempty anatomy_invariants"]
    names = {member["name"].strip().casefold() for member in cast}
    errors = []
    for line in str(node.get("prompt", "")).splitlines():
        if line.strip().casefold().startswith("cast:"):
            extra = [name.strip() for name in line.split(":", 1)[1].split(",")
                     if name.strip() and name.strip().casefold() not in names]
            if extra:
                errors.append(slot + ": global cast line contradicts local cast: " + ", ".join(extra))
    return errors


def _prompt(node):
    if "cast" not in node:
        return node["prompt"]
    return node["prompt"] + "\nScene cast anatomy: " + " | ".join(
        member["name"] + ": " + "; ".join(member["anatomy_invariants"]) for member in node["cast"])


def _roles(pack, asset_id):
    declarations = pack["specification"].get("shared_roles", []) + [
        role for shot in pack["specification"]["shots"] for role in shot["roles"]]
    own = pack["prompt_pack"].get(asset_id)
    if own is None:
        own = next((r for r in pack["specification"]["references"] if r["id"] == asset_id), None)
    roles = ([own["role"]] if own else []) + [role["role"] for role in declarations if role["asset_id"] == asset_id]
    return list(dict.fromkeys(roles))


def _validate_spec(root, spec):
    """Report every field omission together; graph checks follow valid fields."""
    if spec.get("version") not in ("1.0", "1.1") or not str(spec.get("reviewer", "")).strip():
        raise ValueError("board specification requires version 1.0 or 1.1 and a persistent reviewer")
    for source in [spec["story"], spec["approval"], *spec["references"]]:
        _inside(root, source["path"])
    nodes, errors = {}, []
    references = {ref["id"]: ref for ref in spec["references"]}
    if len(references) != len(spec["references"]):
        errors.append("duplicate shared reference ID")
    required = set()
    if spec["version"] == "1.1":
        # Names only, from the engine's board-asset inventory; never verdicts.
        from lib.shot_contract import ASSET_PREDICATES
        required = set(ASSET_PREDICATES)
    for node in spec["image_nodes"]:
        slot = node.get("slot_id")
        label = str(slot)
        if set(node) - NODE_FIELDS:
            errors.append(label + ": unsupported image node fields; native tooling has one prompt per call")
        if not slot or slot in nodes or slot in references:
            errors.append(label + ": duplicate or missing image slot ID")
        if not str(node.get("prompt", "")).strip() or not node.get("required_predicates"):
            errors.append(label + ": image node requires a prompt and named essential review predicates")
        missing = sorted(required - set(node.get("required_predicates") or []))
        if missing:
            errors.append(label + ": missing engine-required asset predicates " + ", ".join(missing))
        if type(node.get("transparent_background", False)) is not bool:
            errors.append(label + ": native transparent_background must be boolean")
        if "cast" in node:
            errors += _cast_errors(label, node)
        try:
            _inside(root, node["output_path"])
        except (ValueError, KeyError) as exc:
            errors.append(label + ": " + (str(exc) if isinstance(exc, ValueError) else "missing output_path"))
        if slot and slot not in nodes and slot not in references:
            nodes[slot] = node
    if errors:
        raise ValueError("; ".join(errors))
    shot_ids = [shot["shot_id"] for shot in spec["shots"]]
    if len(set(shot_ids)) != len(shot_ids):
        raise ValueError("duplicate shot ID")
    for node in nodes.values():
        if node["shot_id"] not in shot_ids:
            raise ValueError("board slot has an unknown owning shot")
        if any(ref not in references for ref in node.get("reference_ids", [])):
            raise ValueError("unknown shared image reference")
        if any(dep not in nodes for dep in node.get("image_dependencies", [])):
            raise ValueError("unknown image dependency")
    visiting, visited = set(), set()
    def visit(slot):
        if slot in visiting:
            raise ValueError("image dependency cycle")
        if slot in visited:
            return
        visiting.add(slot)
        for dep in nodes[slot].get("image_dependencies", []):
            visit(dep)
        visiting.remove(slot)
        visited.add(slot)
    for slot in nodes:
        visit(slot)
    declarations = spec.get("shared_roles", []) + [role for shot in spec["shots"] for role in shot["roles"]]
    if any(role["asset_id"] not in nodes and role["asset_id"] not in references for role in declarations):
        raise ValueError("unknown board role source")
    for shot in spec["shots"]:
        if any(dep not in shot_ids or dep == shot["shot_id"] for dep in shot.get("video_dependencies", [])):
            raise ValueError("unknown/self actual outgoing-video dependency")
    bridge = spec.get("motion_binding")
    if bridge is not None:
        if not isinstance(bridge, dict) or set(bridge) != {"contract_template", "motion_handoffs"}:
            raise ValueError("motion_binding needs exact contract_template and explicit motion_handoffs")
        if not isinstance(bridge["contract_template"], dict) or not isinstance(bridge["motion_handoffs"], dict):
            raise ValueError("motion template and handoffs must be objects")
        for aid, handoff in bridge["motion_handoffs"].items():
            if (not isinstance(handoff, dict)
                    or set(handoff) != {"asset_id", "scene_id", "approved", "pinned_final_frame"}
                    or type(handoff.get("approved")) is not bool
                    or handoff.get("asset_id") != aid or handoff.get("scene_id") not in shot_ids
                    or not isinstance(handoff.get("pinned_final_frame"), dict)):
                raise ValueError("motion handoff needs exact authored asset/scene and explicit approval")
            pin = handoff["pinned_final_frame"]
            if (set(pin) != {"asset_id", "approved", "requirement_id"}
                    or type(pin["approved"]) is not bool
                    or any(not isinstance(pin[k], str) or not pin[k].strip()
                        for k in ("asset_id", "requirement_id"))):
                raise ValueError("motion ending pin needs exact authored endpoint/requirement and approval")
        from jsonschema import Draft202012Validator
        from schemas.artifacts import load_schema
        # Validate authored semantics without dummy bytes or synthetic reviews.
        # The three unresolved asset bindings and mechanical shot fields are
        # deliberately absent until real observations/package artifacts exist.
        shape = deepcopy(load_schema("shot_contract"))
        shape["required"] = [k for k in shape["required"] if k not in {"project_id", "story_revision"}]
        shape["$defs"]["asset"]["allOf"] = []
        shape["$defs"]["asset"]["required"] = ["id", "role", "cast_ids"]
        shape["$defs"]["shot"]["required"] = [k for k in shape["$defs"]["shot"]["required"]
            if k not in {"dominant_action", "duration_seconds", "completed_end_state"}]
        errors = list(Draft202012Validator(shape).iter_errors(bridge["contract_template"]))
        if errors:
            raise ValueError("invalid authored motion template: " + errors[0].message)
        assets = bridge["contract_template"].get("assets", [])
        if not assets or len({a["id"] for a in assets}) != len(assets):
            raise ValueError("motion contract needs unique declared board assets")
        for asset in assets:
            if asset["id"] not in nodes and asset["id"] not in references:
                raise ValueError("motion contract names an undeclared board")
            if set(asset) & {"path", "sha256", "review"}:
                raise ValueError("motion asset bindings must come from actual current board observations")
            declared = [r["role"] for r in declarations if r["asset_id"] == asset["id"]]
            declared += [nodes.get(asset["id"], references.get(asset["id"]))["role"]]
            if asset["role"] not in declared:
                raise ValueError("motion role must be explicitly declared and reviewed")
        template = bridge["contract_template"]
        if [s["id"] for s in template["shots"]] != shot_ids:
            raise ValueError("motion template must cover the exact ordered board shot IDs")
        asset_ids = {a["id"] for a in assets}
        if template.get("payoff_asset_id") not in asset_ids:
            raise ValueError("motion payoff must name a declared contract asset")
        aliases = {}
        for aid in nodes.keys() | references.keys():
            shared = any(r["asset_id"] == aid for r in spec.get("shared_roles", []))
            users = [s["shot_id"] for s in spec["shots"]
                if shared or any(r["asset_id"] == aid for r in s["roles"])] or [shot_ids[0]]
            for index, sid in enumerate(users):
                alias = motion_reference_alias(spec, aid, sid)
                if alias in aliases or (index and alias in nodes.keys() | references.keys()):
                    raise ValueError("motion scene alias collides with a declared board asset")
                aliases[alias] = (aid, sid)
        for index, shot in enumerate(template["shots"]):
            local = {r["asset_id"] for r in spec["shots"][index]["roles"] + spec.get("shared_roles", [])}
            if not set(shot["asset_ids"]) <= asset_ids & local:
                raise ValueError("motion shot asset references must be declared for that shot")
            upstream = [b["shot_id"] for b in shot["upstream"]]
            if (len(set(upstream)) != len(upstream) or any(s not in shot_ids[:index] for s in upstream)
                    or set(upstream) != set(spec["shots"][index].get("video_dependencies", []))):
                raise ValueError("motion upstream must match explicit preceding video dependencies")
            treatment = shot.get("reference_mode", template.get("reference_mode"))
            if treatment == "frame_conditioned":
                frame = shot.get("frame_input")
                starts = [role for role in spec["shots"][index]["roles"] if role["role"] == "start_frame"]
                if (template.get("planning_mode") != "shot_local_v1" or len(starts) != 1 or frame != starts[0]
                        or frame["asset_id"] not in shot["asset_ids"]):
                    raise ValueError("frame-conditioned motion needs one exact existing consumer starting role")
                starting = next(a for a in assets if a["id"] == frame["asset_id"])
                if starting["role"] not in {"start_frame", "payoff_board"} or set(starting["cast_ids"]) != set(shot["cast_ids"]):
                    raise ValueError("frame-conditioned starting composition needs exact declared cast")
            elif "frame_input" in shot:
                raise ValueError("frame_input requires explicit frame_conditioned treatment")
            for aid in shot["asset_ids"]:
                source = next(a for a in assets if a["id"] == aid).get("upstream_source")
                if source and source["shot_id"] not in upstream:
                    raise ValueError("motion asset upstream source lacks its shot dependency")
        for aid, handoff in bridge["motion_handoffs"].items():
            endpoint = handoff["pinned_final_frame"]["asset_id"]
            sid = handoff["scene_id"]
            if (aid not in aliases or endpoint not in aliases
                    or aliases[aid][1] != sid or aliases[endpoint][1] != sid
                    or any(aliases[key][0] not in asset_ids for key in (aid, endpoint))):
                raise ValueError("motion handoff endpoints must be exact declared scene aliases")
            roles = {a["id"]: a["role"] for a in assets}
            if roles[aliases[aid][0]] != "start_frame" or roles[aliases[endpoint][0]] != "end_frame":
                raise ValueError("motion handoff must bind start_frame to end_frame")
    return nodes


def _motion_template_for_plan(spec, plan):
    """Join authored mechanics before publication; no bytes/reviews are fabricated."""
    bridge = spec.get("motion_binding")
    if bridge is None:
        return None
    template = deepcopy(bridge["contract_template"])
    shot_ids = [s["shot_id"] for s in spec["shots"]]
    if [s["id"] for s in plan["scenes"]] != shot_ids:
        raise ValueError("motion projection needs exact ordered scene plan coverage")
    for key in ("project_id", "story_revision"):
        if key in template and template[key] != plan["metadata"][key]:
            raise ValueError("motion template has a stale " + key + " binding")
        template[key] = plan["metadata"][key]
    cards = plan.get("metadata", {}).get("visual_development", {}).get("shot_cards", {})
    for shot, scene in zip(template["shots"], plan["scenes"]):
        values = {"dominant_action": scene["description"],
                  "duration_seconds": scene["end_seconds"] - scene["start_seconds"]}
        card = cards.get(shot["id"], {})
        if card.get("end_state"):
            values["completed_end_state"] = card["end_state"]
        for key, value in values.items():
            if key in shot and shot[key] != value:
                raise ValueError("motion template contradicts authored shot " + shot["id"] + "/" + key)
            shot[key] = value
        if "cast" in card and card["cast"] != shot["cast_ids"]:
            raise ValueError("motion template contradicts authored shot cast")
        for line in shot["dialogue"]:
            if not line["start_seconds"] < line["end_seconds"] <= shot["duration_seconds"]:
                raise ValueError("motion dialogue timing exceeds authored shot")
        handoffs = [h for h in bridge["motion_handoffs"].values() if h["scene_id"] == shot["id"]]
        pin = card.get("pinned_final_frame", {})
        if pin.get("required") and (len(handoffs) != 1
                or handoffs[0]["pinned_final_frame"]["requirement_id"] != pin["requirement_id"]):
            raise ValueError("authored ending pin needs one matching explicit motion handoff")
        if handoffs and (not pin.get("required") or any(
                h["pinned_final_frame"]["requirement_id"] != pin["requirement_id"] for h in handoffs)):
            raise ValueError("motion handoff contradicts authored ending pin requirement")
    from jsonschema import Draft202012Validator
    from schemas.artifacts import load_schema
    shape = deepcopy(load_schema("shot_contract"))
    shape["$defs"]["asset"]["allOf"] = []
    shape["$defs"]["asset"]["required"] = ["id", "role", "cast_ids"]
    errors = list(Draft202012Validator(shape).iter_errors(template))
    if errors:
        raise ValueError("invalid projected motion template: " + errors[0].message)
    return template


@_board_transaction
def prepare_board_pack(project_dir, specification, *, images):
    """Persist one current inventory and all text, including unresolved slots.

    `images` is lib.production_images from the validated configured engine.
    Existing matching packs are returned unchanged; changed authorship requires
    a new reviewed episode preparation rather than overwriting this record.
    """
    started_at = _now()
    root = Path(project_dir).resolve()
    spec = deepcopy(specification)
    nodes = _validate_spec(root, spec)
    plan_path = root / "artifacts/scene_plan.json"
    if spec.get("motion_binding") and plan_path.exists():
        _motion_template_for_plan(spec, json.loads(plan_path.read_text()))
    existing = root / ARTIFACT
    if existing.exists():
        pack = load_board_pack(root)
        if pack["specification_sha256"] != _digest(spec):
            raise ValueError("current board pack already binds another specification")
        _promote_motion_bindings(root, pack)
        return deepcopy(pack)
    slots = [{"id": n["slot_id"], "shot_id": n["shot_id"], "role": n["role"], "status": "unresolved"} for n in nodes.values()]
    # No unresolved outputs means pure reuse: U1's pre-board packet deliberately
    # requires at least one unresolved slot, and no image call needs a packet.
    packet = images.build_preboard_packet(root, story=spec["story"], approval=spec["approval"],
        references=spec["references"], board_slots=slots) if slots else None
    marker = json.loads((root / "project.json").read_text())
    pack = {"version": "1.0", "project_id": marker["project_id"], "story_revision": marker["story_revision"],
        "specification": spec, "specification_sha256": _digest(spec), "preboard_packet": packet,
        "prompt_pack": deepcopy(nodes), "board_bindings": {}, "image_packets": {}, "image_packet_history": [],
        "observations": [], "events": []}
    for reference in spec["references"]:
        roles = _roles(pack, reference["id"])
        for role in roles:
            images.validate_image_reuse(root, reference, role=role)
        pack["board_bindings"][reference["id"]] = {"path": reference["path"], "sha256": reference["sha256"], "origin": "reuse"}
        if roles:
            pack["observations"].append({"asset_id": reference["id"], "roles": roles, "review": deepcopy(reference["review"]), "origin": "reused_current_observation"})
    # Bound starts at pack preparation; earlier authoring time is unobserved.
    _event(pack, "preparation_started", started_at, marker={"project_id": pack["project_id"],
        "story_revision": pack["story_revision"]}, media_kind="image", bound="board_preparation_only")
    _event(pack, "prompt_pack_ready", slots=list(nodes), reused_assets=list(pack["board_bindings"]))
    _promote_motion_bindings(root, pack, publish=False)
    _save(root, pack)
    _promote_motion_bindings(root, pack)
    return deepcopy(pack)


def _observation(pack, asset_id, role=None):
    binding = pack["board_bindings"].get(asset_id)
    return next((o for o in reversed(pack["observations"]) if o["asset_id"] == asset_id
        and binding and o["review"]["subject_sha256"] == binding["sha256"]
        and (role is None or role in o["roles"])), None)


def _correction_observation(pack, asset_id):
    """Newest actual failure still current for at least one declared asset role."""
    current = [_observation(pack, asset_id, role) for role in _roles(pack, asset_id)]
    return next((observation for observation in reversed(pack['observations'])
        if observation in current and any(predicate.get('severity') == 'critical'
            and predicate['status'] == 'fail' for predicate in observation['review']['predicates'])), None)


def _source_depends_on_slot(pack, asset_id, slot_id, visiting=None):
    """Include actual retained sources and authored dependencies in cycle checks."""
    if asset_id == slot_id:
        return True
    visiting = set(visiting or ())
    if asset_id in visiting:
        return False
    visiting.add(asset_id)
    node = pack['prompt_pack'].get(asset_id, {})
    sources = set(pack['board_bindings'].get(asset_id, {}).get('source_bindings', {}))
    sources.update(node.get('reference_ids', []) + node.get('image_dependencies', []))
    return any(_source_depends_on_slot(pack, source, slot_id, visiting) for source in sources)


def _review_blockers(pack, asset_id, role):
    observation = _observation(pack, asset_id, role)
    if not observation:
        return ["missing current role review"]
    review = observation["review"]
    predicates = {p["name"]: p for p in review["predicates"]}
    required = pack["prompt_pack"].get(asset_id, {}).get("required_predicates", [])
    errors = [name for name in required if name not in predicates or predicates[name]["status"] != "pass"]
    errors += [p["name"] for p in predicates.values() if p.get("severity") == "critical" and p["status"] != "pass"]
    if review["status"] != "pass":
        errors.append("review " + review["status"])
    return list(dict.fromkeys(errors))


def _packet_sources_current(root, pack, record):
    for asset_id, expected in record["source_bindings"].items():
        current = pack["board_bindings"].get(asset_id)
        if not current or any(current.get(key) != expected[key] for key in ("path", "sha256")) or not _current(root, current):
            return False
    return True


def _preceding_image_request(root, pack, slot_id):
    source = pack["board_bindings"][slot_id]
    records = pack["image_packet_history"] + list(pack["image_packets"].values())
    preceding = next((record for record in reversed(records) if record["slot_id"] == slot_id
        and _inside(root, record["output_path"]) == _inside(root, source["path"])), None)
    if preceding is None:
        raise ValueError("replacement correction needs the preceding bound image request")
    return preceding


def board_schedule(project_dir, *, images):
    """Report image work and reviewed input candidates, never motion authority."""
    root = Path(project_dir).resolve()
    pack = load_board_pack(root)
    stale = [asset_id for asset_id in pack["board_bindings"] if not _binding_current(root, pack, asset_id)]
    usage = images.image_usage(root)
    pending = {}
    recoverable = []
    native_statuses = {row["call_id"]: images.image_call_status(root, row["call_id"])
        for row in usage["occurrences"] if row["route"] == "native_imagegen"}
    replaced_originals = {(status.get("replaces_host_exception") or {}).get("call_id")
        for status in native_statuses.values()}
    for occurrence in usage["occurrences"]:
        if occurrence["call_id"] in usage["unresolved_originals"]:
            pending.setdefault(occurrence["slot_id"], []).append({key: occurrence[key]
                for key in ("call_id", "state", "route")})
            if occurrence["route"] != "native_imagegen":
                continue
            status = native_statuses[occurrence["call_id"]]
            exception = status.get("host_exception")
            if (occurrence["route"] == "native_imagegen" and occurrence["state"] == "uncertain"
                    and isinstance(exception, dict)
                    and exception.get("kind") == "agent_recorded_host_exception"
                    and not status.get("replaces_host_exception")
                    and not status.get("host_receipt") and not status.get("output")):
                if occurrence["call_id"] in replaced_originals:
                    continue
                recoverable.append({"slot_id": occurrence["slot_id"], "call_id": occurrence["call_id"],
                    "state": occurrence["state"], "route": occurrence["route"],
                    "exception_receipt_sha256": exception["sha256"],
                    "original_request_sha256": status["request_sha256"],
                    "eligibility": "engine must recheck actual return and exact approved scope before reservation"})
    ready, blocked = [], {}
    nodes = pack["prompt_pack"]
    for slot, node in nodes.items():
        if slot in pending:
            continue
        if slot in pack["board_bindings"] and slot not in stale:
            continue
        missing = [asset_id for asset_id in node.get("reference_ids", []) + node.get("image_dependencies", [])
                   if asset_id not in pack["board_bindings"] or asset_id in stale]
        if missing:
            blocked[slot] = missing
        else:
            ready.append(slot)
    # Count each affected shot once, including image descendants and shared roles.
    def uses(slot):
        descendants = {slot}
        changed = True
        while changed:
            before = len(descendants)
            descendants.update(sid for sid, node in nodes.items() if set(node.get("image_dependencies", [])) & descendants)
            changed = len(descendants) != before
        shared = {r["asset_id"] for r in pack["specification"].get("shared_roles", [])}
        return [s["shot_id"] for s in pack["specification"]["shots"] if shared & descendants
            or {r["asset_id"] for r in s["roles"]} & descendants]
    counts = {slot: len(uses(slot)) for slot in nodes}
    ready.sort(key=lambda slot: -counts[slot])
    shot_blockers, candidates = {}, []
    from lib.shot_contract import shot_local, required_asset_ids
    template = pack['specification'].get('motion_binding', {}).get('contract_template', {})
    local = shot_local(template)
    for shot in pack["specification"]["shots"]:
        reasons = []
        consumer = next((s for s in template.get('shots', []) if s['id'] == shot['shot_id']), None)
        needed = required_asset_ids(template, consumer) if local and consumer else None
        for role in pack["specification"].get("shared_roles", []) + shot["roles"]:
            asset_id = role["asset_id"]
            if needed is not None and asset_id not in needed:
                continue
            if asset_id not in pack["board_bindings"] or asset_id in stale:
                reasons.append(asset_id + ": missing/stale bytes")
            else:
                reasons += [asset_id + ": " + reason for reason in _review_blockers(pack, asset_id, role["role"])]
        if reasons:
            shot_blockers[shot["shot_id"]] = list(dict.fromkeys(reasons))
        else:
            candidates.append(shot["shot_id"])
    return {"ready_images": ready, "blocked_images": blocked, "pending_images": pending,
        "recoverable_image_exceptions": recoverable, "unblock_counts": counts,
        "stale_boards": stale, "stale_image_packets": [slot for slot, record in pack["image_packets"].items()
            if not _packet_sources_current(root, pack, record)], "board_ready_shots": candidates,
        "board_blockers": shot_blockers, "video_dependencies": {s["shot_id"]: s.get("video_dependencies", [])
            for s in pack["specification"]["shots"]}, "motion_authority": "canonical governed dry_run required",
        "motion_projection": deepcopy(pack.get("motion_projection"))}


@_board_transaction
def prepare_image_request(project_dir, slot_id, *, correction=None, replace_exception_call_id=None, images):
    """Materialize only current references; no dependency waits enter text prompts."""
    root = Path(project_dir).resolve()
    pack = load_board_pack(root)
    node = pack["prompt_pack"][slot_id]
    if correction is not None and replace_exception_call_id is not None:
        raise ValueError("targeted correction cannot also replace an unresolved host exception")
    if correction is not None:
        expected = board_correction_intent(root, slot_id, changes=correction["changes"],
            **({"replacement_prompt": correction["replacement_prompt"]} if "replacement_prompt" in correction else {}),
            **({"reference_ids": correction["reference_ids"]} if "reference_ids" in correction else {}))
        if expected != correction:
            raise ValueError("targeted image correction is stale or changed")
    bindings = {}
    refs = []
    reference_ids = correction.get("reference_ids") if correction is not None else None
    if reference_ids is None:
        reference_ids = node.get("reference_ids", []) + node.get("image_dependencies", [])
    for asset_id in reference_ids:
        binding = pack["board_bindings"].get(asset_id)
        if not binding or not _binding_current(root, pack, asset_id):
            raise ValueError("image dependency needs current bytes: " + asset_id)
        bindings[asset_id] = {key: binding[key] for key in ("path", "sha256")}
        reference = next((r for r in pack["specification"]["references"] if r["id"] == asset_id), None)
        if reference is None:
            source = pack["prompt_pack"][asset_id]
            reference = {"id": asset_id, "role": source["role"], **bindings[asset_id],
                "applicability": {"story_revision": pack["story_revision"], "roles": [source["role"]]}}
        refs.append(deepcopy(reference))
    prompt, output = _prompt(node), node["output_path"]
    recovery_status = None
    if replace_exception_call_id is not None:
        recovery_status = images.image_call_status(root, replace_exception_call_id)
        exception = recovery_status.get("host_exception")
        if (recovery_status.get("slot_id") != slot_id or recovery_status.get("state") != "uncertain"
                or not isinstance(exception, dict)
                or exception.get("kind") != "agent_recorded_host_exception"
                or recovery_status.get("replaces_host_exception")
                or recovery_status.get("host_receipt") or recovery_status.get("output")):
            raise ValueError("bounded replacement needs the exact recorded native host exception for this slot")
        usage = images.image_usage(root)
        for occurrence in usage["occurrences"]:
            if occurrence["route"] != "native_imagegen" or occurrence["call_id"] == replace_exception_call_id:
                continue
            candidate = images.image_call_status(root, occurrence["call_id"])
            if ((candidate.get("replaces_host_exception") or {}).get("call_id") == replace_exception_call_id):
                # Reuse the exact packet on replay; engine admission only treats
                # the same child call ID as idempotent and rejects another one.
                if not pack["image_packets"].get(slot_id) or pack["image_packets"][slot_id]["request_sha256"] != candidate["request_sha256"]:
                    raise ValueError("recorded host exception already has its one bounded replacement")
        # Keep the replacement request digest and immutable prompt pack distinct
        # from the original envelope. The original output may still arrive late.
        prior = Path(output)
        suffix = hashlib.sha256(replace_exception_call_id.encode()).hexdigest()[:12]
        output = str(prior.with_name(prior.stem + "-recovery-" + suffix + prior.suffix))
    if slot_id in pack["board_bindings"] and not _binding_current(root, pack, slot_id):
        prior = Path(output)
        output = str(prior.with_name(prior.stem + "-sources-" + _digest(bindings)[:16] + prior.suffix))
    if correction is not None:
        if "source_binding" in correction:
            preceding = _preceding_image_request(root, pack, slot_id)
            original = images.image_call_status(root, correction["source_binding"].get("call_id"))
            preceding_digest = images.image_request_digest(preceding["packet"], slot_id,
                preceding["arguments"], output_path=preceding["output_path"])
            if (preceding_digest != correction["preceding_request_sha256"]
                    or original["request_sha256"] != preceding_digest
                    or original["state"] != "imported" or original["slot_id"] != slot_id
                    or original["output"]["sha256"] != correction["source_sha256"]):
                raise ValueError("preceding request differs from the retained original image call")
        prompt = correction["replacement_prompt"] if "replacement_prompt" in correction else prompt + "\nTargeted correction: " + correction["changes"]
        output = correction["output_path"]
    previous = pack["image_packets"].get(slot_id)
    if previous and previous["source_bindings"] == bindings and previous["arguments"]["prompt"] == prompt and previous["output_path"] == output:
        if correction is not None and previous.get("correction_intent") != correction:
            raise ValueError("retained targeted image correction is stale or changed")
        return deepcopy(previous)
    packet = images.build_preboard_packet(root, story=pack["specification"]["story"], approval=pack["specification"]["approval"],
        references=refs, board_slots=pack["preboard_packet"]["board_slots"])
    arguments = {"prompt": prompt, "transparent_background": node.get("transparent_background", False)}
    if refs:
        arguments["referenced_image_paths"] = [str(_inside(root, r["path"])) for r in refs]
    record = {"slot_id": slot_id, "packet": packet, "arguments": arguments, "output_path": output,
        "source_bindings": bindings, "request_sha256": images.image_request_digest(packet, slot_id, arguments, output_path=output)}
    if correction is not None:
        record["correction_intent"] = deepcopy(correction)
    if previous:
        pack["image_packet_history"].append(previous)
    pack["image_packets"][slot_id] = record
    _event(pack, "image_packet_ready", slot_id=slot_id, request_sha256=record["request_sha256"],
        **({"correction_intent": deepcopy(correction)} if correction is not None else {}))
    _save(root, pack)
    return deepcopy(record)


def reserve_board_image(project_dir, slot_id, *, scope_id, call_id=None, correction=None,
                        replace_exception_call_id=None, images):
    root = Path(project_dir).resolve()
    pack = load_board_pack(root)
    binding = pack["board_bindings"].get(slot_id)
    if binding and correction is None and replace_exception_call_id is None and _binding_current(root, pack, slot_id):
        if call_id and call_id == binding.get("call_id"):
            original = images.image_call_status(root, call_id)
            if original["scope_id"] != scope_id:
                raise ValueError("existing image call binds another exact scope")
            return original
        raise ValueError("current board bytes are reused; a new call needs an essential targeted correction")
    request = prepare_image_request(project_dir, slot_id, correction=correction,
        replace_exception_call_id=replace_exception_call_id, images=images)
    return images.reserve_native_image(project_dir, request["packet"], slot_id, request["arguments"],
        output_path=request["output_path"], scope_id=scope_id, call_id=call_id,
        replace_exception_call_id=replace_exception_call_id)


def motion_reference_alias(spec, aid, shot_id):
    """One stable alias per (reference, consumer shot) from the specification.

    Declared/shared users in authored shot order own the aliases; the first owns
    the unsuffixed ID. Local consumer filtering selects which aliases are needed
    but never rebases the unsuffixed ID onto a later filtered consumer.
    """
    shared = any(r["asset_id"] == aid for r in spec.get("shared_roles", []))
    users = [s["shot_id"] for s in spec["shots"]
             if shared or any(r["asset_id"] == aid for r in s["roles"])] or [spec["shots"][0]["shot_id"]]
    return aid if shot_id == users[0] else aid + "--" + shot_id


def _imported_board_gate(root, pack, slot_id, call_id, *, images):
    """The single request gate shared by the selector and bind_imported_board.

    Returns (kind, payload): ('current', None); ('successor', successor) when a
    linked successor is bound current or already submitted; ('restore', record)
    with the last exact retained current-source original packet; or (None, None).
    """
    result = images.image_call_status(root, call_id)
    prepared = pack["image_packets"].get(slot_id)
    if prepared and result["request_sha256"] == prepared["request_sha256"]:
        return "current", None
    linked_lane = False
    if prepared:
        for occurrence in images.image_usage(root)["occurrences"]:
            if occurrence["route"] != "native_imagegen":
                continue
            successor = images.image_call_status(root, occurrence["call_id"])
            link = successor.get("replaces_host_exception") or {}
            if (link.get("call_id") == call_id and successor.get("slot_id") == slot_id
                    and successor.get("request_sha256") == prepared["request_sha256"]
                    and link.get("request_sha256") == result["request_sha256"]
                    and link.get("receipt_sha256") == (result.get("host_exception") or {}).get("sha256")):
                linked_lane = True
                current = pack["board_bindings"].get(slot_id)
                # Keep a submitted successor's lane intact. An un-emitted reservation
                # cannot outrank the authentic original result.
                if ((current and current.get("call_id") == successor["call_id"] and _binding_current(root, pack, slot_id))
                        or any(e.get("kind") == "submitted" for e in successor.get("events", []))):
                    return "successor", successor
                break
        if not linked_lane and result.get("host_exception"):
            # Before any child reservation, the retained recovery packet itself is
            # the intent identity: prepare_image_request derives its output from
            # exactly this original call ID. Ordinary corrections never do.
            marker = "-recovery-" + hashlib.sha256(call_id.encode()).hexdigest()[:12]
            linked_lane = marker in Path(prepared["output_path"]).stem
    # Restore only reverses a lane moved by this original's own host-exception
    # recovery; a normal correction/rejection keeps the original superseded.
    if not result.get("host_exception") or (prepared and not linked_lane):
        return None, "superseded"
    candidates = []
    for record in pack["image_packet_history"] + list(pack["image_packets"].values()):
        if record.get("slot_id") != slot_id:
            continue
        digest = images.image_request_digest(record["packet"], slot_id, record["arguments"],
            output_path=record["output_path"])
        if (digest == result["request_sha256"] == record.get("request_sha256")
                and _inside(root, record["output_path"]) == _inside(root, result["output"]["path"])
                and _packet_sources_current(root, pack, record)):
            candidates.append(record)
    return ("restore", candidates[-1]) if candidates else (None, None)


def imported_board_eligibility(root, pack, slot_id, call_id, *, images):
    """Pure projection of bind_imported_board's gate: 'current', 'restore',
    'successor' or None (superseded); zero writes."""
    return _imported_board_gate(root, pack, slot_id, call_id, images=images)[0]


@_board_transaction
def bind_imported_board(project_dir, slot_id, call_id, *, images):
    """Promote attributable image bytes in preparation only; no contract mutation."""
    root = Path(project_dir).resolve()
    pack = load_board_pack(root)
    if slot_id not in pack["prompt_pack"]:
        raise ValueError("unknown board slot")
    result = images.image_call_status(root, call_id)
    if result["slot_id"] != slot_id or result["state"] != "imported" or not result.get("output_current"):
        raise ValueError("board promotion needs the exact imported current slot bytes")
    prepared = pack["image_packets"].get(slot_id)
    kind, payload = _imported_board_gate(root, pack, slot_id, call_id, images=images)
    if kind == "successor":
        current = pack["board_bindings"].get(slot_id)
        if (current and current.get("call_id") == payload["call_id"]
                and _binding_current(root, pack, slot_id)):
            return deepcopy(current)
        return None
    if kind == "restore":
        original = deepcopy(payload)
        current_packet = pack["image_packets"].get(slot_id)
        if current_packet and current_packet["request_sha256"] != original["request_sha256"]:
            pack["image_packet_history"].append(current_packet)
        pack["image_packets"][slot_id] = original
        _event(pack, "image_packet_restored", slot_id=slot_id,
            request_sha256=original["request_sha256"], reason="imported_original_host_exception_result")
        _save(root, pack)
        prepared = original
    elif kind is None:
        if result.get("host_exception") and payload != "superseded":
            raise ValueError("host-exception original return has no exact retained current-source packet")
        raise ValueError("image return differs from the current prepared board request")
    binding = {"path": result["output"]["path"], "sha256": result["output"]["sha256"], "origin": "canonical_image_import", "call_id": call_id, "route": result.get("route", "native_imagegen"), "source_bindings": deepcopy(prepared["source_bindings"])}
    if pack["board_bindings"].get(slot_id) != binding:
        pack["board_bindings"][slot_id] = binding
        _event(pack, "board_promoted", slot_id=slot_id, call_id=call_id, sha256=binding["sha256"])
        _save(root, pack)
    _promote_motion_bindings(root, pack)
    return deepcopy(binding)


@_board_transaction
def record_board_review(project_dir, asset_id, review, *, roles, started_at=None, images):
    """Retain the designated review once; essential unknown/failure stays visible."""
    from jsonschema import Draft202012Validator
    from schemas.artifacts import load_schema
    if started_at is not None:
        _parse_time(started_at)
    root = Path(project_dir).resolve()
    pack = load_board_pack(root)
    binding = pack["board_bindings"][asset_id]
    schema = load_schema("shot_contract")
    Draft202012Validator({"$defs": schema["$defs"], "$ref": "#/$defs/review"}).validate(review)
    if review["reviewer"] != pack["specification"]["reviewer"]:
        raise ValueError("new board observations require the persistent reviewer")
    if review["subject_sha256"] != binding["sha256"] or review["story_revision"] != pack["story_revision"] or not _binding_current(root, pack, asset_id):
        raise ValueError("board review has stale bytes/story")
    if not roles or set(roles) - set(_roles(pack, asset_id)):
        raise ValueError("review must name applicable declared board roles")
    existing = next((o for o in pack["observations"] if o["review"]["review_id"] == review["review_id"] and o["asset_id"] == asset_id), None)
    observation = {"asset_id": asset_id, "roles": list(dict.fromkeys(roles)), "review": deepcopy(review), "origin": "designated_reviewer"}
    if existing:
        if any(existing[key] != observation[key] for key in observation):
            raise ValueError("retained observation ID already binds another verdict")
        _promote_motion_bindings(root, pack)
        return deepcopy(existing)
    # No fabricated review-start time; completion is the observed retention boundary.
    if started_at is not None:
        observation["review_started_at"] = started_at
    observation["review_completed_at"] = _now()
    pack["observations"].append(observation)
    _event(pack, "board_review_retained", asset_id=asset_id, review_id=review["review_id"], status=review["status"])
    _save(root, pack)
    _promote_motion_bindings(root, pack)
    return deepcopy(observation)


def board_correction_intent(project_dir, slot_id, *, changes, replacement_prompt=_NO_REPLACEMENT,
                            reference_ids=None):
    """Describe an essential correction; a supplied complete prompt replaces verbatim.

    Omitted replacement_prompt retains the original append-only intent contract.
    Both forms bind the authored prompt, current review/source and preceding
    request. Optional ordered sources preserve the base sources; standing scope
    admission separately checks their immutable inventory and transfer authority.
    """
    pack = load_board_pack(project_dir)
    if slot_id not in pack["prompt_pack"]:
        raise ValueError("a reused reference needs an explicit missing/changed image slot")
    observation = _correction_observation(pack, slot_id)
    failed = [p["name"] for p in observation["review"]["predicates"] if p.get("severity") == "critical" and p["status"] == "fail"] if observation else []
    if not failed or not isinstance(changes, str) or not changes.strip():
        raise ValueError("targeted correction needs a named critical failed predicate and exact changes")
    source = pack["board_bindings"][slot_id]
    if not _binding_current(Path(project_dir).resolve(), pack, slot_id):
        raise ValueError("targeted correction needs current source bytes and bindings")
    node = pack["prompt_pack"][slot_id]
    provenance = {}
    # Legacy exact-scope append corrections can target manually bound/CLI boards.
    # Standing admission requires the complete native proof below separately.
    if replacement_prompt is not _NO_REPLACEMENT or (source.get("origin") == "canonical_image_import"
            and source.get("route") == "native_imagegen" and source.get("call_id")):
        preceding = _preceding_image_request(Path(project_dir).resolve(), pack, slot_id)
        provenance = {"original_prompt": node["prompt"],
            "original_prompt_sha256": _digest(node["prompt"]), "specification_sha256": pack["specification_sha256"],
            "source_binding": deepcopy(source), "review_sha256": _digest(observation["review"]),
            "preceding_request_sha256": preceding["request_sha256"],
            "preceding_request": {key: deepcopy(preceding[key]) for key in ("arguments", "output_path", "source_bindings", "request_sha256")},
            "preceding_correction_intent": deepcopy(preceding.get("correction_intent"))}
    if replacement_prompt is not _NO_REPLACEMENT:
        if not isinstance(replacement_prompt, str) or not replacement_prompt.strip():
            raise ValueError("replacement_prompt must be a nonempty complete prompt")
        provenance["replacement_prompt"] = replacement_prompt
    if reference_ids is not None:
        base = node.get("reference_ids", []) + node.get("image_dependencies", [])
        if (not isinstance(reference_ids, list) or not reference_ids
                or any(not isinstance(item, str) or not item for item in reference_ids)
                or len(set(reference_ids)) != len(reference_ids)
                or [item for item in reference_ids if item in base] != base
                or slot_id in reference_ids):
            raise ValueError("correction references must preserve ordered base sources without duplicates or self-reference")
        if any(_source_depends_on_slot(pack, source_id, slot_id) for source_id in reference_ids if source_id not in base):
            raise ValueError("correction reference transitively depends on the corrected slot")
        provenance["reference_ids"] = deepcopy(reference_ids)
    suffix = _digest({"source": source["sha256"], "review": observation["review"], "changes": changes, **provenance})[:16]
    output = Path(pack["prompt_pack"][slot_id]["output_path"])
    return {"slot_id": slot_id, "source_sha256": source["sha256"], "review_id": observation["review"]["review_id"],
        "failed_predicates": failed, "changes": changes, "output_path": str(output.with_name(output.stem + "-correction-" + suffix + output.suffix)),
        "authority": "exact approved image scope and remaining canonical allowance required", **provenance}


def review_observation_transfer(project_dir):
    """Copy one current observation into project/delivery records without re-review.

    These are observations, not final certification or a master AV review.
    Consumers retain all original predicates, warning severity and reviewer.
    """
    root = Path(project_dir).resolve()
    pack = load_board_pack(root)
    current = []
    for asset_id, binding in pack["board_bindings"].items():
        if not _binding_current(root, pack, asset_id):
            continue
        by_observation = {}
        for role in _roles(pack, asset_id):
            observation = _observation(pack, asset_id, role)
            if observation:
                key = _digest(observation["review"])
                by_observation.setdefault(key, {**deepcopy(observation), "roles": []})["roles"].append(role)
        current.extend(by_observation.values())
    return {"project_observations": current, "delivery_observations": deepcopy(current)}


def _promote_motion_bindings(root, pack, *, publish=True):
    """Opt-in deterministic projection; reviews and approval flags are never invented.

    Image-only scopes do not freeze motion planning. Once any motion scope exists,
    changed bindings need the existing fresh/carried-scope workflow. Observations
    remain retained even when projection is blocked, so retry never re-reviews.
    """
    from jsonschema import Draft202012Validator
    from schemas.artifacts import load_schema
    from lib.shot_contract import (contract_digest, shot_local, required_asset_ids,
        project_review_subject, shot_review_subject)
    bridge = pack["specification"].get("motion_binding")
    if bridge is None:
        return
    manifest_path = root / "artifacts/asset_manifest.json"
    plan_path = root / "artifacts/scene_plan.json"
    if not manifest_path.exists() or not plan_path.exists():
        return
    manifest = json.loads(manifest_path.read_text())
    plan = json.loads(plan_path.read_text())
    for artifact in (manifest, plan):
        metadata = artifact.get("metadata", {})
        if any(metadata.get(k) != pack[k] for k in ("project_id", "story_revision")):
            raise ValueError("motion projection needs current package artifacts")
    template = _motion_template_for_plan(pack["specification"], plan)
    local = shot_local(template)
    ready, pending, ready_aliases = [], [], set()
    for asset in template["assets"]:
        aid, role = asset["id"], asset["role"]
        if not _binding_current(root, pack, aid) or _review_blockers(pack, aid, role):
            pending.append(aid)
            if local and not asset.get('upstream_source'):
                asset['preparation_pending'] = True
            continue
        observation = _observation(pack, aid, role)
        binding = pack["board_bindings"][aid]
        path = str(_inside(root, binding["path"]).relative_to(root))
        asset.update(path=path, sha256=binding["sha256"], review=deepcopy(observation["review"]))
        asset.pop('preparation_pending', None)
        shared = any(r["asset_id"] == aid for r in pack["specification"].get("shared_roles", []))
        users = [s["shot_id"] for s in pack["specification"]["shots"]
                 if shared or any(r["asset_id"] == aid for r in s["roles"])]
        if local:
            users = [s['id'] for s in template['shots'] if aid in required_asset_ids(template, s)]
        if not users:
            users = [pack["specification"]["shots"][0]["shot_id"]]
        for sid in users:
            alias = motion_reference_alias(pack["specification"], aid, sid)
            row = {"id": alias, "type": "image", "path": path, "scene_id": sid,
                   "source_tool": binding.get("route", "native_imagegen") if binding["origin"] == "canonical_image_import" else "provided"}
            existing = next((r for r in manifest["assets"] if r["id"] == alias), None)
            if existing and any(existing.get(k) != row[k] for k in ("type", "scene_id")):
                raise ValueError("motion asset ID collides with existing manifest asset")
            if existing:
                existing.update(row)
            else:
                manifest["assets"].append(row)
            ready_aliases.add(alias)
            source_bindings = manifest.setdefault("metadata", {}).setdefault("source_bindings", {})
            source_bindings[alias] = {**source_bindings.get(alias, {}),
                "path": binding["path"] if binding["origin"] == "reuse" else path, "sha256": binding["sha256"], "review": deepcopy(observation["review"]),
                "roles": list(observation["roles"]), "board_asset_id": aid,
                **({k: binding[k] for k in ("call_id", "route") if k in binding})}
        ready.append(aid)
    # Only the exact handoff already authored in the retained approved package is
    # propagated; board review is never transformed into motion approval.
    for aid, handoff in bridge["motion_handoffs"].items():
        endpoints = [aid, handoff.get("pinned_final_frame", {}).get("asset_id")]
        if all(value in ready_aliases for value in endpoints):
            manifest.setdefault("metadata", {}).setdefault("motion_handoffs", {})[aid] = deepcopy(handoff)
    targets = {manifest_path: manifest}
    digest = None
    if local:
        obsolete = {aid for aid, binding in manifest.get('metadata', {}).get('source_bindings', {}).items()
                    if binding.get('board_asset_id') in pending}
        manifest['assets'] = [asset for asset in manifest['assets'] if asset['id'] not in obsolete]
        for aid in obsolete:
            manifest['metadata']['source_bindings'].pop(aid, None)
        handoffs = manifest.get('metadata', {}).get('motion_handoffs', {})
        for aid in list(handoffs):
            if aid in obsolete or handoffs[aid].get('pinned_final_frame', {}).get('asset_id') in obsolete:
                handoffs.pop(aid)
    if not pending or local:
        Draft202012Validator(load_schema("shot_contract")).validate(template)
        digest = contract_digest(template)
        contract_path = root / "artifacts/shot_contract.json"
        if local and not contract_path.exists():
            # Restore real reviews only from exact immutable attempt snapshots.
            # The authored package and current board originals derive semantics;
            # a snapshot is never used to recreate authority or a receipt.
            for request_path in sorted((root / 'production_attempts').glob('*/request.json')):
                request = json.loads(request_path.read_text())
                snapshot_path = request_path.parent / 'shot_contract.json'
                if not snapshot_path.exists():
                    continue
                snapshot = json.loads(snapshot_path.read_text())
                if (request.get('project_id') != pack['project_id']
                        or request.get('story_revision') != pack['story_revision']
                        or contract_digest(snapshot) != request.get('contract_sha256')):
                    raise ValueError('retained motion snapshot has changed or belongs to another run')
                scope = request.get('scope', {})
                evidence = request.get('approval_evidence', {})
                if (scope.get('planning_mode') != 'shot_local_v1' or not evidence.get('path')
                        or not _current(root, evidence)):
                    continue
                if project_review_subject(snapshot) == project_review_subject(template):
                    template['project_review'] = deepcopy(snapshot['project_review'])
                for shot in template['shots']:
                    prior = next((s for s in snapshot['shots'] if s['id'] == shot['id']), None)
                    if prior is not None and shot_review_subject(snapshot, shot['id']) == shot_review_subject(template, shot['id']):
                        shot['review'] = deepcopy(prior['review'])
        if contract_path.exists():
            retained = json.loads(contract_path.read_text())
            if local:
                from lib import production_execution as execution
                selected = execution.load_selected_attempts(root)
                for shot in template['shots']:
                    prior = next((s for s in retained['shots'] if s['id'] == shot['id']), None)
                    observed = [item for item in (prior or {}).get('upstream', []) if set(item) != {'shot_id'}]
                    if not observed:
                        continue
                    if [item['shot_id'] for item in prior['upstream']] != [item['shot_id'] for item in shot['upstream']]:
                        raise ValueError('materialized upstream declaration differs from authored planning')
                    for item in observed:
                        current = execution._validated_upstream_binding(root, retained, shot['id'],
                            item['shot_id'], selected.get(item['shot_id']))
                        if current != item:
                            raise ValueError('materialized upstream selection is stale; explicit rebinding is required')
                        index = next(i for i, declared in enumerate(shot['upstream']) if declared['shot_id'] == item['shot_id'])
                        shot['upstream'][index] = deepcopy(item)
                digest = contract_digest(template)
            retained_digest = contract_digest(retained)
            if not local and retained_digest != digest and retained_digest != pack.get("motion_projection", {}).get("contract_digest"):
                raise ValueError("existing canonical contract differs from retained motion projection")
            if retained_digest == digest or local:
                # A semantic reviewer can bind the completed projection later;
                # replay must preserve that real verdict rather than the template.
                if not local or project_review_subject(retained) == project_review_subject(template):
                    template["project_review"] = retained["project_review"]
                reviews = {s["id"]: s.get("review") for s in retained["shots"]}
                for shot in template["shots"]:
                    if reviews[shot["id"]] is not None and (not local or
                            shot_review_subject(retained, shot['id']) == shot_review_subject(template, shot['id'])):
                        shot["review"] = reviews[shot["id"]]
        targets[contract_path] = template
    Draft202012Validator(load_schema("asset_manifest")).validate(manifest)
    # Canonical readers reject disagreeing compatibility copies. Do the same
    # before publishing any byte, rather than leaving an ambiguous project.
    for path, value in targets.items():
        legacy = root / path.name
        if legacy.exists() and json.loads(legacy.read_text()) != value:
            raise ValueError("legacy canonical motion artifact conflict: " + path.name)
    changed = {path: value for path, value in targets.items()
               if not path.exists() or json.loads(path.read_text()) != value}
    scopes_path = root / "production_scopes.json"
    scopes = json.loads(scopes_path.read_text()).get("scopes", []) if scopes_path.exists() else []
    if changed and any(scope.get("phase") != "image" and
                       (not local or scope.get('planning_mode') != 'shot_local_v1') for scope in scopes):
        raise ValueError("motion bindings are frozen by retained scopes; use existing fresh/carried-scope workflow")
    if not publish:
        return
    for path, value in changed.items():
        temporary = path.with_suffix(".json.board-tmp")
        temporary.write_text(json.dumps(value, indent=2) + "\n")
        temporary.replace(path)
    projection = {"ready_assets": ready, "pending_assets": pending,
        "contract_digest": digest,
        "needs_current_semantic_reviews": bool(digest and (template["project_review"]["subject_sha256"] != project_review_subject(template)
            or any(s.get("review", {}).get("subject_sha256") != shot_review_subject(template, s['id']) for s in template["shots"]))),
        "authority": "fresh exact canonical motion dry-run and retained scope required"}
    if pack.get("motion_projection") != projection:
        pack["motion_projection"] = projection
        _save(root, pack)
