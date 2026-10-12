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
    production, reserved, continue_attempt, missing_cwd_reserved,
)


def approval(root, name='episode-approval'):
    path = Path(root) / (name + '.txt')
    path.write_text('Synthetic creator approval for the exact episode controls below.')
    return {'path': path.name, 'sha256': file_sha256(path), 'approved_by': 'Offline creator fixture'}


def activate(root, limit=1, *, alternate='disabled', fallback=False, name='episode-approval'):
    return controls.record_episode_controls(root, {
        'max_generations_per_shot': limit, 'alternate_repair': alternate,
        'access_fallback': fallback,
    }, evidence=approval(root, name))


def duplicate_request(root, inputs, scope, previous, index, *, scope_id=None):
    inputs = copy.deepcopy(inputs)
    scope = copy.deepcopy(scope)
    scope.update(id=scope_id or 'duplicate-' + str(index), phase='first_pass')
    scope.pop('replaces_attempt_ids', None)
    inputs['governance']['scope_id'] = scope['id']
    inputs['output_path'] = str(root / 'assets' / ('duplicate-' + str(index) + '.mp4'))
    scope['requests']['entry'] = execution.planned_request_digest(inputs, project_dir=root)
    write_scopes(root, scope)
    return inputs


@pytest.mark.parametrize('mutation', ['bad_hash', 'empty_approver', 'outside_root', 'bool_limit', 'extra_field'])
def test_controls_require_retained_creator_evidence_and_valid_exact_fields(tmp_path, mutation):
    project(tmp_path, motion=True)
    activate(tmp_path)
    artifact = tmp_path / 'artifacts/episode_production_controls.json'
    before = artifact.read_bytes()
    evidence = approval(tmp_path, 'invalid-approval')
    payload = {'max_generations_per_shot': 1, 'alternate_repair': 'disabled', 'access_fallback': False}
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
    activate(tmp_path, 1)
    MotionTool().execute(inputs)
    status = controls.episode_production_status(tmp_path)
    cap = status['generation_caps']['entry']
    assert cap == {'controls_limit': 1, 'scope_allowances': {scope['id']: 5}, 'used': 1}
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
    activate(p.root)
    first = p.generate('entry').data['production_attempt_id']
    usage = controls.generation_usage(p.root)
    assert next(i for i in usage['occurrences'] if i['attempt_id'] == first)['model'] is None
    for provider, model in [('grok_cli', 'grok-agent-relabel'), ('openart_cli', None)]:
        admitted = controls.generation_admission(p.root, shot_id='entry', provider=provider,
                                                 model=model, purpose='first_pass')
        assert not admitted['admitted']
        assert 'episode generation limit reached' in ' '.join(admitted['reasons'])
    assert len(p.transport.native_requests) == 1


def test_pending_submitted_and_uncollected_mcp_originals_block_duplicates(fixture_observations, tmp_path):
    from lib import openart_mcp_jobs as jobs
    inputs, _ = prepare_project(tmp_path / 'mcp')
    root = Path(inputs['project_dir'])
    activate(root, 1)
    jobs.prepare(root, attempt_id='original', generation_inputs=inputs, authority_fn=execution.prepare_openart_mcp_handoff)
    jobs.begin(root, 'original', authority_fn=execution.prepare_openart_mcp_handoff)
    shot = inputs['governance']['shot_id']
    def blocked():
        reasons = controls.generation_admission(root, shot_id=shot, provider='grok_cli', purpose='first_pass')['reasons']
        return any('episode generation limit reached' in r for r in reasons)
    assert blocked()
    jobs.receive(root, 'original', outcome={'historyId': 'synthetic-original'})
    jobs.record_status(root, 'original', result={'historyId': 'synthetic-original', 'status': 'COMPLETED',
        'resources': [{'id': 'v', 'mediaType': 'video', 'url': 'https://fixture.invalid/o.mp4'}]})
    assert jobs.attempt_state(root, 'original')['status'] == 'completed'
    assert blocked()
    assert not controls.generation_admission(root, shot_id=shot, provider='grok_cli', purpose='first_pass',
                                             exclude_attempt_id='original')['reasons']


# --- actual native dispatch regressions; retained fixtures, no provider calls


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


def test_status_validates_current_selected_bytes_before_reporting_ready(production):
    p = production
    p.complete_shots()
    before = controls.episode_production_status(p.root)
    assert set(before['shot_readiness'].values()) <= {'ready', 'ready_with_warnings'}, before['shot_readiness']
    assert before['delivery'] != 'draft_only'
    selected = execution.load_selected_attempts(p.root)
    shot_id = sorted(selected)[0]
    Path(selected[shot_id]['output']['path']).write_bytes(b'changed selected bytes')
    status = controls.episode_production_status(p.root)
    assert status['selections'][shot_id]['review_status'] == 'invalid'
    assert status['selections'][shot_id]['error']
    assert status['shot_readiness'][shot_id] == 'invalid'
    assert status['delivery'] == 'draft_only'


def test_proven_missing_output_cwd_preserves_episode_cap_and_same_reservation(
    missing_cwd_reserved,
):
    p, aid, directory, request, frozen = missing_cwd_reserved
    activate(p.root, 1)
    before = controls.generation_usage(p.root)
    assert before["total"] == 0 and before["excluded_never_submitted"] == [aid]
    assert before["occurrences"][0]["local_continuation_eligible"] is True
    result = continue_attempt(missing_cwd_reserved, dry_run=False)
    assert result.success, result.error
    after = controls.generation_usage(p.root)
    assert after["total"] == after["per_shot"]["entry"] == 1
    assert after["excluded_never_submitted"] == []
    assert after["occurrences"][0]["local_continuation_eligible"] is False
    assert len(execution._attempts(p.root)) == len(p.transport.native_requests) == 1
    for name, original in frozen.items():
        assert (directory / name).read_bytes() == original
    inputs = duplicate_request(p.root, p.inputs["entry"], p.scope, aid, 1)
    with pytest.raises(
        (execution.ProductionGovernanceError, controls.EpisodeControlsError)
    ):
        p.cli.execute(inputs)
    assert len(p.transport.native_requests) == 1


@pytest.mark.parametrize('controls_override', [
    {'max_generations_per_shot': 2},
    {'alternate_repair': 'different_provider_or_media_model'},
    {'access_fallback': True},
])
def test_one_shot_controls_refuse_expanded_generation_authority(tmp_path, controls_override):
    project(tmp_path, motion=True)
    payload = {'max_generations_per_shot': 1, 'alternate_repair': 'disabled', 'access_fallback': False}
    payload.update(controls_override)
    with pytest.raises(controls.EpisodeControlsError):
        controls.record_episode_controls(tmp_path, payload, evidence=approval(tmp_path))
    assert not (tmp_path / controls.ARTIFACT).exists()


def test_new_scope_cannot_dispatch_second_original(tmp_path):
    inputs, scope, _ = project(tmp_path, motion=True)
    activate(tmp_path)
    tool = MotionTool()
    first = tool.execute(inputs)
    assert first.success
    duplicate = duplicate_request(tmp_path, inputs, scope, first.data['production_attempt_id'], 1)
    with pytest.raises((execution.ProductionGovernanceError, controls.EpisodeControlsError)):
        tool.execute(duplicate)
    assert tool.calls == 1
    assert controls.generation_usage(tmp_path)['per_shot']['entry'] == 1
