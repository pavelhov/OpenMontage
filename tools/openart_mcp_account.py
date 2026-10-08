"""Read-only discovery and agent-mediated OpenArt MCP attempt bookkeeping."""

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


_PURE_ACTIONS = {"catalog", "form", "profile", "account"}
_ATTEMPT_ACTIONS = {
    "begin", "receive", "poll", "record_status", "download", "collect", "state", "list",
    "provenance", "qualify", "upload_prepare", "upload_receipt",
}
_ACTION_FIELDS = {
    "begin": set(), "receive": {"outcome", "error"}, "poll": set(), "download": set(),
    "record_status": {"result"}, "collect": {"downloaded_path"}, "state": set(),
    "list": set(), "provenance": set(), "qualify": set(),
    "upload_prepare": {"files", "billing_declaration", "openart_project_id"},
    "upload_receipt": {"upload_id", "result"},
}


class OpenArtMCPAccount(BaseTool):
    name = "openart_mcp_account"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "provider_account"
    provider = "openart_mcp"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.ASYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.API
    retry_policy = RetryPolicy(max_retries=0)
    dependencies: list[str] = []
    install_instructions = "Requires the installed and signed-in OpenArt connector in the agent environment."
    supports = {"read_only_discovery": True, "local_attempt_bookkeeping": True,
                "conditional_network_download": True, "agent_mediated_handoff": True,
                "local_credentials": False}
    resource_profile = ResourceProfile(network_required=True)

    input_schema = {
        "type": "object", "required": ["action"], "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": sorted(_PURE_ACTIONS | _ATTEMPT_ACTIONS)},
            "project_dir": {"type": "string", "minLength": 1},
            "attempt_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"},
            "model": {"type": "string", "minLength": 1},
            "mode": {"type": "string", "minLength": 1},
            "require": {"type": "string", "enum": ["candidate", "supported", "qualified"]},
            "outcome": {"type": "object"},
            "error": {"type": "string", "minLength": 1},
            "result": {"type": "object"},
            "downloaded_path": {"type": "string", "minLength": 1},
            "files": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
            "billing_declaration": {"type": "object"},
            "openart_project_id": {"type": "string", "minLength": 1},
            "upload_id": {"type": "string", "minLength": 1},
        },
        "allOf": [
            {"if": {"properties": {"action": {"enum": ["form", "profile"]}}, "required": ["action"]}, "then": {"required": ["model", "mode"]}},
            {"if": {"properties": {"action": {"const": "profile"}}, "required": ["action"]}, "then": {"required": ["require"]}},
            {"if": {"properties": {"action": {"enum": sorted(_ATTEMPT_ACTIONS)}}, "required": ["action"]}, "then": {"required": ["project_dir"]}},
            {"if": {"properties": {"action": {"enum": sorted(_ATTEMPT_ACTIONS - {"upload_prepare", "upload_receipt", "list"})}}, "required": ["action"]}, "then": {"required": ["attempt_id"]}},
            {"if": {"properties": {"action": {"const": "receive"}}, "required": ["action"]}, "then": {"oneOf": [{"required": ["outcome"], "not": {"required": ["error"]}}, {"required": ["error"], "not": {"required": ["outcome"]}}]}},
            {"if": {"properties": {"action": {"const": "record_status"}}, "required": ["action"]}, "then": {"required": ["result"]}},
            {"if": {"properties": {"action": {"const": "collect"}}, "required": ["action"]}, "then": {"required": ["downloaded_path"]}},
            {"if": {"properties": {"action": {"const": "upload_prepare"}}, "required": ["action"]}, "then": {"required": ["files", "billing_declaration"]}},
            {"if": {"properties": {"action": {"const": "upload_receipt"}}, "required": ["action"]}, "then": {"required": ["upload_id", "result"]}},
        ],
    }

    def get_status(self) -> ToolStatus:
        dependencies = _optional_dependencies()
        if dependencies is None:
            return ToolStatus.UNAVAILABLE
        mcp, _ = dependencies
        try:
            mcp.account_summary()
            return ToolStatus.AVAILABLE
        except Exception:
            return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> None:
        return None

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        dependencies = _optional_dependencies()
        if dependencies is None:
            return _dependency_unavailable()
        mcp, dispatch = dependencies
        action = inputs.get("action")
        try:
            if action in _PURE_ACTIONS:
                allowed = {"action"}
                if action in {"form", "profile"}:
                    allowed |= {"model", "mode"}
                if action == "profile":
                    allowed.add("require")
            elif action in _ATTEMPT_ACTIONS:
                allowed = {"action", "project_dir"}
                if action not in {"upload_prepare", "upload_receipt", "list"}:
                    allowed.add("attempt_id")
                allowed |= _ACTION_FIELDS[action]
            else:
                raise mcp.OpenArtMCPError("invalid_action", f"unknown OpenArt MCP action {action!r}")
            extras = set(inputs) - allowed
            if extras:
                raise mcp.OpenArtMCPError("invalid_argument", "unsupported action fields: " + ", ".join(sorted(extras)))
            if action == "catalog":
                data = {"models": mcp.model_catalog(), "video_model_modes": mcp.video_catalog()}
            elif action == "form":
                data = mcp.form(inputs["model"], inputs["mode"])
            elif action == "profile":
                data = mcp.load_profile(inputs["model"], inputs["mode"], require=inputs["require"])
            elif action == "account":
                data = mcp.account_summary()
            elif action in _ATTEMPT_ACTIONS:
                project_dir = inputs.get("project_dir")
                attempt_id = inputs.get("attempt_id")
                if action == "receive" and ("outcome" in inputs) == ("error" in inputs):
                    raise mcp.OpenArtMCPError("invalid_argument", "receive requires exactly one of outcome or error")
                if action == "upload_prepare":
                    if "attempt_id" in inputs:
                        raise mcp.OpenArtMCPError("invalid_argument", "upload_prepare does not accept an attempt_id")
                elif action == "upload_receipt":
                    if "attempt_id" in inputs:
                        raise mcp.OpenArtMCPError("invalid_argument", "upload_receipt does not accept an attempt_id")
                elif action != "list":
                    if not isinstance(project_dir, str) or not project_dir:
                        raise mcp.OpenArtMCPError("invalid_argument", "project_dir is required")
                    if not isinstance(attempt_id, str) or not attempt_id:
                        raise mcp.OpenArtMCPError("invalid_argument", "attempt_id is required")
                data = dispatch.invoke_action(
                    action, project_dir, attempt_id,
                    **{key: value for key, value in inputs.items() if key in _ACTION_FIELDS[action]},
                )
            else:
                raise mcp.OpenArtMCPError("invalid_action", f"unknown OpenArt MCP action {action!r}")
            result_data = data if isinstance(data, dict) else {"value": data}
            result_data = {**result_data}
            result_data.setdefault("action", action)
            result_data.setdefault("provider", self.provider)
            result_data.setdefault("reservations", 0)
            return ToolResult(success=True, cost_usd=None, data={**result_data, "evidence": result_data})
        except mcp.OpenArtMCPError as exc:
            return ToolResult(success=False, error=f"{exc.kind}: {exc.message}", data={"error": exc.public()})
        except (KeyError, TypeError):
            error = mcp.OpenArtMCPError("invalid_argument", "required fields for this action are missing or malformed")
            return ToolResult(success=False, error=f"{error.kind}: {error.message}", data={"error": error.public()})
