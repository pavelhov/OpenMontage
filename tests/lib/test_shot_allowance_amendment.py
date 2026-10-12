"""Retained exact allowance records never grant a second original dispatch."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from lib import production_autonomy as pa, production_execution as execution
from lib import episode_production_controls as controls
from lib.shot_contract import project_review_subject, shot_review_subject
from tests.lib.test_production_factory import creator_fixture, file_bytes, standing_fixture, write
from tests.lib.test_episode_production_controls import approval


PAYLOAD = {'max_generations_per_shot': 2, 'alternate_repair': 'disabled',
           'access_fallback': False, 'first_cut': {'enabled': False}}


def fixture(root, *, two_shots=True):
    kwargs = standing_fixture(root)
    kwargs['policy']['caps'] = {'max_total_attempts': 8, 'max_attempts_per_shot': 2,
                                'max_repair_attempts': 4}
    if two_shots:
        contract = execution.load_shot_contract(root)
        original = contract['shots'][0]
        contract['shots'] = [dict(copy.deepcopy(original), id=s) for s in ('S04', 'S05')]
        contract['project_review']['subject_sha256'] = project_review_subject(contract)
        for shot in contract['shots']:
            shot['review']['subject_sha256'] = shot_review_subject(contract, shot['id'])
        write(root, 'artifacts/shot_contract.json', contract)
        scenes = json.loads((root / 'artifacts/scene_plan.json').read_text())
        scenes['scenes'] = [dict(copy.deepcopy(scenes['scenes'][0]), id=s) for s in ('S04', 'S05')]
        write(root, 'artifacts/scene_plan.json', scenes)
        original_base = kwargs['baselines']['entry']
        kwargs['baselines'] = {}
        for shot in contract['shots']:
            baseline = copy.deepcopy(original_base)
            baseline['projection'] = pa.current_projection(root, shot['id'])
            baseline['dialogue_coverage'] = pa.dialogue_occurrences(baseline['projection'])
            baseline['approval_plan_sha256'] = execution.approval_plan_digest(contract, shot['id'])
            kwargs['baselines'][shot['id']] = baseline
        kwargs['policy']['shots'] = {s: copy.deepcopy(kwargs['policy']['shots']['entry']) for s in ('S04', 'S05')}
    setup = json.loads((root / kwargs['standing_setup']['path']).read_text())
    setup.pop('authorization')
    setup['caps'] = copy.deepcopy(kwargs['policy']['caps'])
    setup['episode_controls'] = copy.deepcopy(PAYLOAD)
    # Unrelated image authority must stay frozen even though this fixture has no board work.
    setup['native_images'] = {'account': 'SYNTHETIC-host', 'image_allowance': 36,
                              'allowed_image_routes': ['native_imagegen']}
    kwargs['standing_setup'] = creator_fixture(root, 'standing-eight-controls', setup)
    command = json.loads((root / kwargs['run_command']['path']).read_text())
    command.pop('authorization')
    command['standing_setup_sha256'] = kwargs['standing_setup']['sha256']
    kwargs['run_command'] = creator_fixture(root, 'command-eight-controls', command)
    return kwargs, pa.activate_standing_run(root, **kwargs)


def successor(root, predecessor, *, limits=None, name='S04-three', change=None):
    setup = json.loads((root / predecessor['path']).read_text())
    setup.pop('authorization')
    setup['caps']['max_attempts_by_shot'] = {'S04': 3} if limits is None else limits
    if change:
        change(setup)
    return creator_fixture(root, name, setup)


def amend(root, activation, predecessor, candidate):
    return pa.amend_standing_allowance(root, expected_policy_sha256=activation['policy_sha256'],
        expected_setup_sha256=predecessor['sha256'], standing_setup=candidate)


def retain_saved_shot_allowance(root, activation, predecessor, candidate, *, shot_id='S04'):
    """Restore synthetic saved history directly; never invoke the retired writer."""
    previous = json.loads((root / predecessor['path']).read_text())
    saved = json.loads((root / candidate['path']).read_text())
    changes = [{'field': 'caps.max_attempts_by_shot', 'shot_id': shot_id,
        'before': previous['caps'].get('max_attempts_by_shot', {}).get(
            shot_id, previous['caps']['max_attempts_per_shot']),
        'after': saved['caps']['max_attempts_by_shot'][shot_id]}]
    record = {'version': '1.0', 'kind': 'standing_allowance_amendment',
        'policy_sha256': activation['policy_sha256'], 'predecessor_setup': predecessor,
        'standing_setup': candidate, 'changes': changes}
    binding = write(root, 'approvals/SYNTHETIC-retained-shot-allowance.json', record)
    path = root / 'production_scopes.json'
    scopes = json.loads(path.read_text()) if path.exists() else {'version': '1.0', 'scopes': []}
    scopes.setdefault('standing_allowance_amendments', []).append(binding)
    write(root, 'production_scopes.json', scopes)
    return {'amendment': binding, 'changes': changes}


def test_retained_shot_increase_preserves_history_without_expanding_generation(tmp_path, monkeypatch):
    kwargs, activated = fixture(tmp_path)
    policy, sha, decision = pa.require_active_policy(tmp_path)
    candidate = successor(tmp_path, kwargs['standing_setup'])
    before = file_bytes(tmp_path)
    assert controls.effective_controls(tmp_path) == {**PAYLOAD, 'max_generations_per_shot': 1,
        'restricted_providers': [],
        'head_sha256': controls.effective_controls(tmp_path)['head_sha256'], 'records': 1}
    assert file_bytes(tmp_path) == before
    result = retain_saved_shot_allowance(tmp_path, activated, kwargs['standing_setup'], candidate)
    assert result['changes'] == [{'field': 'caps.max_attempts_by_shot', 'shot_id': 'S04',
                                 'before': 2, 'after': 3}]
    assert pa.require_active_policy(tmp_path) == (policy, sha, decision)
    assert pa.effective_standing_caps(tmp_path, policy) == dict(policy['caps'], max_attempts_by_shot={'S04': 3})
    for path, content in before.items():
        assert (tmp_path / path).read_bytes() == content
    current = controls.effective_controls(tmp_path)
    assert current['max_generations_per_shot'] == 1
    assert 'max_generations_by_shot' not in current
    assert current['restricted_providers'] == []
    usage = {'total': 4, 'per_shot': {'S04': 2, 'S05': 2}, 'repair': {},
             'occurrences': [{'shot_id': s, 'attempt_id': f'{s}-{i}', 'counted': True,
                              'state': 'failed', 'provider': 'openart_mcp', 'model': 'SYNTHETIC',
                              'phase': 'first_pass'} for s in ('S04', 'S05') for i in range(2)],
             'pending': [], 'unresolved_originals': [], 'excluded_never_submitted': []}
    monkeypatch.setattr(controls, 'generation_usage', lambda *_: copy.deepcopy(usage))
    for shot in ('S04', 'S05'):
        for provider in ('openart_mcp', 'grok_cli'):
            refused = controls.generation_admission(tmp_path, shot_id=shot,
                provider=provider, purpose='first_pass')
            assert not refused['admitted']
            assert any('episode generation limit reached' in reason for reason in refused['reasons'])
    status = controls.episode_production_status(tmp_path)
    assert status['generation_caps']['S04']['controls_limit'] == 1
    assert status['generation_caps']['S05']['controls_limit'] == 1
    # Uncertain originals remain counted and keep blocking duplicates.
    usage['pending'] = usage['unresolved_originals'] = ['S04-1']
    usage['occurrences'][1]['state'] = 'uncertain'
    decision = controls.generation_admission(tmp_path, shot_id='S04', provider='openart_mcp', purpose='first_pass')
    assert not decision['admitted'] and any('generation limit reached' in r for r in decision['reasons'])
    begin = controls.generation_admission(tmp_path, shot_id='S04', provider='openart_mcp',
                                         purpose='mcp_begin', exclude_attempt_id='S04-third')
    assert not begin['admitted'] and any('generation limit reached' in r for r in begin['reasons'])
    usage['per_shot']['S04'] = 3
    usage['occurrences'].append(dict(usage['occurrences'][0], attempt_id='S04-third'))
    assert not controls.generation_admission(tmp_path, shot_id='S04', provider='openart_mcp', purpose='mcp_begin')['admitted']
    frozen = file_bytes(tmp_path)
    assert pa.effective_standing_caps(tmp_path, policy)['max_attempts_by_shot'] == {'S04': 3}
    assert file_bytes(tmp_path) == frozen
    assert usage['occurrences'][1]['state'] == 'uncertain'


def test_fresh_named_shot_expansion_is_refused_without_writing(tmp_path):
    kwargs, activated = fixture(tmp_path)
    candidate = successor(tmp_path, kwargs['standing_setup'])
    frozen = file_bytes(tmp_path)
    with pytest.raises(pa.StandingSetupRequired):
        amend(tmp_path, activated, kwargs['standing_setup'], candidate)
    assert file_bytes(tmp_path) == frozen


def test_retained_named_shot_amendment_never_replays_through_current_writer(tmp_path):
    kwargs, activated = fixture(tmp_path)
    candidate = successor(tmp_path, kwargs['standing_setup'])
    retain_saved_shot_allowance(tmp_path, activated, kwargs['standing_setup'], candidate)
    frozen = file_bytes(tmp_path)
    with pytest.raises(pa.StandingSetupRequired):
        amend(tmp_path, activated, kwargs['standing_setup'], candidate)
    assert file_bytes(tmp_path) == frozen


@pytest.mark.parametrize('limits', [{'S04': True}, {'S04': 3.0}, {'S04': 0}, {'S04': 1},
                                   {'S04': 2}, {'S04': 9}, {'UNKNOWN': 3}, [], None])
def test_unknown_or_invalid_exact_shot_limits_write_nothing(tmp_path, limits):
    kwargs, activated = fixture(tmp_path)
    candidate = successor(tmp_path, kwargs['standing_setup'], change=(lambda s: s['caps'].update(max_attempts_by_shot=limits)))
    frozen = file_bytes(tmp_path)
    with pytest.raises(pa.StandingSetupRequired):
        amend(tmp_path, activated, kwargs['standing_setup'], candidate)
    assert file_bytes(tmp_path) == frozen


@pytest.mark.parametrize('change', [lambda s: s['caps'].update(max_attempts_per_shot=3),
    lambda s: s['caps'].update(max_repair_attempts=5),
    lambda s: s['episode_controls'].update(max_generations_per_shot=3),
    lambda s: s['episode_controls'].update(access_fallback=True),
    lambda s: s['provider_accounts'].update(grok_cli='different-account'),
    lambda s: s['providers'][0].update(id='different-route'),
    lambda s: s['approved_source_sha256'].append('0' * 64),
    lambda s: s['native_images'].update(image_allowance=37),
    lambda s: s['flex'].clear(), lambda s: s['checkpoint_stages'].clear()])
def test_shot_amendment_cannot_expand_other_protected_terms(tmp_path, change):
    kwargs, activated = fixture(tmp_path)
    candidate = successor(tmp_path, kwargs['standing_setup'], change=change)
    frozen = file_bytes(tmp_path)
    with pytest.raises(pa.StandingSetupRequired, match='changes only'):
        amend(tmp_path, activated, kwargs['standing_setup'], candidate)
    assert file_bytes(tmp_path) == frozen


def test_source_only_entry_never_admits_or_replays_shot_increase(tmp_path):
    kwargs, activated = fixture(tmp_path)
    candidate = successor(tmp_path, kwargs['standing_setup'])
    call = lambda: pa.amend_standing_source_max_batches(tmp_path,
        expected_policy_sha256=activated['policy_sha256'], expected_setup_sha256=kwargs['standing_setup']['sha256'],
        standing_setup=candidate)
    with pytest.raises(pa.StandingSetupRequired, match='changes only max_batches'):
        call()
    retain_saved_shot_allowance(tmp_path, activated, kwargs['standing_setup'], candidate)
    with pytest.raises(pa.StandingSetupRequired, match='changes only max_batches'):
        call()


def test_shot_successor_cannot_decrease_drop_or_use_stale_predecessor(tmp_path):
    kwargs, activated = fixture(tmp_path)
    first = successor(tmp_path, kwargs['standing_setup'])
    retain_saved_shot_allowance(tmp_path, activated, kwargs['standing_setup'], first)
    for limits in ({}, {'S04': 2}):
        candidate = successor(tmp_path, first, name='decrease-' + str(len(limits)), limits=limits)
        with pytest.raises(pa.StandingSetupRequired):
            amend(tmp_path, activated, first, candidate)
    candidate = successor(tmp_path, first, name='four', limits={'S04': 4})
    with pytest.raises(pa.StandingSetupRequired, match='stale'):
        amend(tmp_path, activated, kwargs['standing_setup'], candidate)
    with pytest.raises(pa.StandingSetupRequired, match='policy SHA'):
        amend(tmp_path, {'policy_sha256': '0' * 64}, first, candidate)


@pytest.mark.parametrize('tamper', ['authorization', 'successor', 'record', 'activation'])
def test_revoked_or_tampered_shot_authority_keeps_controls_narrow_and_blocks_policy(tmp_path, tamper):
    kwargs, activated = fixture(tmp_path)
    candidate = successor(tmp_path, kwargs['standing_setup'])
    result = retain_saved_shot_allowance(tmp_path, activated, kwargs['standing_setup'], candidate)
    if tamper == 'authorization':
        setup = json.loads((tmp_path / candidate['path']).read_text())
        write(tmp_path, setup['authorization']['path'], {'revoked': True})
    elif tamper == 'successor':
        write(tmp_path, candidate['path'], {})
    elif tamper == 'record':
        path = tmp_path / result['amendment']['path']; path.chmod(0o644)
        write(tmp_path, result['amendment']['path'], {})
    else:
        log = json.loads((tmp_path / 'artifacts/decision_log.json').read_text())
        log['decisions'][-1]['user_approved'] = False
        write(tmp_path, 'artifacts/decision_log.json', log)
    assert pa.load_active_policy(tmp_path)[0] is None
    assert controls.effective_controls(tmp_path)['max_generations_per_shot'] == 1
    with pytest.raises(pa.AutonomyError):
        pa.require_active_policy(tmp_path)
    with pytest.raises(pa.AutonomyError):
        amend(tmp_path, activated, kwargs['standing_setup'], candidate)


def test_later_controls_and_provider_restrictions_remain_owned(tmp_path):
    kwargs, activated = fixture(tmp_path)
    candidate = successor(tmp_path, kwargs['standing_setup'])
    retain_saved_shot_allowance(tmp_path, activated, kwargs['standing_setup'], candidate)
    controls.restrict_episode_provider(tmp_path, ['grok_cli'], evidence=approval(tmp_path), reason='SYNTHETIC restriction')
    effective = controls.effective_controls(tmp_path)
    assert 'max_generations_by_shot' not in effective
    assert effective['max_generations_per_shot'] == 1
    assert effective['restricted_providers'] == ['grok_cli']
    controls.record_episode_controls(tmp_path, dict(PAYLOAD, max_generations_per_shot=1),
                                    evidence=approval(tmp_path, 'narrowed'))
    effective = controls.effective_controls(tmp_path)
    assert 'max_generations_by_shot' not in effective
    assert effective['max_generations_per_shot'] == 1
    assert effective['restricted_providers'] == ['grok_cli']


def test_parallel_saved_shot_readers_are_immutable(tmp_path):
    kwargs, activated = fixture(tmp_path)
    candidate = successor(tmp_path, kwargs['standing_setup'])
    retain_saved_shot_allowance(tmp_path, activated, kwargs['standing_setup'], candidate)
    policy = pa.require_active_policy(tmp_path)[0]
    frozen = file_bytes(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: pa.effective_standing_caps(tmp_path, policy), range(2)))
    assert results == [dict(policy['caps'], max_attempts_by_shot={'S04': 3})] * 2
    assert len(json.loads((tmp_path / 'production_scopes.json').read_text())['standing_allowance_amendments']) == 1
    assert file_bytes(tmp_path) == frozen


def test_retained_shot_increase_never_reopens_consumed_original_scope(tmp_path, monkeypatch):
    from tests.lib import test_production_factory as factory
    from tests.lib.test_factory_source_batches import cap_kwargs
    def declared(root):
        kwargs = cap_kwargs(root)
        kwargs['policy']['caps']['max_repair_attempts'] = 4
        setup = json.loads((root / kwargs['standing_setup']['path']).read_text())
        setup.pop('authorization')
        setup['caps']['max_repair_attempts'] = 4
        setup['episode_controls'] = copy.deepcopy(PAYLOAD)
        kwargs['standing_setup'] = creator_fixture(root, 'SYNTHETIC-scope-controls', setup)
        command = json.loads((root / kwargs['run_command']['path']).read_text())
        command.pop('authorization')
        command['standing_setup_sha256'] = kwargs['standing_setup']['sha256']
        kwargs['run_command'] = creator_fixture(root, 'SYNTHETIC-scope-command', command)
        return kwargs
    with monkeypatch.context() as patch:
        patch.setattr(factory, 'standing_fixture', declared)
        _, inputs, observation = factory.grok_final_fixture(tmp_path)
    policy, sha, decision_id = pa.require_active_policy(tmp_path)
    activated = {'policy_sha256': sha}
    predecessor = policy['activation_basis']['standing_setup']
    candidate = successor(tmp_path, predecessor, limits={'entry': 3})
    counts = {'total': 2, 'per_shot': {'entry': 2}, 'repair': 1, 'unmapped_outbox': []}
    usage = {'occurrences': [{'counted': True, 'shot_id': 'entry', 'attempt_id': str(i),
                              'provider': 'grok_cli', 'model': None} for i in range(2)],
             'pending': []}
    monkeypatch.setattr(pa, 'root_attempt_counts', lambda *args, **kwargs: copy.deepcopy(counts))
    monkeypatch.setattr(controls, 'generation_usage', lambda *args, **kwargs: copy.deepcopy(usage))
    with pytest.raises(pa.AutonomyError, match='generation limit reached'):
        pa.derive_scope(tmp_path, inputs, provider='grok_cli', observation=observation)
    retain_saved_shot_allowance(tmp_path, activated, predecessor, candidate, shot_id='entry')
    frozen = file_bytes(tmp_path)
    original_counts = copy.deepcopy(counts)
    original_usage = copy.deepcopy(usage)
    with pytest.raises(pa.AutonomyError, match='generation limit reached'):
        pa.derive_scope(tmp_path, inputs, provider='grok_cli', observation=observation)
    assert file_bytes(tmp_path) == frozen
    assert counts == original_counts and usage == original_usage
    assert pa.require_active_policy(tmp_path) == (policy, sha, decision_id)
    assert pa.effective_standing_caps(tmp_path, policy)['max_attempts_by_shot'] == {'entry': 3}
    assert controls.effective_controls(tmp_path)['max_generations_per_shot'] == 1
