"""Real offline trim/sampling bytes with synthetic canonical parent/review evidence."""
import shutil
import subprocess
import json
from array import array
from dataclasses import asdict
from fractions import Fraction
from pathlib import Path

import pytest

from lib.production_execution import (
    ProductionGovernanceError, _digest, record_derived_edit, record_selection,
)
from lib.production_provenance import _aac_sha256, trimmed_outgoing_timestamp, validate_attempt_provenance
from lib.shot_contract import UPSTREAM_PREDICATES, file_sha256, selection_digest
from tests.integration.test_first_pass_workflow import attestation
from tests.lib.test_production_provenance import production, attempt, read, save
from tools.tool_registry import registry


REAL_RUN = subprocess.run
REAL_WHICH = shutil.which


def binding(path):
    return {'path': str(path), 'sha256': file_sha256(path)}


def reseal(record):
    path = Path(record['approval']['evidence']['path'])
    accepted = read(path)
    accepted['derived_edit_sha256'] = _digest({k: v for k, v in record.items() if k != 'approval'})
    save(path, accepted)
    record['approval']['evidence'] = binding(path)


def final_timestamp(path):
    """Independent fixture probe; final-image assertions decode in reverse below."""
    result = REAL_RUN(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_frames',
        '-show_entries', 'stream=time_base:format=start_time:frame=best_effort_timestamp',
        '-of', 'json', str(path)], capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    time_base = Fraction(data['streams'][0]['time_base'])
    origin = Fraction(data['format'].get('start_time', '0'))
    return float(max(int(frame['best_effort_timestamp']) * time_base for frame in data['frames']) - origin)


def sample_record(record, timestamp):
    frames = Path(record['outgoing_frame']['path']).parent
    inputs = {'input_path': record['output']['path'], 'strategy': 'timestamps',
        'timestamps': [timestamp], 'format': 'png', 'output_dir': str(frames)}
    sampler = registry.get('frame_sampler')
    result = sampler.execute(inputs)
    assert result.success, result.error
    outgoing = binding(Path(result.data['frames'][0]['path']))
    receipt = Path(record['outgoing_receipt']['path'])
    save(receipt, {'tool': sampler.name, 'provider': sampler.provider, 'input': record['output'],
        'submitted_inputs': inputs, 'outgoing_frame': outgoing, 'tool_result': asdict(result)})
    record['outgoing_frame'], record['outgoing_receipt'] = outgoing, binding(receipt)


@pytest.fixture
def trimmed(production, monkeypatch, request):
    if not REAL_WHICH('ffmpeg') or not REAL_WHICH('ffprobe'):
        pytest.skip('offline real media fixture requires ffmpeg/ffprobe')
    root = production.root
    source = root / 'artifacts/offline-source.mp4'
    options = getattr(request, 'param', 'libx264')
    options = {'codec': options, 'audio': True} if isinstance(options, str) else options
    vfr = options.get('vfr', False)
    source_command = ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
        'testsrc2=s=1280x720:r=30:d=3.333334' if vfr else 'color=c=blue:s=1280x720:r=12:d=8']
    if options['audio']:
        source_command += ['-f', 'lavfi', '-i', 'aevalsrc=sin(2*PI*if(lt(t\\,4.5)\\,440\\,880)*t):s=48000:d=8']
    REAL_RUN([*source_command, '-vf',
        'setpts=if(lt(N\\,29)\\,N*6\\,N+140)' if vfr else
        "drawbox=x=0:y=0:w=iw:h=ih:color=red:t=fill:enable='gte(t,4.5)'",
        '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
        '-g', '12', '-bf', '0', *(['-fps_mode', 'vfr', '-frames:v', '100'] if vfr else []),
        '-c:a', 'aac', str(source)], capture_output=True, check=True)

    transport = production.transport

    def offline_run(argv, **kwargs):
        if Path(str(argv[0])).name in {'ffmpeg', 'ffprobe'}:
            return REAL_RUN(argv, **kwargs)
        result = transport(argv, **kwargs)
        if '--prompt-file' in argv:
            sid = argv[argv.index('--session-id') + 1]
            transport.original_sessions[sid]['artifact'].write_bytes(source.read_bytes())
        return result

    monkeypatch.setattr('subprocess.run', offline_run)
    monkeypatch.setattr('shutil.which', lambda name: '/offline/grok' if name == 'grok' else REAL_WHICH(name))
    aid, directory, parent = attempt(production)
    registry.discover()
    tool = registry.get('video_trimmer')
    output = root / 'assets/video/retained-cut.mp4'
    # Studio production_entry.dispatch submits its resolved project as context.
    contexts = {None: {}, 'correct': {'project_dir': str(root.resolve())},
        'wrong': {'project_dir': str(root.resolve().parent)},
        'extra': {'project_dir': str(root.resolve()), 'caller_note': 'unexpected'}}
    inputs = {'operation': 'cut', 'input_path': parent['path'], 'output_path': str(output),
        'start_seconds': options.get('start', 0), 'end_seconds': options.get('end', 3.5), 'codec': options['codec'],
        'reason': 'Retain completed action; exclude synthetic late red defect.',
        **contexts[options.get('cut_context')]}
    result = tool.execute(inputs)
    assert result.success, result.error
    recipe = root / 'artifacts/trim-recipe.json'
    save(recipe, {'version': '1.0', 'operation': 'canonical_video_trimmer_cut',
        'input': parent, 'output_path': str(output), 'submitted_inputs': inputs,
        'reason': inputs['reason']})
    receipt = root / 'artifacts/trim-receipt.json'
    interval = [inputs['start_seconds'], inputs['end_seconds']]
    audio = {'mode': 'retained_interval_stream_copy' if inputs['codec'] == 'copy' else 'retained_interval_aac_reencode',
        'input_sha256': _aac_sha256(parent['path']), 'output_sha256': _aac_sha256(output),
        'retained_interval_seconds': interval} if options['audio'] else {
            'mode': 'no_audio', 'input_has_audio': False, 'output_has_audio': False,
            'retained_interval_seconds': interval}
    save(receipt, {'tool': tool.name, 'provider': tool.provider,
        'canonical_registry_used': True, 'success': True, 'generation': False,
        'input': parent, 'recipe': binding(recipe), 'output': binding(output),
        'audio': audio,
        'tool_result': asdict(result)})
    frames = root / 'artifacts/trim-outgoing'
    sample_inputs = {'input_path': str(output), 'strategy': 'timestamps',
        'timestamps': [final_timestamp(output)], 'format': 'png', 'output_dir': str(frames),
        **contexts[options.get('sample_context')]}
    sampler = registry.get('frame_sampler')
    sampled_result = sampler.execute(sample_inputs)
    assert sampled_result.success, sampled_result.error
    outgoing = binding(Path(sampled_result.data['frames'][0]['path']))
    sampled = root / 'artifacts/trim-sampling.json'
    save(sampled, {'tool': sampler.name, 'provider': sampler.provider, 'input': binding(output),
        'submitted_inputs': sample_inputs, 'outgoing_frame': outgoing, 'tool_result': asdict(sampled_result)})
    record = {'version': '1.0', 'project_id': root.name,
        'story_revision': production.story['story_revision'], 'shot_id': 'entry',
        'parent_attempt_id': aid, 'parent_output': parent, 'recipe': binding(recipe),
        'execution_receipt': binding(receipt), 'output': binding(output),
        'preserved_output': {'path': str(root / 'production_derived_edits' / file_sha256(output) / 'output.mp4'),
            'sha256': file_sha256(output)}, 'outgoing_frame': outgoing, 'outgoing_receipt': binding(sampled)}
    authority = root / 'artifacts/local-edit-authorization.txt'
    authority.write_text(f'SYNTHETIC retained local-edit approval: trim this exact entry attempt from {interval[0]} to {interval[1]} seconds.')
    approval = root / 'artifacts/trim-approval.json'
    save(approval, {'status': 'approved', 'approved_by': 'synthetic exact trim reviewer',
        'derived_edit_sha256': _digest(record), 'authorization': binding(authority)})
    record['approval'] = {'approved_by': 'synthetic exact trim reviewer', 'evidence': binding(approval)}
    return production, aid, directory, record, result


def check(value, record):
    return validate_attempt_provenance(value.root, record['parent_attempt_id'], shot_id='entry',
        story_revision=value.story['story_revision'], expected_output=record['output'])


def test_actual_canonical_cut_returns_command_and_exact_bytes(trimmed):
    _, _, _, record, result = trimmed
    receipt = result.data['cut_receipt']
    assert receipt['tool'] == 'video_trimmer'
    assert receipt['command_exit_code'] == 0
    assert receipt['input'] == record['parent_output']
    assert receipt['output'] == record['output']
    assert receipt['submitted_inputs'] == read(Path(record['recipe']['path']))['submitted_inputs']


def test_trim_registration_and_fresh_selection_keep_original_attempt(trimmed):
    value, aid, directory, record, _ = trimmed
    before = {name: (directory / name).read_bytes() for name in ('request.json', 'result.json', 'raw_result.json')}
    checked = record_derived_edit(value.root, record)
    assert checked['derived_timing']['duration_seconds'] == 3.5
    assert check(value, record)['selected_output'] == record['output']
    selection = {'attempt_id': aid, 'output': record['output'], 'outgoing_frame': record['outgoing_frame']}
    selection['review'] = attestation(selection_digest(selection), UPSTREAM_PREDICATES)
    record_selection(value.root, 'entry', selection)
    assert all((directory / name).read_bytes() == data for name, data in before.items())
    assert check(value, {**record, 'output': record['parent_output']})['result']['output'] == record['parent_output']
    # Cutting preserves retained interval audio; its AAC bytes differ from the whole parent.
    audio = read(Path(record['execution_receipt']['path']))['audio']
    assert audio['input_sha256'] != audio['output_sha256']


@pytest.mark.parametrize('trimmed', ['copy'], indirect=True)
def test_copy_tail_cut_binds_retained_audio_and_actual_duration(trimmed):
    value, _, _, record, _ = trimmed
    checked = record_derived_edit(value.root, record)
    assert checked['derived_timing']['duration_seconds'] == 3.5
    audio = read(Path(record['execution_receipt']['path']))['audio']
    assert audio['mode'] == 'retained_interval_stream_copy'
    assert audio['input_sha256'] != audio['output_sha256']


def test_retained_trim_reads_verify_full_cut_and_outgoing(trimmed, monkeypatch):
    value, _, _, record, _ = trimmed
    record_derived_edit(value.root, record)
    prior = subprocess.run

    calls = []

    def observed_run(argv, **kwargs):
        calls.append(argv)
        return prior(argv, **kwargs)

    monkeypatch.setattr('subprocess.run', observed_run)
    assert check(value, record)['selected_output'] == record['output']
    assert any('-c:v' in command for command in calls)
    assert any('-frames:v' in command for command in calls)


def test_disk_planted_resealed_non_parent_output_cannot_select(trimmed):
    value, aid, _, record, _ = trimmed
    output = Path(record['output']['path'])
    REAL_RUN(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
        'color=c=green:s=1280x720:r=12:d=3.5', '-f', 'lavfi', '-i',
        'sine=frequency=440:duration=3.5', '-c:v', 'libx264', '-c:a', 'aac', str(output)],
        capture_output=True, check=True)
    record['output'] = binding(output)
    receipt = Path(record['execution_receipt']['path'])
    data = read(receipt)
    data['output'] = record['output']
    data['audio']['output_sha256'] = _aac_sha256(output)
    data['tool_result']['data']['cut_receipt']['output'] = record['output']
    save(receipt, data)
    record['execution_receipt'] = binding(receipt)
    sample_record(record, final_timestamp(output))
    directory = value.root / 'production_derived_edits' / record['output']['sha256']
    directory.mkdir(parents=True)
    preserved = directory / 'output.mp4'
    shutil.copyfile(output, preserved)
    record['preserved_output'] = binding(preserved)
    reseal(record)
    save(directory / 'record.json', record)  # Deliberately bypass record_derived_edit.
    selection = {'attempt_id': aid, 'output': record['output'], 'outgoing_frame': record['outgoing_frame']}
    selection['review'] = attestation(selection_digest(selection), UPSTREAM_PREDICATES)
    with pytest.raises(ProductionGovernanceError, match='not the canonical retained interval cut'):
        record_selection(value.root, 'entry', selection)


def test_disk_planted_resealed_wrong_outgoing_cannot_select(trimmed):
    value, aid, _, record, _ = trimmed
    outgoing = Path(record['outgoing_frame']['path'])
    REAL_RUN(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
        'color=c=green:s=1280x720', '-frames:v', '1', str(outgoing)], capture_output=True, check=True)
    record['outgoing_frame'] = binding(outgoing)
    receipt = Path(record['outgoing_receipt']['path'])
    sampled = read(receipt)
    sampled['outgoing_frame'] = record['outgoing_frame']
    save(receipt, sampled)
    record['outgoing_receipt'] = binding(receipt)
    directory = value.root / 'production_derived_edits' / record['output']['sha256']
    directory.mkdir(parents=True)
    preserved = directory / 'output.mp4'
    shutil.copyfile(record['output']['path'], preserved)
    record['preserved_output'] = binding(preserved)
    reseal(record)
    save(directory / 'record.json', record)  # No registration on this disk path.
    selection = {'attempt_id': aid, 'output': record['output'], 'outgoing_frame': record['outgoing_frame']}
    selection['review'] = attestation(selection_digest(selection), UPSTREAM_PREDICATES)
    with pytest.raises(ProductionGovernanceError, match='not the actual final retained frame'):
        record_selection(value.root, 'entry', selection)


@pytest.mark.parametrize('trimmed', [{'codec': 'copy', 'audio': True, 'vfr': True, 'end': 5.666667}], indirect=True)
def test_vfr_copy_selects_actual_last_decoded_frame(trimmed):
    value, aid, _, record, _ = trimmed
    output = Path(record['output']['path'])
    probe = REAL_RUN(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
        'stream=duration,avg_frame_rate', '-of', 'json', str(output)], capture_output=True, text=True, check=True)
    stream = json.loads(probe.stdout)['streams'][0]
    average_guess = float(stream['duration']) - 1 / float(Fraction(stream['avg_frame_rate']))
    correct = final_timestamp(output)
    assert abs(correct - average_guess) > 0.05
    last = value.root / 'artifacts/decoded-last.png'
    REAL_RUN(['ffmpeg', '-v', 'error', '-y', '-i', str(output), '-vf', 'reverse',
        '-frames:v', '1', str(last)], capture_output=True, check=True)
    assert file_sha256(last) == record['outgoing_frame']['sha256']
    wrong = registry.get('frame_sampler').execute({'input_path': str(output), 'strategy': 'timestamps',
        'timestamps': [average_guess], 'format': 'png', 'output_dir': str(value.root / 'artifacts/wrong-average-frame')})
    assert wrong.success, wrong.error
    assert file_sha256(wrong.data['frames'][0]['path']) != file_sha256(last)
    checked = record_derived_edit(value.root, record)
    assert checked['derived_timing']['final_frame_timestamp_seconds'] == correct
    selection = {'attempt_id': aid, 'output': record['output'], 'outgoing_frame': record['outgoing_frame']}
    selection['review'] = attestation(selection_digest(selection), UPSTREAM_PREDICATES)
    record_selection(value.root, 'entry', selection)


@pytest.mark.parametrize('trimmed', [
    {'codec': 'libx264', 'audio': True, 'start': 2.125, 'end': 5.625},
    {'codec': 'copy', 'audio': True, 'start': 2, 'end': 5.5},
], indirect=True)
def test_nonzero_cut_retains_visual_and_audio_endpoints(trimmed):
    value, aid, _, record, _ = trimmed
    checked = record_derived_edit(value.root, record)
    duration = checked['derived_timing']['duration_seconds']
    assert abs(duration - 3.5) <= 1 / 12
    assert checked['derived_timing']['final_frame_timestamp_seconds'] == final_timestamp(record['output']['path'])
    first = registry.get('frame_sampler').execute({'input_path': record['output']['path'], 'strategy': 'timestamps',
        'timestamps': [0.1], 'format': 'png', 'output_dir': str(value.root / 'artifacts/nonzero-first')})
    assert first.success, first.error

    def rgb(path):
        raw = REAL_RUN(['ffmpeg', '-v', 'error', '-i', str(path), '-frames:v', '1', '-pix_fmt', 'rgb24',
            '-f', 'rawvideo', 'pipe:1'], capture_output=True, check=True).stdout
        return raw[:3]

    beginning, ending = rgb(first.data['frames'][0]['path']), rgb(record['outgoing_frame']['path'])
    assert beginning[2] > 200 and beginning[0] < 20  # Blue source at retained start.
    assert ending[0] > 200 and ending[2] < 20  # Red source at retained endpoint.
    decoded_last = value.root / 'artifacts/nonzero-decoded-last.png'
    REAL_RUN(['ffmpeg', '-v', 'error', '-y', '-i', record['output']['path'], '-vf', 'reverse',
        '-frames:v', '1', str(decoded_last)], capture_output=True, check=True)
    assert file_sha256(decoded_last) == record['outgoing_frame']['sha256']

    def tone(start):
        pcm = REAL_RUN(['ffmpeg', '-v', 'error', '-ss', str(start), '-i', record['output']['path'],
            '-t', '0.5', '-ac', '1', '-ar', '48000', '-f', 's16le', 'pipe:1'], capture_output=True, check=True).stdout
        samples = array('h', pcm)
        return sum(left < 0 <= right for left, right in zip(samples, samples[1:])) / (len(samples) / 48000)

    assert 430 < tone(0.1) < 450
    assert 870 < tone(duration - 0.6) < 890
    selection = {'attempt_id': aid, 'output': record['output'], 'outgoing_frame': record['outgoing_frame']}
    selection['review'] = attestation(selection_digest(selection), UPSTREAM_PREDICATES)
    record_selection(value.root, 'entry', selection)


def test_final_pts_is_normalized_to_nonzero_container_seek_origin(trimmed):
    value, _, _, record, _ = trimmed
    shifted = value.root / 'artifacts/shifted-timeline.mp4'
    REAL_RUN(['ffmpeg', '-v', 'error', '-y', '-i', record['output']['path'], '-c', 'copy',
        '-output_ts_offset', '2.5', str(shifted)], capture_output=True, check=True)
    probe = REAL_RUN(['ffprobe', '-v', 'error', '-show_entries', 'format=start_time', '-of', 'json', str(shifted)],
        capture_output=True, text=True, check=True)
    assert float(json.loads(probe.stdout)['format']['start_time']) > 2
    timestamp = trimmed_outgoing_timestamp(shifted)
    assert timestamp == final_timestamp(shifted)
    sampled = registry.get('frame_sampler').execute({'input_path': str(shifted), 'strategy': 'timestamps',
        'timestamps': [timestamp], 'format': 'png', 'output_dir': str(value.root / 'artifacts/shifted-final')})
    assert sampled.success, sampled.error
    assert file_sha256(sampled.data['frames'][0]['path']) == record['outgoing_frame']['sha256']


@pytest.mark.parametrize('trimmed', [{'codec': 'libx264', 'audio': False}], indirect=True)
def test_video_only_parent_and_cut_have_explicit_no_audio_evidence(trimmed):
    value, _, _, record, _ = trimmed
    checked = record_derived_edit(value.root, record)
    assert checked['selected_output'] == record['output']
    assert read(Path(record['execution_receipt']['path']))['audio'] == {
        'mode': 'no_audio', 'input_has_audio': False, 'output_has_audio': False,
        'retained_interval_seconds': [0, 3.5]}


def test_retained_adapter_version_is_historical_fact(trimmed, monkeypatch):
    from tools.video.video_trimmer import VideoTrimmer
    value, _, _, record, _ = trimmed
    record_derived_edit(value.root, record)
    monkeypatch.setattr(VideoTrimmer, 'version', '0.2.0')
    assert check(value, record)['selected_output'] == record['output']


def test_cut_cannot_drop_existing_source_audio(trimmed):
    value, _, _, record, _ = trimmed
    output = Path(record['output']['path'])
    REAL_RUN(['ffmpeg', '-v', 'error', '-y', '-i', record['parent_output']['path'],
        '-ss', '0', '-to', '3.5', '-c:v', 'libx264', '-an', str(output)], capture_output=True, check=True)
    record['output'] = binding(output)
    record['preserved_output'] = {'path': str(value.root / 'production_derived_edits' / file_sha256(output) / 'output.mp4'),
        'sha256': file_sha256(output)}
    receipt = Path(record['execution_receipt']['path'])
    data = read(receipt)
    data['output'] = record['output']
    data['tool_result']['data']['cut_receipt']['output'] = record['output']
    data['audio'] = {'mode': 'no_audio', 'input_has_audio': False, 'output_has_audio': False,
        'retained_interval_seconds': [0, 3.5]}
    save(receipt, data)
    record['execution_receipt'] = binding(receipt)
    reseal(record)
    with pytest.raises(ProductionGovernanceError, match='source audio was lost'):
        record_derived_edit(value.root, record)


@pytest.mark.parametrize('target', ['parent_output', 'output', 'execution_receipt', 'outgoing_frame', 'outgoing_receipt', 'approval'])
def test_trim_current_bound_bytes_cannot_change(trimmed, target):
    value, _, _, record, _ = trimmed
    record_derived_edit(value.root, record)
    bound = record[target]['evidence'] if target == 'approval' else record[target]
    path = Path(bound['path'])
    path.chmod(0o644)
    path.write_bytes(b'changed offline evidence')
    with pytest.raises(ProductionGovernanceError):
        check(value, record)


@pytest.mark.parametrize('change', ['forged_output', 'cut_end', 'command', 'returncode', 'audio', 'sampler', 'authority'])
def test_resealed_trim_receipt_cannot_bless_untrue_derivation(trimmed, change):
    value, _, _, record, _ = trimmed
    if change == 'forged_output':
        # A real but unrelated 3.5-second green video must not become a canonical trim.
        path = Path(record['output']['path'])
        REAL_RUN(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
            'color=c=green:s=1280x720:r=12:d=3.5', '-f', 'lavfi', '-i',
            'sine=frequency=440:duration=3.5', '-c:v', 'libx264', '-c:a', 'aac', str(path)],
            capture_output=True, check=True)
        record['output'] = binding(path)
        record['preserved_output'] = {'path': str(value.root / 'production_derived_edits' / file_sha256(path) / 'output.mp4'),
            'sha256': file_sha256(path)}
    key = 'recipe' if change == 'cut_end' else 'outgoing_receipt' if change == 'sampler' else 'execution_receipt'
    path = Path(record[key]['path'])
    data = read(path)
    if change == 'cut_end':
        data['submitted_inputs']['end_seconds'] = 7
    elif change == 'command':
        data['tool_result']['data']['cut_receipt']['command_argv'][-1] = 'different.mp4'
    elif change == 'returncode':
        data['tool_result']['data']['cut_receipt']['command_exit_code'] = 1
    elif change == 'audio':
        data['audio']['output_sha256'] = '0' * 64
    elif change == 'sampler':
        data['submitted_inputs']['timestamps'] = [0]
    elif change == 'authority':
        Path(read(Path(record['approval']['evidence']['path']))['authorization']['path']).unlink()
    else:
        data['output'] = record['output']
        data['audio']['output_sha256'] = _aac_sha256(record['output']['path'])
        data['tool_result']['data']['cut_receipt']['output'] = record['output']
    save(path, data)
    record[key] = binding(path)
    reseal(record)
    with pytest.raises(ProductionGovernanceError,
                       match='not the canonical retained interval cut' if change == 'forged_output' else None):
        record_derived_edit(value.root, record)


def test_trim_needs_fresh_review_of_exact_derived_bytes(trimmed):
    value, aid, _, record, _ = trimmed
    record_derived_edit(value.root, record)
    original = {'attempt_id': aid, 'output': record['parent_output'], 'outgoing_frame': record['outgoing_frame']}
    selection = {**original, 'output': record['output'],
        'review': attestation(selection_digest(original), UPSTREAM_PREDICATES)}
    with pytest.raises(ProductionGovernanceError, match='failed or stale'):
        record_selection(value.root, 'entry', selection)
    selection['review'] = attestation(selection_digest(selection), UPSTREAM_PREDICATES)
    next(p for p in selection['review']['predicates'] if p['name'] == 'speaker_source')['status'] = 'fail'
    with pytest.raises(ProductionGovernanceError, match='critical predicates'):
        record_selection(value.root, 'entry', selection)


STUDIO = {'codec': 'libx264', 'audio': True, 'cut_context': 'correct', 'sample_context': 'correct'}


@pytest.mark.parametrize('trimmed', [STUDIO], indirect=True)
def test_studio_project_context_registers_exact_retained_receipts(trimmed):
    value, _, _, record, result = trimmed
    # The actual cut receipt keeps Studio's context; nothing is stripped.
    assert result.data['cut_receipt']['submitted_inputs']['project_dir'] == str(value.root.resolve())
    assert read(Path(record['outgoing_receipt']['path']))['submitted_inputs']['project_dir'] == str(value.root.resolve())
    checked = record_derived_edit(value.root, record)
    assert checked['derived_timing']['duration_seconds'] == 3.5
    assert check(value, record)['selected_output'] == record['output']


@pytest.mark.parametrize('trimmed, message', [
    ({**STUDIO, 'cut_context': 'wrong'}, 'exact canonical cut inputs required'),
    ({**STUDIO, 'cut_context': 'extra'}, 'exact canonical cut inputs required'),
    ({**STUDIO, 'sample_context': 'wrong'}, 'actual final retained-frame sampling differs'),
    ({**STUDIO, 'sample_context': 'extra'}, 'actual final retained-frame sampling differs'),
], indirect=['trimmed'])
def test_project_context_must_be_exactly_the_validated_project(trimmed, message):
    value, _, _, record, _ = trimmed
    with pytest.raises(ProductionGovernanceError, match=message):
        record_derived_edit(value.root, record)


@pytest.mark.parametrize('trimmed', [STUDIO], indirect=True)
def test_recipe_cannot_strip_context_the_actual_cut_received(trimmed):
    value, _, _, record, _ = trimmed
    path = Path(record['recipe']['path'])
    recipe = read(path)
    del recipe['submitted_inputs']['project_dir']
    save(path, recipe)
    record['recipe'] = binding(path)
    receipt = Path(record['execution_receipt']['path'])
    data = read(receipt)
    data['recipe'] = record['recipe']
    save(receipt, data)
    record['execution_receipt'] = binding(receipt)
    reseal(record)
    with pytest.raises(ProductionGovernanceError, match='actual canonical command/return/bindings differ'):
        record_derived_edit(value.root, record)
