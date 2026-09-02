"""Explicit Grok CLI OAuth/subscription image provider.

This adapter is intentionally separate from :mod:`tools.graphics.grok_image`,
which uses the xAI REST API and ``XAI_API_KEY``.
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


_ASPECT_RATIOS = {
    "1:1",
    "16:9",
    "9:16",
    "4:3",
    "3:4",
    "3:2",
    "2:3",
    "2:1",
    "1:2",
    "19.5:9",
    "9:19.5",
    "20:9",
    "9:20",
    "auto",
}


class GrokCLIImage(BaseTool):
    name = "grok_cli_image"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "image_generation"
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
    agent_skills = ["grok-media"]

    capabilities = ["text_to_image", "image_edit", "image_to_image"]
    supports = {
        "image_gen": True,
        "image_edit": True,
        "reference_image": True,
        "multiple_reference_images": True,
        "aspect_ratio": True,
        "explicit_selection_only": True,
        "oauth_session_auth": True,
        "api_key_required": False,
        "browser_automation": False,
        "silent_fallback": False,
        "cost_preestimate": False,
    }
    best_for = [
        "explicit Grok Imagine image generation through an existing CLI OAuth session",
        "reference-conditioned image edits without an xAI API key",
    ]
    not_good_for = [
        "offline or free generation",
        "seeded reproducibility",
        "implicit provider selection",
        "known pre-generation spend estimates",
    ]
    fallback_tools: list[str] = []

    input_schema = {
        "type": "object",
        "required": ["prompt", "output_path"],
        "properties": {
            "prompt": {"type": "string", "maxLength": 4096},
            "operation": {
                "type": "string",
                "enum": ["generate", "image_gen", "image_edit"],
                "default": "image_gen",
                "description": "Native operation, or the image_selector generate alias.",
            },
            "generation_mode": {
                "type": "string",
                "enum": ["generate", "edit"],
                "default": "generate",
                "description": "OpenMontage selector-compatible alias for operation.",
            },
            "aspect_ratio": {"type": "string", "enum": sorted(_ASPECT_RATIOS), "default": "auto"},
            "image_path": {"type": "string"},
            "image_paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "output_path": {"type": "string"},
            "cwd": {"type": "string"},
            "allow_unknown_cost": {
                "type": "boolean",
                "default": False,
                "description": "Explicit approval to dispatch despite unknown CLI media pricing.",
            },
            "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 900, "default": 480},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=250, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=0, retryable_errors=[])
    side_effects = [
        "uses the signed-in Grok CLI OAuth/subscription account",
        "may consume paid or subscription-limited media generation",
        "writes one verified image to output_path",
    ]
    user_visible_verification = ["Inspect the generated image for prompt and reference fidelity"]

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
        return 180.0

    def dry_run(self, inputs: dict[str, Any]) -> dict[str, Any]:
        approved = inputs.get("allow_unknown_cost") is True
        return {
            "tool": self.name,
            "provider": self.provider,
            "model": PINNED_MODEL,
            "cli_version": PINNED_CLI_VERSION,
            "operation": inputs.get("operation", "image_gen"),
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
            native_operation = inputs.get("operation")
            if native_operation == "generate":
                native_operation = None
            generation_mode = inputs.get("generation_mode")
            mode_map = {"generate": "image_gen", "edit": "image_edit"}
            if generation_mode is not None and generation_mode not in mode_map:
                raise GrokCLIContractError(
                    "invalid_argument", f"unsupported generation_mode: {generation_mode}"
                )
            mapped_operation = mode_map.get(str(generation_mode)) if generation_mode is not None else None
            if native_operation is not None and mapped_operation is not None and native_operation != mapped_operation:
                raise GrokCLIContractError(
                    "invalid_argument", "operation and generation_mode select different image operations"
                )
            operation = str(native_operation or mapped_operation or "image_gen")
            if operation not in {"image_gen", "image_edit"}:
                raise GrokCLIContractError(
                    "capability",
                    f"Grok CLI image adapter supports only image_gen and image_edit; {operation!r} is unavailable",
                )
            aspect_ratio = str(inputs.get("aspect_ratio", "auto"))
            if aspect_ratio not in _ASPECT_RATIOS:
                raise GrokCLIContractError("invalid_argument", f"unsupported aspect_ratio: {aspect_ratio}")

            arguments: dict[str, Any] = {"prompt": prompt, "aspect_ratio": aspect_ratio}
            provided_images: Any = inputs.get("image_paths")
            if provided_images is None and inputs.get("image_path") is not None:
                provided_images = [inputs["image_path"]]
            if operation == "image_edit":
                arguments["image"] = validate_local_image_paths(
                    provided_images, field="image_edit", minimum=1, maximum=5
                )
            elif provided_images:
                raise GrokCLIContractError(
                    "invalid_argument", "image_gen does not accept source images; choose image_edit explicitly"
                )

            output_path = str(inputs.get("output_path") or "")
            cwd = str(inputs.get("cwd") or (Path(output_path).expanduser().parent if output_path else Path.cwd()))
            grok_path = str(self._grok_path or os.environ.get("GROK_CLI_PATH", DEFAULT_GROK_PATH))
            sessions_root = str(
                self._sessions_root
                or os.environ.get("GROK_SESSIONS_ROOT")
                or (Path.home() / ".grok" / "sessions")
            )
            timeout_seconds = int(inputs.get("timeout_seconds", 480))
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
            media_kind="image",
        )
