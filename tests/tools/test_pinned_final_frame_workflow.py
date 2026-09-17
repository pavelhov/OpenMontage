"""Offline canonical-plan -> approved endpoint -> real REST payload regression."""
import base64
from copy import deepcopy
from unittest.mock import Mock

import pytest
from jsonschema import Draft202012Validator
import json
from pathlib import Path

from lib.pinned_final_frame import pinned_final_frame_params
from tools.video.grok_video import GrokVideo
from tools.video.grok_cli_video import GrokCLIVideo
from tools.video.video_selector import VideoSelector


@pytest.fixture
def workflow(tmp_path):
    images = tmp_path / 'assets' / 'images'
    images.mkdir(parents=True)
    for name in ('first', 'last'):
        (images / f'{name}.png').write_bytes(name.encode())
    plan = {'version': '1.0', 'scenes': [{'id': 's1', 'type': 'generated',
        'description': 'Return home', 'start_seconds': 0, 'end_seconds': 6}],
        'metadata': {'visual_development': {'shot_cards': {'s1': {
            'pinned_final_frame': {'required': True, 'requirement_id': 'ending-1',
                                   'end_state': 'Subject is back at its original position.'}}}}}}
    manifest = {'version': '1.0', 'assets': [
        {'id': name, 'scene_id': 's1', 'type': 'image',
         'path': f'assets/images/{name}.png', 'source_tool': 'provided'}
        for name in ('first', 'last')], 'metadata': {'motion_handoffs': {'first': {
            'approved': True, 'asset_id': 'first', 'scene_id': 's1',
            'pinned_final_frame': {'requirement_id': 'ending-1', 'asset_id': 'last',
                                   'approved': True}}}}}
    return plan, manifest, tmp_path


def adapt(workflow, **kwargs):
    plan, manifest, root = workflow
    return pinned_final_frame_params(plan, manifest, scene_id='s1',
        keyframe_asset_id='first', project_dir=root,
        **{'provider': 'grok', 'model': 'grok-imagine-video-1.5', **kwargs})


def test_canonical_artifacts_reach_real_grok_http_payload(workflow, monkeypatch):
    plan, manifest, root = workflow
    for name, artifact in [('scene_plan', plan), ('asset_manifest', manifest)]:
        schema = json.loads((Path(__file__).parents[2] / 'schemas' / 'artifacts' /
                             f'{name}.schema.json').read_text())
        Draft202012Validator(schema).validate(artifact)
    before = deepcopy((plan, manifest))
    params = adapt(workflow)
    assert (plan, manifest) == before
    monkeypatch.setenv('XAI_API_KEY', 'offline-fixture')
    selector, rest = VideoSelector(), GrokVideo()
    monkeypatch.setattr(selector, '_providers', lambda: [rest])
    post = Mock(return_value=Mock(json=lambda: {'request_id': 'offline-request'}))
    monkeypatch.setattr('requests.post', post)
    monkeypatch.setattr('requests.get', Mock(side_effect=[
        Mock(json=lambda: {'status': 'done', 'video': {'url': 'https://example.com/video.mp4'}}),
        Mock(content=b'offline-video')]))
    monkeypatch.setattr('tools.video._shared.probe_output', lambda path: {'duration': 6})
    result = selector.execute({**params, 'prompt': 'Return home.', 'duration': '6',
                               'output_path': str(root / 'clip.mp4')})
    assert result.success, result.error
    assert post.call_count == 1
    payload = post.call_args.kwargs['json']
    assert payload['model'] == 'grok-imagine-video-1.5'
    assert payload['image'] == {'url': 'data:image/png;base64,' + base64.b64encode(b'first').decode()}
    assert payload['last_frame'] == {'url': 'data:image/png;base64,' + base64.b64encode(b'last').decode()}
    assert 'endpoint_requirement_id' not in payload
    assert result.data['endpoint_conditioning']['requirement_id'] == 'ending-1'
    assert result.data['endpoint_conditioning']['last_frame'] == {'path': params['last_image_path']}
    assert result.data['endpoint_conditioning']['constraint'] == 'endpoints_only'


@pytest.mark.parametrize('problem', ['unapproved', 'approval_string', 'requirement', 'missing_binding',
    'missing_requirement', 'missing_end_state', 'missing_asset', 'wrong_scene', 'wrong_type',
    'missing_path', 'empty_file', 'absolute_path', 'traversal', 'duplicate_asset', 'duplicate_scene'])
def test_invalid_bindings_reject_before_dispatch(workflow, problem):
    plan, manifest, root = workflow
    requirement = plan['metadata']['visual_development']['shot_cards']['s1']['pinned_final_frame']
    binding = manifest['metadata']['motion_handoffs']['first']['pinned_final_frame']
    last = manifest['assets'][1]
    if problem == 'unapproved': binding['approved'] = False
    elif problem == 'approval_string': binding['approved'] = 'true'
    elif problem == 'requirement': binding['requirement_id'] = 'stale'
    elif problem == 'missing_binding': del manifest['metadata']['motion_handoffs']['first']
    elif problem == 'missing_requirement': plan['metadata'] = {}
    elif problem == 'missing_end_state': requirement['end_state'] = ''
    elif problem == 'missing_asset': binding['asset_id'] = 'missing'
    elif problem == 'wrong_scene': last['scene_id'] = 's2'
    elif problem == 'wrong_type': last['type'] = 'video'
    elif problem == 'missing_path': last['path'] = 'assets/images/missing.png'
    elif problem == 'empty_file': (root / last['path']).write_bytes(b'')
    elif problem == 'absolute_path': last['path'] = str(root / last['path'])
    elif problem == 'traversal': last['path'] = '../outside.png'
    elif problem == 'duplicate_asset': manifest['assets'].append(dict(last))
    elif problem == 'duplicate_scene': plan['scenes'].append(dict(plan['scenes'][0]))
    with pytest.raises(ValueError):
        adapt(workflow)


@pytest.mark.parametrize('kwargs', [{'provider': 'auto'}, {'provider': ''}, {'model': 'auto'}, {'model': ''}])
def test_explicit_provider_and_model_are_required(workflow, kwargs):
    with pytest.raises(ValueError):
        adapt(workflow, **kwargs)


def test_same_image_loop_is_valid(workflow):
    _, manifest, _ = workflow
    manifest['metadata']['motion_handoffs']['first']['pinned_final_frame']['asset_id'] = 'first'
    params = adapt(workflow)
    assert params['reference_image_path'] == params['last_image_path']


@pytest.mark.parametrize('provider,model', [
    ('grok_cli', 'grok-imagine-video-1.5'), ('grok', 'invented-model'),
    ('grok', 'grok-imagine-video-1.5')])
def test_unsupported_or_missing_credentials_do_not_dispatch_or_migrate(workflow, monkeypatch, provider, model):
    monkeypatch.delenv('XAI_API_KEY', raising=False)
    selector = VideoSelector()
    rest, cli = GrokVideo(), GrokCLIVideo()
    monkeypatch.setattr(selector, '_providers', lambda: [cli, rest])
    post = Mock(side_effect=AssertionError('No network'))
    monkeypatch.setattr('requests.post', post)
    cli_execute = Mock(side_effect=AssertionError('No CLI dispatch'))
    monkeypatch.setattr(cli, 'execute', cli_execute)
    result = selector.execute(adapt(workflow, provider=provider, model=model))
    assert not result.success
    assert result.data['fallback_tools'] == []
    post.assert_not_called()
    cli_execute.assert_not_called()


@pytest.mark.parametrize('problem', ['missing_keyframe', 'keyframe_wrong_scene', 'keyframe_wrong_type',
                                      'unknown_scene', 'not_required'])
def test_starting_frame_and_scene_binding_are_checked(workflow, problem):
    plan, manifest, _ = workflow
    if problem == 'missing_keyframe': manifest['assets'].pop(0)
    elif problem == 'keyframe_wrong_scene': manifest['assets'][0]['scene_id'] = 's2'
    elif problem == 'keyframe_wrong_type': manifest['assets'][0]['type'] = 'video'
    elif problem == 'unknown_scene': plan['scenes'][0]['id'] = 's2'
    elif problem == 'not_required':
        plan['metadata']['visual_development']['shot_cards']['s1']['pinned_final_frame']['required'] = False
    with pytest.raises(ValueError):
        adapt(workflow)


def test_symlink_cannot_bind_image_outside_project(workflow, tmp_path_factory):
    _, manifest, root = workflow
    outside = tmp_path_factory.mktemp('outside') / 'last.png'
    outside.write_bytes(b'outside-image')
    last = root / manifest['assets'][1]['path']
    last.unlink()
    last.symlink_to(outside)
    with pytest.raises(ValueError, match='escapes'):
        adapt(workflow)


@pytest.mark.parametrize('approval', [False, None, 'true', 1])
def test_starting_keyframe_requires_explicit_approval(workflow, approval):
    handoff = workflow[1]['metadata']['motion_handoffs']['first']
    if approval is None:
        del handoff['approved']
    else:
        handoff['approved'] = approval
    with pytest.raises(ValueError, match='Starting-keyframe.*approved'):
        adapt(workflow)


@pytest.mark.parametrize('field,value', [('asset_id', 'last'), ('scene_id', 's2'),
                                         ('asset_id', None), ('scene_id', '')])
def test_starting_handoff_identity_must_match_when_present(workflow, field, value):
    workflow[1]['metadata']['motion_handoffs']['first'][field] = value
    with pytest.raises(ValueError, match='handoff.*does not match'):
        adapt(workflow)


def test_optional_handoff_identity_uses_canonical_key_and_asset_scene(workflow):
    handoff = workflow[1]['metadata']['motion_handoffs']['first']
    del handoff['asset_id']
    del handoff['scene_id']
    assert adapt(workflow)['endpoint_requirement_id'] == 'ending-1'
