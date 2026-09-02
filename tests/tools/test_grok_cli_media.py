"""Offline contract tests for the explicit Grok CLI media adapters.

The real ``grok`` executable is never invoked here.  Every process boundary is
replaced with a deterministic fixture, including version discovery and
``ffprobe`` validation.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tools.base_tool import ToolRuntime, ToolStatus
from tools._grok_cli_media import DEFAULT_GROK_PATH, grok_cli_is_qualified
from tools.graphics.grok_cli_image import GrokCLIImage
from tools.graphics.image_selector import ImageSelector
from tools.video.grok_cli_video import GrokCLIVideo
from tools.video.video_selector import VideoSelector


EXPECTED_NON_READ_DENIES = {
    "Bash(*)",
    "Edit(*)",
    "Write(*)",
    "Grep(*)",
    "WebFetch(*)",
    "MCPTool(*)",
}


def _stream(
    tool_name: str,
    raw_type: str,
    artifact: Path,
    *,
    raw_input: dict[str, Any] | None = None,
    cost: float | None = 0.007,
) -> str:
    events: list[dict[str, Any]] = [
        {
            "type": "tool_call",
            "toolCallId": "call-1",
            "title": "Generate media",
            "toolName": tool_name,
            "rawInput": raw_input or {"prompt": "fixture"},
        },
        {
            "type": "tool_call_update",
            "toolCallId": "call-1",
            "status": "completed",
            "rawOutput": {
                "type": raw_type,
                "path": str(artifact),
                "filename": artifact.name,
                "session_folder": artifact.parent.name,
            },
        },
        {"type": "end", "stopReason": "end_turn", "sessionId": "session-1"},
    ]
    if cost is not None:
        events[-1]["total_cost_usd"] = cost
    return "\n".join(json.dumps(event) for event in events) + "\n"


def _failed_stream(tool_name: str, message: str) -> str:
    events = [
        {
            "type": "tool_call",
            "toolCallId": "call-1",
            "title": "Generate media",
            "toolName": tool_name,
            "rawInput": {"prompt": "fixture"},
        },
        {
            "type": "tool_call_update",
            "toolCallId": "call-1",
            "status": "failed",
            "rawOutput": {"error": "tool_execution_failed", "message": message},
        },
        {"type": "end", "stopReason": "end_turn", "sessionId": "session-1"},
    ]
    return "\n".join(json.dumps(event) for event in events) + "\n"


class FakeProcesses:
    def __init__(
        self,
        *,
        media_stdout: str,
        version: str = "grok 1.0.13 (fixture) [stable]\n",
        media_returncode: int = 0,
        media_stderr: str = "",
        probe: dict[str, Any] | None = None,
        timeout_media: bool = False,
    ) -> None:
        self.media_stdout = media_stdout
        self.version = version
        self.media_returncode = media_returncode
        self.media_stderr = media_stderr
        self.probe = probe or {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 736,
                    "height": 400,
                    "duration": "6.0",
                }
            ],
            "format": {"duration": "6.0"},
        }
        self.timeout_media = timeout_media
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.prompt_payloads: list[str] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), dict(kwargs)))
        if argv[-1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, self.version, "")
        if "--prompt-file" in argv:
            prompt_path = Path(argv[argv.index("--prompt-file") + 1])
            self.prompt_payloads.append(prompt_path.read_text(encoding="utf-8"))
            if self.timeout_media:
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 1))
            return subprocess.CompletedProcess(
                argv,
                self.media_returncode,
                self.media_stdout,
                self.media_stderr,
            )
        return subprocess.CompletedProcess(argv, 0, json.dumps(self.probe), "")

    @property
    def media_calls(self) -> list[tuple[list[str], dict[str, Any]]]:
        return [call for call in self.calls if "--prompt-file" in call[0]]


def _install_fake(monkeypatch: pytest.MonkeyPatch, fake: FakeProcesses) -> FakeProcesses:
    monkeypatch.setattr("tools._grok_cli_media.subprocess.run", fake)
    monkeypatch.setattr("tools._grok_cli_media.shutil.which", lambda name: "/usr/bin/ffprobe")
    return fake


def _artifact(sessions: Path, kind: str, suffix: str) -> Path:
    path = sessions / "encoded-cwd" / "session-1" / kind / f"1{suffix}"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic-media")
    return path


def _common_inputs(tmp_path: Path, sessions: Path, suffix: str) -> dict[str, Any]:
    return {
        "prompt": "A single cinematic fixture shot",
        "output_path": str(tmp_path / f"output{suffix}"),
        "cwd": str(tmp_path),
        "grok_path": str(tmp_path / "fake-grok"),
        "grok_sessions_root": str(sessions),
        "allow_unknown_cost": True,
    }


def _execute_image(inputs: dict[str, Any]):
    tool = GrokCLIImage(
        grok_path=str(inputs["grok_path"]),
        sessions_root=str(inputs["grok_sessions_root"]),
    )
    return tool.execute(inputs)


def _execute_video(inputs: dict[str, Any]):
    tool = GrokCLIVideo(
        grok_path=str(inputs["grok_path"]),
        sessions_root=str(inputs["grok_sessions_root"]),
    )
    return tool.execute(inputs)


def test_provider_contract_is_explicit_paid_cli_not_xai_rest():
    assert DEFAULT_GROK_PATH == "grok"
    for tool in (GrokCLIImage(), GrokCLIVideo()):
        assert tool.provider == "grok_cli"
        assert tool.runtime is ToolRuntime.API
        assert tool.supports["explicit_selection_only"] is True
        assert tool.supports["api_key_required"] is False
        assert tool.resource_profile.network_required is True
        assert tool.fallback_tools == []
        assert tool.dependencies == ["cmd:grok", "cmd:ffprobe"]
        assert "grok_path" not in tool.input_schema["properties"]
        assert "grok_sessions_root" not in tool.input_schema["properties"]
        assert not any(dependency == "env:XAI_API_KEY" for dependency in tool.dependencies)
        with pytest.raises(ValueError, match="unknown"):
            tool.estimate_cost({})

    assert GrokCLIImage.agent_skills == ["grok-media"]
    assert GrokCLIVideo.agent_skills == ["grok-media", "ai-video-gen"]
    assert GrokCLIVideo.supports["text_to_video"] is False
    assert GrokCLIVideo.supports["video_edit"] is False
    assert GrokCLIVideo.supports["upscale"] is False


def test_status_requires_pinned_version_and_ffprobe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    executable = tmp_path / "grok"
    executable.write_text("fixture", encoding="utf-8")
    executable.chmod(0o700)

    fake = FakeProcesses(media_stdout="")
    monkeypatch.setattr("tools._grok_cli_media.subprocess.run", fake)
    monkeypatch.setattr(
        "tools._grok_cli_media.shutil.which",
        lambda name: "/usr/bin/ffprobe" if name == "ffprobe" else None,
    )
    assert grok_cli_is_qualified(str(executable)) is True

    fake.version = "grok 1.0.14\n"
    assert grok_cli_is_qualified(str(executable)) is False

    fake.version = "grok 1.0.13\n"
    monkeypatch.setattr("tools._grok_cli_media.shutil.which", lambda name: None)
    assert grok_cli_is_qualified(str(executable)) is False


@pytest.mark.parametrize(
    ("operation", "raw_type", "source_key"),
    [
        ("image_gen", "ImageGen", None),
        ("image_edit", "ImageEdit", "image_paths"),
    ],
)
def test_image_success_shapes_and_exact_headless_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    raw_type: str,
    source_key: str | None,
):
    sessions = tmp_path / "sessions"
    inputs = _common_inputs(tmp_path, sessions, ".jpg") | {"operation": operation, "aspect_ratio": "9:16"}
    raw_input: dict[str, Any] = {
        "prompt": inputs["prompt"],
        "aspect_ratio": "9:16",
    }
    if source_key:
        source = tmp_path / "source.jpg"
        source.write_bytes(b"source-image")
        inputs[source_key] = [str(source)]
        raw_input["image"] = [str(source.resolve())]
    artifact = _artifact(sessions, "images", ".jpg")
    fake = _install_fake(
        monkeypatch,
        FakeProcesses(
            media_stdout=_stream(operation, raw_type, artifact, raw_input=raw_input),
            probe={
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "mjpeg",
                        "width": 720,
                        "height": 1280,
                    }
                ],
                "format": {},
            },
        ),
    )

    result = _execute_image(inputs)

    assert result.success, result.error
    assert result.artifacts == [str(tmp_path / "output.jpg")]
    assert result.cost_usd is None
    assert result.data["media_cost_status"] == "unknown_subscription_media_cost"
    assert result.data["agent_cost_usd"] == pytest.approx(0.007)
    assert result.data["agent_cost_status"] == "exact_terminal_report"
    assert len(fake.media_calls) == 1
    argv, kwargs = fake.media_calls[0]
    assert argv[0] == str(tmp_path / "fake-grok")
    assert argv[1:3] == ["--model", "grok-4.6"]
    assert argv[argv.index("--output-format") + 1] == "streaming-json"
    assert argv[argv.index("--max-turns") + 1] == "3"
    assert argv[argv.index("--tools") + 1] == operation
    assert argv[argv.index("--disallowed-tools") + 1] == "search_tool,use_tool"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--no-subagents" in argv and "--disable-web-search" in argv and "--verbatim" in argv
    deny_values = {argv[index + 1] for index, value in enumerate(argv) if value == "--deny"}
    expected_denies = EXPECTED_NON_READ_DENIES | ({"Read(*)"} if operation == "image_gen" else set())
    assert deny_values == expected_denies
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["shell"] is False
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["text"] is True
    assert json.dumps(operation) in fake.prompt_payloads[0]


@pytest.mark.parametrize(
    ("operation", "raw_type"),
    [
        ("image_to_video", "ImageToVideo"),
        ("reference_to_video", "ReferenceToVideo"),
    ],
)
def test_video_success_shapes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    raw_type: str,
):
    sessions = tmp_path / "sessions"
    source = tmp_path / "source.jpg"
    source.write_bytes(b"source-image")
    inputs = _common_inputs(tmp_path, sessions, ".mp4") | {
        "operation": operation,
        "resolution": "480p",
        "duration": 6,
    }
    if operation == "image_to_video":
        inputs["image_path"] = str(source)
        raw_input = {
            "prompt": inputs["prompt"],
            "image": str(source.resolve()),
            "duration": 6,
            "resolution_name": "480p",
        }
    else:
        inputs["reference_image_paths"] = [str(source)]
        inputs["aspect_ratio"] = "9:16"
        raw_input = {
            "prompt": inputs["prompt"],
            "images": [str(source.resolve())],
            "aspect_ratio": "9:16",
            "duration": 6,
            "resolution_name": "480p",
        }
    artifact = _artifact(sessions, "videos", ".mp4")
    fake = _install_fake(
        monkeypatch,
        FakeProcesses(
            media_stdout=_stream(operation, raw_type, artifact, raw_input=raw_input),
            probe=(
                {
                    "streams": [
                        {
                            "codec_type": "video",
                            "codec_name": "h264",
                            "width": 720,
                            "height": 1280,
                            "duration": "6.0",
                        }
                    ],
                    "format": {"duration": "6.0"},
                }
                if operation == "reference_to_video"
                else None
            ),
        ),
    )

    result = _execute_video(inputs)

    assert result.success, result.error
    if operation == "reference_to_video":
        assert result.data["width"] == 720
        assert result.data["height"] == 1280
    else:
        assert result.data["width"] == 736
        assert result.data["height"] == 400
    assert result.data["duration_seconds"] == pytest.approx(6.0)
    assert result.data["codec_name"] == "h264"
    assert len(fake.media_calls) == 1
    argv, _ = fake.media_calls[0]
    deny_values = {argv[index + 1] for index, value in enumerate(argv) if value == "--deny"}
    assert deny_values == EXPECTED_NON_READ_DENIES


def test_unknown_terminal_cost_is_not_reported_as_free(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    sessions = tmp_path / "sessions"
    artifact = _artifact(sessions, "images", ".jpg")
    inputs = _common_inputs(tmp_path, sessions, ".jpg")
    raw_input = {"prompt": inputs["prompt"], "aspect_ratio": "auto"}
    fake = FakeProcesses(
        media_stdout=_stream("image_gen", "ImageGen", artifact, raw_input=raw_input, cost=None)
    )
    _install_fake(monkeypatch, fake)

    result = _execute_image(inputs)

    assert result.success
    assert result.cost_usd is None
    assert result.data["media_cost_status"] == "unknown_subscription_media_cost"
    assert result.data["agent_cost_status"] == "unknown"


def test_image_adapter_preserves_selector_generation_mode_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    sessions = tmp_path / "sessions"
    source = tmp_path / "source.jpg"
    source.write_bytes(b"source-image")
    artifact = _artifact(sessions, "images", ".jpg")
    inputs = _common_inputs(tmp_path, sessions, ".jpg") | {
        "generation_mode": "edit",
        "image_paths": [str(source)],
        "aspect_ratio": "16:9",
    }
    raw_input = {
        "prompt": inputs["prompt"],
        "image": [str(source.resolve())],
        "aspect_ratio": "16:9",
    }
    fake = FakeProcesses(
        media_stdout=_stream("image_edit", "ImageEdit", artifact, raw_input=raw_input)
    )
    _install_fake(monkeypatch, fake)

    result = _execute_image(inputs)

    assert result.success, result.error
    assert fake.media_calls[0][0][fake.media_calls[0][0].index("--tools") + 1] == "image_edit"


@pytest.mark.parametrize(
    ("generation_mode", "native_tool"),
    [("generate", "image_gen"), ("edit", "image_edit")],
)
def test_image_adapter_accepts_selector_generate_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    generation_mode: str,
    native_tool: str,
):
    sessions = tmp_path / "sessions"
    artifact = _artifact(sessions, "images", ".jpg")
    inputs = _common_inputs(tmp_path, sessions, ".jpg") | {
        "operation": "generate",
        "generation_mode": generation_mode,
    }
    raw_input: dict[str, Any] = {"prompt": inputs["prompt"], "aspect_ratio": "auto"}
    raw_type = "ImageGen"
    if generation_mode == "edit":
        source = tmp_path / "source.jpg"
        source.write_bytes(b"source-image")
        inputs["image_paths"] = [str(source)]
        raw_input["image"] = [str(source.resolve())]
        raw_type = "ImageEdit"
    fake = FakeProcesses(
        media_stdout=_stream(native_tool, raw_type, artifact, raw_input=raw_input)
    )
    _install_fake(monkeypatch, fake)

    result = _execute_image(inputs)

    assert result.success, result.error
    assert fake.media_calls[0][0][fake.media_calls[0][0].index("--tools") + 1] == native_tool


def test_image_selector_executes_real_grok_cli_adapter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    sessions = tmp_path / "sessions"
    artifact = _artifact(sessions, "images", ".jpg")
    executable = tmp_path / "fake-grok"
    executable.write_text("fixture", encoding="utf-8")
    executable.chmod(0o700)
    output = tmp_path / "selector-output.jpg"
    raw_input = {"prompt": "A selector-routed fixture", "aspect_ratio": "auto"}
    fake = FakeProcesses(
        media_stdout=_stream("image_gen", "ImageGen", artifact, raw_input=raw_input)
    )
    _install_fake(monkeypatch, fake)
    provider = GrokCLIImage(grok_path=str(executable), sessions_root=str(sessions))
    selector = ImageSelector()
    selector._providers = lambda: [provider]  # type: ignore[assignment]

    result = selector.execute(
        {
            "prompt": "A selector-routed fixture",
            "operation": "generate",
            "generation_mode": "generate",
            "preferred_provider": "grok_cli",
            "allowed_providers": ["grok_cli"],
            "output_path": str(output),
            "cwd": str(tmp_path),
            "allow_unknown_cost": True,
        }
    )

    assert result.success, result.error
    assert result.data["selected_tool"] == "grok_cli_image"
    assert result.data["selected_provider"] == "grok_cli"
    assert len(fake.media_calls) == 1


def test_selectors_report_exact_pinned_grok_cli_rank_preflights_offline(
    monkeypatch: pytest.MonkeyPatch,
):
    """Exact rank pins are metadata-only and never start a Grok media call."""
    image_provider = GrokCLIImage()
    video_provider = GrokCLIVideo()
    monkeypatch.setattr(image_provider, "get_status", lambda: ToolStatus.UNAVAILABLE)
    monkeypatch.setattr(video_provider, "get_status", lambda: ToolStatus.UNAVAILABLE)
    monkeypatch.setattr(
        "lib.scoring.rank_providers",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not score")),
    )

    image_selector = ImageSelector()
    image_selector._providers = lambda: [image_provider]  # type: ignore[assignment]
    image_result = image_selector.execute({
        "prompt": "offline rank fixture",
        "operation": "rank",
        "preferred_provider": "grok_cli",
        "allowed_providers": ["grok_cli"],
    })

    video_selector = VideoSelector()
    video_selector._providers = lambda: [video_provider]  # type: ignore[assignment]
    video_result = video_selector.execute({
        "prompt": "offline rank fixture",
        "operation": "rank",
        "target_operation": "image_to_video",
        "preferred_provider": "grok_cli",
        "allowed_providers": ["grok_cli"],
    })

    for result, tool_name in ((image_result, "grok_cli_image"), (video_result, "grok_cli_video")):
        assert result.success, result.error
        row = result.data["rankings"][0]
        assert row["tool_name"] == tool_name
        assert row["weighted_score"] is None
        assert row["selection_mode"] == "explicit_pin"
        assert row["cost_estimate_status"] == "unknown"
        assert row["estimated_cost_usd"] is None
        assert row["status"] == str(ToolStatus.UNAVAILABLE)


def test_video_selector_rank_rejects_unsupported_grok_cli_text_to_video(
    monkeypatch: pytest.MonkeyPatch,
):
    provider = GrokCLIVideo()
    monkeypatch.setattr(provider, "get_status", lambda: ToolStatus.UNAVAILABLE)
    monkeypatch.setattr(
        provider,
        "execute",
        lambda inputs: (_ for _ in ()).throw(AssertionError("must not execute")),
    )
    monkeypatch.setattr(
        "lib.scoring.rank_providers",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not score")),
    )
    selector = VideoSelector()
    selector._providers = lambda: [provider]  # type: ignore[assignment]

    result = selector.execute({
        "prompt": "offline rank fixture",
        "operation": "rank",
        "target_operation": "text_to_video",
        "preferred_provider": "grok_cli",
        "allowed_providers": ["grok_cli"],
    })

    assert result.success, result.error
    assert result.data["rankings"] == []


def test_video_adapter_preserves_selector_reference_image_path_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    sessions = tmp_path / "sessions"
    source = tmp_path / "source.jpg"
    source.write_bytes(b"source-image")
    artifact = _artifact(sessions, "videos", ".mp4")
    inputs = _common_inputs(tmp_path, sessions, ".mp4") | {
        "operation": "image_to_video",
        "reference_image_path": str(source),
        "duration": 6,
        "resolution": "480p",
    }
    raw_input = {
        "prompt": inputs["prompt"],
        "image": str(source.resolve()),
        "duration": 6,
        "resolution_name": "480p",
    }
    fake = FakeProcesses(
        media_stdout=_stream("image_to_video", "ImageToVideo", artifact, raw_input=raw_input)
    )
    _install_fake(monkeypatch, fake)

    result = _execute_video(inputs)

    assert result.success, result.error


def test_artifact_must_match_terminal_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    sessions = tmp_path / "sessions"
    artifact = _artifact(sessions, "images", ".jpg")
    wrong_session = sessions / "encoded-cwd" / "session-2" / "images" / "1.jpg"
    wrong_session.parent.mkdir(parents=True)
    wrong_session.write_bytes(artifact.read_bytes())
    inputs = _common_inputs(tmp_path, sessions, ".jpg")
    raw_input = {"prompt": inputs["prompt"], "aspect_ratio": "auto"}
    fake = FakeProcesses(
        media_stdout=_stream("image_gen", "ImageGen", wrong_session, raw_input=raw_input)
    )
    _install_fake(monkeypatch, fake)

    result = _execute_image(inputs)

    assert not result.success
    assert result.data["error_category"] == "artifact"
    assert "session" in result.error.lower()


def test_version_mismatch_fails_before_media_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    fake = _install_fake(monkeypatch, FakeProcesses(media_stdout="", version="grok 1.0.14\n"))
    inputs = _common_inputs(tmp_path, tmp_path / "sessions", ".jpg")
    result = _execute_image(inputs)
    assert not result.success
    assert "version" in result.error.lower() and "1.0.13" in result.error
    assert fake.media_calls == []


def test_prompt_4097_fails_before_any_subprocess(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    def no_process(*args: Any, **kwargs: Any):
        raise AssertionError("preflight must not invoke a subprocess")

    monkeypatch.setattr("tools._grok_cli_media.subprocess.run", no_process)
    inputs = _common_inputs(tmp_path, tmp_path / "sessions", ".jpg") | {"prompt": "x" * 4097}
    result = _execute_image(inputs)
    assert not result.success
    assert "4096" in result.error


@pytest.mark.parametrize(
    ("message", "category"),
    [
        ('HTTP 403 {"code":"personal-team-blocked:spending-limit"}', "spending_limit"),
        ('HTTP 400 {"code":"invalid-argument","error":"bad duration"}', "invalid_argument"),
        ("authentication required; run grok login", "auth"),
        ("Video generation is a SuperGrok feature and isn't available on the free tier", "tier"),
        ("unavailable under zero data retention (ZDR); output storage bucket required", "zdr_storage"),
        ("native tool image_gen is unavailable", "capability"),
        ("cannot prompt because no TTY is available", "headless"),
        ("Denied by permission policy: deny rule on read", "permission_policy"),
    ],
)
def test_semantic_failures_are_classified(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    message: str,
    category: str,
):
    fake = FakeProcesses(media_stdout=_failed_stream("image_gen", message))
    _install_fake(monkeypatch, fake)
    inputs = _common_inputs(tmp_path, tmp_path / "sessions", ".jpg")
    result = _execute_image(inputs)
    assert not result.success
    assert result.data["error_category"] == category
    assert len(fake.media_calls) == 1


def test_nonzero_cli_exit_is_terminal_and_not_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    fake = FakeProcesses(
        media_stdout="",
        media_returncode=1,
        media_stderr="authentication required; run grok login",
    )
    _install_fake(monkeypatch, fake)
    inputs = _common_inputs(tmp_path, tmp_path / "sessions", ".jpg")

    result = _execute_image(inputs)

    assert not result.success
    assert result.data["error_category"] == "auth"
    assert result.data["retry_attempted"] is False
    assert len(fake.media_calls) == 1


def test_subprocess_timeout_is_indeterminate_and_never_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    fake = FakeProcesses(media_stdout="", timeout_media=True)
    _install_fake(monkeypatch, fake)
    inputs = _common_inputs(tmp_path, tmp_path / "sessions", ".jpg")
    result = _execute_image(inputs)
    assert not result.success
    assert result.data["error_category"] == "timeout"
    assert result.data["dispatch_status"] == "indeterminate"
    assert len(fake.media_calls) == 1


@pytest.mark.parametrize("bad_stream", ["not-json\n", json.dumps({"type": "text", "data": "done"}) + "\n"])
def test_malformed_or_exit_zero_prose_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad_stream: str
):
    fake = FakeProcesses(media_stdout=bad_stream)
    _install_fake(monkeypatch, fake)
    inputs = _common_inputs(tmp_path, tmp_path / "sessions", ".jpg")
    result = _execute_image(inputs)
    assert not result.success
    assert result.data["error_category"] == "protocol"


def test_wrong_or_multiple_tool_calls_are_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    sessions = tmp_path / "sessions"
    artifact = _artifact(sessions, "images", ".jpg")
    wrong = _stream("image_edit", "ImageEdit", artifact)
    fake = FakeProcesses(media_stdout=wrong)
    _install_fake(monkeypatch, fake)
    inputs = _common_inputs(tmp_path, sessions, ".jpg")
    result = _execute_image(inputs)
    assert not result.success and result.data["error_category"] == "protocol"

    duplicate_lines = _stream("image_gen", "ImageGen", artifact).splitlines()
    duplicate_lines.insert(1, duplicate_lines[0])
    fake.media_stdout = "\n".join(duplicate_lines) + "\n"
    result = _execute_image(inputs)
    assert not result.success and result.data["error_category"] == "protocol"


def test_changed_tool_arguments_are_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    sessions = tmp_path / "sessions"
    artifact = _artifact(sessions, "images", ".jpg")
    inputs = _common_inputs(tmp_path, sessions, ".jpg")
    stdout = _stream(
        "image_gen",
        "ImageGen",
        artifact,
        raw_input={"prompt": "a silently changed prompt", "aspect_ratio": "auto"},
    )
    fake = FakeProcesses(media_stdout=stdout)
    _install_fake(monkeypatch, fake)

    result = _execute_image(inputs)

    assert not result.success
    assert result.data["error_category"] == "protocol"
    assert "changed" in result.error.lower()

@pytest.mark.parametrize("artifact_case", ["missing_path", "outside_root", "missing_file"])
def test_missing_or_untrusted_artifact_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, artifact_case: str
):
    sessions = tmp_path / "sessions"
    artifact = _artifact(sessions, "images", ".jpg")
    inputs = _common_inputs(tmp_path, sessions, ".jpg")
    raw_input = {"prompt": inputs["prompt"], "aspect_ratio": "auto"}
    stdout = _stream("image_gen", "ImageGen", artifact, raw_input=raw_input)
    if artifact_case == "missing_path":
        events = [json.loads(line) for line in stdout.splitlines()]
        events[1]["rawOutput"].pop("path")
        stdout = "\n".join(json.dumps(event) for event in events) + "\n"
    elif artifact_case == "outside_root":
        outside = tmp_path / "outside.jpg"
        outside.write_bytes(b"outside")
        stdout = stdout.replace(str(artifact), str(outside))
    else:
        artifact.unlink()
    fake = FakeProcesses(media_stdout=stdout)
    _install_fake(monkeypatch, fake)
    result = _execute_image(inputs)
    assert not result.success
    assert result.data["error_category"] == "artifact"


def test_image_requires_a_decodable_stream(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    sessions = tmp_path / "sessions"
    artifact = _artifact(sessions, "images", ".jpg")
    inputs = _common_inputs(tmp_path, sessions, ".jpg")
    raw_input = {"prompt": inputs["prompt"], "aspect_ratio": "auto"}
    fake = FakeProcesses(
        media_stdout=_stream("image_gen", "ImageGen", artifact, raw_input=raw_input),
        probe={"streams": [], "format": {}},
    )
    _install_fake(monkeypatch, fake)

    result = _execute_image(inputs)

    assert not result.success
    assert result.data["error_category"] == "artifact"


def test_invalid_output_destination_fails_before_media_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    target_directory = tmp_path / "output.jpg"
    target_directory.mkdir()
    inputs = _common_inputs(tmp_path, tmp_path / "sessions", ".jpg")
    inputs["output_path"] = str(target_directory)
    fake = FakeProcesses(media_stdout="")
    _install_fake(monkeypatch, fake)

    result = _execute_image(inputs)

    assert not result.success
    assert result.data["error_category"] == "artifact"
    assert fake.media_calls == []


def test_video_requires_playable_video_stream(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    sessions = tmp_path / "sessions"
    artifact = _artifact(sessions, "videos", ".mp4")
    fake = FakeProcesses(
        media_stdout="",
        probe={"streams": [{"codec_type": "audio", "codec_name": "aac"}], "format": {}},
    )
    _install_fake(monkeypatch, fake)
    source = tmp_path / "source.jpg"
    source.write_bytes(b"source")
    inputs = _common_inputs(tmp_path, sessions, ".mp4") | {
        "operation": "image_to_video",
        "image_path": str(source),
        "duration": 6,
    }
    fake.media_stdout = _stream(
        "image_to_video",
        "ImageToVideo",
        artifact,
        raw_input={
            "prompt": inputs["prompt"],
            "image": str(source.resolve()),
            "duration": 6,
            "resolution_name": "480p",
        },
    )
    result = _execute_video(inputs)
    assert not result.success
    assert result.data["error_category"] == "artifact"


def test_video_rejects_attached_cover_art_as_its_only_video_stream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    sessions = tmp_path / "sessions"
    artifact = _artifact(sessions, "videos", ".mp4")
    fake = FakeProcesses(
        media_stdout="",
        probe={
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "mjpeg",
                    "width": 736,
                    "height": 400,
                    "disposition": {"attached_pic": 1},
                },
                {"codec_type": "audio", "codec_name": "aac"},
            ],
            "format": {"duration": "6.0"},
        },
    )
    _install_fake(monkeypatch, fake)
    source = tmp_path / "source.jpg"
    source.write_bytes(b"source")
    inputs = _common_inputs(tmp_path, sessions, ".mp4") | {
        "operation": "image_to_video",
        "image_path": str(source),
        "duration": 6,
    }
    fake.media_stdout = _stream(
        "image_to_video",
        "ImageToVideo",
        artifact,
        raw_input={
            "prompt": inputs["prompt"],
            "image": str(source.resolve()),
            "duration": 6,
            "resolution_name": "480p",
        },
    )

    result = _execute_video(inputs)

    assert not result.success
    assert result.data["error_category"] == "artifact"


@pytest.mark.parametrize(
    ("probe", "operation", "extra"),
    [
        (
            {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 736, "height": 400, "duration": "3.0"}], "format": {"duration": "3.0"}},
            "image_to_video",
            {},
        ),
        (
            {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 320, "height": 180, "duration": "6.0"}], "format": {"duration": "6.0"}},
            "image_to_video",
            {"resolution": "720p"},
        ),
        (
            {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720, "duration": "6.0"}], "format": {"duration": "6.0"}},
            "reference_to_video",
            {"aspect_ratio": "9:16"},
        ),
    ],
)
def test_video_rejects_artifacts_outside_sealed_media_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    probe: dict[str, Any],
    operation: str,
    extra: dict[str, Any],
):
    sessions = tmp_path / "sessions"
    source = tmp_path / "source.jpg"
    source.write_bytes(b"source")
    artifact = _artifact(sessions, "videos", ".mp4")
    inputs = _common_inputs(tmp_path, sessions, ".mp4") | {
        "operation": operation,
        "duration": 6,
        "resolution": "480p",
        **extra,
    }
    if operation == "image_to_video":
        inputs["image_path"] = str(source)
        raw_input = {
            "prompt": inputs["prompt"],
            "image": str(source.resolve()),
            "duration": 6,
            "resolution_name": inputs["resolution"],
        }
    else:
        inputs["reference_image_paths"] = [str(source)]
        raw_input = {
            "prompt": inputs["prompt"],
            "images": [str(source.resolve())],
            "duration": 6,
            "resolution_name": inputs["resolution"],
            "aspect_ratio": inputs["aspect_ratio"],
        }
    fake = FakeProcesses(
        media_stdout=_stream(
            operation,
            "ImageToVideo" if operation == "image_to_video" else "ReferenceToVideo",
            artifact,
            raw_input=raw_input,
        ),
        probe=probe,
    )
    _install_fake(monkeypatch, fake)

    result = _execute_video(inputs)

    assert not result.success
    assert result.data["error_category"] == "artifact"


def test_image_edit_rejects_more_than_five_references_before_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    def no_process(*args: Any, **kwargs: Any):
        raise AssertionError("reference validation must stop before subprocess")

    monkeypatch.setattr("tools._grok_cli_media.subprocess.run", no_process)
    sources = []
    for index in range(6):
        source = tmp_path / f"source-{index}.jpg"
        source.write_bytes(b"source")
        sources.append(str(source))
    inputs = _common_inputs(tmp_path, tmp_path / "sessions", ".jpg") | {
        "operation": "image_edit",
        "image_paths": sources,
    }
    result = _execute_image(inputs)

    assert not result.success
    assert result.data["error_category"] == "invalid_argument"


@pytest.mark.parametrize("operation", ["text_to_video", "video_edit", "upscale"])
def test_unsupported_video_capabilities_fail_without_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operation: str
):
    def no_process(*args: Any, **kwargs: Any):
        raise AssertionError("unsupported capability must not invoke a subprocess")

    monkeypatch.setattr("tools._grok_cli_media.subprocess.run", no_process)
    inputs = _common_inputs(tmp_path, tmp_path / "sessions", ".mp4") | {"operation": operation}
    result = _execute_video(inputs)
    assert not result.success
    assert result.data["error_category"] == "capability"


def test_dry_run_never_invokes_subprocess(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    def no_process(*args: Any, **kwargs: Any):
        raise AssertionError("dry_run must not invoke a subprocess")

    monkeypatch.setattr("tools._grok_cli_media.subprocess.run", no_process)
    inputs = _common_inputs(tmp_path, tmp_path / "sessions", ".jpg")
    inputs["allow_unknown_cost"] = False
    info = GrokCLIImage().dry_run(inputs)
    assert info["would_execute"] is False
    assert info["paid_submission"] is False
    assert info["cost_estimate_status"] == "unknown_subscription_media_cost"
    assert info["model"] == "grok-4.6"


def test_unknown_cost_requires_explicit_approval_before_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    def no_process(*args: Any, **kwargs: Any):
        raise AssertionError("missing unknown-cost approval must stop before subprocess")

    monkeypatch.setattr("tools._grok_cli_media.subprocess.run", no_process)
    inputs = _common_inputs(tmp_path, tmp_path / "sessions", ".jpg")
    inputs["allow_unknown_cost"] = False
    result = _execute_image(inputs)
    assert not result.success
    assert result.data["error_category"] == "spending_approval"

    inputs["allow_unknown_cost"] = True
    dry = GrokCLIImage().dry_run(inputs)
    assert dry["would_execute"] is True
    assert dry["paid_submission"] is False
