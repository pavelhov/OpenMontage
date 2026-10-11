"""Narrow standing source-transfer successors; no provider or host calls."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from lib import production_autonomy as pa, production_execution as execution
from tests.lib.test_production_factory import (creator_fixture, file_bytes, standing_fixture, write,
    correction_factory_fixture, host_receipt)
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
def test_source_batch_successor_keeps_emitted_original_image_collectable(neutral_package, revoke_successor):
    from lib import production_boards as boards, production_images as images
    root = correction_factory_fixture(neutral_package)
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
    candidate = successor(root, predecessor, provider='native_imagegen')
    amend(root, {'policy_sha256': sha}, predecessor, candidate)
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
