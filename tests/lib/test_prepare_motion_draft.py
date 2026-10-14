"""Pure prepare_motion draft helper; all media and routes are synthetic/offline."""
import copy
from pathlib import Path

import pytest

from lib import production_autonomy as pa, production_execution as execution
from lib import production_images as images, production_request as request
from lib.shot_contract import file_sha256
from tests.integration.test_first_pass_workflow import attestation
from tests.lib.test_factory_frame_alias import original_frame_run
from tests.lib.test_production_factory import file_bytes
from tests.lib.test_production_request import neutral_package  # noqa: F401

OBSERVATION = {'cli_version': '1.0.34', 'grok_path': '/SYNTHETIC/offline/grok'}


def _current_prepare(root):
    execution.advance(root)
    action = execution.next_production_action(root)
    assert (action['kind'], action['subject']) == ('prepare_motion', 's3'), action
    return action


def _synthetic_preparation(root, draft, record_id, composite_boards=None):
    inputs = draft['inputs']
    contract = execution.load_shot_contract(root)
    shot = next(s for s in contract['shots'] if s['id'] == draft['shot_id'])
    index = contract['shots'].index(shot)
    native = request.prep_builder(draft['provider'])(inputs, OBSERVATION)
    timing = {'method': 'segmented_estimate', 'duration_seconds': 5, 'language': 'en', 'margin_seconds': .5,
        'rationale': 'SYNTHETIC fixture', 'overlap_policy': 'serial', 'overlap_rationale': 'SYNTHETIC fixture',
        'segments': [], 'action_windows': [{'source_pointer': f'/shot_contract/shots/{index}/' + key,
            'value_sha256': request.digest(shot[key]), 'start_seconds': start, 'end_seconds': end,
            'rationale': 'SYNTHETIC fixture'} for key, start, end in
            [('dominant_action', .1, 3), ('completed_end_state', 3.1, 4.4)]]}
    judgment = {'version': '1.0', 'reviewer': 'SYNTHETIC offline reviewer', 'status': 'pass',
        'evidence_kind': 'reviewed', 'predicates': [{'name': name, 'status': 'pass', 'severity': 'critical',
            'evidence': 'SYNTHETIC fixture only'} for name in sorted(request.PREDICATES)]}
    if composite_boards is not None:
        judgment['composite_boards'] = composite_boards
    request.prepare_compiled_request(inputs, native, {'source': 'real'},
        coverage=draft['coverage'], timing=timing, review=judgment, persist=True)
    assert request.validate_preparation(inputs, native, {'source': 'real'})
    assert (root / ('artifacts/compiled_request-' + record_id + '.json')).stat().st_mode & 0o222 == 0
    assert (root / ('artifacts/preparation_review-' + record_id + '.json')).stat().st_mode & 0o222 == 0


def test_draft_feeds_existing_preparation_and_native_admission(neutral_package, monkeypatch):
    root, transport, _ = original_frame_run(neutral_package, monkeypatch)
    monkeypatch.setenv('GROK_SESSIONS_ROOT', str(root / 'sessions'))
    action = _current_prepare(root)
    usage = images.image_usage(root)['total']
    before = file_bytes(root)
    draft = execution.draft_motion_preparation(root, action, provider='grok_cli')
    record_id = 'motion-' + action['action_id'][:24]
    assert draft['inputs']['output_path'] == str(root / ('assets/video/' + record_id + '.mp4'))
    assert draft['inputs']['compiled_request_id'] == draft['inputs']['preparation_review_id'] == record_id
    assert execution.draft_motion_preparation(root, action, provider='grok_cli') == draft
    # Pure: no writes, no image usage, no provider/native call.
    assert file_bytes(root) == before and images.image_usage(root)['total'] == usage
    assert draft['provider_calls'] == 0 and draft['missing'] == [] and len(transport.native_requests) == 1
    frame = execution.resolve_board_input(root, {'asset_id': 'other'})
    assert draft['frames'] == [{'pointer': '/image_path', 'binding': {'$board': {'asset_id': 'other'}}, **frame}]
    assert draft['inputs']['image_path'] == frame['path'] and file_sha256(frame['path']) == frame['sha256']
    assert draft['inputs']['prompt'] == request.compile_provider_prompt(root, 's3', provider='grok_cli')['prompt']
    assert draft['inputs']['duration'] == 5 and draft['inputs']['governance'] == {'shot_id': 's3'}
    # Producer-owned route controls absent from this fixture's retained template.
    inputs = dict(draft['inputs'], operation='reference_to_video', allow_unknown_cost=True)
    draft = {**draft, 'inputs': inputs}
    _synthetic_preparation(root, draft, record_id)
    result = execution.complete_action(root, action, outcome={
        'inputs': inputs, 'provider': draft['provider'], 'observation': OBSERVATION})
    assert result['status'] == 'dispatched' and result['result']['success'], result['result'].get('error')
    assert len(transport.native_requests) == 2
    assert transport.native_requests[-1]['arguments']['prompt'] == inputs['prompt']


def test_draft_refuses_stale_modified_or_foreign_route_without_writes(neutral_package, monkeypatch):
    root, transport, _ = original_frame_run(neutral_package, monkeypatch)
    action = _current_prepare(root)
    before = file_bytes(root)
    modified = copy.deepcopy(action); modified['inputs']['shot_id'] = 's1'
    for bad in (modified, {**action, 'kind': 'review_board'}, None):
        with pytest.raises(execution.ProductionGovernanceError, match='current prepare_motion'):
            execution.draft_motion_preparation(root, bad, provider='grok_cli')
    for provider in (None, 'openart_cli', 'not_a_route'):
        with pytest.raises(execution.ProductionGovernanceError, match='singleton route'):
            execution.draft_motion_preparation(root, action, provider=provider)
    with pytest.raises(Exception):
        execution.draft_motion_preparation(root, action, provider='grok_cli', compiled_request_id='../escape')
    draft = execution.draft_motion_preparation(root, action, provider='grok_cli')
    assert draft['missing'] == []
    explicit = execution.draft_motion_preparation(root, action, provider='grok_cli',
        output_path='assets/video/explicit.mp4', compiled_request_id='explicit-compiled', preparation_review_id='explicit-review')
    assert explicit['inputs']['output_path'] == str(root / 'assets/video/explicit.mp4')
    assert explicit['inputs']['compiled_request_id'] == 'explicit-compiled'
    assert explicit['inputs']['preparation_review_id'] == 'explicit-review'
    assert file_bytes(root) == before and len(transport.native_requests) == 1


def test_draft_refuses_changed_board_bytes_and_preserves_retained_controls(neutral_package, monkeypatch):
    root, transport, _ = original_frame_run(neutral_package, monkeypatch)
    action = _current_prepare(root)
    policy, _, _ = pa.require_active_policy(root, shot_id='s3')
    template = pa.retained_baselines(root, policy)['s3']['planned_request_template']['inputs']
    draft = execution.draft_motion_preparation(root, action, provider='grok_cli')
    assert {k: v for k, v in draft['inputs'].items() if k not in {
        'image_path', 'project_dir', 'governance', 'prompt', 'output_path', 'compiled_request_id', 'preparation_review_id'}} \
        == {k: v for k, v in template.items() if k != 'image_path'}
    path = Path(execution.resolve_board_input(root, {'asset_id': 'other'})['path'])
    path = path if path.is_absolute() else root / path
    path.write_bytes(path.read_bytes() + b'SYNTHETIC tamper')
    before = file_bytes(root)
    with pytest.raises(execution.ProductionGovernanceError):
        execution.draft_motion_preparation(root, action, provider='grok_cli')
    assert file_bytes(root) == before and len(transport.native_requests) == 1


def _admission_ready(neutral_package, monkeypatch):
    root, transport, _ = original_frame_run(neutral_package, monkeypatch)
    monkeypatch.setenv('GROK_SESSIONS_ROOT', str(root / 'sessions'))
    action = _current_prepare(root)
    draft = execution.draft_motion_preparation(root, action, provider='grok_cli',
        output_path='assets/video/neg-s3.mp4', compiled_request_id='neg-s3', preparation_review_id='neg-s3')
    inputs = dict(draft['inputs'], operation='reference_to_video', allow_unknown_cost=True)
    _synthetic_preparation(root, {**draft, 'inputs': inputs}, 'neg-s3')
    return root, transport, action, inputs


def _asset(root):
    expected = next(a for a in pa.retained_baselines(root, pa.require_active_policy(root, shot_id='s3')[0])['s3']
                    ['projection']['assets'] if a['id'] == 'other')
    return {**expected, **execution.resolve_board_input(root, {'asset_id': 'other'})}, expected


def _cast_bearing_frame(neutral_package, monkeypatch):
    """C88 S02 shape: a prepared start_frame board plus a cast identity_reference."""
    from tests.lib import test_factory_frame_alias as frames
    original = frames.prospective_package

    def cast_bearing(package):
        package = original(package)
        spec = package['boards']; reference = spec['references'][0]
        reference['role'] = 'identity_reference'
        reference['applicability']['roles'] = ['identity_reference', 'start_frame', 'end_frame']
        spec['shared_roles'][0]['role'] = 'identity_reference'
        template = spec['motion_binding']['contract_template']
        template['assets'].append({'id': 'cast', 'role': 'identity_reference', 'cast_ids': ['actor']})
        next(s for s in template['shots'] if s['id'] == 's3')['cast_ids'] = ['actor']
        next(a for a in template['assets'] if a['id'] == 'other')['cast_ids'] = ['actor']
        return package
    monkeypatch.setattr(frames, 'prospective_package', cast_bearing)
    root, transport, _ = frames.original_frame_run(neutral_package, monkeypatch)
    monkeypatch.setenv('GROK_SESSIONS_ROOT', str(root / 'sessions'))
    action = _current_prepare(root)
    projection = pa.current_projection(root, 's3')
    cast = next(a for a in projection['assets'] if a['id'] == 'cast')
    draft = execution.draft_motion_preparation(root, action, provider='grok_cli',
        output_path='assets/video/cast-s3.mp4', compiled_request_id='cast-s3', preparation_review_id='cast-s3')
    inputs = dict(draft['inputs'], operation='reference_to_video', allow_unknown_cost=True)
    member = {key: copy.deepcopy(cast[key]) for key in ('sha256', 'role', 'cast_ids')}
    composite = {'sha256': draft['frames'][0]['sha256'], 'role': 'start_frame', 'members': [member]}
    return root, transport, action, {**draft, 'inputs': inputs}, composite


def test_prepared_frame_with_semantic_composite_membership_admits_and_dispatches(neutral_package, monkeypatch):
    root, transport, action, draft, composite = _cast_bearing_frame(neutral_package, monkeypatch)
    _synthetic_preparation(root, draft, 'cast-s3', composite_boards=[composite])
    review = __import__('json').loads((root / 'artifacts/preparation_review-cast-s3.json').read_text())
    assert review['composite_boards'] == [composite]
    result = execution.complete_action(root, action, outcome={
        'inputs': draft['inputs'], 'provider': draft['provider'], 'observation': OBSERVATION})
    assert result['status'] == 'dispatched' and result['result']['success'], result['result'].get('error')
    assert len(transport.native_requests) == 2
    assert transport.native_requests[-1]['arguments']['prompt'] == draft['inputs']['prompt']


@pytest.mark.parametrize('damage', ['unknown_member', 'duplicate_member', 'wrong_carrier', 'wrong_cast'])
def test_prepared_frame_composite_membership_still_fails_closed(neutral_package, monkeypatch, damage):
    root, transport, _, draft, composite = _cast_bearing_frame(neutral_package, monkeypatch)
    member = composite['members'][0]
    if damage == 'unknown_member':
        member['sha256'] = '0' * 64
    elif damage == 'duplicate_member':
        composite['members'].append(copy.deepcopy(member))
    elif damage == 'wrong_carrier':
        composite['sha256'] = member['sha256']
    else:
        member['cast_ids'] = ['someone_else']
    before = file_bytes(root)
    with pytest.raises((pa.AutonomyError, ValueError)):
        _synthetic_preparation(root, draft, 'cast-s3', composite_boards=[composite])
    assert file_bytes(root) == before and len(transport.native_requests) == 1


def _dispatch(root, action, record_id):
    draft = execution.draft_motion_preparation(root, action, provider='grok_cli',
        output_path=f'assets/video/{record_id}.mp4', compiled_request_id=record_id, preparation_review_id=record_id)
    inputs = dict(draft['inputs'], operation='reference_to_video', allow_unknown_cost=True)
    _synthetic_preparation(root, {**draft, 'inputs': inputs}, record_id)
    result = execution.complete_action(root, action, outcome={
        'inputs': inputs, 'provider': draft['provider'], 'observation': OBSERVATION})
    assert result['status'] == 'dispatched' and result['result']['success'], result['result'].get('error')
    return result


def test_failed_observation_never_rerolls(neutral_package, monkeypatch):
    """One-shot: a recorded failed observation is report-only; no successor request follows."""
    root, transport, _ = original_frame_run(neutral_package, monkeypatch)
    monkeypatch.setenv('GROK_SESSIONS_ROOT', str(root / 'sessions'))
    _dispatch(root, _current_prepare(root), 'orig-s3')
    attempt = next(a for a in execution._attempts(root) if a['shot_id'] == 's3')
    output = attempt['output'] if 'output' in attempt else None
    sha = (output or {}).get('sha256') or file_sha256(root / 'assets/video/orig-s3.mp4')
    review = attestation(sha, ['completed_action'], revision='r1')
    review.update(status='fail', predicates=[{'name': 'completed_action', 'status': 'fail',
        'severity': 'critical', 'evidence': 'SYNTHETIC end state not reached'}])
    execution.record_rejection(root, attempt['attempt_id'], review)
    calls = len(transport.native_requests)
    action = execution.next_production_action(root)
    assert action['kind'] != 'prepare_motion' and 'repair' not in str(action.get('binding', {})), action
    assert len(transport.native_requests) == calls
