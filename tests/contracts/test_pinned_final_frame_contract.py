"""Backward-compatible artifact contracts for required video endpoints."""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]


def schema(name):
    return json.loads((ROOT / 'schemas/artifacts' / f'{name}.schema.json').read_text())


def scene_plan():
    return {
        'version': '1.0',
        'scenes': [{'id': 's1', 'type': 'generated', 'description': 'Door shuts',
                    'start_seconds': 0, 'end_seconds': 6}],
        'metadata': {'unrelated': {'keep': True}, 'visual_development': {
            'shot_cards': {'s1': {'scene_id': 's1', 'dramaturgy': {},
                'pinned_final_frame': {'required': True, 'requirement_id': 'r1',
                                       'end_state': 'Door closed'}}}}},
    }


def manifest():
    return {
        'version': '1.0',
        'assets': [{'id': 'first', 'type': 'image', 'path': 'assets/first.png',
                    'source_tool': 'provided', 'scene_id': 's1'}],
        'metadata': {'unrelated': ['kept'], 'motion_handoffs': {
            'first': {'asset_id': 'first', 'preserve': ['identity'],
                'pinned_final_frame': {'requirement_id': 'r1', 'asset_id': 'last',
                                       'approved': True}}}},
    }


def test_endpoint_sidecars_validate_without_opening_canonical_items():
    for name, artifact in [('scene_plan', scene_plan()), ('asset_manifest', manifest())]:
        contract = schema(name)
        Draft202012Validator.check_schema(contract)
        Draft202012Validator(contract).validate(artifact)
        collection = 'scenes' if name == 'scene_plan' else 'assets'
        assert contract['properties'][collection]['items']['additionalProperties'] is False
        invalid = copy.deepcopy(artifact)
        invalid[collection][0]['pinned_final_frame'] = {}
        assert not Draft202012Validator(contract).is_valid(invalid)


@pytest.mark.parametrize('field,value', [('required', False), ('requirement_id', ''),
                                         ('end_state', ''), ('unsupported', 'x')])
def test_invalid_planned_requirement_is_rejected(field, value):
    artifact = scene_plan()
    artifact['metadata']['visual_development']['shot_cards']['s1']['pinned_final_frame'][field] = value
    assert not Draft202012Validator(schema('scene_plan')).is_valid(artifact)


@pytest.mark.parametrize('missing', ['requirement_id', 'asset_id', 'approved'])
def test_partial_endpoint_binding_is_rejected(missing):
    artifact = manifest()
    del artifact['metadata']['motion_handoffs']['first']['pinned_final_frame'][missing]
    assert not Draft202012Validator(schema('asset_manifest')).is_valid(artifact)


def test_pending_binding_is_storable_and_legacy_metadata_still_valid():
    artifact = manifest()
    artifact['metadata']['motion_handoffs']['first']['pinned_final_frame']['approved'] = False
    Draft202012Validator(schema('asset_manifest')).validate(artifact)
    del artifact['metadata']['motion_handoffs']['first']['pinned_final_frame']
    Draft202012Validator(schema('asset_manifest')).validate(artifact)
    plan = scene_plan()
    del plan['metadata']['visual_development']['shot_cards']['s1']['pinned_final_frame']
    Draft202012Validator(schema('scene_plan')).validate(plan)
