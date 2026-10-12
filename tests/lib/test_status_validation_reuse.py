"""Bounded read-only status reuse: invalidation, isolation and write opt-out. Offline."""
import asyncio
import os
import threading
import time
from pathlib import Path

import pytest

from lib import production_autonomy as pa
from lib import production_draft as draft


def counting(root, name='policy.json'):
    calls = []

    def compute():
        calls.append(1)
        return {'bytes': (Path(root) / name).read_bytes().decode(), 'nested': [1]}
    return calls, lambda: pa.status_reused(('k',), (root,), compute)


@pytest.fixture
def root(tmp_path):
    (tmp_path / 'artifacts').mkdir()
    (tmp_path / 'policy.json').write_text('v1')
    (tmp_path / 'artifacts' / 'review.json').write_text('r1')
    return tmp_path


def test_outside_scope_always_recomputes(root):
    calls, call = counting(root)
    call(); call()
    assert len(calls) == 2


def test_scope_reuses_identical_inputs_and_returns_copies(root):
    calls, call = counting(root)
    with pa.status_reuse():
        first = call()
        first['nested'].append('tampered')
        second = call()
    assert len(calls) == 1 and second == {'bytes': 'v1', 'nested': [1]}


@pytest.mark.parametrize('edit', [
    lambda r: (r / 'policy.json').write_text('v2'),                 # policy bytes
    lambda r: (r / 'policy.json').write_text('v1'),                 # same bytes rewritten
    lambda r: (r / 'artifacts' / 'review.json').write_text('r2'),   # nested review note
    lambda r: (r / 'artifacts' / 'new.json').write_text('{}'),      # added file
    lambda r: (r / 'artifacts' / 'review.json').unlink(),           # removed file
    lambda r: os.utime(r / 'policy.json'),                          # touch only
    lambda r: os.chmod(r / 'policy.json', 0o600),                   # metadata only
    lambda r: (r / 'artifacts' / 'review.json').rename(r / 'artifacts' / 'moved.json'),
])
def test_any_edit_inside_scope_invalidates(root, edit):
    calls, call = counting(root)
    with pa.status_reuse():
        call()
        time.sleep(0.002)
        edit(root)
        call()
        call()
    assert len(calls) == 2


def test_mutation_during_compute_is_never_retained(root):
    calls = []

    def compute():
        calls.append(1)
        (root / 'artifacts' / f'write{len(calls)}.json').write_text('x')
        return len(calls)
    with pa.status_reuse():
        assert [pa.status_reused(('k',), (root,), compute) for _ in range(3)] == [1, 2, 3]


def test_exception_not_memoized_and_scope_cleared(root):
    calls = []

    def boom():
        calls.append(1)
        raise ValueError('x')
    with pa.status_reuse():
        for _ in range(2):
            with pytest.raises(ValueError):
                pa.status_reused(('k',), (root,), boom)
    assert len(calls) == 2
    calls2, call = counting(root)
    with pytest.raises(RuntimeError):
        with pa.status_reuse():
            call()
            raise RuntimeError
    assert pa._STATUS_REUSE.get() is None
    with pa.status_reuse():
        call()
    assert len(calls2) == 2


def test_nested_status_joins_but_write_opt_out_recomputes(root):
    calls, call = counting(root)
    with pa.status_reuse():
        call()
        with pa.status_reuse():
            call()
        with pa.no_status_reuse():
            call(); call()
            with pa.status_reuse():   # status read nested under a writer stays disabled
                call(); call()
                with pa.status_reuse():
                    call()
        call()
    assert len(calls) == 6


def test_writer_opt_out_is_monotonic_and_restored(root):
    calls, call = counting(root)
    with pa.no_status_reuse():
        with pa.no_status_reuse():
            pass
        with pa.status_reuse():
            call(); call()        # still disabled after the inner opt-out exits
    assert len(calls) == 2
    with pa.status_reuse():
        call(); call()            # depth restored to 0: reuse works again
    assert len(calls) == 3


def test_draft_status_read_under_writer_recomputes(root):
    calls, call = counting(root)
    read = draft._status_read(lambda: call())
    with pa.no_status_reuse():
        read(); read()
    assert len(calls) == 2


def test_execution_lock_suppresses_inherited_and_nested_reuse(root, tmp_path_factory):
    from lib import production_execution as execution
    lock_root = tmp_path_factory.mktemp('lock')   # lock file outside the stamped inputs
    calls, call = counting(root)
    read = draft._status_read(lambda: call())
    with pa.status_reuse():
        call()
        with execution._lock(lock_root):
            call(); read(); read()
            with pa.status_reuse():
                call(); call()
        call()                     # outer scope resumes after the lock
    assert len(calls) == 6


def test_draft_write_entries_opt_out_and_reads_scope():
    for name in ('append_creator_draft_exception', 'compose_first_cut', 'deliver_standalone',
                 'record_first_cut_acceptance'):
        assert getattr(draft, name).__code__.co_names.count('no_status_reuse') == 1, name
    for name in ('first_cut_status', 'first_cut_media_status', 'first_cut_acceptance_status',
                 '_clip_findings', '_current_review_status'):
        assert getattr(draft, name).__code__.co_names.count('status_reuse') == 1, name


def test_thread_does_not_see_scope(root):
    calls, call = counting(root)
    with pa.status_reuse():
        call()
        worker = threading.Thread(target=call)
        worker.start(); worker.join()
        call()
    assert len(calls) == 2


def test_copied_context_into_asyncio_tasks_is_isolated(root):
    calls, call = counting(root)

    async def child():
        call()   # runs in a task whose context is a copy holding the parent's scope
        call()

    async def main():
        with pa.status_reuse():
            call()
            await asyncio.gather(asyncio.create_task(child()), asyncio.create_task(child()))
            await asyncio.get_running_loop().run_in_executor(None, call)
            call()
    asyncio.run(main())
    assert len(calls) == 6   # parent once, 2 per child task (no reuse), executor once; last reuses


def test_unsupported_platform_or_coarse_or_unreadable_stamp_recomputes(root, monkeypatch):
    calls, call = counting(root)
    monkeypatch.setattr(pa, '_REUSE_PLATFORM', False)
    with pa.status_reuse():
        call(); call()
    monkeypatch.setattr(pa, '_REUSE_PLATFORM', True)
    for path in root.rglob('*'):
        os.utime(path, ns=(1_000_000_000, 2_000_000_000))
    os.utime(root, ns=(1_000_000_000, 2_000_000_000))
    monkeypatch.setattr(pa, '_reuse_stamp', lambda *b: None)
    with pa.status_reuse():
        call(); call()
    assert len(calls) == 4


def test_coarse_timestamps_disable_reuse(root):
    for path in [*root.rglob('*')]:
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    stamp = pa._reuse_stamp(str(root))
    # ctime is set by the kernel; only when every timestamp is whole-second is reuse refused.
    assert stamp is None or any(row[-1] % 1_000_000_000 for row in stamp[1:])
    assert pa._reuse_stamp(str(root / 'missing')) is None


def test_load_active_policy_never_reused_global_or_shot(root, monkeypatch):
    calls = []

    def fake(r, *, shot_id=None):
        calls.append(shot_id)
        return ((Path(r) / 'policy.json').read_text(), 'sha', 'd', [])
    monkeypatch.setattr(pa, '_load_active_policy', fake)
    assert pa.load_active_policy(root)[0] == 'v1'
    with pa.status_reuse():
        # Global and shot loads can reach upstream board provenance and thereby
        # external MCP discovery/account/environment state: always fresh.
        pa.load_active_policy(root)
        pa.load_active_policy(root)
        pa.load_active_policy(root, shot_id='S01')
        pa.load_active_policy(root, shot_id='S01')
        (root / 'policy.json').write_text('revoked')
        assert pa.load_active_policy(root)[0] == 'revoked'
    assert calls == [None, None, None, 'S01', 'S01', None]


def test_load_active_policy_sees_external_state_change_within_scope(root, monkeypatch):
    """Focused proof for the transitive external branch: a state the project stamp
    cannot see (env/discovery readiness) flips inside one operation and both
    global and shot loads observe it."""
    external = {'ready': True}

    def fake(r, *, shot_id=None):
        return (('ok' if external['ready'] else None), 'sha', 'd', [])
    monkeypatch.setattr(pa, '_load_active_policy', fake)
    with pa.status_reuse():
        assert pa.load_active_policy(root)[0] == 'ok'
        assert pa.load_active_policy(root, shot_id='S01')[0] == 'ok'
        external['ready'] = False
        assert pa.load_active_policy(root)[0] is None
        assert pa.load_active_policy(root, shot_id='S01')[0] is None


def test_global_policy_fresh_after_pipeline_gate_change_within_scope(tmp_path, monkeypatch):
    """Input boundary: nonempty checkpoint_stages consults the pipeline manifest
    loader (outside the project stamp). Each global load re-enters that branch
    within one scope. (The loader itself is process lru_cached; a disk edit is
    not claimed to be visible -- this proves only no extra policy-level reuse.)"""
    from lib import pipeline_loader
    from tests.lib.test_production_autonomy import install, make_policy
    root = tmp_path / 'offline-first-pass'
    root.mkdir()
    marker = {'project_id': 'offline-first-pass', 'story_revision': 'story-1',
              'pipeline_type': 'framework-smoke', 'governance': {'mode': 'strict', 'version': '1.0'}}
    install(root, make_policy(checkpoint_stages=['script']), marker=marker)
    real = pipeline_loader.load_pipeline_readonly
    state = {'gated': True, 'loads': 0}

    def manifest(name):
        state['loads'] += 1
        loaded = real(name)
        if not state['gated']:
            loaded = {**loaded, 'stages': [{**s, 'human_approval_default': False} if s['name'] == 'script' else s
                                           for s in loaded['stages']]}
        return loaded
    monkeypatch.setattr(pipeline_loader, 'load_pipeline_readonly', manifest)
    with pa.status_reuse():
        assert pa.load_active_policy(root)[0] is not None
        state['gated'] = False
        policy, _, _, reasons = pa.load_active_policy(root)
        assert policy is None and pa.CHECKPOINT_STAGES_ERROR in reasons
    assert state['loads'] == 2


def _baseline_fixture(root, monkeypatch):
    """Real retained_baselines body over real evidence bytes; only the pure
    descriptor comparison is stubbed to the retained value."""
    from lib.production_request import digest
    base = {'project_id': 'p', 'shots': {'S01': {'d': 1}}}
    content = {'kind': pa.EVIDENCE_KIND, 'policy': base, 'baselines': {'S01': {'nested': [1]}}}
    rel = f'approvals/autonomy-policy-{digest(content)}.json'
    raw = __import__('json').dumps(content).encode()
    (root / 'approvals').mkdir(exist_ok=True)
    (root / rel).write_bytes(raw)
    policy = {**base, 'evidence': {'path': rel, 'sha256': __import__('hashlib').sha256(raw).hexdigest()}}
    calls = []
    real = pa._retained_baselines

    def counted(r, p):
        calls.append(1)
        return real(r, p)
    monkeypatch.setattr(pa, '_retained_baselines', counted)
    monkeypatch.setattr(pa, 'baseline_descriptor', lambda retained, r=None: {'d': 1})
    return policy, rel, calls


def test_retained_baselines_reuse_isolated_and_invalidated(root, monkeypatch):
    policy, rel, calls = _baseline_fixture(root, monkeypatch)
    with pa.status_reuse():
        first = pa.retained_baselines(root, policy)
        first['S01']['nested'].append('poison')
        second = pa.retained_baselines(root, policy)
        assert second == {'S01': {'nested': [1]}} and len(calls) == 1
        # A different full supplied policy (e.g. historical) misses.
        other = {**policy, 'project_id': 'q'}
        with pytest.raises(pa.AutonomyError):
            pa.retained_baselines(root, other)
        assert len(calls) == 2
        # Changed evidence bytes inside the same operation fail closed.
        time.sleep(0.01)
        (root / rel).write_bytes(b'{"tampered": true}')
        with pytest.raises(pa.AutonomyError, match='evidence bytes changed'):
            pa.retained_baselines(root, policy)


def test_retained_baselines_fresh_outside_scope_and_under_no_status_reuse(root, monkeypatch):
    policy, _, calls = _baseline_fixture(root, monkeypatch)
    pa.retained_baselines(root, policy); pa.retained_baselines(root, policy)
    assert len(calls) == 2
    with pa.status_reuse():
        pa.retained_baselines(root, policy)
        with pa.no_status_reuse():
            pa.retained_baselines(root, policy); pa.retained_baselines(root, policy)
    assert len(calls) == 5


def test_one_coarse_entry_disables_reuse_despite_fine_others(root):
    os.utime(root / 'policy.json', ns=(1_000_000_000, 1_000_000_000))
    stamp = pa._reuse_stamp(str(root))
    # ctime is kernel-set; reuse is refused unless that entry's ctime is fine.
    ctime = os.stat(root / 'policy.json').st_ctime_ns
    assert stamp is None if ctime % 1_000_000_000 == 0 else stamp is not None


def test_coarse_entry_rejected(monkeypatch, root):
    real = os.stat

    class Coarse:
        def __init__(self, s):
            self.__dict__.update({k: getattr(s, k) for k in dir(s) if k.startswith('st_')})
            self.st_mtime_ns = self.st_ctime_ns = 5_000_000_000
    monkeypatch.setattr(pa.os, 'stat', lambda p, **k: Coarse(real(p, **k)) if str(p).endswith('policy.json') else real(p, **k))
    assert pa._reuse_stamp(str(root / 'policy.json')) is None


# boards._current: per-file exact-bytes memo inside one status operation only.
from lib import production_boards as boards
import hashlib


def bind(root, data=b'frame-1', hashes=None):
    (root / 'board.png').write_bytes(data)
    binding = {'path': 'board.png', 'sha256': hashlib.sha256(data).hexdigest()}
    if hashes is not None:
        hashes.clear()
    return binding


@pytest.fixture
def hashes(monkeypatch):
    count = []
    real = hashlib.sha256

    def counting_sha(*a, **k):
        count.append(1)
        return real(*a, **k)
    monkeypatch.setattr(boards.hashlib, 'sha256', counting_sha)
    return count


def test_boards_current_reuses_within_scope_only(root, hashes):
    binding = bind(root, hashes=hashes)
    r = root.resolve()
    assert boards._current(r, binding) and boards._current(r, binding)
    assert len(hashes) == 2
    with pa.status_reuse():
        assert all(boards._current(r, binding) for _ in range(5))
    assert len(hashes) == 3
    with pa.status_reuse(), pa.no_status_reuse():
        boards._current(r, binding); boards._current(r, binding)
    assert len(hashes) == 5


def test_boards_current_detects_same_size_same_mtime_tamper(root):
    binding = bind(root, b'AAAA')
    r = root.resolve()
    with pa.status_reuse():
        assert boards._current(r, binding)
        st = os.stat(root / 'board.png')
        time.sleep(0.002)
        (root / 'board.png').write_bytes(b'BBBB')
        os.utime(root / 'board.png', ns=(st.st_atime_ns, st.st_mtime_ns))   # ctime still moves
        assert not boards._current(r, binding)


@pytest.mark.parametrize('edit', ['delete', 'retarget', 'replace'])
def test_boards_current_delete_symlink_and_replace(root, edit):
    (root / 'a.png').write_bytes(b'good')
    (root / 'b.png').write_bytes(b'evil')
    (root / 'board.png').symlink_to(root / 'a.png')
    binding = {'path': 'board.png', 'sha256': hashlib.sha256(b'good').hexdigest()}
    r = root.resolve()
    with pa.status_reuse():
        assert boards._current(r, binding)
        if edit == 'delete':
            (root / 'a.png').unlink()
        elif edit == 'retarget':
            (root / 'board.png').unlink()
            (root / 'board.png').symlink_to(root / 'b.png')
        else:
            (root / 'a.png').unlink()
            (root / 'a.png').write_bytes(b'evil')
        assert not boards._current(r, binding)


def test_boards_current_write_during_hash_not_retained(root, hashes, monkeypatch):
    binding = bind(root, hashes=hashes)
    r = root.resolve()
    real = Path.read_bytes

    def racing(self):
        data = real(self)
        if self.name == 'board.png':
            time.sleep(0.002)
            os.utime(self)              # concurrent metadata change mid-compute
        return data
    monkeypatch.setattr(Path, 'read_bytes', racing)
    with pa.status_reuse():
        boards._current(r, binding); boards._current(r, binding); boards._current(r, binding)
    assert len(hashes) == 3


def test_boards_current_thread_and_task_isolated(root, hashes):
    binding = bind(root, hashes=hashes)
    r = root.resolve()
    with pa.status_reuse():
        boards._current(r, binding)
        t = threading.Thread(target=lambda: [boards._current(r, binding) for _ in range(2)])
        t.start(); t.join()

        async def main():
            await asyncio.gather(*(asyncio.to_thread(boards._current, r, binding) for _ in range(2)),
                                 asyncio.create_task(asyncio.sleep(0, boards._current(r, binding))))
        asyncio.run(main())
    assert len(hashes) == 1 + 2 + 2 + 1


def test_board_transaction_suppresses_outer_reuse(root, hashes):
    binding = bind(root, hashes=hashes)
    r = root.resolve()
    op = boards._board_transaction(lambda rt: [boards._current(rt, binding) for _ in range(3)])
    with pa.status_reuse():
        boards._current(r, binding)
        op(r)
    assert len(hashes) == 4
