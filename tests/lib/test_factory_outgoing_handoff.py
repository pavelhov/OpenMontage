"""Factual outgoing extraction/replay; real local media, no provider transport."""
import copy
import json
import os
import shutil
import subprocess

import pytest

from lib import production_execution as execution, shot_contract
from lib.shot_contract import file_sha256
from tools.analysis.frame_sampler import FrameSampler


@pytest.fixture
def original(tmp_path, monkeypatch):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('local ffmpeg required')
    root = tmp_path.resolve()
    directory = root / 'production_attempts/original-A'
    directory.mkdir(parents=True)
    (root / 'project.json').write_text(json.dumps({'project_id': 'offline', 'story_revision': 'r1'}))
    video = directory / 'original.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
        'testsrc2=size=160x90:rate=10:duration=1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video)], check=True)
    state = {'status': 'generated', 'output': {'path': str(video), 'sha256': file_sha256(video)}}
    monkeypatch.setattr(shot_contract, 'factory_project', lambda _: True)
    monkeypatch.setattr(execution, 'load_attempt_result', lambda *_: copy.deepcopy(state))
    monkeypatch.setattr('lib.production_provenance.validate_attempt_provenance',
        lambda *args, **kwargs: {'request': {'project_id': 'offline', 'story_revision': 'r1', 'shot_id': 'A'},
                                'result': copy.deepcopy(state)})
    return root, directory, state


def test_outgoing_is_last_actual_frame_and_unknown_selection_replays_without_sampling(original, monkeypatch):
    root, directory, state = original
    with pytest.raises(ValueError, match='lacks its retained sampling receipt'):
        execution._factory_outgoing_selection(root, 'A', 'original-A')
    assert not (directory / 'factory_outgoing_sampling.json').exists()
    selected = execution._factory_outgoing_selection(root, 'A', 'original-A', sample=True)
    expected = FrameSampler().execute({'input_path': state['output']['path'], 'strategy': 'timestamps',
        'timestamps': [.9], 'format': 'png', 'output_dir': str(root / 'expected')})
    assert expected.success
    assert file_sha256(expected.data['frames'][0]['path']) == selected['outgoing_frame']['sha256']
    receipt = json.loads((directory / 'factory_outgoing_sampling.json').read_text())
    assert receipt['submitted_inputs']['timestamps'] == [.9]
    assert receipt['input'] == state['output']
    assert selected['review']['status'] == 'unknown'
    assert all(p['status'] == 'unknown' for p in selected['review']['predicates'])
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('replay must reuse saved sampling'))
    assert execution._factory_outgoing_selection(root, 'A', 'original-A') == selected
    # The same factual selection cannot bypass generic manifest review admission.
    with pytest.raises(ValueError, match='failed or stale'):
        execution.record_selection(root, 'A', selected)
    assert execution.load_selected_attempts(root) == {}


def test_outgoing_sampler_failure_retains_terminal_local_defect_without_replacing_original(original, monkeypatch):
    root, directory, state = original
    from tools.base_tool import ToolResult
    monkeypatch.setattr(FrameSampler, 'execute', lambda *a, **k: ToolResult(success=False, error='offline decode failure'))
    assert execution._factory_outgoing_selection(root, 'A', 'original-A', sample=True) is None
    receipt = json.loads((directory / 'factory_outgoing_sampling.json').read_text())
    assert receipt['input'] == state['output'] and receipt['outgoing_frame'] is None
    assert receipt['tool_result']['error'] == 'offline decode failure'
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('failed sampling is not rerun'))
    assert execution._factory_outgoing_selection(root, 'A', 'original-A', sample=True) is None
    assert execution.load_selected_attempts(root) == {}


def test_factory_selection_publication_recovers_interrupted_pair_without_duplicate_history(original, monkeypatch):
    root, *_ = original
    selected = execution._factory_outgoing_selection(root, 'A', 'original-A', sample=True)
    replace = os.replace
    monkeypatch.setattr(os, 'replace', lambda *a: (_ for _ in ()).throw(OSError('interrupted')))
    with pytest.raises(OSError, match='interrupted'):
        execution._publish_selection_locked(root, 'A', selected, history_id='factory-original-original-A')
    assert len(list((root / 'production_selections').glob('*.json'))) == 1
    monkeypatch.setattr(os, 'replace', replace)
    execution._publish_selection_locked(root, 'A', selected, history_id='factory-original-original-A')
    assert execution.load_selected_attempts(root) == {'A': selected}
    execution._publish_selection_locked(root, 'A', selected, history_id='factory-original-original-A')
    assert len(list((root / 'production_selections').glob('*.json'))) == 1


@pytest.mark.parametrize('changed', ['original', 'frame', 'receipt'])
def test_outgoing_replay_rejects_changed_original_frame_or_receipt(original, changed):
    root, directory, state = original
    selected = execution._factory_outgoing_selection(root, 'A', 'original-A', sample=True)
    if changed == 'original':
        target = state['output']['path']
    elif changed == 'frame':
        target = selected['outgoing_frame']['path']
    else:
        target = directory / 'factory_outgoing_sampling.json'
    from pathlib import Path
    Path(target).chmod(0o644)
    Path(target).write_bytes(b'changed')
    with pytest.raises(ValueError):
        execution._factory_outgoing_selection(root, 'A', 'original-A')


def test_generic_project_cannot_create_automatic_outgoing_selection(original, monkeypatch):
    root, directory, _ = original
    monkeypatch.setattr(shot_contract, 'factory_project', lambda _: False)
    with pytest.raises(ValueError, match='activated standing make_video'):
        execution._factory_outgoing_selection(root, 'A', 'original-A', sample=True)
    assert not (directory / 'factory_outgoing_sampling.json').exists()


def test_retained_outgoing_receipt_uses_frozen_authority_after_current_dispatch_is_disabled(original, monkeypatch):
    root, _, _ = original
    selected = execution._factory_outgoing_selection(root, 'A', 'original-A', sample=True)
    monkeypatch.setattr(shot_contract, 'factory_project', lambda _: False)
    monkeypatch.setattr(shot_contract, 'frozen_factory_authority', lambda *_: True)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('source-only read must not sample'))
    assert execution._factory_outgoing_selection(root, 'A', 'original-A') == selected
    with pytest.raises(ValueError, match='activated standing make_video'):
        execution._factory_outgoing_selection(root, 'A', 'original-A', sample=True)


@pytest.mark.parametrize('mismatch', [False, True])
def test_existing_selection_must_match_actual_sampled_frame_without_overwriting_prior_review(original, mismatch):
    root, directory, _ = original
    selected = execution._factory_outgoing_selection(root, 'A', 'original-A', sample=True)
    selected['review']['reviewer'] = 'SYNTHETIC prior reviewer; no real semantic claim'
    selected['review']['status'] = 'pass'
    for predicate in selected['review']['predicates']:
        predicate['status'] = 'pass'
    if mismatch:
        authored = root / 'authored-endboard.png'
        authored.write_bytes(b'SYNTHETIC authored illustration is not actual outgoing footage')
        selected['outgoing_frame'] = {'path': str(authored), 'sha256': file_sha256(authored)}
    selected['review']['subject_sha256'] = shot_contract.selection_digest(selected)
    execution._publish_selection_locked(root, 'A', selected)
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    if mismatch:
        with pytest.raises(ValueError, match='differs from actual original outgoing sampling'):
            execution._factory_handoff_selection_locked(root, 'A', 'original-A')
    else:
        assert execution._factory_handoff_selection_locked(root, 'A', 'original-A') == selected
    assert execution.load_selected_attempts(root)['A'] == selected
    assert {p: p.read_bytes() for p in root.rglob('*') if p.is_file()} == before
    assert len(list((root / 'production_selections').glob('*.json'))) == 1
