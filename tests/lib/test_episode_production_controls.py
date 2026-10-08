"""Episode admission regressions through real strict dispatch; synthetic offline only."""
import copy
import json
from pathlib import Path

import pytest

from lib import episode_production_controls as controls
from lib import production_execution as execution
from lib.shot_contract import file_sha256
from tests.lib.test_production_execution import MotionTool, project, write_scopes
from tests.integration.test_openart_mcp_governance import (
    fixture_observations, prepare_project, install_fake_download,
)
from tests.integration.test_local_grok_continuation import (
    production, reserved, continue_attempt,
)


def test_without_controls_preserves_existing_exact_strict_repair(tmp_path):
    inputs, scope, _ = project(tmp_path, motion=True)
    tool = MotionTool()
    first = tool.execute(inputs)
    scope.update(id='repair', phase='repair', replaces_attempt_ids=[first.data['production_attempt_id']])
    inputs['governance']['scope_id'] = 'repair'
    inputs['output_path'] = str(tmp_path / 'assets' / 'repair.mp4')
    scope['requests']['entry'] = execution.planned_request_digest(inputs, project_dir=tmp_path)
    write_scopes(tmp_path, scope)
    assert tool.execute(inputs).success
    assert tool.calls == 2
    assert not (tmp_path / 'artifacts' / 'episode_production_controls.json').exists()


def approval(root, name='episode-approval'):
    path = Path(root) / (name + '.txt')
    path.write_text('Synthetic creator approval for the exact episode controls below.')
    return {'path': path.name, 'sha256': file_sha256(path), 'approved_by': 'Offline creator fixture'}


def activate(root, limit=2, *, alternate='disabled', fallback=False, name='episode-approval'):
    return controls.record_episode_controls(root, {
        'max_generations_per_shot': limit, 'alternate_repair': alternate,
        'access_fallback': fallback,
    }, evidence=approval(root, name))


def repair_request(root, inputs, scope, previous, index, *, scope_id=None):
    inputs = copy.deepcopy(inputs)
    scope = copy.deepcopy(scope)
    scope.update(id=scope_id or 'repair-' + str(index), phase='repair', replaces_attempt_ids=[previous])
    inputs['governance']['scope_id'] = scope['id']
    inputs['output_path'] = str(root / 'assets' / ('repair-' + str(index) + '.mp4'))
    scope['requests']['entry'] = execution.planned_request_digest(inputs, project_dir=root)
    write_scopes(root, scope)
    return inputs


@pytest.mark.parametrize('limit', [1, 2, 3])
def test_declared_limit_caps_real_dispatch_without_hidden_two_clamp(tmp_path, limit):
    inputs, scope, _ = project(tmp_path, motion=True)
    assert scope['attempts_per_shot']['entry'] == 5
    activate(tmp_path, limit)
    tool = MotionTool()
    result = tool.execute(inputs)
    for index in range(1, limit):
        inputs = repair_request(tmp_path, inputs, scope, result.data['production_attempt_id'], index)
        result = tool.execute(inputs)
        assert result.success
    assert tool.calls == limit
    assert controls.generation_usage(tmp_path)['per_shot']['entry'] == limit
    denied = repair_request(tmp_path, inputs, scope, result.data['production_attempt_id'], limit)
    with pytest.raises((execution.ProductionGovernanceError, controls.EpisodeControlsError)):
        tool.execute(denied)
    assert tool.calls == limit


def test_single_repair_scope_cap_still_bounds_higher_episode_allowance(tmp_path):
    inputs, scope, _ = project(tmp_path, motion=True)
    activate(tmp_path, 3)
    tool = MotionTool()
    result = tool.execute(inputs)
    repair_scope = copy.deepcopy(scope)
    repair_scope['attempts_per_shot']['entry'] = 1
    inputs = repair_request(tmp_path, inputs, repair_scope, result.data['production_attempt_id'], 1)
    result = tool.execute(inputs)
    assert result.success
    inputs = repair_request(tmp_path, inputs, repair_scope, result.data['production_attempt_id'], 2,
                            scope_id='repair-1')
    with pytest.raises((execution.ProductionGovernanceError, controls.EpisodeControlsError)):
        tool.execute(inputs)
    assert tool.calls == 2


def test_later_opt_in_and_amendments_keep_old_spent_generations(tmp_path):
    inputs, scope, _ = project(tmp_path, motion=True)
    tool = MotionTool()
    result = tool.execute(inputs)
    activate(tmp_path, 1)
    assert controls.generation_usage(tmp_path)['total'] == 1
    first = (tmp_path / 'artifacts/episode_production_controls.json').read_bytes()
    activate(tmp_path, 3, name='amended-approval')
    records = json.loads((tmp_path / 'artifacts/episode_production_controls.json').read_text())['records']
    assert len(records) == 2
    assert records[0] == json.loads(first)['records'][0]
    assert records[1]['previous_sha256']
    assert controls.generation_usage(tmp_path)['total'] == 1
    inputs = repair_request(tmp_path, inputs, scope, result.data['production_attempt_id'], 1)
    assert tool.execute(inputs).success
    assert controls.generation_usage(tmp_path)['total'] == 2


@pytest.mark.parametrize('mutation', ['bad_hash', 'empty_approver', 'outside_root', 'bool_limit', 'extra_field'])
def test_controls_require_retained_creator_evidence_and_valid_exact_fields(tmp_path, mutation):
    project(tmp_path, motion=True)
    activate(tmp_path)
    artifact = tmp_path / 'artifacts/episode_production_controls.json'
    before = artifact.read_bytes()
    evidence = approval(tmp_path, 'invalid-approval')
    payload = {'max_generations_per_shot': 3, 'alternate_repair': 'disabled', 'access_fallback': False}
    if mutation == 'bad_hash': evidence['sha256'] = '0' * 64
    elif mutation == 'empty_approver': evidence['approved_by'] = ''
    elif mutation == 'outside_root':
        outside = tmp_path.parent / 'foreign-approval.txt'; outside.write_text('Unretained foreign approval')
        evidence.update(path=str(outside), sha256=file_sha256(outside))
    elif mutation == 'bool_limit': payload['max_generations_per_shot'] = True
    else: payload['allowed_provider'] = 'anything'
    with pytest.raises(controls.EpisodeControlsError):
        controls.record_episode_controls(tmp_path, payload, evidence=evidence)
    assert artifact.read_bytes() == before


@pytest.mark.parametrize('terminal', [False, True])
def test_unproven_failures_count_once_and_uncertainty_blocks_retry(tmp_path, terminal):
    from tools.base_tool import ToolResult
    inputs, _, _ = project(tmp_path, motion=True)
    activate(tmp_path)
    class Failure(MotionTool):
        def execute(self, provider_inputs):
            self.calls += 1
            return ToolResult(success=False, error='offline provider failure', data={
                'dispatch_status': 'failed' if terminal else 'not_dispatched',
            })
    tool = Failure()
    result = tool.execute(inputs)
    assert not result.success
    usage = controls.generation_usage(tmp_path)
    # A wrapper label alone cannot prove that no provider accepted work.
    assert usage['total'] == 1
    assert usage['excluded_never_submitted'] == []
    for _ in range(2): assert controls.generation_usage(tmp_path) == usage
    if not terminal:
        assert usage['unresolved_originals']
        with pytest.raises((execution.ProductionGovernanceError, controls.EpisodeControlsError)):
            tool.execute(inputs)
        assert tool.calls == 1


def test_proven_local_refusal_excluded_then_fresh_continuation_consumes_slot(reserved):
    p, aid, _, _, _ = reserved
    activate(p.root, 1)
    before = controls.generation_usage(p.root)
    assert before['total'] == 0
    assert before['excluded_never_submitted'] == [aid]
    result = continue_attempt(reserved, dry_run=False)
    assert result.success, result.error
    after = controls.generation_usage(p.root)
    assert after['total'] == 1 and after['per_shot']['entry'] == 1
    assert after['excluded_never_submitted'] == []
    assert len(p.transport.native_requests) == 1
    selected = p.select('entry', result)
    from lib.production_provenance import validate_attempt_provenance
    validate_attempt_provenance(p.root, aid, shot_id='entry', story_revision=p.story['story_revision'],
                               expected_output=selected['output'])
    for _ in range(2):
        assert controls.episode_production_status(p.root)['usage']['total'] == 1
        assert controls.generation_usage(p.root)['total'] == 1
    assert len(p.transport.native_requests) == 1


def test_provider_restriction_blocks_fresh_local_continuation(reserved):
    p, aid, _, _, _ = reserved
    activate(p.root)
    changed = controls.restrict_episode_provider(p.root, ['grok_cli'], evidence=approval(p.root, 'restrict'), reason='Creator restricts future Grok')
    assert aid in changed['blocked_unsubmitted']['local_continuation_attempt_ids']
    assert changed['remote_cancellation'] is False
    with pytest.raises((execution.ProductionGovernanceError, controls.EpisodeControlsError)):
        continue_attempt(reserved, dry_run=False)
    assert p.transport.native_requests == []
    assert controls.generation_usage(p.root)['total'] == 0


def test_restriction_blocks_old_strict_scope_preserves_completed_grok(production):
    p = production
    activate(p.root)
    first = p.generate('entry')
    selected = p.select('entry', first)
    request_path = p.root / 'production_attempts' / first.data['production_attempt_id'] / 'request.json'
    frozen = request_path.read_bytes()
    scope = (p.root / 'production_scopes.json').read_bytes()
    changed = controls.restrict_episode_provider(p.root, ['grok_cli'], evidence=approval(p.root, 'restrict'), reason='Prospective only')
    assert 'first-generation' in changed['blocked_unsubmitted']['scope_ids']
    with pytest.raises((execution.ProductionGovernanceError, controls.EpisodeControlsError)):
        p.generate('interior')
    from lib.production_provenance import validate_attempt_provenance
    validate_attempt_provenance(p.root, first.data['production_attempt_id'], shot_id='entry',
        story_revision=p.story['story_revision'], expected_output=selected['output'])
    assert request_path.read_bytes() == frozen
    assert (p.root / 'production_scopes.json').read_bytes() == scope
    assert len(p.transport.native_requests) == 1


def test_mcp_prepare_zero_begin_once_and_restriction_rechecked_at_begin(fixture_observations, tmp_path):
    from lib import openart_mcp_jobs as jobs
    inputs, _ = prepare_project(tmp_path / 'mcp')
    root = Path(inputs['project_dir'])
    activate(root)
    jobs.prepare(root, attempt_id='prepared', generation_inputs=inputs, authority_fn=execution.prepare_openart_mcp_handoff)
    assert controls.generation_usage(root)['total'] == 0
    changed = controls.restrict_episode_provider(root, ['openart_mcp'], evidence=approval(root, 'restrict'), reason='Creator changed future permission')
    assert 'prepared' in changed['blocked_unsubmitted']['mcp_prepared_attempt_ids']
    with pytest.raises(ValueError): jobs.begin(root, 'prepared', authority_fn=execution.prepare_openart_mcp_handoff)
    assert jobs.attempt_state(root, 'prepared')['status'] == 'prepared'
    assert controls.generation_usage(root)['total'] == 0


def test_mcp_original_status_download_and_collection_do_not_consume_new_slots(fixture_observations, tmp_path, monkeypatch):
    from lib import openart_mcp_jobs as jobs
    from tests.integration.test_openart_first_pass_workflow import real_av_clip
    inputs, _ = prepare_project(tmp_path / 'mcp')
    root = Path(inputs['project_dir'])
    activate(root, 1)
    jobs.prepare(root, attempt_id='original', generation_inputs=inputs, authority_fn=execution.prepare_openart_mcp_handoff)
    jobs.begin(root, 'original', authority_fn=execution.prepare_openart_mcp_handoff)
    assert controls.generation_usage(root)['total'] == 1
    assert controls.generation_usage(root)['unresolved_originals'] == ['original']
    changed = controls.restrict_episode_provider(root, ['openart_mcp'], evidence=approval(root, 'restrict'), reason='Original recovery still permitted')
    assert changed['pending_originals'] == ['original']
    jobs.receive(root, 'original', outcome={'historyId': 'synthetic-original'})
    status = {'historyId': 'synthetic-original', 'status': 'COMPLETED', 'resources': [
        {'id': 'v', 'mediaType': 'video', 'url': 'https://fixture.invalid/original.mp4'}]}
    jobs.record_status(root, 'original', result=status)
    media = real_av_clip(tmp_path / 'synthetic.mp4', seconds=1)
    install_fake_download(monkeypatch, jobs, media)
    downloaded = jobs.download_original(root, 'original')
    jobs.collect(root, 'original', downloaded_path=downloaded['downloaded_path'])
    jobs.collect(root, 'original', downloaded_path=downloaded['downloaded_path'])
    assert controls.generation_usage(root)['total'] == 1
    assert controls.generation_usage(root)['unresolved_originals'] == []
    status = controls.episode_production_status(root)
    assert status['provider_calls'] == 0 and status['usage']['total'] == 1


# --- reconcile regressions (status caps, evidence replay, routes, pending originals)

def test_status_reports_controls_limit_and_per_scope_allowances_separately(tmp_path):
    inputs, scope, _ = project(tmp_path, motion=True)
    activate(tmp_path, 2)
    MotionTool().execute(inputs)
    status = controls.episode_production_status(tmp_path)
    cap = status['generation_caps']['entry']
    assert cap == {'controls_limit': 2, 'scope_allowances': {scope['id']: 5}, 'used': 1}
    assert 'effective' not in cap and 'policy caps not included' in status['generation_caps_note']
    assert status['delivery'] == 'draft_only' and status['provider_calls'] == 0


def test_changed_evidence_bytes_invalidate_recorded_controls(tmp_path):
    project(tmp_path, motion=True)
    activate(tmp_path)
    (tmp_path / 'episode-approval.txt').write_text('edited after approval')
    with pytest.raises(controls.EpisodeControlsError):
        controls.effective_controls(tmp_path)


def test_grok_media_model_is_none_and_agent_model_relabel_is_same_route(production):
    p = production
    activate(p.root, 3, alternate='different_provider_or_media_model')
    first = p.generate('entry').data['production_attempt_id']
    usage = controls.generation_usage(p.root)
    assert next(i for i in usage['occurrences'] if i['attempt_id'] == first)['model'] is None
    same = controls.generation_admission(p.root, shot_id='entry', provider='grok_cli', model='grok-agent-relabel',
                                         purpose='repair', replaces_attempt_ids=[first])
    assert not same['admitted'] and 'different provider' in same['reasons'][0]
    other = controls.generation_admission(p.root, shot_id='entry', provider='openart_cli', model=None,
                                          purpose='repair', replaces_attempt_ids=[first])
    assert other['admitted'], other['reasons']


def test_access_fallback_requires_explicit_episode_approval(tmp_path):
    inputs, _, _ = project(tmp_path, motion=True)
    activate(tmp_path, 3)
    first = MotionTool().execute(inputs).data['production_attempt_id']
    kwargs = dict(shot_id='entry', provider='grok_cli', purpose='repair', replaces_attempt_ids=[first],
                  repair_basis='access_fallback')
    assert 'access fallback is not approved' in ' '.join(controls.generation_admission(tmp_path, **kwargs)['reasons'])
    activate(tmp_path, 3, fallback=True, name='fallback-approval')
    assert controls.generation_admission(tmp_path, **kwargs)['admitted']


def test_pending_submitted_and_uncollected_mcp_originals_block_duplicates(fixture_observations, tmp_path):
    from lib import openart_mcp_jobs as jobs
    inputs, _ = prepare_project(tmp_path / 'mcp')
    root = Path(inputs['project_dir'])
    activate(root, 3)
    jobs.prepare(root, attempt_id='original', generation_inputs=inputs, authority_fn=execution.prepare_openart_mcp_handoff)
    jobs.begin(root, 'original', authority_fn=execution.prepare_openart_mcp_handoff)
    shot = inputs['governance']['shot_id']
    def blocked():
        reasons = controls.generation_admission(root, shot_id=shot, provider='grok_cli', purpose='first_pass')['reasons']
        return any('pending original' in r for r in reasons)
    assert blocked()
    jobs.receive(root, 'original', outcome={'historyId': 'synthetic-original'})
    jobs.record_status(root, 'original', result={'historyId': 'synthetic-original', 'status': 'COMPLETED',
        'resources': [{'id': 'v', 'mediaType': 'video', 'url': 'https://fixture.invalid/o.mp4'}]})
    assert jobs.attempt_state(root, 'original')['status'] == 'completed'
    assert blocked()
    assert not controls.generation_admission(root, shot_id=shot, provider='grok_cli', purpose='first_pass',
                                             exclude_attempt_id='original')['reasons']


# --- actual native dispatch regressions; retained fixtures, no provider calls

def native_episode_context(tmp_path, monkeypatch, request):
    """Reuse the strict native fixture without exporting circular pytest fixtures."""
    import socket
    from lib import production_autonomy as pa
    from tests.integration.test_openart_mcp_autonomy import lifecycle
    from tests.integration.test_openart_alternate_model_repair import repair_context
    from tests.lib.test_production_autonomy import install_existing

    def forbidden(*args, **kwargs):
        pytest.fail('episode dispatch regression attempted a network connection')
    for name in ('connect', 'connect_ex'):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    base = lifecycle.__wrapped__(tmp_path, monkeypatch)
    context = repair_context.__wrapped__(base, tmp_path, monkeypatch, None, request)
    root, policy, _, inputs, native, compiled, timing, jobs = context
    policy = copy.deepcopy(policy)
    # Keep the existing policy ceiling above the episode allowance, so a
    # two-generation refusal proves the shared episode cap itself.
    policy['caps'] = {'max_total_attempts': 3, 'max_attempts_per_shot': 3, 'max_repair_attempts': 2}
    policy, sha = install_existing(root, policy,
        templates={'entry': execution.planned_request_template(inputs, project_dir=root)})
    assert jobs.list_attempts(root) == []
    (root / 'production_scopes.json').write_text(json.dumps({'version': '1.0', 'scopes': []}))
    scope = pa.derive_scope(root, inputs, provider='openart_mcp')
    inputs['governance']['scope_id'] = scope['id']
    return root, policy, sha, inputs, native, compiled, timing, jobs


def test_mcp_begin_rechecks_alternate_repair_amended_after_native_prepare(tmp_path, monkeypatch, request):
    from lib import production_autonomy as pa
    from tests.integration.test_openart_alternate_model_repair import _begin, _compile, ORIGINAL
    context = native_episode_context(tmp_path, monkeypatch, request)
    root, _, _, inputs, native, _, timing, jobs = context
    activate(root, 2, alternate='disabled')
    _begin(context, inputs, ORIGINAL, native)
    jobs.record_status(root, ORIGINAL, result={'historyId': 'fixture-' + ORIGINAL,
        'status': 'FAILED', 'error': 'Synthetic provider rejection'})
    terminal = jobs.terminal_failure_record(root, ORIGINAL)
    original_dir = root / 'openart_mcp/attempts' / ORIGINAL
    # Engineering rejection binds the actual terminal receipt; there is no
    # semantic review of nonexistent footage.
    (original_dir / 'rejection.json').write_text(json.dumps({'status': 'fail',
        'kind': 'generation_terminal_failure', 'attempt_id': ORIGINAL,
        'terminal_failure_sha256': terminal['terminal_failure_sha256'],
        'reviewer': 'Synthetic offline engineering reviewer'}))
    candidate = copy.deepcopy(inputs)
    candidate['output_path'] = str(root / 'same-route-repair.mp4')
    candidate, _, _ = _compile(root, candidate, 'same-route-repair', timing)
    scope = pa.derive_scope(root, candidate, provider='openart_mcp', phase='repair',
        replaces_attempt_ids=[ORIGINAL])
    candidate['governance']['scope_id'] = scope['id']
    jobs.prepare(root, attempt_id='prepared-repair', generation_inputs=candidate,
        authority_fn=execution.prepare_openart_mcp_handoff)
    assert controls.generation_usage(root)['total'] == 1
    prepared = (root / 'openart_mcp/attempts/prepared-repair/state.json').read_bytes()
    original = (original_dir / 'state.json').read_bytes()
    scopes = (root / 'production_scopes.json').read_bytes()
    activate(root, 2, alternate='different_provider_or_media_model', name='alternate-required')
    with pytest.raises(ValueError, match='different provider or media model'):
        jobs.begin(root, 'prepared-repair', authority_fn=execution.prepare_openart_mcp_handoff)
    assert jobs.attempt_state(root, 'prepared-repair')['status'] == 'prepared'
    assert jobs._load(root, 'prepared-repair')['begin_envelope'] is None
    assert controls.generation_usage(root)['total'] == 1
    assert (root / 'openart_mcp/attempts/prepared-repair/state.json').read_bytes() == prepared
    assert (original_dir / 'state.json').read_bytes() == original
    assert (root / 'production_scopes.json').read_bytes() == scopes


@pytest.mark.parametrize('governance', [None, {'mode': 'legacy', 'version': '1.0'},
                                        {'mode': 'strict', 'version': '0.9'}])
def test_record_controls_rejects_legacy_or_non_strict_project(tmp_path, governance):
    marker = {'project_id': 'legacy-episode', 'story_revision': 'story-1'}
    if governance is not None:
        marker['governance'] = governance
    marker_path = tmp_path / 'project.json'
    marker_path.write_text(json.dumps(marker))
    before = marker_path.read_bytes()
    with pytest.raises(controls.EpisodeControlsError, match='strict governed project'):
        activate(tmp_path)
    assert marker_path.read_bytes() == before
    assert not (tmp_path / 'artifacts/episode_production_controls.json').exists()
    assert controls.effective_controls(tmp_path) is None


@pytest.mark.parametrize('agent_label', ['grok-agent-relabel', 'grok-4-fast'])
def test_access_fallback_grok_agent_label_is_not_a_different_media_route(production, monkeypatch, agent_label):
    import subprocess
    from lib import production_autonomy as pa
    p = production
    activate(p.root, 2, fallback=True)
    launches = []
    def access_refusal(argv, **kwargs):
        if '--prompt-file' in argv:
            aid = argv[argv.index('--session-id') + 1]
            assert (p.root / 'production_attempts' / aid / 'request.json').is_file()
            launches.append(aid)
            return subprocess.CompletedProcess(argv, 1, '',
                'HTTP 403 personal-team-blocked:spending-limit')
        return p.transport(argv, **kwargs)
    monkeypatch.setattr('tools._grok_cli_media.subprocess.run', access_refusal)
    result = p.generate('entry')
    assert not result.success
    aid = result.data['production_attempt_id']
    assert launches == [aid]
    retained = execution.load_attempt_result(p.root, aid)
    assert retained['status'] == 'failed' and retained['output'] is None
    assert retained['result']['data']['dispatch_status'] == 'failed'
    before = (p.root / 'production_attempts' / aid / 'result.json').read_bytes()
    # The canonical adapter rejects explicit model selection before derivation.
    # Exercise the fallback route comparison against the actual terminal journal
    # as well, so compatibility agent labels cannot become model alternatives.
    with pytest.raises(pa.AutonomyError, match='different provider or media model'):
        pa._validate_access_fallback(p.root, 'entry', [aid], 'grok_cli', agent_label)
    assert controls.generation_usage(p.root)['total'] == 1
    assert (p.root / 'production_attempts' / aid / 'result.json').read_bytes() == before
    assert launches == [aid]


def test_disabled_alternate_repair_keeps_ordinary_repair_on_route_but_fallback_independent(production):
    p = production
    activate(p.root, 3, alternate='disabled', fallback=True)
    first = p.generate('entry').data['production_attempt_id']
    kwargs = dict(shot_id='entry', purpose='repair', replaces_attempt_ids=[first])
    changed = controls.generation_admission(p.root, provider='openart_cli', model='other-model', **kwargs)
    assert not changed['admitted'] and 'alternate repair is disabled' in ' '.join(changed['reasons'])
    begin = controls.generation_admission(p.root, provider='openart_mcp', model='other-model',
                                          **{**kwargs, 'purpose': 'mcp_begin'})
    assert not begin['admitted']
    same = controls.generation_admission(p.root, provider='grok_cli', model=None, **kwargs)
    assert same['admitted'], same['reasons']
    fallback = controls.generation_admission(p.root, provider='openart_cli', model='other-model',
                                             repair_basis='access_fallback', **kwargs)
    assert fallback['admitted'], fallback['reasons']


@pytest.mark.parametrize('change', ['output_bytes', 'outgoing_frame_deleted'])
def test_status_validates_current_selected_bytes_before_reporting_ready(production, change):
    p = production
    p.complete_shots()
    before = controls.episode_production_status(p.root)
    assert set(before['shot_readiness'].values()) <= {'ready', 'ready_with_warnings'}, before['shot_readiness']
    assert before['delivery'] != 'draft_only'
    selected = execution.load_selected_attempts(p.root)
    shot_id = sorted(selected)[0]
    if change == 'output_bytes':
        Path(selected[shot_id]['output']['path']).write_bytes(b'changed selected bytes')
    else:
        Path(selected[shot_id]['outgoing_frame']['path']).unlink()
    status = controls.episode_production_status(p.root)
    assert status['selections'][shot_id]['review_status'] == 'invalid'
    assert status['selections'][shot_id]['error']
    assert status['shot_readiness'][shot_id] == 'invalid'
    assert status['delivery'] == 'draft_only'
