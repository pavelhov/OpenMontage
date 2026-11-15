"""Selected native input ownership; synthetic bytes, no provider requests.

These replace the removed H3-specific geometry workaround. Dimensions are facts,
while native physical support and producer-authored framing are separate checks.
"""
import copy
import hashlib

import pytest
from PIL import Image

from lib import openart_mcp as mcp, production_request as request, spicy_jobs, spicy_uploads
from tests.lib.test_spicy_native_requests_v2 import carrier, observation
from tests.lib.test_openart_mcp_native import observations  # noqa: F401


@pytest.mark.parametrize('size', [(768, 1344), (720, 1264), (1024, 1536), (1344, 768)])
def test_normal_grok_input_builder_has_no_invented_portrait_shape_quota(tmp_path, size):
    path = tmp_path / 'SYNTHETIC-current-start.png'
    Image.new('RGB', size).save(path)
    original = path.read_bytes()
    native = request.prepare_grok_native({'operation': 'image_to_video', 'prompt': 'SYNTHETIC close-up',
        'duration': 6, 'image_path': str(path)}, {'cli_version': '1.0.34', 'grok_path': '/SYNTHETIC/grok'})
    assert native['request']['arguments']['image'] == str(path)
    assert 'aspect_ratio' not in native['request']['arguments']
    assert path.read_bytes() == original
    with Image.open(path) as source:
        assert source.size == size


def test_selected_seedance_minimum_fails_normal_preparation_before_source_resolution(tmp_path, monkeypatch):
    path = tmp_path / 'SYNTHETIC-too-small.png'
    Image.new('RGB', (299, 400)).save(path)
    original = path.read_bytes()
    # Isolate only discovery/account boundaries. The actual selected-route
    # validator must reject before upload/reference resolution or any request.
    profile = {'model': 'byte-plus-seedance-2-5', 'mode': 'element2video', 'source': 'real',
        'form': {'jsonSchema': {'type': 'object', 'properties': {'prompt': {'type': 'string'}}}},
        'native_capabilities': {'roles': {'reference_image': {'supported': True}}}}
    monkeypatch.setattr(mcp, '_check_profile', lambda profile: None)
    monkeypatch.setattr(mcp, 'account_summary', lambda: {})
    monkeypatch.setattr('lib.openart_mcp_jobs.resolve_input_asset',
        lambda *a, **k: pytest.fail('unsupported input must fail before source resolution'))
    with pytest.raises(ValueError, match='reference_dimensions_invalid'):
        mcp.prepare_native_request({'project_dir': str(tmp_path), 'model': profile['model'],
            'mode': profile['mode'], 'prompt': 'SYNTHETIC',
            'input_assets': [{'role': 'reference_image', 'source_path': str(path)}]}, profile)
    assert path.read_bytes() == original
    # This route-specific observation never becomes a universal minimum.
    mcp.validate_image_source_dimensions(path, model='fal-h3-max-turbo', mode='image2video')


@pytest.mark.parametrize('format,accepted', [('PNG', True), ('JPEG', False)])
def test_selected_native_schema_validates_actual_source_format_before_request(tmp_path, monkeypatch, format, accepted):
    model = 'bytedance/seedance-2.0-fast/image-to-video'
    observed = observation(model)
    observed['input_schema']['properties']['image_url']['contentMediaType'] = ['image/png']
    observed['schema_sha256'] = spicy_jobs.digest(observed['input_schema'])
    path = tmp_path / 'SYNTHETIC-start-with-untrusted-extension.png'
    Image.new('RGB', (768, 1344)).save(path, format=format)
    original = path.read_bytes()
    inputs = carrier(tmp_path, model, {'prompt': 'SYNTHETIC close-up',
        'duration_seconds': 5, 'generate_audio': False})
    inputs['input_assets'] = [{'role': 'first_frame', 'pointer': '/image_url', 'source_path': str(path)}]
    monkeypatch.setattr(spicy_uploads, 'usable_receipt_v2', lambda *a: None)
    monkeypatch.setattr(spicy_jobs, 'client_for_credential',
        lambda *a, **k: pytest.fail('input validation must not request a provider'))
    if accepted:
        native = spicy_jobs.build_native_request_v2(inputs, copy.deepcopy(observed))
        assert native['payload_state'] == 'needs_upload'
        assert native['sources'][0]['mime'] == 'image/png'
        assert native['sources'][0]['source_sha256'] == hashlib.sha256(original).hexdigest()
    else:
        with pytest.raises(ValueError, match='source media does not satisfy native slot MIME'):
            spicy_jobs.build_native_request_v2(inputs, copy.deepcopy(observed))
    assert path.read_bytes() == original


def test_h3_normal_captured_native_form_accepts_actual_768_by_1344_without_exception(observations, tmp_path, monkeypatch):
    path = tmp_path / 'SYNTHETIC-current-768x1344.png'
    Image.new('RGB', (768, 1344)).save(path)
    original = path.read_bytes()
    sha = hashlib.sha256(original).hexdigest()
    # Simulate only the completed upload's ready reference. Use the existing
    # captured form, exact profile check and native parameter validation intact.
    def ready_reference(root, asset, **kwargs):
        assert asset['source_path'] == str(path) and asset['source_sha256'] == sha
        return {'source_path': str(path), 'source_sha256': sha, 'receipt_sha256': 'b' * 64,
            'visual_reference': {'type': 'image', 'id': 'SYNTHETIC-ready',
                'url': 'https://fixture.invalid/start.png', 'label': 'SYNTHETIC uploaded original'}}
    monkeypatch.setattr('lib.openart_mcp_jobs.resolve_input_asset', ready_reference)
    profile = mcp.load_profile('fal-h3-max-turbo', 'image2video', require='candidate')
    native = mcp.prepare_native_request({'model': profile['model'], 'mode': profile['mode'],
        'project_dir': str(tmp_path), 'prompt': 'SYNTHETIC intentional close-up',
        'duration': 5, 'resolution': '768P', 'input_assets': [{'role': 'first_frame',
            'source_path': str(path), 'source_sha256': sha}]}, profile)
    assert native['body']['params']['startFrame']['id'] == 'SYNTHETIC-ready'
    assert 'aspectRatio' not in native['body']['params']
    assert native['input_assets'][0]['source_sha256'] == sha
    with Image.open(path) as source:
        assert source.size == (768, 1344)
    assert path.read_bytes() == original
