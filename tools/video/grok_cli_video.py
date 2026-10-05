"""Explicit Grok CLI OAuth/subscription image-to-video provider.

The CLI exposes image-first primitives only. Direct text-to-video, video
editing, and upscaling fail closed instead of being emulated or silently sent
to the existing xAI REST provider.

Pinned first/last frames and mid-clip keyframes require Grok CLI 1.0.34+.
They dispatch through native ``reference_to_video`` fields, not REST.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from lib.shot_contract import file_sha256
from tools._grok_cli_media import (
    DEFAULT_GROK_PATH,
    FRAME_PIN_MIN_CLI_VERSION,
    MIN_CLI_VERSION,
    PINNED_MODEL,
    MODEL_PROVENANCE,
    GrokCLIContractError,
    execute_grok_cli_media,
    grok_cli_is_qualified,
    validate_local_image_paths,
    validate_prompt,
    validate_session_id,
)
from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)


_REFERENCE_ASPECT_RATIOS = {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"}
_PINNED_FRAME_OPERATIONS = frozenset({"reference_to_video", "first_last_frame"})
_UNSUPPORTED_FRAME_ALIASES = frozenset({
    "last_frame_url",
    "last_frame_path",
    "end_frame",
    "end_frame_url",
    "end_frame_path",
    "loop",
    "seamless_loop",
})
_URL_FINAL_FRAME_KEYS = frozenset({"last_image_url"})


class GrokCLIVideo(BaseTool):
    name = "grok_cli_video"
    version = "0.4.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "grok_cli"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["cmd:grok", "cmd:ffprobe"]
    install_instructions = (
        f"Install Grok CLI {MIN_CLI_VERSION} or newer on PATH (or set GROK_CLI_PATH), "
        "then sign in interactively with `grok login`. This adapter never initiates login. "
        f"Pinned first/last frames and keyframes require CLI {FRAME_PIN_MIN_CLI_VERSION}+."
    )
    agent_skills = ["grok-media", "ai-video-gen"]

    capabilities = ["image_to_video", "reference_to_video", "first_last_frame"]
    supports = {
        "text_to_video": False,
        "image_to_video": True,
        "reference_to_video": True,
        "first_last_frame": True,
        "video_edit": False,
        "upscale": False,
        "reference_image": True,
        "multiple_reference_images": True,
        "native_audio": True,
        "preset_voices": True,
        "explicit_selection_only": True,
        "oauth_session_auth": True,
        "api_key_required": False,
        "browser_automation": False,
        "silent_fallback": False,
        "cost_preestimate": False,
    }
    best_for = [
        "explicit animation of one prepared first-frame image",
        "explicit multi-reference short video through an existing Grok CLI OAuth session",
        "local first/last-frame pins and mid-clip keyframes on CLI 1.0.34+",
    ]
    not_good_for = [
        "direct text-to-video",
        "video editing or post-generation upscaling",
        "offline or free generation",
        "implicit provider selection",
        "safe automatic retry after a timeout",
        "URL-based final frames or a native seamless-loop switch",
        "explicit Imagine Image 2.0 / Video 1.5 model selection",
    ]
    fallback_tools: list[str] = []

    input_schema = {
        "type": "object",
        "required": ["prompt", "operation", "output_path"],
        "additionalProperties": False,
        "properties": {
            "prompt": {"type": "string", "maxLength": 4096},
            "operation": {
                "type": "string",
                "enum": ["image_to_video", "reference_to_video", "first_last_frame"],
            },
            "image_path": {
                "type": "string",
                "description": "image_to_video source, or first_last_frame / reference_to_video first_frame alias.",
            },
            "first_frame": {
                "type": "string",
                "description": "Native CLI first-frame path for reference_to_video / first_last_frame.",
            },
            "last_image_path": {
                "type": "string",
                "description": "Local final frame for first_last_frame / reference_to_video. CLI 1.0.34+.",
            },
            "last_frame": {
                "type": "string",
                "description": "Native CLI last-frame path alias for last_image_path. CLI 1.0.34+.",
            },
            "keyframes": {
                "type": "array",
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "required": ["image", "timestamp_s"],
                    "properties": {
                        "image": {"type": "string"},
                        "timestamp_s": {"type": "number", "exclusiveMinimum": 0},
                    },
                    "additionalProperties": False,
                },
                "description": "Up to 4 mid-clip literal frame anchors. CLI 1.0.34+.",
            },
            "reference_image_path": {
                "type": "string",
                "description": "Selector-compatible first-frame alias for image_path / first_frame.",
            },
            "reference_image_paths": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 14,
            },
            "voices": {
                "type": "array", "items": {"type": "string", "minLength": 1},
                "maxItems": 3,
                "description": "reference_to_video / first_last_frame only: preset voice IDs; CLI >=1.0.25.",
            },
            "duration": {"type": "integer", "minimum": 1, "maximum": 15, "default": 6},
            "resolution": {"type": "string", "enum": ["480p", "720p"], "default": "480p"},
            "aspect_ratio": {
                "type": "string",
                "enum": sorted(_REFERENCE_ASPECT_RATIOS),
                "default": "16:9",
            },
            "endpoint_requirement_id": {
                "type": "string",
                "minLength": 1,
                "description": "OpenMontage provenance only; not sent to the native CLI tool.",
            },
            "output_path": {"type": "string"},
            "cwd": {"type": "string"},
            "cli_session_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"},
            "allow_unknown_cost": {
                "type": "boolean",
                "default": False,
                "description": "Explicit approval to dispatch despite unknown CLI media pricing.",
            },
            "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 900, "default": 600},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=1000, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=0, retryable_errors=[])
    side_effects = [
        "uses the signed-in Grok CLI OAuth/subscription account",
        "may consume paid or subscription-limited media generation",
        "writes one verified video to output_path",
    ]
    user_visible_verification = ["Watch the generated clip for motion, continuity, and prompt fidelity"]

    def __init__(self, *, grok_path: str | None = None, sessions_root: str | None = None) -> None:
        self._grok_path = grok_path
        self._sessions_root = sessions_root

    def get_status(self) -> ToolStatus:
        configured = self._grok_path or os.environ.get("GROK_CLI_PATH", DEFAULT_GROK_PATH)
        return ToolStatus.AVAILABLE if grok_cli_is_qualified(configured) else ToolStatus.UNAVAILABLE

    def get_info(self) -> dict[str, Any]:
        info = super().get_info()
        info["pinned_final_frame"] = {
            "supported": True,
            "models": [],
            "billing": "subscription_or_usage_unknown",
            "requires_explicit_route_approval": True,
            "minimum_cli_version": FRAME_PIN_MIN_CLI_VERSION,
            "local_paths_only": True,
            "native_tool": "reference_to_video",
        }
        return info

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        raise ValueError(
            "Grok CLI OAuth/subscription media cost is unknown before generation; "
            "do not treat this provider as free"
        )

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return 300.0

    def dry_run(self, inputs: dict[str, Any]) -> dict[str, Any]:
        approved = inputs.get("allow_unknown_cost") is True
        return {
            "tool": self.name,
            "provider": self.provider,
            "model": PINNED_MODEL,
            **MODEL_PROVENANCE,
            "cli_version": None,
            "minimum_cli_version": MIN_CLI_VERSION,
            "frame_pin_minimum_cli_version": FRAME_PIN_MIN_CLI_VERSION,
            "operation": inputs.get("operation"),
            "status": "not_checked_offline",
            "would_execute": approved,
            "paid_submission": False,
            "estimated_cost_usd": None,
            "cost_estimate_status": "unknown_subscription_media_cost",
            "requires_explicit_selection": True,
            "requires_unknown_cost_approval": not approved,
            "network_required": True,
        }

    @staticmethod
    def _error_result(error: GrokCLIContractError, session_id: str | None = None) -> ToolResult:
        return ToolResult(
            success=False,
            data={
                "provider": "grok_cli",
                "model": PINNED_MODEL,
                **MODEL_PROVENANCE,
                "cli_version": None,
                "session_id": session_id,
                "dispatch_session_id": session_id,
                "error_category": error.category,
                "dispatch_status": error.dispatch_status,
                "retry_attempted": False,
                "fallback_attempted": False,
            },
            error=f"Grok CLI {error.category} error: {error}",
            model=PINNED_MODEL,
        )

    @staticmethod
    def _resolve_single_path(
        inputs: dict[str, Any],
        *,
        keys: tuple[str, ...],
        field: str,
    ) -> str | None:
        present = [key for key in keys if key in inputs and inputs.get(key) is not None]
        if not present:
            return None
        if any(not isinstance(inputs[key], (str, Path)) for key in present):
            raise GrokCLIContractError("invalid_argument", f"{field} must be one local image path")
        values = [validate_local_image_paths(inputs[key], field=key, minimum=1, maximum=1)[0] for key in present]
        if len(set(values)) > 1:
            raise GrokCLIContractError(
                "invalid_argument",
                f"{' and '.join(present)} identify different source images",
            )
        return values[0]

    @staticmethod
    def _normalize_keyframes(raw: Any, *, duration: int) -> list[dict[str, Any]]:
        if not isinstance(raw, list):
            raise GrokCLIContractError("invalid_argument", "keyframes must be a list of {image, timestamp_s}")
        if len(raw) > 4:
            raise GrokCLIContractError("invalid_argument", "keyframes supports at most 4 mid-clip anchors")
        normalized: list[dict[str, Any]] = []
        previous_timestamp: float | None = None
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise GrokCLIContractError("invalid_argument", f"keyframes[{index}] must be an object")
            unexpected = set(item) - {"image", "timestamp_s"}
            if unexpected:
                raise GrokCLIContractError(
                    "invalid_argument",
                    f"keyframes[{index}] has unsupported keys: {', '.join(sorted(unexpected))}",
                )
            if "image" not in item or "timestamp_s" not in item:
                raise GrokCLIContractError(
                    "invalid_argument",
                    f"keyframes[{index}] requires image and timestamp_s",
                )
            try:
                if isinstance(item["timestamp_s"], bool) or not isinstance(item["timestamp_s"], (int, float)):
                    raise ValueError("timestamp must be numeric")
                timestamp = float(item["timestamp_s"])
            except (TypeError, ValueError) as exc:
                raise GrokCLIContractError(
                    "invalid_argument",
                    f"keyframes[{index}].timestamp_s must be a number",
                ) from exc
            if not 0 < timestamp < duration:
                raise GrokCLIContractError(
                    "invalid_argument",
                    f"keyframes[{index}].timestamp_s must be strictly inside 0..{duration}",
                )
            if previous_timestamp is not None and timestamp - previous_timestamp < (1.0 / 3.0):
                raise GrokCLIContractError(
                    "invalid_argument",
                    "keyframes timestamps must increase and be at least 1/3 second apart",
                )
            image = validate_local_image_paths(
                item["image"],
                field=f"keyframes[{index}].image",
                minimum=1,
                maximum=1,
            )[0]
            normalized.append({"image": image, "timestamp_s": timestamp})
            previous_timestamp = timestamp
        return normalized

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        session_id: str | None = None
        try:
            if "cli_session_id" in inputs:
                if inputs["cli_session_id"] is None:
                    raise GrokCLIContractError("invalid_argument", "cli_session_id must be a safe token, not null")
                session_id = validate_session_id(inputs["cli_session_id"])
            unsupported = _UNSUPPORTED_FRAME_ALIASES.intersection(inputs)
            if unsupported:
                raise GrokCLIContractError(
                    "capability",
                    "Use last_image_path / last_frame for a local final frame; "
                    "no loop switch or other ending-frame alias is supported: "
                    + ", ".join(sorted(unsupported)),
                )
            if _URL_FINAL_FRAME_KEYS.intersection(inputs):
                raise GrokCLIContractError(
                    "capability",
                    "Grok CLI pinned frames accept local filesystem paths only; "
                    "use last_image_path, or choose REST grok_video with "
                    "model=grok-imagine-video-1.5 after explicit route approval",
                )
            if any(key in inputs for key in ("model", "model_name")):
                raise GrokCLIContractError("capability", "Grok CLI video does not expose Imagine model selection")

            unexpected = set(inputs) - self.input_schema["properties"].keys()
            if unexpected:
                raise GrokCLIContractError("capability", "Unsupported Grok CLI video controls: " + ", ".join(sorted(unexpected)))
            if "endpoint_requirement_id" in inputs and (
                not isinstance(inputs["endpoint_requirement_id"], str) or not inputs["endpoint_requirement_id"].strip()
            ):
                raise GrokCLIContractError("invalid_argument", "endpoint_requirement_id must be non-empty")

            prompt = validate_prompt(inputs.get("prompt"))
            operation = str(inputs.get("operation") or "image_to_video")
            if operation not in {"image_to_video", "reference_to_video", "first_last_frame"}:
                raise GrokCLIContractError(
                    "capability",
                    "Grok CLI video supports only image_to_video, reference_to_video, and first_last_frame; "
                    f"direct {operation}, video editing, and upscaling are unavailable",
                )

            unsupported_voice = {
                key for key in inputs
                if ("voice" in key or "audio" in key) and key != "voices"
            }
            if unsupported_voice:
                raise GrokCLIContractError(
                    "capability",
                    "Unsupported voice/audio parameters: " + ", ".join(sorted(unsupported_voice)),
                )
            voices = inputs.get("voices", [])
            if not isinstance(voices, list) or len(voices) > 3 or any(
                not isinstance(voice, str) or not voice.strip() or voice != voice.strip()
                for voice in voices
            ):
                raise GrokCLIContractError(
                    "invalid_argument",
                    "voices must be a list of at most 3 non-empty preset identifiers without surrounding whitespace",
                )
            if "voices" in inputs and operation not in _PINNED_FRAME_OPERATIONS:
                raise GrokCLIContractError(
                    "capability",
                    "voices is supported only for reference_to_video and first_last_frame",
                )

            resolution = str(inputs.get("resolution", "480p"))
            if resolution not in {"480p", "720p"}:
                raise GrokCLIContractError("capability", "Grok CLI video supports only 480p and 720p")
            raw_duration = inputs.get("duration", 6)
            if isinstance(raw_duration, str) and raw_duration.isdecimal():
                raw_duration = int(raw_duration)
            if isinstance(raw_duration, bool) or not isinstance(raw_duration, int):
                raise GrokCLIContractError("invalid_argument", "duration must be integral seconds")
            duration = raw_duration

            arguments: dict[str, Any] = {
                "prompt": prompt,
                "duration": duration,
                "resolution_name": resolution,
            }
            native_tool = operation

            if operation == "image_to_video":
                pin_keys = {
                    "first_frame",
                    "last_image_path",
                    "last_frame",
                    "keyframes",
                    "reference_image_paths",
                    "endpoint_requirement_id",
                    "aspect_ratio",
                }.intersection(inputs)
                if pin_keys:
                    raise GrokCLIContractError(
                        "capability",
                        "image_to_video does not accept pinned last frames or keyframes; "
                        "use operation=first_last_frame or reference_to_video: "
                        + ", ".join(sorted(pin_keys)),
                    )
                if duration not in {6, 10}:
                    raise GrokCLIContractError(
                        "invalid_argument",
                        "image_to_video duration must be exactly 6 or 10 seconds",
                    )
                image_path = self._resolve_single_path(
                    inputs,
                    keys=("image_path", "reference_image_path"),
                    field="image_to_video",
                )
                if image_path is None:
                    raise GrokCLIContractError(
                        "invalid_argument",
                        "image_to_video requires image_path or reference_image_path",
                    )
                arguments["image"] = image_path
            else:
                if not 1 <= duration <= 15:
                    raise GrokCLIContractError(
                        "invalid_argument",
                        f"{operation} duration must be between 1 and 15 seconds",
                    )
                aspect_ratio = str(inputs.get("aspect_ratio", "16:9"))
                if aspect_ratio not in _REFERENCE_ASPECT_RATIOS:
                    raise GrokCLIContractError(
                        "invalid_argument",
                        f"unsupported aspect_ratio: {aspect_ratio}",
                    )
                arguments["aspect_ratio"] = aspect_ratio

                first_frame = self._resolve_single_path(
                    inputs,
                    keys=("first_frame", "image_path", "reference_image_path"),
                    field="first_frame",
                )
                last_frame = self._resolve_single_path(
                    inputs,
                    keys=("last_frame", "last_image_path"),
                    field="last_frame",
                )
                if (operation == "first_last_frame" or "endpoint_requirement_id" in inputs) and last_frame is None:
                    raise GrokCLIContractError(
                        "invalid_argument",
                        "first_last_frame / endpoint_requirement_id requires last_image_path or last_frame",
                    )

                references = inputs.get("reference_image_paths")
                images: list[str] | None = None
                if references is not None:
                    images = validate_local_image_paths(
                        references,
                        field="reference_to_video",
                        minimum=1,
                        maximum=14,
                    )

                keyframes: list[dict[str, Any]] | None = None
                if "keyframes" in inputs:
                    keyframes = self._normalize_keyframes(inputs.get("keyframes"), duration=duration)

                if first_frame is not None:
                    arguments["first_frame"] = first_frame
                if last_frame is not None:
                    arguments["last_frame"] = last_frame
                if images is not None:
                    arguments["images"] = images
                if keyframes:
                    arguments["keyframes"] = keyframes
                if voices:
                    arguments["voices"] = voices

                if not any(key in arguments for key in ("images", "voices", "first_frame", "last_frame", "keyframes")):
                    raise GrokCLIContractError(
                        "invalid_argument",
                        f"{operation} requires images, voices, first_frame, last_frame, and/or keyframes",
                    )
                native_tool = "reference_to_video"

            output_path = str(inputs.get("output_path") or "")
            cwd = str(inputs.get("cwd") or (Path(output_path).expanduser().parent if output_path else Path.cwd()))
            grok_path = str(self._grok_path or os.environ.get("GROK_CLI_PATH", DEFAULT_GROK_PATH))
            sessions_root = str(
                self._sessions_root
                or os.environ.get("GROK_SESSIONS_ROOT")
                or (Path.home() / ".grok" / "sessions")
            )
            timeout_seconds = inputs.get("timeout_seconds", 600)
            if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
                raise GrokCLIContractError("invalid_argument", "timeout_seconds must be an integer")
            if not 30 <= timeout_seconds <= 900:
                raise GrokCLIContractError("invalid_argument", "timeout_seconds must be between 30 and 900")
            if inputs.get("allow_unknown_cost") is not True:
                raise GrokCLIContractError(
                    "spending_approval",
                    "Grok CLI media pricing is unknown; set allow_unknown_cost=true only after explicit approval",
                )
            input_assets: list[dict[str, Any]] = []

            def record(path: str, role: str, **extra: Any) -> None:
                input_assets.append({"role": role, "path": path,
                    "sha256": file_sha256(path), **extra})

            for key in ("image", "first_frame", "last_frame"):
                if key in arguments:
                    record(arguments[key], "first_frame" if key == "image" else key)
            for index, path in enumerate(arguments.get("images", [])):
                record(path, "reference", index=index)
            for index, frame in enumerate(arguments.get("keyframes", [])):
                record(frame["image"], "keyframe", index=index, timestamp_s=frame["timestamp_s"])
            receipt = {
                "version": "1.0", "provider": self.provider, "native_tool": native_tool,
                "adapter_version": self.version, **MODEL_PROVENANCE,
                "requirement_id": inputs.get("endpoint_requirement_id"),
                "submitted_arguments": arguments, "input_assets": input_assets,
            }
            receipt["request_sha256"] = hashlib.sha256(
                json.dumps(receipt, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            ).hexdigest()
        except GrokCLIContractError as exc:
            return self._error_result(exc, session_id)
        except (TypeError, ValueError, OSError) as exc:
            return self._error_result(GrokCLIContractError("invalid_argument", str(exc)), session_id)

        result = execute_grok_cli_media(
            tool_name=native_tool,
            arguments=arguments,
            output_path=output_path,
            cwd=cwd,
            grok_path=grok_path,
            sessions_root=sessions_root,
            timeout_seconds=timeout_seconds,
            media_kind="video",
            session_id=session_id,
        )
        receipt.update(cli_version=result.data.get("cli_version"),
            session_id=result.data.get("session_id"),
            dispatch_status=result.data.get("dispatch_status"),
            submission_evidence="verified_native_call" if result.success else "unconfirmed")
        result.data["conditioning_receipt"] = receipt
        endpoints = {item["role"]: {"path": item["path"], "sha256": item["sha256"]}
                     for item in input_assets if item["role"] in {"first_frame", "last_frame"}}
        result.data["endpoint_conditioning"] = {
            "requirement_id": inputs.get("endpoint_requirement_id"),
            "first_frame": endpoints.get("first_frame"), "last_frame": endpoints.get("last_frame"),
            "constraint": "endpoints_only", "request_sha256": receipt["request_sha256"],
        }
        return result
