"""Delivery freezes candidates while an already-dispatched original finishes."""
import contextlib
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from lib import production_draft as draft
from lib import production_execution as execution
from tests.integration.test_first_pass_workflow import production, read  # noqa: F401
from tools.base_tool import ToolResult


def test_running_original_publishes_after_delivery_then_full_cut_converges(production, monkeypatch):
    p = production
    for sid in ('entry', 'interior'):
        if sid == 'interior':
            p.bind_upstream(sid)
        p.select(sid, p.generate(sid))
    p.bind_upstream('payoff')
    durations = {shot['id']: shot['duration_seconds'] for shot in p.contract['shots']}
    native_entered, finish_native, native_finished = (threading.Event() for _ in range(3))
    compose_entered, finish_compose, retention_waiting = (threading.Event() for _ in range(3))
    real_lock = execution._lock

    def transport(argv, **kwargs):
        if '--prompt-file' in argv:
            native_entered.set()  # Real wrapper already retained its one reservation.
            assert finish_native.wait(10), 'test did not release the native original'
            result = p.transport(argv, **kwargs)
            native_finished.set()
            return result
        return p.transport(argv, **kwargs)

    @contextlib.contextmanager
    def observed_lock(root):
        if threading.current_thread().name.startswith('original') and native_finished.is_set():
            retention_waiting.set()
        with real_lock(root):
            yield

    def probe(path):
        words = Path(path).read_text().split()
        duration = float(words[2]) if words[1] == 'CUT' else durations[words[3]]
        return {'duration_seconds': duration, 'video_codec': 'h264', 'audio_codec': 'aac',
                'has_audio': True, 'width': 1280, 'height': 720}

    compose_calls = []

    def compose(inputs):
        cuts = inputs['edit_decisions']['cuts']
        compose_calls.append([cut['id'] for cut in cuts])
        if len(compose_calls) == 1:
            compose_entered.set()
            assert finish_compose.wait(10), 'test did not release composition'
        duration = sum(cut['out_seconds'] - cut['in_seconds'] for cut in cuts)
        Path(inputs['output_path']).write_text(f'SYNTHETIC CUT {duration}')
        return ToolResult(success=True)

    monkeypatch.setattr('tools._grok_cli_media.subprocess.run', transport)
    monkeypatch.setattr(execution, '_lock', observed_lock)
    monkeypatch.setattr(draft, '_probe', probe)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='original') as originals, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix='delivery') as deliveries:
        original = originals.submit(p.generate, 'payoff')
        try:
            assert native_entered.wait(10)
            pending = next(path.parent for path in (p.root / 'production_attempts').glob('*/request.json')
                           if read(path)['shot_id'] == 'payoff')
            before = draft.first_cut_candidates(p.root)

            # Keep the same lock through the real compose and its current-cut check,
            # matching Studio's outer closeout lock.
            def deliver():
                with draft.delivery_lock(p.root):
                    result = draft.deliver_standalone(p.root, compose=compose)
                    assert result['currency'] == {'status': 'current', 'reasons': []}
                    assert not (pending / 'result.json').exists()
                    assert draft.first_cut_candidates(p.root) == before
                    return result

            partial_future = deliveries.submit(deliver)
            assert compose_entered.wait(10)
            finish_native.set()
            assert retention_waiting.wait(10), 'normal result retention did not acquire the delivery lock'
            assert not original.done()
            assert not (pending / 'result.json').exists()
            assert draft.first_cut_candidates(p.root) == before
        finally:
            finish_native.set()
            finish_compose.set()
        partial = partial_future.result(timeout=10)
        retained = original.result(timeout=10)

    assert retained.success
    assert read(pending / 'result.json')['status'] == 'generated'
    assert partial['status'] == 'partial'
    full = draft.deliver_standalone(p.root, compose=compose)
    assert full['status'] == 'complete'
    assert full['currency'] == {'status': 'current', 'reasons': []}
    assert [clip['shot_id'] for clip in full['clips']] == ['entry', 'interior', 'payoff']
    assert full['technical']['duration_seconds'] == sum(durations.values())
    assert draft.deliver_standalone(p.root, compose=compose)['first_cut'] == full['first_cut']
    assert compose_calls == [['entry', 'interior'], ['entry', 'interior', 'payoff']]
    assert len(p.transport.native_requests) == 3  # One original for each declared shot.
    assert len(list((p.root / 'production_attempts').glob('*/request.json'))) == 3
