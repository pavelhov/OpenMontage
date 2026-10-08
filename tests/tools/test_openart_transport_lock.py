"""Transport lock lifetime proofs using only an executable fake OpenArt CLI."""
import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tools import _openart_cli as cli
from lib.openart_jobs import process_alive, process_identity


@pytest.fixture
def transport_env(tmp_path, monkeypatch):
    state = tmp_path / "private-state"
    log = tmp_path / "calls.jsonl"
    binary = tmp_path / "bin" / "openart"
    binary.parent.mkdir(mode=0o700)
    binary.write_text(
        "#!" + sys.executable + "\n"
        "import json, os, sys, time\n"
        "log = os.environ['FAKE_OPENART_LOG']\n"
        "with open(log, 'a') as f: f.write(json.dumps({'pid': os.getpid(), 'argv': sys.argv[1:]}) + '\\n')\n"
        "marker = os.environ.get('FAKE_OPENART_STARTED')\n"
        "if marker:\n"
        "    with open(marker, 'w') as f: f.write(str(os.getpid()))\n"
        "    release = os.environ['FAKE_OPENART_RELEASE']\n"
        # Safety fallback in case the test process itself cannot signal release.
        "    deadline = time.monotonic() + 15\n"
        "    while not os.path.exists(release) and time.monotonic() < deadline: time.sleep(.02)\n"
        "print(json.dumps({'version': 'fake'}))\n"
    )
    binary.chmod(0o700)
    monkeypatch.setenv("OPENART_CLI_PATH", str(binary))
    monkeypatch.setenv("OPENMONTAGE_OPENART_STATE_DIR", str(state))
    monkeypatch.setenv("FAKE_OPENART_LOG", str(log))
    monkeypatch.delenv("FAKE_OPENART_STARTED", raising=False)
    monkeypatch.delenv("FAKE_OPENART_RELEASE", raising=False)
    return {"state": state, "log": log, "binary": binary}


def _calls(env):
    if not env["log"].exists():
        return []
    return [json.loads(line) for line in env["log"].read_text().splitlines()]


def _wait_for(path, seconds=4):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(.01)
    pytest.fail(f"timed out waiting for {path.name}")


def test_run_readonly_has_bounded_wait_behind_same_process_thread_lock(transport_env):
    entered = threading.Event()
    release = threading.Event()

    def holder():
        with cli.transport_lock():
            entered.set()
            release.wait(3)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    try:
        assert entered.wait(2)
        started = time.monotonic()
        with pytest.raises(cli.OpenArtCLIError) as err:
            cli.run_readonly(["version"], timeout=0.2)
        elapsed = time.monotonic() - started
        assert err.value.kind == "transport_busy"
        assert 0.15 <= elapsed < 1.5
        assert _calls(transport_env) == []
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()


def test_transport_lock_is_reentrant_for_nested_readonly_call(transport_env):
    with cli.transport_lock():
        assert cli.held_lock_fd() is not None
        result = cli.run_readonly(["version"], timeout=1)
        assert result["parsed"] == {"version": "fake"}
        assert cli.held_lock_fd() is not None
    assert cli.held_lock_fd() is None
    assert len(_calls(transport_env)) == 1


def test_orphaned_cli_child_keeps_flock_until_exit(transport_env, tmp_path, monkeypatch):
    started = tmp_path / "child.started"
    release = tmp_path / "release.child"
    monkeypatch.setenv("FAKE_OPENART_STARTED", str(started))
    monkeypatch.setenv("FAKE_OPENART_RELEASE", str(release))
    env = os.environ.copy()
    # The outer process owns the transport; the fake executable inherits its flock fd.
    runner = subprocess.Popen(
        [sys.executable, "-c",
         "from tools import _openart_cli as c; c.run_readonly(['version'], timeout=20)"],
        cwd=Path(__file__).resolve().parents[2], env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    child_pid = None
    child_identity = None
    try:
        _wait_for(started)
        child_pid = int(started.read_text())
        assert child_pid != runner.pid
        child_identity = process_identity(child_pid)
        assert child_identity and child_identity.get("kind") not in {"unknown", "unsupported"}
        assert process_alive(child_identity) == "alive"
        # Kill only the Python transport owner; leave its actual CLI child alive.
        runner.kill()
        runner.wait(timeout=2)

        began = time.monotonic()
        with pytest.raises(cli.OpenArtCLIError) as err:
            cli.run_readonly(["version"], timeout=0.2)
        elapsed = time.monotonic() - began
        assert err.value.kind == "transport_busy"
        assert 0.15 <= elapsed < 1.5
        assert len(_calls(transport_env)) == 1
        assert not release.exists()

        release.touch(mode=0o600)
        assert _wait_process_exit(child_identity)
        result = cli.run_readonly(["version"], timeout=2)
        assert result["parsed"] == {"version": "fake"}
        assert [c["argv"][:1] for c in _calls(transport_env)] == [["version"], ["version"]]

        streams = transport_env["state"] / "streams"
        raw_outputs = [p for p in streams.glob("*.stdout") if p.read_bytes()]
        assert len(raw_outputs) == 2
        assert all(json.loads(p.read_text()) == {"version": "fake"} for p in raw_outputs)
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in raw_outputs)
        assert stat.S_IMODE(streams.stat().st_mode) == 0o700
    finally:
        # Always unblock the fake process before considering a narrowly targeted cleanup kill.
        release.touch(mode=0o600, exist_ok=True)
        if runner.poll() is None:
            runner.kill()
            runner.wait(timeout=2)
        if child_identity is not None and not _wait_process_exit(child_identity, seconds=2):
            # Never signal a PID unless its kernel birth identity still matches.
            if process_alive(child_identity) == "alive":
                os.kill(child_pid, 9)
                _wait_process_exit(child_identity, seconds=2)


def _wait_process_exit(identity, seconds=4):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process_alive(identity) == "dead":
            return True
        time.sleep(.02)
    return False
