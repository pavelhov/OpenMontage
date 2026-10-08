"""Real local governance for canonical roles/native settings; no provider calls."""
import copy
import json
import pytest

from lib import production_execution as execution, production_autonomy as autonomy
from tests.lib.test_production_autonomy import fixture_planning, install_existing, make_policy


def enrolled(tmp_path):
    contract = fixture_planning(tmp_path)
    start = next(a for a in contract['assets'] if a['role'] == 'start_frame')
    inputs = {'operation': 'image_to_video', 'model': 'kling-3', 'mode': 'image2video',
              'native_params': {'duration': 8},
              'input_assets': [{'role': 'first_frame', 'source_path': str(tmp_path / start['path']),
                                'source_sha256': start['sha256'], 'upload_id': 'approved-start'}]}
    template = execution.planned_request_template(inputs, project_dir=tmp_path)
    policy, _ = install_existing(tmp_path, make_policy(), templates={'entry': template})
    return contract, inputs, policy


def test_canonical_roles_and_native_duration_bind_real_retained_template(tmp_path):
    contract, inputs, policy = enrolled(tmp_path)
    execution._check_motion_inputs(contract, 'entry', inputs, tmp_path)
    autonomy._validate_request_delta(tmp_path, policy, 'entry', inputs, 'openart_cli')
    assert execution.planned_request_digest(inputs, project_dir=tmp_path) == execution.approved_request_digest(
        execution.planned_request_template(inputs, project_dir=tmp_path), project_dir=tmp_path)


@pytest.mark.parametrize('change', ['source_bytes', 'wrong_role', 'duplicate_duration', 'bool_duration', 'missing_duration'])
def test_canonical_native_governance_rejects_source_role_and_duration_drift(tmp_path, change):
    contract, inputs, policy = enrolled(tmp_path)
    if change == 'source_bytes':
        from pathlib import Path
        Path(inputs['input_assets'][0]['source_path']).write_bytes(b'changed')
    elif change == 'wrong_role':
        inputs['input_assets'][0]['role'] = 'last_frame'
    elif change == 'duplicate_duration': inputs['duration'] = 8
    elif change == 'bool_duration': inputs['native_params']['duration'] = True
    elif change == 'missing_duration': inputs['native_params'].pop('duration')
    with pytest.raises((execution.ProductionGovernanceError, autonomy.AutonomyError)):
        execution._check_motion_inputs(contract, 'entry', inputs, tmp_path)
        autonomy._validate_request_delta(tmp_path, policy, 'entry', inputs, 'openart_cli')


def test_smartshot_uses_its_actual_native_duration_without_output_goal_aliases():
    assert execution.explicit_motion_duration({'model': 'smart-shot', 'native_params': {'videoDuration': 5}}) == 5
    with pytest.raises(execution.ProductionGovernanceError):
        execution.explicit_motion_duration({'model': 'fal-h3-max-turbo', 'native_params': {'videoDuration': 5}})
    with pytest.raises(execution.ProductionGovernanceError):
        execution.explicit_motion_duration({'delivery_duration': 5})


def test_canonical_resolution_flex_changes_only_explicit_approved_controls(tmp_path):
    fixture_planning(tmp_path)
    source = tmp_path / 'assets/start.svg'
    from lib.shot_contract import file_sha256
    baseline = {'model': 'kling-3', 'mode': 'image2video', 'operation': 'image_to_video',
        'native_params': {'duration': 8, 'resolution': '720p', 'generateAudio': False},
        'input_assets': [{'role': 'first_frame', 'source_path': str(source),
            'source_sha256': file_sha256(source), 'upload_id': 'approved'}]}
    policy = make_policy()
    policy['flex']['resolution'] = ['480p']
    policy, _ = install_existing(tmp_path, policy, templates={'entry': execution.planned_request_template(baseline, project_dir=tmp_path)})
    actual = copy.deepcopy(baseline)
    actual['native_params']['resolution'] = '480p'
    autonomy._validate_request_delta(tmp_path, policy, 'entry', actual, 'openart_cli')
    actual['native_params']['generateAudio'] = True
    with pytest.raises(autonomy.AutonomyError, match='request changed'):
        autonomy._validate_request_delta(tmp_path, policy, 'entry', actual, 'openart_cli')


def test_fresh_reference_guided_mode_keeps_identity_bytes_and_boarded_semantics(tmp_path):
    contract = fixture_planning(tmp_path)
    contract['shots'][0]['reference_mode'] = 'reference_guided'
    from tests.lib.test_shot_contract import refresh
    from tests.lib.test_production_request import write
    from lib import production_request as preparation
    refresh(contract)
    write(tmp_path / 'artifacts/shot_contract.json', contract)
    write(tmp_path / 'project.json', {'project_id': contract['project_id'], 'story_revision': contract['story_revision'],
        'governance': {'mode': 'strict', 'version': '1.0'}})
    packet = preparation.source_packet(tmp_path, 'entry', provider='openart_mcp')
    identity = next(ref for ref in packet['binding']['references'] if ref['role'] == 'identity_reference' and set(ref['cast_ids']).intersection(packet['shot']['cast_ids']))
    native = {'provider': 'openart_mcp', 'model': 'fal-h3-max', 'mode': 'element2video',
        'input_assets': [{'role': 'reference_image', 'source_sha256': identity['sha256']}]}
    preparation.validate_openart_asset_roles(packet, native)
    with pytest.raises(ValueError, match='approved shot asset'):
        preparation.validate_openart_asset_roles(packet, {**native, 'input_assets': [
            {'role': 'reference_image', 'source_sha256': 'f' * 64}]})
    with pytest.raises(ValueError, match='omits approved reference'):
        preparation.validate_openart_asset_roles(packet, {**native, 'input_assets': []})
    boarded = copy.deepcopy(packet)
    boarded['binding'].pop('reference_mode')
    with pytest.raises(ValueError, match='approved start board'):
        preparation.validate_openart_asset_roles(boarded, native)
    with pytest.raises(ValueError, match='reference-guided mode'):
        preparation.validate_openart_asset_roles(packet, {**native, 'mode': 'image2video'})


def test_guided_smartshot_character_reference_does_not_claim_native_frame_pin(tmp_path):
    from lib.production_request import validate_openart_asset_roles, _check_required_native_controls
    packet = {'binding': {'reference_mode': 'reference_guided', 'references': [
        {'id': 'start', 'role': 'start_frame', 'sha256': 'a' * 64},
        {'id': 'identity', 'role': 'identity_reference', 'sha256': 'b' * 64, 'cast_ids': ['hero']}]},
        'shot': {'asset_ids': ['start'], 'cast_ids': ['hero']}}
    native = {'provider': 'openart_mcp', 'model': 'smart-shot', 'mode': 'generate-shot-video',
        'input_assets': [{'role': 'environment_reference', 'source_sha256': 'a' * 64},
                         {'role': 'character_reference', 'source_sha256': 'b' * 64}]}
    validate_openart_asset_roles(packet, native)
    for pin in ('pinned_final_frame', 'pinned_start_frame'):
        scene = {'metadata': {'visual_development': {'shot_cards': {'shot': {pin: {'required': True}}}}}}
        with pytest.raises(ValueError, match='native.*pin'):
            _check_required_native_controls(scene, None, 'shot', native=native, project_dir=tmp_path)


def test_native_only_approved_flex_is_logged_before_dispatch(tmp_path):
    fixture_planning(tmp_path)
    baseline = {'duration': 8, 'native_params': {'resolution': '720p'}}
    policy = make_policy()
    policy['flex']['resolution'] = ['480p']
    policy, sha = install_existing(tmp_path, policy, templates={
        'entry': execution.planned_request_template(baseline, project_dir=tmp_path)})
    actual = {**baseline, 'native_params': {'resolution': '480p'}}
    autonomy._validate_request_delta(tmp_path, policy, 'entry', actual, 'openart_cli')
    autonomy._append_route_decisions_locked(tmp_path, policy, sha, 'd-act', 'entry', 'openart_cli', actual)
    decisions = json.loads((tmp_path / 'artifacts/decision_log.json').read_text())['decisions']
    changes = [row for row in decisions if row['category'] == 'budget_tradeoff']
    assert len(changes) == 1
    assert '"resolution": {"from": "720p", "to": "480p"}' in changes[0]['reason']


def test_omitted_source_sha_is_derived_and_approval_still_binds_actual_bytes(tmp_path):
    contract, inputs, policy = enrolled(tmp_path)
    no_claim = copy.deepcopy(inputs)
    no_claim['input_assets'][0].pop('source_sha256')
    assert execution.planned_request_digest(no_claim, project_dir=tmp_path) == execution.planned_request_digest(inputs, project_dir=tmp_path)
    assert execution.planned_request_template(no_claim, project_dir=tmp_path)['inputs']['input_assets'][0]['source_sha256'] == inputs['input_assets'][0]['source_sha256']
    bad = copy.deepcopy(no_claim)
    bad['input_assets'][0].update(upload_id='one', reference_id='different')
    with pytest.raises(execution.ProductionGovernanceError, match='conflicting'):
        execution.planned_request_digest(bad, project_dir=tmp_path)
