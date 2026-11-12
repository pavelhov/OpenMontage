"""Real local retiming, pitch preservation and independent provenance replay."""
import copy
import json
import shutil
import subprocess
from array import array
from dataclasses import asdict
from pathlib import Path

import pytest

from lib.production_provenance import _aac_sha256
from lib.production_trim_validation import _validate_retimed_cut
from lib.shot_contract import file_sha256
from tools.video.video_trimmer import VideoTrimmer
from tests.lib.test_production_provenance import production  # noqa: F401
from tests.lib.test_trimmed_derivative import trimmed, reseal, sample_record, save  # noqa: F401

pytestmark = pytest.mark.skipif(not (shutil.which('ffmpeg') and shutil.which('ffprobe')),
                                reason='ffmpeg/ffprobe unavailable')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def binding(path):
    return {'path': str(path), 'sha256': file_sha256(path)}


@pytest.fixture(params=[True, False], ids=['native-audio', 'silent-source'])
def retimed(tmp_path, request):
    source, output = tmp_path / 'source.mp4', tmp_path / 'retimed.mp4'
    command = ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=s=160x90:r=30:d=3']
    if request.param:
        command += ['-f', 'lavfi', '-i', 'aevalsrc=sin(2*PI*if(lt(t\\,2)\\,440\\,880)*t):s=48000:d=3']
    subprocess.run([*command, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source)],
                   capture_output=True, check=True)
    inputs = {'operation': 'retime', 'input_path': str(source), 'output_path': str(output),
              'codec': 'libx264', 'reason': 'Synthetic editor-selected retained intervals',
              'intervals': [{'start_seconds': 0, 'end_seconds': 1, 'content': 'dialogue'},
                            {'start_seconds': 2, 'end_seconds': 3, 'content': 'nonspeaking'}]}
    result = VideoTrimmer().execute(inputs)
    assert result.success, result.error
    recipe = {'version': '1.0', 'operation': 'canonical_video_trimmer_retime', 'input': binding(source),
              'output_path': str(output), 'submitted_inputs': inputs, 'reason': inputs['reason']}
    recipe_path = tmp_path / 'recipe.json'
    recipe_path.write_text(json.dumps(recipe))
    value = {'parent_output': binding(source), 'output': binding(output), 'recipe': binding(recipe_path)}
    intervals = result.data['intervals']
    audio = {'mode': 'retimed_intervals_pitch_preserved_aac', 'input_sha256': _aac_sha256(source),
             'output_sha256': _aac_sha256(output), 'intervals': intervals} if request.param else {
                 'mode': 'no_audio', 'input_has_audio': False, 'output_has_audio': False, 'intervals': intervals}
    receipt = {'tool': 'video_trimmer', 'provider': 'ffmpeg', 'canonical_registry_used': True,
               'success': True, 'generation': False, 'input': value['parent_output'], 'recipe': value['recipe'],
               'output': value['output'], 'audio': audio, 'tool_result': asdict(result)}
    return value, recipe, receipt, request.param


def validate(parts):
    value, recipe, receipt, _ = parts

    def bound(item, label):
        path = Path(item['path'])
        require(file_sha256(path) == item['sha256'], label + ' changed')
        return path

    return _validate_retimed_cut(value, recipe, receipt, bound, require, _aac_sha256)


def test_multi_interval_exact_replay_time_map_and_pitch(retimed):
    value, recipe, receipt, has_audio = retimed
    timing = validate(retimed)
    expected = [
        {'source_start_seconds': 0, 'source_end_seconds': 1, 'content': 'dialogue', 'speed': 1.25,
         'output_start_seconds': 0.0, 'output_end_seconds': 0.8},
        {'source_start_seconds': 2, 'source_end_seconds': 3, 'content': 'nonspeaking', 'speed': 1.5,
         'output_start_seconds': 0.8, 'output_end_seconds': 1.466667}]
    assert timing['time_map'] == expected
    assert timing['duration_seconds'] == pytest.approx(1.466667, abs=0.08)
    assert 0 < timing['final_frame_timestamp_seconds'] < timing['duration_seconds']
    if has_audio:
        for start, hz in [(0.15, 440), (1.0, 880)]:
            raw = subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(start), '-i', value['output']['path'],
                '-t', '0.25', '-f', 's16le', '-ac', '1', '-ar', '48000', '-'], capture_output=True, check=True).stdout
            samples = array('h', raw)
            crossings = sum(a <= 0 < b for a, b in zip(samples, samples[1:]))
            assert crossings / (len(samples) / 48000) == pytest.approx(hz, abs=15)


@pytest.mark.parametrize('target', ['interval', 'command', 'audio', 'submitted'])
def test_retime_rejects_resealed_receipt_claims(retimed, target):
    value, recipe, receipt, has_audio = copy.deepcopy(retimed)
    if target == 'interval':
        receipt['tool_result']['data']['retime_receipt']['intervals'][0]['speed'] = 2
    elif target == 'command':
        receipt['tool_result']['data']['retime_receipt']['command_argv'][2] = 'another.mp4'
    elif target == 'audio':
        receipt['audio']['mode'] = 'whole_clip_unchanged'
    else:
        recipe['submitted_inputs']['intervals'][0]['speed'] = 2
    with pytest.raises(ValueError):
        validate((value, recipe, receipt, has_audio))


@pytest.mark.parametrize('intervals', [[],
    [{'start_seconds': -1, 'end_seconds': 1, 'content': 'dialogue'}],
    [{'start_seconds': 0, 'end_seconds': 4, 'content': 'dialogue'}],
    [{'start_seconds': 1, 'end_seconds': 2, 'content': 'dialogue'},
     {'start_seconds': 0, 'end_seconds': 1, 'content': 'nonspeaking'}],
    [{'start_seconds': 0, 'end_seconds': 1, 'content': 'dialogue', 'speed': True}],
    [{'start_seconds': 0, 'end_seconds': 1, 'content': 'dialogue', 'speed': 4.1}],
    [{'start_seconds': 0, 'end_seconds': 1, 'content': 'unknown'}],
    [{'start_seconds': 0, 'end_seconds': float('nan'), 'content': 'dialogue'}]])
def test_retime_rejects_invalid_intervals(intervals):
    with pytest.raises(ValueError):
        VideoTrimmer.retime_plan({'input_path': 'input.mp4', 'intervals': intervals},
                                 has_audio=True, source_duration=3)


def test_explicit_speed_overrides_content_default_and_copy_is_refused():
    inputs = {'input_path': 'input.mp4', 'intervals': [
        {'start_seconds': 0, 'end_seconds': 1, 'content': 'dialogue', 'speed': 2}]}
    resolved, command = VideoTrimmer.retime_plan(inputs, has_audio=True, source_duration=3)
    assert resolved[0]['speed'] == 2 and resolved[0]['output_end_seconds'] == 0.5
    assert 'atempo=2' in command[command.index('-filter_complex') + 1]
    with pytest.raises(ValueError, match='re-encoding'):
        VideoTrimmer.retime_plan(dict(inputs, codec='copy'), has_audio=False, source_duration=3)


def test_retime_registers_through_existing_derived_edit_boundary(trimmed):
    from lib.production_execution import record_derived_edit
    from lib.production_trim_validation import trimmed_outgoing_timestamp
    value, _, _, record, _ = trimmed
    output = Path(record['output']['path'])
    inputs = {'operation': 'retime', 'input_path': record['parent_output']['path'],
              'output_path': str(output), 'codec': 'libx264', 'reason': 'Synthetic retained dialogue and reaction',
              'project_dir': str(value.root), 'intervals': [
                  {'start_seconds': 0, 'end_seconds': 2.5, 'content': 'dialogue'},
                  {'start_seconds': 5, 'end_seconds': 6.5, 'content': 'nonspeaking'}]}
    result = VideoTrimmer().execute(inputs)
    assert result.success, result.error
    recipe_path = Path(record['recipe']['path'])
    save(recipe_path, {'version': '1.0', 'operation': 'canonical_video_trimmer_retime',
                      'input': record['parent_output'], 'output_path': str(output),
                      'submitted_inputs': inputs, 'reason': inputs['reason']})
    record['recipe'], record['output'] = binding(recipe_path), binding(output)
    record['preserved_output'] = {'path': str(value.root / 'production_derived_edits' / file_sha256(output) / 'output.mp4'),
                                  'sha256': file_sha256(output)}
    receipt_path = Path(record['execution_receipt']['path'])
    save(receipt_path, {'tool': 'video_trimmer', 'provider': 'ffmpeg', 'canonical_registry_used': True,
         'success': True, 'generation': False, 'input': record['parent_output'], 'recipe': record['recipe'],
         'output': record['output'], 'tool_result': asdict(result), 'audio': {
             'mode': 'retimed_intervals_pitch_preserved_aac',
             'input_sha256': _aac_sha256(record['parent_output']['path']), 'output_sha256': _aac_sha256(output),
             'intervals': result.data['intervals']}})
    record['execution_receipt'] = binding(receipt_path)
    sample_record(record, trimmed_outgoing_timestamp(output))
    reseal(record)
    checked = record_derived_edit(value.root, record)
    assert checked['derived_timing']['duration_seconds'] == pytest.approx(3, abs=0.1)
    assert checked['derived_timing']['time_map'] == result.data['intervals']
    assert checked['selected_output'] == record['output']
    assert checked['selected_outgoing_frame'] == record['outgoing_frame']


def test_retime_self_consistent_output_hash_cannot_bypass_replay(retimed):
    value, recipe, receipt, has_audio = copy.deepcopy(retimed)
    output = Path(value['output']['path'])
    output.write_bytes(output.read_bytes() + b'self-consistent altered container')
    changed = binding(output)
    value['output'] = changed
    receipt['output'] = changed
    receipt['tool_result']['data']['retime_receipt']['output'] = changed
    with pytest.raises(ValueError, match='canonical retimed interval edit'):
        validate((value, recipe, receipt, has_audio))
