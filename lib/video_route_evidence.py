"""Bounded, provenance-labeled route evidence for video planning.

Pure data plus pure lookups: no catalog refresh, no provider call, and no
generation authority.

Audio facts are separate dimensions:

* ``native_output`` - native audio by default (hosted provider documentation)
  or under an explicit exact-form switch.
* ``explicit_toggle`` - whether the exact retained form exposes an on/off param.
* ``reference_audio`` - whether the exact form accepts an audio reference role.
* ``connector_observed`` / ``dialogue_fidelity`` - never proven here.

A form without an audio field means "no exposed switch", never "no audio".
Routes absent from ``AUDIO_DEFAULT_EVIDENCE`` stay ``unknown``.
"""
from __future__ import annotations

from copy import deepcopy

AUDIO_TOGGLE_PARAMS = ("audio", "generateAudio", "generateSound")
_RETRIEVED = "2026-10-08"

_FAL_H3_MAX = {
    "url": "https://fal.ai/learn/devs/introducing-h3-max-by-fal",
    "claim": "fal states H3 Max and its Turbo variant keep the base model's natively synchronized audio and video.",
    "retrieved": _RETRIEVED,
}
_OPENART_H3 = {
    "url": "https://openart.ai/ai-model/minimax-h3",
    "claim": "OpenArt states MiniMax H3 and H3 Max generate dialogue, sound effects and ambience alongside the video in the same pass.",
    "retrieved": _RETRIEVED,
}
_FAL_TURBO_TARGET_AUDIO = {
    "url": "https://fal.ai/models/minimax/h3-max-turbo/image-to-video/api",
    "claim": "fal's own endpoint has an optional target_audio_url soundtrack pin; the OpenArt form does not expose it and it is not an audio on/off switch.",
    "retrieved": _RETRIEVED,
}

# Exact (provider, model, mode) -> hosted provider documentation of default
# native audio output. Only routes present in the observed OpenArt catalog.
AUDIO_DEFAULT_EVIDENCE = {
    ("openart_mcp", "fal-h3-max-turbo", "text2video"): [_FAL_H3_MAX, _FAL_TURBO_TARGET_AUDIO],
    ("openart_mcp", "fal-h3-max-turbo", "image2video"): [_FAL_H3_MAX, _FAL_TURBO_TARGET_AUDIO],
    ("openart_mcp", "fal-h3-max", "text2video"): [_FAL_H3_MAX, _OPENART_H3],
    ("openart_mcp", "fal-h3-max", "image2video"): [_FAL_H3_MAX, _OPENART_H3],
    ("openart_mcp", "fal-h3-max", "element2video"): [_FAL_H3_MAX, _OPENART_H3],
    ("openart_mcp", "minimax-h3", "text2video"): [_OPENART_H3],
    ("openart_mcp", "minimax-h3", "image2video"): [_OPENART_H3],
    ("openart_mcp", "minimax-h3", "element2video"): [_OPENART_H3],
}


def explicit_audio_off(native_params):
    """Toggle keys a request explicitly sets False (an audio-off control)."""
    params = native_params if isinstance(native_params, dict) else {}
    return [name for name in AUDIO_TOGGLE_PARAMS if params.get(name) is False]


def _form_default(param):
    schema = param.get("schema") if isinstance(param.get("schema"), dict) else {}
    value = schema.get("default")
    return value if isinstance(value, bool) else None


def audio_output_conflict(audio, native_params):
    """Code when a request cannot produce required native audio, else None.

    An explicit False switch always conflicts. On a ``supported_toggle`` route
    the effective value is the explicit ``native_params`` value, else the exact
    form default: default False conflicts, and an omitted switch without a
    declared default is unknown. Never injects or rewrites a setting.
    """
    if explicit_audio_off(native_params):
        return "conflicting_controls:native_audio"
    audio = audio if isinstance(audio, dict) else {}
    if audio.get("native_output") != "supported_toggle":
        return None
    params = native_params if isinstance(native_params, dict) else {}
    toggle = audio.get("explicit_toggle") if isinstance(audio.get("explicit_toggle"), dict) else {}
    defaults = toggle.get("defaults") if isinstance(toggle.get("defaults"), dict) else {}
    for name in toggle.get("native_params") or []:
        value = params[name] if name in params else defaults.get(name)
        if value is False:
            return "conflicting_controls:native_audio"
        if value is not True:
            return "missing_controls:native_audio_enabled"
    return None


def audio_capability(provider, model, mode, native_capabilities=None):
    """Pure per-route audio view; never injects a request parameter."""
    caps = native_capabilities if isinstance(native_capabilities, dict) else {}
    params = caps.get("params") if isinstance(caps.get("params"), dict) else {}
    roles = caps.get("roles") if isinstance(caps.get("roles"), dict) else {}
    toggles = [name for name in AUDIO_TOGGLE_PARAMS
               if isinstance(params.get(name), dict) and params[name].get("binding") in ("native_param", "flag")]
    refs = deepcopy(AUDIO_DEFAULT_EVIDENCE.get((provider, model, mode), []))
    if toggles:
        output, kind = "supported_toggle", "route_form"
    elif refs:
        output, kind = "supported_default", "hosted_provider_documentation"
    else:
        output, kind = "unknown", "none"
    reference = roles.get("reference_audio") if isinstance(roles.get("reference_audio"), dict) else {}
    return {
        "native_output": output,
        "output_evidence": {"kind": kind, "refs": refs},
        "explicit_toggle": {"available": bool(toggles), "native_params": toggles,
                            # Exact retained form default per switch; None = no declared default.
                            "defaults": {name: _form_default(params[name]) for name in toggles}},
        "reference_audio": {"available": reference.get("supported") is True},
        "connector_observed": "not_tested",
        "dialogue_fidelity": "unverified",
    }


# Generic scene-profile notes ported from Social Studio's routing policy
# (observed 2026-10-07). Suggestive editorial guidance only: it never changes
# candidate order, eligibility or authority.
SCENE_GUIDANCE_SOURCES = {
    "catalog": "OpenArt MCP catalog and model forms inspected 2026-10-07; refresh through canonical engine before use.",
    "pixverse": "https://docs.platform.pixverse.ai/v6-2056814m0",
    "seedance": "https://seed.bytedance.com/en/blog/one-take-creation-flexible-referencing-introducing-seedance-2-5",
    "wan": "https://docs.modelstudio.console.alibabacloud.com/en/model-studio/wan3-video-generation-guide",
    "h3": "https://fal.ai/learn/devs/introducing-h3-max-by-fal",
}


def _note(model, modes, reason, sources):
    return {"route": {"provider": "openart_mcp", "tool": "openart_mcp_video", "model": model, "modes": list(modes)},
            "reason": reason, "sources": list(sources)}


_BOTH = ["element2video", "image2video"]
SCENE_GUIDANCE = {
    "budget_board_animation": {
        "description": "Simple motion from a reviewed board, with sound requirements checked separately.",
        "notes": [
            _note("pixverseV6", ["image2video"], "Simple board-animation candidate; image form exposes start/optional end frame and optional audio. No separate identity-reference form in this connector.", ["catalog", "pixverse"]),
            _note("fal-h3-max-turbo", ["image2video"], "Alternative board-animation candidate when its duration/resolution fit. This connector form exposes start/optional end frame, no audio toggle (default audio output per provider documentation, not result-verified) and no separate identity references.", ["catalog", "h3"]),
            _note("byte-plus-seedance-2-mini", ["image2video"], "Sound-capable image-mode alternative when the exact request needs it; temporal pins do not establish separate identity-reference support.", ["catalog", "seedance"]),
        ],
    },
    "speaking_characters": {
        "description": "Visible speakers, native dialogue and recognizable cast; require exact audio/reference evidence.",
        "notes": [
            _note("byte-plus-seedance-2-mini", _BOTH, "Control-fit candidate for short speaking scenes. Element form exposes visual references/audio; image form exposes start/end/audio. Neither form inherits the other's controls.", ["catalog", "seedance"]),
            _note("byte-plus-seedance-2-5", _BOTH, "Alternative for more complex/longer speaking scenes if its exact form fits. Vendor audiovisual/reference claims are not measured dialogue fidelity.", ["catalog", "seedance"]),
            _note("wan3-0", _BOTH, "Audio-capable alternative when its exact reference or pin mode satisfies the scene. Review actual mouth/dialogue synchronization.", ["catalog", "wan"]),
        ],
    },
    "general_action": {
        "description": "One readable action and consequence, optionally with native audio.",
        "notes": [
            _note("wan3-0", _BOTH, "Control-fit candidate for general action/native sound with either references or temporal pins, according to its exact form.", ["catalog", "wan"]),
            _note("byte-plus-seedance-2-mini", _BOTH, "Alternative for short action scenes whose exact reference/audio or pin requirements fit.", ["catalog", "seedance"]),
            _note("byte-plus-seedance-2-5", _BOTH, "Alternative when longer/complex action needs the exposed controls; newer naming proves no quality advantage.", ["catalog", "seedance"]),
        ],
    },
    "complex_physical_hero": {
        "description": "Geometry-critical contact, full completion or the central visual payoff.",
        "notes": [
            _note("byte-plus-seedance-2-5", _BOTH, "Candidate for complex action based on current audiovisual/reference controls and vendor claims; no evidence guarantees correct physical completion.", ["catalog", "seedance"]),
            _note("wan3-0", _BOTH, "Alternative with current audio/reference or pin controls. Reviewed geometry/action boards and semantic QC remain necessary.", ["catalog", "wan"]),
        ],
    },
    "identity_continuity": {
        "description": "Multiple recognizable characters or recurring identity continuity.",
        "notes": [
            _note("byte-plus-seedance-2-mini", ["element2video"], "Reference-form candidate for short cast-continuity scenes. Exact reference count/roles and dialogue still require canonical evidence.", ["catalog", "seedance"]),
            _note("byte-plus-seedance-2-5", ["element2video"], "Reference-form alternative for more complex or longer continuity scenes; vendor consistency claims do not certify this cast.", ["catalog", "seedance"]),
            _note("wan3-0", ["element2video"], "Reference-form alternative subject to exact identity controls and actual review.", ["catalog", "wan"]),
        ],
    },
}
MANAGED_ROUTE_GUIDANCE = {
    "route": {"provider": "grok_cli", "tool": "grok_cli_video", "model": None,
              "modes": ["image_to_video", "reference_to_video", "first_last_frame"]},
    "reason": "Compatible explicit episode choice when current canonical CLI controls, request and account fit. Subscription cost/quota and media model may remain unknown; no advertised OpenArt capability transfers to this route.",
    "sources": [],
}


def scene_model_guidance(scene_profile):
    """Suggestive, provenance-labeled notes for a generic scene profile."""
    if scene_profile not in SCENE_GUIDANCE:
        raise ValueError("unknown scene profile: " + repr(scene_profile))
    row = SCENE_GUIDANCE[scene_profile]
    return {
        "profile": scene_profile,
        "description": row["description"],
        "notes": deepcopy(row["notes"]),
        "managed_route": deepcopy(MANAGED_ROUTE_GUIDANCE),
        "sources": deepcopy(SCENE_GUIDANCE_SOURCES),
        "basis": "suggestive_not_ranking",
        "observed_on": "2026-10-07",
        "provenance": "Editorial guidance ported from Social Studio video-model-routing policy; vendor claims, not measured quality or success rates.",
    }
