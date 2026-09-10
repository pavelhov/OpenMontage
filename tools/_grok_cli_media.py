"""Strict subprocess boundary shared by the Grok CLI media adapters.

This module implements a narrow contract around the installed Grok Build CLI.
It requires a minimum version and advertised options. It never drives a browser,
logs in, retries, or selects fallback providers. A successful result requires one
exact native media tool call, a complete semantic NDJSON transcript, and a
locally verified artifact.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from tools.base_tool import ToolResult


MIN_CLI_VERSION = "1.0.18"
PINNED_MODEL = "grok-4.6"
MAX_MEDIA_PROMPT_CHARS = 4096
DEFAULT_GROK_PATH = "grok"


def grok_cli_is_qualified(grok_path: str | None = None) -> bool:
    """Return whether the installed CLI interface and artifact probe are usable.

    This performs only non-generating ``--version`` and ``--help`` checks.
    Authentication and media entitlement remain execute-time concerns.
    """

    configured = grok_path or os.environ.get("GROK_CLI_PATH", DEFAULT_GROK_PATH)
    expanded = Path(configured).expanduser()
    if expanded.is_absolute():
        if not expanded.is_file() or not os.access(expanded, os.X_OK):
            return False
        executable = str(expanded)
    else:
        resolved = shutil.which(configured)
        if not resolved:
            return False
        executable = resolved
    if shutil.which("ffprobe") is None:
        return False
    try:
        _verify_compatibility(executable, cwd=Path.cwd())
    except (GrokCLIContractError, OSError):
        return False
    return True

_RAW_OUTPUT_TYPES = {
    "image_gen": "ImageGen",
    "image_edit": "ImageEdit",
    "image_to_video": "ImageToVideo",
    "reference_to_video": "ReferenceToVideo",
}

_DENY_RULES = (
    "Bash(*)",
    "Edit(*)",
    "Write(*)",
    "Read(*)",
    "Grep(*)",
    "WebFetch(*)",
    "MCPTool(*)",
)

_READ_CLASSIFIED_MEDIA_TOOLS = {
    # Grok 1.0.18 classifies even text-to-image as a read operation.
    # --tools still exposes only the selected native media tool, not Read.
    "image_gen",
    "image_edit",
    "image_to_video",
    "reference_to_video",
}


class GrokCLIContractError(Exception):
    """A classified, non-retryable Grok CLI adapter failure."""

    def __init__(
        self, category: str, message: str, *, dispatch_status: str = "not_dispatched",
        diagnostics: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.category = category
        self.dispatch_status = dispatch_status
        self.diagnostics = diagnostics or {}


def _failure(error: GrokCLIContractError, *, started: float, cli_version: str | None = None) -> ToolResult:
    return ToolResult(
        success=False,
        data={
            "provider": "grok_cli",
            "model": PINNED_MODEL,
            "cli_version": cli_version or error.diagnostics.get("cli_version"),
            "error_category": error.category,
            "dispatch_status": error.dispatch_status,
            "retry_attempted": False,
            "fallback_attempted": False,
            "diagnostics": error.diagnostics,
        },
        error=f"Grok CLI {error.category} error: {error}",
        cost_usd=None if error.dispatch_status != "not_dispatched" else 0.0,  # type: ignore[arg-type]
        duration_seconds=round(time.monotonic() - started, 2),
        model=PINNED_MODEL,
    )


def validate_prompt(prompt: Any) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise GrokCLIContractError("invalid_argument", "prompt is required")
    # Grok CLI occasionally strips a trailing newline from sealed media prompts.
    # Normalize at seal time so the handshake does not reject otherwise-identical
    # successful generations.
    normalized = prompt.rstrip("\n\r")
    if len(normalized) > MAX_MEDIA_PROMPT_CHARS:
        raise GrokCLIContractError(
            "prompt_length",
            f"prompt exceeds the Grok Imagine limit of {MAX_MEDIA_PROMPT_CHARS} characters",
        )
    return normalized


def validate_local_image_paths(values: Any, *, field: str, minimum: int, maximum: int) -> list[str]:
    if isinstance(values, (str, Path)):
        candidates = [str(values)]
    elif isinstance(values, (list, tuple)):
        candidates = [str(value) for value in values]
    else:
        candidates = []
    if not minimum <= len(candidates) <= maximum:
        raise GrokCLIContractError(
            "invalid_argument", f"{field} requires between {minimum} and {maximum} local image path(s)"
        )

    resolved: list[str] = []
    for value in candidates:
        if value.startswith(("http://", "https://", "data:", "file://")):
            raise GrokCLIContractError(
                "invalid_argument", f"{field} accepts local filesystem paths only in this adapter"
            )
        path = Path(value).expanduser()
        try:
            path = path.resolve(strict=True)
        except OSError as exc:
            raise GrokCLIContractError("artifact", f"input image is not readable: {value}") from exc
        if not path.is_file() or path.stat().st_size <= 0:
            raise GrokCLIContractError("artifact", f"input image is empty or not a regular file: {path}")
        resolved.append(str(path))
    return resolved


def _classify_message(message: str, *, dispatched: bool) -> GrokCLIContractError:
    clean = " ".join(message.strip().split()) or "Grok CLI returned an unspecified failure"
    lower = clean.lower()
    dispatch_status = "failed" if dispatched else "not_dispatched"

    if "personal-team-blocked:spending-limit" in lower or "spending limit" in lower or "balance" in lower and "exhaust" in lower:
        return GrokCLIContractError("spending_limit", clean, dispatch_status=dispatch_status)
    if "authentication" in lower or "not logged in" in lower or "run grok login" in lower or "unauthorized" in lower:
        return GrokCLIContractError("auth", clean, dispatch_status=dispatch_status)
    if "supergrok" in lower or "free or x basic" in lower or "free tier" in lower:
        return GrokCLIContractError("tier", clean, dispatch_status=dispatch_status)
    if "zero data retention" in lower or "zdr" in lower or "output storage" in lower or "upload_url" in lower:
        return GrokCLIContractError("zdr_storage", clean, dispatch_status=dispatch_status)
    if "prompt length" in lower or "maximum allowed length of 4096" in lower:
        return GrokCLIContractError("prompt_length", clean, dispatch_status=dispatch_status)
    if "invalid-argument" in lower or "invalid argument" in lower or "bad request" in lower:
        return GrokCLIContractError("invalid_argument", clean, dispatch_status=dispatch_status)
    if "tty" in lower or "interactive" in lower and ("required" in lower or "prompt" in lower):
        return GrokCLIContractError("headless", clean, dispatch_status=dispatch_status)
    if "denied by permission policy" in lower or "deny rule on read" in lower:
        return GrokCLIContractError("permission_policy", clean, dispatch_status=dispatch_status)
    if "tool" in lower and any(word in lower for word in ("unavailable", "unknown", "not found", "disabled")):
        return GrokCLIContractError("capability", clean, dispatch_status=dispatch_status)
    return GrokCLIContractError("cli", clean, dispatch_status=dispatch_status)


_DIAGNOSTIC_LOG_LIMIT = 256 * 1024
_DIAGNOSTIC_EVENTS = {
    "turn_started", "turn_ended", "tool_started", "tool_completed", "tool_call",
    "tool_call_update", "end", "user_message_chunk", "agent_message_chunk",
    "agent_thought_chunk", "mcp_init_completed", "first_token",
}


def _activity_summary(raw: str | bytes | None) -> dict[str, Any]:
    """Summarize bounded partial logs without exposing prompts, args or secrets.

    Absence of a recorded tool call is evidence about logging only, never proof
    that a remote request was not submitted. Accept native NDJSON and persisted
    ACP session updates; ignore incomplete/unknown records.
    """
    data = raw.encode("utf-8", errors="replace") if isinstance(raw, str) else raw or b""
    tail = data[-_DIAGNOSTIC_LOG_LIMIT:]
    counts: dict[str, int] = {}
    last_event: str | None = None
    completed = False
    for line in tail.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            continue
        if not isinstance(event, dict):
            continue
        params = event.get("params")
        if isinstance(params, dict) and isinstance(params.get("update"), dict):
            event = params["update"]
        kind = event.get("type") or event.get("sessionUpdate")
        if not isinstance(kind, str) or kind not in _DIAGNOSTIC_EVENTS:
            continue
        counts[kind] = counts.get(kind, 0) + 1
        last_event = kind
        if kind == "tool_completed" or kind == "tool_call_update" and event.get("status") == "completed":
            completed = True
    called = bool(counts.get("tool_call") or counts.get("tool_started"))
    if completed:
        observed_stage = "tool_completion_recorded"
    elif called:
        observed_stage = "tool_call_recorded"
    elif counts:
        observed_stage = "no_tool_call_recorded"
    else:
        observed_stage = "no_activity_recorded"
    return {
        "bytes_examined": len(tail),
        "truncated": len(data) > len(tail),
        "event_counts": counts,
        "last_recorded_event": last_event,
        "tool_call_observed": called,
        "tool_completion_observed": completed,
        "observed_stage": observed_stage,
    }


def _session_diagnostics(root: Path, cwd: Path, session_id: str) -> dict[str, Any]:
    directory = root.expanduser() / quote(str(cwd), safe="") / session_id
    diagnostics: dict[str, Any] = {"session_id": session_id, "session_directory": str(directory)}
    for name in ("events.jsonl", "updates.jsonl"):
        try:
            # Only inspect this dispatch's known directory, never scan other sessions.
            with (directory / name).open("rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                handle.seek(max(0, size - _DIAGNOSTIC_LOG_LIMIT))
                summary = _activity_summary(handle.read(_DIAGNOSTIC_LOG_LIMIT))
                summary["truncated"] = size > _DIAGNOSTIC_LOG_LIMIT
                diagnostics[name] = summary
        except OSError:
            diagnostics[name] = {"readable": False}
    return diagnostics


def _run_process(argv: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd),
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        if "--prompt-file" in argv:
            process_stage = "media_dispatch"
        elif "--version" in argv:
            process_stage = "version_check"
        elif "--help" in argv:
            process_stage = "compatibility_check"
        else:
            process_stage = "artifact_probe"
        raise GrokCLIContractError(
            "timeout",
            "the process exceeded its hard timeout; recorded activity does not establish remote submission state; the adapter will not retry",
            dispatch_status="not_dispatched" if any(flag in argv for flag in ("--version", "--help")) else "indeterminate",
            diagnostics={
                "timeout_seconds": timeout,
                "process_stage": process_stage,
                "stdout": _activity_summary(exc.stdout),
                "stderr_present": bool(exc.stderr),
            },
        ) from exc
    except FileNotFoundError as exc:
        raise GrokCLIContractError("capability", f"Grok CLI executable not found: {argv[0]}") from exc
    except OSError as exc:
        raise GrokCLIContractError("headless", f"could not start the headless Grok CLI: {exc}") from exc


def _verify_version(grok_path: str, *, cwd: Path) -> str:
    process = _run_process([grok_path, "--version"], cwd=cwd, timeout=10)
    output = "\n".join(part for part in (process.stdout, process.stderr) if part).strip()
    if process.returncode != 0:
        raise _classify_message(output, dispatched=False)
    match = re.search(
        r"(?m)^grok\s+v?((\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)(?=\s|$)",
        output,
    )
    if match is None:
        raise GrokCLIContractError("version", "could not read a Grok CLI release version")
    version = match.group(1)
    release = tuple(int(match.group(i)) for i in (2, 3, 4))
    minimum = tuple(int(part) for part in MIN_CLI_VERSION.split("."))
    if release < minimum or (release == minimum and match.group(5)):
        raise GrokCLIContractError(
            "version",
            f"requires Grok CLI {MIN_CLI_VERSION} or newer; observed {version}",
            diagnostics={"cli_version": version},
        )
    return version


def _verify_compatibility(grok_path: str, *, cwd: Path) -> str:
    """Check the advertised CLI interface, not media entitlement or semantics."""
    version = _verify_version(grok_path, cwd=cwd)
    try:
        process = _run_process([grok_path, "--help"], cwd=cwd, timeout=10)
        if process.returncode != 0:
            raise GrokCLIContractError("capability", "could not inspect Grok CLI options with --help")
        help_text = process.stdout
        headings = list(re.finditer(
            r"(?m)^[ \t]+(?:-[A-Za-z],?[ \t]+)?(--[a-z][a-z0-9-]*)(?=[ \t\n]|$)",
            help_text,
        ))
        options = {
            heading.group(1): help_text[heading.end():headings[index + 1].start() if index + 1 < len(headings) else len(help_text)]
            for index, heading in enumerate(headings)
        }
        # Derive the required option names from the actual dispatch builder so
        # adding a new control cannot silently omit its compatibility check.
        argv = _generation_argv(grok_path, Path("prompt.md"), "image_gen", cwd, session_id="probe")
        required = {arg for arg in argv[1:] if arg.startswith("--")}
        missing = sorted(required - options.keys())
        for option, value in (("--output-format", "streaming-json"), ("--permission-mode", "dontAsk")):
            if option in options and not re.search(r"(?<![\w-])" + re.escape(value) + r"(?![\w-])", options[option]):
                missing.append(f"{option}={value}")
        if missing:
            raise GrokCLIContractError("capability", "Grok CLI lacks required interface: " + ", ".join(missing))
    except GrokCLIContractError as exc:
        exc.diagnostics["cli_version"] = version
        raise
    return version


def _build_instruction(tool_name: str, arguments: dict[str, Any]) -> str:
    serialized = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (
        "Call the native Grok Build tool named "
        f"{json.dumps(tool_name)} exactly once with exactly this JSON object as its arguments:\n"
        f"{serialized}\n"
        "Do not call any other tool. Do not search, browse, use MCP, read or write project files, "
        "run shell commands, ask another agent, alter the arguments, retry, or substitute a capability. "
        "If the exact tool call is unavailable or fails, stop and report that exact failure."
    )


def _generation_argv(
    grok_path: str, prompt_path: Path, tool_name: str, cwd: Path, *, session_id: str | None = None,
) -> list[str]:
    argv = [
        grok_path,
        "--model",
        PINNED_MODEL,
        "--prompt-file",
        str(prompt_path),
        "--output-format",
        "streaming-json",
        "--max-turns",
        "3",
        "--no-subagents",
        "--disable-web-search",
        "--tools",
        tool_name,
        "--disallowed-tools",
        "search_tool,use_tool",
        "--permission-mode",
        "dontAsk",
        "--verbatim",
        "--cwd",
        str(cwd),
    ]
    if session_id is not None:
        argv.extend(("--session-id", session_id))
    for rule in _DENY_RULES:
        if rule == "Read(*)" and tool_name in _READ_CLASSIFIED_MEDIA_TOOLS:
            continue
        argv.extend(("--deny", rule))
    return argv


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_content_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_content_text(item) for item in value)
    return ""


def _normalize_sealed_argument_value(value: Any) -> Any:
    """Normalize values for sealed media-arg equality checks.

    Grok CLI 1.0.13 has been observed to drop a trailing newline from sealed
    prompt strings while still generating the requested artifact. Treat that as
    semantically identical. Keep every other mutation as a hard protocol reject.
    """

    if isinstance(value, str):
        return value.rstrip("\n\r")
    if isinstance(value, list):
        return [_normalize_sealed_argument_value(item) for item in value]
    if isinstance(value, tuple):
        return [_normalize_sealed_argument_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _normalize_sealed_argument_value(item)
            for key, item in value.items()
        }
    return value


def _sealed_arguments_match(observed: Any, expected: dict[str, Any]) -> bool:
    if not isinstance(observed, dict):
        return False
    return _normalize_sealed_argument_value(observed) == _normalize_sealed_argument_value(expected)


def _parse_stream(
    stdout: str, *, tool_name: str, expected_arguments: dict[str, Any]
) -> tuple[Path, float | None, str]:
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GrokCLIContractError(
                "protocol", f"malformed streaming-json at line {line_number}"
            ) from exc
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise GrokCLIContractError("protocol", f"invalid streaming-json event at line {line_number}")
        events.append(event)

    if not events:
        raise GrokCLIContractError("protocol", "Grok CLI produced no streaming-json events")

    tool_calls = [event for event in events if event.get("type") == "tool_call"]
    if len(tool_calls) != 1:
        raise GrokCLIContractError(
            "protocol", f"expected exactly one tool call, observed {len(tool_calls)}"
        )
    tool_call = tool_calls[0]
    observed_tool = tool_call.get("toolName") or tool_call.get("title")
    if observed_tool != tool_name:
        raise GrokCLIContractError(
            "protocol", f"expected tool {tool_name!r}, observed {observed_tool!r}"
        )
    tool_call_id = tool_call.get("toolCallId")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        raise GrokCLIContractError("protocol", "tool call is missing its correlation ID")

    updates = [
        event
        for event in events
        if event.get("type") == "tool_call_update" and event.get("toolCallId") == tool_call_id
    ]
    failed = [event for event in updates if str(event.get("status", "")).lower() == "failed"]
    if failed:
        raise _classify_message(_content_text(failed[-1]), dispatched=True)

    completed = [event for event in updates if str(event.get("status", "")).lower() == "completed"]
    if len(completed) != 1:
        detail = _content_text(updates[-1]) if updates else ""
        if detail:
            classified = _classify_message(detail, dispatched=True)
            if classified.category != "cli":
                raise classified
        raise GrokCLIContractError(
            "protocol", f"expected one completed tool update, observed {len(completed)}",
            dispatch_status="indeterminate",
        )

    if not _sealed_arguments_match(tool_call.get("rawInput"), expected_arguments):
        raise GrokCLIContractError(
            "protocol",
            "Grok changed the sealed media-tool arguments; the artifact was rejected",
            dispatch_status="failed",
        )

    raw_output = completed[0].get("rawOutput")
    expected_type = _RAW_OUTPUT_TYPES[tool_name]
    if not isinstance(raw_output, dict) or raw_output.get("type") != expected_type:
        detail = _content_text(completed[0])
        classified = _classify_message(detail, dispatched=True)
        if classified.category != "cli":
            raise classified
        raise GrokCLIContractError(
            "protocol", f"completed tool update is missing rawOutput type {expected_type!r}",
            dispatch_status="indeterminate",
        )
    raw_path = raw_output.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise GrokCLIContractError("artifact", "completed tool output did not contain an artifact path")

    terminal = [event for event in events if event.get("type") == "end"]
    if len(terminal) != 1:
        raise GrokCLIContractError(
            "protocol", f"expected one terminal end event, observed {len(terminal)}",
            dispatch_status="indeterminate",
        )
    if terminal[0].get("stopReason") != "end_turn":
        raise GrokCLIContractError(
            "protocol", f"Grok CLI ended with {terminal[0].get('stopReason')!r}, not end_turn",
            dispatch_status="indeterminate",
        )

    raw_cost = terminal[0].get("total_cost_usd")
    cost: float | None = None
    if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool):
        candidate = float(raw_cost)
        # Grok's streaming contract uses zero when cost is partial, unknown, or
        # withheld.  A positive finite terminal value is the only exact report.
        if math.isfinite(candidate) and candidate > 0:
            cost = candidate
    return Path(raw_path), cost, str(terminal[0].get("sessionId") or "")


def _trusted_session_artifact(path: Path, sessions_root: Path, session_id: str) -> Path:
    try:
        root = sessions_root.expanduser().resolve(strict=True)
    except OSError as exc:
        raise GrokCLIContractError("artifact", f"Grok sessions root does not exist: {sessions_root}") from exc
    if path.is_symlink():
        raise GrokCLIContractError("artifact", "Grok returned a symbolic-link artifact")
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise GrokCLIContractError("artifact", f"Grok artifact is missing: {path}") from exc
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise GrokCLIContractError(
            "artifact", f"Grok returned an artifact outside the configured sessions root: {resolved}"
        ) from exc
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise GrokCLIContractError("artifact", f"Grok artifact is empty or not a regular file: {resolved}")
    if not session_id or session_id not in relative.parts:
        raise GrokCLIContractError(
            "artifact",
            "Grok artifact path does not match the terminal session ID",
        )
    return resolved


def _probe_artifact(path: Path, *, media_kind: str) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise GrokCLIContractError("artifact", "ffprobe is required to validate Grok media artifacts")
    process = _run_process(
        [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ],
        cwd=path.parent,
        timeout=30,
    )
    if process.returncode != 0:
        raise GrokCLIContractError(
            "artifact", f"ffprobe could not decode the Grok artifact: {(process.stderr or '').strip()}"
        )
    try:
        payload = json.loads(process.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise GrokCLIContractError("artifact", "ffprobe returned malformed JSON") from exc
    streams = payload.get("streams") if isinstance(payload, dict) else None
    video_streams = [
        stream
        for stream in streams or []
        if isinstance(stream, dict) and stream.get("codec_type") == "video"
    ]
    if media_kind == "video":
        video_streams = [
            stream
            for stream in video_streams
            if not (
                isinstance(stream.get("disposition"), dict)
                and stream["disposition"].get("attached_pic") == 1
            )
        ]
    if not video_streams:
        raise GrokCLIContractError("artifact", "artifact has no decodable video/image stream")
    stream = video_streams[0]
    try:
        width = int(stream.get("width", 0))
        height = int(stream.get("height", 0))
    except (TypeError, ValueError) as exc:
        raise GrokCLIContractError("artifact", "artifact dimensions are invalid") from exc
    codec_name = stream.get("codec_name")
    if width <= 0 or height <= 0 or not isinstance(codec_name, str) or not codec_name:
        raise GrokCLIContractError("artifact", "artifact stream has invalid codec or dimensions")

    metadata: dict[str, Any] = {"codec_name": codec_name, "width": width, "height": height}
    if media_kind == "video":
        raw_duration = stream.get("duration")
        if raw_duration is None and isinstance(payload.get("format"), dict):
            raw_duration = payload["format"].get("duration")
        try:
            duration = float(raw_duration)
        except (TypeError, ValueError) as exc:
            raise GrokCLIContractError("artifact", "video artifact has no positive duration") from exc
        if not math.isfinite(duration) or duration <= 0:
            raise GrokCLIContractError("artifact", "video artifact has no positive duration")
        metadata["duration_seconds"] = duration
    return metadata


def _validate_media_contract(
    metadata: dict[str, Any],
    *,
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    """Reject playable artifacts that do not match the sealed request."""

    expected_duration = arguments.get("duration")
    if expected_duration is not None and "duration_seconds" in metadata:
        observed_duration = float(metadata["duration_seconds"])
        if abs(observed_duration - float(expected_duration)) > 0.75:
            raise GrokCLIContractError(
                "artifact",
                f"{tool_name} artifact duration {observed_duration:.3f}s does not match requested {expected_duration}s",
                dispatch_status="failed",
            )

    resolution = arguments.get("resolution_name")
    minimum_short_edge = {"480p": 360, "720p": 640}.get(str(resolution))
    if minimum_short_edge is not None:
        short_edge = min(int(metadata["width"]), int(metadata["height"]))
        if short_edge < minimum_short_edge:
            raise GrokCLIContractError(
                "artifact",
                f"{tool_name} artifact short edge {short_edge}px is below the qualified {resolution} floor",
                dispatch_status="failed",
            )

    aspect_ratio = arguments.get("aspect_ratio")
    if aspect_ratio and aspect_ratio != "auto":
        try:
            numerator, denominator = (float(part) for part in str(aspect_ratio).split(":", 1))
            expected_ratio = numerator / denominator
            observed_ratio = float(metadata["width"]) / float(metadata["height"])
        except (TypeError, ValueError, ZeroDivisionError) as exc:
            raise GrokCLIContractError("invalid_argument", f"invalid aspect ratio: {aspect_ratio}") from exc
        relative_error = abs(observed_ratio - expected_ratio) / expected_ratio
        if relative_error > 0.08:
            raise GrokCLIContractError(
                "artifact",
                f"{tool_name} artifact aspect ratio {observed_ratio:.3f} does not match requested {aspect_ratio}",
                dispatch_status="failed",
            )


def _prepare_output_path(output_path: str) -> Path:
    """Validate the caller's output destination before any media dispatch."""

    target = Path(output_path).expanduser().resolve(strict=False)
    if not output_path or target.name in {"", ".", ".."}:
        raise GrokCLIContractError("invalid_argument", "output_path is required")
    if target.exists() and not target.is_file():
        raise GrokCLIContractError("artifact", f"output path is not a regular file: {target}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, probe_name = tempfile.mkstemp(
            prefix=f".{target.stem}-grok-cli-preflight-",
            suffix=target.suffix,
            dir=target.parent,
        )
        os.close(descriptor)
        Path(probe_name).unlink()
    except OSError as exc:
        raise GrokCLIContractError(
            "artifact", f"output destination is not writable: {target.parent}"
        ) from exc
    return target


def _copy_and_validate(
    source: Path,
    output_path: Path,
    *,
    media_kind: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    output_path = output_path.expanduser().resolve(strict=False)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.stem}-grok-cli-",
        suffix=output_path.suffix,
        dir=output_path.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        if temporary.stat().st_size <= 0:
            raise GrokCLIContractError("artifact", "copied Grok artifact is empty")
        metadata = _probe_artifact(temporary, media_kind=media_kind)
        _validate_media_contract(metadata, tool_name=tool_name, arguments=arguments)
        os.replace(temporary, output_path)
        return metadata
    except GrokCLIContractError:
        raise
    except OSError as exc:
        raise GrokCLIContractError("artifact", f"could not copy the Grok artifact: {exc}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def execute_grok_cli_media(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    output_path: str,
    cwd: str,
    grok_path: str,
    sessions_root: str,
    timeout_seconds: int,
    media_kind: str,
) -> ToolResult:
    """Execute one explicit Grok media primitive and import its artifact."""

    started = time.monotonic()
    prompt_path: Path | None = None
    dispatch_session_id: str | None = None
    cli_version: str | None = None
    media_process_returned = False
    try:
        if tool_name not in _RAW_OUTPUT_TYPES:
            raise GrokCLIContractError("capability", f"unsupported Grok CLI media tool: {tool_name}")
        working_directory = Path(cwd).expanduser().resolve(strict=True)
        if not working_directory.is_dir():
            raise GrokCLIContractError("invalid_argument", f"cwd is not a directory: {cwd}")
        target = _prepare_output_path(output_path)

        # Resolve once so discovery and dispatch use the same PATH selection.
        grok_path = str(Path(grok_path).expanduser())
        resolved = shutil.which(grok_path) if not Path(grok_path).is_absolute() else grok_path
        if resolved:
            grok_path = str(Path(resolved).absolute())
        cli_version = _verify_compatibility(grok_path, cwd=working_directory)
        instruction = _build_instruction(tool_name, arguments)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="openmontage-grok-cli-", suffix=".md", delete=False
        ) as prompt_file:
            prompt_file.write(instruction)
            prompt_path = Path(prompt_file.name)
        os.chmod(prompt_path, 0o600)

        dispatch_session_id = str(uuid4())
        try:
            process = _run_process(
                _generation_argv(grok_path, prompt_path, tool_name, working_directory, session_id=dispatch_session_id),
                cwd=working_directory,
                timeout=timeout_seconds,
            )
            media_process_returned = True
        except GrokCLIContractError as exc:
            if exc.category == "timeout":
                exc.diagnostics.update(_session_diagnostics(Path(sessions_root), working_directory, dispatch_session_id))
            raise
        if process.returncode != 0:
            raise _classify_message(
                "\n".join(part for part in (process.stderr, process.stdout) if part),
                dispatched=True,
            )
        source_path, reported_cost, session_id = _parse_stream(
            process.stdout, tool_name=tool_name, expected_arguments=arguments
        )
        trusted_source = _trusted_session_artifact(source_path, Path(sessions_root), session_id)
        metadata = _copy_and_validate(
            trusted_source,
            target,
            media_kind=media_kind,
            tool_name=tool_name,
            arguments=arguments,
        )

        agent_cost_status = "exact_terminal_report" if reported_cost is not None else "unknown"
        return ToolResult(
            success=True,
            data={
                "provider": "grok_cli",
                "model": PINNED_MODEL,
                "cli_version": cli_version,
                "operation": tool_name,
                "output": str(target.expanduser().resolve(strict=False)),
                "source_artifact": str(trusted_source),
                "session_id": session_id,
                "media_cost_status": "unknown_subscription_media_cost",
                "agent_cost_status": agent_cost_status,
                "agent_cost_usd": reported_cost,
                "retry_attempted": False,
                "fallback_attempted": False,
                **metadata,
            },
            artifacts=[str(target.expanduser().resolve(strict=False))],
            # The terminal dollar field is the coding-agent turn cost. It does
            # not include or qualify the subscription media charge.
            cost_usd=None,  # type: ignore[arg-type] -- None explicitly means unknown, never free.
            duration_seconds=round(time.monotonic() - started, 2),
            model=PINNED_MODEL,
        )
    except GrokCLIContractError as exc:
        if media_process_returned and exc.dispatch_status == "not_dispatched":
            exc.dispatch_status = "indeterminate"
        return _failure(exc, started=started, cli_version=cli_version)
    except (TypeError, ValueError, OSError) as exc:
        error = GrokCLIContractError(
            "invalid_argument", str(exc),
            dispatch_status="indeterminate" if media_process_returned else "not_dispatched",
        )
        return _failure(error, started=started, cli_version=cli_version)
    finally:
        if prompt_path is not None:
            try:
                prompt_path.unlink(missing_ok=True)
            except OSError:
                pass
