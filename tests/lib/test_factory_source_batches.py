"""Narrow standing allowance successors; no provider or host calls."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from lib import production_autonomy as pa, production_execution as execution
from tests.lib.test_production_factory import (creator_fixture, file_bytes, standing_fixture, write,
    board_factory_fixture, host_receipt)
from tests.lib.test_production_request import neutral_package  # noqa: F401


def successor(root, setup_binding, *, name='source-successor', limit=36, change=None, provider='grok_cli'):
    setup = json.loads((root / setup_binding['path']).read_text())
    setup.pop('authorization')
    next(term for term in setup['source_transfer'] if term['provider'] == provider)['max_batches'] = limit
    if change:
        change(setup)
    return creator_fixture(root, name, setup)


def amend(root, activation, predecessor, candidate):
    return pa.amend_standing_source_max_batches(root,
        expected_policy_sha256=activation['policy_sha256'],
        expected_setup_sha256=predecessor['sha256'], standing_setup=candidate)


def test_source_batch_successor_appends_preserves_activation_and_replays_after_progress(tmp_path):
    kwargs = standing_fixture(tmp_path)
    activation = pa.activate_standing_run(tmp_path, **kwargs)
    # The existing sidecar includes immutable authority: preserve every existing key.
    scopes = {'version': '1.0', 'scopes': [], 'retained_original_marker': 'SYNTHETIC'}
    write(tmp_path, 'production_scopes.json', scopes)
    candidate = successor(tmp_path, kwargs['standing_setup'])
    frozen = file_bytes(tmp_path)
    result = amend(tmp_path, activation, kwargs['standing_setup'], candidate)
    assert result['provider_calls'] == 0 and result['replayed'] is False
    for path, content in frozen.items():
        if path.name != 'production_scopes.json':
            assert (tmp_path / path).read_bytes() == content
    retained = json.loads((tmp_path / 'production_scopes.json').read_text())
    assert {key: retained[key] for key in scopes} == scopes
    active, sha, decision_id = pa.require_active_policy(tmp_path)
    assert (sha, decision_id) == (activation['policy_sha256'], activation['decision_id'])
    assert next(t for t in pa.effective_standing_setup(tmp_path, active)['source_transfer'] if t['provider'] == 'grok_cli')['max_batches'] == 36
    assert all(t['max_batches'] == 3 for t in json.loads((tmp_path / kwargs['standing_setup']['path']).read_text())['source_transfer'])
    # Independent progress is neither reset nor rewritten on replay.
    write(tmp_path, 'notes/next-progress.json', {'SYNTHETIC': 'generation and selection progress'})
    before = file_bytes(tmp_path)
    assert amend(tmp_path, activation, kwargs['standing_setup'], candidate) == dict(result, replayed=True)
    assert file_bytes(tmp_path) == before


@pytest.mark.parametrize('change', [
    lambda s: s['provider_accounts'].update(grok_cli='different-account'),
    lambda s: s['providers'][0].update(id='different-provider'),
    lambda s: s['caps'].update(max_total_attempts=100),
    lambda s: s['approved_source_sha256'].append('0' * 64),
    lambda s: s['required_permissions'].append({'path': 'new-rights', 'sha256': '0' * 64}),
    lambda s: s['source_transfer'][0].update(max_files=100),
    lambda s: s['source_transfer'][0].update(max_file_bytes=100000),
    lambda s: s['source_transfer'][0].update(max_batch_bytes=100000),
    lambda s: s['source_transfer'][0].update(roles=['start_frame']),
    lambda s: s['source_transfer'][0].update(generated_board_sources=True),
    lambda s: s['flex'].clear(),
    lambda s: s.update(enabled=False),
    lambda s: s.update(native_images={'image_allowance': 100}),
    lambda s: s['checkpoint_stages'].clear(),
    lambda s: s.update(submitted_shot_rewrites={'entry': 'different'}),
])
def test_source_batch_successor_rejects_unrelated_expansion_or_dropped_protections(tmp_path, change):
    kwargs = standing_fixture(tmp_path)
    activation = pa.activate_standing_run(tmp_path, **kwargs)
    candidate = successor(tmp_path, kwargs['standing_setup'], change=change)
    before = file_bytes(tmp_path)
    with pytest.raises(pa.StandingSetupRequired, match='changes only max_batches'):
        amend(tmp_path, activation, kwargs['standing_setup'], candidate)
    assert file_bytes(tmp_path) == before


@pytest.mark.parametrize('limit', [0, -1, True, 1, 3, 36.0])
def test_source_batch_successor_never_resets_or_reduces_allowance(tmp_path, limit):
    kwargs = standing_fixture(tmp_path)
    activation = pa.activate_standing_run(tmp_path, **kwargs)
    candidate = successor(tmp_path, kwargs['standing_setup'], limit=limit)
    before = file_bytes(tmp_path)
    with pytest.raises(pa.StandingSetupRequired):
        amend(tmp_path, activation, kwargs['standing_setup'], candidate)
    assert file_bytes(tmp_path) == before


def test_source_batch_successor_exact_chain_rejects_stale_predecessor_and_policy(tmp_path):
    kwargs = standing_fixture(tmp_path)
    activation = pa.activate_standing_run(tmp_path, **kwargs)
    first = successor(tmp_path, kwargs['standing_setup'])
    amend(tmp_path, activation, kwargs['standing_setup'], first)
    second = successor(tmp_path, first, name='second-successor', limit=40)
    before = file_bytes(tmp_path)
    with pytest.raises(pa.StandingSetupRequired, match='stale'):
        amend(tmp_path, activation, kwargs['standing_setup'], second)
    with pytest.raises(pa.StandingSetupRequired, match='policy SHA'):
        amend(tmp_path, {'policy_sha256': '0' * 64}, first, second)
    assert file_bytes(tmp_path) == before
    amend(tmp_path, activation, first, second)
    policy, _, _ = pa.require_active_policy(tmp_path)
    assert next(t for t in pa.effective_standing_setup(tmp_path, policy)['source_transfer'] if t['provider'] == 'grok_cli')['max_batches'] == 40


@pytest.mark.parametrize('change', ['authorization', 'record', 'successor', 'revoked'])
def test_source_batch_successor_changed_or_revoked_authority_fails_closed(tmp_path, change):
    kwargs = standing_fixture(tmp_path)
    activation = pa.activate_standing_run(tmp_path, **kwargs)
    candidate = successor(tmp_path, kwargs['standing_setup'])
    result = amend(tmp_path, activation, kwargs['standing_setup'], candidate)
    setup = json.loads((tmp_path / candidate['path']).read_text())
    if change == 'authorization':
        write(tmp_path, setup['authorization']['path'], {'revoked': True})
    elif change == 'record':
        record = json.loads((tmp_path / result['amendment']['path']).read_text())
        record['changes'][0]['after'] = 100
        (tmp_path / result['amendment']['path']).chmod(0o644)
        write(tmp_path, result['amendment']['path'], record)
    elif change == 'successor':
        setup['source_transfer'][0]['max_batches'] = 100
        write(tmp_path, candidate['path'], setup)
    else:
        log = json.loads((tmp_path / 'artifacts/decision_log.json').read_text())
        log['decisions'][-1]['user_approved'] = False
        write(tmp_path, 'artifacts/decision_log.json', log)
    assert pa.load_active_policy(tmp_path)[0] is None
    before = file_bytes(tmp_path)
    with pytest.raises(pa.AutonomyError):
        amend(tmp_path, activation, kwargs['standing_setup'], candidate)
    assert file_bytes(tmp_path) == before


def test_source_batch_successor_grok_transfer_keeps_cumulative_original_count(tmp_path, monkeypatch):
    kwargs = standing_fixture(tmp_path)
    activation = pa.activate_standing_run(tmp_path, **kwargs)
    candidate = successor(tmp_path, kwargs['standing_setup'])
    inputs = {'governance': {'shot_id': 'entry'}, 'image_path': str(tmp_path / 'assets/start.svg')}
    originals = [{'scope_id': 'original-' + str(i), 'scope': {'provider': 'grok_cli'},
        'submitted_inputs': copy.deepcopy(inputs)} for i in range(3)]
    monkeypatch.setattr(execution, '_attempts', lambda root: copy.deepcopy(originals))
    policy, _, _ = pa.require_active_policy(tmp_path)
    with pytest.raises(pa.StandingSetupRequired, match='exhausted'):
        pa._validate_standing_transfer(tmp_path, policy, inputs, 'grok_cli')
    amend(tmp_path, activation, kwargs['standing_setup'], candidate)
    pa._validate_standing_transfer(tmp_path, policy, inputs, 'grok_cli')
    assert execution._attempts(tmp_path) == originals
    originals.extend(copy.deepcopy(originals[0]) for _ in range(33))
    with pytest.raises(pa.StandingSetupRequired, match='exhausted'):
        pa._validate_standing_transfer(tmp_path, policy, inputs, 'grok_cli')


def test_source_batch_successor_interrupted_append_reuses_exact_retained_record(tmp_path, monkeypatch):
    from lib import production_images as images
    kwargs = standing_fixture(tmp_path)
    activation = pa.activate_standing_run(tmp_path, **kwargs)
    candidate = successor(tmp_path, kwargs['standing_setup'])
    with monkeypatch.context() as patch:
        patch.setattr(images, '_write_image_scopes', lambda *args: (_ for _ in ()).throw(OSError('SYNTHETIC interruption')))
        with pytest.raises(OSError, match='interruption'):
            amend(tmp_path, activation, kwargs['standing_setup'], candidate)
    records = list((tmp_path / 'approvals').glob('source-max-batches-*.json'))
    assert len(records) == 1
    frozen = records[0].read_bytes()
    result = amend(tmp_path, activation, kwargs['standing_setup'], candidate)
    assert records[0].read_bytes() == frozen
    before = file_bytes(tmp_path)
    assert amend(tmp_path, activation, kwargs['standing_setup'], candidate) == dict(result, replayed=True)
    assert file_bytes(tmp_path) == before


def test_source_batch_successor_serializes_concurrent_exact_replay(tmp_path):
    kwargs = standing_fixture(tmp_path)
    activation = pa.activate_standing_run(tmp_path, **kwargs)
    candidate = successor(tmp_path, kwargs['standing_setup'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: amend(tmp_path, activation, kwargs['standing_setup'], candidate), range(2)))
    assert sorted(result['replayed'] for result in results) == [False, True]
    assert len(json.loads((tmp_path / 'production_scopes.json').read_text())['source_batch_amendments']) == 1
    assert len(list((tmp_path / 'approvals').glob('source-max-batches-*.json'))) == 1


@pytest.mark.parametrize('revoke_successor', [False, True])
@pytest.mark.parametrize('allowance_kind', ['source', 'total'])
def test_source_batch_successor_keeps_emitted_original_image_collectable(neutral_package, revoke_successor, allowance_kind):
    from lib import production_boards as boards, production_images as images
    root = board_factory_fixture(neutral_package)
    policy, sha, _ = pa.require_active_policy(root)
    prepared = boards.prepare_image_request(root, 'payoff', images=images)
    scope = images.derive_native_image_scope(root, prepared['packet'], 'payoff', prepared['arguments'],
        output_path=prepared['output_path'])
    boards.reserve_board_image(root, 'payoff', scope_id=scope, call_id='SYNTHETIC-original', images=images)
    envelope = images.begin_native_image(root, 'SYNTHETIC-original')
    retained_envelope = json.loads((root / 'production_scopes.json').read_text())['image_envelope']
    usage = images.image_usage(root)
    assert usage['total'] == 1
    predecessor = policy['activation_basis']['standing_setup']
    if allowance_kind == 'source':
        candidate = successor(root, predecessor, provider='native_imagegen')
        amend(root, {'policy_sha256': sha}, predecessor, candidate)
    else:
        candidate = cap_successor(root, predecessor, total=policy['caps']['max_total_attempts'] + 1)
        amend_allowance(root, {'policy_sha256': sha}, predecessor, candidate)
    assert images.image_usage(root) == usage
    if revoke_successor:
        setup = json.loads((root / candidate['path']).read_text())
        write(root, setup['authorization']['path'], {'revoked': True})
        assert pa.load_active_policy(root)[0] is None
    imported = images.import_native_image(root, 'SYNTHETIC-original',
        host_receipt=host_receipt(root, envelope, name='SYNTHETIC-original-return'))
    assert imported['state'] == 'imported'
    assert images.image_usage(root)['total'] == 1
    assert json.loads((root / 'production_scopes.json').read_text())['image_envelope'] == retained_envelope
    assert images.import_native_image(root, 'SYNTHETIC-original',
        host_receipt=host_receipt(root, envelope, name='SYNTHETIC-original-return')) == imported


def cap_kwargs(root):
    kwargs = standing_fixture(root)
    kwargs['policy']['caps']['max_total_attempts'] = 8
    setup = json.loads((root / kwargs['standing_setup']['path']).read_text()); setup.pop('authorization')
    setup['caps']['max_total_attempts'] = 8
    kwargs['standing_setup'] = creator_fixture(root, 'standing-eight', setup)
    command = json.loads((root / kwargs['run_command']['path']).read_text()); command.pop('authorization')
    command['standing_setup_sha256'] = kwargs['standing_setup']['sha256']
    kwargs['run_command'] = creator_fixture(root, 'command-eight', command)
    return kwargs


def cap_fixture(root):
    kwargs = cap_kwargs(root)
    return kwargs, pa.activate_standing_run(root, **kwargs)


def cap_successor(root, predecessor, *, name='total-nine', total=9, change=None):
    setup = json.loads((root / predecessor['path']).read_text()); setup.pop('authorization')
    setup['caps']['max_total_attempts'] = total
    if change:
        change(setup)
    return creator_fixture(root, name, setup)


def amend_allowance(root, activation, predecessor, candidate):
    return pa.amend_standing_allowance(root, expected_policy_sha256=activation['policy_sha256'],
        expected_setup_sha256=predecessor['sha256'], standing_setup=candidate)


def test_total_allowance_five_counted_including_failed_plus_four_remaining_requires_nine(tmp_path):
    kwargs, activation = cap_fixture(tmp_path)
    policy, _, _ = pa.require_active_policy(tmp_path)
    # Terminal failed submission remains one of five counted originals.
    originals = [{'attempt_id': 'original-' + str(i), 'shot_id': 'S0' + str(i + 1),
        'phase': 'first_pass', 'status': 'failed' if i == 4 else 'generated'} for i in range(5)]
    assert pa.attempt_counts(originals)['total'] == 5
    candidate = cap_successor(tmp_path, kwargs['standing_setup'])
    for index in range(3):
        pa.check_caps(policy, 'remaining-' + str(index), pa.attempt_counts(originals), 'first_pass', root=tmp_path)
        originals.append({'attempt_id': 'new-' + str(index), 'shot_id': 'remaining-' + str(index), 'phase': 'first_pass'})
    counts = pa.attempt_counts(originals)
    assert counts['total'] == 8
    # Prepared exact human evidence alone cannot admit the ninth attempt.
    before = file_bytes(tmp_path)
    with pytest.raises(pa.AutonomyError, match='total attempts exhausted'):
        pa.check_caps(policy, 'final', counts, 'first_pass', root=tmp_path)
    assert file_bytes(tmp_path) == before
    result = amend_allowance(tmp_path, activation, kwargs['standing_setup'], candidate)
    assert result['changes'] == [{'field': 'caps.max_total_attempts', 'before': 8, 'after': 9}]
    assert result['provider_calls'] == 0
    for path, content in before.items():
        assert (tmp_path / path).read_bytes() == content
    assert pa.require_active_policy(tmp_path) == (policy, activation['policy_sha256'], activation['decision_id'])
    assert policy['caps']['max_total_attempts'] == 8
    assert pa.effective_standing_caps(tmp_path, policy) == dict(policy['caps'], max_total_attempts=9)
    pa.check_caps(policy, 'final', counts, 'first_pass', root=tmp_path)
    originals.append({'attempt_id': 'final', 'shot_id': 'final', 'phase': 'first_pass'})
    assert pa.attempt_counts(originals)['total'] == 9
    with pytest.raises(pa.AutonomyError, match='total attempts exhausted'):
        pa.check_caps(policy, 'extra', pa.attempt_counts(originals), 'first_pass', root=tmp_path)
    assert originals[4]['status'] == 'failed'
    before = file_bytes(tmp_path)
    assert amend_allowance(tmp_path, activation, kwargs['standing_setup'], candidate) == dict(result, replayed=True)
    assert file_bytes(tmp_path) == before


def test_total_allowance_preserves_legacy_source_record_and_single_current_setup_chain(tmp_path):
    kwargs, activation = cap_fixture(tmp_path)
    source = successor(tmp_path, kwargs['standing_setup'])
    source_result = amend(tmp_path, activation, kwargs['standing_setup'], source)
    assert amend_allowance(tmp_path, activation, kwargs['standing_setup'], source) == dict(source_result, replayed=True)
    old = json.loads((tmp_path / 'production_scopes.json').read_text())
    old_bytes = (tmp_path / source_result['amendment']['path']).read_bytes()
    candidate = cap_successor(tmp_path, source)
    with pytest.raises(pa.StandingSetupRequired, match='stale'):
        amend_allowance(tmp_path, activation, kwargs['standing_setup'], candidate)
    result = amend_allowance(tmp_path, activation, source, candidate)
    current = json.loads((tmp_path / 'production_scopes.json').read_text())
    assert current['source_batch_amendments'] == old['source_batch_amendments']
    assert (tmp_path / source_result['amendment']['path']).read_bytes() == old_bytes
    policy, _, _ = pa.require_active_policy(tmp_path)
    assert pa.effective_standing_caps(tmp_path, policy)['max_total_attempts'] == 9
    assert next(t for t in pa.effective_standing_setup(tmp_path, policy)['source_transfer'] if t['provider'] == 'grok_cli')['max_batches'] == 36
    assert amend_allowance(tmp_path, activation, source, candidate) == dict(result, replayed=True)
    with pytest.raises(pa.StandingSetupRequired, match='changes only max_batches'):
        amend(tmp_path, activation, source, candidate)
    # Existing source-only public references can append safely after the new entry.
    next_source = successor(tmp_path, candidate, name='source-after-total', limit=40)
    amend(tmp_path, activation, candidate, next_source)
    assert pa.effective_standing_caps(tmp_path, policy)['max_total_attempts'] == 9
    assert next(t for t in pa.effective_standing_setup(tmp_path, policy)['source_transfer'] if t['provider'] == 'grok_cli')['max_batches'] == 40
    assert current['source_batch_amendments'] == json.loads((tmp_path / 'production_scopes.json').read_text())['source_batch_amendments']


@pytest.mark.parametrize('change', [
    lambda s: s['caps'].update(max_attempts_per_shot=3),
    lambda s: s['caps'].update(max_repair_attempts=3),
    lambda s: s['provider_accounts'].update(grok_cli='different-account'),
    lambda s: s['providers'][0].update(id='different-provider'),
    lambda s: s['approved_source_sha256'].append('0' * 64),
    lambda s: s.update(required_permissions=[{'SYNTHETIC': 'different-rights'}]),
    lambda s: s['source_transfer'][0].update(max_files=100),
    lambda s: s['source_transfer'][0].update(max_file_bytes=100000),
    lambda s: s['source_transfer'][0].update(max_batch_bytes=100000),
    lambda s: s['source_transfer'][0].update(roles=['start_frame']),
    lambda s: s['source_transfer'][0].update(generated_board_sources=True),
    lambda s: s['flex'].clear(),
    lambda s: s.update(enabled=False),
    lambda s: s.update(native_images={'image_allowance': 100}),
    lambda s: s.update(episode_controls={'access_fallback': True}),
    lambda s: s['checkpoint_stages'].clear(),
    lambda s: s.update(submitted_shot_rewrites={'entry': 'different'}),
])
def test_total_allowance_rejects_all_unrelated_authority_changes(tmp_path, change):
    kwargs, activation = cap_fixture(tmp_path)
    candidate = cap_successor(tmp_path, kwargs['standing_setup'], change=change)
    before = file_bytes(tmp_path)
    with pytest.raises(pa.StandingSetupRequired, match='changes only max_batches and max_total_attempts'):
        amend_allowance(tmp_path, activation, kwargs['standing_setup'], candidate)
    assert file_bytes(tmp_path) == before


def test_total_allowance_actual_scope_derivation_and_dispatch_replay_share_authenticated_cap(tmp_path, monkeypatch):
    from tests.lib import test_production_factory as factory
    # Existing compiled-request fixture, actual standing activation; no provider call.
    with monkeypatch.context() as patch:
        patch.setattr(factory, 'standing_fixture', cap_kwargs)
        _, inputs, observation = factory.grok_final_fixture(tmp_path)
    policy, sha, decision_id = pa.require_active_policy(tmp_path)
    activation = {'policy_sha256': sha, 'decision_id': decision_id}
    predecessor = policy['activation_basis']['standing_setup']
    candidate = cap_successor(tmp_path, predecessor)
    counted = pa.attempt_counts([{'attempt_id': 'counted-' + str(i), 'shot_id': 'earlier-' + str(i),
        'phase': 'first_pass', 'status': 'failed' if i == 4 else 'generated'} for i in range(8)])
    monkeypatch.setattr(pa, 'root_attempt_counts', lambda *args, **kwargs: copy.deepcopy(counted))
    before = file_bytes(tmp_path)
    with pytest.raises(pa.AutonomyError, match='total attempts exhausted'):
        pa.derive_scope(tmp_path, inputs, provider='grok_cli', observation=observation)
    assert file_bytes(tmp_path) == before
    amendment = amend_allowance(tmp_path, activation, predecessor, candidate)
    scope = pa.derive_scope(tmp_path, inputs, provider='grok_cli', observation=observation)
    inputs['governance']['scope_id'] = scope['id']
    assert pa.validate_derived_scope(tmp_path, scope, inputs=inputs, observation=observation) == (policy, sha, decision_id)
    retained = json.loads((tmp_path / 'production_scopes.json').read_text())
    assert retained['scopes'] == [scope]
    assert scope['evidence'] == policy['evidence']
    assert scope['derived_from_policy']['policy_sha256'] == sha
    assert retained['standing_allowance_amendments'] == [amendment['amendment']]
    frozen_scope = copy.deepcopy(retained['scopes'])
    # An additional counted original cannot bypass the same cap at dispatch replay.
    counted['total'] = 9
    with pytest.raises(pa.AutonomyError, match='total attempts exhausted'):
        pa.validate_derived_scope(tmp_path, scope, inputs=inputs, observation=observation)
    counted['total'] = 8
    setup = json.loads((tmp_path / candidate['path']).read_text())
    write(tmp_path, setup['authorization']['path'], {'revoked': True})
    with pytest.raises(pa.AutonomyError, match='Strict'):
        pa.validate_derived_scope(tmp_path, scope, inputs=inputs, observation=observation)
    assert json.loads((tmp_path / 'production_scopes.json').read_text())['scopes'] == frozen_scope


def test_total_allowance_mcp_remaining_cap_audit_uses_same_successor(tmp_path, monkeypatch):
    kwargs, activation = cap_fixture(tmp_path)
    candidate = cap_successor(tmp_path, kwargs['standing_setup'])
    amend_allowance(tmp_path, activation, kwargs['standing_setup'], candidate)
    policy, sha, decision_id = pa.require_active_policy(tmp_path)
    counts = {'total': 8, 'per_shot': {}, 'repair': 0, 'unmapped_outbox': []}
    monkeypatch.setattr(pa, 'root_attempt_counts', lambda *args, **kwargs: copy.deepcopy(counts))
    # Isolate the retained MCP audit writer; route admission is tested elsewhere.
    with execution._lock(tmp_path):
        pa._append_route_decisions_locked(tmp_path, policy, sha, decision_id, 'entry', 'openart_mcp',
            {'model': 'SYNTHETIC-approved-model', 'mode': 'image2video'})
    log = json.loads((tmp_path / 'artifacts/decision_log.json').read_text())
    entry = next(d for d in reversed(log['decisions']) if d['category'] == 'provider_selection')
    remaining = json.loads(entry['reason'].split('remaining attempt caps ', 1)[1].split(' [policy:', 1)[0])
    assert remaining == {'total': 0, 'per_shot': 0}
    assert '[policy:' + sha + ' activation:' + decision_id + ']' in entry['reason']


@pytest.mark.parametrize('total', [0, -1, True, 7, 8, 9.0])
def test_total_allowance_rejects_reset_reduction_noninteger_or_no_change(tmp_path, total):
    kwargs, activation = cap_fixture(tmp_path)
    candidate = cap_successor(tmp_path, kwargs['standing_setup'], total=total)
    before = file_bytes(tmp_path)
    with pytest.raises(pa.StandingSetupRequired):
        amend_allowance(tmp_path, activation, kwargs['standing_setup'], candidate)
    assert file_bytes(tmp_path) == before


@pytest.mark.parametrize('change', ['authorization', 'record', 'successor', 'activation', 'policy_sha', 'missing_authorization'])
def test_total_allowance_changed_revoked_stale_or_unapproved_is_strict(tmp_path, change):
    kwargs, activation = cap_fixture(tmp_path)
    candidate = cap_successor(tmp_path, kwargs['standing_setup'])
    if change == 'policy_sha':
        with pytest.raises(pa.StandingSetupRequired, match='policy SHA'):
            amend_allowance(tmp_path, {'policy_sha256': '0' * 64}, kwargs['standing_setup'], candidate)
        return
    setup = json.loads((tmp_path / candidate['path']).read_text())
    if change == 'missing_authorization':
        write(tmp_path, setup['authorization']['path'], {'not_actual_creator_authorization': True})
        with pytest.raises(pa.StandingSetupRequired):
            amend_allowance(tmp_path, activation, kwargs['standing_setup'], candidate)
        return
    result = amend_allowance(tmp_path, activation, kwargs['standing_setup'], candidate)
    if change == 'authorization':
        write(tmp_path, setup['authorization']['path'], {'revoked': True})
    elif change == 'record':
        path = tmp_path / result['amendment']['path']; path.chmod(0o644)
        record = json.loads(path.read_text()); record['changes'][0]['after'] = 100
        write(tmp_path, result['amendment']['path'], record)
    elif change == 'successor':
        setup['caps']['max_total_attempts'] = 100
        write(tmp_path, candidate['path'], setup)
    else:
        log = json.loads((tmp_path / 'artifacts/decision_log.json').read_text())
        log['decisions'][-1]['user_approved'] = False
        write(tmp_path, 'artifacts/decision_log.json', log)
    assert pa.load_active_policy(tmp_path)[0] is None
    before = file_bytes(tmp_path)
    with pytest.raises(pa.AutonomyError):
        amend_allowance(tmp_path, activation, kwargs['standing_setup'], candidate)
    assert file_bytes(tmp_path) == before
