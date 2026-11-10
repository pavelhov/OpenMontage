"""Offline regression corpus and adversarial bounded route qualification."""
import json
from pathlib import Path
import pytest
from lib import spicy_routes as routes

FIXTURE = Path(__file__).parents[1] / 'fixtures/spicy/catalogue-2026-10-22.json'


def test_catalogue_video_output_coverage():
    rows = json.loads(FIXTURE.read_text())['items']
    coverage = routes.route_coverage(rows)
    assert len(coverage) == 160
    assert sum(item['status'] != 'blocked' for item in coverage) == 155
    assert {item['model'] for item in coverage if item['status'] == 'blocked'} == {
        'spicyapi/character-swap-v1/video-edit', 'spicyapi/head-swap-video-v1/video-edit',
        'spicyapi/character-swap-v1/video-analyze', 'spicyapi/head-swap-video-v1/video-analyze',
        'spicyapi/foley-v1/video-to-audio'}


def test_closed_nested_controls_defaults_and_conditionals():
    schema = {'type': 'object', 'properties': {'shots': {'type': 'array', 'items': {
        'type': 'object', 'required': ['text'], 'properties': {'text': {'type':'string'},
        'duration': {'type':'number','minimum':1,'default':2}}}}}, 'required':['shots']}
    value = routes.materialize_defaults(schema, {'shots':[{'text':'one'}]})
    assert value == {'shots':[{'text':'one','duration':2}]}
    assert routes.validate_payload(schema, value)
    with pytest.raises(routes.SchemaError):
        routes.validate_payload(schema, {'shots':[{'text':'one','unknown':True}]})
    conditional = {'type':'object','properties':{'a':{'type':'string'},'b':{'type':'string'}},
        'anyOf':[{'required':['a']},{'required':['b']}]}
    with pytest.raises(routes.SchemaError):
        routes.validate_payload(conditional, {})


@pytest.mark.parametrize('schema', [
    {'type':'object','unevaluatedProperties':False},
    {'type':'object','$ref':'https://unsafe.test/schema'},
    {'type':'object','$defs':{'self':{'$ref':'#/$defs/self'}},'$ref':'#/$defs/self'},
    {'type':'object','properties':{'x':{'type':'string','$id':'nested'}}},
])
def test_unknown_recursive_or_remote_schema_is_blocked(schema):
    assert routes.describe_route(schema)['status'] == 'blocked'


def test_unbound_web_and_media_uris_rejected():
    schema = {'type':'object','properties':{'image_url':{'type':'string',
        'contentMediaType':['image/png'],'x-ui':{'widget':'upload'}},
        'web':{'type':'string','format':'uri'}}}
    for value in ({'image_url':'spicy://f/x'},{'web':'https://example.com'}):
        with pytest.raises(routes.SchemaError):
            routes.validate_payload(schema,value)
    binding = {'pointer':'/image_url','role':'first_frame','media_kind':'image','mime':'image/png','uri':'spicy://f/x'}
    assert routes.validate_payload(schema,{'image_url':'spicy://f/x'},[binding])
