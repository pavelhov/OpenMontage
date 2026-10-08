"""Pure OpenArt native video capability discovery and typed input binding.

No CLI calls, no network. Capabilities come from a captured ``model form`` schema joined
with the exact transport surface of the installed official CLI version. Nothing here is
model-family specific: every model/mode is described only by its own form schema.

Public contract (consumed by lib/openart_jobs.py and production governance):

    caps = mode_capabilities(form, model=..., mode=..., cli_version="0.1.1")
    bound = bind_native_inputs(inputs, caps)

Errors are ``OpenArtCLIError`` with kinds: ``control_not_in_form``, ``transport_unsupported``,
``control_invalid``, ``ambiguous_role``, ``mode_unreachable``, ``transport_unknown``.
A control or role is never dropped, renamed or silently coerced.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Optional

from tools._openart_cli import OpenArtCLIError, form_root_schema

ROLES = ("first_frame", "last_frame", "reference_image", "reference_video", "reference_audio")
BINDINGS = ("prompt", "flag", "role", "transport_unsupported")

# Exact official CLI transport surfaces, verified from `openart generate video --help` and the
# binary strings for each version. Only names listed here can ever reach the native body.
#   flags: native schema property -> CLI flag (value carried verbatim)
#   roles: canonical role -> {flag, max, modes (CLI mode the flag selects)}
#   modes: CLI-selectable modes (0.1.1 infers image2video from --image)
#   native_params_flag: generic structured params flag (absent in 0.1.1)
TRANSPORT_SURFACES: dict[str, dict] = {
    "0.1.1": {
        "prompt": "prompt",
        "flags": {"duration": "--duration", "aspectRatio": "--aspect-ratio", "resolution": "--resolution"},
        "roles": {"first_frame": {"flag": "--image", "max": 1, "modes": ("image2video",),
                                  "wire_fields": {"label": "string", "type": "string", "url": "string"},
                                  "wire_constants": {"type": "image"}}},
        "modes": ("text2video", "image2video"),
        "native_params_flag": None,
    },
}

# Canonical role -> candidate native schema property names. These are generic JSON-schema
# property names observed across the catalog, not model families. A role binds only when
# exactly one candidate exists in the form; two candidates are ambiguous and fail closed.
_ROLE_NATIVE_CANDIDATES = {
    "first_frame": ("startFrame", "firstFrame"),
    "last_frame": ("endFrame", "lastFrame"),
    "reference_image": ("visualReferences", "referenceImages", "imageReferences"),
    "reference_video": ("videoReferences", "referenceVideos", "visualReferences"),
    "reference_audio": ("audioReferences", "referenceAudios", "visualReferences"),
}

# Legacy flat aliases normalized into input_assets. Values are (role, kind, plural).
_LEGACY_ASSET_ALIASES = {
    "image_path": ("first_frame", "source_path", False),
    "image_upload_id": ("first_frame", "upload_id", False),
    "last_image_path": ("last_frame", "source_path", False),
    "end_image_upload_id": ("last_frame", "upload_id", False),
    "reference_image_paths": ("reference_image", "source_path", True),
    "reference_image_upload_ids": ("reference_image", "upload_id", True),
    "reference_video_paths": ("reference_video", "source_path", True),
    "reference_video_upload_ids": ("reference_video", "upload_id", True),
    "reference_audio_paths": ("reference_audio", "source_path", True),
    "reference_audio_upload_ids": ("reference_audio", "upload_id", True),
}
LEGACY_ASSET_KEYS = tuple(_LEGACY_ASSET_ALIASES)
# Snake-case input aliases for the transport flag params (legacy flat inputs).
FLAG_ALIASES = {"duration": "duration", "aspect_ratio": "aspectRatio", "resolution": "resolution"}
_ASSET_KEYS = {"role", "source_sha256", "source_path", "upload_id", "body_pointer"}


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def transport_surface(cli_version: str) -> dict:
    surface = TRANSPORT_SURFACES.get(cli_version)
    if surface is None:
        raise OpenArtCLIError("transport_unknown", f"no verified transport surface for CLI {cli_version!r}")
    return surface


def _role_native(props: dict, role: str) -> tuple[Optional[str], Optional[str]]:
    found = [name for name in _ROLE_NATIVE_CANDIDATES[role] if name in props]
    if len(found) > 1:
        return None, f"ambiguous native properties {found}"
    return (found[0], None) if found else (None, None)


def _spec_of(spec: Any) -> dict:
    captured = copy.deepcopy(spec)
    spec = spec if isinstance(spec, dict) else {}
    nullable = False
    inner = spec
    for key in ("anyOf", "oneOf"):
        options = spec.get(key)
        if isinstance(options, list):
            non_null = [o for o in options if not (isinstance(o, dict) and o.get("type") == "null")]
            nullable = len(non_null) != len(options)
            inner = non_null[0] if len(non_null) == 1 and isinstance(non_null[0], dict) else {"type": "union"}
    return {"type": inner.get("type"), "enum": inner.get("enum"), "const": inner.get("const"),
            "minimum": inner.get("minimum"), "maximum": inner.get("maximum"),
            "min_items": inner.get("minItems"), "max_items": inner.get("maxItems"),
            "schema": captured,
            "nullable": nullable}


def _reference_types(spec: Any) -> set[str]:
    """Only discriminated item type enums/consts establish accepted reference kinds."""
    if not isinstance(spec, dict):
        return set()
    items = spec.get("items", {})
    if not isinstance(items, dict):
        return set()
    branches = items.get("oneOf", items.get("anyOf", [items]))
    kinds = set()
    if not isinstance(branches, list):
        return kinds
    for branch in branches:
        if not isinstance(branch, dict):
            continue
        prop = branch.get("properties", {}).get("type", {})
        if isinstance(prop, dict):
            vals = prop.get("enum", [prop["const"]] if "const" in prop else [])
            if isinstance(vals, list):
                kinds.update(v for v in vals if isinstance(v, str) and v in {"image", "video", "audio"})
    return kinds


def _wire_schema_problem(spec: Any, native: str, transport: dict) -> Optional[str]:
    """Check known CLI object shape against the intact reference property schema.

    This is descriptive compatibility only. Exact preview validation still checks every
    actual value and constraint, including URL and label, before any dispatch.
    """
    if spec is True or spec == {}:
        return None
    if not isinstance(spec, dict):
        return f"CLI_wire_schema_incompatible_{native}"
    fields = transport["wire_fields"]
    for name in spec.get("required", []):
        if name not in fields:
            return f"CLI_omits_required_{native}_{name}"
    typ = spec.get("type")
    if typ is not None and typ != "object" and not (isinstance(typ, list) and "object" in typ):
        return f"CLI_wire_type_incompatible_{native}"
    props = spec.get("properties", {})
    if spec.get("additionalProperties") is False and set(fields) - set(props):
        return f"CLI_sends_disallowed_{native}_{sorted(set(fields) - set(props))[0]}"
    from tools._openart_cli import _schema_validator
    for name, expected_type in fields.items():
        prop = props.get(name, {})
        if prop is False:
            return f"CLI_wire_schema_incompatible_{native}_{name}"
        if not isinstance(prop, dict):
            continue
        declared = prop.get("type")
        if declared is not None and declared != expected_type and not (
                isinstance(declared, list) and expected_type in declared):
            return f"CLI_wire_type_incompatible_{native}_{name}"
        if name in transport["wire_constants"] and not _schema_validator(prop).is_valid(
                transport["wire_constants"][name]):
            return f"CLI_wire_value_incompatible_{native}_{name}"
    for branch in spec.get("allOf", []):
        problem = _wire_schema_problem(branch, native, transport)
        if problem:
            return problem
    for keyword in ("anyOf", "oneOf"):
        if keyword in spec:
            rows = [_wire_schema_problem(branch, native, transport) for branch in spec[keyword]]
            if any(row is None for row in rows):
                return None
            return rows[0] if rows else f"CLI_wire_schema_incompatible_{native}"
    return None


def _branch_capabilities(schema: dict, mode: str, cli_version: str, surface: dict, element_types=None) -> dict:
    props = schema["properties"]
    required = set(schema.get("required") or [])
    role_natives: dict[str, str] = {}
    roles: dict[str, dict] = {}
    for role in ROLES:
        native, problem = _role_native(props, role)
        if problem or native is None:
            roles[role] = {"native": None, "supported": False, "reason": problem or "not_in_form"}
            continue
        if native == "visualReferences":
            accepted = _reference_types(props[native])
            if not accepted and element_types is not None:
                accepted = set(element_types) & {"image", "video", "audio"}
            kind = role.removeprefix("reference_")
            if accepted and kind not in accepted:
                roles[role] = {"native": None, "supported": False, "reason": "reference_type_not_in_form"}
                continue
            if not accepted and role != "reference_image":
                roles[role] = {"native": None, "supported": False, "reason": "reference_types_unverified"}
                continue
        role_natives[native] = role
        transport = surface["roles"].get(role)
        if transport is None:
            roles[role] = {"native": native, "supported": False, "reason": "transport_unsupported"}
        elif mode not in transport["modes"]:
            roles[role] = {"native": native, "supported": False, "reason": "transport_mode_mismatch"}
        else:
            wire_problem = _wire_schema_problem(props[native], native, transport) if "wire_fields" in transport else None
            roles[role] = {"native": native, "supported": wire_problem is None, "reason": wire_problem,
                           "max": transport["max"], "flag": transport["flag"]}
            if "wire_fields" in transport:
                roles[role].update(wire_fields=dict(transport["wire_fields"]),
                                   wire_schema_compatible=wire_problem is None)
    for role, row in roles.items():
        native = row.get("native")
        if native and native in props and (role.startswith("reference_") or role == "first_frame"):
            description = _spec_of(props[native])
            if description["type"] == "array":
                row.update(min_items=description["min_items"], max_items=description["max_items"])
            if native == "visualReferences":
                row["accepted_types"] = sorted(_reference_types(props[native]) or set(element_types or []))
            if role == "reference_audio":
                item = props[native].get("items", {}) if isinstance(props[native], dict) else {}
                choices = item.get("oneOf", item.get("anyOf", [item])) if isinstance(item, dict) else []
                audio_roles = []
                for choice in choices if isinstance(choices, list) else []:
                    ap = choice.get("properties", {}) if isinstance(choice, dict) else {}
                    at = ap.get("type", {})
                    if isinstance(at, dict) and (at.get("const") == "audio" or "audio" in at.get("enum", [])):
                        ar = ap.get("audioRole")
                        if isinstance(ar, dict):
                            audio_roles.append({k: copy.deepcopy(ar[k]) for k in ("type", "enum", "const") if k in ar} |
                                               {"required": "audioRole" in choice.get("required", [])})
                row["audio_role_info"] = audio_roles or None
    params: dict[str, dict] = {}
    for name in sorted(props):
        if name == surface["prompt"]:
            binding = "prompt"
        elif name in role_natives:
            binding = "role" if roles[role_natives[name]]["supported"] else "transport_unsupported"
        elif name in surface["flags"]:
            binding = "flag"
        else:
            binding = "transport_unsupported"
        params[name] = {**_spec_of(props[name]), "required": name in required, "binding": binding}
        if name in role_natives:
            params[name]["role"] = role_natives[name]
    reasons = []
    if mode not in surface["modes"]:
        reasons.append(f"CLI {cli_version} cannot select mode {mode}")
    for name in sorted(required):
        if name not in props:
            reasons.append(f"required {name} has no schema")
        elif params[name]["binding"] == "transport_unsupported":
            reasons.append(f"required {name} is not expressible by CLI {cli_version}")
    for role, transport in surface["roles"].items():
        if mode in transport["modes"] and not roles[role]["supported"]:
            reasons.append(roles[role]["reason"] or f"CLI mode {mode} sends {role} but the form has no single property for it")
    return {"params": params, "roles": roles, "additional_properties": schema.get("additionalProperties"),
            "unreachable": bool(reasons), "unreachable_reason": "; ".join(reasons) or None}


def _merged_view(branches: list[dict]) -> tuple[dict, dict]:
    """Descriptive union view; binding always re-validates against one concrete branch."""
    params: dict[str, dict] = {}
    for idx, branch in enumerate(branches):
        for name, spec in branch["params"].items():
            row = params.setdefault(name, {**spec, "branches": []})
            if any(row.get(k) != v for k, v in spec.items()):
                row["varies"] = True
                if row["binding"] != spec["binding"]:
                    row["binding"] = "transport_unsupported"
            row["branches"].append(idx)
    roles = {}
    for role in ROLES:
        rows = [b["roles"][role] for b in branches if not b["unreachable"]]
        supported = [r for r in rows if r["supported"]]
        roles[role] = dict(supported[0]) if supported else dict(
            (rows or [branches[0]["roles"][role]])[0])
        roles[role]["branches"] = [i for i, b in enumerate(branches) if b["roles"][role]["supported"]]
    return params, roles


def mode_capabilities(form: Any, *, model: str, mode: str, cli_version: str = "0.1.1",
                      element_types: Optional[list[str]] = None) -> dict:
    """Describe exactly which native controls/roles of one model/mode the CLI can carry.

    Root ``anyOf``/``oneOf`` forms are evaluated per branch; ``params``/``roles`` are then a
    descriptive merge, and binding must select exactly one reachable branch.
    """
    root = form_root_schema(form, model=model, mode=mode)
    surface = transport_surface(cli_version)
    branches = [_branch_capabilities(b, mode, cli_version, surface, element_types) for b in root["branches"]]
    if root["union"] is None:
        params, roles = branches[0]["params"], branches[0]["roles"]
        unreachable, reason = branches[0]["unreachable"], branches[0]["unreachable_reason"]
        schema_digest = _sha(root["branches"][0])
    else:
        params, roles = _merged_view(branches)
        unreachable = all(b["unreachable"] for b in branches)
        reason = "; ".join(f"branch {i}: {b['unreachable_reason']}" for i, b in enumerate(branches)
                           if b["unreachable"]) or None
        schema_digest = _sha({root["union"]: root["branches"]})
    caps = {
        "model": model, "mode": mode, "cli_version": cli_version, "form_sha256": schema_digest,
        "union": root["union"], "branches": branches if root["union"] else None,
        "params": params, "roles": roles,
        "native_params_supported": surface["native_params_flag"] is not None,
        "unreachable": unreachable, "unreachable_reason": reason,
    }
    caps["capabilities_sha256"] = _sha(caps)
    return caps


def public_capabilities(caps: dict) -> dict:
    """Describe structure without exposing captured prompts, reference URLs or defaults."""
    safe_values = {"duration", "aspectRatio", "resolution", "audioRole"}
    def scrub(value, parameter=None):
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                if key == "params":
                    out[key] = {n: scrub(v, n) for n, v in item.items()}
                elif key == "audio_role_info":
                    out[key] = scrub(item, "audioRole")
                elif key == "schema":
                    out["schema_sha256"] = _sha(item)
                elif key in {"default", "examples", "example", "description", "title"} or (
                        key in {"enum", "const"} and parameter not in safe_values):
                    if item is not None:
                        out[key + "_sha256"] = _sha(item)
                else:
                    out[key] = scrub(item, parameter)
            return out
        if isinstance(value, list):
            return [scrub(v, parameter) for v in value]
        return value
    return scrub(caps)


def verify_capabilities(caps: Any) -> dict:
    if not isinstance(caps, dict) or caps.get("capabilities_sha256") != _sha(
            {k: v for k, v in caps.items() if k != "capabilities_sha256"}):
        raise OpenArtCLIError("control_invalid", "capabilities digest mismatch")
    return caps


_TYPE_CHECKS = {
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
}


def _check_value(name: str, spec: dict, value: Any) -> None:
    from tools._openart_cli import _schema_validator
    if not _schema_validator(spec["schema"]).is_valid(value):
        raise OpenArtCLIError("control_invalid", f"{name} violates its captured parameter schema")


def _same_json_value(left: Any, right: Any) -> bool:
    """Exact JSON type and value agreement; Python bool/int and int/float equality is unsafe."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same_json_value(left[k], right[k]) for k in left)
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(_same_json_value(a, b) for a, b in zip(left, right))
    return left == right


def normalize_input_assets(inputs: dict) -> list[dict]:
    """Merge canonical ``input_assets`` with legacy aliases; any disagreement fails closed."""
    raw = inputs.get("input_assets", [])
    if not isinstance(raw, list):
        raise OpenArtCLIError("control_invalid", "input_assets must be a list")
    assets: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) - _ASSET_KEYS or entry.get("role") not in ROLES:
            raise OpenArtCLIError("control_invalid", "input_assets entry has unknown role or keys")
        assets.append(dict(entry))
    legacy: dict[str, dict[str, list]] = {}
    for key, (role, field, plural) in _LEGACY_ASSET_ALIASES.items():
        if key not in inputs:
            continue
        values = inputs[key] if plural else [inputs[key]]
        if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
            raise OpenArtCLIError("control_invalid", f"{key} must hold non-empty string(s)")
        legacy.setdefault(role, {})[field] = values
    for role, fields in legacy.items():
        count = {len(v) for v in fields.values()}
        if len(count) > 1:
            raise OpenArtCLIError("ambiguous_role", f"{role} legacy paths and upload ids differ in count")
        rows = [{"role": role, **{f: vals[i] for f, vals in fields.items()}} for i in range(count.pop())]
        canonical = [a for a in assets if a["role"] == role]
        if not canonical:
            assets.extend(rows)
            continue
        if len(rows) != len(canonical):
            raise OpenArtCLIError("ambiguous_role", f"{role} legacy alias count disagrees with input_assets")
        for row, have in zip(rows, canonical):
            if any(have.get(k) is not None and not _same_json_value(have[k], v) for k, v in row.items()):
                raise OpenArtCLIError("ambiguous_role", f"{role} legacy alias disagrees with input_assets")
            have.update(row)
    return assets


_ERROR_RANK = ("control_invalid", "ambiguous_role", "transport_unsupported", "control_not_in_form",
               "mode_unreachable")


def bind_native_inputs(inputs: dict, caps: dict) -> dict:
    """Bind canonical inputs to one verified model/mode capability set.

    Returns {controls, input_assets, native_params, expected_params, capabilities_sha256,
    union_branch}. ``expected_params`` is the exact dry-run ``params`` the CLI flags must
    produce, excluding role-bound upload URLs (only the caller may resolve a guarded upload
    URL). Union forms must match exactly one reachable branch; zero -> that branch's most
    specific error, several -> ``ambiguous_role`` (never a guessed branch).
    """
    caps = verify_capabilities(caps)
    if not isinstance(inputs, dict):
        raise OpenArtCLIError("control_invalid", "inputs must be an object")
    if inputs.get("model", caps["model"]) != caps["model"] or inputs.get("mode", caps["mode"]) != caps["mode"]:
        raise OpenArtCLIError("control_invalid", "model/mode differ from capabilities")
    if not caps.get("union"):
        bound, branch = _bind_branch(inputs, caps), None
    else:
        matches, errors = [], []
        for idx, branch_caps in enumerate(caps["branches"]):
            try:
                matches.append((idx, _bind_branch(inputs, {**caps, **branch_caps})))
            except OpenArtCLIError as exc:
                errors.append(exc)
        if not matches:
            reachable = [e for e in errors if e.kind != "mode_unreachable"] or errors
            best = min(reachable, key=lambda e: _ERROR_RANK.index(e.kind) if e.kind in _ERROR_RANK else 99)
            raise OpenArtCLIError(best.kind, f"no {caps['union']} branch accepts the inputs: {best.message}")
        if len(matches) > 1:
            raise OpenArtCLIError("ambiguous_role", f"inputs match {caps['union']} branches "
                                  f"{[m[0] for m in matches]}; set a discriminating native param")
        branch, bound = matches[0]
    bound["capabilities_sha256"] = caps["capabilities_sha256"]
    bound["union_branch"] = branch
    return bound


def _bind_branch(inputs: dict, caps: dict) -> dict:
    """Bind canonical inputs to one verified model/mode capability set.

    Returns {controls, input_assets, native_params, expected_params, capabilities_sha256}.
    ``expected_params`` is the exact dry-run ``params`` the CLI flags must produce, excluding
    role-bound upload URLs: only the caller may resolve a guarded upload URL.
    """
    if caps["unreachable"]:
        raise OpenArtCLIError("mode_unreachable", caps["unreachable_reason"] or "mode unreachable")
    params = caps["params"]
    native = inputs.get("native_params", {})
    if not isinstance(native, dict):
        raise OpenArtCLIError("control_invalid", "native_params must be an object")
    native = dict(native)
    for alias, name in FLAG_ALIASES.items():
        if inputs.get(alias) is None:
            continue
        if name in native and not _same_json_value(native[name], inputs[alias]):
            raise OpenArtCLIError("ambiguous_role", f"{alias} disagrees with native_params.{name}")
        native[name] = inputs[alias]
    if "prompt" in native:
        raise OpenArtCLIError("ambiguous_role", "prompt is a top-level input, not a native param")
    for name, value in native.items():
        spec = params.get(name)
        if spec is None:
            raise OpenArtCLIError("control_not_in_form", f"{name} is not in the {caps['model']}/{caps['mode']} form")
        if spec["binding"] == "role" or any(r["native"] == name for r in caps["roles"].values()):
            raise OpenArtCLIError("ambiguous_role", f"{name} is a media role; use input_assets")
        if spec["binding"] != "flag":
            raise OpenArtCLIError("transport_unsupported",
                                  f"{name} exists in the form but CLI {caps['cli_version']} cannot carry it")
        _check_value(name, spec, value)
    prompt = inputs.get("prompt")
    if "prompt" in params:
        if prompt is not None or params["prompt"]["required"]:
            if not isinstance(prompt, str) or not prompt.strip():
                raise OpenArtCLIError("control_invalid", "prompt must be a non-empty string")
            _check_value("prompt", params["prompt"], prompt)
    elif prompt is not None:
        raise OpenArtCLIError("control_not_in_form", "prompt is not in the form")
    by_role: dict[str, list] = {}
    for asset in normalize_input_assets(inputs):
        by_role.setdefault(asset["role"], []).append(asset)
    bound_assets = []
    for role in ROLES:
        rows = by_role.get(role)
        if not rows:
            continue
        cap = caps["roles"][role]
        if cap["native"] is None:
            raise OpenArtCLIError("control_not_in_form", f"{role} has no native property ({cap['reason']})")
        if not cap["supported"]:
            raise OpenArtCLIError("transport_unsupported",
                                  f"{role} -> {cap['native']} exists but CLI {caps['cli_version']} cannot carry it")
        if len(rows) > cap["max"]:
            raise OpenArtCLIError("ambiguous_role", f"{role} accepts at most {cap['max']} asset(s)")
        for row in rows:
            if not row.get("upload_id") or not (row.get("source_path") or row.get("source_sha256")):
                raise OpenArtCLIError("control_invalid", f"{role} needs upload_id and a source path or hash")
            bound_assets.append({**row, "native": cap["native"]})
    for name, spec in params.items():
        if spec["required"] and not ((name == "prompt" and prompt) or name in native
                                     or (spec["binding"] == "role" and spec.get("role") in by_role)):
            raise OpenArtCLIError("control_invalid", f"required native {name} is missing")
    controls = {"prompt": prompt, "model": caps["model"], "mode": caps["mode"]}
    for alias, name in FLAG_ALIASES.items():
        controls[alias] = native.get(name)
    expected = dict(native)
    if prompt is not None:
        expected["prompt"] = prompt
    return {"controls": controls, "input_assets": bound_assets, "native_params": native,
            "expected_params": expected}
