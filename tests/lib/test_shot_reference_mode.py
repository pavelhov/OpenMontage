"""Offline mixed-reference contracts; synthetic reviews never qualify a provider."""
import copy
import json

import pytest

from lib import production_execution as execution, production_request as request
from lib.shot_contract import contract_digest, validate_shot_contract
from tests.lib.test_shot_contract import package, refresh
from tests.lib.test_production_request import reference_free_package, write


@pytest.fixture
def mixed(package):
    contract, root = package
    cutaway = copy.deepcopy(contract['shots'][0])
    cutaway.update(id='cutaway', reference_mode='reference_free', cast_ids=[],
                   required_visible_speakers=[], dialogue=[], asset_ids=[], upstream=[],
                   initial_state='A cube hangs over a floor.',
                   dominant_action='The cube drops.', completed_end_state='The cube rests on the floor.',
                   prop_body_invariants=['The cube remains solid.'])
    contract['shots'].append(cutaway)
    refresh(contract)
    write(root / 'artifacts/shot_contract.json', contract)
    write(root / 'project.json', {'project_id': contract['project_id'],
          'story_revision': contract['story_revision'], 'governance': {'version': '1.0', 'mode': 'strict'}})
    write(root / 'artifacts/scene_plan.json', {'version': '1.0', 'scenes': [
          {'id': sid, 'type': 'generated', 'description': 'Synthetic contract test',
           'script_section_id': f's{i}', 'start_seconds': i * 8, 'end_seconds': (i + 1) * 8}
          for i, sid in enumerate(('entry', 'cutaway'))]})
    write(root / 'artifacts/script.json', {'version': '1.0', 'title': 'Synthetic contract test',
          'total_duration_seconds': 16, 'sections': [
          {'id': f's{i}', 'text': 'Synthetic shot context.', 'start_seconds': i * 8, 'end_seconds': (i + 1) * 8}
          for i in range(2)]})
    return contract, root


def check(mixed, shot='cutaway'):
    contract, root = mixed
    return validate_shot_contract(contract, project_dir=root, shot_id=shot,
                                  story_revision=contract['story_revision'])


def test_local_free_cutaway_keeps_boarded_shot_and_omits_native_references(mixed):
    _, root = mixed
    assert check(mixed)['eligible'] and check(mixed, 'entry')['eligible']
    packet = request.source_packet(root, 'cutaway')
    assert packet['binding']['reference_mode'] == 'reference_free'
    assert packet['binding']['references'] == packet['binding']['upstream'] == []
    boarded = request.source_packet(root, 'entry')
    assert 'reference_mode' not in boarded['binding']
    assert {'start_frame', 'end_frame', 'payoff_board'}.issubset(
        {item['role'] for item in boarded['binding']['references']})
    assert not (root / 'production_attempts').exists()


@pytest.mark.parametrize('field', ['cast_ids', 'required_visible_speakers', 'dialogue', 'asset_ids', 'upstream'])
def test_local_free_cutaway_cannot_drop_declared_obligations(mixed, field):
    contract, _ = mixed
    values = {'cast_ids': ['patient'], 'required_visible_speakers': ['patient'],
              'dialogue': [{'speaker_id': 'patient', 'source': 'visible', 'text': 'Help.',
                            'start_seconds': 0, 'end_seconds': 1}],
              'asset_ids': [contract['payoff_asset_id']], 'upstream': [{'shot_id': 'entry'}]}
    contract['shots'][1][field] = values[field]
    refresh(contract)
    assert not check(mixed)['eligible']


def test_empty_boarded_shot_is_still_refused(mixed):
    contract, _ = mixed
    contract['shots'][0]['asset_ids'] = []
    refresh(contract)
    assert not check(mixed, 'entry')['eligible']
    contract['shots'][1].pop('reference_mode')
    refresh(contract)
    assert not check(mixed)['eligible']


@pytest.mark.parametrize('failure', ['payoff', 'late_cast'])
def test_local_free_flag_preserves_global_project_prerequisites(mixed, failure):
    contract, _ = mixed
    if failure == 'payoff':
        contract['payoff_asset_id'] = 'missing-payoff'
    else:
        contract['late_cast_ids'].append('missing-late-character')
    refresh(contract)
    assert not check(mixed)['eligible']
    assert not check(mixed, 'entry')['eligible']


def test_local_free_flag_cannot_create_a_grok_text_only_route(mixed):
    _, root = mixed
    with pytest.raises(ValueError, match='OpenArt text2video'):
        request.source_packet(root, 'cutaway', provider='grok_cli')


def test_grok_preparation_consumes_selector_plumbing_without_changing_authority(mixed):
    contract, root = mixed
    first = next(a for a in contract['assets'] if a['role'] == 'start_frame')
    inputs = {'project_dir': str(root), 'governance': {'scope_id': 'planned', 'shot_id': 'entry'},
              'operation': 'reference_to_video', 'prompt': 'Synthetic offline source.',
              'first_frame': str(root / first['path']), 'duration': 8, 'aspect_ratio': '16:9',
              'resolution': '720p', 'output_path': str(root / 'fixture.mp4'), 'allow_unknown_cost': True,
              'preferred_tool': 'grok_cli_video', 'hosting_provider': 'grok_cli',
              'preferred_provider': 'grok_cli', 'allowed_providers': ['grok_cli'],
              'preferred_provider_gap': 0.1, 'target_operation': 'reference_to_video',
              'task_context': {'description': 'Synthetic planning context'}}
    before = copy.deepcopy(inputs)
    native = request.prepare_grok_native(inputs, {'cli_version': '1.0.34', 'grok_path': '/synthetic/grok'})
    assert inputs == before
    assert native['request']['arguments']['first_frame'] == inputs['first_frame']
    assert native['request']['arguments']['duration'] == 8
    assert not set(('preferred_tool', 'hosting_provider', 'target_operation', 'task_context')) & set(native['request']['arguments'])


@pytest.mark.parametrize('pin', ['pinned_start_frame', 'pinned_initial_frame', 'pinned_first_frame', 'pinned_final_frame'])
def test_local_free_flag_cannot_erase_native_frame_pin(mixed, pin):
    _, root = mixed
    scene = json.loads((root / 'artifacts/scene_plan.json').read_text())
    scene['metadata'] = {'visual_development': {'shot_cards': {'cutaway': {
        pin: {'required': True, 'requirement_id': 'required-cutaway-pin', 'end_state': 'Pinned state'}}}}}
    write(root / 'artifacts/scene_plan.json', scene)
    with pytest.raises(ValueError, match='frame pin|ending-frame'):
        request.source_packet(root, 'cutaway')


@pytest.mark.parametrize('kind', ['reference_assets', 'motion_handoffs'])
def test_local_free_flag_cannot_erase_manifest_reference(mixed, kind):
    _, root = mixed
    write(root / 'artifacts/asset_manifest.json', {'version': '1.0', 'assets': [],
          'metadata': {kind: {'ref': {'scene_id': 'cutaway', 'asset_id': 'ref'}}}})
    with pytest.raises(ValueError, match='reference/handoff'):
        request.source_packet(root, 'cutaway')


def test_local_reference_mode_is_frozen_in_planning(mixed):
    contract, _ = mixed
    prior_contract, prior_plan = contract_digest(contract), execution.approval_plan_digest(contract)
    contract['shots'][1].pop('reference_mode')
    assert contract_digest(contract) != prior_contract
    assert execution.approval_plan_digest(contract) != prior_plan


def test_local_free_compilation_binds_mode_and_global_plan(reference_free_package):
    from lib.shot_contract import ASSET_PREDICATES, file_sha256
    root, inputs, native, profile, compiled, review = reference_free_package
    contract = json.loads((root / 'artifacts/shot_contract.json').read_text())
    contract.pop('reference_mode')
    contract['shots'][0]['reference_mode'] = 'reference_free'
    for name, role in [('start', 'start_frame'), ('end', 'end_frame'), ('payoff', 'payoff_board')]:
        path = root / 'assets' / f'{name}.svg'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"/>')
        sha = file_sha256(path)
        contract['assets'].append({'id': name, 'role': role, 'cast_ids': [],
            'path': str(path.relative_to(root)), 'sha256': sha, 'review': {
                'review_id': 'synthetic-board-review', 'reviewer': 'synthetic fixture author',
                'story_revision': contract['story_revision'], 'subject_sha256': sha, 'status': 'pass',
                'predicates': [{'name': p, 'status': 'pass', 'severity': 'critical',
                                'evidence': 'Synthetic fixture only'} for p in sorted(ASSET_PREDICATES)]}})
    contract['payoff_asset_id'] = 'payoff'
    boarded = copy.deepcopy(contract['shots'][0])
    boarded.pop('reference_mode')
    boarded.update(id='boarded', asset_ids=['start', 'end', 'payoff'])
    contract['shots'].append(boarded)
    refresh(contract)
    write(root / 'artifacts/shot_contract.json', contract)
    # The existing native prompt is unchanged; only the actual source binding changes.
    with pytest.raises(ValueError, match='stale source'):
        request.validate_preparation(inputs, native, profile)
    compiled = request.prepare_compiled_request(inputs, native, profile,
        coverage=compiled['coverage'], timing=compiled['timing'])
    review['subject_sha256'] = request.digest(compiled)
    write(root / 'artifacts/compiled_request-c1.json', compiled)
    write(root / 'artifacts/preparation_review-r1.json', review)
    assert request.validate_preparation(inputs, native, profile)
    compiled['source_binding'].pop('reference_mode')
    review['subject_sha256'] = request.digest(compiled)
    write(root / 'artifacts/compiled_request-c1.json', compiled)
    write(root / 'artifacts/preparation_review-r1.json', review)
    with pytest.raises(ValueError, match='stale source'):
        request.validate_preparation(inputs, native, profile)
