"""Offline checks for explicit Image 2.0 selection and current pricing."""
from unittest.mock import Mock

import pytest

from tools.graphics.grok_image import GrokImage
from tools.graphics.grok_cli_image import GrokCLIImage
from tools.graphics.image_selector import ImageSelector

MODEL = 'grok-imagine-image-2.0'


@pytest.mark.parametrize('resolution,quality,expected', [
    ('1k', 'low', .04), ('2k', 'low', .06), ('1k', 'medium', .06), ('2k', 'medium', .08),
    ('1k', 'auto', .04),
])
def test_explicit_image_2_payload_and_cost(resolution, quality, expected):
    inputs = {'model': MODEL, 'prompt': 'Poster', 'resolution': resolution, 'quality': quality}
    endpoint, payload = GrokImage()._build_payload(inputs)
    assert endpoint.endswith('/images/generations')
    assert payload['model'] == MODEL and payload['quality'] == quality
    assert GrokImage().estimate_cost(inputs) == pytest.approx(expected)


def test_image_2_edit_auto_cost_and_reference_limit():
    inputs = {'model': MODEL, 'prompt': 'Poster', 'image_urls': ['https://example.com/frame.png'] * 5}
    endpoint, payload = GrokImage()._build_payload(inputs)
    assert endpoint.endswith('/images/edits') and len(payload['images']) == 5
    assert GrokImage().estimate_cost(inputs) == pytest.approx(.11)
    inputs['image_urls'] *= 2
    with pytest.raises(ValueError, match='at most 5'):
        GrokImage()._build_payload(inputs)


@pytest.mark.parametrize('overrides', [
    {'model': 'grok-imagine-video-1.5'}, {'model': 'grok-imagine-image', 'quality': 'low'},
    {'quality': 'high'}, {'resolution': '4k'}, {'prompt': ''},
])
def test_invalid_image_2_requests_never_dispatch(monkeypatch, overrides):
    post = Mock(side_effect=AssertionError('no HTTP'))
    monkeypatch.setattr('requests.post', post)
    result = GrokImage().execute({'prompt': 'Poster', 'model': MODEL, **overrides})
    assert not result.success
    post.assert_not_called()


def test_exact_image_2_cannot_silently_route_to_cli(monkeypatch):
    selector, rest, cli = ImageSelector(), GrokImage(), GrokCLIImage()
    assert selector._filter_candidates({'model': MODEL}, [rest, cli]) == [rest]
    inputs = {'model': MODEL, 'preferred_provider': 'grok_cli', 'allowed_providers': ['grok_cli']}
    assert selector._filter_candidates(inputs, [rest, cli]) == []
    runner = Mock(side_effect=AssertionError('no CLI'))
    monkeypatch.setattr('tools.graphics.grok_cli_image.execute_grok_cli_media', runner)
    assert not cli.execute({'prompt': 'Poster', 'model': MODEL}).success
    runner.assert_not_called()


def test_image_selector_model_alias_quotes_same_price(monkeypatch):
    from tools.base_tool import ToolStatus
    selector, rest = ImageSelector(), GrokImage()
    monkeypatch.setattr(selector, '_providers', lambda: [rest])
    monkeypatch.setattr(rest, 'get_status', lambda: ToolStatus.AVAILABLE)
    inputs = {'prompt': 'Poster', 'preferred_provider': 'grok', 'allowed_providers': ['grok'],
              'quality': 'medium', 'resolution': '2k'}
    direct = selector.estimate_cost({**inputs, 'model': MODEL})
    aliased_inputs = {**inputs, 'model_name': MODEL}
    aliased = selector.estimate_cost(aliased_inputs)
    assert direct == aliased == pytest.approx(.08)
    assert 'model' not in aliased_inputs
