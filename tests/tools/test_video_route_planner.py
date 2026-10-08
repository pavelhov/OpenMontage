"""Shared route chooser, split audio capability and repair decision validation (offline)."""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from lib import video_model_selection as vms
from lib.video_model_selection import choose_video_route, request_delta, validate_repair_decision
from lib.video_route_evidence import audio_capability, scene_model_guidance
from tests.lib.test_openart_mcp_native import observations, source_project, minimum  # noqa: F401

PIX = {"provider": "openart_cli", "tool": "openart_cli_video", "model": "pixverse-v6"}


def row(model, mode="image2video", provider="openart_cli", fit=0, cost=None, audio=None, **extra):
    route = {"provider": provider, "tool": provider + "_video", "model": model, "mode": mode}
    evidence = {"fit_score": fit, "controls": {}, "cost": cost or {"status": "unknown"}}
    if audio is not None:
        evidence["audio"] = audio
    base = {"route": route, "status": "eligible_for_planning", "request": {"model": model, "mode": mode},
            "source": "fixture", "blockers": [], "evidence": evidence}
    base.update(extra)
    return base


def pool(*models, provider="openart_cli"):
    return [{"provider": provider, "model": m} for m in models]


def auto(*models, **kw):
    return {"mode": "auto", "approved_pool": pool(*models, **kw)}


H3_DEFAULT = audio_capability("openart_mcp", "fal-h3-max-turbo", "image2video", {"params": {}, "roles": {}})
TOGGLE = audio_capability("openart_mcp", "seedance", "image2video", {"params": {"generateAudio": {"binding": "native_param", "schema": {"type": "boolean", "default": True}}}})
UNKNOWN = audio_capability("openart_mcp", "mystery", "image2video", {"params": {}})


def h3_inputs(mcp, root, assets, **extra):
    model = "fal-h3-max-turbo"
    profile = mcp.load_profile(model, "image2video", require="supported")
    params = minimum(profile["form"]["jsonSchema"])
    for field in {r[0] for r in mcp.ROLES.values()}:
        params.pop(field, None)
    inputs = {"project_dir": str(root), "openart_project_id": "fixture-project", "operation": "first_last_frame",
              "native_params": params, "native_audio": True,
              "input_assets": [{"role": "first_frame", **assets["start"]}, {"role": "last_frame", **assets["end"]}],
              "output_path": str(root / "clip.mp4"), "model": model, "mode": "image2video",
              "model_selection_intent": {"mode": "exact", "provider": "openart_mcp", "model": model,
                                         "approved_pool": [{"provider": "openart_mcp", "model": model}]}}
    inputs.update(extra)
    return inputs, profile


# AE1: supported implicit/default audio without any injected param.
def test_h3_default_audio_capability_is_split_and_honest():
    assert H3_DEFAULT["native_output"] == "supported_default"
    assert H3_DEFAULT["output_evidence"]["kind"] == "hosted_provider_documentation"
    assert H3_DEFAULT["explicit_toggle"] == {"available": False, "native_params": [], "defaults": {}}
    assert H3_DEFAULT["connector_observed"] == "not_tested"
    assert H3_DEFAULT["dialogue_fidelity"] == "unverified"
    urls = {ref["url"] for ref in H3_DEFAULT["output_evidence"]["refs"]}
    assert "https://fal.ai/learn/devs/introducing-h3-max-by-fal" in urls
    assert UNKNOWN["native_output"] == "unknown" and UNKNOWN["output_evidence"]["refs"] == []
    assert TOGGLE["native_output"] == "supported_toggle"
    assert TOGGLE["explicit_toggle"]["native_params"] == ["generateAudio"]


DOCUMENTED_DEFAULT = [
    ("fal-h3-max-turbo", "text2video"), ("fal-h3-max-turbo", "image2video"),
    ("fal-h3-max", "text2video"), ("fal-h3-max", "image2video"), ("fal-h3-max", "element2video"),
    ("minimax-h3", "text2video"), ("minimax-h3", "image2video"), ("minimax-h3", "element2video"),
]


@pytest.mark.parametrize("model,mode", DOCUMENTED_DEFAULT)
def test_every_documented_route_is_supported_default_without_toggle(model, mode):
    cap = audio_capability("openart_mcp", model, mode, {"params": {"prompt": {"binding": "native_param"}}})
    assert cap["native_output"] == "supported_default"
    assert cap["output_evidence"]["kind"] == "hosted_provider_documentation" and cap["output_evidence"]["refs"]
    assert cap["explicit_toggle"]["available"] is False
    assert (cap["connector_observed"], cap["dialogue_fidelity"]) == ("not_tested", "unverified")


@pytest.mark.parametrize("provider,model,mode", [
    ("openart_mcp", "fal-h3-max-turbo", "element2video"), ("openart_mcp", "fal-h3-max-turbo", "video2video"),
    ("openart_mcp", "fal-h3-max", "video2video"), ("openart_mcp", "minimax-h3", "video2video"),
    ("openart_cli", "fal-h3-max-turbo", "image2video"), ("openart_mcp", "minimax-h3-fast", "image2video"),
])
def test_neighboring_unmapped_routes_stay_unknown(provider, model, mode):
    cap = audio_capability(provider, model, mode, {"params": {}})
    assert cap["native_output"] == "unknown" and cap["output_evidence"] == {"kind": "none", "refs": []}


def test_fixture_h3_forms_have_no_audio_field():
    fixture = json.loads((Path(__file__).parents[1] / "fixtures/openart/mcp_native_forms.json").read_text())
    found = []

    def walk(node):
        if isinstance(node, dict):
            if str(node.get("model", "")).startswith(("fal-h3", "minimax-h3")) and isinstance(node.get("jsonSchema"), dict):
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(fixture)
    assert found
    for form in found:
        props = set(form["jsonSchema"].get("properties") or {})
        assert not props & {"audio", "generateAudio", "generateSound", "target_audio_url"}, form["model"]


def test_h3_exact_native_audio_requirement_plans_without_audio_param(source_project, monkeypatch):
    from lib import openart_mcp as mcp
    from lib.production_execution import _openart_mcp_controls
    from tools.video.openart_mcp_video import OpenArtMCPVideo
    from tools.video.video_selector import VideoSelector
    root, assets, _, _ = source_project
    monkeypatch.setattr(mcp, "_ALLOW_FIXTURE_PRODUCTION", True)
    inputs, profile = h3_inputs(mcp, root, assets)
    before = deepcopy(profile)
    tool = OpenArtMCPVideo()
    assert VideoSelector._missing_pinned_controls(inputs, tool) == []
    plan = vms.plan_video_model_selection(inputs, [tool])
    assert plan["status"] == "planned", plan
    planned = deepcopy(plan["planned_request"])
    assert "native_audio" not in planned
    planned.pop("model_selection_intent", None)
    native = mcp.prepare_native_request(_openart_mcp_controls(planned), profile)
    body = json.dumps(native["body"])
    for key in ("generateAudio", "generateSound", "target_audio_url", '"audio"'):
        assert key not in body
    # Reporting audio_capability leaves the immutable profile and frozen request unchanged.
    again = mcp.load_profile("fal-h3-max-turbo", "image2video", require="supported")
    assert "audio_capability" not in again
    assert again == before
    assert mcp.prepare_native_request(_openart_mcp_controls(planned), again) == native
    entry = tool.get_info()["model_catalog"]["fal-h3-max-turbo"]["modes"]["image2video"]
    assert entry["audio_capability"]["native_output"] == "supported_default"
    # An explicit form switch is still refused when the form has none.
    with_switch = deepcopy(inputs)
    with_switch["native_params"] = {**inputs["native_params"], "audio": True}
    assert VideoSelector._missing_pinned_controls(with_switch, tool)
    off = deepcopy(inputs)
    off["native_audio"] = False
    assert vms.plan_video_model_selection(off, [tool])["status"] == "blocked"


# AE2: requirement semantics.
def test_native_audio_requirement_uses_split_capability():
    cands = [row("mystery", audio=UNKNOWN), row("h3", audio=H3_DEFAULT), row("seed", audio=TOGGLE)]
    res = choose_video_route(cands, auto("mystery", "h3", "seed"), requirements={"controls": ["native_audio"]})
    assert res["status"] == "planned"
    assert res["selected"]["route"]["model"] == "h3"
    assert {r["route"]["model"]: r["code"] for r in res["rejected"]} == {"mystery": "missing_controls:native_audio"}
    res = choose_video_route(cands, auto("mystery", "h3", "seed"), requirements={"controls": ["native_audio_toggle"]})
    assert res["selected"]["route"]["model"] == "seed"
    only_unknown = choose_video_route([row("mystery", audio=UNKNOWN)], auto("mystery"),
                                      requirements={"controls": ["native_audio"]})
    assert only_unknown["status"] == "blocked"
    with_ref = deepcopy(H3_DEFAULT)
    with_ref["reference_audio"]["available"] = True
    res = choose_video_route([row("h3", "image2video", audio=H3_DEFAULT), row("h3", "element2video", audio=with_ref)],
                             auto("h3"), requirements={"controls": ["reference_audio"]})
    assert res["selected"]["route"]["mode"] == "element2video"


# AE3: one ordering implementation.
def test_fit_then_priority_then_pool_order():
    cands = [row("a", fit=1), row("b", fit=2), row("c", fit=2)]
    res = choose_video_route(cands, auto("a", "b", "c"))
    assert [r["route"]["model"] for r in res["ordered"]] == ["b", "c", "a"]
    res = choose_video_route(cands, auto("a", "b", "c"), account_priority=[{"provider": "openart_cli", "model": "c"}])
    assert [r["route"]["model"] for r in res["ordered"]] == ["c", "b", "a"]
    res = choose_video_route(cands, auto("a", "b", "c"), account_priority=[{"provider": "openart_cli", "model": "a"}])
    assert res["selected"]["route"]["model"] == "b"  # fit beats account priority
    assert res["authority"] == "none"


def test_full_mode_account_priority_selects_same_model_mode():
    cands = [row("pixverse-v6", "text2video"), row("pixverse-v6", "image2video")]
    res = choose_video_route(cands, auto("pixverse-v6"), account_priority=[{**PIX, "mode": "image2video"}])
    assert res["selected"]["route"]["mode"] == "image2video"
    outside = choose_video_route(cands, auto("pixverse-v6"), account_priority=[{**PIX, "model": "other"}])
    assert outside["blockers"][0]["code"] == "priority_outside_approved_pool"


def test_exact_and_prefer_selected_route_keep_mode():
    cands = [row("pixverse-v6", "text2video", fit=999), row("pixverse-v6", "image2video"), row("other", fit=999)]
    prefer = {"mode": "prefer", "provider": "openart_cli", "model": "pixverse-v6",
              "approved_pool": pool("pixverse-v6", "other")}
    res = choose_video_route(cands, prefer, selected_route={**PIX, "mode": "image2video"})
    assert res["selected"]["route"]["mode"] == "image2video"
    exact = {**prefer, "mode": "exact", "approved_pool": pool("pixverse-v6")}
    res = choose_video_route(cands, exact, selected_route={**PIX, "mode": "image2video"})
    assert res["selected"]["route"]["mode"] == "image2video"
    assert {r["code"] for r in res["rejected"]} == {"outside_exact_route", "outside_approved_pool"}


def test_unknown_cost_blocks_best_value_and_scene_profile_never_reorders():
    usd = lambda a: {"status": "estimated", "amount": a, "unit": "usd"}
    best = {**auto("a", "b"), "goal": "best_value"}
    assert choose_video_route([row("a", cost=usd(2)), row("b")], best)["blockers"][0]["code"] == "cost_not_comparable"
    assert choose_video_route([row("a", cost=usd(2)), row("b", cost=usd(1))], best)["selected"]["route"]["model"] == "b"
    plain = choose_video_route([row("a"), row("b")], auto("a", "b"))
    guided = choose_video_route([row("a"), row("b")], auto("a", "b"), scene_profile="speaking_characters")
    assert [r["route"] for r in plain["ordered"]] == [r["route"] for r in guided["ordered"]]
    assert guided["scene_guidance"] == scene_model_guidance("speaking_characters")
    with pytest.raises(ValueError):
        scene_model_guidance("no_such_profile")


def test_caller_rows_and_blockers_survive_unchanged():
    excluded = row("x", status="excluded", blockers=[{"code": "exact_route_lock"}], observation={"o": 1})
    kept = row("a", observation={"grok": True}, canonical_transition_blocker={"code": "t"})
    res = choose_video_route([excluded, kept], auto("a", "x"))
    rej = res["rejected"][0]
    assert rej["code"] == "candidate_excluded" and rej["blockers"] == [{"code": "exact_route_lock"}]
    assert rej["observation"] == {"o": 1} and rej["source"] == "fixture"
    sel = res["selected"]
    assert sel["observation"] == {"grok": True} and sel["canonical_transition_blocker"] == {"code": "t"}
    assert sel["request"] == kept["request"]


def test_existing_planner_uses_the_shared_chooser(monkeypatch):
    from tests.tools.test_video_model_selection import Route, request
    calls = []
    real = vms.choose_video_route
    monkeypatch.setattr(vms, "choose_video_route", lambda *a, **k: calls.append(a) or real(*a, **k))
    plan = vms.plan_video_model_selection(request(), [Route()])
    assert plan["status"] == "planned" and calls


# AE4: explicit producer repair decisions.
REVIEW = {"status": "fail", "predicates": [{"name": "whole-object-completed", "status": "fail"},
                                          {"name": "grain", "status": "fail", "severity": "cosmetic"}]}
ORIG = {"provider": "openart_mcp", "tool": "openart_mcp_video", "model": "h3", "mode": "image2video"}
POOL_INTENT = {"mode": "prefer", "provider": "openart_mcp", "model": "h3",
               "approved_pool": [{"provider": "openart_mcp", "model": "h3"}, {"provider": "openart_mcp", "model": "wan"}]}
EXACT_INTENT = {**POOL_INTENT, "mode": "exact", "approved_pool": POOL_INTENT["approved_pool"][:1]}
PRIOR = {"prompt": "a", "model": "h3", "mode": "image2video"}


def decision(remedy="prompt_staging", route=ORIG, proposed=None):
    proposed = proposed if proposed is not None else {**PRIOR, "prompt": "b"}
    return {"decision_id": "d1", "producer": "ep", "remedy": remedy, "rationale": "object cut off",
            "addresses_predicates": ["whole-object-completed"], "defect_evidence": ["frame 40"],
            "request_delta": request_delta(PRIOR, proposed), "selected_route": route}, proposed


def check(d, proposed, intent=POOL_INTENT, review=REVIEW):
    return validate_repair_decision(d, review=review, original_route=ORIG, intent=intent,
                                    original_request=PRIOR, proposed_request=proposed)


def test_valid_prompt_staging_and_model_change():
    d, p = decision()
    res = check(d, p)
    assert res["status"] == "valid_generation_decision" and res["grants_authority"] is False
    assert res["failed_predicates"] == ["whole-object-completed"]  # cosmetic excluded
    assert check(d, p, intent=EXACT_INTENT)["status"] == "valid_generation_decision"
    wan = {**ORIG, "model": "wan"}
    d, p = decision("model_change", wan, {**PRIOR, "model": "wan"})
    assert check(d, p)["selected_route"] == wan  # producer route honored, never substituted


@pytest.mark.parametrize("case,code", [
    ("none", "missing_producer_decision"), ("not_failed", "predicates_not_failed"),
    ("cosmetic", "predicates_not_failed"), ("identical", "identical_reroll"),
    ("delta", "request_delta_mismatch"), ("staging_route", "route_changed"),
    ("exact_model", "exact_intent"), ("same_model_mode", "same_model_reroll"),
    ("managed", "managed_reroll_not_model_change"), ("outside", "outside_approved_pool"),
    ("passed_review", "no_failed_critical_predicates")])
def test_invalid_repair_decisions(case, code):
    d, p = decision()
    intent, review = POOL_INTENT, REVIEW
    if case == "none":
        d = None
    elif case == "not_failed":
        d["addresses_predicates"] = ["identity-stable"]
    elif case == "cosmetic":
        d["addresses_predicates"] = ["grain"]
    elif case == "identical":
        d, p = decision(proposed=dict(PRIOR))
    elif case == "delta":
        d["request_delta"] = {"prompt": {"before": "a", "after": "c"}}
    elif case == "staging_route":
        d["selected_route"] = {**ORIG, "mode": "element2video"}
    elif case == "exact_model":
        d, p = decision("model_change", {**ORIG, "model": "wan"}, {**PRIOR, "model": "wan"})
        intent = EXACT_INTENT
    elif case == "same_model_mode":
        d, p = decision("model_change", {**ORIG, "mode": "element2video"}, {**PRIOR, "mode": "element2video"})
    elif case == "managed":
        d, p = decision("model_change", {**ORIG, "model": None}, {**PRIOR, "model": None})
    elif case == "outside":
        d, p = decision("model_change", {**ORIG, "model": "kling"}, {**PRIOR, "model": "kling"})
    elif case == "passed_review":
        review = {"status": "pass", "predicates": []}
    with pytest.raises(ValueError, match=code):
        check(d, p, intent=intent, review=review)


def test_edit_returns_engine_handoff_and_keeps_failure():
    d, _ = decision("edit")
    d["request_delta"] = {"trim": "remove frames 40-48"}
    with pytest.raises(ValueError, match="invalid_edit_handoff"):
        validate_repair_decision(d, review=REVIEW, original_route=ORIG, intent=POOL_INTENT)
    d["edit_handoff"] = {"tool": "video_trimmer", "inputs": {"cut": [40, 48]}}
    res = validate_repair_decision(d, review=REVIEW, original_route=ORIG, intent=EXACT_INTENT)
    assert res["status"] == "needs_engine_handoff"
    assert res["predicates_remain_failed"] == ["whole-object-completed"]
    assert res["grants_authority"] is False


def test_plumbing_only_change_is_identical_reroll_but_creative_delta_kept():
    plumbed = {**PRIOR, "output_path": "attempt2.mp4", "timeout_seconds": 900, "cli_session_id": "s2"}
    assert request_delta(PRIOR, plumbed) == {}
    d, p = decision(proposed=plumbed)
    with pytest.raises(ValueError, match="identical_reroll"):
        check(d, p)
    d, p = decision(proposed={**plumbed, "prompt": "b"})
    res = check(d, p)
    assert res["request_delta"] == {"prompt": {"before": "a", "after": "b"}}


GROK = {"provider": "grok_cli", "tool": "grok_cli_video", "model": None, "mode": "image2video"}


def test_managed_provider_prefer_without_model():
    from lib.video_model_selection import validate_model_selection_intent
    prefer = {"mode": "prefer", "provider": "grok_cli",
              "approved_pool": [{"provider": "grok_cli"}, {"provider": "openart_cli", "model": "wan"}]}
    assert validate_model_selection_intent(prefer)["provider"] == "grok_cli"
    grok = {"route": GROK, "status": "eligible_for_planning", "request": {}, "evidence": {"fit_score": 0}}
    wan = row("wan", fit=5)
    res = choose_video_route([wan, grok], prefer, selected_route=GROK)
    assert res["status"] == "planned" and res["selected"]["route"] == GROK
    down = {**grok, "status": "excluded", "blockers": [{"code": "unavailable"}]}
    res = choose_video_route([wan, down], prefer, selected_route=GROK)
    assert res["selected"]["route"]["model"] == "wan"
    assert "preferred_route_not_eligible" in res["limitations"]
    # A model-less prefer still needs a matching model-less pool entry.
    with pytest.raises(ValueError):
        validate_model_selection_intent({"mode": "prefer", "provider": "grok_cli",
                                         "approved_pool": [{"provider": "openart_cli", "model": "wan"}]})
    with pytest.raises(ValueError):
        validate_model_selection_intent({"mode": "prefer", "approved_pool": [{"provider": "grok_cli"}]})
    exact = {"mode": "exact", "provider": "grok_cli", "approved_pool": [{"provider": "grok_cli"}]}
    res = choose_video_route([wan, grok], exact)
    assert res["selected"]["route"] == GROK


@pytest.mark.parametrize("remedy,route,proposed_key", [
    ("prompt_staging", ORIG, "prompt"), ("model_change", {**ORIG, "model": "wan"}, "model")])
@pytest.mark.parametrize("missing", [None, {}])
def test_generation_repair_needs_actual_original_request(remedy, route, proposed_key, missing):
    proposed = {**PRIOR, proposed_key: "wan" if proposed_key == "model" else "b"}
    d = decision(remedy, route, proposed)[0]
    d["request_delta"] = request_delta(missing, proposed)  # producer claims all fields added
    with pytest.raises(ValueError, match="missing_original_request"):
        validate_repair_decision(d, review=REVIEW, original_route=ORIG, intent=POOL_INTENT,
                                 original_request=missing, proposed_request=proposed)
    with pytest.raises(ValueError, match="missing_proposed_request"):
        validate_repair_decision(d, review=REVIEW, original_route=ORIG, intent=POOL_INTENT,
                                 original_request=PRIOR, proposed_request=missing)


# Required audio output never silently becomes an explicitly silent request.
def _wan_inputs(root, native_params, native_audio=True):
    inputs = {"project_dir": str(root), "openart_project_id": "fixture-project", "prompt": "fixture",
              "native_params": native_params, "output_path": str(root / "c.mp4"), "model": "wan3-0",
              "mode": "text2video",
              "model_selection_intent": {"mode": "exact", "provider": "openart_mcp", "model": "wan3-0",
                                         "approved_pool": [{"provider": "openart_mcp", "model": "wan3-0"}]}}
    if native_audio is not None:
        inputs["native_audio"] = native_audio
    return inputs


def test_required_audio_conflicts_with_explicit_audio_off(source_project, monkeypatch):
    from lib import openart_mcp as mcp
    from tools.video.openart_mcp_video import OpenArtMCPVideo
    from tools.video.video_selector import VideoSelector
    monkeypatch.setattr(mcp, "_ALLOW_FIXTURE_PRODUCTION", True)
    root, _, _, _ = source_project
    tool = OpenArtMCPVideo()
    off = {"prompt": "fixture", "audio": False}
    conflict = _wan_inputs(root, dict(off))
    plan = vms.plan_video_model_selection(deepcopy(conflict), [tool])
    assert plan["status"] == "blocked"
    assert any(r.get("code") == "conflicting_controls:native_audio" for r in plan.get("rejected") or []) \
        or "conflicting_controls:native_audio" in json.dumps(plan)
    assert any("conflicts" in m for m in VideoSelector._missing_pinned_controls(conflict, tool))
    # Explicit audio off without an output requirement is a valid exact setting, kept verbatim.
    silent = _wan_inputs(root, dict(off), native_audio=None)
    plan = vms.plan_video_model_selection(deepcopy(silent), [tool])
    assert plan["status"] == "planned", plan
    assert plan["planned_request"]["native_params"]["audio"] is False
    assert VideoSelector._missing_pinned_controls(silent, tool) == []
    # Required audio with the switch on stays valid.
    on = _wan_inputs(root, {"prompt": "fixture", "audio": True})
    assert vms.plan_video_model_selection(deepcopy(on), [tool])["status"] == "planned"
    assert VideoSelector._missing_pinned_controls(on, tool) == []


def test_chooser_rejects_precompiled_audio_off_row_for_required_output():
    off = row("seed", audio=TOGGLE)
    off["request"]["native_params"] = {"generateAudio": False}
    res = choose_video_route([off, row("h3", audio=H3_DEFAULT)], auto("seed", "h3"),
                             requirements={"controls": ["native_audio"]})
    assert res["selected"]["route"]["model"] == "h3"
    assert res["rejected"][0]["code"] == "conflicting_controls:native_audio"
    assert res["rejected"][0]["request"]["native_params"] == {"generateAudio": False}  # not rewritten
    # Without the output requirement the explicit-off row stays eligible.
    assert choose_video_route([off], auto("seed"))["status"] == "planned"


@pytest.mark.parametrize("remedy,route", [("prompt_staging", ORIG), ("model_change", {**ORIG, "model": "wan"})])
@pytest.mark.parametrize("extra", [
    {"model": "kling"}, {"mode": "element2video"}, {"provider": "openart_cli"},
    {"preferred_provider": "openart_cli"}, {"hosting_provider": "fal.ai"}, {"tool": "openart_cli_video"}, {"preferred_tool": "openart_cli_video"},
    {"allowed_providers": ["openart_mcp", "openart_cli"]}, {"allowed_providers": ["openart_cli"]}])
def test_proposed_request_must_target_selected_route(remedy, route, extra):
    proposed = {**PRIOR, "model": route["model"], "prompt": "b", **extra}
    d = decision(remedy, route, proposed)[0]
    with pytest.raises(ValueError, match="request_route_mismatch"):
        check(d, proposed)
    ok = {**PRIOR, "model": route["model"], "prompt": "b", "preferred_provider": "openart_mcp",
          "allowed_providers": ["openart_mcp"], "preferred_tool": "openart_mcp_video",
          "hosting_provider": "openart_mcp"}
    d = decision(remedy, route, ok)[0]
    assert check(d, ok)["status"] == "valid_generation_decision"


def test_managed_and_operation_only_prompt_staging_requests_stay_valid():
    route = {**ORIG, "provider": "grok_cli", "tool": "grok_cli_video", "model": None}
    prior = {"prompt": "a", "operation": "image_to_video", "preferred_provider": "grok_cli"}
    proposed = {**prior, "prompt": "b"}
    d = {**decision()[0], "selected_route": route, "request_delta": request_delta(prior, proposed)}
    res = validate_repair_decision(d, review=REVIEW, original_route=route, intent=POOL_INTENT,
                                   original_request=prior, proposed_request=proposed)
    assert res["status"] == "valid_generation_decision" and "model" not in proposed


def test_unknown_scene_profile_blocks_chooser_but_direct_guidance_raises():
    res = choose_video_route([row("h3")], auto("h3"), scene_profile="no_such_profile")
    assert res["status"] == "blocked" and res["blockers"][0]["code"] == "unknown_scene_profile"
    with pytest.raises(ValueError):
        scene_model_guidance("no_such_profile")


# Effective switch value: explicit native_params value, else exact form default; never injected.
def _toggle(default):
    spec = {"type": "boolean"} if default is None else {"type": "boolean", "default": default}
    return audio_capability("openart_mcp", "pix", "text2video",
                            {"params": {"generateAudio": {"binding": "native_param", "schema": spec}}})


@pytest.mark.parametrize("default,native_params,code", [
    (False, {}, "conflicting_controls:native_audio"),
    (False, {"generateAudio": False}, "conflicting_controls:native_audio"),
    (None, {}, "missing_controls:native_audio_enabled"),
    (False, {"generateAudio": True}, None),
    (True, {}, None),
    (None, {"generateAudio": True}, None),
])
def test_chooser_uses_effective_audio_switch_value(default, native_params, code):
    audio = _toggle(default)
    assert audio["explicit_toggle"]["defaults"] == {"generateAudio": default}
    cand = row("pix", audio=audio)
    cand["request"]["native_params"] = dict(native_params)
    res = choose_video_route([cand, row("h3", audio=H3_DEFAULT)], auto("pix", "h3"),
                             requirements={"controls": ["native_audio"]})
    if code:
        assert res["selected"]["route"]["model"] == "h3"  # H3 documented default, no toggle, stays eligible
        assert res["rejected"][0]["code"] == code
        assert res["rejected"][0]["request"]["native_params"] == native_params  # never injected
    else:
        assert res["selected"]["route"]["model"] == "pix" and not res["rejected"]
    # Without an output requirement the same request is a valid exact setting.
    assert choose_video_route([cand], auto("pix"))["status"] == "planned"


def _pix_inputs(root, native_params, native_audio=True):
    inputs = _wan_inputs(root, native_params, native_audio)
    inputs.update(model="pixverseV6", model_selection_intent={
        "mode": "exact", "provider": "openart_mcp", "model": "pixverseV6",
        "approved_pool": [{"provider": "openart_mcp", "model": "pixverseV6"}]})
    return inputs


@pytest.mark.parametrize("make,native_params,ok", [
    (_pix_inputs, {"prompt": "fixture"}, False),                          # form default false, omitted
    (_pix_inputs, {"prompt": "fixture", "generateAudio": True}, True),    # explicit true
    (_wan_inputs, {"prompt": "fixture"}, True),                           # form default true, omitted
])
def test_real_fixture_forms_use_effective_audio_default(source_project, monkeypatch, make, native_params, ok):
    from lib import openart_mcp as mcp
    from tools.video.openart_mcp_video import OpenArtMCPVideo
    from tools.video.video_selector import VideoSelector
    monkeypatch.setattr(mcp, "_ALLOW_FIXTURE_PRODUCTION", True)
    root, _, _, _ = source_project
    tool = OpenArtMCPVideo()
    inputs = make(root, dict(native_params))
    plan = vms.plan_video_model_selection(deepcopy(inputs), [tool])
    missing = VideoSelector._missing_pinned_controls(inputs, tool)
    if ok:
        assert plan["status"] == "planned", plan
        assert plan["planned_request"]["native_params"] == native_params  # nothing injected
        assert missing == []
    else:
        assert plan["status"] == "blocked"
        assert "conflicting_controls:native_audio" in json.dumps(plan)
        assert any("default off" in m for m in missing)
    # Without the output requirement the exact settings stay valid verbatim.
    silent = make(root, dict(native_params), native_audio=None)
    assert vms.plan_video_model_selection(deepcopy(silent), [tool])["status"] == "planned"
    assert VideoSelector._missing_pinned_controls(silent, tool) == []
