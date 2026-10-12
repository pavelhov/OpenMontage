"""AE2: a begun original with no retained reply never holds independent work."""
from lib import production_execution as execution, production_images as images
from tests.lib.test_factory_receipts_and_payoff import pending_openart_with_boards
from tests.lib.test_production_factory import file_bytes
from tests.integration.test_openart_mcp_autonomy import lifecycle  # noqa: F401


def test_reply_unavailable_original_stays_counted_recoverable_and_never_preempts(lifecycle):
    root, jobs = pending_openart_with_boards(lifecycle, accepted=False)
    original = {k: v for k, v in jobs.frozen_request(root, 'accepted-original').items() if k != 'status'}
    assert jobs.resume_original(root, 'accepted-original')['next_action']['action'] == 'blocked_original_host_reply_unavailable'
    before = file_bytes(root)
    action = execution.next_production_action(root)
    # Independent declared work proceeds; the original is not begun again.
    assert (action['kind'], action['subject']) == ('prepare_board_image', 'future1')
    assert file_bytes(root) == before
    receive = execution._recoverable_original_action(root, 'accepted-original')
    assert receive['kind'] == 'receive_openart_original' and receive['host_required']
    # The exact replay for the original is still accepted out of order.
    step = execution.complete_action(root, receive, outcome={
        'result': {'historyId': 'late-synthetic-job', 'status': 'PENDING'}})
    assert jobs.resume_original(root, 'accepted-original')['next_action']['action'] == 'poll_original'
    assert {k: v for k, v in jobs.frozen_request(root, 'accepted-original').items() if k != 'status'} == original
    assert len(jobs.list_attempts(root)) == 1 and images.image_usage(root)['total'] == 0
    assert step['action']['kind'] != 'begin_openart_original'
    # Once received, a forged or repeated replay is stale.
    try:
        execution.complete_action(root, receive, outcome={'result': {'historyId': 'other'}})
    except ValueError:
        pass
    else:
        raise AssertionError('stale receive replay accepted')


def test_final_reply_unavailable_original_does_not_stall_delivery(lifecycle):
    """Last unresolved original: closeout proceeds; late exact receive stays valid."""
    root, _, _, inputs, _, _, jobs = lifecycle
    jobs.prepare(root, attempt_id='accepted-original', generation_inputs=inputs,
        authority_fn=execution.prepare_openart_mcp_handoff)
    jobs.begin(root, 'accepted-original', authority_fn=execution.prepare_openart_mcp_handoff)
    original = {k: v for k, v in jobs.frozen_request(root, 'accepted-original').items() if k != 'status'}
    action = execution.next_production_action(root)
    assert action['kind'] == 'compose_cut'
    [pending] = action['inputs']['recoverable_originals']
    assert (pending['kind'], pending['subject']) == ('receive_openart_original', 'accepted-original')
    step = execution.advance(root)
    assert step['action']['kind'] == 'compose_cut' and step['provider_calls'] == 0
    assert len(jobs.list_attempts(root)) == 1
    # The exact late reply is still accepted for the one original request.
    execution.complete_action(root, pending, outcome={
        'result': {'historyId': 'late-synthetic-job', 'status': 'PENDING'}})
    assert jobs.resume_original(root, 'accepted-original')['next_action']['action'] == 'poll_original'
    assert {k: v for k, v in jobs.frozen_request(root, 'accepted-original').items() if k != 'status'} == original
    assert len(jobs.list_attempts(root)) == 1
