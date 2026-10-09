"""Exact media replay for approved canonical cuts and outgoing frame sampling.

Callers retain authority/parent/disk bindings and supply their bound-file and
require callbacks. Every read still replays current cut and outgoing bytes.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from lib.shot_contract import file_sha256


def _canonical_inputs(submitted, canonical_keys, root):
    """Exact canonical keys, plus only Studio's resolved current-project context.

    ``project_dir`` is nonsemantic dispatch context; it is retained, never
    stripped, and must name exactly the project being validated.
    """
    if not isinstance(submitted, dict):
        return False
    keys = set(submitted)
    if keys == canonical_keys:
        return True
    context = submitted.get('project_dir')
    return (keys == canonical_keys | {'project_dir'} and root is not None and isinstance(context, str)
            and bool(context) and Path(context).resolve() == Path(root).resolve())


def trimmed_outgoing_timestamp(path):
    """Actual final decoded video PTS in FrameSampler's input-seek timeline.

    Producers use this for timestamp sampling of CFR and VFR trims. The format
    start time is subtracted because FFmpeg input ``-ss`` is relative to it.
    """
    import math
    import subprocess
    from fractions import Fraction
    try:
        probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_frames',
            '-show_entries', 'stream=time_base:format=start_time:frame=best_effort_timestamp',
            '-of', 'json', str(path)], capture_output=True, text=True, timeout=30, check=True)
        data = json.loads(probe.stdout)
        time_base = Fraction(data['streams'][0]['time_base'])
        origin = Fraction(data.get('format', {}).get('start_time', '0'))
        final = max(int(frame['best_effort_timestamp']) * time_base for frame in data['frames'])
        timestamp = float(final - origin)
        if time_base <= 0 or not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError('invalid final presentation timestamp')
        return timestamp
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, ZeroDivisionError) as exc:
        raise ValueError('cannot probe actual final retained presentation timestamp') from exc


def _validate_trimmed_cut(value, recipe, receipt, native, bound, require, aac_sha256, root=None):
    """Bind and replay the exact canonical local cut on every validation.

    AAC hashes here describe the retained interval, never unchanged whole-clip
    audio. Semantic/audio completeness remains the selection review's job.
    """
    import math
    import subprocess
    import tempfile
    from fractions import Fraction
    from tools.video.video_trimmer import VideoTrimmer

    require(set(recipe) == {'version', 'operation', 'input', 'output_path', 'submitted_inputs', 'reason'}
            and recipe['version'] == '1.0', 'complete typed canonical trim recipe required')
    submitted = recipe['submitted_inputs']
    require(_canonical_inputs(submitted, {
        'operation', 'input_path', 'output_path', 'start_seconds', 'end_seconds', 'codec', 'reason'}, root),
        'exact canonical cut inputs required')
    start, end, codec = submitted['start_seconds'], submitted['end_seconds'], submitted['codec']
    require(type(start) in (int, float) and type(end) in (int, float)
            and math.isfinite(start) and math.isfinite(end)
            and 0 <= start < end <= native['result']['result']['data']['duration_seconds'],
            'bounded retained cut interval required')
    require(submitted['operation'] == 'cut' and submitted['input_path'] == value['parent_output']['path']
            and submitted['output_path'] == value['output']['path']
            and isinstance(codec, str) and bool(codec)
            and isinstance(recipe['reason'], str) and bool(recipe['reason'].strip())
            and submitted['reason'] == recipe['reason'], 'canonical cut input/output/reason differs')
    require(receipt.get('tool') == VideoTrimmer.name and receipt.get('provider') == VideoTrimmer.provider
            and receipt.get('canonical_registry_used') is True and receipt.get('success') is True
            and receipt.get('generation') is False and receipt.get('input') == value['parent_output']
            and receipt.get('recipe') == value['recipe'] and receipt.get('output') == value['output'],
            'successful canonical existing-footage cut receipt required')
    result = receipt.get('tool_result', {})
    data = result.get('data', {})
    actual = data.get('cut_receipt', {})
    require(result.get('success') is True and value['output']['path'] in result.get('artifacts', [])
            and data.get('operation') == 'cut' and data.get('input') == submitted['input_path']
            and data.get('output') == submitted['output_path'] and data.get('start_seconds') == start
            and data.get('end_seconds') == end, 'actual canonical cut return differs')
    command = actual.get('command_argv')
    require(isinstance(command, list) and all(isinstance(arg, str) for arg in command)
            and bool(command) and Path(command[0]).name.lower() in {'ffmpeg', 'ffmpeg.exe'},
            'actual canonical FFmpeg invocation required')
    expected = [command[0], '-y', '-i', submitted['input_path'], '-ss', str(start), '-to', str(end)]
    expected += ['-c', 'copy'] if codec == 'copy' else ['-c:v', codec, '-c:a', 'aac']
    expected.append(submitted['output_path'])
    adapter_version = actual.get('adapter_version')
    require(isinstance(adapter_version, str) and bool(re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][\w.-]+)?', adapter_version)),
            'retained cut adapter version required')
    require(actual == {'version': '1.0', 'tool': VideoTrimmer.name, 'provider': VideoTrimmer.provider,
        'adapter_version': adapter_version, 'operation': 'cut', 'input': value['parent_output'],
        'output': value['output'], 'submitted_inputs': submitted, 'command_argv': expected,
        'command_exit_code': 0}, 'actual canonical command/return/bindings differ')
    parent, output = bound(value['parent_output'], 'native trim input'), bound(value['output'], 'trim output')

    def media(path):
        probe = subprocess.run(['ffprobe', '-v', 'error',
            '-show_entries', 'stream=codec_type,duration,avg_frame_rate', '-of', 'json', str(path)],
            capture_output=True, text=True, timeout=30, check=True)
        streams = json.loads(probe.stdout)['streams']
        stream = next(stream for stream in streams if stream.get('codec_type') == 'video')
        duration, fps = float(stream['duration']), float(Fraction(stream['avg_frame_rate']))
        require(math.isfinite(duration) and math.isfinite(fps) and duration > 0 and fps > 0,
                'actual trim video timing invalid')
        return {'duration_seconds': duration, 'fps': fps}, any(stream.get('codec_type') == 'audio' for stream in streams)

    try:
        (source_timing, source_audio), (output_timing, output_audio) = media(parent), media(output)
        require(end <= source_timing['duration_seconds'] + 0.0001
                and abs(output_timing['duration_seconds'] - (end - start)) <= 1 / output_timing['fps'] + 0.0001,
                'actual retained duration differs from cut interval (copy cuts may need re-encoding)')
        audio = receipt.get('audio', {})
        require(source_audio == output_audio, 'native source audio was lost or added by cut')
        if source_audio:
            mode = 'retained_interval_stream_copy' if codec == 'copy' else 'retained_interval_aac_reencode'
            require(audio == {'mode': mode, 'input_sha256': aac_sha256(parent),
                'output_sha256': aac_sha256(output), 'retained_interval_seconds': [start, end]},
                'actual retained interval AAC evidence differs')
        else:
            require(audio == {'mode': 'no_audio', 'input_has_audio': False, 'output_has_audio': False,
                'retained_interval_seconds': [start, end]}, 'actual no-audio interval evidence differs')
        # On-disk records and in-memory imports have the same caller-writable
        # evidence. Neither proves that registration ran, so both must replay.
        with tempfile.TemporaryDirectory(prefix='openmontage-trim-proof-') as temporary:
            replayed = Path(temporary) / output.name
            # Receipt facts may name a resolved executable; never execute a
            # caller-supplied binary path during verification.
            subprocess.run(['ffmpeg', *expected[1:-1], str(replayed)],
                           capture_output=True, timeout=120, check=True)
            require(file_sha256(replayed) == value['output']['sha256'],
                    'output is not the canonical retained interval cut')
        output_timing['final_frame_timestamp_seconds'] = trimmed_outgoing_timestamp(output)
        return output_timing
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, ZeroDivisionError, StopIteration) as exc:
        require(False, 'cannot verify actual canonical trim media: ' + str(exc))


def _validate_trim_sampling(value, sampled, timing, outgoing, require, root=None):
    """Require the actual final retained frame, not the native endpoint."""
    import subprocess
    import tempfile
    timestamp = timing['final_frame_timestamp_seconds']
    inputs = {'input_path': value['output']['path'], 'strategy': 'timestamps', 'timestamps': [timestamp],
        'format': 'png', 'output_dir': str(outgoing.parent)}
    submitted = sampled.get('submitted_inputs')
    canonical = _canonical_inputs(submitted, set(inputs), root)
    if canonical and 'project_dir' in submitted:
        inputs = {**inputs, 'project_dir': submitted['project_dir']}
    result = sampled.get('tool_result', {})
    frames = result.get('data', {}).get('frames', [])
    require(sampled.get('provider') == 'ffmpeg' and canonical and submitted == inputs
            and result.get('success') is True and result.get('data', {}).get('strategy') == 'timestamps'
            and result.get('data', {}).get('frame_count') == 1 and frames == [{
                'path': str(outgoing), 'timestamp_seconds': timestamp, 'index': 0}],
            'actual final retained-frame sampling differs')
    try:
        with tempfile.TemporaryDirectory(prefix='openmontage-trim-outgoing-') as temporary:
            replayed = Path(temporary) / 'outgoing.png'
            subprocess.run(['ffmpeg', '-y', '-ss', str(timestamp), '-i', value['output']['path'],
                '-frames:v', '1', str(replayed)], capture_output=True, timeout=60, check=True)
            require(file_sha256(replayed) == value['outgoing_frame']['sha256'],
                    'outgoing bytes are not the actual final retained frame')
    except (OSError, subprocess.SubprocessError) as exc:
        require(False, 'cannot verify retained outgoing frame: ' + str(exc))


