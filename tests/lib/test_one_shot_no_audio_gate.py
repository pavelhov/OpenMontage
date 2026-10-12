"""The one-shot factory retains native audio and never gates draft delivery on it."""
from __future__ import annotations

import inspect
import json
import subprocess

import pytest

from lib import production_draft as draft, production_execution as execution
from lib.shot_contract import file_sha256
from tests.integration.test_first_pass_workflow import production
from tests.lib.test_first_cut import REAL_RUN, needs_ff
from tools.video.video_compose import VideoCompose

pytestmark = needs_ff


@pytest.fixture
def p(production, monkeypatch):
    real = draft._probe

    def probe(path):
        from pathlib import Path
        if Path(path).read_bytes().startswith(b'SYNTHETIC'):
            return {'duration_seconds': 8.0, 'video_codec': 'h264', 'audio_codec': 'aac',
                    'has_audio': True, 'width': 1280, 'height': 720}
        return real(path)

    monkeypatch.setattr(draft, '_probe', probe)
    return production


def silent(path, seconds=1.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    REAL_RUN(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
              f'color=c=gray:s=160x90:r=30:d={seconds}', '-c:v', 'libx264',
              '-pix_fmt', 'yuv420p', str(path)], check=True)
    return path


def with_native_tone(path, seconds=1.0):
    """Create a real source clip with a recognizable native audio signal."""
    path.parent.mkdir(parents=True, exist_ok=True)
    REAL_RUN(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
              f'color=c=gray:s=160x90:r=30:d={seconds}', '-f', 'lavfi', '-i',
              f'sine=frequency=997:sample_rate=48000:duration={seconds}',
              '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
              '-b:a', '128k', '-shortest', str(path)], check=True)
    return path


def test_factory_compose_has_no_external_audio_writer_or_quality_gate(p, monkeypatch):
    monkeypatch.setattr(subprocess, 'run', REAL_RUN)
    contract = execution.load_shot_contract(p.root)
    # Dialogue is present in the contract while every source and the final cut
    # are silent. That must not block a technically playable draft.
    assert any(shot.get('dialogue') for shot in contract['shots'])
    clips = {shot['id']: silent(p.root / f"assets/video/{shot['id']}-silent.mp4")
             for shot in contract['shots']}
    assert all(draft._probe(path)['has_audio'] is False for path in clips.values())
    monkeypatch.setattr(draft, 'first_cut_candidates', lambda root: [
        {'shot_id': sid, 'status': 'candidate', 'attempt_id': 'att-' + sid,
         'output': {'path': str(path.relative_to(p.root)), 'sha256': file_sha256(path)},
         'duration_seconds': 1.0, 'strict_selected': False, 'review': 'unknown', 'findings': []}
        for sid, path in clips.items()])
    calls = []

    def compose(inputs):
        calls.append(inputs)
        return VideoCompose().execute(inputs)

    cut = draft.compose_first_cut(p.root, compose=compose, operation_id='no-audio-gate')
    assert cut['status'] == 'complete'
    # The composer may emit a silent stream; this is factual stream metadata,
    # not proof of source dialogue or an audio review.
    assert isinstance(cut['technical']['has_audio'], bool)
    assert cut['audio'] == 'native_source'
    assert calls[0]['edit_decisions']['metadata']['preserve_source_audio'] is True
    assert 'audio_path' not in calls[0]
    assert _intent(p.root)['audio'] is None
    assert 'audio_path' not in inspect.signature(draft.compose_first_cut).parameters
    assert 'audio_path' not in inspect.signature(draft.deliver_standalone).parameters


def test_factory_composition_preserves_native_source_audio_without_audio_gate(p, monkeypatch):
    monkeypatch.setattr(subprocess, 'run', REAL_RUN)
    contract = execution.load_shot_contract(p.root)
    clips = {shot['id']: with_native_tone(p.root / f"assets/video/{shot['id']}-tone.mp4")
             for shot in contract['shots']}
    assert all(draft._probe(path)['has_audio'] is True for path in clips.values())
    monkeypatch.setattr(draft, 'first_cut_candidates', lambda root: [
        {'shot_id': sid, 'status': 'candidate', 'attempt_id': 'tone-' + sid,
         'output': {'path': str(path.relative_to(p.root)), 'sha256': file_sha256(path)},
         'duration_seconds': 1.0, 'strict_selected': False, 'review': 'unknown', 'findings': []}
        for sid, path in clips.items()])
    calls = []

    def compose(inputs):
        calls.append(inputs)
        return VideoCompose().execute(inputs)

    cut = draft.compose_first_cut(p.root, compose=compose, operation_id='native-audio-preserved')
    assert cut['status'] == 'complete'
    assert cut['technical']['has_audio'] is True
    assert cut['audio'] == 'native_source'
    assert calls[0]['edit_decisions']['metadata']['preserve_source_audio'] is True
    assert 'audio_path' not in calls[0]
    assert _intent(p.root)['audio'] is None

    # Decode the assembled file and check both audible energy and the source
    # tone's frequency. Stream metadata alone would also pass for synthesized
    # silence, so this verifies actual source signal survives composition.
    pcm = REAL_RUN(['ffmpeg', '-v', 'error', '-ss', '0.1', '-t', '0.7', '-i',
                    str(p.root / cut['export']['path']), '-vn', '-ac', '1', '-ar', '48000',
                    '-f', 's16le', '-'], check=True, capture_output=True).stdout
    samples = [int.from_bytes(pcm[index:index + 2], 'little', signed=True)
               for index in range(0, len(pcm) - 1, 2)]
    assert len(samples) > 20_000
    assert max(samples) - min(samples) > 2000
    crossings = sum(left <= 0 < right for left, right in zip(samples, samples[1:]))
    measured_hz = crossings / (len(samples) / 48_000)
    assert measured_hz == pytest.approx(997, abs=20)
    assert 'audio_path' not in inspect.signature(draft.compose_first_cut).parameters
    assert 'audio_path' not in inspect.signature(draft.deliver_standalone).parameters


def _intent(root):
    path = next((root / 'renders/first_cut').glob('*/compose-intent.json'))
    return json.loads(path.read_text())
