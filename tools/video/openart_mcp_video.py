"""OpenArt video through its installed, agent-mediated MCP connector."""

from __future__ import annotations

from typing import Any
import importlib
from tools.base_tool import (
    BaseTool, Determinism, ExecutionMode, ResourceProfile, RetryPolicy,
    ToolResult, ToolRuntime, ToolStability, ToolStatus, ToolTier,
)


def _optional_dependencies():
    """Defer optional plugin imports without hiding broken installed dependencies."""
    try:
        return (importlib.import_module("lib.openart_mcp"),
                importlib.import_module("lib.openart_mcp_dispatch"))
    except ModuleNotFoundError as exc:
        if exc.name not in {"lib.openart_mcp", "lib.openart_mcp_dispatch"}:
            raise
        return None


def _dependency_unavailable() -> ToolResult:
    message = "OpenArt MCP requires lib.openart_mcp and lib.openart_mcp_dispatch"
    return ToolResult(success=False, cost_usd=None,
                      error=f"dependency_unavailable: {message}",
                      data={"error": {"kind": "dependency_unavailable", "message": message}})


class OpenArtMCPVideo(BaseTool):
    name = "openart_mcp_video"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "openart_mcp"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.ASYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API
    retry_policy = RetryPolicy(max_retries=0)
    dependencies: list[str] = []
    install_instructions = "Requires the installed and signed-in OpenArt connector in the agent environment."
    agent_skills = ["openart-mcp", "ai-video-gen"]

    capabilities = ["text_to_video", "image_to_video", "reference_to_video", "first_last_frame"]
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        "reference_to_video": True,
        "first_last_frame": False,
        "multiple_reference_images": False,
        "native_audio": False,
        "explicit_selection_only": True,
        "control_scope": "per_verified_model_mode",
        "connector_handoff": True,
    }
    best_for = [
        "OpenArt models and modes using the user's installed connector sign-in",
        "native model-specific image, video, audio, and frame controls when present in the verified form",
    ]
    not_good_for = ["requests that require an exact, verified credit ceiling when one is unavailable"]
    resource_profile = ResourceProfile(network_required=True)

    input_schema = {
        "type": "object",
        "required": ["project_dir", "governance", "model", "mode", "prompt"],
        "additionalProperties": False,
        "properties": {
            "project_dir": {"type": "string", "minLength": 1},
            "governance": {"type": "object", "required": ["scope_id", "shot_id", "stage"],
                "additionalProperties": False, "properties": {
                    "scope_id": {"type": "string", "minLength": 1},
                    "shot_id": {"type": "string", "minLength": 1},
                    "stage": {"type": "string", "minLength": 1},
                }},
            "operation": {"type": "string", "enum": ["text_to_video", "image_to_video", "first_last_frame", "reference_to_video", "shot_video"]},
            "model": {"type": "string", "minLength": 1},
            "mode": {"type": "string", "minLength": 1},
            "prompt": {"type": "string", "minLength": 1},
            "duration": {"type": ["integer", "number", "string"]},
            "aspect_ratio": {"type": "string"},
            "resolution": {"type": "string"},
            "native_params": {"type": "object", "additionalProperties": True},
            "input_assets": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                "required": ["role", "source_path"], "properties": {
                    "role": {"type": "string", "enum": ["first_frame", "last_frame", "reference_image", "reference_video", "reference_audio", "character_reference", "environment_reference"]},
                    "source_path": {"type": "string", "minLength": 1},
                    "source_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "upload_id": {"type": "string", "minLength": 1},
                    "reference_id": {"type": "string", "minLength": 1},
                }}},
            "openart_project_id": {"type": "string", "minLength": 1},
            "first_frame": {"type": "string"},
            "last_frame": {"type": "string"},
            "output_path": {"type": "string", "minLength": 1},
            "compiled_request_id": {"type": "string", "minLength": 1},
            "preparation_review_id": {"type": "string", "minLength": 1},
            "unknown_cost_authorization_id": {"type": "string", "minLength": 1},
            "attempt_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"},
        },
        "allOf": [{
            "if": {"properties": {"operation": {"const": "first_last_frame"}}, "required": ["operation"]},
            "then": {
                "required": ["mode", "input_assets"],
                "properties": {
                    "mode": {"const": "image2video"},
                    "input_assets": {
                        "contains": {"properties": {"role": {"const": "first_frame"}}, "required": ["role"]},
                        "allOf": [{"contains": {"properties": {"role": {"const": "last_frame"}}, "required": ["role"]}}],
                    },
                },
            },
        }],
    }

    def get_status(self) -> ToolStatus:
        dependencies = _optional_dependencies()
        if dependencies is None:
            return ToolStatus.UNAVAILABLE
        mcp, _ = dependencies
        try:
            if mcp.video_catalog() and mcp.account_summary() and not self._catalog_error():
                return ToolStatus.AVAILABLE
        except Exception:
            pass
        return ToolStatus.UNAVAILABLE

    def get_info(self) -> dict[str, Any]:
        info = super().get_info()
        dependencies = _optional_dependencies()
        mcp = dependencies[0] if dependencies is not None else None
        catalog, catalog_error = self._catalog_snapshot()
        # Native catalog/form/account readiness enables production; a prior
        # generated result is optional evidence reported per route.
        qualified_routes = [
            (model, mode, route)
            for model, model_row in catalog.items()
            for mode, route in model_row.get("modes", {}).items()
            if route.get("production_ready") is True
        ]
        supported_roles = {
            role for _, _, route in qualified_routes
            for role, spec in route.get("native_capabilities", {}).get("roles", {}).items()
            if spec.get("supported") is True
        }
        # Per-route audio facts: a form without an audio field is "no toggle",
        # not "no audio". Default output comes only from bounded provider docs.
        audio_by_route = {(model, mode): route.get("audio_capability") or {} for model, mode, route in qualified_routes}
        audio_routes = [{"model": model, "mode": mode} for (model, mode), audio in audio_by_route.items()
                        if audio.get("explicit_toggle", {}).get("available") is True]
        audio_output = {
            status: [{"model": model, "mode": mode} for (model, mode), audio in audio_by_route.items()
                     if audio.get("native_output", "unknown") == status]
            for status in ("supported_toggle", "supported_default", "unknown", "unsupported")
        }
        conditional = {
            "first_last_frame": "first_frame" in supported_roles and "last_frame" in supported_roles,
            "reference_image": "reference_image" in supported_roles,
            "reference_video": "reference_video" in supported_roles,
            "reference_audio": "reference_audio" in supported_roles,
        }
        mode_support = {
            name: [{"model": model, "mode": mode} for model, mode, route in qualified_routes
                   if ((name == "first_last_frame" and route.get("native_capabilities", {}).get("roles", {}).get("first_frame", {}).get("supported") is True
                        and route.get("native_capabilities", {}).get("roles", {}).get("last_frame", {}).get("supported") is True)
                       or (name == "reference_image" and route.get("native_capabilities", {}).get("roles", {}).get(name, {}).get("supported") is True)
                       or (name == "reference_video" and route.get("native_capabilities", {}).get("roles", {}).get(name, {}).get("supported") is True)
                       or (name == "reference_audio" and route.get("native_capabilities", {}).get("roles", {}).get(name, {}).get("supported") is True))]
            for name in conditional
        }
        mode_support["native_audio"] = audio_routes
        conditional["native_audio"] = bool(audio_routes)
        try:
            observed_count = len(mcp.video_catalog()) if mcp is not None else 0
        except mcp.OpenArtMCPError:
            observed_count = 0
        info.update(
            generation_enabled=bool(qualified_routes),
            production_ready_routes=[
                {"model": model, "mode": mode,
                 "empirical_result_status": route.get("empirical_result_status", "not_tested")}
                for model, mode, route in qualified_routes
            ],
            model_catalog=catalog,
            observed_model_mode_count=observed_count,
            catalog_status="unavailable" if catalog_error else "available",
            catalog_unavailable_reason=catalog_error,
            exact_quote_available=False,
            billing_unit="unknown_unless_verified",
            account_separation="agent_mediated_connector",
            auto_continue_available="supported_with_fresh_exact_policy_for_production_ready_route",
            auto_continue_requires=(
                "active_auto_continue_policy_with_openart_mcp_unknown_cost_variant_and_exact_production_ready_account_project_model_mode"
            ),
            auto_continue_policy_active=None,
            supports={**info.get("supports", {}), "conditional_model_modes": mode_support},
            conditional_capabilities=conditional,
            # native_audio above lists explicit on/off forms only. Output status
            # is per exact route; unknown is never false, and no route here
            # claims observed connector audio or dialogue fidelity.
            native_audio_output=audio_output,
        )
        return info

    @staticmethod
    def _model_catalog() -> dict[str, Any]:
        """Return exact verified model/mode qualification and native controls.

        The selector uses this same catalog as ``get_info`` so MCP cannot fall
        back to provider-wide capability flags when an exact mode is absent.
        Real current catalog/form/account routes are production-ready; a prior
        generated result only changes ``empirical_result_status``.
        """
        catalog, _ = OpenArtMCPVideo._catalog_snapshot()
        return catalog

    @staticmethod
    def _catalog_snapshot() -> tuple[dict[str, Any], str | None]:
        dependencies = _optional_dependencies()
        if dependencies is None:
            return {}, "dependency_unavailable: OpenArt MCP libraries are unavailable"
        mcp, _ = dependencies
        try:
            catalog = mcp.model_catalog()
        except mcp.OpenArtMCPError as exc:
            return {}, f"{exc.kind}: {exc.message}"
        return catalog, None

    @staticmethod
    def _catalog_error() -> str | None:
        return OpenArtMCPVideo._catalog_snapshot()[1]

    def estimate_cost(self, inputs: dict[str, Any]) -> None:
        # Unknown provider exposure must not be represented as a zero-dollar quote.
        return None

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        dependencies = _optional_dependencies()
        if dependencies is None:
            return _dependency_unavailable()
        mcp, dispatch = dependencies
        try:
            prepared = dispatch.invoke(inputs)
            return ToolResult(success=True, cost_usd=None, data={
                **prepared,
                "provider": self.provider,
                "generation_enabled": False,
                "prepared_for_connector_handoff": True,
                "paid_submission": False,
                "next": {"action": "begin", "tool": "openart_mcp_account"},
            })
        except mcp.OpenArtMCPError as exc:
            return ToolResult(success=False, error=f"{exc.kind}: {exc.message}", data={"error": exc.public()})
