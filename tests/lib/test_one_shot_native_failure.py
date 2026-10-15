"""Counted native failures close out; unavailable replies never consume twice."""
import copy

import pytest

from lib import production_boards as boards, production_execution as execution, production_images as images
from tests.lib.test_production_request import neutral_package  # noqa: F401
from tests.lib.test_production_factory import board_factory_fixture, file_bytes
from tests.lib.test_production_images import host_receipt, write
from lib.shot_contract import file_sha256
from tests.integration.test_openart_mcp_autonomy import lifecycle  # noqa: F401


def fail_current_image(root):
    state = execution.advance(root)
    assert state['action']['kind'] == 'receive_native_image' and 'host_envelope' in state
    call_id = state['action']['subject']
    receipt = host_receipt(root, state['host_envelope'], name=call_id,
                           result={'isError': True, 'error': 'SYNTHETIC terminal image error'})
    images.import_native_image(root, call_id, host_receipt=receipt)
    return {'action': execution.next_production_action(root)}, call_id


def test_terminal_counted_images_continue_independent_slots_then_compose(neutral_package):
    root = board_factory_fixture(neutral_package)
    state, call_id = fail_current_image(root)
    first = images.image_call_status(root, call_id)
    assert first['state'] == 'failed' and images.image_usage(root)['total'] == 1
    assert state['action']['kind'] == 'prepare_board_image'
    assert state['action']['subject'] != first['slot_id']
    before = file_bytes(root)
    assert execution.next_production_action(root) == state['action']
    assert file_bytes(root) == before
    for _ in range(8):
        action = execution.next_production_action(root)
        if action['kind'] == 'compose_cut':
            break
        _, other = fail_current_image(root)
        assert other != call_id
    else:
        pytest.fail('terminal originals prevented incomplete composition')
    usage = images.image_usage(root)
    assert usage['total'] == 3  # dependent end never becomes ready after its start fails
    assert len({row['slot_id'] for row in usage['occurrences']}) == usage['total']
    assert images.image_call_status(root, call_id) == first
    assert not execution._attempts(root)


def test_native_unavailable_reply_defers_and_exact_late_receive_stays_valid(neutral_package):
    root = board_factory_fixture(neutral_package)
    state = execution.advance(root)
    original = copy.deepcopy(state['host_envelope'])
    call_id = state['action']['subject']
    exception = root / 'notes/native-exception.json'
    write(exception, {'call_id': call_id, 'tool_name': original['tool_name'],
        'arguments': original['arguments'], 'exception': {'message': 'SYNTHETIC lost host reply'}})
    images.record_native_image_host_exception(root, call_id, host_exception={
        'path': str(exception), 'sha256': file_sha256(exception)})
    assert execution.next_production_action(root)['kind'] == 'prepare_board_image'
    saved = images.image_call_status(root, call_id)
    deferred = execution._recoverable_native_original_action(root, call_id)
    assert deferred['inputs']['action'] == 'blocked_original_host_reply_unavailable'
    assert deferred['inputs']['counted_original'] is True
    assert deferred['inputs']['slot_id'] == saved['slot_id']
    for _ in range(8):
        action = execution.next_production_action(root)
        if action['kind'] == 'compose_cut':
            break
        fail_current_image(root)
    else:
        pytest.fail('unavailable reply prevented incomplete composition')
    assert action['inputs']['recoverable_originals'] == [deferred]
    assert images.image_call_status(root, call_id) == saved
    count = images.image_usage(root)['total']
    receipt = host_receipt(root, original, name='late-original')
    forged = copy.deepcopy(deferred)
    forged['binding']['request_sha256'] = '0' * 64
    forged['action_id'] = execution._digest({k: v for k, v in forged.items() if k != 'action_id'})
    with pytest.raises(ValueError, match='stale, foreign or modified'):
        execution.complete_action(root, forged, outcome={'host_receipt': receipt})
    step = execution.complete_action(root, deferred, outcome={'host_receipt': receipt})
    assert images.image_call_status(root, call_id)['state'] == 'imported'
    assert images.image_usage(root)['total'] == count
    assert step['action']['kind'] != 'begin_native_image'
    assert boards.load_board_pack(root)['board_bindings'][saved['slot_id']]['call_id'] == call_id
    replay = execution.complete_action(root, deferred, outcome={'host_receipt': receipt})
    assert replay['replayed'] and images.image_usage(root)['total'] == count


@pytest.mark.parametrize('provider,state,expected', [
    ('grok_cli', 'uncertain', 'collect_grok_original'),
    ('grok_cli', 'submitted', 'collect_grok_original'),
    ('openart_mcp', 'submitted', 'poll_openart_original'),
])
def test_partial_before_uncertain_cli_collection_keeps_known_job_priority(
        lifecycle, monkeypatch, provider, state, expected):
    """Resolve known originals before assembly, including uncertain CLI returns."""
    from lib import production_draft as draft, openart_mcp_jobs as jobs
    root, _, _, _, _, _, _ = lifecycle
    shot_id = execution.load_shot_contract(root)['shots'][0]['id']
    original = {'attempt_id': 'SYNTHETIC-pending', 'shot_id': shot_id, 'media_kind': 'motion',
        'request_sha256': 'a' * 64, 'provider': provider, 'scope': {'provider': provider}}
    monkeypatch.setattr(execution, '_attempts', lambda _: [original])
    monkeypatch.setattr(execution, '_state', lambda *args: {'status': state})
    monkeypatch.setattr(jobs, 'resume_original', lambda *args: {'request_sha256': 'a' * 64,
        'next_action': {'action': 'poll_original', 'attempt_id': original['attempt_id']}})
    monkeypatch.setattr(draft, 'first_cut_candidates', lambda _: [
        {'shot_id': 'SYNTHETIC-playable-sibling', 'status': 'candidate'}])
    monkeypatch.setattr(draft, 'latest_first_cut', lambda _: None)
    before = file_bytes(root)
    action = execution.next_production_action(root)
    assert action['kind'] == expected
    assert file_bytes(root) == before
    if provider == 'grok_cli':
        assert action['subject'] == original['attempt_id']
        assert action['binding']['counted_original'] is True
        # A current partial does not change the same-original recovery priority.
        monkeypatch.setattr(draft, 'latest_first_cut', lambda _: {'status': 'partial'})
        monkeypatch.setattr(draft, 'first_cut_media_status', lambda *args: {'status': 'current'})
        assert execution.next_production_action(root) == action
    assert execution._attempts(root) == [original]
