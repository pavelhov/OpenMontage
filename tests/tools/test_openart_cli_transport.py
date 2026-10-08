"""U1 OpenArt CLI transport: argv-only, serialized, private, read-only, offline-safe."""
import hashlib
import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

from tools import _openart_cli as cli
from tools.openart_account import OpenArtAccount

REPO = Path(__file__).resolve().parents[2]

FAKE = r'''#!{python}
import json, os, sys, time
log = os.environ.get("FAKE_OPENART_LOG")
start = time.time()
mode = os.environ.get("FAKE_OPENART_MODE", "ok")
if mode == "sleep":
    time.sleep(float(os.environ.get("FAKE_OPENART_SLEEP", "5")))
if mode == "hold":
    time.sleep(0.15)
out = {{"argv": sys.argv[1:], "token_env": os.environ.get("OPENART_TOKEN"),
       "accessToken": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlc2ln",
       "url": "https://cdn.example/x.mp4?X-Amz-Signature=abc&X-Amz-Credential=k",
       "items": [{{"model": "m1", "totalCredits": 12}}]}}
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps({{"argv": sys.argv[1:], "start": start, "end": time.time()}}) + "\n")
if mode == "nonzero":
    sys.stderr.write("error: not logged in token=sk-secretsecretsecret\n")
    sys.exit(3)
if mode == "malformed":
    print("{{not json")
elif mode == "huge":
    sys.stdout.write("x" * (2 * 1024 * 1024))
else:
    print(json.dumps(out))
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    binary = tmp_path / "bin" / "openart"
    binary.parent.mkdir()
    binary.write_text(FAKE.format(python=sys.executable))
    binary.chmod(0o755)
    state = tmp_path / "state"
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("OPENART_CLI_PATH", str(binary))
    monkeypatch.setenv("OPENMONTAGE_OPENART_STATE_DIR", str(state))
    monkeypatch.setenv("FAKE_OPENART_LOG", str(log))
    monkeypatch.delenv("FAKE_OPENART_MODE", raising=False)
    return {"binary": binary, "state": state, "log": log}


def calls(env):
    if not env["log"].exists():
        return []
    return [json.loads(line) for line in env["log"].read_text().splitlines()]


def test_missing_binary_is_typed_and_calls_nothing(env, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENART_CLI_PATH", str(tmp_path / "absent"))
    with pytest.raises(cli.OpenArtCLIError) as err:
        cli.run_readonly(["version"])
    assert err.value.kind == "missing_binary"


def test_local_bin_fallback_when_path_lacks_openart(env, monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    target = home / ".local" / "bin" / "openart"
    target.write_text(env["binary"].read_text())
    target.chmod(0o755)
    monkeypatch.delenv("OPENART_CLI_PATH")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert cli.resolve_binary() == str(target)


def test_explicit_override_wins_and_must_be_executable(env, monkeypatch, tmp_path):
    assert cli.resolve_binary() == str(env["binary"])
    plain = tmp_path / "plain"
    plain.write_text("x")
    monkeypatch.setenv("OPENART_CLI_PATH", str(plain))
    with pytest.raises(cli.OpenArtCLIError) as err:
        cli.resolve_binary()
    assert err.value.kind == "missing_binary"


def test_argv_metacharacters_are_data_and_global_flags_added(env, tmp_path):
    marker = tmp_path / "pwned"
    prompt = f"a cat; touch {marker} && $(id) `id` | rm -rf ~"
    out = cli.run_readonly(cli.native_dry_run_argv(prompt, model="m1", mode="text2video", duration=5))
    assert not marker.exists()
    argv = calls(env)[0]["argv"]
    assert prompt in argv and "--dry-run" in argv and "--json" in argv and "--no-input" in argv
    assert "--async" not in argv and "-o" not in argv and "--output" not in argv
    assert out["parsed"]["argv"] == argv


def test_openart_token_env_is_stripped_from_child(env, monkeypatch):
    monkeypatch.setenv("OPENART_TOKEN", "leaky")
    out = cli.run_readonly(["version"])
    assert out["parsed"]["token_env"] is None


@pytest.mark.parametrize("bad", ["--help", "-x", "a b", "m;rm", "", "x" * 80, "$(id)"])
def test_identifier_injection_rejected_before_call(env, bad):
    with pytest.raises(cli.OpenArtCLIError) as err:
        cli.model_cost_argv(bad, "text2video")
    assert err.value.kind == "invalid_argument"
    assert calls(env) == []


@pytest.mark.parametrize("prompt", ["", "   ", "--async", "-o out.mp4"])
def test_bad_prompt_rejected(env, prompt):
    with pytest.raises(cli.OpenArtCLIError):
        cli.native_dry_run_argv(prompt, model="m1", mode="text2video")


def test_native_dry_run_refuses_local_image_and_bad_duration(env, tmp_path):
    with pytest.raises(cli.OpenArtCLIError):
        cli.native_dry_run_argv("p", model="m1", mode="image2video", image=str(tmp_path / "a.png"))
    with pytest.raises(cli.OpenArtCLIError):
        cli.native_dry_run_argv("p", model="m1", mode="text2video", duration="5; rm")


@pytest.mark.parametrize("argv", [
    ["generate", "video", "p", "--model", "m1", "--async"],
    ["generate", "video", "p", "--model", "m1"],
    ["generate", "video", "p", "--dry-run", "--async"],
    ["generate", "video", "p", "--dry-run", "-o", "x.mp4"],
    ["upload", "add", "f.png"],
    ["upload", "add", "f.png", "--dry-run"],
    ["auth", "login"], ["login"], ["logout"],
    ["creation", "wait", "abc"],
    ["model", "list", "--token", "x"],
])
def test_non_read_only_commands_refused_before_any_call(env, argv):
    with pytest.raises(cli.OpenArtCLIError) as err:
        cli.run_readonly(argv)
    assert err.value.kind == "not_read_only"
    assert calls(env) == []


@pytest.mark.parametrize("mode,kind", [("malformed", "malformed_json"), ("nonzero", "nonzero_exit"),
                                       ("huge", "output_too_large")])
def test_failure_kinds_are_bounded_and_redacted(env, monkeypatch, mode, kind):
    monkeypatch.setenv("FAKE_OPENART_MODE", mode)
    with pytest.raises(cli.OpenArtCLIError) as err:
        cli.run_readonly(["account"])
    assert err.value.kind == kind
    public = json.dumps(err.value.public())
    assert "sk-secret" not in public and len(public) < 8192
    if mode == "nonzero":
        assert err.value.diagnostics["returncode"] == 3


def test_timeout_kind(env, monkeypatch):
    monkeypatch.setenv("FAKE_OPENART_MODE", "sleep")
    with pytest.raises(cli.OpenArtCLIError) as err:
        cli.run_readonly(["version"], timeout=0.5)
    assert err.value.kind == "timeout"


def test_public_copy_redacts_tokens_and_signed_urls_raw_receipt_private(env):
    out = cli.run_readonly(["model", "list"])
    public = json.dumps(out["public"])
    assert "eyJhbGci" not in public and "X-Amz-Signature" not in public and "abc" not in public
    assert "https://cdn.example/x.mp4" in public
    assert "private_receipt" not in out and str(env["state"]) not in json.dumps(
        {k: v for k, v in out.items() if k != "parsed"})
    receipt = cli.receipt_path(out["receipt_id"])
    assert receipt.is_relative_to(env["state"].resolve())
    assert hashlib.sha256(receipt.read_bytes()).hexdigest() == out["receipt_sha256"]
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    assert "X-Amz-Signature" in receipt.read_text()
    assert stat.S_IMODE(env["state"].stat().st_mode) == 0o700


def test_redact_helper():
    data = {"Authorization": "Bearer x", "nested": [{"refresh_token": "r", "ok": "sk-abcdefghijklmnop1234"}],
            "u": "https://a.b/c?sig=1"}
    red = cli.redact(data)
    assert red["Authorization"] == "[redacted]" and red["nested"][0]["refresh_token"] == "[redacted]"
    assert "sk-" not in red["nested"][0]["ok"] and red["u"] == "https://a.b/c?[redacted]"


def test_state_dir_inside_checkout_rejected(env, monkeypatch):
    monkeypatch.setenv("OPENMONTAGE_OPENART_STATE_DIR", str(REPO / "tmp_openart_state"))
    with pytest.raises(cli.OpenArtCLIError) as err:
        cli.state_dir()
    assert err.value.kind == "unsafe_state"
    assert not (REPO / "tmp_openart_state").exists()


def test_unsafe_existing_state_rejected(env, tmp_path, monkeypatch):
    env["state"].mkdir(mode=0o755)
    env["state"].chmod(0o755)
    with pytest.raises(cli.OpenArtCLIError) as err:
        cli.run_readonly(["version"])
    assert err.value.kind == "unsafe_state" and calls(env) == []
    env["state"].chmod(0o700)
    side = env["state"] / "attempts.sqlite-wal"
    side.write_text("")
    side.chmod(0o644)
    with pytest.raises(cli.OpenArtCLIError):
        cli.verify_private_state()
    side.chmod(0o600)
    cli.verify_private_state()
    link = tmp_path / "link"
    link.symlink_to(env["state"])
    monkeypatch.setenv("OPENMONTAGE_OPENART_STATE_DIR", str(link))
    with pytest.raises(cli.OpenArtCLIError):
        cli.state_dir()


def test_private_file_helper_creates_0600(env):
    path = cli.write_private(cli.state_dir() / "receipts" / "r.json", b"{}")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_transport_lock_serializes_all_calls(env, monkeypatch):
    monkeypatch.setenv("FAKE_OPENART_MODE", "hold")
    threads = [threading.Thread(target=cli.run_readonly, args=(argv,))
               for argv in (["version"], ["account"], ["model", "list"])]
    for t in threads: t.start()
    for t in threads: t.join()
    spans = sorted((c["start"], c["end"]) for c in calls(env))
    assert len(spans) == 3
    assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))


def test_account_quote_requires_explicit_read_only_and_exact_argv(env, tmp_path):
    tool = OpenArtAccount()
    refused = tool.execute({"action": "quote", "model": "m1", "mode": "text2video"})
    assert not refused.success and calls(env) == []
    result = tool.execute({"action": "quote", "model": "m1", "mode": "text2video", "read_only": True,
                           "project_dir": str(tmp_path)})
    assert result.success and result.cost_usd == 0
    assert result.data["reservations"] == 0 and result.data["paid_submission"] is False
    assert result.data["evidence"]["qualification"] == "unqualified_for_dispatch"
    assert calls(env)[0]["argv"] == ["model", "cost", "--model", "m1", "--mode", "text2video", "--json", "--no-input"]
    assert not (tmp_path / "production_attempts").exists()
    assert not list(tmp_path.rglob("*.sqlite*"))


def test_account_inspect_and_native_dry_run(env):
    tool = OpenArtAccount()
    inspect = tool.execute({"action": "inspect", "read_only": True})
    assert inspect.success
    assert [c["argv"][0] for c in calls(env)] == ["version", "account"]
    dry = tool.execute({"action": "native_dry_run", "read_only": True, "prompt": "p", "model": "m1",
                        "mode": "text2video", "duration": 5})
    assert dry.success and "--dry-run" in calls(env)[-1]["argv"]
    assert dry.data["evidence"]["qualification"] == "unqualified_for_dispatch"


@pytest.mark.parametrize("action", ["status", "collect", "resolve_attempt", "submit", "upload", "bogus"])
def test_delegated_or_generation_actions_unavailable(env, action):
    result = OpenArtAccount().execute({"action": action, "read_only": True})
    assert not result.success and calls(env) == []


def test_readiness_gates_truthful_without_probe(env):
    ready = cli.readiness()
    assert ready["generation_enabled"] is False and calls(env) == []
    for gate in ("async_result_contract", "upload_billing", "exhaustive_history", "account_identity",
                 "native_audio", "end_frame_pin", "model_ids", "settings_exact_quote"):
        assert ready["gates"][gate] == "unqualified"
    probed = cli.readiness(probe=True)
    assert [c["argv"][0] for c in calls(env)] == ["version"]
    assert probed["binary"]["available"] is True and probed["generation_enabled"] is False


def test_form_controls_and_missing_controls():
    form = {"type": "object", "properties": {"duration": {"type": "integer", "default": 5},
                                             "aspect_ratio": {"enum": ["16:9"]}}, "required": ["aspect_ratio"]}
    controls = cli.form_controls(form)
    assert controls["duration"]["default"] == 5 and controls["duration"]["required"] is False
    assert cli.missing_controls(controls, ["end_image", "duration", "audio"]) == ["audio", "end_image"]
    with pytest.raises(cli.OpenArtCLIError) as err:
        cli.form_controls({"fields": []})
    assert err.value.kind == "form_shape_unqualified"


def test_form_controls_accepts_jsonschema_wrapper_and_rejects_ambiguity():
    schema = {"type": "object", "properties": {"prompt": {"type": "string"},
              "duration": {"type": "integer", "default": 5}}, "required": ["prompt"]}
    wrapped = {"model": "m1", "media": "video", "mode": "text2video", "jsonSchema": schema}
    controls = cli.form_controls(wrapped, model="m1", mode="text2video")
    assert controls["duration"]["default"] == 5
    with pytest.raises(cli.OpenArtCLIError, match="conflicting schema"):
        cli.form_schema(dict(wrapped, schema={"properties": {"duration": {"default": 8}}}),
                        model="m1", mode="text2video")
    with pytest.raises(cli.OpenArtCLIError, match="target model"):
        cli.form_controls(dict(wrapped, model="other"), model="m1", mode="text2video")


def test_offline_quote_status_never_calls(env):
    assert cli.offline_quote_status(None)["status"] == "quote_required"
    ev = {"kind": "model_cost", "model": "m1", "mode": "text2video", "covers_settings": False,
          "request_sha256": "a"}
    status = cli.offline_quote_status(ev, request_sha256="a")
    assert status["status"] == "quote_required" and status["reason"] == "settings_not_covered"
    ok = dict(ev, covers_settings=True)
    assert cli.offline_quote_status(ok, request_sha256="a")["status"] == "retained"
    assert cli.offline_quote_status(ok, request_sha256="b")["status"] == "quote_required"
    assert calls(env) == []


def test_offline_preparation_blocks_cli(env):
    from tools.base_tool import offline_preparation
    with offline_preparation():
        with pytest.raises(cli.OpenArtCLIError) as err:
            cli.run_readonly(["version"])
    assert err.value.kind == "offline_only" and calls(env) == []
    cli.run_readonly(["version"])


def test_account_tool_is_never_governed_generation():
    from lib.production_execution import preflight
    tool = OpenArtAccount()
    assert tool.provider == "openart"
    assert preflight(tool, {"action": "inspect"})["governed"] is False
    schema = json.loads((REPO / "schemas/tools/openart_account.schema.json").read_text())
    assert set(schema["properties"]["action"]["enum"]) >= {"inspect", "quote", "native_dry_run"}


@pytest.mark.parametrize("argv", [
    ["generate", "video", "p", "--model", "m1", "--dry-run", "--dry-run=false"],
    ["generate", "video", "p", "--model", "m1", "--dry-run=false"],
    ["generate", "video", "p", "--model", "m1", "--dry-run=true"],
    ["generate", "video", "p", "--model", "m1", "--dry-run", "--dry-run"],
    ["generate", "video", "p", "--dry-run=false", "--model", "m1", "--dry-run"],
    ["generate", "video", "p", "--model", "m1", "--model", "m2", "--dry-run"],
    ["generate", "video", "p", "--model=m1", "--dry-run"],
    ["generate", "video", "p", "--model", "m1", "--image", "x.png", "--dry-run"],
    ["generate", "video", "p", "--model", "m1", "--", "--dry-run"],
    ["generate", "video", "p", "--model", "m1", "--json=false", "--dry-run"],
    ["generate", "video", "p", "--model", "m1", "-n", "--dry-run"],
    ["generate", "video", "--dry-run"],
    ["model", "form", "m1"], ["model", "form", "m1", "text2video", "--async"],
    ["model", "cost", "--model", "m1"], ["model", "cost", "--mode", "x", "--model", "m1"],
    ["model", "list", "--dry-run=false"], ["creation", "get"], ["creation", "get", "a", "b"],
    ["creation", "list", "--limit", "5"], ["version", "--json=false"], ["account", "extra"],
])
def test_strict_grammar_refuses_overrides_duplicates_and_unknowns(env, argv):
    with pytest.raises(cli.OpenArtCLIError) as err:
        cli.run_readonly(argv)
    assert err.value.kind == "not_read_only"
    assert calls(env) == []


def test_sanctioned_grammar_accepted(env):
    for argv in (["model", "form", "m1", "text2video"], ["model", "cost"], ["creation", "get", "c1"],
                 ["creation", "list"], cli.model_cost_argv("m1", "text2video")):
        cli.run_readonly(argv)
    assert all(c["argv"][-2:] == ["--json", "--no-input"] for c in calls(env))


@pytest.mark.parametrize("parts", [("..",), ("a/b",), ("/abs",), ("",), (".",), ("a", "..")])
def test_private_dir_rejects_traversal(env, parts):
    with pytest.raises(cli.OpenArtCLIError):
        cli.private_dir(*parts)


def test_write_private_rejects_escape_and_completes_short_writes(env, monkeypatch, tmp_path):
    root = cli.state_dir()
    for bad in (root / ".." / "x.json", tmp_path / "x.json", root):
        with pytest.raises(cli.OpenArtCLIError):
            cli.write_private(bad, b"{}")
    real = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real(fd, bytes(data[:3])))
    path = cli.write_private(root / "jobs" / "j.json", b"0123456789")
    assert path.read_bytes() == b"0123456789"
    with pytest.raises(FileExistsError):
        cli.write_private(path, b"x")


def test_native_video_argv_is_dry_run_creative_prefix():
    kw = dict(model="m1", mode="text2video", duration=5, aspect_ratio="9:16", resolution="768p")
    assert cli.native_dry_run_argv("p", **kw) == cli.native_video_argv("p", **kw) + ["--dry-run"]
    cli._check_read_only(cli.native_dry_run_argv("p", **kw))


def test_dry_run_request_digest_from_observed_v011_shape():
    observed = {"endpoint": "POST /api/cli/v1/generate", "body": {
        "model": "__openmontage_contract_probe__", "media": "video", "mode": "text2video",
        "params": {"aspectRatio": "9:16", "duration": 5, "prompt": "p", "resolution": "768p"}}}
    req = cli.dry_run_request(observed)
    assert req["endpoint"].startswith("POST ") and len(req["body_sha256"]) == 64
    reordered = {"body": dict(reversed(list(observed["body"].items()))), "endpoint": observed["endpoint"]}
    assert cli.dry_run_request(reordered)["body_sha256"] == req["body_sha256"]
    with pytest.raises(cli.OpenArtCLIError) as err:
        cli.dry_run_request({"request": {}})
    assert err.value.kind == "dry_run_shape_unqualified"


def test_image2video_without_image_is_not_misrepresented(env):
    with pytest.raises(cli.OpenArtCLIError) as err:
        cli.native_dry_run_argv("p", model="m1", mode="image2video")
    assert err.value.kind == "unsupported_gate"


def test_output_is_captured_to_private_files_not_parent_memory(env, monkeypatch):
    import subprocess as sp
    seen = {}
    real = sp.run
    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)
    monkeypatch.setattr(cli.subprocess, "run", spy)
    monkeypatch.setenv("FAKE_OPENART_MODE", "huge")
    with pytest.raises(cli.OpenArtCLIError):
        cli.run_readonly(["version"])
    assert isinstance(seen["stdout"], int) and isinstance(seen["stderr"], int)
    assert "capture_output" not in seen
    cli.verify_private_state()


def test_nonzero_keeps_redacted_stderr_diagnostic(env, monkeypatch):
    monkeypatch.setenv("FAKE_OPENART_MODE", "nonzero")
    with pytest.raises(cli.OpenArtCLIError) as err:
        cli.run_readonly(["account"])
    public = err.value.public()
    assert "not logged in" in public["diagnostics"]["stderr"] and "sk-secret" not in json.dumps(public)


@pytest.mark.parametrize("bad", [True, "5", float("nan"), float("inf"), -1, 0, 301])
def test_timeout_validated_before_launch(env, bad):
    with pytest.raises(cli.OpenArtCLIError) as err:
        cli.run_readonly(["version"], timeout=bad)
    assert err.value.kind == "invalid_argument"
    assert calls(env) == []


@pytest.mark.parametrize("bad", ["../x", "/abs", "", "20261005T1-zzzzzzzz"])
def test_receipt_id_rejects_paths(env, bad):
    with pytest.raises(cli.OpenArtCLIError):
        cli.receipt_path(bad)


def test_synthetic_surface_probe_fixed_guard_receipt_and_public_shape(env):
    env["binary"].write_text("#!" + sys.executable + '\nimport json,sys\na=sys.argv\nprint(json.dumps({"endpoint":"POST /api/cli/v1/generate","body":{"model":a[a.index("--model")+1],"mode":"image2video","media":"video","params":{"image":a[a.index("--image")+1],"prompt":a[3]}}}))\n')
    result = cli.readonly_transport_surface_probe(model="m1", duration=5)
    assert result["schema_only"] and result["unqualified_for_dispatch"]
    assert result["image_param_key"] == "image"
    assert result["public"]["body_shape"]["params"]["image"] == "string"
    assert "example.invalid" not in json.dumps(result["public"])
    receipt = json.loads(cli.receipt_path(result["receipt_id"]).read_text())
    assert receipt["schema_only"] and receipt["unqualified_for_dispatch"]
    assert receipt["argv"][-3:] == ["--dry-run", "--json", "--no-input"]
    assert receipt["argv"][receipt["argv"].index("--image")+1] == cli._SURFACE_IMAGE
    with pytest.raises(cli.OpenArtCLIError):
        cli.run_readonly(receipt["argv"][:-2])
    assert not cli._SURFACE_PROBE.get()


def test_surface_probe_does_not_allow_caller_image_or_submit(env):
    with pytest.raises(TypeError):
        cli.readonly_transport_surface_probe(model="m1", image_url="https://user.example/x")
    with pytest.raises(cli.OpenArtCLIError):
        cli.check_submit_argv(["generate", "video", cli._SURFACE_PROMPT, "--model", "m1", "--image", cli._SURFACE_IMAGE, "--async"])


def test_failed_synthetic_probe_retains_non_authoritative_private_receipt(env, monkeypatch):
    monkeypatch.setenv("FAKE_OPENART_MODE", "nonzero")
    with pytest.raises(cli.OpenArtCLIError) as err:
        cli.readonly_transport_surface_probe(model="m1")
    diag = err.value.diagnostics
    receipt = json.loads(cli.receipt_path(diag["receipt_id"]).read_text())
    assert receipt["parsed"] is None and receipt["returncode"] == 3
    assert receipt["schema_only"] and receipt["unqualified_for_dispatch"]
    assert hashlib.sha256(cli.receipt_path(diag["receipt_id"]).read_bytes()).hexdigest() == diag["receipt_sha256"]
    assert not cli._SURFACE_PROBE.get()


def test_exact_video_help_retains_text_without_enablement(env):
    env["binary"].write_text("#!" + sys.executable + '\nprint("Usage: openart generate video [options] --image")\n')
    result = cli.readonly_video_help()
    assert result["parsed"]["help"].startswith("Usage:")
    receipt = json.loads(cli.receipt_path(result["receipt_id"]).read_text())
    assert receipt["argv"] == ["generate", "video", "--help", "--json", "--no-input"]
