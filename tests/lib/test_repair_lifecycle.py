"""Offline OpenArt MCP-shaped critical-repair lifecycle: rejection -> same-route
repair prepare -> existing admission -> host once -> review -> compose. No provider calls."""
import copy

import pytest

from lib import episode_production_controls as controls
from lib import production_autonomy as pa
from lib import production_execution as execution
from tests.integration.test_first_pass_workflow import attestation
from tests.lib.test_episode_production_controls import approval
from tests.lib.test_production_repair_batches import (  # noqa: F401
    collect_fixture, lifecycle, mcp_compile, mcp_select, mcp_two, media_path, real_av_clip)


def _same_route_controls(root, **overrides):
    values = {'max_generations_per_shot': 2, 'alternate_repair': 'disabled', 'access_fallback': False,
              'first_cut': {'enabled': False}}
    values.update(overrides)
    controls.record_episode_controls(root, values, evidence=approval(root, 'same-route'))


def _reject(root, aid, severity='critical', status='fail'):
    output = execution.load_attempt_result(root, aid)['output']
    review = attestation(output['sha256'], ['completed_action'], execution._read(root / 'project.json')['story_revision'])
    review.update(status='fail', predicates=[{'name': 'completed_action', 'status': status,
        'severity': severity, 'evidence': 'SYNTHETIC end state not reached'}])
    execution.record_rejection(root, aid, review)


@pytest.fixture
def failed_entry(mcp_two, monkeypatch, tmp_path):
    root, policy, originals, timing, jobs = mcp_two
    media = real_av_clip(tmp_path.resolve() / 'original.mp4', seconds=2)
    for sid in originals:
        collect_fixture(root, jobs, 'original-' + sid, media, monkeypatch)
    return root, originals, timing, jobs


def _state(root):
    return execution.next_production_action(root)


def _repair_inputs(root, originals, timing, model=None):
    inputs = copy.deepcopy(originals['entry']); inputs['output_path'] = str(root / 'repair-entry.mp4')
    if model:
        inputs['model'] = model
    inputs, _ = mcp_compile(root, inputs, 'entry', 'repair-entry', timing)
    inputs['governance'].pop('scope_id', None)
    return inputs


def _repair_action(root):
    _same_route_controls(root)
    _reject(root, 'original-entry')
    mcp_select(root, 'other', 'original-other')
    action = _state(root)
    assert (action['kind'], action['subject']) == ('prepare_motion', 'entry'), action
    return action


def _counts(root):
    return pa.root_attempt_counts(root, pa.require_active_policy(root, shot_id='entry')[0])


def test_mcp_critical_rejection_repairs_once_reviews_repair_and_completes_cut(failed_entry, monkeypatch, tmp_path):
    from lib import production_draft as draft
    root, originals, timing, jobs = failed_entry
    action = _repair_action(root)
    repair = action['binding']['repair']
    original_sha = execution.load_attempt_result(root, 'original-entry')['output']['sha256']
    assert repair == {'replaces_attempt_ids': ['original-entry'], 'output_sha256': original_sha,
                      'provider': 'openart_mcp', 'model': originals['entry']['model']}
    assert action['inputs']['phase'] == 'repair'
    # Tampered route/model: refused before any scope, row or bytes are written.
    scopes_before = (root / 'production_scopes.json').read_bytes()
    for provider, model in (('openart_mcp', 'wan3-0'), ('grok_cli', None)):
        with pytest.raises(execution.ProductionGovernanceError):
            execution.complete_action(root, action, outcome={
                'inputs': _repair_inputs(root, originals, timing, model), 'provider': provider})
    assert (root / 'production_scopes.json').read_bytes() == scopes_before
    assert len(jobs.list_attempts(root)) == 2
    # Tampered binding (cap/route edit in the action itself) is stale.
    forged = copy.deepcopy(action); forged['binding']['repair']['model'] = 'wan3-0'
    with pytest.raises(execution.ProductionGovernanceError):
        execution.complete_action(root, forged, outcome={'inputs': _repair_inputs(root, originals, timing), 'provider': 'openart_mcp'})
    result = execution.complete_action(root, action, outcome={
        'inputs': _repair_inputs(root, originals, timing), 'provider': 'openart_mcp'})
    assert result['status'] == 'dispatched' and result['result']['success'], result
    rows = {r['attempt_id']: r for r in jobs.list_attempts(root)}
    assert len(rows) == 3
    rid = next(a for a in rows if a.startswith('mcp-'))
    assert 'scope' not in rows[rid] and rows[rid]['scope_snapshot']['phase'] == 'repair'
    assert rows[rid]['scope_snapshot']['replaces_attempt_ids'] == ['original-entry']
    # Host step is for the repair only; prepare_motion is not re-emitted.
    assert result['action']['subject'] == rid and result['action']['kind'] != 'prepare_motion'
    jobs.begin(root, rid, authority_fn=execution.prepare_openart_mcp_handoff)
    jobs.receive(root, rid, outcome={'historyId': 'history-' + rid})
    collect_fixture(root, jobs, rid, real_av_clip(tmp_path.resolve() / 'repair.mp4', seconds=2, freq=660), monkeypatch)
    review = _state(root)
    assert (review['kind'], review['subject']) == ('review_selection', rid), review
    mcp_select(root, 'entry', rid)
    final = _state(root)
    assert final['kind'] == 'compose_cut', final
    cut = draft.compose_first_cut(root)
    assert cut['status'] == 'complete', cut
    assert [c['attempt_id'] for c in cut['clips']] == [rid, 'original-other']
    # Failed original, its retained rejection and cumulative counters persist.
    assert execution.load_attempt_result(root, 'original-entry')['output']['sha256'] == original_sha
    assert _counts(root)['repair'] == 1
    assert controls.generation_usage(root)['per_shot']['entry'] == 2
    assert controls.generation_usage(root)['total'] == 3


def test_rejected_repair_at_cap_ends_in_stable_incomplete_cut(failed_entry, monkeypatch, tmp_path):
    from lib import production_draft as draft
    root, originals, timing, jobs = failed_entry
    action = _repair_action(root)
    rid = execution.complete_action(root, action, outcome={
        'inputs': _repair_inputs(root, originals, timing), 'provider': 'openart_mcp'})['action']['subject']
    jobs.begin(root, rid, authority_fn=execution.prepare_openart_mcp_handoff)
    jobs.receive(root, rid, outcome={'historyId': 'history-' + rid})
    collect_fixture(root, jobs, rid, real_av_clip(tmp_path.resolve() / 'repair.mp4', seconds=2, freq=660), monkeypatch)
    assert _state(root)['subject'] == rid
    _reject(root, rid)
    for _ in range(3):
        assert _state(root)['kind'] == 'compose_cut'
    candidate = next(row for row in draft.first_cut_candidates(root) if row['shot_id'] == 'entry')
    assert candidate['review'] == 'fail', candidate
    cut = draft.compose_first_cut(root)
    assert cut['export'] and cut['technical']['duration_seconds'] > 0, cut
    assert len(jobs.list_attempts(root)) == 3 and _counts(root)['repair'] == 1


def test_interrupted_undispatched_repair_scope_at_cap_is_reused(failed_entry):
    root, originals, timing, jobs = failed_entry
    action = _repair_action(root)
    # Interrupt after scope derivation, before attempt reservation.
    scope = pa.derive_scope(root, _repair_inputs(root, originals, timing), provider='openart_mcp',
                            phase='repair', replaces_attempt_ids=['original-entry'])
    resumed = _state(root)
    assert resumed == action
    result = execution.complete_action(root, resumed, outcome={
        'inputs': _repair_inputs(root, originals, timing), 'provider': 'openart_mcp'})
    assert result['result']['success'], result
    scopes = [s for s in execution._read(root / 'production_scopes.json')['scopes'] if s.get('phase') == 'repair']
    assert [s['id'] for s in scopes] == [scope['id']]
    row = next(r for r in jobs.list_attempts(root) if r['attempt_id'] == result['action']['subject'])
    assert row['scope_id'] == scope['id']


@pytest.mark.parametrize('case', ['cosmetic', 'unknown', 'first_cut_enabled', 'tampered_bytes'])
def test_no_automatic_repair_outside_critical_same_route_current_bytes(failed_entry, case):
    root, originals, timing, jobs = failed_entry
    _same_route_controls(root, **({'first_cut': {'enabled': True}} if case == 'first_cut_enabled' else {}))
    _reject(root, 'original-entry', severity='cosmetic' if case == 'cosmetic' else 'critical',
            status='unknown' if case == 'unknown' else 'fail')
    mcp_select(root, 'other', 'original-other')
    if case == 'tampered_bytes':
        path = execution.load_attempt_result(root, 'original-entry')['output']['path']
        with open(path, 'ab') as handle:
            handle.write(b'tamper')
        # Current-byte provenance fails closed before any repair is scheduled.
        with pytest.raises(execution.ProductionGovernanceError, match='bytes differ'):
            _state(root)
    else:
        action = _state(root)
        assert action['kind'] != 'prepare_motion', action
    assert len(jobs.list_attempts(root)) == 2


def test_uncertain_dispatched_repair_is_collected_before_any_new_prepare(failed_entry):
    root, originals, timing, jobs = failed_entry
    action = _repair_action(root)
    rid = execution.complete_action(root, action, outcome={
        'inputs': _repair_inputs(root, originals, timing), 'provider': 'openart_mcp'})['action']['subject']
    jobs.begin(root, rid, authority_fn=execution.prepare_openart_mcp_handoff)
    # Host began, no receipt yet: the uncertain repair owns the shot.
    for _ in range(2):
        pending = _state(root)
        assert pending['subject'] == rid and pending['kind'] == 'receive_openart_original', pending
    assert len(jobs.list_attempts(root)) == 3 and _counts(root)['repair'] == 1
