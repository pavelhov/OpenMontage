"""Offline actual-observation shapes and narrowly ordered payoff preparation."""
import copy
import json

import pytest

from lib import production_execution as execution, production_images as images, production_boards as boards
from lib import production_request as request
from lib.shot_contract import file_sha256, PROJECT_PREDICATES, SHOT_PREDICATES, project_review_subject, shot_review_subject
from tests.lib.test_production_images import board_package, reserve, host_receipt, PNG  # noqa: F401
from tests.lib.test_production_request import neutral_package  # noqa: F401
from tests.lib.test_production_package_authoring import packages
from tests.lib.test_production_factory import correction_factory_fixture, retain_factory_board, file_bytes, write
from tests.integration.test_first_pass_workflow import attestation


def observation(root, envelope, **kwargs):
    pointer = host_receipt(root, envelope, **kwargs)
    return json.loads((root / pointer['path']).read_text()), pointer


@pytest.mark.parametrize('inline', [False, True])
def test_receipt_action_pointer_inline_and_exact_replay(board_package, inline):
    root, _, _, target = board_package
    call = reserve(board_package, call_id='observed')
    envelope = images.begin_native_image(root, call['call_id'])
    action = execution.next_production_action(root)
    observed, pointer = observation(root, envelope)
    receipt = observed if inline else pointer
    result = execution.complete_action(root, action, outcome={'host_receipt': receipt})
    assert result['action']['kind'] != 'begin_native_image' and 'host_envelope' not in result
    imported = images.image_call_status(root, call['call_id'])
    assert imported['state'] == 'imported' and (root / target).read_bytes() == PNG
    retained = root / imported['host_receipt']['path']
    before = file_bytes(root); usage = images.image_usage(root)
    replay = execution.complete_action(root, action, outcome={'host_receipt': observed})
    assert replay['replayed'] and replay['provider_calls'] == 0 and 'host_envelope' not in replay
    assert file_bytes(root) == before and images.image_usage(root) == usage
    assert retained.read_bytes() == before[retained.relative_to(root)]
    assert imported['provenance']['receipt_sha256'] == file_sha256(retained)
    if not inline:
        # Switching to inline replay never normalizes an existing pointer receipt.
        assert retained.read_bytes() == (root / pointer['path']).read_bytes()


@pytest.mark.parametrize('damage', ['tool', 'arguments', 'missing_arguments', 'wrapper', 'mixed', 'changed_replay', 'stale_action'])
def test_inline_receipt_rejects_without_retention(board_package, damage):
    root, _, _, _ = board_package
    call = reserve(board_package, call_id='observed')
    envelope = images.begin_native_image(root, call['call_id'])
    action = execution.next_production_action(root)
    observed, _ = observation(root, envelope)
    if damage == 'tool': observed['tool_name'] = 'another.host'
    elif damage == 'arguments': observed['arguments']['prompt'] = 'Different observed invocation'
    elif damage == 'missing_arguments': del observed['arguments']
    elif damage == 'wrapper': observed = observed['result']
    elif damage == 'mixed': observed['path'] = 'receipts/unrelated.json'
    elif damage == 'changed_replay':
        execution.complete_action(root, action, outcome={'host_receipt': observed})
        observed['result']['output_hint'] = 'Changed actual return'
    elif damage == 'stale_action': action['binding']['request_sha256'] = '0'*64
    before = file_bytes(root); usage = images.image_usage(root)
    with pytest.raises(ValueError):
        execution.complete_action(root, action, outcome={'host_receipt': observed})
    assert file_bytes(root) == before and images.image_usage(root) == usage


def test_inline_crash_after_receipt_retention_restores_original(board_package, monkeypatch):
    root, _, _, target = board_package
    call = reserve(board_package, call_id='interrupted')
    envelope = images.begin_native_image(root, call['call_id'])
    action = execution.next_production_action(root)
    observed, _ = observation(root, envelope)
    before_usage = images.image_usage(root)
    with monkeypatch.context() as patch:
        def interrupt(*args, **kwargs):
            raise RuntimeError('SYNTHETIC crash after receipt bytes, before journal write')
        patch.setattr(images, '_write', interrupt)
        with pytest.raises(RuntimeError):
            execution.complete_action(root, action, outcome={'host_receipt': observed})
    retained = root / 'production_images/interrupted/host-return.json'
    frozen = retained.read_bytes()
    assert images.image_call_status(root, call['call_id'])['state'] == 'submitted'
    assert images.image_usage(root) == before_usage
    resumed = execution.complete_action(root, action, outcome={'host_receipt': observed})
    assert retained.read_bytes() == frozen and (root / target).read_bytes() == PNG
    assert 'host_envelope' not in resumed and images.image_usage(root)['total'] == 1
    assert len([e for e in images.image_call_status(root, call['call_id'])['events'] if e['kind'] == 'tool_return']) == 1


@pytest.mark.parametrize('original_inline', [False, True])
def test_inline_unknown_return_and_additive_supplement(board_package, original_inline):
    root, _, _, target = board_package
    call = reserve(board_package, call_id='supplement')
    envelope = images.begin_native_image(root, call['call_id'])
    action = execution.next_production_action(root)
    partial, partial_pointer = observation(root, envelope, name='partial', result={'output_hint': 'Synthetic fixture'})
    with pytest.raises(ValueError, match='unresolved'):
        execution.complete_action(root, action, outcome={'host_receipt': partial if original_inline else partial_pointer})
    original = images.image_call_status(root, call['call_id'])
    original_raw = (root / original['host_receipt']['path']).read_bytes()
    complete, _ = observation(root, envelope, name='complete')
    receive = execution.next_production_action(root)
    outcome = {'host_receipt': complete, 'supplemental_to': original['host_receipt']['sha256']}
    result = execution.complete_action(root, receive, outcome=outcome)
    imported = images.image_call_status(root, call['call_id'])
    assert imported['host_receipt'] == original['host_receipt'] and imported['provenance'] == original['provenance']
    assert (root / original['host_receipt']['path']).read_bytes() == original_raw
    assert (root / target).read_bytes() == PNG and images.image_usage(root)['total'] == 1
    assert execution.complete_action(root, receive, outcome=outcome)['replayed']
    assert 'host_envelope' not in result


def payoff_fixture(package_fixture, *, ancestor=False):
    root, base, bind = package_fixture
    _, full = packages(base)
    def rename(value):
        if isinstance(value, dict):
            return {k: rename(v) for k, v in value.items()}
        if isinstance(value, list):
            return [rename(v) for v in value]
        return {'s1-start': 'start', 's2-start': 'payoff'}.get(value, value) if isinstance(value, str) else value
    full = rename(full)
    for node in full['boards']['image_nodes']:
        node['required_predicates'].append('cast_count')
    # This independent unrelated slot deliberately remains unmaterialized.
    unrelated = copy.deepcopy(full['boards']['image_nodes'][0])
    unrelated.update(slot_id='unrelated', output_path='assets/images/candidates/unrelated.png')
    full['boards']['image_nodes'].insert(0, unrelated)
    if ancestor:
        parent = copy.deepcopy(unrelated)
        parent.update(slot_id='payoff-ancestor', shot_id='s2', role='identity',
                      output_path='assets/images/candidates/payoff-ancestor.png')
        full['boards']['image_nodes'].append(parent)
        next(n for n in full['boards']['image_nodes'] if n['slot_id'] == 'payoff')['image_dependencies'] = ['payoff-ancestor']
    correction_factory_fixture((root, full, bind))
    retain_factory_board(root, 'start')
    return root


def test_ready_first_shot_defers_project_review_for_exact_missing_payoff(neutral_package):
    root = payoff_fixture(neutral_package)
    before = file_bytes(root)
    action = execution.next_production_action(root)
    assert (action['kind'], action['subject']) == ('prepare_board_image', 'payoff')
    assert file_bytes(root) == before
    contract = execution.load_shot_contract(root)
    assert contract['project_review']['status'] == 'unknown'
    retain_factory_board(root, 'payoff')
    assert execution.next_production_action(root)['kind'] == 'review_planning'


def test_payoff_prepares_only_existing_ancestor_then_payoff(neutral_package):
    root = payoff_fixture(neutral_package, ancestor=True)
    before = file_bytes(root)
    action = execution.next_production_action(root)
    assert (action['kind'], action['subject']) == ('prepare_board_image', 'payoff-ancestor')
    assert file_bytes(root) == before
    retain_factory_board(root, 'payoff-ancestor')
    action = execution.next_production_action(root)
    assert (action['kind'], action['subject']) == ('prepare_board_image', 'payoff')


def test_payoff_deferral_collects_pending_original_without_reemission(neutral_package):
    root = payoff_fixture(neutral_package)
    retain_factory_board(root, 'payoff', predicates={'cast_identity': 'fail'})
    prepared = boards.prepare_image_request(root, 'unrelated', images=images)
    scope = images.derive_native_image_scope(root, prepared['packet'], 'unrelated', prepared['arguments'], output_path=prepared['output_path'])
    call = boards.reserve_board_image(root, 'unrelated', scope_id=scope, call_id='existing-original', images=images)
    images.begin_native_image(root, call['call_id'])
    before = file_bytes(root); usage = images.image_usage(root)
    result = execution.advance(root)
    assert (result['action']['kind'], result['action']['subject']) == ('receive_native_image', 'existing-original')
    assert 'host_envelope' not in result and file_bytes(root) == before and images.image_usage(root) == usage


@pytest.mark.parametrize('shot_review_current', [False, True])
def test_current_project_review_has_no_global_board_gate(neutral_package, shot_review_current):
    root = payoff_fixture(neutral_package)
    contract = execution.load_shot_contract(root)
    contract['project_review'] = attestation(project_review_subject(contract), PROJECT_PREDICATES, revision='r1')
    if shot_review_current:
        contract['shots'][0]['review'] = attestation(shot_review_subject(contract, 's1'), SHOT_PREDICATES, revision='r1')
    write(root, 'artifacts/shot_contract.json', contract)
    before = file_bytes(root)
    action = execution.next_production_action(root)
    assert (action['kind'], action['subject']) == ('prepare_motion' if shot_review_current else 'review_planning', 's1')
    assert file_bytes(root) == before
    assert 'payoff' not in boards.load_board_pack(root)['board_bindings']
    assert 'unrelated' not in boards.load_board_pack(root)['board_bindings']


@pytest.mark.parametrize('severity', ['critical', 'cosmetic'])
def test_failed_project_review_not_deferred_or_changed(neutral_package, severity):
    root = payoff_fixture(neutral_package)
    contract = execution.load_shot_contract(root)
    contract['project_review']['status'] = 'fail'
    contract['project_review']['predicates'][0]['status'] = 'fail'
    contract['project_review']['predicates'][0]['severity'] = severity
    write(root, 'artifacts/shot_contract.json', contract)
    before = file_bytes(root)
    action = execution.next_production_action(root)
    assert (action['kind'], action['subject']) == ('review_planning', 's1')
    assert file_bytes(root) == before


def test_planning_reuses_exact_current_project_verdict(neutral_package):
    root = payoff_fixture(neutral_package)
    contract = execution.load_shot_contract(root)
    retained = attestation(project_review_subject(contract), PROJECT_PREDICATES, revision='r1')
    retained['review_id'] = 'SYNTHETIC-retained-project-observation'
    contract['project_review'] = retained
    write(root, 'artifacts/shot_contract.json', contract)
    action = execution.next_production_action(root)
    assert action['kind'] == 'review_planning'
    target = attestation(shot_review_subject(contract, 's1'), SHOT_PREDICATES, revision='r1')
    result = execution.complete_action(root, action, outcome={'shot_review': target})
    assert result['action']['kind'] == 'prepare_motion' and 'host_envelope' not in result
    assert execution.load_shot_contract(root)['project_review'] == retained
    assert images.image_usage(root)['total'] == 1


@pytest.mark.parametrize('damage', ['stale', 'revision', 'unknown', 'failed', 'missing_predicate', 'unknown_predicate'])
def test_planning_omission_requires_actual_current_passing_project(neutral_package, damage):
    root = payoff_fixture(neutral_package)
    retain_factory_board(root, 'payoff')  # No unavailable-payoff ordering ambiguity.
    contract = execution.load_shot_contract(root)
    retained = attestation(project_review_subject(contract), PROJECT_PREDICATES, revision='r1')
    if damage == 'stale': retained['subject_sha256'] = '0'*64
    elif damage == 'revision': retained['story_revision'] = 'other-revision'
    elif damage == 'unknown': retained['status'] = 'unknown'
    elif damage == 'failed': retained['status'] = 'fail'
    elif damage == 'missing_predicate': retained['predicates'].pop()
    elif damage == 'unknown_predicate': retained['predicates'][0]['status'] = 'unknown'
    contract['project_review'] = retained
    write(root, 'artifacts/shot_contract.json', contract)
    action = execution.next_production_action(root)
    assert action['kind'] == 'review_planning'
    target = attestation(shot_review_subject(contract, 's1'), SHOT_PREDICATES, revision='r1')
    before = file_bytes(root)
    with pytest.raises(ValueError):
        execution.complete_action(root, action, outcome={'shot_review': target})
    assert file_bytes(root) == before


def pending_board(root, slot, call_id):
    prepared = boards.prepare_image_request(root, slot, images=images)
    scope = images.derive_native_image_scope(root, prepared['packet'], slot, prepared['arguments'], output_path=prepared['output_path'])
    call = boards.reserve_board_image(root, slot, scope_id=scope, call_id=call_id, images=images)
    return images.begin_native_image(root, call['call_id'])


def test_second_pending_payoff_keeps_first_original_collection_priority(neutral_package):
    root = payoff_fixture(neutral_package)
    first = pending_board(root, 'unrelated', 'first-original')
    second = pending_board(root, 'payoff', 'payoff-original')
    before = file_bytes(root); usage = images.image_usage(root)
    action = execution.next_production_action(root)
    assert (action['kind'], action['subject']) == ('receive_native_image', 'first-original')
    assert execution.advance(root)['action'] == action
    assert file_bytes(root) == before and images.image_usage(root) == usage
    observed, _ = observation(root, first, name='first-original')
    result = execution.complete_action(root, action, outcome={'host_receipt': observed})
    assert 'host_envelope' not in result
    next_action = execution.next_production_action(root)
    assert (next_action['kind'], next_action['subject']) == ('receive_native_image', 'payoff-original')
    assert images._call(images._load(root), 'payoff-original')['submitted_arguments'] == second['arguments']


def test_scene_review_only_does_not_prioritize_missing_payoff(neutral_package):
    root = payoff_fixture(neutral_package)
    contract = execution.load_shot_contract(root)
    contract['project_review'] = attestation(project_review_subject(contract), PROJECT_PREDICATES, revision='r1')
    write(root, 'artifacts/shot_contract.json', contract)
    action = execution.next_production_action(root)
    assert (action['kind'], action['subject']) == ('review_planning', 's1')


def test_payoff_missing_current_observation_requests_existing_board_review(neutral_package):
    root = payoff_fixture(neutral_package)
    envelope = pending_board(root, 'payoff', 'payoff-original')
    observed, _ = observation(root, envelope, name='payoff-original')
    images.import_native_image(root, 'payoff-original', host_receipt=observed)
    boards.bind_imported_board(root, 'payoff', 'payoff-original', images=images)
    before = file_bytes(root)
    action = execution.next_production_action(root)
    assert (action['kind'], action['subject']) == ('review_board', 'payoff')
    assert file_bytes(root) == before


def test_failed_payoff_verdict_remains_failed(neutral_package):
    root = payoff_fixture(neutral_package)
    _, failed = retain_factory_board(root, 'payoff', predicates={'cast_identity': 'fail'})
    before = file_bytes(root); usage = images.image_usage(root)
    action = execution.next_production_action(root)
    assert (action['kind'], action['subject']) == ('prepare_board_correction', 'payoff')
    assert action['inputs']['failed_predicates'] == ['cast_identity']
    assert action['binding']['correction_observation']['review'] == failed
    assert boards._observation(boards.load_board_pack(root), 'payoff', 'payoff_board')['review'] == failed
    assert boards._review_blockers(boards.load_board_pack(root), 'payoff', 'payoff_board')
    with pytest.raises(execution.ProductionGovernanceError, match='board correction requires exact producer changes'):
        execution.complete_action(root, action, outcome={})
    assert images.image_usage(root) == usage
    assert file_bytes(root) == before


def test_inline_output_collision_preserves_original_receipt_and_media(board_package):
    root, _, _, target = board_package
    call = reserve(board_package, call_id='collision')
    envelope = images.begin_native_image(root, call['call_id'])
    observed, _ = observation(root, envelope)
    output = root / target; output.write_bytes(b'SYNTHETIC occupied output bytes')
    with pytest.raises(ValueError, match='overwrite different retained bytes'):
        images.import_native_image(root, call['call_id'], host_receipt=observed)
    current = images._call(images._load(root), call['call_id'])
    assert current['state'] == 'returned' and current['host_receipt']['sha256'] == request.digest(observed)
    assert output.read_bytes() == b'SYNTHETIC occupied output bytes'
    assert images.image_usage(root)['total'] == 1
    assert json.loads((root / current['host_receipt']['path']).read_text()) == observed


@pytest.mark.parametrize('original, changed', [(True, 1), (1, 1.0)])
def test_inline_replay_rejects_changed_json_value_types(board_package, original, changed):
    root, _, _, _ = board_package
    call = reserve(board_package, call_id='strict-types')
    envelope = images.begin_native_image(root, call['call_id'])
    observed, _ = observation(root, envelope)
    observed['result']['observed_marker'] = original
    action = execution.next_production_action(root)
    execution.complete_action(root, action, outcome={'host_receipt': observed})
    before = file_bytes(root)
    observed['result']['observed_marker'] = changed
    with pytest.raises(ValueError, match='different actual return'):
        execution.complete_action(root, action, outcome={'host_receipt': observed})
    assert file_bytes(root) == before


def test_inline_rejects_numeric_boolean_argument_substitution(board_package):
    root, _, _, _ = board_package
    call = reserve(board_package, call_id='strict-arguments')
    envelope = images.begin_native_image(root, call['call_id'])
    observed, _ = observation(root, envelope)
    observed['arguments']['transparent_background'] = 0
    before = file_bytes(root)
    with pytest.raises(ValueError, match='exact submitted arguments differ'):
        execution.complete_action(root, execution.next_production_action(root), outcome={'host_receipt': observed})
    assert file_bytes(root) == before


@pytest.mark.parametrize('original, changed', [(True, 1), (1, 1.0), (None, 'removed')])
def test_inline_supplement_preserves_exact_retained_json_fields(board_package, original, changed):
    root, _, _, _ = board_package
    call = reserve(board_package, call_id='strict-supplement')
    envelope = images.begin_native_image(root, call['call_id'])
    partial, _ = observation(root, envelope, name='partial',
        result={'output_hint': 'Synthetic fixture', 'observed_marker': original})
    with pytest.raises(ValueError, match='unresolved'):
        images.import_native_image(root, call['call_id'], host_receipt=partial)
    retained = images.image_call_status(root, call['call_id'])['host_receipt']
    complete, _ = observation(root, envelope, name='complete')
    if changed != 'removed': complete['result']['observed_marker'] = changed
    before = file_bytes(root)
    with pytest.raises(ValueError, match='without changing retained fields'):
        images.import_native_image(root, call['call_id'], host_receipt=complete, supplemental_to=retained['sha256'])
    assert file_bytes(root) == before


def test_stale_project_review_defers_only_for_declared_payoff(neutral_package):
    root = payoff_fixture(neutral_package)
    contract = execution.load_shot_contract(root)
    contract['project_review'] = attestation('0'*64, PROJECT_PREDICATES, revision='r1')
    write(root, 'artifacts/shot_contract.json', contract)
    before = file_bytes(root)
    action = execution.next_production_action(root)
    assert (action['kind'], action['subject']) == ('prepare_board_image', 'payoff')
    assert file_bytes(root) == before


def test_independent_motion_stays_ahead_of_unrelated_pending_image(neutral_package):
    root = payoff_fixture(neutral_package)
    retain_factory_board(root, 'payoff', predicates={'cast_identity': 'fail'})
    pending_board(root, 'unrelated', 'unrelated-original')
    contract = execution.load_shot_contract(root)
    contract['project_review'] = attestation(project_review_subject(contract), PROJECT_PREDICATES, revision='r1')
    contract['shots'][0]['review'] = attestation(shot_review_subject(contract, 's1'), SHOT_PREDICATES, revision='r1')
    write(root, 'artifacts/shot_contract.json', contract)
    before = file_bytes(root); usage = images.image_usage(root)
    action = execution.next_production_action(root)
    assert (action['kind'], action['subject']) == ('prepare_motion', 's1')
    assert file_bytes(root) == before and images.image_usage(root) == usage
