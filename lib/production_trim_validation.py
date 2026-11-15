"""Exact media replay for approved canonical cuts and outgoing frame sampling.

Callers retain authority/parent/disk bindings and supply their bound-file and
require callbacks. Each validation checks current bindings and comparisons;
unchanged subprocess facts may be reused within one bounded read scope.
Writers retain fresh replay through the existing no_status_reuse boundary.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from lib.shot_contract import file_sha256



def _record_media_files(value, root):
    files = []
    for binding in [value.get(name) for name in ('parent_output', 'output', 'preserved_output',
            'outgoing_frame', 'recipe', 'execution_receipt', 'outgoing_receipt')] + [
                value.get('approval', {}).get('evidence')]:
        if isinstance(binding, dict) and isinstance(binding.get('path'), str):
            path = Path(binding['path'])
            files.append(path if path.is_absolute() or root is None else Path(root) / path)
    if root is not None:
        files.append(Path(root) / 'production_derived_edits' / value['output']['sha256'] / 'record.json')
    return files


def _status_media_fact(kind, arguments, files, compute):
    """Bounded reuse of subprocess-produced facts, never a validation verdict.

    Record/recipe/receipt comparisons and bound/require callbacks still execute
    freshly in their callers. Every read-scope hit rechecks physical inputs,
    implementation and resolved tools; writers/locks always recompute.
    """
    import subprocess
    from lib.production_autonomy import _active_reuse, status_reused
    # A root-less relative binding may be resolved by the caller's bound helper
    # somewhere other than CWD. Without the actual absolute source, stay fresh.
    if _active_reuse() is None or any(not Path(path).is_absolute() for path in files):
        return compute()
    from lib.production_execution import _digest
    from tools.video import video_trimmer
    from lib import production_provenance
    tool = video_trimmer.VideoTrimmer
    programs = tuple(shutil.which(name) for name in ('ffmpeg', 'ffprobe'))
    if not all(programs):
        return compute()
    dependencies = [*files, Path(__file__), Path(production_provenance.__file__),
                    Path(video_trimmer.__file__), *programs]
    environment = [(name, os.environ.get(name)) for name in
        ('PATH', 'LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH', 'DYLD_FALLBACK_LIBRARY_PATH')]
    helpers = (subprocess.run, file_sha256, production_provenance.file_sha256,
        trimmed_outgoing_timestamp, _trimmed_outgoing_timestamp_fresh, _canonical_inputs,
        _probe_json, _replayed_sha256, production_provenance._aac_sha256,
        production_provenance._aac_sha256_fresh, tool.source_facts, tool.retime_plan,
        getattr(compute, '__code__', compute))
    configuration = [tool.name, tool.provider, video_trimmer.RETIME_DEFAULT_SPEEDS,
                     video_trimmer.RETIME_SPEED_RANGE, environment]
    key = ('local_media_fact', kind, _digest([arguments, configuration]), programs,
           tuple(id(helper) for helper in helpers))
    return status_reused(key, dependencies, compute)


def _probe_json(arguments, files, context):
    import subprocess
    return _status_media_fact('probe_json', [arguments, context], files,
        lambda: json.loads(subprocess.run(arguments, capture_output=True, text=True,
                                          timeout=30, check=True).stdout))


def _replayed_sha256(arguments, filename, files, context, *, prefix, timeout):
    import subprocess
    import tempfile
    def compute():
        with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
            replayed = Path(temporary) / filename
            subprocess.run([*arguments, str(replayed)], capture_output=True, timeout=timeout, check=True)
            return file_sha256(replayed)
    return _status_media_fact('replayed_sha256', [arguments, filename, context], files, compute)


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
    return _status_media_fact('final_pts', [str(path)], [path],
                              lambda: _trimmed_outgoing_timestamp_fresh(path))


def _trimmed_outgoing_timestamp_fresh(path):
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
            and 0 <= start < end, 'bounded retained cut interval required')
    # MCP retains the original bytes, not provider-reported media timing. Its
    # upper bound is the hash-bound source probe below, not requested duration.
    if 'connector_provenance' not in native:
        require(end <= native['result']['result']['data']['duration_seconds'],
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
        streams = _probe_json(['ffprobe', '-v', 'error',
            '-show_entries', 'stream=codec_type,duration,avg_frame_rate', '-of', 'json', str(path)],
            _record_media_files(value, root), [value, recipe, receipt])['streams']
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
        # Receipt facts may name a resolved executable; replay only the
        # registered binary, and compare its observed hash freshly on every call.
        actual_sha256 = _replayed_sha256(['ffmpeg', *expected[1:-1]], output.name,
            _record_media_files(value, root), [value, recipe, receipt],
            prefix='openmontage-trim-proof-', timeout=120)
        require(actual_sha256 == value['output']['sha256'],
                'output is not the canonical retained interval cut')
        output_timing['final_frame_timestamp_seconds'] = trimmed_outgoing_timestamp(output)
        return output_timing
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, ZeroDivisionError, StopIteration) as exc:
        require(False, 'cannot verify actual canonical trim media: ' + str(exc))


def _validate_retimed_cut(value, recipe, receipt, bound, require, aac_sha256, root=None):
    """Bind and replay an exact canonical multi-interval retime on every validation.

    The time map is recomputed from the submitted intervals and the probed
    parent; receipt claims are compared to it, never trusted.
    """
    import math
    import subprocess
    from fractions import Fraction
    from tools.video.video_trimmer import VideoTrimmer

    require(set(recipe) == {'version', 'operation', 'input', 'output_path', 'submitted_inputs', 'reason'}
            and recipe['version'] == '1.0', 'complete typed canonical retime recipe required')
    submitted = recipe['submitted_inputs']
    require(_canonical_inputs(submitted, {
        'operation', 'input_path', 'output_path', 'intervals', 'codec', 'reason'}, root),
        'exact canonical retime inputs required')
    require(submitted['operation'] == 'retime' and submitted['input_path'] == value['parent_output']['path']
            and submitted['output_path'] == value['output']['path']
            and isinstance(recipe['reason'], str) and bool(recipe['reason'].strip())
            and submitted['reason'] == recipe['reason'], 'canonical retime input/output/reason differs')
    require(receipt.get('tool') == VideoTrimmer.name and receipt.get('provider') == VideoTrimmer.provider
            and receipt.get('canonical_registry_used') is True and receipt.get('success') is True
            and receipt.get('generation') is False and receipt.get('input') == value['parent_output']
            and receipt.get('recipe') == value['recipe'] and receipt.get('output') == value['output'],
            'successful canonical existing-footage retime receipt required')
    parent, output = bound(value['parent_output'], 'native retime input'), bound(value['output'], 'retime output')
    try:
        source_duration, source_audio = _status_media_fact('source_facts',
            [str(parent), value, recipe, receipt], _record_media_files(value, root),
            lambda: VideoTrimmer.source_facts(parent))
        try:
            resolved, arguments = VideoTrimmer.retime_plan(
                submitted, has_audio=source_audio, source_duration=source_duration)
        except ValueError as exc:
            require(False, 'invalid retime intervals: ' + str(exc))
        result = receipt.get('tool_result', {})
        data = result.get('data', {})
        actual = data.get('retime_receipt', {})
        require(result.get('success') is True and value['output']['path'] in result.get('artifacts', [])
                and data.get('operation') == 'retime' and data.get('input') == submitted['input_path']
                and data.get('output') == submitted['output_path'] and data.get('intervals') == resolved,
                'actual canonical retime return differs')
        command = actual.get('command_argv')
        require(isinstance(command, list) and all(isinstance(arg, str) for arg in command)
                and bool(command) and Path(command[0]).name.lower() in {'ffmpeg', 'ffmpeg.exe'},
                'actual canonical FFmpeg invocation required')
        expected = [command[0], *arguments, submitted['output_path']]
        adapter_version = actual.get('adapter_version')
        require(isinstance(adapter_version, str)
                and bool(re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][\w.-]+)?', adapter_version)),
                'retained retime adapter version required')
        require(actual == {'version': '1.0', 'tool': VideoTrimmer.name, 'provider': VideoTrimmer.provider,
            'adapter_version': adapter_version, 'operation': 'retime', 'input': value['parent_output'],
            'output': value['output'], 'submitted_inputs': submitted, 'intervals': resolved,
            'source_has_audio': source_audio, 'command_argv': expected, 'command_exit_code': 0},
            'actual canonical retime command/return/bindings differ')
        streams = _probe_json(['ffprobe', '-v', 'error', '-show_entries',
            'stream=codec_type,duration,avg_frame_rate', '-of', 'json', str(output)],
            _record_media_files(value, root), [value, recipe, receipt])['streams']
        video = next(stream for stream in streams if stream.get('codec_type') == 'video')
        duration, fps = float(video['duration']), float(Fraction(video['avg_frame_rate']))
        require(math.isfinite(duration) and math.isfinite(fps) and duration > 0 and fps > 0,
                'actual retime video timing invalid')
        # Each interval may round to its own frame boundary.
        require(abs(duration - resolved[-1]['output_end_seconds']) <= (len(resolved) + 1) / fps + 0.0001,
                'actual retimed duration differs from the interval time map')
        output_audio = any(stream.get('codec_type') == 'audio' for stream in streams)
        require(source_audio == output_audio, 'native source audio was lost or added by retime')
        audio = receipt.get('audio', {})
        if source_audio:
            require(audio == {'mode': 'retimed_intervals_pitch_preserved_aac',
                'input_sha256': aac_sha256(parent), 'output_sha256': aac_sha256(output),
                'intervals': resolved}, 'actual retimed interval AAC evidence differs')
        else:
            require(audio == {'mode': 'no_audio', 'input_has_audio': False, 'output_has_audio': False,
                'intervals': resolved}, 'actual no-audio retime evidence differs')
        # Never execute a caller-supplied binary path during verification.
        actual_sha256 = _replayed_sha256(['ffmpeg', *arguments], output.name,
            _record_media_files(value, root), [value, recipe, receipt],
            prefix='openmontage-retime-proof-', timeout=300)
        require(actual_sha256 == value['output']['sha256'],
                'output is not the canonical retimed interval edit')
        return {'duration_seconds': duration, 'fps': fps,
                'final_frame_timestamp_seconds': trimmed_outgoing_timestamp(output), 'time_map': resolved}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, ZeroDivisionError,
            StopIteration) as exc:
        if type(exc).__name__ == 'ProductionGovernanceError':
            raise
        require(False, 'cannot verify actual canonical retime media: ' + str(exc))


def _validate_trim_sampling(value, sampled, timing, outgoing, require, root=None):
    """Require the actual final retained frame, not the native endpoint."""
    import subprocess
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
        actual_sha256 = _replayed_sha256(['ffmpeg', '-y', '-ss', str(timestamp), '-i',
            value['output']['path'], '-frames:v', '1'], 'outgoing.png',
            _record_media_files(value, root), [value, sampled, timing],
            prefix='openmontage-trim-outgoing-', timeout=60)
        require(actual_sha256 == value['outgoing_frame']['sha256'],
                'outgoing bytes are not the actual final retained frame')
    except (OSError, subprocess.SubprocessError) as exc:
        require(False, 'cannot verify retained outgoing frame: ' + str(exc))


