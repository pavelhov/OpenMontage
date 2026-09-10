"""xAI Grok Imagine video generation with native synchronized audio.

Generates 1-15 second videos with synchronized sound (dialogue with lip-sync,
SFX, ambient, background music) in a single pass. No post-production audio needed.
"""

from __future__ import annotations

import base64
import binascii
import mimetypes
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft7Validator

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


def _file_to_data_uri(path_str: str) -> str:
    path = Path(path_str).expanduser()
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"Input image must be a non-empty regular file: {path}")
    mime_type, _ = mimetypes.guess_type(path.name)
    if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise ValueError(f"Unsupported input image format: {path.suffix}")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _normalize_media_ref(url_value: str | None, path_value: str | None) -> dict[str, str] | None:
    if url_value and path_value:
        raise ValueError("Provide either an image URL or a local path, not both")
    if url_value:
        parsed = urlsplit(url_value)
        if not (parsed.scheme == "https" and parsed.netloc) and not url_value.startswith(
            ("data:image/jpeg;base64,", "data:image/png;base64,", "data:image/webp;base64,")
        ):
            raise ValueError("Image URLs must be public HTTPS URLs or image data URIs")
        if url_value.startswith("data:"):
            try:
                if not base64.b64decode(url_value.split(",", 1)[1], validate=True):
                    raise ValueError("Empty image data URI")
            except binascii.Error as exc:
                raise ValueError("Invalid base64 image data URI") from exc
        return {"url": url_value}
    if path_value:
        return {"url": _file_to_data_uri(path_value)}
    return None


class GrokVideo(BaseTool):
    name = "grok_video"
    version = "0.2.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "grok"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = []
    install_instructions = (
        "Set XAI_API_KEY to your xAI API key.\n"
        "  Get one from the xAI developer console"
    )
    agent_skills = ["grok-media", "ai-video-gen"]

    capabilities = ["text_to_video", "image_to_video", "reference_to_video", "first_last_frame"]
    first_last_frame_models = frozenset({"grok-imagine-video-1.5"})
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        "reference_to_video": True,
        "first_last_frame": True,  # Requires explicit grok-imagine-video-1.5.
        "reference_image": True,
        "multiple_reference_images": True,
        "native_audio": True,
        "lip_sync": True,
        "cinematic_quality": True,
    }
    best_for = [
        "cinematic clips with native synchronized audio (dialogue, SFX, music)",
        "reference-conditioned video with product/character consistency",
        "lip-synced dialogue and foley in a single generation pass",
        "cost-effective high-quality video ($0.07/s at 720p)",
    ]
    not_good_for = ["offline generation"]
    fallback_tools = ["veo_video", "runway_video", "kling_video", "minimax_video"]
    # Native synchronized audio (lip-sync + dialogue + SFX in one pass) puts
    # Grok on par with the other premium providers; without a quality_score the
    # scorer only counted supports/stability flags, under-ranking it relative to
    # seedance (0.95) / runway / higgsfield (0.9). See lib/scoring.py.
    quality_score = 0.9

    input_schema = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string"},
            "operation": {
                "type": "string",
                "enum": ["text_to_video", "image_to_video", "reference_to_video", "first_last_frame"],
                "default": "text_to_video",
            },
            "model": {
                "type": "string",
                "enum": ["grok-imagine-video", "grok-imagine-video-1.5"],
                "default": "grok-imagine-video",
            },
            "duration": {
                "type": "integer",
                "minimum": 1,
                "maximum": 15,
                "default": 5,
            },
            "aspect_ratio": {
                "type": "string",
                "enum": ["16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3"],
                "default": "16:9",
            },
            "resolution": {
                "type": "string",
                "enum": ["480p", "720p", "1080p"],
                "default": "720p",
            },
            "image_url": {"type": "string", "description": "Reference image URL for image_to_video"},
            "image_path": {"type": "string", "description": "Local reference image path for image_to_video"},
            "reference_image_url": {"type": "string", "minLength": 1, "description": "Selector alias for image_url (first frame)."},
            "reference_image_path": {"type": "string", "minLength": 1, "description": "Selector alias for image_path (first frame)."},
            "last_image_url": {"type": "string", "minLength": 1, "description": "Pinned last frame; REST last_frame.url. Requires grok-imagine-video-1.5."},
            "last_image_path": {"type": "string", "minLength": 1, "description": "Local pinned last frame. Use the same first/last image for a loop."},
            "loop": False,
            "seamless_loop": False,
            "last_frame": False,
            "end_frame": False,
            "start_frame": False,
            "last_frame_url": False,
            "last_frame_path": False,
            "end_frame_url": False,
            "end_frame_path": False,
            "reference_audios": False,
            "reference_audio_urls": False,
            "reference_audio_paths": False,
            "video_url": False,
            "video_path": False,
            "reference_image_urls": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 7,
                "description": "Reference image URLs for reference_to_video",
            },
            "reference_image_paths": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 7,
                "description": "Local reference image paths for reference_to_video",
            },
            "output_path": {"type": "string"},
            "poll_interval_seconds": {"type": "integer", "minimum": 2, "default": 5},
            "timeout_seconds": {"type": "integer", "minimum": 30, "default": 900},
        },
        "allOf": [
            {
                "if": {"anyOf": [
                    {"required": ["last_image_url"]},
                    {"required": ["last_image_path"]},
                    {"required": ["operation"], "properties": {"operation": {"const": "first_last_frame"}}},
                ]},
                "then": {
                    "required": ["model"],
                    "properties": {"model": {"const": "grok-imagine-video-1.5"}, "resolution": {"enum": ["480p", "720p"]}},
                },
            },
            {
                "if": {"required": ["operation"], "properties": {"operation": {"const": "first_last_frame"}}},
                "then": {"anyOf": [{"required": ["last_image_url"]}, {"required": ["last_image_path"]}]},
            },
            {"not": {"required": ["last_image_url", "last_image_path"]}},
            {"not": {"required": ["image_url", "image_path"]}},
        ],
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=500, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=0, retryable_errors=[])
    idempotency_key_fields = [
        "prompt", "operation", "model", "duration", "aspect_ratio", "resolution",
        "image_url", "image_path", "reference_image_url", "reference_image_path",
        "last_image_url", "last_image_path", "reference_image_urls", "reference_image_paths",
    ]
    side_effects = ["writes video file to output_path", "calls xAI video API"]
    user_visible_verification = ["Watch generated clip for motion quality and prompt fidelity"]

    def get_status(self) -> ToolStatus:
        if os.environ.get("XAI_API_KEY"):
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    @staticmethod
    def _input_image_count(inputs: dict[str, Any]) -> int:
        count = 0
        if any(inputs.get(key) for key in ("image_url", "image_path", "reference_image_url", "reference_image_path")):
            count += 1
        if inputs.get("last_image_url") or inputs.get("last_image_path"):
            count += 1
        count += len(inputs.get("reference_image_urls") or [])
        count += len(inputs.get("reference_image_paths") or [])
        return count

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        duration = int(inputs.get("duration", 5))
        resolution = inputs.get("resolution", "720p")
        model = inputs.get("model", "grok-imagine-video")
        rates = {
            "grok-imagine-video": ({"480p": 0.05, "720p": 0.07}, 0.002),
            "grok-imagine-video-1.5": ({"480p": 0.08, "720p": 0.14, "1080p": 0.25}, 0.01),
        }
        if model not in rates or resolution not in rates[model][0]:
            raise ValueError("Unsupported Grok model/resolution for cost estimate")
        base_per_second = rates[model][0][resolution]
        input_image_cost = self._input_image_count(inputs) * rates[model][1]
        # https://docs.x.ai/developers/pricing (verified 2026-09-10).
        return base_per_second * duration + input_image_cost

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        duration = int(inputs.get("duration", 5))
        return 90.0 + duration * 8.0

    def _build_payload(self, inputs: dict[str, Any]) -> dict[str, Any]:
        # Direct execute() calls do not pass through an external schema validator.
        error = next(Draft7Validator(self.input_schema).iter_errors(inputs), None)
        if error is not None:
            location = ".".join(str(part) for part in error.path) or "inputs"
            raise ValueError(f"Invalid {location}: {error.validator} constraint failed")
        operation = inputs.get("operation", "text_to_video")
        model = inputs.get("model", "grok-imagine-video")

        def alias(primary: str, alternate: str) -> str | None:
            if primary in inputs and alternate in inputs and inputs[primary] != inputs[alternate]:
                raise ValueError(f"Conflicting {primary} and {alternate}")
            return inputs.get(primary) or inputs.get(alternate)

        first = _normalize_media_ref(
            alias("image_url", "reference_image_url"), alias("image_path", "reference_image_path")
        )
        last = _normalize_media_ref(inputs.get("last_image_url"), inputs.get("last_image_path"))
        refs = [
            _normalize_media_ref(url, None) for url in inputs.get("reference_image_urls", [])
        ] + [
            _normalize_media_ref(None, path) for path in inputs.get("reference_image_paths", [])
        ]
        if len(refs) > 7 or any(ref is None for ref in refs):
            raise ValueError("reference_to_video supports at most 7 non-empty reference images")
        if model != "grok-imagine-video-1.5" and (last or first and refs):
            raise ValueError("Pinned last frames and first-frame/reference combinations require explicit model=grok-imagine-video-1.5")
        if operation == "text_to_video" and (first or last or refs):
            raise ValueError("text_to_video cannot accept frame/reference inputs; select the intended operation")
        if operation == "image_to_video" and not first:
            raise ValueError("image_to_video requires image_url or image_path (or its reference_image alias)")
        if operation == "first_last_frame" and not last:
            raise ValueError("first_last_frame requires last_image_url or last_image_path; the first frame is optional")
        if operation == "reference_to_video" and not (refs or last):
            raise ValueError("reference_to_video requires reference images or a pinned last frame")
        prompt = inputs.get("prompt", "")
        if not prompt.strip() and not (model == "grok-imagine-video-1.5" and (first or last or refs)):
            raise ValueError("A non-empty prompt is required for text-to-video and classic Grok video")
        resolution = inputs.get("resolution", "720p")
        if resolution == "1080p" and (model != "grok-imagine-video-1.5" or last or refs):
            raise ValueError("1080p requires grok-imagine-video-1.5 text/image-to-video; frame pairs and reference-to-video are capped at 720p")
        payload: dict[str, Any] = {
            "model": model,
            "duration": inputs.get("duration", 5),
            "resolution": resolution,
        }
        if prompt.strip():
            payload["prompt"] = prompt
        if inputs.get("aspect_ratio"):
            payload["aspect_ratio"] = inputs["aspect_ratio"]
        if first:
            payload["image"] = first
        if last:
            payload["last_frame"] = last
        if refs:
            payload["reference_images"] = refs

        return payload

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        try:
            payload = self._build_payload(inputs)
        except (ValueError, TypeError, OSError) as exc:
            return ToolResult(success=False, error=f"Grok video invalid input: {exc}", data={"dispatch_status": "not_dispatched"})
        api_key = os.environ.get("XAI_API_KEY")
        if not api_key:
            return ToolResult(
                success=False,
                error="XAI_API_KEY not set. " + self.install_instructions,
            )

        import requests
        from tools.video._shared import probe_output

        start = time.time()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(
                "https://api.x.ai/v1/videos/generations",
                headers=headers,
                json=payload,
                timeout=60,
            )
            response.raise_for_status()
            request_id = response.json()["request_id"]

            timeout_seconds = int(inputs.get("timeout_seconds", 900))
            poll_interval = int(inputs.get("poll_interval_seconds", 5))
            deadline = time.time() + timeout_seconds

            result_data: dict[str, Any] | None = None
            while time.time() < deadline:
                result = requests.get(
                    f"https://api.x.ai/v1/videos/{request_id}",
                    headers={"Authorization": headers["Authorization"]},
                    timeout=30,
                )
                result.raise_for_status()
                result_data = result.json()
                status = result_data.get("status")
                if status == "done":
                    break
                if status in {"failed", "expired"}:
                    detail = result_data.get("error") or result_data.get("message") or status
                    return ToolResult(success=False, error=f"Grok video generation {status}: {detail}")
                time.sleep(poll_interval)

            if not result_data or result_data.get("status") != "done":
                return ToolResult(success=False, error="Grok video generation timed out")

            video_url = (result_data.get("video") or {}).get("url")
            if not video_url:
                return ToolResult(success=False, error="xAI video output missing url")

            download = requests.get(video_url, timeout=300)
            download.raise_for_status()
            output_path = Path(inputs.get("output_path", "grok_video_output.mp4"))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(download.content)

        except Exception as e:
            return ToolResult(success=False, error=f"Grok video generation failed: {e}")

        probed = probe_output(output_path)
        return ToolResult(
            success=True,
            data={
                "provider": "grok",
                "model": payload["model"],
                "prompt": inputs.get("prompt", ""),
                "operation": inputs.get("operation", "text_to_video"),
                "request_id": request_id,
                "output": str(output_path),
                "output_path": str(output_path),
                "format": "mp4",
                **probed,
            },
            artifacts=[str(output_path)],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.time() - start, 2),
            model=payload["model"],
        )
