"""Offline bounded-intent plans and canonical selector dispatch seam."""
from copy import deepcopy

import pytest

from lib.video_model_selection import plan_video_model_selection, selection_intent_allows_route
from tools.base_tool import ToolResult, ToolStatus
from tools.video.video_selector import VideoSelector


class Route:
    capability = "video_generation"
    def __init__(self, provider="openart_cli", models=("qualified",), *, ready=True, cost=None):
        self.provider, self.name = provider, provider + "_video"
        self.supports = {"text_to_video": True, "image_to_video": True, "reference_image": True,
                         "explicit_selection_only": provider.endswith("cli"),
                         "native_audio": False, "first_last_frame": False}
        self.input_schema = {"additionalProperties": False, "properties": {
            "model": {"enum": list(models)}, "prompt": {"type": "string"},
            "operation": {"type": "string"}, "duration": {"type": "integer"},
            "aspect_ratio": {"enum": ["16:9", "9:16"]}, "resolution": {"enum": ["720p"]},
            "mode": {"type": "string"}, "output_path": {"type": "string"}}}
        self.ready, self.calls, self.cost, self.last = ready, 0, cost, None
        self.catalog = {m: {"modes": {"text2video": {
            "operation": "text_to_video", "level": "pre_submit", "source": "real", "production_ready": True,
            "full_result_qualified": False,
            "result_proof_id": "transport-proof", "native_controls": {
                "duration": {"qualified_in_exact_preview": True, "preview": {"value": 5}},
                "aspectRatio": {"qualified_in_exact_preview": True, "enum": [{"value": "16:9"}, {"value": "9:16"}]},
                "resolution": {"qualified_in_exact_preview": True, "enum": [{"value": "720p"}]},
            }}}} for m in models} if provider == "openart_cli" else {}

    def get_info(self):
        return {"supports": self.supports, "model_catalog": self.catalog,
                "qualification_required": self.provider == "openart_cli", "billing_unit": "credits"}

    def get_status(self):
        return ToolStatus.AVAILABLE if self.ready else ToolStatus.UNAVAILABLE

    def is_operation_available(self, operation):
        return self.supports.get(operation, False)

    def estimate_cost(self, inputs):
        return self.cost

    def execute(self, inputs):
        self.calls += 1
        self.last = inputs
        return ToolResult(success=False, data={"dispatch_status": "uncertain"}, error="original outcome unknown")


def intent(mode="exact", model="qualified", pool=None, **extra):
    return {"mode": mode, **({"model": model} if model else {}),
            "approved_pool": pool or [{"provider": "openart_cli", "model": "qualified"}], **extra}


def request(selection=None, **extra):
    return {"prompt": "A landscape", "operation": "text_to_video", "duration": "5",
            "aspect_ratio": "16:9", "resolution": "720p", "output_path": "clip.mp4",
            "model_selection_intent": selection or intent(), **extra}


def test_rank_hint_is_consumed_before_planned_generation(monkeypatch):
    route = Route()
    selector = VideoSelector()
    monkeypatch.setattr(selector, '_providers', lambda: [route])
    ranked = selector.execute(request(operation='rank', target_operation='text_to_video'))
    assert ranked.success and route.calls == 0
    assert ranked.data['planned_request']['operation'] == 'text_to_video'
    assert 'target_operation' not in ranked.data['planned_request']


def test_exact_ignores_cost_and_other_pool_candidates():
    route = Route(models=("qualified", "other"))
    plan = plan_video_model_selection(request(intent(pool=[{"provider": route.provider, "model": m} for m in ("other", "qualified")], goal="best_value")), [route])
    assert plan["selected"]["model"] == "qualified"
    assert plan["selected"]["cost"]["status"] == "unknown"
    assert plan["planned_request"]["duration"] == "5"
    assert plan["planned_request"]["allowed_providers"] == ["openart_cli"]
    assert plan["planned_request"]["preferred_tool"] == route.name
    assert route.calls == 0


def test_preference_fallback_only_approved_pool():
    route = Route(models=("qualified", "candidate", "unapproved"))
    route.catalog["candidate"]["modes"]["text2video"]["level"] = "pre_submit"
    route.catalog["candidate"]["modes"]["text2video"]["production_ready"] = False
    selection = intent("prefer", "candidate", [{"provider": route.provider, "model": m} for m in ("candidate", "qualified")])
    plan = plan_video_model_selection(request(selection), [route])
    assert plan["selected"]["model"] == "qualified"
    assert plan["rejected_candidates"][0]["code"] == "unqualified_model"
    assert not selection_intent_allows_route(selection, route.provider, "unapproved")


def test_pre_submit_native_profile_is_eligible_without_empirical_result():
    route = Route()
    mode = route.catalog["qualified"]["modes"]["text2video"]
    assert mode["level"] == "pre_submit" and mode["production_ready"] is True
    assert mode["full_result_qualified"] is False
    plan = plan_video_model_selection(request(), [route])
    assert plan["status"] == "planned"
    assert plan["selected"]["model"] == "qualified"


def test_pre_submit_profile_with_unqualified_required_native_control_blocks():
    route = Route()
    route.catalog["qualified"]["modes"]["text2video"]["native_controls"]["resolution"]["qualified_in_exact_preview"] = False
    plan = plan_video_model_selection(request(), [route])
    assert plan["status"] == "blocked"
    assert plan["rejected_candidates"][0]["code"] == "unqualified_native_control:resolution"


def test_exact_candidate_never_substituted():
    route = Route(models=("qualified", "candidate"))
    route.catalog["candidate"]["modes"]["text2video"]["production_ready"] = False
    plan = plan_video_model_selection(request(intent("exact", "candidate", [{"provider": route.provider, "model": m} for m in ("candidate", "qualified")])), [route])
    assert plan["status"] == "blocked"
    assert route.calls == 0


@pytest.mark.parametrize("extra,code", [({"native_audio": True}, "missing_controls:native_audio"),
    ({"last_image_path": "end.png"}, "missing_controls:first_last_frame"),
    ({"reference_image_paths": ["a.png", "b.png"]}, "missing_controls:multiple_reference_images"),
    ({"resolution": "768P"}, "unsupported_setting:resolution"),
    ({"duration": "10"}, "unqualified_setting:duration")])
def test_required_controls_fail_before_launch(extra, code):
    route = Route()
    plan = plan_video_model_selection(request(**extra), [route])
    assert plan["status"] == "blocked"
    assert plan["rejected_candidates"][0]["code"] == code
    assert route.calls == 0


def test_auto_best_value_comparable_estimates():
    cheap, expensive = Route("a", ("a1",), cost=1), Route("b", ("b1",), cost=2)
    selection = intent("auto", None, [{"provider": "b", "model": "b1"}, {"provider": "a", "model": "a1"}], goal="best_value")
    plan = plan_video_model_selection(request(selection), [cheap, expensive])
    assert plan["selected"]["model"] == "a1"
    assert plan["selected"]["cost"]["status"] == "estimated"


def test_unknown_subscription_not_zero_or_comparable_to_usd():
    cli, api = Route(), Route("api", ("api1",), cost=1)
    selection = intent("auto", None, [{"provider": cli.provider, "model": "qualified"}, {"provider": "api", "model": "api1"}], goal="best_value")
    assert plan_video_model_selection(request(selection), [cli, api])["blockers"][0]["code"] == "cost_not_comparable"


def test_mixed_pool_does_not_promote_candidate():
    cli, api = Route(), Route("api", ("api1",), cost=1)
    cli.catalog["qualified"]["modes"]["text2video"]["source"] = "fixture"
    selection = intent("auto", None, [{"provider": cli.provider, "model": "qualified"}, {"provider": "api", "model": "api1"}], goal="max_clean")
    plan = plan_video_model_selection(request(selection), [cli, api])
    assert plan["selected"]["provider"] == "api"
    assert "not_quality_benchmark" in plan["selected"]["fit_basis"]


def test_grok_agent_model_not_backend():
    grok = Route("grok_cli", ())
    grok.supports.update(text_to_video=False, image_to_video=True, native_audio=True)
    bad = intent("exact", "grok-agent", [{"provider": "grok_cli", "model": "grok-agent"}])
    assert plan_video_model_selection(request(bad, operation="image_to_video"), [grok])["status"] == "blocked"
    good = intent("exact", None, [{"provider": "grok_cli", "tool": grok.name}], provider="grok_cli")
    plan = plan_video_model_selection(request(good, operation="image_to_video"), [grok])
    assert plan["selected"]["model"] is None
    assert "model" not in plan["planned_request"]


def test_selector_planned_request_flows_through_canonical_pin(monkeypatch):
    route = Route()
    selector = VideoSelector()
    monkeypatch.setattr(selector, "_providers", lambda: [route])
    monkeypatch.setattr("lib.scoring.rank_providers", lambda *a: pytest.fail("exact CLI pin must not score"))
    fn = VideoSelector.execute
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    result = fn(selector, request())
    assert route.calls == 1
    assert route.last["model"] == "qualified"
    assert "model_selection_intent" not in route.last
    assert "preferred_tool" not in route.last
    assert result.data["dispatch_status"] == "uncertain"
    assert result.data["fallback_tools"] == []
    assert result.data["model_selection"]["intent"]["mode"] == "exact"


def test_selector_rank_and_blocked_plan_never_execute(monkeypatch):
    route = Route()
    selector = VideoSelector()
    monkeypatch.setattr(selector, "_providers", lambda: [route])
    result = selector.execute(request(operation="rank", target_operation="text_to_video"))
    assert result.success and result.data["planned_request"]["model"] == "qualified"
    blocked = selector.execute(request(native_audio=True))
    assert not blocked.success and blocked.data["dispatch_status"] == "not_dispatched"
    assert route.calls == 0


def test_policy_predicate_exact_lock_and_preferred_bounded_pool():
    pool = [{"provider": "openart_cli", "model": "qualified"}, {"provider": "api", "model": "api1"}]
    exact = intent(pool=pool)
    preferred = intent("prefer", pool=pool)
    assert selection_intent_allows_route(exact, "openart_cli", "qualified")
    assert not selection_intent_allows_route(exact, "api", "api1")
    assert selection_intent_allows_route(preferred, "api", "api1")
    assert not selection_intent_allows_route(preferred, "api", "other")
    assert not selection_intent_allows_route({}, "api", "other")


def test_crossed_scopes_and_missing_explicit_pool():
    route = Route()
    assert plan_video_model_selection(request(allowed_providers=["api"]), [route])["status"] == "blocked"
    malformed = {"mode": "auto", "goal": "balanced"}
    assert plan_video_model_selection(request(malformed), [route])["blockers"][0]["code"] == "invalid_model_selection_intent"


def test_single_unknown_auto_best_value_stays_blocked():
    selection = intent("auto", None, [{"provider": "openart_cli", "model": "qualified"}], goal="best_value")
    assert plan_video_model_selection(request(selection), [Route()])["blockers"][0]["code"] == "cost_not_comparable"


def test_exact_policy_cannot_admit_same_model_other_provider():
    selection = intent(pool=[{"provider": "openart_cli", "model": "qualified"},
                             {"provider": "api", "model": "qualified"}])
    assert not selection_intent_allows_route(selection, "api", "qualified")
    selection["provider"] = "openart_cli"
    assert selection_intent_allows_route(selection, "openart_cli", "qualified")
    assert not selection_intent_allows_route(selection, "api", "qualified")


def test_unpublished_cli_price_metadata_does_not_become_cost_authority():
    route = Route(models=("qualified", "second"))
    pool = [{"provider": route.provider, "model": model} for model in ("qualified", "second")]
    selection = intent("auto", None, pool, goal="best_value")
    for row in route.catalog.values():
        row["modes"]["text2video"]["selection_cost"] = {"amount": 0, "unit": "credits"}
    plan = plan_video_model_selection(request(selection), [route])
    assert plan["status"] == "blocked"
    assert plan["blockers"][0]["code"] == "cost_not_comparable"


def test_provider_pin_and_endpoint_transport_survive_planning():
    route = Route()
    assert plan_video_model_selection(request(preferred_provider="other"), [route])["rejected_candidates"][0]["code"] == "crossed_provider_constraints"
    grok = Route("grok_cli", ())
    grok.supports.update(text_to_video=False, first_last_frame=True)
    selection = intent("exact", None, [{"provider": "grok_cli"}], provider="grok_cli")
    blocked = plan_video_model_selection(request(selection, operation="first_last_frame", last_image_url="https://example.test/end.png"), [grok])
    assert blocked["rejected_candidates"][0]["code"] == "missing_controls:last_image_url"


@pytest.mark.parametrize("extra,code", [
    ({"keyframes": [{"image": "middle.png", "timestamp_s": 2}]}, "missing_controls:keyframes"),
    ({"reference_image_paths": ["one.png"]}, "unqualified_model_references"),
    ({"reference_image_urls": ["https://example.test/one.png"]}, "unqualified_model_references"),
    ({"image_path": "one.png"}, "unqualified_model_references"),
    ({"reference_video_paths": ["one.mp4"]}, "unqualified_model_references"),
    ({"reference_audio_urls": ["https://example.test/one.wav"]}, "unqualified_model_references"),
    ({"watermark": False}, "missing_controls:watermark"),
])
def test_text_only_qualified_profile_cannot_drop_media_or_native_controls(extra, code):
    route = Route()
    plan = plan_video_model_selection(request(**extra), [route])
    assert plan["status"] == "blocked"
    assert plan["rejected_candidates"][0]["code"] == code
    assert route.calls == 0


@pytest.mark.parametrize('key,value', [
    ('audio_path', 'a.wav'), ('audio_url', 'https://example.test/a.wav'),
    ('negative_prompt', 'blur'), ('seed', 7), ('fps', 24),
    ('camera_motion', 'pan'), ('start_image', 'start.png'), ('end_image', 'end.png'),
    ('future_native_control', False),
])
def test_open_schema_route_refuses_all_undeclared_creative_inputs(monkeypatch, key, value):
    # Faithful to existing API routes: an open input schema does not mean the
    # adapter consumes arbitrary keys, even when its prompt path can generate.
    route = Route('api', ('api1',), cost=1)
    route.input_schema.pop('additionalProperties')
    selector = VideoSelector()
    monkeypatch.setattr(selector, '_providers', lambda: [route])
    selection = intent('auto', None, [{'provider': 'api', 'model': 'api1'}])
    result = selector.execute(request(selection, **{key: value}))
    assert not result.success
    assert result.data['model_selection']['rejected_candidates'][0]['code'] == 'missing_controls:' + key
    assert result.data['dispatch_status'] == 'not_dispatched'
    assert route.calls == 0


def test_declared_native_values_preserved_and_validated_on_open_route():
    route = Route('api', ('api1',), cost=1)
    route.input_schema.pop('additionalProperties')
    route.input_schema['properties'].update(seed={'type': 'integer'}, fps={'type': 'integer', 'enum': [24, 30]},
                                            negative_prompt={'type': 'string'})
    selection = intent('auto', None, [{'provider': 'api', 'model': 'api1'}])
    extras = {'seed': 7, 'fps': 24, 'negative_prompt': 'blur'}
    plan = plan_video_model_selection(request(selection, **extras), [route])
    assert plan['status'] == 'planned'
    assert all(plan['planned_request'][key] == value for key, value in extras.items())
    for extra in ({'seed': '7'}, {'fps': 60}, {'negative_prompt': 1}):
        blocked = plan_video_model_selection(request(selection, **extra), [route])
        assert blocked['status'] == 'blocked'
        assert blocked['rejected_candidates'][0]['code'].startswith('unsupported_setting:')
    assert route.calls == 0


@pytest.mark.parametrize('target', ['image_path', 'image_url'])
def test_reference_alias_allowed_only_when_selector_really_translates(target):
    route = Route('api', ('api1',), cost=1)
    route.input_schema['properties'][target] = {'type': 'string', 'minLength': 1}
    selection = intent('auto', None, [{'provider': 'api', 'model': 'api1'}])
    req = request(selection, operation='image_to_video', reference_image_path='source.png')
    plan = plan_video_model_selection(req, [route])
    assert plan['status'] == 'planned'
    assert plan['planned_request']['reference_image_path'] == 'source.png'
    # The actual selector's existing dispatch adapter maps the approved alias.
    assert route.calls == 0
    for value in (False, [], ''):
        blocked = plan_video_model_selection({**req, 'reference_image_path': value}, [route])
        assert blocked['rejected_candidates'][0]['code'] == 'unsupported_setting:reference_image_path'
    del route.input_schema['properties'][target]
    assert plan_video_model_selection(req, [route])['rejected_candidates'][0]['code'] == 'missing_controls:reference_image_path'


def test_reference_alias_not_exempt_for_wrong_operation_or_conflicting_target():
    route = Route('api', ('api1',), cost=1)
    route.input_schema['properties']['image_path'] = {'type': 'string'}
    selection = intent('auto', None, [{'provider': 'api', 'model': 'api1'}])
    wrong_operation = plan_video_model_selection(request(selection, reference_image_path='source.png'), [route])
    assert wrong_operation['rejected_candidates'][0]['code'] == 'missing_controls:reference_image_path'
    conflict = plan_video_model_selection(request(selection, operation='image_to_video',
        reference_image_path='source.png', image_path='other.png'), [route])
    assert conflict['rejected_candidates'][0]['code'] == 'conflicting_alias:reference_image_path'


def test_canonical_governance_plumbing_retained_without_granting_authority():
    from lib.production_execution import GOVERNANCE_KEYS
    route = Route('api', ('api1',), cost=1)
    selection = intent('auto', None, [{'provider': 'api', 'model': 'api1'}])
    plumbing = {key: {'mode': 'dry_run'} if key == 'governance' else 'retained-by-wrapper' for key in GOVERNANCE_KEYS}
    plumbing['scene_id'] = 'scene-1'
    plan = plan_video_model_selection(request(selection, **plumbing), [route])
    assert plan['status'] == 'planned'
    assert all(plan['planned_request'][key] == value for key, value in plumbing.items())
    assert 'Planning grants no dispatch or billing authority.' in plan['limitations']
    assert route.calls == 0


@pytest.mark.parametrize('existing', ['https://example.test/other.png', None])
def test_url_alias_cannot_be_skipped_by_existing_target(existing):
    route = Route('api', ('api1',), cost=1)
    route.input_schema['properties']['image_url'] = {'type': 'string'}
    selection = intent('auto', None, [{'provider': 'api', 'model': 'api1'}])
    plan = plan_video_model_selection(request(selection, operation='image_to_video',
        reference_image_path='source.png', image_url=existing), [route])
    assert plan['status'] == 'blocked'
    assert plan['rejected_candidates'][0]['code'] == 'conflicting_alias:reference_image_path'
    assert route.calls == 0


def test_mcp_native_roles_and_audio_use_exact_form_capabilities(monkeypatch, tmp_path):
    from lib import openart_mcp as mcp
    route = Route(provider='openart_mcp')
    route.supports.update(explicit_selection_only=True, reference_image=False,
                          native_audio=False, first_last_frame=False, multiple_reference_images=False)
    route.catalog = {'qualified': {'modes': {'image2video': {'operation': 'image_to_video',
                        'production_ready': True}}}}
    route.input_schema['properties'].update(
        input_assets={'type': 'array'}, native_params={'type': 'object'})
    profile = {'native_capabilities': {'roles': {
        role: {'supported': True} for role in ('first_frame', 'last_frame', 'reference_image')},
        'params': {'audio': {'binding': 'native_param'}}}}
    calls = []
    monkeypatch.setattr(mcp, 'load_profile', lambda *a, **kw: profile)
    monkeypatch.setattr(mcp, 'prepare_native_request', lambda controls, p: calls.append((controls, p)))
    inputs = {'project_dir': str(tmp_path), 'prompt': 'A landscape', 'operation': 'image_to_video',
        'model_selection_intent': {'mode': 'exact', 'model': 'qualified', 'provider': 'openart_mcp',
                                  'approved_pool': [{'provider': 'openart_mcp', 'model': 'qualified'}]},
        'native_params': {'audio': True}, 'input_assets': [
            {'role': role, 'source_path': str(tmp_path / (str(i) + '.png'))}
            for i, role in enumerate(['first_frame', 'last_frame', 'reference_image', 'reference_image'])],
        'output_path': str(tmp_path / 'clip.mp4')}
    plan = plan_video_model_selection(inputs, [route])
    assert plan['status'] == 'planned'
    assert calls[0][0]['project_dir'] == str(tmp_path.resolve())
    assert route.calls == 0
    profile['native_capabilities']['roles']['last_frame']['supported'] = False
    assert plan_video_model_selection(inputs, [route])['status'] == 'blocked'


def test_canonical_mcp_native_controls_are_requested():
    from lib.video_model_selection import _requested_controls
    assert set(_requested_controls({'native_params': {'generateSound': True}, 'input_assets': [
        {'role': 'last_frame'}, {'role': 'character_reference'}, {'role': 'environment_reference'}]})) == {
            'first_last_frame', 'native_audio', 'multiple_reference_images'}


# Actual native source receipts are synthetic and isolated by these fixtures.
from tests.lib.test_openart_mcp_native import observations, source_project, minimum


@pytest.mark.parametrize('model,expected', [('fal-h3-max', 'planned'), ('gemini-omni-flash', 'blocked')])
def test_explicit_mcp_first_last_frame_exact_pool_uses_actual_source_builder(source_project, monkeypatch, model, expected):
    from lib import openart_mcp as mcp, openart_mcp_jobs as jobs
    from tools.video.openart_mcp_video import OpenArtMCPVideo
    root, assets, _, _ = source_project
    monkeypatch.setattr(mcp, '_ALLOW_FIXTURE_PRODUCTION', True)  # native readiness, no prior result
    profile = mcp.load_profile(model, 'image2video', require='supported')
    params = minimum(profile['form']['jsonSchema'])
    for field in {row[0] for row in mcp.ROLES.values()}:
        params.pop(field, None)
    inputs = {'project_dir': str(root), 'openart_project_id': 'fixture-project',
        'operation': 'first_last_frame', 'native_params': params,
        'input_assets': [{'role': 'first_frame', **assets['start']}, {'role': 'last_frame', **assets['end']}],
        'output_path': str(root / 'clip.mp4'),
        'model': model, 'mode': 'image2video',
        'model_selection_intent': {'mode': 'exact', 'provider': 'openart_mcp', 'model': model,
                                  'approved_pool': [{'provider': 'openart_mcp', 'model': model}]}}
    if expected == 'planned':
        from lib.production_execution import _openart_mcp_controls
        exact_inputs = deepcopy(inputs)
        exact_inputs.pop('model_selection_intent')
        mcp.prepare_native_request(_openart_mcp_controls(exact_inputs), profile)
    plan = plan_video_model_selection(inputs, [OpenArtMCPVideo()])
    assert plan['status'] == expected, plan
    if expected == 'planned':
        assert plan['planned_request']['operation'] == 'first_last_frame'
        assert plan['planned_request']['mode'] == 'image2video'
        controls = deepcopy(plan['planned_request'])
        controls.pop('model_selection_intent', None)
        from lib.production_execution import _openart_mcp_controls
        native = mcp.prepare_native_request(_openart_mcp_controls(controls), profile)
        assert native['body']['params']['endFrame']['id']
    else:
        assert plan['rejected_candidates'][0]['code'] == 'unqualified_native_controls'
