"""Hermetic readiness tests: no Node, npm registry, or browser required."""

import os
import subprocess
from pathlib import Path

import pytest

from tools.video.hyperframes_compose import HyperFramesCompose


@pytest.fixture
def runtime(monkeypatch):
    for name in ("_npm_resolve_cache", "_cli_probe_cache", "_cli_command_cache"):
        monkeypatch.setattr(HyperFramesCompose, name, None)
    monkeypatch.setattr(
        HyperFramesCompose, "_node_major_version", classmethod(lambda cls: 22)
    )
    binaries = {name: f"/runtime/bin/{name}" for name in ("node", "npx", "npm", "ffmpeg")}
    monkeypatch.setattr(
        "tools.video.hyperframes_compose.shutil.which",
        lambda name, path=None: binaries.get((name, path)) if path else binaries.get(name),
    )
    return HyperFramesCompose(), binaries


def test_installed_cli_works_offline_and_is_used_for_workspace_render(runtime, monkeypatch, tmp_path):
    tool, binaries = runtime
    binaries["hyperframes"] = "/installed/bin/hyperframes"
    monkeypatch.setenv("NODE_OPTIONS", "--require /intentional/host-hook.js")
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        assert cmd[0] == "/installed/bin/hyperframes", "Must not contact npm/npx offline"
        assert kwargs.get("env", os.environ)["NODE_OPTIONS"] == os.environ["NODE_OPTIONS"]
        return subprocess.CompletedProcess(cmd, 0, "v0.8.26\n" if "--version" in cmd else "{}", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert tool._runtime_check()["runtime_available"] is True
    # Even if PATH changes after preflight, render uses the executable checked.
    binaries["hyperframes"] = "/different/bin/hyperframes"
    tool._run_hf(["render"], cwd=tmp_path, timeout=30, check=False)
    assert [cmd[1:] for cmd, _ in calls] == [["--version"], ["doctor", "--json"], ["render"]]
    assert calls[-1][1]["cwd"] == str(tmp_path)


def test_repository_install_precedes_global_install(runtime, monkeypatch):
    tool, binaries = runtime
    module = __import__(HyperFramesCompose.__module__, fromlist=["__file__"])
    bin_dir = str(Path(module.__file__).resolve().parents[2] / "node_modules" / ".bin")
    binaries[("hyperframes", bin_dir)] = f"{bin_dir}/hyperframes"
    binaries["hyperframes"] = "/global/hyperframes"
    assert tool._cli_command() == (f"{bin_dir}/hyperframes",)


@pytest.mark.parametrize("failure", ["exit", "timeout", "missing"])
def test_local_version_success_does_not_hide_doctor_failure(runtime, monkeypatch, failure):
    tool, binaries = runtime
    binaries["hyperframes"] = "/installed/hyperframes"

    def run(cmd, **kwargs):
        if "--version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "0.8.26", "")
        assert cmd == ["/installed/hyperframes", "doctor", "--json"]
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        if failure == "missing":
            raise FileNotFoundError("removed after version check")
        return subprocess.CompletedProcess(cmd, 1, "", "browser missing")

    monkeypatch.setattr(subprocess, "run", run)
    result = tool._runtime_check()
    assert result["runtime_available"] is False
    assert result["cli_probe_error"]


@pytest.mark.parametrize("stdout,returncode", [("", 0), ("not a version", 0), ("0.8.26", 1)])
def test_broken_installed_cli_does_not_fall_back_to_registry(runtime, monkeypatch, stdout, returncode):
    tool, binaries = runtime
    binaries["hyperframes"] = "/installed/hyperframes"

    def run(cmd, **kwargs):
        assert cmd == ["/installed/hyperframes", "--version"]
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")

    monkeypatch.setattr(subprocess, "run", run)
    assert tool._runtime_check()["runtime_available"] is False


def test_offline_without_install_reports_registry_failure(runtime, monkeypatch):
    tool, _ = runtime
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", run)
    result = tool._runtime_check()
    assert result["runtime_available"] is False
    assert "offline" in result["npm_resolve_error"]
    assert calls == [["/runtime/bin/npm", "view", "hyperframes", "version"]]


def test_npx_fallback_retains_doctor_and_render_command(runtime, monkeypatch, tmp_path):
    tool, _ = runtime
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "0.8.26" if "view" in cmd else "{}", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert tool._runtime_check()["runtime_available"] is True
    tool._run_hf(["render"], cwd=tmp_path, timeout=30, check=False)
    assert calls[1:] == [
        ["/runtime/bin/npx", "--yes", "hyperframes", "doctor", "--json"],
        ["/runtime/bin/npx", "--yes", "hyperframes", "render"],
    ]
