"""Exact selected-route authoring guidance; no provider or account mutation."""
import copy

import pytest

from lib import openart_mcp as mcp, production_request as request
from tools.tool_registry import registry
from tests.lib.test_openart_mcp_native import observations  # noqa: F401
from tests.lib.test_production_request import neutral_package  # noqa: F401
from tests.lib.test_production_package_authoring import packages


def test_selected_profile_only_and_modes(observations, monkeypatch):
    monkeypatch.setattr(mcp, '_ALLOW_FIXTURE_PRODUCTION', True)
    monkeypatch.setattr(mcp, 'model_catalog', lambda *a, **kw: pytest.fail('whole catalog profile lookup'))
    start = registry.describe_video_inputs('openart_mcp', 'byte-plus-seedance-2-mini', 'image2video')
    refs = registry.describe_video_inputs('openart_mcp', 'byte-plus-seedance-2-mini', 'element2video')
    assert start['status'] == refs['status'] == 'available'
    assert start['roles']['start_frame']['supported'] and start['roles']['start_frame']['required']
    assert start['roles']['end_frame']['supported'] and not start['roles']['end_frame']['required']
    assert not start['roles']['reference_image']['supported']
    assert refs['roles']['reference_image']['minimum'] == 1
    assert refs['roles']['reference_image']['maximum'] is None
    assert refs['roles']['reference_image']['maximum_status'] == 'unknown'
    assert not refs['roles']['start_frame']['supported'] and not refs['roles']['timed_keyframe']['supported']
    assert refs['provenance']['form_sha256'] and refs['readiness']['production_ready']


@pytest.mark.parametrize('provider,model,mode,operation', [
    ('grok_cli', 'invented', None, 'reference_to_video'),
    ('grok_cli', None, 'image2video', 'image_to_video'),
    ('openart_mcp', 'byte-plus-seedance-2-mini', 'element2video', 'image_to_video'),
    ('openart_mcp', 'absent', 'image2video', 'image_to_video'),
])
def test_unavailable_selection_is_explicit_and_read_only(observations, monkeypatch, provider, model, mode, operation):
    monkeypatch.setattr(mcp, '_ALLOW_FIXTURE_PRODUCTION', True)
    before = {p: p.read_bytes() for p in observations.iterdir()}
    answer = registry.describe_video_inputs(provider, model, mode, operation)
    assert answer['status'] == 'unavailable' and answer['blockers']
    assert {p: p.read_bytes() for p in observations.iterdir()} == before


def test_openart_endpoint_and_reference_templates(observations, monkeypatch, neutral_package):
    monkeypatch.setattr(mcp, '_ALLOW_FIXTURE_PRODUCTION', True)
    root, base, bind = neutral_package
    package, _ = packages(base)
    for shot in package['shots']:
        shot['duration_seconds'] = 6
        ending = copy.deepcopy(shot['boards'][0]); ending.update(slot_id=shot['id']+'-end', role='end_frame')
        shot['boards'].append(ending)
    settings = {'preferred_provider': 'openart_mcp', 'allowed_providers': ['openart_mcp'],
                'model': 'byte-plus-seedance-2-mini', 'mode': 'image2video', 'operation': 'image_to_video',
                'native_params': {'resolution': '720p'}}
    inputs = request.build_video_request_templates(package, settings)
    assert [a['role'] for a in inputs['s1']['input_assets']] == ['first_frame', 'last_frame']
    assert inputs['s1']['input_assets'][1]['source_path'] == {'$board': {'asset_id': 's1-end'}}
    for shot in package['shots']:
        shot['motion'].update(reference_mode='reference_guided'); shot['motion'].pop('frame_input')
        shot['roles'] = [{'asset_id': board['slot_id'], 'role': 'reference_image'} for board in shot['boards']]
    settings.update(mode='element2video', operation='reference_to_video')
    inputs = request.build_video_request_templates(package, settings)
    assert 'image_path' not in inputs['s1']
    assert [a['role'] for a in inputs['s1']['input_assets']] == ['reference_image'] * 2
    request.compile_package(root, bind(package))


@pytest.mark.parametrize('defect', ['fifth', 'spacing', 'outside', 'role', 'duplicate', 'unknown', 'operation', 'duration'])
def test_compact_native_negatives_before_dispatch(neutral_package, defect):
    root, base, bind = neutral_package
    package, _ = packages(base)
    first = package['shots'][0]; first['duration_seconds'] = 6
    settings = {'provider': 'grok_cli', 'operation': 'reference_to_video', 'resolution': '720p'}
    first['roles'] = [{'asset_id': 's1-start', 'role': 'start_frame'}]
    for index in range(4):
        node = copy.deepcopy(first['boards'][0]); node.update(slot_id='middle'+str(index), role='timed_keyframe', time_seconds=index+1)
        first['boards'].append(node)
        first['roles'].append({'asset_id': node['slot_id'], 'role': 'timed_keyframe', 'time_seconds': index+1})
    if defect == 'fifth': first['roles'].append({'asset_id': 's1-start', 'role': 'timed_keyframe', 'time_seconds': 5})
    if defect == 'spacing': first['roles'][2]['time_seconds'] = 1.1
    if defect == 'outside': first['roles'][-1]['time_seconds'] = 6
    if defect == 'role': settings['operation'] = 'image_to_video'
    if defect == 'duplicate': first['roles'].append(copy.deepcopy(first['roles'][0]))
    if defect == 'unknown': first['roles'][0]['asset_id'] = 'missing'
    if defect == 'operation': settings['operation'] = 'text_to_video'
    if defect == 'duration': first['duration_seconds'] = 16
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    with pytest.raises(ValueError): request.build_video_request_templates(package, settings)
    assert {p: p.read_bytes() for p in root.rglob('*') if p.is_file()} == before


def test_grok_descriptor_operation_contract():
    plain = registry.describe_video_inputs('grok_cli', operation='image_to_video')
    pinned = registry.describe_video_inputs('grok_cli', operation='first_last_frame')
    assert plain['settings']['duration']['enum'] == [6, 10]
    assert not plain['roles']['timed_keyframe']['supported']
    assert pinned['roles']['start_frame']['required'] and pinned['roles']['end_frame']['required']
    assert pinned['roles']['timed_keyframe']['maximum'] == 4
    assert pinned['roles']['reference_image']['maximum'] == 14
    assert pinned['model_policy'] == 'cli_managed_unreported' and pinned['model'] is None


def test_exact_alternative_branches_cannot_mix_native_roles(neutral_package, monkeypatch):
    root, base, bind = neutral_package
    package, _ = packages(base)
    first = package['shots'][0]; first['duration_seconds'] = 6
    extra = copy.deepcopy(first['boards'][0]); extra.update(slot_id='aux', role='reference_image')
    first['boards'].append(extra)
    reference = {'type':'object', 'required':['id','type','url'], 'properties':{
        'id':{'type':'string'}, 'type':{'const':'image'}, 'url':{'type':'string'}}}
    schema = {'oneOf':[
        {'type':'object', 'properties':{'prompt':{'type':'string'}, 'duration':{'type':'integer'}, 'startFrame':reference},
         'required':['prompt','duration','startFrame'], 'additionalProperties':False},
        {'type':'object', 'properties':{'prompt':{'type':'string'}, 'duration':{'type':'integer'}, 'visualReferences':{'type':'array','minItems':1,'items':reference}},
         'required':['prompt','duration','visualReferences'], 'additionalProperties':False}]}
    profile = {'provider':'openart_mcp','model':'SYNTHETIC-branch','mode':'image2video','source':'fixture','production_ready':True,
        'native_capabilities':{'root_schema':schema,'roles':{'first_frame':{'supported':True},'reference_image':{'supported':True}}}}
    monkeypatch.setattr(mcp, 'load_profile', lambda *a, **k: copy.deepcopy(profile))
    descriptor = registry.describe_video_inputs('openart_mcp', profile['model'], profile['mode'])
    assert len(descriptor['branches']) == 2
    assert descriptor['roles']['start_frame']['branches'] == [0]
    assert descriptor['roles']['reference_image']['branches'] == [1]
    with pytest.raises(ValueError, match='branch'):
        request.build_video_request_templates(package, {'provider':'openart_mcp','model':profile['model'], 'mode':profile['mode'], 'native_params':{}})
