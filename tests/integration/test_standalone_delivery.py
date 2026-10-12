"""U4 engine-only delivery: explicit export profile, intent-before-render recovery.

Real local FFmpeg renders tiny lavfi clips; no Studio, Drive, account prefix or
provider call participates.
"""
from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest

from lib import production_draft as draft
from lib.media_profiles import delivery_timing_issues
from lib.shot_contract import file_sha256
from tests.integration.test_first_pass_workflow import attestation, production, read, save  # noqa: F401
from tests.lib.test_first_cut import REAL_RUN, lavfi, needs_ff, p  # noqa: F401
from tools.video.video_compose import VideoCompose

pytestmark = needs_ff


@pytest.fixture
def clips(p, monkeypatch):
    from lib import production_execution as execution
    monkeypatch.setattr(subprocess, 'run', REAL_RUN)
    shots = [shot['id'] for shot in execution.load_shot_contract(p.root)['shots']]
    paths = {sid: lavfi(p.root / f'assets/video/{sid}-real.mp4', color, 440 + 110 * i, 1.0)
             for i, (sid, color) in enumerate(zip(shots, ['red', 'green', 'blue', 'white']))}

    def candidates(root):
        # Mirror real _candidate: findings/review re-derived from current retained
        # evidence on every call (synthetic rows carry no historical source warning).
        root = Path(root).resolve()
        revision = read(root / 'project.json')['story_revision']
        selected = execution.load_selected_attempts(root)
        rows = []
        for sid, path in paths.items():
            sha = file_sha256(path)
            review, findings = draft._current_findings(root, sid, 'att-' + sid, sha, revision, selected)
            rows.append({'shot_id': sid, 'status': 'candidate', 'attempt_id': 'att-' + sid,
                         'output': {'path': str(path.relative_to(p.root)), 'sha256': sha},
                         'duration_seconds': 1.0, 'strict_selected': False,
                         'review': review['status'] if review is not None else ('fail' if findings else 'unknown'),
                         'findings': findings})
        return rows
    monkeypatch.setattr(draft, 'first_cut_candidates', candidates)
    return paths


def counting(calls, *, after=None):
    def compose(inputs):
        calls.append(inputs)
        result = VideoCompose().execute(inputs)
        if after:
            after(Path(inputs['output_path']))
        return result
    return compose


def ffprobe(path):
    return json.loads(REAL_RUN(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)],
                               capture_output=True, text=True, check=True).stdout)


def test_explicit_profile_is_rendered_and_technically_validated(p, clips):
    cut = draft.compose_first_cut(p.root, export_profile='tiktok_720p', operation_id='op-profile')
    assert cut['status'] == 'complete' and cut['export_profile'] == 'tiktok_720p'
    output = p.root / cut['export']['path']
    assert (cut['technical']['width'], cut['technical']['height']) == (720, 1280)
    assert delivery_timing_issues(ffprobe(output), target_fps=30) == []
    assert cut['technical']['has_audio']  # native source audio retained, never a listening gate


def test_no_profile_keeps_first_clip_geometry(p, clips):
    cut = draft.compose_first_cut(p.root)
    assert cut['status'] == 'complete' and cut.get('export_profile') is None
    assert (cut['technical']['width'], cut['technical']['height']) == (160, 90)


def test_unknown_profile_fails_before_any_render(p, clips):
    with pytest.raises(ValueError, match='Unknown profile'):
        draft.compose_first_cut(p.root, export_profile='nope', operation_id='op-bad')
    assert not (p.root / 'renders/first_cut').exists()


def test_crash_after_complete_render_records_without_rerender(p, clips):
    calls = []

    def crash(_output):
        raise KeyboardInterrupt('host died after render')
    with pytest.raises(KeyboardInterrupt):
        draft.compose_first_cut(p.root, export_profile='tiktok_720p', operation_id='op-1',
                                compose=counting(calls, after=crash))
    assert len(calls) == 1 and draft.latest_first_cut(p.root) is None
    rendered = file_sha256(p.root / 'renders/first_cut/v001/first_cut.mp4')
    resumed = draft.compose_first_cut(p.root, export_profile='tiktok_720p', operation_id='op-1',
                                      compose=counting(calls))
    assert len(calls) == 1, 'valid existing render must be recorded, not re-rendered'
    assert resumed['sequence'] == 1 and resumed['status'] == 'complete'
    assert resumed['export']['sha256'] == rendered and resumed['operation_id'] == 'op-1'
    again = draft.compose_first_cut(p.root, export_profile='tiktok_720p', operation_id='op-1',
                                    compose=counting(calls))
    assert len(calls) == 1 and again['path'] == resumed['path']
    assert len(list((p.root / 'production_first_cuts').glob('*.json'))) == 1


def test_confirmed_duration_mismatch_retains_original_before_rerender(p, clips):
    calls = []
    original = {}

    def incomplete(output):
        lavfi(output, 'black', 440, 0.5)
        original['sha256'] = file_sha256(output)
        raise KeyboardInterrupt('host died before complete duration')
    with pytest.raises(KeyboardInterrupt):
        draft.compose_first_cut(p.root, operation_id='op-2', compose=counting(calls, after=incomplete))
    resumed = draft.compose_first_cut(p.root, operation_id='op-2', compose=counting(calls))
    assert len(calls) == 2 and resumed['sequence'] == 1 and resumed['status'] == 'complete'
    assert resumed['export']['path'] == 'renders/first_cut/v001/first_cut.mp4'
    retained = p.root / ('renders/first_cut/v001/first_cut.invalid-' + original['sha256'] + '.mp4')
    assert file_sha256(retained) == original['sha256']


def test_confirmed_truncated_mp4_retains_original_then_resumes_local_render(p, clips):
    calls = []

    def truncate(output):
        output.write_bytes(output.read_bytes()[:2000])
        raise KeyboardInterrupt('host died mid-write')
    with pytest.raises(KeyboardInterrupt):
        draft.compose_first_cut(p.root, operation_id='op-partial', compose=counting(calls, after=truncate))
    output = p.root / 'renders/first_cut/v001/first_cut.mp4'
    original = file_sha256(output)
    resumed = draft.compose_first_cut(p.root, operation_id='op-partial', compose=counting(calls))
    assert resumed['status'] == 'complete' and resumed['sequence'] == 1 and len(calls) == 2
    retained = output.parent / ('first_cut.invalid-' + original + '.mp4')
    assert file_sha256(retained) == original
    assert file_sha256(output) == resumed['export']['sha256']


def test_changed_clip_bytes_reject_stale_intent(p, clips):
    def crash(_output):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        draft.compose_first_cut(p.root, operation_id='op-3', compose=counting([], after=crash))
    first = next(iter(clips.values()))
    first.write_bytes(first.read_bytes() + b'\0')
    with pytest.raises(ValueError, match='compose intent'):
        draft.compose_first_cut(p.root, operation_id='op-3')


def test_standalone_delivery_needs_no_studio(p, clips):
    import sys
    result = draft.deliver_standalone(p.root, export_profile='generic_hd', operation_id='op-standalone')
    assert result['status'] == 'complete' and Path(result['export_path']).is_file()
    assert result['export_profile'] == 'generic_hd' and result['technical']['width'] == 1920
    assert result['not_certification'] is True and result['provider_calls'] == 0
    assert [row['shot_id'] for row in result['clips']] == list(clips)
    replay = draft.deliver_standalone(p.root, export_profile='generic_hd', operation_id='op-standalone')
    assert replay['first_cut'] == result['first_cut']
    assert not any(name.startswith(('production_entry', 'studio_storage', 'export_stills')) for name in sys.modules)


def failing_once(calls):
    from tools.base_tool import ToolResult

    def compose(inputs):
        calls.append(inputs)
        if len(calls) == 1:
            return ToolResult(success=False, error='synthetic ordinary compose failure')
        return VideoCompose().execute(inputs)
    return compose


def test_ordinary_render_failure_resumes_local_render_as_latest(p, clips):
    calls = []
    failed = draft.compose_first_cut(p.root, export_profile='tiktok_720p', operation_id='op-fail',
                                     compose=failing_once(calls))
    assert failed['status'] == 'render_failed' and failed['sequence'] == 1
    resumed = draft.compose_first_cut(p.root, export_profile='tiktok_720p', operation_id='op-fail',
                                      compose=failing_once(calls))
    assert len(calls) == 2 and resumed['status'] == 'complete' and resumed['sequence'] == 2
    assert resumed['export']['path'] == 'renders/first_cut/v002/first_cut.mp4'
    history = {Path(failed['path']).name, Path(resumed['path']).name}
    assert {path.name for path in (p.root / 'production_first_cuts').glob('*.json')} == history
    assert draft.latest_first_cut(p.root)['path'] == resumed['path']
    again = draft.compose_first_cut(p.root, export_profile='tiktok_720p', operation_id='op-fail',
                                    compose=failing_once(calls))
    assert len(calls) == 2 and again['path'] == resumed['path']


@pytest.mark.parametrize('change', ['profile', 'clip'])
def test_completed_operation_refuses_changed_inputs(p, clips, change):
    cut = draft.compose_first_cut(p.root, export_profile='tiktok_720p', operation_id='op-done')
    assert cut['status'] == 'complete'
    kwargs = {'export_profile': 'tiktok_720p', 'operation_id': 'op-done'}
    if change == 'profile':
        kwargs['export_profile'] = 'generic_hd'
    else:
        first = next(iter(clips.values()))
        first.write_bytes(first.read_bytes() + b'\0')
    with pytest.raises(ValueError, match='different clips, profile or audio'):
        draft.compose_first_cut(p.root, **kwargs)


@pytest.mark.parametrize(('probe_stage', 'failure'), [
    (stage, failure) for stage in ('media', 'profile') for failure in ('timeout', 'os_error', 'process_error')
] + [('profile', 'packet_check_error')])
def test_transient_resumed_probe_failure_preserves_original_then_resumes(p, clips, monkeypatch,
                                                                       probe_stage, failure):
    calls = []

    def crash(_output):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        draft.compose_first_cut(p.root, export_profile='tiktok_720p', operation_id='op-probe',
                                compose=counting(calls, after=crash))
    output = p.root / 'renders/first_cut/v001/first_cut.mp4'
    original = file_sha256(output)
    name = '_probe' if probe_stage == 'media' else '_profile_issues'
    real = getattr(draft, name)
    problem = {'timeout': subprocess.TimeoutExpired('ffprobe', 30),
               'os_error': OSError('ffprobe temporarily unavailable'),
               'process_error': subprocess.CalledProcessError(1, 'ffprobe'),
               'packet_check_error': 'First-packet timing check failed: transient ffprobe failure'}[failure]
    broken = iter([problem])

    def flaky(path, *args):
        # Candidate/source probes must remain available; fail the export probe.
        error = next(broken, None) if Path(path) == output else None
        if isinstance(error, str):
            return [error]
        if error:
            raise error
        return real(path, *args)
    monkeypatch.setattr(draft, name, flaky)
    with pytest.raises(draft.RenderProbeUncertain, match='existing output preserved') as failure_result:
        draft.compose_first_cut(p.root, export_profile='tiktok_720p', operation_id='op-probe',
                                compose=counting(calls))
    if isinstance(problem, str):
        assert problem in str(failure_result.value)
    else:
        assert type(problem).__name__ in str(failure_result.value)
        assert failure_result.value.__cause__ is problem
    assert file_sha256(output) == original and len(calls) == 1
    assert draft.latest_first_cut(p.root) is None
    resumed = draft.compose_first_cut(p.root, export_profile='tiktok_720p', operation_id='op-probe',
                                      compose=counting(calls))
    assert len(calls) == 1 and resumed['status'] == 'complete' and resumed['sequence'] == 1
    assert resumed['export']['sha256'] == original and file_sha256(output) == original
    assert len(list((p.root / 'production_first_cuts').glob('*.json'))) == 1


@pytest.mark.parametrize('probe_stage', ['media', 'profile'])
def test_initial_postrender_probe_uncertainty_retries_only_verification(p, clips, monkeypatch, probe_stage):
    calls = []
    output = p.root / 'renders/first_cut/v001/first_cut.mp4'
    name = '_probe' if probe_stage == 'media' else '_profile_issues'
    real = getattr(draft, name)
    errors = iter([subprocess.TimeoutExpired('ffprobe', 30)])

    def flaky(path, *args):
        error = next(errors, None) if Path(path) == output else None
        if error:
            raise error
        return real(path, *args)
    monkeypatch.setattr(draft, name, flaky)
    with pytest.raises(draft.RenderProbeUncertain, match='existing output preserved'):
        draft.compose_first_cut(p.root, export_profile='tiktok_720p', operation_id='initial-probe',
                                compose=counting(calls))
    original = file_sha256(output)
    assert len(calls) == 1 and draft.latest_first_cut(p.root) is None
    intent = output.parent / 'compose-intent.json'
    retained_intent = file_sha256(intent)
    resumed = draft.compose_first_cut(p.root, export_profile='tiktok_720p', operation_id='initial-probe',
                                      compose=counting(calls))
    assert resumed['status'] == 'complete' and resumed['sequence'] == 1
    assert len(calls) == 1 and resumed['export']['sha256'] == original and file_sha256(output) == original
    assert file_sha256(intent) == retained_intent
    assert len(list((p.root / 'production_first_cuts').glob('*.json'))) == 1


def test_new_defect_note_updates_standalone_report_without_rerender(p, clips):
    calls = []
    first = draft.deliver_standalone(p.root, export_profile='tiktok_720p', compose=counting(calls))
    assert len(calls) == 1 and all(row['findings'] == [] for row in first['clips'])
    shot_id, path = next(iter(clips.items()))
    revision = read(p.root / 'project.json')['story_revision']
    note = {'name': 'hand_anatomy', 'status': 'fail', 'severity': 'cosmetic',
            'evidence': 'SYNTHETIC fixture note at 00:00.4'}
    rejections = p.root / 'production_attempts' / ('att-' + shot_id) / 'rejections'
    rejections.mkdir(parents=True)
    (rejections / 'note.json').write_text(json.dumps({
        'status': 'fail', 'subject_sha256': file_sha256(path), 'story_revision': revision,
        'predicates': [note]}))
    record_bytes = Path(first['first_cut']['path'])
    record_sha = file_sha256(p.root / record_bytes)
    again = draft.deliver_standalone(p.root, export_profile='tiktok_720p', compose=counting(calls))
    assert len(calls) == 1, 'annotation must not re-render'
    assert again['export'] == first['export'] and again['first_cut'] == first['first_cut']
    assert file_sha256(p.root / record_bytes) == record_sha, 'immutable cut record unchanged'
    assert again['clips'][0]['findings'] == [note]
    assert again['currency'] == {'status': 'current', 'reasons': []}
    assert again['annotation_currency'] == {'status': 'stale', 'reasons': ['findings_changed']}


def test_producer_completes_after_real_rejection_annotation_without_changing_media_or_history(p, monkeypatch):
    from lib import production_execution as execution

    # Actual offline request/attempt/selection APIs; verdicts remain SYNTHETIC.
    for shot in p.contract['shots']:
        sid = shot['id']
        if shot['upstream']:
            p.bind_upstream(sid)
        result = p.generate(sid)
        assert result.success, result.error
        if sid != 'payoff':
            p.select(sid, result)
    aid = result.data['production_attempt_id']
    output_sha = file_sha256(result.artifacts[0])
    note = {'name': 'lighting_polish', 'status': 'fail', 'severity': 'cosmetic',
            'evidence': 'SYNTHETIC slightly warm grade'}
    review = attestation(output_sha, [])
    review.update(status='fail', predicates=[note])
    execution.record_rejection(p.root, aid, review)
    monkeypatch.setattr(subprocess, 'run', REAL_RUN)
    calls = []

    def compose(inputs):
        from tools.base_tool import ToolResult
        calls.append(inputs)
        duration = sum(c['out_seconds'] - c['in_seconds'] for c in inputs['edit_decisions']['cuts'])
        lavfi(Path(inputs['output_path']), 'blue', 440, duration)
        return ToolResult(success=True)

    first = draft.deliver_standalone(p.root, compose=compose)
    assert first['status'] == 'complete' and execution.next_production_action(p.root)['kind'] == 'complete'
    cut = draft.latest_first_cut(p.root)
    evidence = p.root / 'artifacts/creator-watched.txt'
    evidence.write_text('SYNTHETIC creator watched the exact export and disclosure')
    disclosure = p.root / 'artifacts/disclosure.json'
    disclosure.write_text(json.dumps({'first_cut': first['first_cut'], 'export': first['export'],
                                     'clips': draft.disclosure_clip_map(cut), 'findings': [note]}))
    acceptance = draft.record_first_cut_acceptance(p.root, first['first_cut'], accepted_by='synthetic creator',
        evidence={'path': str(evidence), 'sha256': file_sha256(evidence)},
        disclosure={'path': str(disclosure), 'sha256': file_sha256(disclosure)})
    assert draft.first_cut_acceptance_status(p.root, acceptance)['status'] == 'current'
    historical_paths = [*(p.root / 'production_attempts').rglob('*'),
                        p.root / 'production_scopes.json', p.root / 'selected_attempts.json',
                        Path(cut['path']), Path(acceptance['path']), Path(first['export_path'])]
    retained_hashes = {path: file_sha256(path) for path in historical_paths if path.is_file()}
    changed = {**note, 'evidence': 'SYNTHETIC revised cosmetic disclosure at 00:16.4'}
    execution.record_rejection(p.root, aid, {**review, 'predicates': [changed]})
    again = draft.deliver_standalone(p.root, compose=compose)
    assert len(calls) == 1 and again['first_cut'] == first['first_cut'] and again['export'] == first['export']
    assert again['currency'] == {'status': 'current', 'reasons': []}
    assert again['annotation_currency'] == {'status': 'stale', 'reasons': ['findings_changed']}
    assert sorted(again['clips'][-1]['findings'], key=lambda value: value['evidence']) == sorted(
        [note, changed], key=lambda value: value['evidence'])
    assert execution.next_production_action(p.root)['kind'] == 'complete'
    assert execution.advance(p.root)['status'] == 'complete'
    assert draft.first_cut_acceptance_status(p.root, acceptance) == {
        'status': 'stale', 'reasons': ['findings_changed']}
    assert all(file_sha256(path) == sha for path, sha in retained_hashes.items())
    assert len(list((p.root / 'production_first_cuts').glob('*.json'))) == 1
    assert len(list((p.root / 'production_first_cut_acceptances').glob('*.json'))) == 1
    disclosure.write_text(disclosure.read_text() + '\n')
    assert 'disclosure_changed' in draft.first_cut_acceptance_status(p.root, acceptance)['reasons']


@pytest.mark.parametrize('accept_baseline', [False, True])
def test_selected_repair_invalidates_saved_cut_and_composes_new_candidate_once(p, monkeypatch, accept_baseline):
    from lib import production_execution as execution
    from tools.base_tool import ToolResult

    for shot in p.contract['shots']:
        sid = shot['id']
        if shot['upstream']:
            p.bind_upstream(sid)
        result = p.generate(sid)
        assert result.success, result.error
        if sid != 'payoff':
            p.select(sid, result)
    original = result.data['production_attempt_id']
    review = attestation(file_sha256(result.artifacts[0]), [])
    review.update(status='fail', predicates=[{'name': 'lighting_polish', 'status': 'fail',
                  'severity': 'cosmetic', 'evidence': 'SYNTHETIC retained cosmetic finding'}])
    execution.record_rejection(p.root, original, review)
    mock_run = subprocess.run
    monkeypatch.setattr(subprocess, 'run', REAL_RUN)
    calls = []

    def compose(inputs):
        calls.append(inputs)
        duration = sum(c['out_seconds'] - c['in_seconds'] for c in inputs['edit_decisions']['cuts'])
        lavfi(Path(inputs['output_path']), 'blue' if len(calls) == 1 else 'red', 440, duration)
        return ToolResult(success=True)

    first = draft.deliver_standalone(p.root, compose=compose)
    assert execution.next_production_action(p.root)['kind'] == 'complete'
    historical_paths = [p.root / first['first_cut']['path'], Path(first['export_path'])]
    if accept_baseline:
        evidence = p.root / 'artifacts/accept-baseline.txt'
        evidence.write_text('SYNTHETIC creator accepts this exact baseline')
        acceptance = draft.record_first_cut_acceptance(p.root, first['first_cut'], accepted_by='synthetic creator',
            evidence={'path': str(evidence), 'sha256': file_sha256(evidence)})
        historical_paths.append(Path(acceptance['path']))
    retained = {path: file_sha256(path) for path in historical_paths}
    monkeypatch.setattr(subprocess, 'run', mock_run)
    repair = copy.deepcopy(p.scope)
    repair.update(id='payoff-repair', phase='repair', replaces_attempt_ids=[original], attempts_per_shot={'payoff': 1})
    inputs = copy.deepcopy(p.inputs['payoff'])
    inputs['governance']['scope_id'] = repair['id']
    inputs['output_path'] = str(p.root / 'assets/video/payoff-repair.mp4')
    repair['requests'] = {'payoff': execution.planned_request_digest(inputs, project_dir=p.root)}
    save(p.root / 'production_scopes.json', {'version': '1.0', 'scopes': [p.scope, repair]})
    replacement = p.selector.execute(inputs)
    assert replacement.success, replacement.error
    selected = p.select('payoff', replacement)
    monkeypatch.setattr(subprocess, 'run', REAL_RUN)
    candidate = draft.first_cut_candidates(p.root)[-1]
    assert candidate['attempt_id'] == selected['attempt_id']
    assert draft.first_cut_media_status(p.root, first['first_cut']) == {
        'status': 'stale', 'reasons': ['candidate_bindings_changed']}
    assert execution.next_production_action(p.root)['kind'] == 'compose_cut'
    with pytest.raises(ValueError, match='different clips, profile or audio'):
        draft.deliver_standalone(p.root, operation_id=first['operation_id'], compose=compose)
    fresh = draft.deliver_standalone(p.root, compose=compose)
    assert len(calls) == 2 and fresh['status'] == 'complete'
    assert fresh['operation_id'] != first['operation_id'] and fresh['export'] != first['export']
    assert fresh['clips'][-1]['attempt_id'] == selected['attempt_id']
    assert fresh['clips'][-1]['output'] == candidate['output']
    assert fresh['currency'] == {'status': 'current', 'reasons': []}
    assert execution.next_production_action(p.root)['kind'] == 'complete'
    assert execution.advance(p.root)['status'] == 'complete'
    replay = draft.deliver_standalone(p.root, compose=compose)
    assert replay['first_cut'] == fresh['first_cut'] and len(calls) == 2
    assert all(file_sha256(path) == digest for path, digest in retained.items())
    assert len(list((p.root / 'production_first_cuts').glob('*.json'))) == 2
    if accept_baseline:
        assert 'candidate_bindings_changed' in draft.first_cut_acceptance_status(p.root, acceptance)['reasons']


def test_changed_candidate_timing_does_not_replay_an_obsolete_placement(p, clips, monkeypatch):
    calls = []
    first = draft.deliver_standalone(p.root, compose=counting(calls))
    original_rows = draft.first_cut_candidates(p.root)
    current_rows = [{**row, 'duration_seconds': 0.5 if index == 0 else row['duration_seconds']}
                    for index, row in enumerate(original_rows)]
    # Canonical candidate timing seam; source identities and bytes remain exact.
    monkeypatch.setattr(draft, 'first_cut_candidates', lambda root: current_rows)
    assert 'candidate_bindings_changed' in draft.first_cut_media_status(p.root, first['first_cut'])['reasons']
    fresh = draft.deliver_standalone(p.root, compose=counting(calls))
    assert len(calls) == 2 and fresh['status'] == 'complete'
    assert fresh['operation_id'] == first['operation_id'], 'unchanged clip/profile/audio intent identity retained'
    assert fresh['clips'][0]['master_end'] == 0.5 and fresh['technical']['duration_seconds'] == pytest.approx(2.5, abs=0.05)
    assert fresh['currency'] == {'status': 'current', 'reasons': []}
    replay = draft.deliver_standalone(p.root, compose=counting(calls))
    assert len(calls) == 2 and replay['first_cut'] == fresh['first_cut']
    assert file_sha256(first['export_path']) == first['export']['sha256']
    assert file_sha256(p.root / first['first_cut']['path']) == first['first_cut']['sha256']


@pytest.mark.parametrize('damage', ['delete', 'corrupt'])
def test_replay_with_missing_or_changed_export_rerenders_locally(p, clips, damage):
    calls = []
    first = draft.compose_first_cut(p.root, export_profile='tiktok_720p', operation_id='op-x',
                                    compose=counting(calls))
    exported = p.root / first['export']['path']
    if damage == 'delete':
        exported.unlink()
    else:
        exported.write_bytes(exported.read_bytes()[:-100] + b'changed')
        changed = file_sha256(exported)
    again = draft.compose_first_cut(p.root, export_profile='tiktok_720p', operation_id='op-x',
                                    compose=counting(calls))
    assert len(calls) == 2 and again['status'] == 'complete' and again['sequence'] == 2
    fresh = p.root / again['export']['path']
    assert fresh.is_file() and file_sha256(fresh) == again['export']['sha256']
    assert Path(first['path']).is_file(), 'prior cut record retained'
    if damage == 'corrupt':
        assert file_sha256(exported) == changed, 'existing changed bytes preserved'
    assert draft.latest_first_cut(p.root)['path'] == again['path']
    replay = draft.compose_first_cut(p.root, export_profile='tiktok_720p', operation_id='op-x',
                                     compose=counting(calls))
    assert len(calls) == 2 and replay['path'] == again['path']


def test_restored_older_export_does_not_shadow_newer_recovery(p, clips):
    calls = []
    first = draft.compose_first_cut(p.root, export_profile='tiktok_720p', operation_id='op-r',
                                    compose=counting(calls))
    exported = p.root / first['export']['path']
    original = exported.read_bytes()
    exported.unlink()
    recovered = draft.compose_first_cut(p.root, export_profile='tiktok_720p', operation_id='op-r',
                                        compose=counting(calls))
    exported.write_bytes(original)  # v001 restored intact after v002 recovery
    replay = draft.compose_first_cut(p.root, export_profile='tiktok_720p', operation_id='op-r',
                                     compose=counting(calls))
    assert len(calls) == 2 and replay['sequence'] == 2 and replay['path'] == recovered['path']


def test_standalone_report_projects_current_review_successor(p, clips, monkeypatch):
    calls = []
    first = draft.deliver_standalone(p.root, export_profile='tiktok_720p', compose=counting(calls))
    assert first['clips'][0]['review'] == 'unknown'
    # SYNTHETIC verdict boundary: a current successor review now passes these bytes.
    monkeypatch.setattr(draft, '_current_findings',
                        lambda *args: ({'status': 'pass', 'predicates': []}, []))
    again = draft.deliver_standalone(p.root, export_profile='tiktok_720p', compose=counting(calls))
    assert len(calls) == 1 and again['export'] == first['export']
    row = again['clips'][0]
    assert row['review'] == 'pass' and row['review_at_render'] == 'unknown'
