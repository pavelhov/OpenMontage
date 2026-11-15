"""Future opt-in start-image review/correction through normal factory actions.

All image bytes and creator evidence here are explicitly synthetic offline fixtures.
"""
import base64
import copy
import io
import json

import pytest
from PIL import Image

from lib import production_boards as boards, production_execution as execution, production_images as images
from tests.lib.test_production_factory import board_factory_fixture
from tests.lib.test_production_request import neutral_package  # noqa: F401
from tests.lib.test_production_images import host_receipt
from tests.integration.test_first_pass_workflow import attestation


def factory(neutral_package, *, allowance=4, opt_in=True):
    root, package, bind = neutral_package
    package = copy.deepcopy(package)
    package['shots'] = package['shots'][:1]
    spec = package['boards']
    spec['shots'] = spec['shots'][:1]
    spec['image_nodes'] = [n for n in spec['image_nodes'] if n['slot_id'] != 'other']
    for node in spec['image_nodes']:
        node['shot_id'] = 's1'
    spec['motion_binding']['contract_template']['shots'] = spec['motion_binding']['contract_template']['shots'][:1]
    spec['motion_binding']['contract_template']['assets'] = [a for a in spec['motion_binding']['contract_template']['assets'] if a['id'] != 'other']
    return board_factory_fixture((root, package, bind), image_allowance=allowance, image_start_review=opt_in)


def receipt(root, step, *, size=(9, 16)):
    output = io.BytesIO()
    Image.new('RGB', size, 'navy').save(output, format='PNG')
    return host_receipt(root, step['host_envelope'], name=step['host_envelope']['call_id'],
        result={'image_url': 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode(),
                'output_hint': 'SYNTHETIC offline test image; no real cast/prop review'})


def to_start_review(root, *, size=(9, 16)):
    step = execution.advance(root)
    for _ in range(8):
        if step['action']['kind'] == 'prepare_motion':
            return step
        assert step['action']['kind'] == 'receive_native_image', step
        step = execution.complete_action(root, step['action'], outcome={'host_receipt': receipt(root, step, size=size)})
    pytest.fail('factory did not reach starting-image review')


def review_outcome(root, *, failed=False, suffix='one', shot_id='s1', asset_id='start',
                   failed_predicate='prop_ownership_count'):
    pack = boards.load_board_pack(root)
    row = next(r for r in boards.starting_image_reviews(root, shot_id) if r['asset_id'] == asset_id)
    names = row['required_predicates']
    review = attestation(row['sha256'], names, revision='r1')
    review.update(review_id='synthetic-start-review-' + suffix, reviewer=pack['specification']['reviewer'])
    for predicate in review['predicates']:
        predicate.update(severity='critical', evidence='SYNTHETIC offline authored current-byte observation: ' + predicate['name'])
        if failed and predicate['name'] == failed_predicate:
            predicate['status'] = 'fail'
    review['status'] = 'fail' if failed else 'pass'
    return {'asset_id': asset_id, 'roles': ['start_frame'], 'review': review}


def test_normal_factory_requires_real_named_review_before_dependent_images(neutral_package):
    root = factory(neutral_package)
    step = to_start_review(root)
    row = step['action']['inputs']['start_image_reviews'][0]
    assert row['asset_id'] == 'start' and row['dimensions'] == [9, 16]
    assert row['blockers'] and row['status'] == 'unknown'
    assert 'end' not in boards.load_board_pack(root)['board_bindings']
    before = images.image_usage(root)['total']
    with pytest.raises(ValueError, match='starting image|actual passing|producer must view'):
        execution.complete_action(root, step['action'], outcome={'preparation': {}})
    assert images.image_usage(root)['total'] == before
    result = execution.complete_action(root, step['action'], outcome={'board_reviews': [review_outcome(root)]})
    assert result['action']['kind'] == 'receive_native_image', result
    assert images.image_call_status(root, result['host_envelope']['call_id'])['slot_id'] == 'end'
    result = execution.complete_action(root, result['action'], outcome={'host_receipt': receipt(root, result)})
    assert result['action']['kind'] == 'prepare_motion'
    assert not result['action']['inputs']['start_image_reviews'][0]['blockers']
    assert execution._attempts(root) == []


def test_failed_start_correction_keeps_originals_and_reuses_normal_once_only_receipts(neutral_package):
    root = factory(neutral_package)
    step = to_start_review(root)
    old = copy.deepcopy(boards.load_board_pack(root)['board_bindings']['start'])
    original_bytes = (root / old['path']).read_bytes()
    old_call = copy.deepcopy(images.image_call_status(root, old['call_id']))
    result = execution.complete_action(root, step['action'], outcome={
        'board_reviews': [review_outcome(root, failed=True)],
        'image_correction': {'asset_id': 'start', 'prompt': 'SYNTHETIC correct one prop owner; preserve current beat, portrait and both cast'}})
    assert result['action']['kind'] == 'receive_native_image', result
    new_call = result['host_envelope']['call_id']
    assert new_call != old['call_id']
    pack = boards.load_board_pack(root)
    assert pack['board_bindings']['start'] == old
    assert pack['image_packet_history'][-1]['request_sha256'] == old_call['request_sha256']
    assert images.image_usage(root)['total'] == 3  # payoff + failed start + corrected start
    resumed = execution.advance(root)
    assert resumed['action']['subject'] == new_call and 'host_envelope' not in resumed
    with pytest.raises(ValueError, match='already emitted'):
        images.begin_native_image(root, new_call)
    result = execution.complete_action(root, result['action'], outcome={'host_receipt': receipt(root, result)})
    assert result['action']['kind'] == 'prepare_motion'
    new = boards.load_board_pack(root)['board_bindings']['start']
    assert new['call_id'] == new_call and new['path'] != old['path']
    assert (root / old['path']).read_bytes() == original_bytes
    assert images.image_call_status(root, old['call_id']) == old_call
    # Equal synthetic bytes still need a new judgment of the new original path.
    assert result['action']['inputs']['start_image_reviews'][0]['blockers']
    with pytest.raises(ValueError, match='current prepared board request'):
        boards.bind_imported_board(root, 'start', old['call_id'], images=images)
    assert boards.load_board_pack(root)['board_bindings']['start'] == new
    result = execution.complete_action(root, result['action'], outcome={'board_reviews': [review_outcome(root, suffix='two')]})
    assert images.image_call_status(root, result['host_envelope']['call_id'])['slot_id'] == 'end'
    assert images.image_usage(root)['total'] == 4
    assert execution._attempts(root) == []


def test_spare_image_allowance_exhaustion_blocks_correction_without_mutation(neutral_package):
    root = factory(neutral_package, allowance=3)
    step = to_start_review(root)
    result = execution.complete_action(root, step['action'], outcome={'board_reviews': [review_outcome(root, failed=True)]})
    assert result['action']['kind'] == 'compose_cut'
    assert any('allowance exhausted' in reason for reason in result['action']['inputs']['blocked_shots']['s1'])
    before = (root / boards.ARTIFACT).read_bytes()
    with pytest.raises(ValueError, match='episode image allowance exhausted'):
        boards.prepare_start_image_correction(root, 'start', 'SYNTHETIC fix', images=images)
    assert (root / boards.ARTIFACT).read_bytes() == before
    assert images.image_usage(root)['total'] == 2
    assert execution._attempts(root) == []


@pytest.mark.parametrize('change', ['wrong_aspect', 'mechanical', 'missing_check', 'wrong_hash'])
def test_current_image_requirements_reject_bad_evidence(neutral_package, change):
    root = factory(neutral_package)
    step = to_start_review(root, size=(16, 9) if change == 'wrong_aspect' else (9, 16))
    row = review_outcome(root)
    if change == 'missing_check':
        row['review']['predicates'] = [p for p in row['review']['predicates'] if p['name'] != 'no_future_beat_state']
    elif change == 'wrong_hash':
        row['review']['subject_sha256'] = '0' * 64
    if change == 'wrong_aspect':
        execution.complete_action(root, step['action'], outcome={'board_reviews': [row]})
        assert any('9:16 pixel dimensions' in reason for reason in boards.starting_image_blockers(root, 's1'))
    elif change == 'mechanical':
        assert boards.starting_image_blockers(root, 's1')
    elif change == 'wrong_hash':
        with pytest.raises(ValueError, match='stale bytes'):
            execution.complete_action(root, step['action'], outcome={'board_reviews': [row]})
    else:
        execution.complete_action(root, step['action'], outcome={'board_reviews': [row]})
        assert boards.starting_image_blockers(root, 's1')
    assert execution._attempts(root) == []


def test_legacy_factory_retains_report_only_review_and_no_correction_authority(neutral_package):
    root = factory(neutral_package, opt_in=False)
    step = to_start_review(root)
    assert 'start_image_reviews' not in step['action']['inputs']
    assert boards.starting_image_reviews(root, 's1') == []
    with pytest.raises(ValueError, match='future run authority'):
        boards.prepare_start_image_correction(root, 'start', 'SYNTHETIC fix', images=images)


@pytest.mark.parametrize('tamper', ['prompt', 'review', 'original_bytes'])
def test_reserved_correction_rechecks_current_proof_before_emitting_envelope(neutral_package, tamper):
    root = factory(neutral_package)
    step = to_start_review(root)
    execution.complete_action(root, step['action'], outcome={'board_reviews': [review_outcome(root, failed=True)]})
    boards.prepare_start_image_correction(root, 'start', 'SYNTHETIC corrected prompt', images=images)
    reserved = execution.advance(root, max_steps=1)
    assert reserved['action']['kind'] == 'begin_native_image'
    assert 'host_envelope' not in reserved
    pack = boards.load_board_pack(root)
    if tamper == 'prompt':
        pack['image_packets']['start']['arguments']['prompt'] = 'SYNTHETIC modified after admission'
    elif tamper == 'review':
        next(o for o in pack['observations'] if o['origin'] == 'designated_reviewer')['review']['predicates'][0]['evidence'] = 'SYNTHETIC changed proof'
    else:
        (root / pack['board_bindings']['start']['path']).write_bytes(b'SYNTHETIC changed original bytes')
    if tamper != 'original_bytes':
        (root / boards.ARTIFACT).write_text(json.dumps(pack))
    result = execution.advance(root)
    assert result['status'] == 'blocked' and 'host_envelope' not in result
    assert images.image_usage(root)['total'] == 3
    assert execution._attempts(root) == []


def test_access_failure_of_correction_does_not_create_another_image_or_video(neutral_package):
    root = factory(neutral_package)
    step = to_start_review(root)
    result = execution.complete_action(root, step['action'], outcome={
        'board_reviews': [review_outcome(root, failed=True)],
        'image_correction': {'asset_id': 'start', 'prompt': 'SYNTHETIC correct props'}})
    error = host_receipt(root, result['host_envelope'], name='SYNTHETIC-correction-access-error',
        result={'isError': True, 'error': 'HTTP 403 access forbidden'})
    result = execution.complete_action(root, result['action'], outcome={'host_receipt': error})
    assert result['action']['kind'] == 'compose_cut'
    assert images.image_usage(root)['total'] == 3
    again = execution.advance(root)
    assert again['action']['kind'] == 'compose_cut' and 'host_envelope' not in again
    assert images.image_usage(root)['total'] == 3
    assert execution._attempts(root) == []


def test_direct_motion_preflight_cannot_bypass_future_start_review(neutral_package):
    from tests.lib.test_production_execution import MotionTool
    root = factory(neutral_package)
    # An alternate caller can materialize all declared images, but a mechanical
    # import or incomplete semantic review must never authorize direct video.
    for slot in ('payoff', 'start', 'end'):
        packet = boards.prepare_image_request(root, slot, images=images)
        scope_id = images.derive_native_image_scope(root, packet['packet'], slot, packet['arguments'], output_path=packet['output_path'])
        call = boards.reserve_board_image(root, slot, scope_id=scope_id, call_id='SYNTHETIC-direct-' + slot, images=images)
        envelope = images.begin_native_image(root, call['call_id'])
        received = {'host_envelope': envelope}
        images.import_native_image(root, call['call_id'], host_receipt=receipt(root, received))
        boards.bind_imported_board(root, slot, call['call_id'], images=images)
        if slot == 'start':
            # The future rule normally makes end preparation wait. Simulate a
            # valid starting review, then an honest later failed observation.
            good = review_outcome(root)
            boards.record_board_review(root, 'start', good['review'], roles=good['roles'], images=images)
    failed = review_outcome(root, failed=True, suffix='late-failure')
    boards.record_board_review(root, 'start', failed['review'], roles=failed['roles'], images=images)
    inputs = {'project_dir': str(root), 'governance': {'scope_id': 'SYNTHETIC-direct', 'shot_id': 's1'},
              'prompt': 'SYNTHETIC direct caller', 'output_path': str(root / 'assets/video/direct.mp4')}
    scopes_path = root / 'production_scopes.json'
    scopes = json.loads(scopes_path.read_text())
    contract = execution.load_shot_contract(root)
    scopes['scopes'].append({'id': 'SYNTHETIC-direct', 'project_id': contract['project_id'], 'story_revision': 'r1',
        'status': 'approved', 'approved_by': 'SYNTHETIC offline fixture', 'phase': 'first_pass', 'provider': 'fake',
        'evidence': boards.load_board_pack(root)['specification']['approval'],
        'requests': {'s1': execution.planned_request_digest(inputs, project_dir=root)},
        'attempts_per_shot': {'s1': 1}, 'planning_mode': 'shot_local_v1',
        'approval_plan_sha256': execution.approval_plan_digest(contract, 's1')})
    scopes_path.write_text(json.dumps(scopes))
    with pytest.raises(ValueError, match='prop_ownership_count|starting image review'):
        execution.preflight(MotionTool(), inputs)
    assert execution._attempts(root) == []


def test_changed_current_command_preserves_collection_without_fresh_authority(neutral_package):
    root = factory(neutral_package)
    step = to_start_review(root)
    result = execution.complete_action(root, step['action'], outcome={
        'board_reviews': [review_outcome(root, failed=True)],
        'image_correction': {'asset_id': 'start', 'prompt': 'SYNTHETIC correct props'}})
    original = result['host_envelope']['call_id']
    from lib import production_autonomy as autonomy
    policy = json.loads((root / autonomy.POLICY_ARTIFACT).read_text())
    command = root / policy['activation_basis']['run_command']['path']
    command.write_text('{}')
    resumed = execution.advance(root)
    assert resumed['action']['subject'] == original and 'host_envelope' not in resumed
    returned = execution.complete_action(root, result['action'], outcome={'host_receipt': receipt(root, result)})
    assert images.image_call_status(root, original)['state'] == 'imported'
    assert returned['provider_calls'] == 0 and 'host_envelope' not in returned
    assert images.image_usage(root)['total'] == 3
    with pytest.raises(ValueError, match='Strict:'):
        boards.prepare_start_image_correction(root, 'start', 'SYNTHETIC another correction', images=images)
    assert execution._attempts(root) == []


@pytest.mark.parametrize('size, accepted', [((941, 1672), True), ((943, 1672), False),
                                           ((1024, 1536), False), ((1672, 941), False)])
def test_start_geometry_accepts_only_one_pixel_rounding(neutral_package, size, accepted):
    root = factory(neutral_package)
    step = to_start_review(root, size=size)
    outcome = review_outcome(root)
    execution.complete_action(root, step['action'], outcome={'board_reviews': [outcome]})
    blockers = boards.starting_image_blockers(root, 's1')
    assert (not any('9:16 pixel dimensions' in reason for reason in blockers)) == accepted


@pytest.mark.parametrize('failed_predicate', ['cast_identity', 'asset_quality', 'unrelated_cosmetic'])
def test_correction_accepts_failed_existing_required_check_only(neutral_package, failed_predicate):
    root = factory(neutral_package)
    step = to_start_review(root)
    outcome = review_outcome(root, failed=True, failed_predicate=failed_predicate)
    if failed_predicate == 'unrelated_cosmetic':
        outcome['review']['predicates'].append({'name': failed_predicate, 'status': 'fail',
            'severity': 'cosmetic', 'evidence': 'SYNTHETIC optional lighting preference'})
    execution.complete_action(root, step['action'], outcome={'board_reviews': [outcome]})
    if failed_predicate == 'unrelated_cosmetic':
        with pytest.raises(ValueError, match='actual named failed'):
            boards.prepare_start_image_correction(root, 'start', 'SYNTHETIC correction', images=images)
    else:
        correction = boards.prepare_start_image_correction(root, 'start', 'SYNTHETIC correction', images=images)
        assert correction['board_correction_intent']['slot_id'] == 'start'
    assert images.image_usage(root)['total'] == 2


def test_fresh_correction_reservation_preserves_missing_first_image_capacity(neutral_package):
    root = board_factory_fixture(neutral_package, image_allowance=5, image_start_review=True)
    for slot in ('payoff', 'start', 'other'):
        packet = boards.prepare_image_request(root, slot, images=images)
        scope = images.derive_native_image_scope(root, packet['packet'], slot, packet['arguments'], output_path=packet['output_path'])
        original = boards.reserve_board_image(root, slot, scope_id=scope, call_id='SYNTHETIC-first-' + slot, images=images)
        envelope = images.begin_native_image(root, original['call_id'])
        images.import_native_image(root, original['call_id'], host_receipt=receipt(root, {'host_envelope': envelope}))
        boards.bind_imported_board(root, slot, original['call_id'], images=images)
    corrections = []
    for slot, shot in [('start', 's1'), ('other', 's3')]:
        outcome = review_outcome(root, failed=True, suffix=slot, shot_id=shot, asset_id=slot)
        boards.record_board_review(root, slot, outcome['review'], roles=outcome['roles'], images=images)
        packet = boards.prepare_start_image_correction(root, slot, 'SYNTHETIC correct ' + slot, images=images)
        scope = images.derive_native_image_scope(root, packet['packet'], slot, packet['arguments'], output_path=packet['output_path'])
        corrections.append((slot, packet, scope))
    assert boards.start_image_correction_allowance(root, images=images)['spare'] == 1
    slot, packet, scope = corrections[0]
    original = boards.reserve_board_image(root, slot, scope_id=scope, call_id='SYNTHETIC-correction-one', images=images)
    assert boards.start_image_correction_allowance(root, images=images)['spare'] == 0
    # Replaying reservation and emitting its original envelope excludes this
    # already-counted call; neither operation needs another spare occurrence.
    assert boards.reserve_board_image(root, slot, scope_id=scope, call_id=original['call_id'], images=images)['call_id'] == original['call_id']
    envelope = images.begin_native_image(root, original['call_id'])
    assert envelope['call_id'] == original['call_id']
    slot, packet, scope = corrections[1]
    with pytest.raises(ValueError, match='episode image allowance exhausted'):
        boards.reserve_board_image(root, slot, scope_id=scope, call_id='SYNTHETIC-correction-two', images=images)
    assert images.image_usage(root)['total'] == 4
    # The retained final occurrence still admits the first end image once the
    # corrected start has been imported and actually reviewed.
    images.import_native_image(root, original['call_id'], host_receipt=receipt(root, {'host_envelope': envelope}))
    boards.bind_imported_board(root, 'start', original['call_id'], images=images)
    outcome = review_outcome(root, suffix='corrected-start')
    boards.record_board_review(root, 'start', outcome['review'], roles=outcome['roles'], images=images)
    packet = boards.prepare_image_request(root, 'end', images=images)
    scope = images.derive_native_image_scope(root, packet['packet'], 'end', packet['arguments'], output_path=packet['output_path'])
    boards.reserve_board_image(root, 'end', scope_id=scope, call_id='SYNTHETIC-final-first-image', images=images)
    assert images.image_usage(root)['total'] == 5
    assert execution._attempts(root) == []
