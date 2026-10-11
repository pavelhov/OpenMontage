"""U4 engine-only delivery: explicit export profile, intent-before-render recovery.

Real local FFmpeg renders tiny lavfi clips; no Studio, Drive, account prefix or
provider call participates.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from lib import production_draft as draft
from lib.media_profiles import delivery_timing_issues
from lib.shot_contract import file_sha256
from tests.integration.test_first_pass_workflow import attestation, production, read  # noqa: F401
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
    monkeypatch.setattr(draft, 'first_cut_candidates', lambda root: [
        {'shot_id': sid, 'status': 'candidate', 'attempt_id': 'att-' + sid,
         'output': {'path': str(path.relative_to(p.root)), 'sha256': file_sha256(path)},
         'duration_seconds': 1.0, 'strict_selected': False, 'review': 'unknown', 'findings': []}
        for sid, path in paths.items()])
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


def test_partial_local_output_rerenders_under_same_operation(p, clips):
    calls = []

    def truncate(output):
        output.write_bytes(output.read_bytes()[:2000])
        raise KeyboardInterrupt('host died mid-write')
    with pytest.raises(KeyboardInterrupt):
        draft.compose_first_cut(p.root, operation_id='op-2', compose=counting(calls, after=truncate))
    resumed = draft.compose_first_cut(p.root, operation_id='op-2', compose=counting(calls))
    assert len(calls) == 2 and resumed['sequence'] == 1 and resumed['status'] == 'complete'
    assert resumed['export']['path'] == 'renders/first_cut/v001/first_cut.mp4'


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


def test_unreadable_resumed_output_rerenders_instead_of_raising(p, clips, monkeypatch):
    calls = []

    def crash(_output):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        draft.compose_first_cut(p.root, export_profile='tiktok_720p', operation_id='op-probe',
                                compose=counting(calls, after=crash))
    real = draft._profile_issues
    broken = iter([subprocess.CalledProcessError(1, 'ffprobe')])

    def flaky(path, profile):
        error = next(broken, None)
        if error:
            raise error
        return real(path, profile)
    monkeypatch.setattr(draft, '_profile_issues', flaky)
    resumed = draft.compose_first_cut(p.root, export_profile='tiktok_720p', operation_id='op-probe',
                                      compose=counting(calls))
    assert len(calls) == 2 and resumed['status'] == 'complete' and resumed['sequence'] == 1


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
    assert 'findings_changed' in again['currency']['reasons']


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
