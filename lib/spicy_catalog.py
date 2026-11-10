"""Spicy native route catalog, input validation and explicit account observation.

The static route table defines the application-supported subset of the public,
anonymous catalog controls for the routes OpenMontage supports. It never proves account entitlement: readiness
comes only from an explicit, authenticated ``observe_account`` probe retained
under the project's ``artifacts/`` directory and bound to a credential digest.

Video is the only callable OpenMontage Spicy route. Image routes are listed
as diligence metadata only; no OpenMontage tool dispatches them.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VIDEO_MODEL = "bytedance/seedance-2.0-fast/image-to-video"
VIDEO_MODE = "image_to_video"
OBSERVATION_VERSION = "openmontage.spicy.account_observation.v1"
# A retained authenticated observation proves readiness for at most one hour.
# Older, future-dated or unparsable observations fail closed; nothing refreshes
# automatically -- callers rerun ``observe_account`` explicitly.
OBSERVATION_TTL_SECONDS = 3600
OBSERVATION_CLOCK_SKEW_SECONDS = 60

_ASPECTS = ["16:9", "9:16", "4:3", "3:4", "1:1", "21:9", "adaptive"]

# Public schema snapshot (anonymous catalog, 2026-10-21). Not account proof.
VIDEO_ROUTES: dict[str, dict[str, Any]] = {
    VIDEO_MODEL: {
        "mode": VIDEO_MODE,
        "modality": "video",
        "task": "image-to-video",
        "required": ["prompt", "image_url"],
        "properties": {
            "prompt": {"type": "string", "minLength": 1, "maxLength": 4000},
            "image_url": {"type": "string"},
            "last_image_url": {"type": "string"},
            "duration_seconds": {"type": "integer", "enum": list(range(4, 16))},
            "resolution": {"type": "string", "enum": ["480p", "720p"]},
            "aspect_ratio": {"type": "string", "enum": list(_ASPECTS)},
            "generate_audio": {"type": "boolean"},
        },
        # Only the publicly documented default. OpenMontage always sends every
        # control explicitly; generate_audio has no engine-chosen default.
        "defaults": {"aspect_ratio": "adaptive"},
        # Fields OpenMontage always submits (last_image_url is optional).
        "always_sent": ["prompt", "image_url", "duration_seconds", "resolution",
                        "aspect_ratio", "generate_audio"],
        "public_source": "https://api.spicyapi.ai/console/v1/catalog/models/bytedance/seedance-2.0-fast/image-to-video?locale=en",
    },
}

# Diligence-only metadata. There is no OpenMontage Spicy project-image route.
IMAGE_ROUTES_DILIGENCE_ONLY: dict[str, dict[str, Any]] = {
    "bytedance/seedream-5.0-pro/text-to-image": {
        "required": ["prompt"],
        "properties": {"prompt": {"type": "string"},
                       "resolution": {"type": "string", "enum": ["1k", "1.5k", "2k"]}},
        "callable_in_openmontage": False,
    },
    "bytedance/seedream-5.0-pro/edit": {
        "required": ["prompt", "image_urls"],
        "properties": {"prompt": {"type": "string"},
                       "image_urls": {"type": "array", "minItems": 1, "maxItems": 10}},
        "callable_in_openmontage": False,
    },
}

_SPICY_URI = re.compile(r"^spicy://[A-Za-z0-9._~/\-]+$")
_PLACEHOLDER = re.compile(r"^\$upload:[0-9a-f]{64}$")


class SpicyCatalogError(ValueError):
    """Sanitized catalog/validation failure."""


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def route(model: str) -> dict[str, Any]:
    if model not in VIDEO_ROUTES:
        raise SpicyCatalogError("Spicy model is not a supported OpenMontage video route")
    return VIDEO_ROUTES[model]


def validate_native_input(model: str, payload: dict[str, Any], *, allow_placeholders: bool = False) -> None:
    """Raise unless ``payload`` fits the application-supported native control subset."""
    spec = route(model)
    if not isinstance(payload, dict):
        raise SpicyCatalogError("Spicy native input must be an object")
    props = spec["properties"]
    unknown = set(payload) - set(props)
    if unknown:
        raise SpicyCatalogError("Spicy native input has unsupported fields: " + ", ".join(sorted(unknown)))
    for key in spec["required"]:
        if key not in payload:
            raise SpicyCatalogError("Spicy native input omits required field: " + key)
    for key, value in payload.items():
        rule = props[key]
        kind = rule["type"]
        if kind == "string" and not isinstance(value, str):
            raise SpicyCatalogError(f"Spicy field {key} must be a string")
        if kind == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
            raise SpicyCatalogError(f"Spicy field {key} must be an integer")
        if kind == "boolean" and type(value) is not bool:
            raise SpicyCatalogError(f"Spicy field {key} must be a boolean")
        if "enum" in rule and value not in rule["enum"]:
            raise SpicyCatalogError(f"Spicy field {key} is outside the supported values")
        if kind == "string" and "minLength" in rule and not rule["minLength"] <= len(value) <= rule["maxLength"]:
            raise SpicyCatalogError(f"Spicy field {key} length is outside the supported range")
    for key in ("image_url", "last_image_url"):
        if key in payload:
            value = payload[key]
            if not (_SPICY_URI.match(value) or (allow_placeholders and _PLACEHOLDER.match(value))):
                raise SpicyCatalogError(f"Spicy field {key} must be a committed spicy:// upload URI")


def _describe_video_inputs_v1(model: str | None = None, mode: str | None = None,
                          operation: str | None = None) -> dict[str, Any]:
    """Static descriptor of one exact route. Never claims account readiness."""
    def role(supported=False, required=False, native_field=None, minimum=0, maximum=0, timing=None):
        return {"supported": supported, "required": required, "native_field": native_field,
                "minimum": minimum, "maximum": maximum, "maximum_status": "known", "timing": timing}
    result = {"provider": "spicy", "tool": "spicy_video", "model": model, "model_policy": "exact",
              "mode": mode, "operation": operation, "status": "unavailable",
              "roles": {name: role() for name in ("start_frame", "end_frame", "timed_keyframe", "reference_image")},
              "settings": {}, "branches": [], "root_schema": None,
              "readiness": {"account": "unproven", "source": "public_catalog_snapshot",
                            "requires": "explicit authenticated account observation"},
              "provenance": {}, "blockers": [], "authority": "none"}
    if model not in VIDEO_ROUTES:
        result["blockers"].append({"code": "unsupported_model", "message": "Spicy model is not a supported video route"})
        return result
    spec = VIDEO_ROUTES[model]
    if (mode or VIDEO_MODE) != VIDEO_MODE or operation not in (None, VIDEO_MODE, "first_last_frame"):
        result["blockers"].append({"code": "unsupported_mode", "message": "Spicy route supports image_to_video only"})
        return result
    result.update(status="available", mode=VIDEO_MODE, operation=operation or VIDEO_MODE)
    result["roles"]["start_frame"] = role(True, True, "image_url", 1, 1)
    end_required = operation == "first_last_frame"
    result["roles"]["end_frame"] = role(True, end_required, "last_image_url", int(end_required), 1)
    props = spec["properties"]
    result["settings"] = {"duration": copy.deepcopy(props["duration_seconds"]),
                          "resolution": copy.deepcopy(props["resolution"]),
                          "aspect_ratio": copy.deepcopy(props["aspect_ratio"]),
                          "generate_audio": copy.deepcopy(props["generate_audio"])}
    result["root_schema"] = {"type": "object", "required": list(spec["required"]),
                             "properties": copy.deepcopy(props), "additionalProperties": False}
    if end_required:
        result["root_schema"]["required"].append("last_image_url")
    result["provenance"] = {"source": "public_catalog_snapshot", "url": spec["public_source"],
                            "schema_sha256": digest(result["root_schema"])}
    return result


# ---------------------------------------------------------------------------
# Explicit authenticated account observation


def _slug(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", model)


def observation_path(project_dir, model: str) -> Path:
    return Path(project_dir).resolve() / "artifacts" / f"spicy_account_observation-{_slug(model)}.json"


def _fsync_dir(directory: Path) -> None:
    """Require directory durability; a failed sync must stop callers before HTTP."""
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_json(path: Path, value: Any, *, exclusive: bool = False) -> None:
    """Durably write JSON: file fsync, atomic publish, then directory fsync."""
    path = Path(path)
    created = []
    probe = path.parent
    while not probe.exists():
        created.append(probe)
        probe = probe.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    for directory in reversed(created):
        _fsync_dir(directory.parent)
    fd, temp = tempfile.mkstemp(prefix=".spicy-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        if exclusive:
            os.link(temp, path)  # raises FileExistsError; never replaces
            os.unlink(temp)
        else:
            os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


# JSON Schema annotations do not restrict accepted instances. Vendor UI metadata
# is intentionally permitted only under its documented x-* extension namespace.
_SCHEMA_ANNOTATIONS = {"title", "description", "default", "examples", "$comment",
                       "$schema", "$id", "$anchor", "$defs", "definitions",
                       "readOnly", "writeOnly", "deprecated"}
# The source uploader accepts exactly these formats (lib.spicy_uploads._mime).
SOURCE_UPLOAD_MIMES = frozenset({"image/png", "image/jpeg", "image/webp"})
_KNOWN_SOURCE_MEDIA_TYPES = SOURCE_UPLOAD_MIMES | {"image/gif"}


def _source_media_compatible(schema: dict[str, Any]) -> bool:
    """Conservatively check the provider's source-media declaration.

    JSON Schema 2020-12 contentMediaType is an annotation with a string value.
    Spicy's observed source fields instead use a nonstandard MIME list. Accept
    only known image declarations covering every format our uploader can emit;
    never infer broader upload support or ignore an unknown content keyword.
    """
    if "contentMediaType" not in schema:
        return True
    declared = schema["contentMediaType"]
    types = [declared] if isinstance(declared, str) else declared
    if (not isinstance(types, list) or not types
            or any(not isinstance(value, str) or value not in _KNOWN_SOURCE_MEDIA_TYPES
                   for value in types)):
        return False
    return SOURCE_UPLOAD_MIMES <= set(types)


def _handled_keywords(schema: dict[str, Any], assertions: set[str]) -> bool:
    return all(isinstance(key, str) and
               (key in assertions or key in _SCHEMA_ANNOTATIONS or key.startswith("x-"))
               for key in schema)


def _static_bounds(rule: dict[str, Any]) -> tuple[Any, Any]:
    if "enum" in rule:
        values = rule["enum"]
        if rule["type"] == "string":
            return min(map(len, values)), max(map(len, values))
        if rule["type"] == "integer":
            return min(values), max(values)
    if rule["type"] == "string":
        if "minLength" in rule:
            return rule["minLength"], rule["maxLength"]
        # Source controls accept committed spicy:// URIs, with no local upper
        # length bound. A finite authenticated maxLength cannot prove coverage.
        return len("spicy://x"), None
    return None, None


def _covers_bound(schema, keyword, bound, *, lower=False, integer=False):
    if keyword not in schema:
        return True
    value = schema[keyword]
    if type(value) not in ((int,) if integer else (int, float)):
        return False
    import math
    if (type(value) is float and not math.isfinite(value)) or (integer and value < 0) or bound is None:
        return False
    return value <= bound if lower else value >= bound


def schema_compatible(model: str, schema: Any) -> bool:
    """Prove the authenticated schema admits the complete supported envelope.

    Known wider constraints and annotation/UI metadata are allowed. Unhandled
    assertion keywords fail closed rather than silently overstating readiness.
    This is deliberately narrower than a general JSON Schema implication solver.
    """
    spec = VIDEO_ROUTES.get(model)
    if not isinstance(schema, dict) or spec is None:
        return False
    root_keywords = {"type", "properties", "required", "additionalProperties",
                     "unevaluatedProperties", "minProperties", "maxProperties"}
    if not _handled_keywords(schema, root_keywords) or schema.get("type", "object") != "object":
        return False
    props = schema.get("properties")
    if not isinstance(props, dict):
        return False
    # All emitted fields are explicitly declared below, so neither additional
    # nor unevaluated property policies can restrict those field instances.
    for key in ("additionalProperties", "unevaluatedProperties"):
        if key in schema and not isinstance(schema[key], (bool, dict)):
            return False
    required = schema.get("required", [])
    if (not isinstance(required, list) or any(not isinstance(key, str) for key in required)
            or not set(required) <= set(spec["always_sent"])):
        return False
    if (not _covers_bound(schema, "minProperties", len(spec["always_sent"]), lower=True, integer=True)
            or not _covers_bound(schema, "maxProperties", len(spec["properties"]), integer=True)):
        return False
    for key, rule in spec["properties"].items():
        live = props.get(key)
        kind = rule["type"]
        assertions = {"type", "enum"}
        if kind == "string":
            assertions.update(("minLength", "maxLength"))
            if key in ("image_url", "last_image_url"):
                # This is targeted provider media compatibility, not treating
                # arbitrary content vocabulary as JSON Schema assertions.
                assertions.add("contentMediaType")
        elif kind == "integer":
            assertions.update(("minimum", "maximum"))
        if not isinstance(live, dict) or not _handled_keywords(live, assertions):
            return False
        if key in ("image_url", "last_image_url") and not _source_media_compatible(live):
            return False
        if live.get("type") != kind:
            return False
        if "enum" in live:
            enum = live["enum"]
            if not isinstance(enum, list) or not enum:
                return False
            admitted = {json.dumps(value, sort_keys=True) for value in enum}
            emitted = rule.get("enum", [True, False] if kind == "boolean" else None)
            if emitted is None or not {json.dumps(value) for value in emitted} <= admitted:
                return False
        low, high = _static_bounds(rule)
        if kind == "integer":
            if (not _covers_bound(live, "minimum", low, lower=True)
                    or not _covers_bound(live, "maximum", high)):
                return False
        elif kind == "string":
            if (not _covers_bound(live, "minLength", low, lower=True, integer=True)
                    or not _covers_bound(live, "maxLength", high, integer=True)):
                return False
    return True


def observe_account(project_dir, client, model: str) -> dict[str, Any]:
    """Explicit live probe: authenticated model listing for this credential.

    Read-only, one GET. Records the credential digest (never the key), the
    exact returned schema digest and enablement. Callers invoke explicitly.
    """
    route(model)
    response = client.list_models(modality="video", include_schema=True)
    data = response.get("data") if isinstance(response, dict) else None
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise SpicyCatalogError("Spicy model listing response is malformed")
    item = next((i for i in items if isinstance(i, dict) and i.get("model") == model), None)
    schema = item.get("inputSchema") if item else None
    observation = {
        "version": OBSERVATION_VERSION, "model": model, "authenticated": True,
        "observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "listed": item is not None,
        "enabled": bool(item and item.get("enabled") is True),
        "available": bool(item and item.get("available") is True),
        "input_schema": copy.deepcopy(schema),
        "schema_sha256": digest(schema) if schema is not None else None,
        "schema_compatible": schema_compatible(model, schema),
        "credential_binding": {k: client.binding[k] for k in ("credential_ref", "credential_env", "credential_digest")},
        "request_id": response.get("request_id"),
    }
    atomic_write_json(observation_path(project_dir, model), observation)
    return observation


def load_account_observation(project_dir, model: str) -> dict[str, Any] | None:
    """Return a retained observation only if it proves an enabled, compatible route."""
    try:
        observation = json.loads(observation_path(project_dir, model).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(observation, dict) or observation.get("version") != OBSERVATION_VERSION:
        return None
    binding = observation.get("credential_binding")
    if (observation.get("model") != model or observation.get("authenticated") is not True
            or observation.get("enabled") is not True or observation.get("available") is not True
            or not isinstance(binding, dict) or not isinstance(binding.get("credential_digest"), str)
            or observation.get("input_schema") is None
            or observation.get("schema_sha256") != digest(observation["input_schema"])
            or not schema_compatible(model, observation["input_schema"])):
        return None
    observed = parse_utc(observation.get("observed_at"))
    if observed is None:
        return None
    age = (datetime.now(timezone.utc) - observed).total_seconds()
    if age < -OBSERVATION_CLOCK_SKEW_SECONDS or age > OBSERVATION_TTL_SECONDS:
        return None
    return observation


_UTC_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?(Z|\+00:00)")


def parse_utc(value: Any) -> datetime | None:
    """Parse a strict UTC ISO stamp (``...Z`` or ``+00:00``); None if invalid."""
    if not isinstance(value, str) or not _UTC_STAMP.fullmatch(value):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def utc_now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


OBSERVATION_VERSION_V2 = 'openmontage.spicy.account_observation.v2'


def observation_path_v2(project_dir, model):
    return Path(project_dir).resolve() / 'artifacts' / ('spicy_account_observation_v2-' + digest(model) + '.json')


def observe_account_v2(project_dir, client, model, credential_ref=None):
    """Explicit authenticated exact-model/balance read, bound to this project and key."""
    from lib.spicy_routes import qualify_route
    if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,255}', model) or '..' in model.split('/'):
        raise SpicyCatalogError('Spicy exact model identifier is invalid')
    binding = {key: client.binding[key] for key in ('credential_ref', 'credential_digest')}
    if credential_ref is not None and credential_ref != binding['credential_ref']:
        raise SpicyCatalogError('Spicy observation client credential differs')
    response = client.get_model(model)
    item = response.get('data') if isinstance(response, dict) else None
    if not isinstance(item, dict) or item.get('model') != model:
        raise SpicyCatalogError('Spicy exact model observation is malformed')
    balance_response = client.get_balance()
    balance = balance_response.get('data') if isinstance(balance_response, dict) else None
    if not isinstance(balance, dict) or any(not isinstance(balance.get(key), str) or not re.fullmatch(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?', balance[key]) for key in ('available', 'held', 'total')):
        raise SpicyCatalogError('Spicy account balance observation is malformed')
    if balance['held'].startswith('-'):
        raise SpicyCatalogError('Spicy held balance cannot be negative')
    if (Path(project_dir).resolve() / 'artifacts').is_symlink():
        raise SpicyCatalogError('Spicy observation artifacts directory must not be a symlink')
    qualification = qualify_route(item)
    record = {'version': OBSERVATION_VERSION_V2, 'model': model, 'authenticated': True,
        'observed_at': utc_now_stamp(), 'project_binding': digest(str(Path(project_dir).resolve())),
        'listed': True, 'enabled': item.get('enabled') is True, 'available': item.get('available') is True,
        'input_schema': copy.deepcopy(item.get('inputSchema')), 'schema_sha256': qualification['schema_sha256'],
        'task': qualification['task'], 'mode': qualification['mode'], 'modality': item.get('modality'),
        'credential_binding': binding, 'qualification': qualification,
        'balance': {key: balance[key] for key in ('available', 'held', 'total')},
        'request_id': response.get('request_id')}
    record['observation_sha256'] = digest(record)
    atomic_write_json(observation_path_v2(project_dir, model), record)
    return copy.deepcopy(record)


def load_account_observation_v2(project_dir, model, credential_ref):
    """A stale corpus or another project/key never establishes paid eligibility."""
    from lib.spicy_jobs import credential_binding, SpicyError
    from lib.spicy_routes import qualify_route
    try:
        path = observation_path_v2(project_dir, model)
        if path.is_symlink() or path.parent.is_symlink():
            return None
        value = json.loads(path.read_text(encoding='utf-8'))
        current = credential_binding(credential_ref)
        binding = {key: current[key] for key in ('credential_ref', 'credential_digest')}
        if (not isinstance(value, dict) or value.get('version') != OBSERVATION_VERSION_V2
            or value.get('model') != model or value.get('authenticated') is not True
            or value.get('project_binding') != digest(str(Path(project_dir).resolve()))
            or value.get('credential_binding') != binding
            or value.get('enabled') is not True or value.get('available') is not True
            or value.get('schema_sha256') != digest(value.get('input_schema'))
            or value.get('observation_sha256') != digest({k:v for k,v in value.items() if k != 'observation_sha256'})):
            return None
        age = (datetime.now(timezone.utc) - parse_utc(value.get('observed_at'))).total_seconds()
        if not -OBSERVATION_CLOCK_SKEW_SECONDS <= age <= OBSERVATION_TTL_SECONDS:
            return None
        qualified = qualify_route({'model': model, 'modality': value.get('modality'), 'tasks': [value.get('task')],
            'enabled': True, 'available': True, 'inputSchema': value['input_schema']})
        if qualified['status'] == 'blocked' or qualified != value.get('qualification'):
            return None
        return value
    except (OSError, ValueError, TypeError, KeyError, SpicyError):
        return None


def describe_video_inputs(model=None, mode=None, operation=None, *, project_dir=None,
                          credential_ref=None, observation=None):
    """Legacy static descriptor or exact observed v2 schema, without refreshing account state."""
    if project_dir is None and observation is None:
        return _describe_video_inputs_v1(model, mode, operation)
    from lib.spicy_routes import qualify_route
    observed = observation if observation is not None else load_account_observation_v2(project_dir, model, credential_ref)
    if not isinstance(observed, dict) or observed.get('model') != model:
        return {'status': 'blocked', 'media': [], 'controls': [], 'blocked_controls': [],
            'blockers': [{'code': 'account_observation_missing', 'message': 'fresh exact account/project schema observation is required'}]}
    result = qualify_route({'model': model, 'modality': observed.get('modality', 'video'),
        'tasks': [observed.get('task')], 'enabled': observed.get('enabled'), 'available': observed.get('available'),
        'inputSchema': observed.get('input_schema')})
    if mode not in (None, result['mode']) or operation not in (None, result['mode'], result['task']):
        result['status'] = 'blocked'
        result['blockers'].append({'code': 'mode_mismatch', 'message': 'requested mode/operation differs from exact native task'})
    result['blocked_controls'] = copy.deepcopy(result['links'])
    result['root_schema'] = copy.deepcopy(observed.get('input_schema'))
    return result
