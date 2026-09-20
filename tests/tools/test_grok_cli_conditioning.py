"""Offline canonical conditioning and caller-reserved CLI session proof."""
import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from lib.pinned_final_frame import pinned_final_frame_params
from tools._grok_cli_media import GrokCLIContractError, execute_grok_cli_media
from tools.base_tool import ToolStatus
from tools.video.grok_cli_video import GrokCLIVideo
from tools.video.video_selector import VideoSelector
from tests.tools.test_pinned_final_frame_workflow import workflow
from tests.tools.test_grok_cli_media import FakeProcesses, _artifact, _install_fake, _stream


def test_canonical_cli_bridge_retains_every_control_and_receipt(workflow, monkeypatch):
    plan, manifest, root = workflow
    params = pinned_final_frame_params(plan, manifest, scene_id='s1',
        keyframe_asset_id='first', provider='grok_cli', project_dir=root)
    assert 'model' not in params
    # Same bytes represent different roles; never deduplicate by hash.
    identity = root / 'identity.png'
    identity.write_bytes(b'last')
    mid = root / 'mid.png'
    mid.write_bytes(b'middle')
    sessions = root / 'sessions'
    artifact = _artifact(sessions, 'videos', '.mp4')
    expected = dict(prompt='Return home.', duration=6, resolution_name='480p',
        aspect_ratio='16:9', first_frame=params['reference_image_path'],
        last_frame=params['last_image_path'], images=[str(identity)],
        keyframes=[{'image': str(mid), 'timestamp_s': 3.0}], voices=['eve'])
    fake = FakeProcesses(media_stdout=_stream('reference_to_video', 'ReferenceToVideo', artifact,
        raw_input=expected), version='grok 1.0.34', probe={
        'streams': [{'codec_type': 'video', 'codec_name': 'h264', 'width': 1280,
                     'height': 720, 'duration': '6.0'}], 'format': {'duration': '6.0'}})
    _install_fake(monkeypatch, fake)
    cli = GrokCLIVideo(grok_path='fake-grok', sessions_root=str(sessions))
    monkeypatch.setattr(cli, 'get_status', lambda: ToolStatus.AVAILABLE)
    selector = VideoSelector()
    monkeypatch.setattr(selector, '_providers', lambda: [cli])
    result = selector.execute({**params, 'prompt': 'Return home.', 'duration': '6',
        'reference_image_paths': [str(identity)], 'keyframes': [{'image': str(mid), 'timestamp_s': 3}],
        'voices': ['eve'], 'output_path': str(root / 'clip.mp4'), 'allow_unknown_cost': True,
        'cli_session_id': 'session-1'})
    assert result.success, result.error
    assert json.loads(fake.prompt_payloads[0].splitlines()[1]) == expected
    receipt = result.data['conditioning_receipt']
    assert receipt['submitted_arguments'] == expected
    assert receipt['native_tool'] == 'reference_to_video'
    assert receipt['cli_version'] == '1.0.34'
    assert receipt['media_model'] is None
    assert receipt['requirement_id'] == 'ending-1'
    assets = receipt['input_assets']
    assert [item['role'] for item in assets] == ['first_frame', 'last_frame', 'reference', 'keyframe']
    for item in assets:
        assert item['sha256'] == hashlib.sha256(Path(item['path']).read_bytes()).hexdigest()
    assert assets[1]['sha256'] == assets[2]['sha256']
    endpoint = result.data['endpoint_conditioning']
    assert endpoint['requirement_id'] == 'ending-1'
    assert endpoint['last_frame']['sha256'] == assets[1]['sha256']
    assert endpoint['request_sha256'] == receipt['request_sha256']
    assert result.data['session_id'] == 'session-1'


def video_inputs(tmp_path):
    first = tmp_path / 'first.png'
    last = tmp_path / 'last.png'
    first.write_bytes(b'first')
    last.write_bytes(b'last')
    return dict(prompt='Return home.', operation='reference_to_video', first_frame=str(first),
        last_frame=str(last), duration=6, output_path=str(tmp_path / 'clip.mp4'),
        allow_unknown_cost=True, cli_session_id='reserved-session')


@pytest.mark.parametrize('through_selector', [False, True])
@pytest.mark.parametrize('extra', [
    {'endpoint_requirement_id': 'end-1', 'last_frame': None},
    {'model': 'grok-imagine-video-1.5'},
    {'model_name': 'grok-imagine-video-1.5'},
    {'model_variant': 'something'},
    {'last_image_path': 'missing-different.png'},
    {'reference_image_path': 'missing-different.png'},
    {'surprise_control': True},
    {'end_frame': 'something.png'},
    {'reference_image_urls': ['https://example.com/image.png']},
    {'duration': 6.5}, {'duration': True}, {'timeout_seconds': 30.5},
    {'endpoint_requirement_id': ''},
])
def test_invalid_controls_never_start_a_process(tmp_path, monkeypatch, extra, through_selector):
    monkeypatch.setattr('tools._grok_cli_media.subprocess.run',
                        lambda *a, **k: pytest.fail('invalid controls must not start a process'))
    inputs = {**video_inputs(tmp_path), **extra}
    cli = GrokCLIVideo()
    if through_selector:
        selector = VideoSelector()
        monkeypatch.setattr(cli, 'get_status', lambda: ToolStatus.AVAILABLE)
        monkeypatch.setattr(selector, '_providers', lambda: [cli])
        inputs.update(preferred_provider='grok_cli', allowed_providers=['grok_cli'])
        result = selector.execute(inputs)
    else:
        result = cli.execute(inputs)
    assert not result.success
    assert result.data.get('dispatch_status') == 'not_dispatched'


@pytest.mark.parametrize('times', [[0], [6], [-1], [float('nan')], [float('inf')],
                                    [True], ['3'], [3, 2], [3, 3.1]])
def test_invalid_keyframe_times_never_dispatch(tmp_path, monkeypatch, times):
    inputs = video_inputs(tmp_path)
    inputs['keyframes'] = [{'image': inputs['first_frame'], 'timestamp_s': time} for time in times]
    executor = Mock(side_effect=AssertionError('invalid keyframe must not dispatch'))
    monkeypatch.setattr('tools.video.grok_cli_video.execute_grok_cli_media', executor)
    result = GrokCLIVideo().execute(inputs)
    assert not result.success
    assert result.data['session_id'] == 'reserved-session'
    executor.assert_not_called()


@pytest.mark.parametrize('session', ['../escape', '/tmp/escape', 'a/b', 'a\\b', '', 'x' * 129, 123])
def test_unsafe_session_identity_rejects_before_process(tmp_path, monkeypatch, session):
    monkeypatch.setattr('tools._grok_cli_media.subprocess.run',
                        lambda *a, **k: pytest.fail('unsafe session must not start a process'))
    result = execute_grok_cli_media(tool_name='image_to_video', arguments={},
        output_path=str(tmp_path / 'clip.mp4'), cwd=str(tmp_path), grok_path='fake-grok',
        sessions_root=str(tmp_path / 'sessions'), timeout_seconds=600, media_kind='video', session_id=session)
    assert not result.success
    assert result.data['dispatch_status'] == 'not_dispatched'
    assert 'session_id' in result.error


@pytest.mark.parametrize('failure', ['preflight', 'timeout', 'nonzero', 'parse', 'import', 'session_mismatch'])
def test_reserved_session_retained_for_all_failure_paths(tmp_path, monkeypatch, failure):
    from urllib.parse import quote
    from tools._grok_cli_media import _session_diagnostics

    sid = 'caller-reserved-session'
    sessions = tmp_path / 'sessions'
    session_dir = sessions / quote(str(tmp_path.resolve()), safe='') / sid
    session_dir.mkdir(parents=True)
    (session_dir / 'events.jsonl').write_text('{"type":"tool_call"}\n')
    monkeypatch.setattr('tools._grok_cli_media._verify_compatibility', Mock(
        side_effect=GrokCLIContractError('capability', 'not qualified') if failure == 'preflight' else None,
        return_value='1.0.34'))
    launch = Mock(return_value=subprocess.CompletedProcess([], 1 if failure == 'nonzero' else 0, 'bad-json', 'failed'))
    if failure == 'timeout':
        launch.side_effect = GrokCLIContractError('timeout', 'timed out', dispatch_status='indeterminate')
    monkeypatch.setattr('tools._grok_cli_media._run_process', launch)
    if failure in {'import', 'session_mismatch'}:
        monkeypatch.setattr('tools._grok_cli_media._parse_stream',
                            lambda *a, **k: (session_dir / 'artifact.mp4', None, sid if failure == 'import' else 'other-session'))
        monkeypatch.setattr('tools._grok_cli_media._trusted_session_artifact', lambda path, *a: path)
        monkeypatch.setattr('tools._grok_cli_media._copy_and_validate',
                            Mock(side_effect=OSError('cannot import artifact')))
    result = execute_grok_cli_media(tool_name='reference_to_video', arguments={'voices': ['eve']},
        output_path=str(tmp_path / 'clip.mp4'), cwd=str(tmp_path), grok_path='fake-grok',
        sessions_root=str(sessions), timeout_seconds=600, media_kind='video', session_id=sid)
    assert not result.success
    assert result.data['session_id'] == sid
    assert result.data['dispatch_session_id'] == sid
    assert result.data['session_directory'] == str(session_dir)
    assert result.data['dispatch_status'] == ('not_dispatched' if failure == 'preflight' else 'failed' if failure == 'nonzero' else 'indeterminate')
    if failure == 'preflight':
        launch.assert_not_called()
    else:
        assert launch.call_count == 1
        argv = launch.call_args.args[0]
        assert argv[argv.index('--session-id') + 1] == sid
        assert result.data['diagnostics']['session_id'] == sid
        # An outer executor can recover observations with its already saved ID.
        assert _session_diagnostics(sessions, tmp_path.resolve(), sid)['events.jsonl']['tool_call_observed']
    assert result.data['retry_attempted'] is False


def test_equivalent_path_aliases_normalize_identically(tmp_path, monkeypatch):
    from tools.base_tool import ToolResult

    inputs = video_inputs(tmp_path)
    inputs.update(image_path=str(tmp_path / '.' / 'first.png'),
                  reference_image_path=inputs['first_frame'], last_image_path=inputs['last_frame'])
    executor = Mock(return_value=ToolResult(success=True, data={'cli_version': '1.0.34',
        'session_id': 'reserved-session', 'dispatch_status': 'completed'}))
    monkeypatch.setattr('tools.video.grok_cli_video.execute_grok_cli_media', executor)
    cli = GrokCLIVideo()
    monkeypatch.setattr(cli, 'get_status', lambda: ToolStatus.AVAILABLE)
    selector = VideoSelector()
    monkeypatch.setattr(selector, '_providers', lambda: [cli])
    result = selector.execute({**inputs, 'preferred_provider': 'grok_cli', 'allowed_providers': ['grok_cli']})
    assert result.success, result.error
    arguments = executor.call_args.kwargs['arguments']
    assert arguments['first_frame'] == str((tmp_path / 'first.png').resolve())
    assert arguments['last_frame'] == str((tmp_path / 'last.png').resolve())
    assert executor.call_args.kwargs['session_id'] == 'reserved-session'


def test_unexpected_process_oserror_is_indeterminate(tmp_path, monkeypatch):
    monkeypatch.setattr('tools._grok_cli_media._verify_compatibility', lambda *a, **k: '1.0.34')
    monkeypatch.setattr('tools._grok_cli_media._run_process', Mock(side_effect=OSError('process pipe failed')))
    result = execute_grok_cli_media(tool_name='reference_to_video', arguments={'voices': ['eve']},
        output_path=str(tmp_path / 'clip.mp4'), cwd=str(tmp_path), grok_path='fake-grok',
        sessions_root=str(tmp_path / 'sessions'), timeout_seconds=600, media_kind='video', session_id='reserved')
    assert not result.success
    assert result.data['session_id'] == 'reserved'
    assert result.data['dispatch_status'] == 'indeterminate'


def test_cli_route_name_normalizes_without_inventing_model(workflow):
    plan, manifest, root = workflow
    params = pinned_final_frame_params(plan, manifest, scene_id='s1', keyframe_asset_id='first',
        provider=' Grok_CLI ', project_dir=root)
    assert params['preferred_provider'] == 'grok_cli'
    assert 'model' not in params


def test_timeout_receipt_keeps_payload_hashes_without_claiming_verified_call(tmp_path, monkeypatch):
    from tools.base_tool import ToolResult

    inputs = video_inputs(tmp_path)
    inputs['endpoint_requirement_id'] = 'end-1'
    monkeypatch.setattr('tools.video.grok_cli_video.execute_grok_cli_media',
        lambda **kwargs: ToolResult(success=False, data={'cli_version': '1.0.34',
            'session_id': kwargs['session_id'], 'dispatch_status': 'indeterminate'}, error='timed out'))
    result = GrokCLIVideo().execute(inputs)
    assert not result.success
    receipt = result.data['conditioning_receipt']
    assert receipt['submission_evidence'] == 'unconfirmed'
    assert receipt['session_id'] == 'reserved-session'
    assert receipt['submitted_arguments']['last_frame'] == inputs['last_frame']
    stable_request = {key: value for key, value in receipt.items() if key not in {
        'request_sha256', 'cli_version', 'session_id', 'dispatch_status', 'submission_evidence'}}
    assert receipt['request_sha256'] == hashlib.sha256(
        json.dumps(stable_request, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    assert result.data['endpoint_conditioning']['requirement_id'] == 'end-1'


@pytest.mark.parametrize('route', [{}, {'preferred_provider':'grok'},
                                   {'preferred_provider':'grok_cli'}])
def test_native_last_frame_alias_requires_exact_cli_route_before_discovery(monkeypatch, route):
    selector = VideoSelector()
    providers = Mock(side_effect=AssertionError('unsupported route must not discover providers'))
    monkeypatch.setattr(selector, '_providers', providers)
    result = selector.execute({**route, 'last_frame':'last.png'})
    assert not result.success
    assert result.data['dispatch_status'] == 'not_dispatched'
    providers.assert_not_called()
