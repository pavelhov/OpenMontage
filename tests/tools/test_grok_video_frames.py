"""Offline REST payload and routing contracts for xAI's pinned-frame mode."""
from unittest.mock import Mock

import pytest
from jsonschema import Draft7Validator

from tools.base_tool import ToolResult, ToolStatus
from tools.video.grok_video import GrokVideo
from tools.video.grok_cli_video import GrokCLIVideo
from tools.video.video_selector import VideoSelector

MODEL = 'grok-imagine-video-1.5'
FIRST = 'https://example.com/first.png'
LAST = 'https://example.com/last.png'


def request(**overrides):
    return dict(operation='first_last_frame', model=MODEL, image_url=FIRST,
                last_image_url=LAST, duration=6, resolution='720p', **overrides)


def test_rest_same_image_loop_payload_has_no_invented_switch():
    inputs = request()
    inputs['last_image_url'] = FIRST
    payload = GrokVideo()._build_payload(inputs)
    assert payload == {'model': MODEL, 'image': {'url': FIRST},
                       'last_frame': {'url': FIRST}, 'duration': 6, 'resolution': '720p'}
    assert GrokVideo().estimate_cost(inputs) == pytest.approx(.86)


def test_local_first_last_frames_and_reference_guidance(tmp_path):
    first, last = tmp_path / 'first.png', tmp_path / 'last.png'
    first.write_bytes(b'first-fixture')
    last.write_bytes(b'last-fixture')
    payload = GrokVideo()._build_payload({
        'model': MODEL, 'operation': 'first_last_frame',
        'reference_image_path': str(first), 'last_image_path': str(last),
        'reference_image_urls': [FIRST], 'prompt': 'Camera returns home.',
    })
    assert payload['image']['url'].startswith('data:image/png;base64,')
    assert payload['last_frame']['url'].startswith('data:image/png;base64,')
    assert payload['image'] != payload['last_frame']
    assert payload['reference_images'] == [{'url': FIRST}]


def test_last_frame_only_and_image_to_video_pair():
    inputs = request()
    del inputs['image_url']
    assert 'image' not in GrokVideo()._build_payload(inputs)
    inputs.update(operation='image_to_video', image_url=FIRST)
    assert GrokVideo()._build_payload(inputs)['last_frame'] == {'url': LAST}


@pytest.mark.parametrize('change', [
    {'model': 'grok-imagine-video'}, {'model': 'grok-imagine-image-2.0'},
    {'model': None}, {'operation': 'invented'}, {'operation': 'text_to_video'},
    {'duration': 0}, {'duration': 16}, {'duration': 6.5}, {'duration': True},
    {'resolution': '1080p'}, {'resolution': '540p'}, {'aspect_ratio': '2:1'},
    {'last_image_url': ''}, {'last_image_url': None},
    {'last_image_path': '/tmp/ambiguous.png'}, {'loop': True}, {'seamless_loop': True},
    {'last_frame': {'url': LAST}}, {'image_path': '/tmp/ambiguous.png'},
    {'image_url': 'file:///etc/passwd'}, {'last_image_url': 'data:image/png;base64,?'},
    {'reference_image_urls': [FIRST] * 8}, {'reference_image_urls': [FIRST] * 7, 'reference_image_paths': ['x.png']},
    {'reference_audios': [{'voice_id': 'eve'}]}, {'video_url': FIRST},
    {'reference_image_url': LAST}, {'timeout_seconds': 0},
])
def test_bad_inputs_fail_before_http(monkeypatch, change):
    post = Mock(side_effect=AssertionError('must not dispatch'))
    monkeypatch.setattr('requests.post', post)
    inputs = request()
    inputs.update(change)
    result = GrokVideo().execute(inputs)
    assert not result.success
    assert result.data['dispatch_status'] == 'not_dispatched'
    post.assert_not_called()


def test_explicit_model_and_last_frame_required_by_schema():
    validator = Draft7Validator(GrokVideo.input_schema)
    assert validator.is_valid(request())
    for field in ('model', 'last_image_url'):
        inputs = request()
        del inputs[field]
        assert not validator.is_valid(inputs)
        with pytest.raises(ValueError):
            GrokVideo()._build_payload(inputs)


def test_classic_generation_and_15_second_new_reference_mode():
    tool = GrokVideo()
    assert tool._build_payload({'prompt': 'A cloud moves.'})['model'] == 'grok-imagine-video'
    assert tool.estimate_cost({'duration': 6, 'resolution': '720p'}) == pytest.approx(.42)
    payload = tool._build_payload({'model': MODEL, 'operation': 'reference_to_video',
                                   'reference_image_urls': [FIRST], 'duration': 15})
    assert payload['duration'] == 15
    assert 'image' not in payload
    assert tool._build_payload({'prompt': 'Cloud', 'model': MODEL, 'resolution': '1080p'})['resolution'] == '1080p'


def test_http_submission_sends_verified_last_frame_once(monkeypatch, tmp_path):
    monkeypatch.setenv('XAI_API_KEY', 'offline-fixture')
    post = Mock(return_value=Mock(json=lambda: {'request_id': 'request-1'}))
    monkeypatch.setattr('requests.post', post)
    get = Mock(side_effect=[Mock(json=lambda: {'status': 'done', 'video': {'url': 'https://example.com/video.mp4'}}),
                            Mock(content=b'fixture-video')])
    monkeypatch.setattr('requests.get', get)
    monkeypatch.setattr('tools.video._shared.probe_output', lambda path: {'duration': 6})
    result = GrokVideo().execute(request(output_path=str(tmp_path / 'clip.mp4')))
    assert result.success and result.model == MODEL
    assert post.call_count == 1 and get.call_count == 2
    assert post.call_args.kwargs['json']['last_frame'] == {'url': LAST}
    assert (tmp_path / 'clip.mp4').read_bytes() == b'fixture-video'


@pytest.mark.parametrize('operation', ['first_last_frame', 'image_to_video'])
def test_selector_local_frames_do_not_upload_to_fal(monkeypatch, tmp_path, operation):
    image = tmp_path / 'frame.png'
    image.write_bytes(b'fixture')
    tool, selector = GrokVideo(), VideoSelector()
    monkeypatch.setattr(tool, 'get_status', lambda: ToolStatus.AVAILABLE)
    monkeypatch.setattr(selector, '_providers', lambda: [tool])
    monkeypatch.setattr(selector, '_select_best_tool', lambda *a: (tool, None))
    captured = []
    def execute(inputs):
        captured.append(tool._build_payload(inputs))
        return ToolResult(success=True)
    monkeypatch.setattr(tool, 'execute', execute)
    upload = Mock(side_effect=AssertionError('No cross-provider upload'))
    monkeypatch.setattr('tools.video._shared.upload_image_fal', upload)
    result = selector.execute({'operation': operation, 'model': MODEL,
        'preferred_provider': 'grok', 'allowed_providers': ['grok'],
        'reference_image_path': str(image), 'last_image_path': str(image), 'duration': '6'})
    assert result.success and captured[0]['image'] == captured[0]['last_frame']
    upload.assert_not_called()


def test_selector_rejects_unsupported_frame_route_and_unknown_model(monkeypatch):
    selector, cli, rest = VideoSelector(), GrokCLIVideo(), GrokVideo()
    monkeypatch.setattr(selector, '_providers', lambda: [cli, rest])
    for operation in ('image_to_video', 'first_last_frame'):
        inputs = {'operation': operation, 'preferred_provider': 'grok_cli',
                  'allowed_providers': ['grok_cli'], 'last_image_url': LAST}
        result = selector.execute(inputs)
        assert not result.success and 'pinned final frame' in result.error
        assert selector.fallback_tools_for(inputs) == []
        rank = selector.execute({**inputs, 'operation': 'rank', 'target_operation': operation})
        assert rank.data['rankings'] == []
    for model in ('grok-imagine-video', 'invented-video', None):
        assert selector._filter_candidates({**request(), 'model': model}, [rest]) == []


@pytest.mark.parametrize('field', ['last_image_url', 'last_image_path', 'last_frame', 'end_frame', 'loop', 'seamless_loop'])
def test_direct_cli_rejects_frame_controls_before_any_process(monkeypatch, field):
    runner = Mock(side_effect=AssertionError('No process'))
    monkeypatch.setattr('tools.video.grok_cli_video.execute_grok_cli_media', runner)
    result = GrokCLIVideo().execute({'prompt': 'Loop', 'operation': 'image_to_video', field: LAST})
    assert not result.success and result.data['error_category'] == 'capability'
    assert result.data['dispatch_status'] == 'not_dispatched'
    runner.assert_not_called()


@pytest.mark.parametrize('field', ['last_frame', 'end_frame_url', 'loop', 'seamless_loop'])
def test_selector_rejects_unsupported_controls_without_provider_discovery(monkeypatch, field):
    selector = VideoSelector()
    providers = Mock(side_effect=AssertionError('Do not even discover a fallback'))
    monkeypatch.setattr(selector, '_providers', providers)
    result = selector.execute({'prompt': 'Loop', field: LAST})
    assert not result.success and result.data['fallback_tools'] == []
    providers.assert_not_called()
