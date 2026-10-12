"""One-shot boards recover the same native image original after host interruption."""
import pytest
from lib import production_execution as execution, production_images as images
from tests.lib.test_production_images import board_package, reserve, host_receipt  # noqa: F401


def test_pending_board_original_never_emits_second_envelope(board_package):
    root, *_ = board_package
    call = reserve(board_package, call_id='board-original')
    first = execution.advance(root)
    assert first['host_envelope']['call_id'] == call['call_id']
    pending = execution.advance(root)
    assert 'host_envelope' not in pending
    assert images.image_usage(root)['total'] == 1
    receipt = host_receipt(root, first['host_envelope'])
    result = execution.complete_action(root, pending['action'], outcome={'host_receipt': receipt})
    assert images.image_call_status(root, call['call_id'])['state'] == 'imported'
    assert result['provider_calls'] == 0
    assert images.image_usage(root)['total'] == 1
    replay = execution.complete_action(root, pending['action'], outcome={'host_receipt': receipt})
    assert replay['replayed'] and replay['provider_calls'] == 0
    assert images.image_usage(root)['total'] == 1
