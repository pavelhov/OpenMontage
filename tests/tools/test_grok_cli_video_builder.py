"""Offline native preparation boundaries; forward-authored extraction coverage."""
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from tools._grok_cli_media import (
    GROK_VIDEO_NATIVE_PROFILE, MODEL_PROVENANCE, GrokCLIContractError,
    check_cli_feature_gates, observe_grok_cli_compatibility,
)
from tools.base_tool import ToolResult
from tools.video.grok_cli_video import GrokCLIVideo, build_native_video_request


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


@pytest.fixture
def native_inputs(tmp_path, monkeypatch):
    # Pure preparation must never even discover or inspect the installed CLI.
    monkeypatch.setattr('tools._grok_cli_media.subprocess.run',
                        Mock(side_effect=AssertionError('no process during preparation')))
    files = []
    for name in ('first', 'last', 'mid', 'reference'):
        path = tmp_path / f'{name}.png'
        path.write_bytes(name.encode())
        files.append(str(path))
    return dict(prompt='Return home.', operation='first_last_frame', duration=6,
                first_frame=files[0], last_frame=files[1],
                keyframes=[{'image': files[2], 'timestamp_s': 3}],
                reference_image_paths=[files[3]], voices=['eve'],
                endpoint_requirement_id='end-1')


def build(inputs):
    return build_native_video_request(inputs, adapter_version=GrokCLIVideo.version)


def test_exact_arguments_receipt_digest_and_execute_bytes(native_inputs, tmp_path, monkeypatch):
    prepared = build(native_inputs)
    expected_arguments = dict(prompt='Return home.', duration=6, resolution_name='480p',
                              aspect_ratio='16:9', first_frame=native_inputs['first_frame'],
                              last_frame=native_inputs['last_frame'], images=native_inputs['reference_image_paths'],
                              keyframes=[{'image': native_inputs['keyframes'][0]['image'], 'timestamp_s': 3.0}],
                              voices=['eve'])
    assets = [
        {'role': 'first_frame', 'path': native_inputs['first_frame']},
        {'role': 'last_frame', 'path': native_inputs['last_frame']},
        {'role': 'reference', 'path': native_inputs['reference_image_paths'][0], 'index': 0},
        {'role': 'keyframe', 'path': native_inputs['keyframes'][0]['image'], 'index': 0, 'timestamp_s': 3.0},
    ]
    for item in assets:
        item['sha256'] = hashlib.sha256(Path(item['path']).read_bytes()).hexdigest()
    receipt = dict(version='1.0', provider='grok_cli', native_tool='reference_to_video',
                   adapter_version=GrokCLIVideo.version, **MODEL_PROVENANCE,
                   requirement_id='end-1', submitted_arguments=expected_arguments, input_assets=assets)
    receipt['request_sha256'] = hashlib.sha256(canonical(receipt)).hexdigest()
    assert canonical(prepared['receipt']) == canonical(receipt)
    assert canonical(prepared['arguments']) == canonical(expected_arguments)
    assert prepared['operation'] == 'first_last_frame'
    assert prepared['request_sha256'] == receipt['request_sha256']
    executor = Mock(return_value=ToolResult(success=True, data={
        'cli_version': '1.0.34', 'session_id': 'fake-session', 'dispatch_status': 'succeeded'}))
    monkeypatch.setattr('tools.video.grok_cli_video.execute_grok_cli_media', executor)
    result = GrokCLIVideo().execute({**native_inputs, 'output_path': str(tmp_path / 'clip.mp4'),
                                    'allow_unknown_cost': True})
    receipt.update(cli_version='1.0.34', session_id='fake-session',
                   dispatch_status='succeeded', submission_evidence='verified_native_call')
    assert canonical(result.data['conditioning_receipt']) == canonical(receipt)
    assert canonical(executor.call_args.kwargs['arguments']) == canonical(expected_arguments)


@pytest.mark.parametrize('operation,durations', [
    ('image_to_video', [6, 10]), ('reference_to_video', [1, 15]), ('first_last_frame', [1, 15]),
])
def test_native_duration_boundaries(native_inputs, operation, durations):
    inputs = dict(prompt=native_inputs['prompt'], operation=operation, image_path=native_inputs['first_frame'])
    if operation == 'first_last_frame':
        inputs['last_frame'] = native_inputs['last_frame']
    for duration in durations:
        assert build({**inputs, 'duration': duration})['arguments']['duration'] == duration


@pytest.mark.parametrize('extra', [
    {'model': None}, {'model_name': 'grok-imagine-video-1.5'}, {'native_audio': True},
    {'voice': 'eve'}, {'surprise': True}, {'last_image_url': 'https://example.com/a.png'},
    {'end_frame': 'image.png'}, {'loop': True}, {'duration': 0}, {'duration': 16},
    {'duration': 6.5}, {'duration': True}, {'resolution': '1080p'}, {'aspect_ratio': '5:4'},
    {'voices': ['a', 'b', 'c', 'd']}, {'voices': [' eve']}, {'keyframes': None},
    {'keyframes': [{'image': 'irrelevant', 'timestamp_s': 0}]},
    {'endpoint_requirement_id': ''},
])
def test_rejected_native_controls(native_inputs, extra):
    with pytest.raises(GrokCLIContractError):
        build({**native_inputs, **extra})


@pytest.mark.parametrize('extra', [
    {'duration': 1}, {'duration': 15}, {'aspect_ratio': '16:9'}, {'voices': []},
    {'first_frame': None}, {'last_frame': None}, {'last_image_path': None},
    {'keyframes': []}, {'reference_image_paths': []}, {'endpoint_requirement_id': 'end-1'},
])
def test_image_to_video_rejects_reference_controls(native_inputs, extra):
    with pytest.raises(GrokCLIContractError):
        build(dict(prompt=native_inputs['prompt'], operation='image_to_video',
                   image_path=native_inputs['first_frame'], **extra))


def test_images_voices_limits_and_alias_conflict(native_inputs):
    assert len(build({**native_inputs, 'reference_image_paths': [native_inputs['first_frame']] * 14,
                      'voices': ['a', 'b', 'c']})['arguments']['images']) == 14
    with pytest.raises(GrokCLIContractError):
        build({**native_inputs, 'reference_image_paths': [native_inputs['first_frame']] * 15})
    with pytest.raises(GrokCLIContractError):
        build({**native_inputs, 'image_path': native_inputs['last_frame']})


@pytest.mark.parametrize('arguments,minimum', [({'voices': ['eve']}, '1.0.25'),
    ({'first_frame': 'local.png'}, '1.0.34'), ({'last_frame': 'local.png'}, '1.0.34'),
    ({'keyframes': [{'image': 'local.png', 'timestamp_s': 3}]}, '1.0.34')])
def test_feature_gates_minimum_and_prerelease(arguments, minimum):
    check_cli_feature_gates(minimum, arguments)
    check_cli_feature_gates(minimum + '+build', arguments)
    for version in ('1.0.18', minimum + '-rc.1'):
        with pytest.raises(GrokCLIContractError) as caught:
            check_cli_feature_gates(version, arguments)
        assert caught.value.category == 'capability'


def test_observation_is_fresh_absolute_and_thin(tmp_path, monkeypatch):
    verify = Mock(return_value='1.0.34')
    monkeypatch.setattr('tools._grok_cli_media._verify_compatibility', verify)
    monkeypatch.setattr('tools._grok_cli_media.shutil.which', lambda path: str(tmp_path / 'grok'))
    for _ in range(2):
        assert observe_grok_cli_compatibility('grok', cwd=tmp_path) == {
            'grok_path': str(tmp_path / 'grok'), 'cli_version': '1.0.34'}
    assert verify.call_count == 2
    assert verify.call_args.kwargs['cwd'] == tmp_path


def test_native_profile_json_and_provenance():
    profile = json.loads(canonical(GROK_VIDEO_NATIVE_PROFILE))
    assert profile['image_to_video'] == dict(durations=[6, 10], resolutions=['480p', '720p'],
        aspect_ratios=None, pins=False, max_images=0, max_voices=0)
    for operation in ('reference_to_video', 'first_last_frame'):
        assert profile[operation]['duration_range'] == [1, 15]
        assert profile[operation]['max_images'] == 14
        assert profile[operation]['max_voices'] == 3
    assert profile['min_cli'] == dict(base='1.0.18', voices='1.0.25', pins='1.0.34')
    assert profile['model_argument'] is False
    assert profile['native_audio_flag'] is None
    assert profile['media_model'] is None
    assert profile['agent_model'] == 'grok-4.6'


@pytest.mark.parametrize('configured', ['grok', './grok'])
def test_observer_missing_path_name_does_not_authorize_local_binary(tmp_path, monkeypatch, configured):
    local = tmp_path / 'grok'
    local.write_text('offline fake executable')
    local.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('tools._grok_cli_media.shutil.which', lambda path: None)
    verify = Mock(return_value='1.0.34')
    monkeypatch.setattr('tools._grok_cli_media._verify_compatibility', verify)
    with pytest.raises(GrokCLIContractError) as caught:
        observe_grok_cli_compatibility(configured, cwd=tmp_path)
    assert caught.value.category == 'capability'
    verify.assert_not_called()


@pytest.mark.parametrize('kind', ['absolute', 'resolved_relative', 'unresolved_relative'])
def test_observer_explicit_paths_match_dispatch_resolution(tmp_path, monkeypatch, kind):
    working = tmp_path / 'working'
    working.mkdir()
    monkeypatch.chdir(tmp_path)
    configured = str(tmp_path / 'absolute-grok') if kind == 'absolute' else 'bin/grok'
    resolved = 'bin/grok' if kind == 'resolved_relative' else None
    monkeypatch.setattr('tools._grok_cli_media.shutil.which', lambda path: resolved)
    verify = Mock(return_value='1.0.34')
    monkeypatch.setattr('tools._grok_cli_media._verify_compatibility', verify)
    expected = (tmp_path / 'absolute-grok' if kind == 'absolute' else
                (tmp_path if resolved else working) / 'bin/grok')
    observed = observe_grok_cli_compatibility(configured, cwd=working)
    assert observed == {'cli_version': '1.0.34', 'grok_path': str(expected)}
    verify.assert_called_once_with(str(expected), cwd=working)
