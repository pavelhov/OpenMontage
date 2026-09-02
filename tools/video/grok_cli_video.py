"""Explicit Grok CLI OAuth/subscription image-to-video provider.

The CLI exposes image-first primitives only.  Direct text-to-video, video
editing, and upscaling fail closed instead of being emulated or silently sent
to the existing xAI REST provider.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from tools._grok_cli_media import (
    DEFAULT_GROK_PATH,
    PINNED_CLI_VERSION,
    PINNED_MODEL,
    GrokCLIContractError,
    execute_grok_cli_media,
    grok_cli_is_qualified,
    validate_local_image_paths,
    validate_prompt,
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


class GrokCLIVideo(BaseTool):
    name = "grok_cli_video"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "grok_cli"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["cmd:grok", "cmd:ffprobe"]
    install_instructions = (
        f"Install Grok CLI {PINNED_CLI_VERSION} on PATH (or set GROK_CLI_PATH), "
        "then sign in interactively with `grok login`. This adapter never initiates login."
    )
    agent_skills = ["grok-media", "ai-video-gen"]

    capabilities = ["image_to_video", "reference_to_video"]
    supports = {
        "text_to_video": False,
        "image_to_video": True,
        "reference_to_video": True,
        "video_edit": False,
        "upscale": False,
        "reference_image": True,
        "multiple_reference_images": True,
        "native_audio": True,
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
    ]
    not_good_for = [
        "direct text-to-video",
        "video editing or post-generation upscaling",
        "offline or free generation",
        "implicit provider selection",
        "safe automatic retry after a timeout",
    ]
    fallback_tools: list[str] = []

    input_schema = {
        "type": "object",
        "required": ["prompt", "operation", "output_path"],
        "properties": {
            "prompt": {"type": "string", "maxLength": 4096},
            "operation": {
                "type": "string",
                "enum": ["image_to_video", "reference_to_video"],
            },
            "image_path": {"type": "string"},
            "reference_image_path": {
                "type": "string",
                "description": "OpenMontage selector-compatible alias for image_path.",
            },
            "reference_image_paths": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 7,
            },
            "duration": {"type": "integer", "minimum": 1, "maximum": 15, "default": 6},
            "resolution": {"type": "string", "enum": ["480p", "720p"], "default": "480p"},
            "aspect_ratio": {
                "type": "string",
                "enum": sorted(_REFERENCE_ASPECT_RATIOS),
                "default": "16:9",
            },
            "output_path": {"type": "string"},
            "cwd": {"type": "string"},
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
            "cli_version": PINNED_CLI_VERSION,
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
    def _error_result(error: GrokCLIContractError) -> ToolResult:
        return ToolResult(
            success=False,
            data={
                "provider": "grok_cli",
                "model": PINNED_MODEL,
                "cli_version": PINNED_CLI_VERSION,
                "error_category": error.category,
                "dispatch_status": error.dispatch_status,
                "retry_attempted": False,
                "fallback_attempted": False,
            },
            error=f"Grok CLI {error.category} error: {error}",
            model=PINNED_MODEL,
        )

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        try:
            prompt = validate_prompt(inputs.get("prompt"))
            operation = str(inputs.get("operation") or "image_to_video")
            if operation not in {"image_to_video", "reference_to_video"}:
                raise GrokCLIContractError(
                    "capability",
                    "Grok CLI video supports only image_to_video and reference_to_video; "
                    f"direct {operation}, video editing, and upscaling are unavailable",
                )
            resolution = str(inputs.get("resolution", "480p"))
            if resolution not in {"480p", "720p"}:
                raise GrokCLIContractError("capability", "Grok CLI video supports only 480p and 720p")
            duration = int(inputs.get("duration", 6))

            arguments: dict[str, Any] = {
                "prompt": prompt,
                "duration": duration,
                "resolution_name": resolution,
            }
            if operation == "image_to_video":
                if duration not in {6, 10}:
                    raise GrokCLIContractError(
                        "invalid_argument", "image_to_video duration must be exactly 6 or 10 seconds"
                    )
                image_path = inputs.get("image_path")
                reference_image_path = inputs.get("reference_image_path")
                if image_path and reference_image_path and image_path != reference_image_path:
                    raise GrokCLIContractError(
                        "invalid_argument",
                        "image_path and reference_image_path identify different source images",
                    )
                image_paths = validate_local_image_paths(
                    image_path or reference_image_path,
                    field="image_to_video",
                    minimum=1,
                    maximum=1,
                )
                arguments["image"] = image_paths[0]
            else:
                if not 1 <= duration <= 15:
                    raise GrokCLIContractError(
                        "invalid_argument", "reference_to_video duration must be between 1 and 15 seconds"
                    )
                aspect_ratio = str(inputs.get("aspect_ratio", "16:9"))
                if aspect_ratio not in _REFERENCE_ASPECT_RATIOS:
                    raise GrokCLIContractError("invalid_argument", f"unsupported aspect_ratio: {aspect_ratio}")
                arguments["images"] = validate_local_image_paths(
                    inputs.get("reference_image_paths"),
                    field="reference_to_video",
                    minimum=1,
                    maximum=7,
                )
                arguments["aspect_ratio"] = aspect_ratio

            output_path = str(inputs.get("output_path") or "")
            cwd = str(inputs.get("cwd") or (Path(output_path).expanduser().parent if output_path else Path.cwd()))
            grok_path = str(self._grok_path or os.environ.get("GROK_CLI_PATH", DEFAULT_GROK_PATH))
            sessions_root = str(
                self._sessions_root
                or os.environ.get("GROK_SESSIONS_ROOT")
                or (Path.home() / ".grok" / "sessions")
            )
            timeout_seconds = int(inputs.get("timeout_seconds", 600))
            if not 30 <= timeout_seconds <= 900:
                raise GrokCLIContractError("invalid_argument", "timeout_seconds must be between 30 and 900")
            if inputs.get("allow_unknown_cost") is not True:
                raise GrokCLIContractError(
                    "spending_approval",
                    "Grok CLI media pricing is unknown; set allow_unknown_cost=true only after explicit approval",
                )
        except GrokCLIContractError as exc:
            return self._error_result(exc)
        except (TypeError, ValueError, OSError) as exc:
            return self._error_result(GrokCLIContractError("invalid_argument", str(exc)))

        return execute_grok_cli_media(
            tool_name=operation,
            arguments=arguments,
            output_path=output_path,
            cwd=cwd,
            grok_path=grok_path,
            sessions_root=sessions_root,
            timeout_seconds=timeout_seconds,
            media_kind="video",
        )
