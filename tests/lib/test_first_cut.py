"""U3 first cut: truthful candidates, real-AV preview, creator acceptance.

Candidate/status logic uses the offline integration Production fixture (fake
native bytes, probe seam patched). Composition behavior uses tiny real lavfi
clips through the actual VideoCompose FFmpeg path.
"""
from __future__ import annotations

import copy
import shutil
import subprocess
from pathlib import Path

import pytest

from lib import production_draft as draft
from lib.production_execution import ProductionGovernanceError, record_rejection, record_selection, reconcile_attempt
from lib.shot_contract import file_sha256
from tests.integration.test_first_pass_workflow import attestation, production, read  # noqa: F401
from tools.base_tool import ToolResult
from tools.video.video_compose import VideoCompose

REAL_RUN = subprocess.run  # captured before the offline fixture patches the module

needs_ff = pytest.mark.skipif(not (shutil.which('ffmpeg') and shutil.which('ffprobe')),
                              reason='ffmpeg/ffprobe unavailable')


@pytest.fixture
def p(production, monkeypatch):
    real = draft._probe

    def probe(path):
        if Path(path).read_bytes().startswith(b'SYNTHETIC'):
            return {'duration_seconds': 8.0, 'video_codec': 'h264', 'audio_codec': 'aac',
                    'has_audio': True, 'width': 1280, 'height': 720}
        return real(path)
    monkeypatch.setattr(draft, '_probe', probe)
    return production


def rows(p):
    return {row['shot_id']: row for row in draft.first_cut_candidates(p.root)}


def lavfi(path, color, freq, seconds=1.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'color=c={color}:s=160x90:r=30:d={seconds}',
                    '-f', 'lavfi', '-i', f'sine=frequency={freq}:sample_rate=48000:duration={seconds}',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(path)], check=True)
    return path


def fake_compose(inputs):
    return ToolResult(success=False, error='synthetic render failure')


def test_failed_predicate_candidate_is_disclosed_never_strict(p):
    result = p.generate('entry')
    aid = result.data['production_attempt_id']
    output = {'path': result.artifacts[0], 'sha256': file_sha256(result.artifacts[0])}
    review = attestation(output['sha256'], ['possession'])
    review.update(status='fail', predicates=[{'name': 'possession', 'status': 'fail', 'evidence': 'duplicate bill'}])
    record_rejection(p.root, aid, review)
    entry = rows(p)['entry']
    assert entry['status'] == 'candidate' and entry['attempt_id'] == aid and entry['review'] == 'fail'
    assert entry['strict_selected'] is False and entry['findings'][0]['name'] == 'possession'
    assert rows(p)['interior'] == {'shot_id': 'interior', 'status': 'missing', 'reason': 'upstream_blocked',
                                   'detail': {'upstream_shot_id': 'entry', 'why': 'failed'}}
    frame = p.root / 'assets/images/x.png'
    frame.write_bytes(b'x')
    with pytest.raises(ProductionGovernanceError):
        record_selection(p.root, 'entry', {'attempt_id': aid, 'output': output,
            'outgoing_frame': {'path': str(frame), 'sha256': file_sha256(frame)}, 'review': review})
    cut = draft.compose_first_cut(p.root, compose=fake_compose)
    assert cut['status'] == 'render_failed' and cut['render_error'] == 'synthetic render failure'
    assert cut['clips'][0]['findings'][0]['name'] == 'possession' and cut['export'] is None
    assert (p.root / cut['render_log']['path']).is_file()
    assert not (p.root / 'selected_attempts.json').exists()


def test_unreviewed_candidate_stays_unknown_and_later_pending_keeps_candidate(p):
    p.generate('entry')
    assert rows(p)['entry']['review'] == 'unknown'
    assert rows(p)['interior']['detail']['why'] == 'unreviewed'
    assert rows(p)['payoff']['detail'] == {'upstream_shot_id': 'interior', 'why': 'missing'}


def test_pending_and_failed_originals_report_exact_reasons_without_repair(p):
    p.transport.timeout_next = True
    original = p.generate('entry')
    aid = original.data['production_attempt_id']
    assert rows(p)['entry'] == {'shot_id': 'entry', 'status': 'missing', 'reason': 'original_pending',
                                'detail': {'status': 'uncertain'}}
    request = read(p.root / 'production_attempts' / aid / 'request.json')
    failed = copy.deepcopy(original)
    failed.error = 'Synthetic authoritative original native failure'
    failed.data['conditioning_receipt'].update(submission_evidence='verified_native_call', dispatch_status='failed')
    failed.data['dispatch_status'] = 'failed'
    reconcile_attempt(p.root, aid, failed, request_sha256=request['request_sha256'])
    assert rows(p)['entry']['reason'] == 'attempt_failed'
    cut = draft.compose_first_cut(p.root, compose=fake_compose)
    assert cut['status'] == 'incomplete' and cut['export'] is None and cut['clips'] == []
    assert [m['reason'] for m in cut['missing']] == ['attempt_failed', 'upstream_blocked', 'upstream_blocked']
    assert len(p.transport.native_requests) == 1
    assert not list(p.root.glob('checkpoint_assets*')) and not list(p.root.glob('checkpoint_compose*'))


def test_external_audio_rejected_for_dialogue(p):
    p.generate('entry')
    with pytest.raises(ValueError, match='native dialogue'):
        draft.compose_first_cut(p.root, audio_path=p.root / 'music.wav', compose=fake_compose)
    assert not (p.root / 'production_first_cuts').exists()


@needs_ff
def test_real_av_preview_intervals_order_native_audio_and_guard(tmp_path):
    a = lavfi(tmp_path / 'a.mp4', 'red', 440, 1.0)
    b = lavfi(tmp_path / 'b.mp4', 'blue', 880, 1.5)
    music = tmp_path / 'music.wav'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo',
                    '-t', '3', str(music)], check=True)
    edit = {'version': '1.0', 'render_runtime': 'ffmpeg', 'metadata': {'preserve_source_audio': True,
            'compose_target': {'width': 160, 'height': 90, 'fit': 'pad'}},
            'cuts': [{'id': 'a', 'source': str(a), 'in_seconds': 0, 'out_seconds': 1.0},
                     {'id': 'b', 'source': str(b), 'in_seconds': 0, 'out_seconds': 1.5}]}
    out = tmp_path / 'out.mp4'
    for operation in ('compose', 'render'):
        guarded = VideoCompose().execute({'operation': operation, 'edit_decisions': edit,
                                          'output_path': str(out), 'audio_path': str(music)})
        assert not guarded.success and 'preserve_source_audio' in guarded.error and not out.exists()
    assert VideoCompose().execute({'operation': 'compose', 'edit_decisions': edit, 'output_path': str(out)}).success
    tech = draft._probe(out)
    assert tech['has_audio'] and abs(tech['duration_seconds'] - 2.5) < 0.1
    assert (tech['width'], tech['height']) == (160, 90)

    def sample(t):
        pixel = subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(t), '-i', str(out), '-frames:v', '1',
                                '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', '1x1', '-'],
                               capture_output=True, check=True).stdout
        level = subprocess.run(['ffmpeg', '-ss', str(t), '-t', '0.3', '-i', str(out), '-af', 'volumedetect',
                                '-f', 'null', '-'], capture_output=True, text=True).stderr
        return pixel, float(level.split('mean_volume:')[1].split('dB')[0])
    (red, red_db), (blue, blue_db) = sample(0.4), sample(1.8)
    assert red[0] > 150 and red[2] < 100 and blue[2] > 150 and blue[0] < 100
    assert red_db > -40 and blue_db > -40  # native sine survives, not silence


@needs_ff
def test_compose_first_cut_versions_acceptance_and_staleness(p, monkeypatch):
    # No generation here: restore real local FFmpeg for tiny lavfi fixtures.
    monkeypatch.setattr(subprocess, 'run', REAL_RUN)
    clips = {sid: lavfi(p.root / f'assets/video/{sid}-real.mp4', color, freq, 1.0)
             for sid, color, freq in [('entry', 'red', 440), ('interior', 'green', 660)]}

    def fake_rows(root):
        out = [{'shot_id': sid, 'status': 'candidate', 'attempt_id': 'att-' + sid,
                'output': {'path': str(path.relative_to(p.root)), 'sha256': file_sha256(path)},
                'duration_seconds': 1.0, 'strict_selected': False, 'review': 'unknown', 'findings': []}
               for sid, path in clips.items()]
        return out + [{'shot_id': 'payoff', 'status': 'missing', 'reason': 'no_attempt', 'detail': {}}]
    monkeypatch.setattr(draft, 'first_cut_candidates', fake_rows)
    first = draft.compose_first_cut(p.root)
    assert first['status'] == 'incomplete' and first['missing'][0]['reason'] == 'no_attempt'
    assert first['export']['path'] == 'renders/first_cut/v001/first_cut.mp4' and first['technical']['has_audio']
    assert [(c['master_start'], c['master_end']) for c in first['clips']] == [(0.0, 1.0), (1.0, 2.0)]
    assert first['audio'] == 'native_source'
    evidence = p.root / 'artifacts/creator-ok.txt'
    evidence.write_text('creator watched v001')
    cut = {'path': first['path'], 'sha256': first['sha256']}
    acceptance = draft.record_first_cut_acceptance(p.root, cut, accepted_by='creator',
        evidence={'path': str(evidence), 'sha256': file_sha256(evidence)})
    stored = read(Path(acceptance['path']))
    assert stored['not_certification'] is True and stored['export'] == first['export']
    assert stored['status_at_acceptance'] == 'incomplete'
    assert draft.first_cut_acceptance_status(p.root, acceptance) == {'status': 'current', 'reasons': []}
    original = clips['entry'].read_bytes()
    clips['entry'].write_bytes(original + b'\0')
    assert 'source_changed:entry' in draft.first_cut_acceptance_status(p.root, acceptance)['reasons']
    clips['entry'].write_bytes(original)
    second = draft.compose_first_cut(p.root)
    assert second['export']['path'] == 'renders/first_cut/v002/first_cut.mp4'
    assert file_sha256(p.root / first['export']['path']) == first['export']['sha256']
    assert draft.first_cut_acceptance_status(p.root, acceptance)['reasons'] == ['superseded']
    assert draft.latest_first_cut(p.root)['sequence'] == 2
    exported = p.root / second['export']['path']
    exported.write_bytes(b'tampered')
    later = {'path': second['path'], 'sha256': second['sha256']}
    assert 'export_changed' in draft.first_cut_status(p.root, later)['reasons']
    with pytest.raises(ValueError, match='current first-cut'):
        draft.record_first_cut_acceptance(p.root, later, accepted_by='creator',
            evidence={'path': str(evidence), 'sha256': file_sha256(evidence)})


def silent(path, color, seconds=1.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    REAL_RUN(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'color=c={color}:s=160x90:r=30:d={seconds}',
              '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path)], check=True)
    return path


@needs_ff
@pytest.mark.parametrize('with_dialogue', [False, True])
def test_audio_requirement_follows_contract_dialogue(p, monkeypatch, with_dialogue):
    """Approved silent coverage completes without audio; dialogue lacking audio stays incomplete."""
    from lib import production_execution as execution
    monkeypatch.setattr(subprocess, 'run', REAL_RUN)
    contract = copy.deepcopy(execution.load_shot_contract(p.root))
    for shot in contract['shots']:
        if not with_dialogue:
            shot['dialogue'] = []
    if with_dialogue:
        assert any(shot.get('dialogue') for shot in contract['shots'])
    monkeypatch.setattr(execution, 'load_shot_contract', lambda root: contract)
    clips = {shot['id']: silent(p.root / f"assets/video/{shot['id']}-silent.mp4", 'gray')
             for shot in contract['shots']}
    monkeypatch.setattr(draft, 'first_cut_candidates', lambda root: [
        {'shot_id': sid, 'status': 'candidate', 'attempt_id': 'att-' + sid,
         'output': {'path': str(path.relative_to(p.root)), 'sha256': file_sha256(path)},
         'duration_seconds': 1.0, 'strict_selected': False, 'review': 'unknown', 'findings': []}
        for sid, path in clips.items()])
    cut = draft.compose_first_cut(p.root)
    assert cut['export'] is not None
    assert cut['audio_required'] is with_dialogue
    assert bool(cut['audio_missing']) is with_dialogue
    assert cut['status'] == ('incomplete' if with_dialogue else 'complete')


@pytest.mark.parametrize('status', ['prepared', 'submitted', 'consumed_awaiting_original_receipt', 'completed'])
def test_nonterminal_mcp_states_are_pending_not_failed(p, monkeypatch, status):
    from lib import production_execution as execution
    monkeypatch.setattr(execution, '_attempts', lambda root: [
        {'attempt_id': 'mcp-1', 'provider': 'openart_mcp', 'shot_id': 'entry',
         'story_revision': 'courier-story-1', 'scope_attempt_index': 0}])
    monkeypatch.setattr(execution, 'load_attempt_result', lambda root, aid: {'status': status})
    assert rows(p)['entry']['reason'] == 'original_pending'
    assert rows(p)['entry']['detail'] == {'status': status}


def test_terminal_mcp_failure_is_attempt_failed(p, monkeypatch):
    from lib import production_execution as execution
    monkeypatch.setattr(execution, '_attempts', lambda root: [
        {'attempt_id': 'mcp-1', 'provider': 'openart_mcp', 'shot_id': 'entry', 'story_revision': 'courier-story-1'}])
    monkeypatch.setattr(execution, 'load_attempt_result', lambda root, aid: {'status': 'failed'})
    assert rows(p)['entry']['reason'] == 'attempt_failed'


def test_mcp_attempts_order_by_retained_creation_chronology(tmp_path):
    import os
    for aid, when in [('zzz-old', 1_000), ('aaa-new', 2_000)]:
        path = tmp_path / 'openart_mcp' / 'attempts' / aid / 'evidence.json'
        path.parent.mkdir(parents=True)
        path.write_text('{}')
        os.utime(path, ns=(when * 10**9, when * 10**9))
    own = [{'attempt_id': a, 'provider': 'openart_mcp'} for a in ('zzz-old', 'aaa-new')]
    newest = sorted(own, key=lambda a: draft._attempt_order(tmp_path, a), reverse=True)
    assert [a['attempt_id'] for a in newest] == ['aaa-new', 'zzz-old']


def test_selected_pass_discloses_current_cosmetic_findings_once(p):
    _select_entry_with_cosmetic(p)
    entry = rows(p)['entry']
    assert entry['strict_selected'] is True and entry['review'] == 'pass'
    assert [f['name'] for f in entry['findings']] == ['lighting_polish']
    assert entry['findings'][0]['severity'] == 'cosmetic'


def _select_entry_with_cosmetic(p, *, prior_rejection=True):
    from lib.shot_contract import UPSTREAM_PREDICATES, selection_digest
    result = p.generate('entry')
    aid = result.data['production_attempt_id']
    output = {'path': result.artifacts[0], 'sha256': file_sha256(result.artifacts[0])}
    if prior_rejection:
        old = attestation(output['sha256'], ['possession'])
        old.update(status='fail', predicates=[{'name': 'possession', 'status': 'fail', 'evidence': 'old defect'}])
        record_rejection(p.root, aid, old)
    frame = p.root / 'assets/images/entry-frame.png'
    frame.write_bytes(b'frame')
    selection = {'attempt_id': aid, 'output': output,
                 'outgoing_frame': {'path': str(frame), 'sha256': file_sha256(frame)}}
    review = attestation(selection_digest(selection), UPSTREAM_PREDICATES)
    review['predicates'].append({'name': 'lighting_polish', 'status': 'fail', 'severity': 'cosmetic',
                                 'evidence': 'slightly warm grade'})
    selection['review'] = review
    record_selection(p.root, 'entry', selection)
    return selection


def test_accepted_cut_with_selected_cosmetic_findings_stays_current(p, monkeypatch):
    _select_entry_with_cosmetic(p)
    monkeypatch.setattr(subprocess, 'run', REAL_RUN)

    def compose(inputs):  # real playable bytes at the requested export path
        lavfi(Path(inputs['output_path']), 'blue', 440, 1.0)
        return ToolResult(success=True)
    cut = draft.compose_first_cut(p.root, compose=compose)
    assert [f['name'] for f in cut['clips'][0]['findings']] == ['lighting_polish']
    binding = {'path': cut['path'], 'sha256': cut['sha256']}
    assert draft.first_cut_status(p.root, binding) == {'status': 'current', 'reasons': []}
    evidence = p.root / 'artifacts/creator-ok.txt'
    evidence.write_text('creator watched')
    acceptance = draft.record_first_cut_acceptance(p.root, binding, accepted_by='creator',
        evidence={'path': str(evidence), 'sha256': file_sha256(evidence)})
    assert draft.first_cut_acceptance_status(p.root, acceptance) == {'status': 'current', 'reasons': []}


def _patch_selected_review(p, monkeypatch, mutate):
    from lib import production_execution as execution
    real = execution.load_selected_attempts

    def load(root):
        selected = copy.deepcopy(real(root))
        mutate(selected['entry']['review'])
        return selected
    monkeypatch.setattr(execution, 'load_selected_attempts', load)


@pytest.mark.parametrize('field,value', [('story_revision', 'courier-story-0'), ('subject_sha256', '0' * 64)])
def test_stale_selected_review_is_not_strict_and_blocks_downstream(p, monkeypatch, field, value):
    _select_entry_with_cosmetic(p, prior_rejection=False)
    assert rows(p)['interior']['reason'] == 'no_attempt'
    _patch_selected_review(p, monkeypatch, lambda review: review.update({field: value}))
    entry, interior = rows(p)['entry'], rows(p)['interior']
    assert entry['status'] == 'candidate' and entry['strict_selected'] is False and entry['review'] == 'unknown'
    assert entry['findings'] == []
    assert interior['reason'] == 'upstream_blocked' and interior['detail']['upstream_shot_id'] == 'entry'


@pytest.mark.parametrize('bound', [True, False])
def test_provisional_unknown_audio_review_stays_honest(p, monkeypatch, bound):
    _select_entry_with_cosmetic(p, prior_rejection=False)

    def provisional(review):
        review.update(status='provisional', draft_policy_sha256='0' * 64)  # schema-required binding
        for item in review['predicates']:
            if item['name'] == 'speaker_source':
                item['status'] = 'unknown'
    _patch_selected_review(p, monkeypatch, provisional)
    monkeypatch.setattr(draft, 'provisional_audio_review', lambda review, root: bound)
    entry, interior = rows(p)['entry'], rows(p)['interior']
    assert entry['strict_selected'] is False and entry['review'] == 'provisional'
    assert {f['name']: f['status'] for f in entry['findings']}['speaker_source'] == 'unknown'
    if bound:
        assert interior['reason'] == 'no_attempt'
    else:
        assert interior['detail'] == {'upstream_shot_id': 'entry', 'why': 'unreviewed'}


def test_changed_selected_output_is_unplayable_and_blocks_downstream(p):
    selection = _select_entry_with_cosmetic(p, prior_rejection=False)
    assert rows(p)['interior']['reason'] == 'no_attempt'
    source = Path(selection['output']['path'])
    source = source if source.is_absolute() else p.root / source
    source.write_bytes(source.read_bytes() + b'changed')
    entry, interior = rows(p)['entry'], rows(p)['interior']
    assert entry['status'] == 'missing' and entry['reason'] == 'unplayable'
    assert interior['reason'] == 'upstream_blocked'
    assert interior['detail'] == {'upstream_shot_id': 'entry', 'why': 'missing'}
