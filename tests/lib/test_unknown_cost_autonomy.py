"""Unknown-cost OpenArt Auto-continue policy variant. Offline only; no CLI, network or spend."""
import copy
import json

import pytest

from lib import production_autonomy as pa
from lib.production_request import digest
from tests.lib.test_production_autonomy import H, GROK_ROW, install, make_policy, openart_row

UNKNOWN = {'id': 'openart_cli', 'billing': 'unknown_cost_no_ceiling',
           'exposure_acknowledgement': 'no_enforceable_credit_ceiling', 'account_id_sha256': H('acct'),
           'workspace': '__unobserved_workspace__', 'routes': [{'model': 'pixverseV6', 'mode': 'text2video'}]}


def unknown_policy(**over):
    policy = make_policy(**over)
    policy['providers'] = [copy.deepcopy(UNKNOWN), copy.deepcopy(policy['providers'][1])]
    policy['evidence'] = {'path': f'approvals/autonomy-policy-{H("p")}.json', 'sha256': H('e')}
    return policy


def t2v_row(model='pixverseV6'):
    return openart_row(model=model, mode='text2video', controls={'native_controls': {}, 'required_unqualified': []})


def test_unknown_variant_schema_valid_and_old_policies_unchanged():
    assert pa.schema_errors(unknown_policy()) == []
    old = make_policy(evidence={'path': f'approvals/autonomy-policy-{H("p")}.json', 'sha256': H('e')})
    assert pa.schema_errors(old) == []
    # Omitted new fields leave the digested content of existing policies untouched.
    assert 'model_selection_intents' not in pa.evidence_content(old, {})
    assert not pa._is_unknown(pa._openart_spec(old))


@pytest.mark.parametrize('mutate', [
    lambda s: s.pop('exposure_acknowledgement'),
    lambda s: s.update(exposure_acknowledgement='go'),
    lambda s: s.update(allow_unknown_cost=True),
    lambda s: s.update(ceiling='40'),
    lambda s: s.update(workspace='ws'),
    lambda s: s.update(routes=[]),
])
def test_unknown_variant_requires_exact_acknowledged_shape(mutate):
    policy = unknown_policy()
    mutate(policy['providers'][0])
    assert pa.schema_errors(policy)
    assert not pa._is_unknown(policy['providers'][0]) or pa.schema_errors(policy)


def test_generic_go_on_priced_policy_never_unknown():
    policy = make_policy()
    policy['providers'][0]['allow_unknown_cost'] = True
    assert pa.schema_errors(policy)
    assert not pa._is_unknown(dict(UNKNOWN, exposure_acknowledgement=None))
    assert not pa._is_unknown(dict(UNKNOWN, billing='credits'))


@pytest.mark.parametrize('field', ['unknown_cost_authorization', 'unknown_cost_authorization_sha256',
                                   'unknown_cost_authorization_id', 'unknown_cost_evidence_id'])
def test_caller_authority_injection_refused(field):
    with pytest.raises(pa.AutonomyError, match='unavailable'):
        pa._refuse_unknown_cost({field: 'x'})
    if field != 'unknown_cost_evidence_id':
        with pytest.raises(pa.AutonomyError):
            pa._refuse_unknown_cost({field: 'x'}, derived_unknown_id='scope-a', allow_evidence=True)
    pa._refuse_unknown_cost({'unknown_cost_authorization_id': 'scope-a'}, derived_unknown_id='scope-a')


def test_eligible_routes_only_exact_approved_unknown_routes():
    policy = unknown_policy()
    ok, bad = pa.eligible_routes(policy, 'entry', [t2v_row(), t2v_row('kling-3'), GROK_ROW])
    assert {(r['provider'], r['model']) for r in ok} == {('openart_cli', 'pixverseV6'), ('grok_cli', None)}
    assert pa.eligible_routes(policy, 'entry', [openart_row(model='pixverseV6', mode='i2v')])[0] == []
    wrong = t2v_row()
    wrong['models'][0]['account_id_sha256'] = H('other')
    assert pa.eligible_routes(policy, 'entry', [wrong])[0] == []


def test_model_lock_must_match_an_unknown_route():
    policy = unknown_policy()
    policy['locked']['controls'] = {'model': {'value': 'kling-3'}}
    assert any('excludes every approved unknown-cost route' in c for c in pa.policy_conflicts(policy))
    policy['locked']['controls'] = {'model': {'value': 'pixverseV6'}}
    assert pa.policy_conflicts(policy) == []


EXACT = {'mode': 'exact', 'model': 'pixverseV6', 'provider': 'openart_cli',
         'approved_pool': [{'provider': 'openart_cli', 'model': 'pixverseV6'}, {'provider': 'grok_cli'}]}


def test_exact_per_shot_intent_survives_broader_episode_pool():
    policy = unknown_policy(model_selection_intents={'entry': copy.deepcopy(EXACT)})
    assert pa.schema_errors(policy) == [] and pa.policy_conflicts(policy) == []
    ok, _ = pa.eligible_routes(policy, 'entry', [t2v_row(), GROK_ROW])
    assert [(r['provider'], r['model']) for r in ok] == [('openart_cli', 'pixverseV6')]
    assert pa._intent_allows(policy, 'entry', 'openart_cli', 'pixverseV6')
    assert not pa._intent_allows(policy, 'entry', 'grok_cli', None)
    auto = unknown_policy(model_selection_intents={'entry': {'mode': 'auto', 'approved_pool': EXACT['approved_pool']}})
    assert {r['provider'] for r in pa.eligible_routes(auto, 'entry', [t2v_row(), GROK_ROW])[0]} == {'openart_cli', 'grok_cli'}


@pytest.mark.parametrize('intents', [
    {'other-shot': EXACT},
    {'entry': dict(EXACT, approved_pool=[{'provider': 'openart_cli', 'model': 'kling-3'}], model='kling-3')},
    {'entry': {'mode': 'auto', 'approved_pool': [{'provider': 'grok_cli', 'model': 'x'}]}},
    {'entry': {'mode': 'auto', 'approved_pool': [{'provider': 'openart_cli', 'model': 'pixverseV6', 'tool': 'grok_cli_video'}]}},
    {'entry': {'mode': 'lock', 'approved_pool': EXACT['approved_pool']}},
])
def test_invalid_or_out_of_policy_intents_conflict(intents):
    policy = unknown_policy(model_selection_intents=intents)
    assert pa.schema_errors(policy) or pa.policy_conflicts(policy)


def test_preferred_tool_is_route_plumbing_only_when_canonical(tmp_path):
    policy, _ = install(tmp_path, make_policy())
    from tests.lib.test_production_autonomy import retained_fixture
    base = retained_fixture(tmp_path)['entry']['planned_request_template']['inputs']
    inputs = dict(copy.deepcopy(base), project_dir=str(tmp_path), duration=8)
    inputs['image_path'] = str(tmp_path / 'assets/start.svg')
    pa._validate_request_delta(tmp_path, policy, 'entry', dict(inputs), 'grok_cli')
    pa._validate_request_delta(tmp_path, policy, 'entry',
                               dict(inputs, preferred_tool='grok_cli_video', hosting_provider='grok_cli'), 'grok_cli')
    for bad in ({'preferred_tool': 'openart_cli_video'}, {'preferred_tool': 'fal_video'},
                {'hosting_provider': 'fal'}, {'preferred_provider': 'openart_cli'}):
        with pytest.raises(pa.AutonomyError):
            pa._validate_request_delta(tmp_path, policy, 'entry', dict(inputs, **bad), 'grok_cli')


def test_installed_unknown_policy_activates_and_revocation_drift_is_strict(tmp_path):
    policy, sha = install(tmp_path, unknown_policy())
    loaded, loaded_sha, _ = pa.require_active_policy(tmp_path)
    assert loaded_sha == sha and pa._is_unknown(pa._openart_spec(loaded))
    log = json.loads((tmp_path / 'artifacts/decision_log.json').read_text())
    log['decisions'][0]['user_approved'] = False
    (tmp_path / 'artifacts/decision_log.json').write_text(json.dumps(log))
    with pytest.raises(pa.AutonomyError, match='Strict'):
        pa.require_active_policy(tmp_path)


def test_policy_evidence_drift_changes_digest_and_is_strict(tmp_path):
    policy, sha = install(tmp_path, unknown_policy())
    drifted = json.loads((tmp_path / 'artifacts/autonomy_policy.json').read_text())
    drifted['providers'][0]['routes'].append({'model': 'kling-3', 'mode': 'text2video'})
    (tmp_path / 'artifacts/autonomy_policy.json').write_text(json.dumps(drifted))
    with pytest.raises(pa.AutonomyError, match='Strict'):
        pa.require_active_policy(tmp_path)


def test_existing_priced_policy_does_not_permit_unknown_mode(tmp_path):
    install(tmp_path, make_policy())
    with pytest.raises(pa.AutonomyError, match='unavailable'):
        pa._scope_material(tmp_path, {'unknown_cost_evidence_id': 'e', 'governance': {'shot_id': 'entry'}}, 'openart_cli')


def test_unknown_policy_refuses_exact_credit_fields_and_missing_evidence(tmp_path):
    install(tmp_path, unknown_policy())
    base = {'governance': {'shot_id': 'entry'}, 'model': 'pixverseV6', 'mode': 'text2video'}
    with pytest.raises(pa.AutonomyError, match='exact-credit'):
        pa._scope_material(tmp_path, dict(base, credit_quote_id='q', unknown_cost_evidence_id='e'), 'openart_cli')
    with pytest.raises(pa.AutonomyError, match='unknown_cost_evidence_id'):
        pa._scope_material(tmp_path, base, 'openart_cli')
    with pytest.raises(pa.AutonomyError, match='OpenArt-only'):
        pa._scope_material(tmp_path, dict(base, unknown_cost_evidence_id='e'), 'grok_cli')
