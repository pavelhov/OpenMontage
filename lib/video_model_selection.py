"""Read-only, bounded model-intent planning; dispatch remains an exact tool pin.

Registry capabilities are evidence of control fit, not comparative video quality.
No provider/model defaults or prices are introduced here. The result is a plan,
never generation authority, an account readiness check, or a retry instruction.
"""
from __future__ import annotations

from copy import deepcopy
import math

from tools.base_tool import ToolStatus

POOL_ITEM_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["provider"],
    "properties": {key: {"type": "string", "minLength": 1}
                   for key in ("provider", "model", "tool")},
}
MODEL_SELECTION_INTENT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["mode", "approved_pool"],
    "properties": {
        "mode": {"enum": ["exact", "prefer", "auto"]},
        "goal": {"enum": ["best_value", "max_clean", "balanced"], "default": "balanced"},
        "provider": {"type": "string", "minLength": 1},
        "model": {"type": "string", "minLength": 1},
        "approved_pool": {"type": "array", "minItems": 1, "uniqueItems": True, "items": POOL_ITEM_SCHEMA},
    },
    "allOf": [
        {"if": {"properties": {"mode": {"const": "prefer"}}}, "then": {"required": ["model"]}},
        {"if": {"properties": {"mode": {"const": "exact"}}},
         "then": {"anyOf": [{"required": ["model"]}, {"required": ["provider"]}]}},
    ],
}


def validate_model_selection_intent(intent):
    """Raise ValueError for malformed intent; return a defensive valid copy."""
    from jsonschema import Draft202012Validator
    errors = list(Draft202012Validator(MODEL_SELECTION_INTENT_SCHEMA).iter_errors(intent))
    if errors:
        raise ValueError(errors[0].message)
    if intent["mode"] in ("exact", "prefer") and not any(
        item.get("model") == intent.get("model")
        and (not intent.get("provider") or item["provider"] == intent["provider"])
        for item in intent["approved_pool"]
    ):
        raise ValueError("The instructed model/route is absent from the approved pool.")
    if intent["mode"] == "exact":
        providers = {item["provider"] for item in intent["approved_pool"]
                     if item.get("model") == intent.get("model")
                     and (not intent.get("provider") or item["provider"] == intent["provider"])}
        if len(providers) != 1:
            raise ValueError("An exact model must resolve to one explicitly approved provider.")
    return deepcopy(intent)


def selection_intent_allows_route(intent, provider, model, tool=None):
    """Boolean lock predicate for policy owners; eligibility is a separate gate.

    Managed media has model=None. Missing tool cannot satisfy a tool-specific
    pool entry. Invalid intent fails closed rather than implying authorization.
    """
    try:
        intent = validate_model_selection_intent(intent)
    except (ValueError, TypeError):
        return False
    if intent["mode"] == "exact" and (
        model != intent.get("model")
        or intent.get("provider") and provider != intent["provider"]
    ):
        return False
    return any(item["provider"] == provider and item.get("model") == model
               and (not item.get("tool") or item["tool"] == tool)
               for item in intent["approved_pool"])


def _blocked(code, message, rejected=None):
    return {"status": "blocked", "dispatch_status": "not_dispatched",
            "blockers": [{"code": code, "message": message}],
            "rejected_candidates": rejected or [], "fallback_tools": [],
            "fallback_attempted": False}


def _requested_controls(inputs):
    controls = []
    assets = inputs.get('input_assets') or []
    roles = [a.get('role') for a in assets if isinstance(a, dict)] if isinstance(assets, list) else []
    params = inputs.get('native_params') or {}
    params = params if isinstance(params, dict) else {}
    if 'last_frame' in roles:
        controls.append('first_last_frame')
    if any(params.get(k) is True for k in ('audio', 'generateAudio', 'generateSound')):
        controls.append('native_audio')
    if sum(role in {'reference_image', 'character_reference', 'environment_reference'} for role in roles) > 1:
        controls.append('multiple_reference_images')
    if any(inputs.get(k) for k in ("last_image_path", "last_image_url", "last_frame", "endpoint_requirement_id")) or inputs.get("operation") == "first_last_frame":
        controls.append("first_last_frame")
    if any(inputs.get(k) for k in ("native_audio", "generate_audio", "voices", "voice")) or inputs.get("sound") in ("on", "true", "yes"):
        controls.append("native_audio")
    refs = inputs.get("reference_image_paths") or inputs.get("reference_image_urls") or []
    if isinstance(refs, (list, tuple)) and len(refs) > 1:
        controls.append("multiple_reference_images")
    return list(dict.fromkeys(controls))


def _setting_error(spec, value, *, duration_hint=False):
    """Validate observed enums/ranges without silently changing case or settings."""
    from jsonschema import Draft202012Validator
    if duration_hint and spec.get("type") == "integer" and isinstance(value, str) and value.isdecimal():
        value = int(value)  # Same lossless conversion as the canonical selector.
    return next(Draft202012Validator(spec).iter_errors(value), None) is not None


_SELECTOR_HANDLED_KEYS = frozenset({
    "model_selection_intent", "preferred_provider", "allowed_providers", "preferred_tool",
    "hosting_provider", "target_operation", "task_context", "preferred_provider_gap",
    # The selector supplies these universal generation/stock request semantics.
    "prompt", "operation", "output_path",
})


def _validate_provider_inputs(tool, inputs, props):
    """Refuse every undeclared creative input, including open-schema routes.

    Governance keys are exempt from capability checks only because the canonical
    wrapper consumes them; this function neither validates nor grants authority.
    Aliases are legal only when the selector actually maps them on this route.
    """
    from pathlib import Path
    from lib.production_execution import GOVERNANCE_KEYS

    exempt = _SELECTOR_HANDLED_KEYS | GOVERNANCE_KEYS | {"scene_id"}
    for key, value in inputs.items():
        if value is None:
            continue
        translated_key = None
        if key == "reference_image_path" and inputs.get("operation") == "image_to_video":
            translated_key = "image_path" if "image_path" in props else "image_url" if "image_url" in props else None
            if translated_key:
                if not isinstance(value, str) or not value:
                    return "unsupported_setting:" + key
                # image_url is supplied by the established upload adapter, not
                # by treating a local path as a URL during read-only planning.
                if translated_key == "image_url" and "image_url" in inputs:
                    # The selector uploads only when image_url is absent. A
                    # supplied URL (even null) would skip mapping this path.
                    return "conflicting_alias:" + key
                if translated_key == "image_path":
                    if _setting_error(props[translated_key], value):
                        return "unsupported_setting:" + key
                    existing = inputs.get("image_path")
                    if existing is not None and (not isinstance(existing, str) or Path(existing).expanduser().resolve() != Path(value).expanduser().resolve()):
                        return "conflicting_alias:" + key
        elif key == "last_frame" and tool.provider == "grok_cli" and "last_image_path" in props:
            translated_key = "last_image_path"
            if _setting_error(props[translated_key], value):
                return "unsupported_setting:" + key
            existing = inputs.get(translated_key)
            if existing is not None and (not isinstance(value, str) or not isinstance(existing, str)
                                        or Path(existing).expanduser().resolve() != Path(value).expanduser().resolve()):
                return "conflicting_alias:" + key
        if translated_key:
            continue
        if key not in props:
            if key not in exempt:
                return "missing_controls:" + key
            continue
        # Only duration receives the selector's lossless decimal-string hint
        # conversion. A seed/fps integer field still requires an actual integer.
        if key not in GOVERNANCE_KEYS and key != "scene_id" and _setting_error(props[key], value, duration_hint=key == "duration"):
            return "unsupported_setting:" + key
    return None


def _cost(tool, info, metadata, inputs):
    # Explicit-only subscription routes must supply a request-applicable quote,
    # never an estimate_cost USD default. No private quotes are inspected here.
    # Current registry exposes no public, exact-request CLI quote evidence.
    # Catalog/form cost responses cannot price arbitrary native settings. Keep
    # both credits and subscription quota unknown instead of adding a synthetic
    # selection_cost metadata contract with no production publisher.
    if getattr(tool, "supports", {}).get("explicit_selection_only") or info.get("usd_cost_status") == "unknown":
        return {"status": "unknown", "amount": None, "unit": info.get("billing_unit"), "basis": "no_request_applicable_quote"}
    try:
        amount = tool.estimate_cost(inputs)
    except Exception:
        amount = None
    if isinstance(amount, (int, float)) and not isinstance(amount, bool) and math.isfinite(amount) and amount >= 0:
        return {"status": "estimated", "amount": amount, "unit": "USD", "basis": "provider_estimate_not_benchmark"}
    return {"status": "unknown", "amount": None, "unit": None, "basis": "estimate_unavailable"}


def _candidate(tool, item, inputs):
    info = tool.get_info()
    props = getattr(tool, "input_schema", {}).get("properties", {})
    supports = info.get("supports") or getattr(tool, "supports", {})
    model = item.get("model")
    catalog = info.get("model_catalog") or {}
    metadata = catalog.get(model, {}) if model else {}
    explicit = supports.get("explicit_selection_only") is True
    # A managed media route exposes no selectable model. An agent model string
    # in other metadata must never become a video backend.
    declared = set(catalog)
    for key in ("model", "model_id", "model_name"):
        declared.update(props.get(key, {}).get("enum", []))
    declared.update(getattr(tool, "_MODELS", {}))
    declared.update(getattr(tool, "_MODEL_ALIASES", {}))
    if model and model not in declared:
        return None, "model_mismatch"
    if not model and declared:
        return None, "exact_model_required"
    operation = inputs.get("operation", "text_to_video")
    mode_operation = "image_to_video" if tool.provider == "openart_mcp" and operation == "first_last_frame" else operation
    modes = metadata.get("modes")
    if isinstance(modes, dict):
        matching = [(mode, row) for mode, row in modes.items() if row.get("operation") == mode_operation and (not inputs.get("mode") or inputs["mode"] == mode)]
        if len(matching) != 1:
            return None, "unqualified_model_operation"
        native_mode, metadata = matching[0]
        if tool.provider == "openart_mcp":
            if metadata.get("production_ready") is not True:
                return None, "unqualified_model"
            from lib import openart_mcp as mcp
            from lib.production_execution import _openart_mcp_controls
            try:
                profile = mcp.load_profile(model, native_mode, require="supported")
                controls = deepcopy(inputs)
                controls.pop('model_selection_intent', None)
                controls.update(model=model, mode=native_mode)
                mcp.prepare_native_request(_openart_mcp_controls(controls), profile)
                # Exact form compilation above proved this request, while global
                # wrapper flags intentionally make no model-wide capability claim.
                capabilities = profile.get('native_capabilities') or {}
                role_caps, param_caps = capabilities.get('roles') or {}, capabilities.get('params') or {}
                metadata = deepcopy(metadata)
                metadata['first_last_frame'] = all(role_caps.get(role, {}).get('supported') is True for role in ('first_frame', 'last_frame'))
                metadata['native_audio'] = any(key in param_caps for key in ('audio', 'generateAudio', 'generateSound'))
                requested_roles = [a.get('role') for a in inputs.get('input_assets', []) if isinstance(a, dict)
                                   and a.get('role') in {'reference_image', 'character_reference', 'environment_reference'}]
                metadata['multiple_reference_images'] = bool(requested_roles) and all(role_caps.get(role, {}).get('supported') is True for role in requested_roles)
            except (ValueError, KeyError):
                return None, "unqualified_native_controls"
        elif metadata.get("production_ready") is not True or metadata.get("source") != "real":
            return None, "unqualified_model"
        if metadata.get("required_unqualified") or metadata.get("argv_flag_without_effective_preview"):
            return None, "unqualified_native_controls"
    elif info.get("qualification_required") or explicit and model:
        # Qualified model-selectable CLI routes publish their full profiles.
        return None, "unqualified_model"
    else:
        native_mode = None
    if metadata.get("operation") and metadata["operation"] != mode_operation:
        return None, "unsupported_operation"
    if supports.get(operation) is not True and metadata.get("operation") != mode_operation:
        return None, "unsupported_operation"
    checker = getattr(tool, "is_operation_available", None)
    if callable(checker) and not checker(operation):
        return None, "operation_unavailable"
    missing = [name for name in _requested_controls(inputs)
               if metadata.get(name, supports.get(name)) is not True]
    if missing:
        return None, "missing_controls:" + ",".join(missing)
    for key in ("last_image_path", "last_image_url", "last_frame", "keyframes", "voices"):
        if inputs.get(key) and key not in props:
            return None, "missing_controls:" + key
    frame_models = getattr(tool, "first_last_frame_models", None)
    if "first_last_frame" in _requested_controls(inputs) and frame_models is not None and model not in frame_models:
        return None, "missing_controls:model_first_last_frame"
    # Presence of references must not be lost just because the caller labels
    # the operation text-to-video. A text-only qualified mode cannot absorb an
    # image/video/audio pin via prose or another mode's global capabilities.
    reference_keys = ("image_path", "image_url", "reference_image_path", "reference_image_url", "first_frame",
                      "reference_image_paths", "reference_image_urls", "image_list", "image_upload_id")
    media_reference_keys = reference_keys + (
        "reference_video_url", "reference_video_path", "reference_video_urls", "reference_video_paths",
        "reference_audio_urls", "reference_audio_paths", "video_list", "element_list", "refers")
    if any(inputs.get(k) for k in media_reference_keys) and metadata.get("operation") == "text_to_video":
        return None, "unqualified_model_references"
    if any(inputs.get(k) for k in reference_keys) and supports.get("reference_image") is not True:
        return None, "missing_controls:reference_image"
    input_error = _validate_provider_inputs(tool, inputs, props)
    if input_error:
        return None, input_error
    for key in ("duration", "aspect_ratio", "resolution"):
        if key not in inputs:
            continue
        if tool.provider == "openart_mcp":
            continue  # Exact retained form validation above preserves JSON types.
        native_key = "aspectRatio" if key == "aspect_ratio" else key
        controls = metadata.get("native_controls")
        if isinstance(controls, dict):
            control = controls.get(native_key, {})
            if control.get("qualified_in_exact_preview") is not True:
                return None, "unqualified_native_control:" + key
            values = [v["value"] for v in control.get("enum") or [] if isinstance(v, dict) and "value" in v]
            preview = control.get("preview", {}).get("value")
            value = inputs[key]
            if key == "duration" and isinstance(value, str) and value.isdecimal():
                value = int(value)
            if values:
                if value not in values:
                    return None, "unsupported_setting:" + key
            elif preview is None or value != preview:
                return None, "unqualified_setting:" + key
        else:
            if key not in props:
                return None, "unsupported_setting:" + key
            if _setting_error(props[key], inputs[key], duration_hint=key == "duration"):
                return None, "unsupported_setting:" + key
            values = metadata.get("durations" if key == "duration" else "resolutions" if key == "resolution" else "aspect_ratios")
            value = int(inputs[key]) if key == "duration" and isinstance(inputs[key], str) and inputs[key].isdecimal() else inputs[key]
            if values and value not in values:
                return None, "unsupported_setting:" + key
    if tool.get_status() != ToolStatus.AVAILABLE:
        return None, "route_unavailable"
    request = deepcopy(inputs)
    request.pop("model_selection_intent", None)
    request.pop("target_operation", None)  # rank-only hint already consumed
    request.update(preferred_provider=tool.provider, allowed_providers=[tool.provider], preferred_tool=tool.name)
    if model:
        request["model"] = model
    if native_mode:
        request["mode"] = native_mode
    # No assertions about video cleanliness. Only observed control fit and proof.
    fit = len(_requested_controls(inputs)) + int(bool(metadata.get("result_proof_id")))
    return {"provider": tool.provider, "tool": tool.name, "model": model,
            "fit_score": fit, "fit_basis": "control_fit_and_transport_evidence_heuristic_not_quality_benchmark",
            "cost": _cost(tool, info, metadata, request), "request": request}, None


def plan_video_model_selection(inputs, tools):
    """Resolve an explicit approved pool into one canonical exact request.

    Prefer falls back only during planning. An execution failure never invokes
    this helper again or chooses a replacement. No launch/account refresh occurs.
    """
    intent = inputs.get("model_selection_intent")
    try:
        intent = validate_model_selection_intent(intent)
    except (ValueError, TypeError) as exc:
        return _blocked("invalid_model_selection_intent", str(exc))
    mode, goal = intent["mode"], intent.get("goal", "balanced")
    pool = intent["approved_pool"]
    preferred = lambda item: item.get("model") == intent.get("model") and (not intent.get("provider") or item["provider"] == intent["provider"])
    if mode in ("exact", "prefer") and not any(preferred(item) for item in pool):
        return _blocked("model_outside_approved_pool", "The instructed model is absent from the approved pool.")
    if mode == "exact":
        pool = [item for item in pool if preferred(item)]
    eligible, rejected = [], []
    for item in pool:
        matched = [tool for tool in tools if tool.provider == item["provider"] and (not item.get("tool") or tool.name == item["tool"])]
        if inputs.get("allowed_providers") and item["provider"] not in inputs["allowed_providers"]:
            rejected.append({**item, "code": "provider_outside_allowed_providers"})
            continue
        for tool in matched:
            pinned_provider = inputs.get("preferred_provider")
            if pinned_provider not in (None, "auto", item["provider"]):
                rejected.append({**item, "tool": tool.name, "code": "crossed_provider_constraints"})
                continue
            if inputs.get("preferred_tool") and inputs["preferred_tool"] != tool.name or inputs.get("hosting_provider") and inputs["hosting_provider"] != getattr(tool, "hosting_provider", tool.provider):
                rejected.append({**item, "tool": tool.name, "code": "crossed_route_constraints"})
                continue
            if inputs.get("model") and inputs["model"] != item.get("model"):
                rejected.append({**item, "tool": tool.name, "code": "crossed_exact_model_constraints"})
                continue
            try:
                row, error = _candidate(tool, item, inputs)
            except Exception:
                row, error = None, "registry_evidence_unavailable"
            if row:
                eligible.append(row)
            else:
                rejected.append({**item, "tool": tool.name, "code": error})
        if not matched:
            rejected.append({**item, "code": "route_not_found"})
    if not eligible:
        return _blocked("no_eligible_model", "No approved model fits the required controls and qualification.", rejected)
    if mode == "exact" and len(eligible) != 1:
        return _blocked("ambiguous_exact_model", "Name the provider/tool for this exact model.", rejected)
    preferred_rows = [row for row in eligible if preferred(row)] if mode == "prefer" else []
    choice_pool = preferred_rows or eligible
    if goal == "best_value" and mode != "exact" and not preferred_rows:
        costs = [row["cost"] for row in choice_pool]
        if any(cost["status"] == "unknown" for cost in costs) or len({cost["unit"] for cost in costs}) != 1:
            return _blocked("cost_not_comparable", "Best value requires applicable costs in one comparable unit; unknown cost is not zero.", rejected)
        selected = min(choice_pool, key=lambda row: (row["cost"]["amount"], -row["fit_score"]))
    elif len(choice_pool) == 1:
        selected = choice_pool[0]
    else:
        selected = max(choice_pool, key=lambda row: row["fit_score"])
    return {"status": "planned", "intent": intent, "goal": goal,
            "selected": {key: value for key, value in selected.items() if key != "request"},
            "planned_request": selected["request"],
            "eligible_candidates": [{key: value for key, value in row.items() if key != "request"} for row in eligible],
            "rejected_candidates": rejected, "fallback_tools": [],
            "limitations": ["Planning grants no dispatch or billing authority.",
                            "Control fit and transport proof do not establish cast/dialogue or comparative quality.",
                            "Tied fit uses approved pool order; no vendor ranking is assumed.",
                            "Dispatch readiness and unresolved original jobs remain governed by the selected route."]}
