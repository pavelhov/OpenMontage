"""Capability-level video selector that routes between generation and stock providers.

Provider discovery is automatic — any BaseTool with capability="video_generation"
is picked up from the registry.  Adding a new video provider requires only creating
the tool file in tools/video/; no changes to this selector are needed.
"""

from __future__ import annotations

import os
from pathlib import Path

from lib.video_model_selection import MODEL_SELECTION_INTENT_SCHEMA

from tools.base_tool import (
    BaseTool,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)


class VideoSelector(BaseTool):
    name = "video_selector"
    version = "0.3.1"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "selector"
    stability = ToolStability.BETA
    runtime = ToolRuntime.HYBRID
    agent_skills = [
        "ai-video-gen",
        "create-video",
        "ltx2",
        "gemini-omni",
        "atlas-cloud",
    ]

    # Operations that REQUIRE motion: an image-only tool (image_selector) is not
    # an acceptable last-resort fallback for these, so fallback_tools_for() drops it.
    MOTION_REQUIRED_OPERATIONS = frozenset({"image_to_video", "reference_to_video", "first_last_frame", "video_edit"})
    # Default score gap for the preferred_provider override (see input_schema).
    PREFERRED_PROVIDER_GAP = 0.15

    capabilities = [
        "text_to_video", "image_to_video", "reference_to_video", "first_last_frame", "video_edit", "stock_video",
        "provider_selection", "search_video", "download_video",
    ]
    supports = {
        "user_preference_routing": True,
        "offline_fallback": True,
        "reference_image": True,
        "stock_fallback": True,
    }
    best_for = [
        "preflight routing",
        "user-facing recommendation flows",
        "switching between cloud, local, and stock video tools",
    ]

    input_schema = {
        "type": "object",
        "properties": {
            "model_selection_intent": MODEL_SELECTION_INTENT_SCHEMA,
            "preferred_tool": {
                "type": "string",
                "description": "Exact tool name; never falls back.",
            },
            "hosting_provider": {
                "type": "string",
                "description": "Required API host, e.g. fal.ai, atlascloud, replicate.",
            },
            "prompt": {"type": "string"},
            "preferred_provider": {
                "type": "string",
                "description": "Provider name or 'auto'. Valid values are discovered at runtime from the registry.",
                "default": "auto",
            },
            "preferred_provider_gap": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "default": 0.15,
                "description": (
                    "Max weighted-score gap (0-1) within which an explicit preferred_provider "
                    "overrides the top-ranked provider. If the preferred provider's best score "
                    "falls more than this far below the overall top, the preference is ignored "
                    "and the top-ranked provider wins. Default 0.15 — honors a preference unless "
                    "it would drag selection to a drastically worse provider."
                ),
            },
            "allowed_providers": {"type": "array", "items": {"type": "string"}},
            "operation": {
                "type": "string",
                "enum": ["text_to_video", "image_to_video", "reference_to_video", "first_last_frame", "video_edit", "rank"],
                "default": "text_to_video",
            },
            "target_operation": {
                "type": "string",
                "enum": ["text_to_video", "image_to_video", "reference_to_video", "first_last_frame", "video_edit"],
                "description": "Operation to score when operation='rank'.",
                "default": "text_to_video",
            },
            "aspect_ratio": {
                "type": "string",
                "enum": ["16:9", "9:16", "1:1"],
                "default": "16:9",
                "description": "Video aspect ratio. Passed through to the selected provider.",
            },
            "duration": {
                "type": "string",
                "description": "Duration hint (e.g., '5', '10'). Passed through to the selected provider.",
            },
            "image_path": {"type": "string"},
            "native_params": {"type": "object"},
            "input_assets": {"type": "array", "items": {"type": "object"}},
            "end_image_upload_id": {"type": "string"},
            "reference_image_upload_ids": {"type": "array", "items": {"type": "string"}},
            "reference_video_upload_ids": {"type": "array", "items": {"type": "string"}},
            "reference_audio_upload_ids": {"type": "array", "items": {"type": "string"}},
            "reference_audio_paths": {"type": "array", "items": {"type": "string"}},
            "first_frame": {"type": "string"},
            "last_frame": {"type": "string"},
            "keyframes": {"type": "array", "items": {"type": "object"}},
            "voices": {"type": "array", "items": {"type": "string"}},
            "cli_session_id": {"type": "string"},
            "allow_unknown_cost": {"type": "boolean"},
            "cwd": {"type": "string"},
            "timeout_seconds": {"type": "integer"},
            "reference_image_path": {
                "type": "string",
                "description": "Local path to a reference image for image_to_video. Auto-uploaded if the provider requires a URL.",
            },
            "reference_image_url": {
                "type": "string",
                "description": "URL of a reference image for image_to_video.",
            },
            "reference_image_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Reference image URLs for providers that support reference-conditioned video.",
            },
            "reference_image_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Local reference image paths for providers that support reference-conditioned video.",
            },
            "reference_video_url": {
                "type": "string",
                "description": "Reference video URL for providers that support video-conditioned generation.",
            },
            "reference_video_path": {
                "type": "string",
                "description": "Local reference video path. Providers that require URLs should reject this clearly.",
            },
            "reference_video_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Reference video URLs for mixed-media generation.",
            },
            "reference_video_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Local reference video paths for mixed-media generation.",
            },
            "reference_audio_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Reference audio URLs for mixed-media generation.",
            },
            "reference_audio_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Local reference audio paths for mixed-media generation.",
            },
            "endpoint_requirement_id": {"type": "string", "minLength": 1},
            "last_image_url": {"type": "string", "description": "Optional final frame for first/last-frame generation."},
            "last_image_path": {"type": "string", "description": "Optional local final frame."},
            "video_url": {"type": "string", "description": "Source video URL for video editing."},
            "video_path": {"type": "string", "description": "Local source video for video editing."},
            "video_clips": {"type": "array", "items": {"type": "object"}},
            "refers": {"type": "array", "items": {"type": "object"}},
            "image_list": {
                "type": "array",
                "description": "Provider-specific list of image references, e.g. Kling Official Video Omni.",
            },
            "video_list": {
                "type": "array",
                "description": "Provider-specific list of video references, e.g. Kling Official Video Omni.",
            },
            "element_list": {
                "type": "array",
                "description": "Provider-specific element references, e.g. Kling Official element_id objects.",
            },
            "multi_shot": {
                "type": "boolean",
                "description": "Provider-specific multi-shot mode.",
            },
            "shot_type": {
                "type": "string",
                "description": "Provider-specific multi-shot type.",
            },
            "multi_prompt": {
                "type": "array",
                "description": "Structured multi-shot prompts; not inferred from prose.",
            },
            "image_url": {
                "type": "string",
                "description": "Alias for reference_image_url (used by some providers like Kling via fal.ai).",
            },
            "resolution": {
                "type": "string",
                "description": "Resolution hint for providers that support named output resolutions.",
            },
            "api_family": {
                "type": "string",
                "description": "Provider-specific API family hint passed through when supported, e.g. classic/turbo/omni.",
            },
            "model_name": {
                "type": "string",
                "description": "Provider-specific model name passed through when supported.",
            },
            "model": {
                "type": "string",
                "description": "Exact provider model id, e.g. an Atlas Cloud live model route.",
            },
            "model_variant": {
                "type": "string",
                "description": "Provider route variant, e.g. standard or developer.",
            },
            "mode": {
                "type": "string",
                "description": "Provider-specific quality mode passed through when supported.",
            },
            "sound": {
                "type": "string",
                "description": "Provider-specific native audio toggle passed through when supported.",
            },
            "watermark": {
                "type": "boolean",
                "description": "Provider-specific watermark toggle passed through when supported.",
            },
            "callback_url": {
                "type": "string",
                "description": "Provider-specific callback URL. Current OpenMontage providers still poll by default.",
            },
            "external_task_id": {
                "type": "string",
                "description": "Provider-specific idempotency/provenance task id.",
            },
            "workflow_json": {
                "type": "string",
                "description": (
                    "Optional full ComfyUI workflow JSON. Routes to a custom-workflow-capable "
                    "provider (e.g. comfyui_video) based on server availability, not bundled "
                    "model readiness. Requires output_node."
                ),
            },
            "workflow_path": {
                "type": "string",
                "description": (
                    "Optional path to a ComfyUI workflow JSON file. Routes to a custom-workflow-"
                    "capable provider based on server availability. Requires output_node."
                ),
            },
            "output_node": {
                "type": "string",
                "description": "ComfyUI output node ID for a custom workflow_json/workflow_path.",
            },
            "workflow_name": {
                "type": "string",
                "description": "Optional human-readable provenance label for a custom workflow.",
            },
            "workflow_model": {
                "type": "string",
                "description": "Optional model/provenance label for a custom workflow.",
            },
            "workflow_model_stack": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Optional provenance metadata for custom workflow dependencies.",
            },
            "output_path": {"type": "string"},
        },
    }

    def _providers(self) -> list[BaseTool]:
        """Auto-discover video generation providers from the registry."""
        from tools.tool_registry import registry

        registry.ensure_discovered()
        return [
            t
            for t in registry.get_by_capability("video_generation")
            if t.name != self.name
        ]

    @property
    def fallback_tools(self) -> list[str]:
        """Static (input-agnostic) fallback list for external consumers / contracts.

        See :meth:`fallback_tools_for` for the input-aware form used during
        routing, which drops ``image_selector`` for motion-required briefs.
        """
        return [t.name for t in self._automatic_candidates()] + ["image_selector"]

    def fallback_tools_for(self, inputs: dict[str, object]) -> list[str]:
        """Input-aware fallback list used during routing.

        ``image_selector`` is a legitimate degraded last-resort for a still-image
        brief (text_to_video with no motion requirement), but for motion-required
        operations (image_to_video / reference_to_video) an image-only fallback
        silently defeats the brief. Gate it here at the selector layer so a direct
        caller — with no director skill enforcing the prohibition — still cannot
        fall back to an image tool when motion was requested.
        """
        providers = self._providers()
        if self._exact_explicit_candidate(inputs, providers) is not None:
            return []
        tools = [t.name for t in self._filter_candidates(inputs, providers)]
        operation = inputs.get("operation", "text_to_video")
        if operation in self.MOTION_REQUIRED_OPERATIONS or self._requires_final_frame(inputs):
            return tools
        return tools + ["image_selector"]

    @property
    def provider_matrix(self) -> dict[str, dict[str, str]]:
        """Built at runtime from each provider's best_for field."""
        matrix = {}
        for tool in self._automatic_candidates():
            strength = ", ".join(tool.best_for) if tool.best_for else tool.name
            matrix[tool.provider] = {"tool": tool.name, "strength": strength}
        return matrix

    def get_status(self) -> ToolStatus:
        if any(tool.get_status() == ToolStatus.AVAILABLE for tool in self._automatic_candidates()):
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, object]) -> float:
        candidates = self._filter_candidates(inputs, self._providers())
        if self._requires_final_frame(inputs) and not any(self._tool_selectable(tool, inputs) for tool in candidates):
            raise ValueError(
                "Cannot estimate pinned final frame cost: no eligible available route. "
                "Check the selected provider's final-frame support, explicit model, and credentials; "
                "an unavailable route is not free."
            )
        if not candidates:
            return 0.0
        explicit = self._exact_explicit_candidate(inputs, candidates)
        if explicit is not None:
            return explicit.estimate_cost(inputs)
        tool, _ = self._select_best_tool(inputs, candidates, self._prepare_task_context(inputs))
        return tool.estimate_cost(inputs) if tool else 0.0

    def estimate_runtime(self, inputs: dict[str, object]) -> float:
        candidates = self._filter_candidates(inputs, self._providers())
        if not candidates:
            return 0.0
        explicit = self._exact_explicit_candidate(inputs, candidates)
        if explicit is not None:
            return explicit.estimate_runtime(inputs)
        tool, _ = self._select_best_tool(inputs, candidates, self._prepare_task_context(inputs))
        return tool.estimate_runtime(inputs) if tool else 0.0

    def execute(self, inputs: dict[str, object]) -> ToolResult:
        from lib.scoring import rank_providers

        inputs = dict(inputs)
        model_selection = None
        if "model_selection_intent" in inputs:
            from lib.video_model_selection import plan_video_model_selection

            plan_inputs = self._rank_inputs(inputs) if inputs.get("operation") == "rank" else inputs
            plan = plan_video_model_selection(plan_inputs, self._providers())
            if plan["status"] != "planned":
                return ToolResult(success=False, data={"model_selection": plan, "fallback_tools": [],
                    "fallback_attempted": False, "dispatch_status": "not_dispatched"},
                    error="; ".join(blocker["message"] for blocker in plan["blockers"]))
            if inputs.get("operation") == "rank":
                return ToolResult(success=True, data={"model_selection": plan,
                    "planned_request": plan["planned_request"], "dispatch_status": "not_dispatched"})
            # Continue through the established singleton route in this same
            # invocation: a second selector invocation would be a nested dispatch.
            # Governed production approves the exact rank-mode planned_request
            # before executing it; planning itself grants no authority.
            inputs = plan["planned_request"]
            model_selection = plan
        # Native aliases are accepted only on an explicitly locked provider route.
        if "last_frame" in inputs and any(self._is_exact_provider_pin(inputs, provider) for provider in ("grok_cli", "openart_cli")):
            if inputs.get("last_image_path") is not None and (
                not isinstance(inputs["last_frame"], str)
                or not isinstance(inputs["last_image_path"], str)
                or Path(inputs["last_frame"]).expanduser().resolve() != Path(inputs["last_image_path"]).expanduser().resolve()
            ):
                return ToolResult(success=False, data={"fallback_tools": [], "dispatch_status": "not_dispatched"},
                                  error="Conflicting last_frame and last_image_path")
            inputs["last_image_path"] = inputs.pop("last_frame")
        if self._is_exact_provider_pin(inputs, "grok_cli") and any(key in inputs for key in ("model", "model_name")):
            return ToolResult(success=False, data={"fallback_tools": [], "dispatch_status": "not_dispatched"},
                error="Grok CLI video does not expose Imagine model selection; omit model/model_name for the subscription CLI route.")
        unsupported_frame_controls = {
            "last_frame", "last_frame_url", "last_frame_path", "end_frame",
            "end_frame_url", "end_frame_path", "loop", "seamless_loop",
        }
        if self._is_exact_provider_pin(inputs, "openart_mcp"):
            unsupported_frame_controls.discard("last_frame")
        if unsupported_frame_controls.intersection(inputs):
            return ToolResult(
                success=False,
                data={"fallback_tools": [], "fallback_attempted": False, "dispatch_status": "not_dispatched"},
                error="Use last_image_url or last_image_path for a pinned final frame; no loop switch or other ending-frame alias is supported.",
            )
        if inputs.get("endpoint_requirement_id") and not (inputs.get("last_image_url") or inputs.get("last_image_path")
                or self._is_exact_provider_pin(inputs, "openart_mcp") and (inputs.get("last_frame") or any(
                    isinstance(row, dict) and row.get("role") == "last_frame" for row in inputs.get("input_assets", [])))):
            return ToolResult(
                success=False,
                data={"fallback_tools": [], "fallback_attempted": False, "dispatch_status": "not_dispatched"},
                error="An endpoint_requirement_id requires the approved last_image_url or last_image_path; resolve the ending-frame asset before generation.",
            )
        candidates = self._providers()

        # Crossed scopes: a preferred explicit-only provider outside a non-empty
        # allowed set (or an allowed set that is not exactly that provider) is a
        # contradictory route approval. Refuse before ranking or provider calls.
        preferred = inputs.get("preferred_provider", "auto")
        allowed = self._allowed_provider_set(inputs)
        if (inputs.get("operation") != "rank"
                and preferred not in (None, "auto") and allowed and allowed != {preferred}
                and any(t.provider == preferred and self._is_explicit_only(t) for t in candidates)):
            frame_note = (
                " Grok CLI pins require local last_image_path on CLI >=1.0.34; HTTPS last_image_url needs "
                "Grok REST (separately API-billed) with explicit route approval and credentials. "
                "No fallback was attempted."
                if self._requires_final_frame(inputs) else "")
            return ToolResult(
                success=False,
                data={"alternatives_considered": [], "fallback_tools": [], "fallback_attempted": False,
                      "dispatch_status": "not_dispatched", "crossed_scopes": {
                          "preferred_provider": preferred, "allowed_providers": sorted(allowed)}},
                error=(f"preferred_provider {preferred!r} is explicit-only and needs allowed_providers "
                       f"exactly [{preferred!r}]; got {sorted(allowed)}. No provider was called."
                       + frame_note),
            )

        # Rank mode — return scored provider rankings without generating
        if inputs.get("operation") == "rank":
            rank_inputs = self._rank_inputs(inputs)
            task_context = self._prepare_task_context(rank_inputs)
            pinned_explicit = self._exact_explicit_candidate(rank_inputs, candidates)
            if (
                pinned_explicit is not None
                and not self._rank_operation_eligible(pinned_explicit, rank_inputs)
            ):
                return ToolResult(
                    success=True,
                    data={
                        "rankings": [],
                        "explanation": (
                            f"{pinned_explicit.name}: does not support "
                            f"{rank_inputs['operation']}"
                        ),
                        "normalized_task_context": task_context,
                    },
                )
            # Explicit-only providers stay out of automatic ranking. For an
            # exact singleton pin, return an unscored preflight row only when
            # the requested target operation is actually supported.
            explicit = self._exact_explicit_candidate(
                rank_inputs,
                self._filter_candidates(rank_inputs, candidates),
            )
            if explicit is not None:
                return ToolResult(
                    success=True,
                    data={
                        "rankings": [self._explicit_pin_ranking(explicit)],
                        "explanation": f"{explicit.name}: explicitly pinned; not scored",
                        "normalized_task_context": task_context,
                    },
                )
            candidates = self._filter_candidates(rank_inputs, candidates, rank_mode=True)
            rankings = rank_providers(candidates, task_context)
            return ToolResult(
                success=True,
                data={
                    "rankings": self._serialize_rankings(candidates, rankings),
                    "explanation": "\n".join(r.explain() for r in rankings[:5]),
                    "normalized_task_context": task_context,
                },
            )

        # Normal generation — use scored selection
        task_context = self._prepare_task_context(inputs)
        # An exact explicit-only singleton pin is decided before filtering so a
        # missing control or unqualified model fails visibly instead of being
        # filtered away into scoring/fallback.
        pinned = self._exact_explicit_candidate(inputs, candidates)
        if pinned is not None:
            missing = self._missing_pinned_controls(inputs, pinned)
            if missing:
                return ToolResult(
                    success=False,
                    data={"alternatives_considered": [], "fallback_tools": [], "fallback_attempted": False,
                          "dispatch_status": "not_dispatched", "missing_controls": missing},
                    error=("The explicitly pinned provider does not support the requested "
                           + ", ".join(missing)
                           + ". No prompt substitute, fallback or credit reservation was attempted; "
                           "choose a route that supports it or revise the creative request."),
                )
        candidates = self._filter_candidates(inputs, candidates)
        explicit_route = pinned is not None or self._exact_explicit_candidate(inputs, candidates) is not None
        if pinned is not None and self._exact_explicit_candidate(inputs, candidates) is None:
            tool, score = None, None  # pinned route filtered out: never score or fall back
        else:
            tool, score = self._select_best_tool(inputs, candidates, task_context)
        if tool is None:
            return ToolResult(
                success=False,
                data=(
                    {"alternatives_considered": [], "fallback_tools": [], "fallback_attempted": False, "dispatch_status": "not_dispatched"}
                    if explicit_route or self._requires_final_frame(inputs) else {}
                ),
                error=(
                    "No available provider supports the requested pinned final frame/model on the requested route. "
                    "Check the provider's pinned_final_frame capability, exact model, and credential availability. "
                    "Grok CLI pins require local last_image_path on CLI >=1.0.34; HTTPS last_image_url needs "
                    "Grok REST (separately API-billed) with explicit route approval and credentials. "
                    "No fallback was attempted."
                    if self._requires_final_frame(inputs) else "No video generation provider available."
                ),
            )

        # Adapt input keys: stock tools use 'query' while generators use 'prompt'
        adapted = dict(inputs)
        if hasattr(tool, "input_schema"):
            required = tool.input_schema.get("properties", {})
            if "query" in required and "query" not in adapted:
                adapted["query"] = adapted.get("prompt", "")
            # The selector accepts duration hints as strings; strict provider
            # schemas accept integral seconds. Do not truncate fractional input.
            duration = adapted.get("duration")
            if required.get("duration", {}).get("type") == "integer" and isinstance(duration, str) and duration.isdecimal():
                adapted["duration"] = int(duration)

        # Auto-resolve reference_image_path to a URL for providers that need it
        if adapted.get("operation") == "image_to_video" and adapted.get(
            "reference_image_path"
        ):
            tool_props = getattr(tool, "input_schema", {}).get("properties", {})
            # If the provider uses image_url (not reference_image_path), upload and convert
            if "image_path" in tool_props:
                if adapted.get("image_path") and (
                    not isinstance(adapted["image_path"], str)
                    or not isinstance(adapted["reference_image_path"], str)
                    or Path(adapted["image_path"]).expanduser().resolve() != Path(adapted["reference_image_path"]).expanduser().resolve()
                ):
                    return ToolResult(success=False, error="Conflicting image_path and reference_image_path")
                adapted["image_path"] = adapted["reference_image_path"]
            elif "image_url" in tool_props and "image_url" not in adapted:
                try:
                    from tools.video._shared import upload_image_fal

                    adapted["image_url"] = upload_image_fal(
                        adapted["reference_image_path"]
                    )
                except Exception as e:
                    return ToolResult(
                        success=False,
                        data=(
                            {"alternatives_considered": [], "fallback_tools": []}
                            if explicit_route else {}
                        ),
                        error=f"Failed to upload reference image: {e}",
                    )

        # Routing-only constraints are consumed by the selector; never forward
        # them to the provider tool.
        if getattr(tool, "provider", None) != "openart_mcp":
            adapted.pop("preferred_tool", None)
            adapted.pop("hosting_provider", None)
        if tool.input_schema.get("additionalProperties") is False and getattr(tool, "provider", None) != "openart_mcp":
            for key in ("preferred_provider", "preferred_provider_gap", "allowed_providers", "task_context", "target_operation"):
                adapted.pop(key, None)
        result = tool.execute(adapted)
        if model_selection is not None:
            result.data["model_selection"] = model_selection
            result.data["fallback_tools"] = []
            result.data["alternatives_considered"] = []
        if explicit_route:
            result.data["alternatives_considered"] = []
            result.data["fallback_tools"] = []
        if result.success:
            result.data.setdefault("selected_tool", tool.name)
            result.data["selected_provider"] = tool.provider
            result.data["selection_reason"] = (
                score.explain() if score else f"Selected {tool.provider} ({tool.name})"
            )
            if score:
                result.data["provider_score"] = score.to_dict()
            result.data.update(self._tool_context_payload(tool))
            if not explicit_route and model_selection is None:
                result.data["alternatives_considered"] = [
                    t.name for t in candidates
                    if t.name != tool.name and t.get_status().value == "available"
                ]
                # Input-aware fallback list (drops image_selector for motion-required briefs).
                result.data.setdefault("fallback_tools", self.fallback_tools_for(inputs))
        return result

    def _select_best_tool(
        self,
        inputs: dict[str, object],
        candidates: list[BaseTool],
        task_context: dict[str, object],
    ) -> tuple[BaseTool | None, object]:
        """Select the best provider using scored ranking.

        Respects preferred_provider and environment hints as tie-breakers,
        but the scoring engine drives the primary selection.
        """
        from lib.scoring import rank_providers

        candidates = self._filter_candidates(inputs, candidates)

        explicit = self._exact_explicit_candidate(inputs, candidates)
        if explicit is not None:
            return (explicit, None) if self._tool_selectable(explicit, inputs) else (None, None)

        preferred = inputs.get("preferred_provider", "auto")

        env_hint = os.environ.get("VIDEO_GEN_LOCAL_MODEL", "").lower()
        env_map = {
            "wan2.2-ti2v-5b": "wan",
            "wan2.2-t2v-a14b": "wan",
            "wan2.2-i2v-a14b": "wan",
            "wan2.1-1.3b": "wan",
            "wan2.1-14b": "wan",
            "hunyuan-1.5": "hunyuan",
            "ltx2-local": "ltx",
            "cogvideo-5b": "cogvideo",
            "cogvideo-2b": "cogvideo",
        }
        if preferred == "auto" and env_hint in env_map:
            preferred = env_map[env_hint]

        rankings = rank_providers(candidates, task_context)

        # Selectable tools, keyed by NAME (not provider). Keying by provider
        # string shadowed one of two tools that legitimately share a provider —
        # e.g. seedance_video (fal) and seedance_replicate both have
        # provider="seedance", so only the first-registered was ever reachable.
        # Keying by name keeps every backend selectable; ranking picks the best.
        selectable_by_name: dict[str, BaseTool] = {
            tool.name: tool
            for tool in candidates
            if self._tool_selectable(tool, inputs)
        }

        def _tool_for(score: object) -> BaseTool | None:
            return selectable_by_name.get(getattr(score, "tool_name", None))

        # If a preferred provider is explicitly requested, honor it ONLY when its
        # best ranked tool is within a configurable score gap of the overall top.
        # The prior code returned the preferred provider on the first ranking
        # match regardless of how far below the top it scored (the comment
        # claimed "unless drastically worse" but no gate enforced it).
        if preferred != "auto" and rankings:
            try:
                gap = float(
                    inputs.get("preferred_provider_gap", self.PREFERRED_PROVIDER_GAP)
                )
            except (TypeError, ValueError):
                gap = self.PREFERRED_PROVIDER_GAP
            top_score = rankings[0].weighted_score
            preferred_score = next(
                (
                    s
                    for s in rankings
                    if s.provider == preferred and _tool_for(s) is not None
                ),
                None,
            )
            if (
                preferred_score is not None
                and preferred_score.weighted_score >= top_score - gap
            ):
                return _tool_for(preferred_score), preferred_score

        # Return the highest-scored selectable provider
        for score in rankings:
            tool = _tool_for(score)
            if tool is not None:
                return tool, score

        return None, None

    def _prepare_task_context(self, inputs: dict[str, object]) -> dict[str, object]:
        from lib.scoring import normalize_task_context

        return normalize_task_context(
            inputs.get("task_context", {}),
            prompt=str(inputs.get("prompt", "")),
            capability=self.capability,
            operation=str(inputs.get("operation", "text_to_video")),
        )

    @staticmethod
    def _rank_inputs(inputs: dict[str, object]) -> dict[str, object]:
        rank_inputs = dict(inputs)
        rank_inputs["operation"] = inputs.get("target_operation", "text_to_video")
        return rank_inputs

    @staticmethod
    def _tool_context_payload(tool: BaseTool) -> dict[str, object]:
        info = tool.get_info()
        return {
            "selected_tool_agent_skills": info.get("agent_skills", []),
            "required_agent_skills": info.get("agent_skills", []),
            "selected_tool_usage_location": info.get("usage_location"),
            "selected_tool_best_for": info.get("best_for", []),
        }

    def _serialize_rankings(
        self, candidates: list[BaseTool], rankings: list[object]
    ) -> list[dict[str, object]]:
        tool_by_name = {tool.name: tool for tool in candidates}
        serialized: list[dict[str, object]] = []
        for score in rankings:
            item = score.to_dict()
            tool = tool_by_name.get(score.tool_name)
            if tool:
                info = tool.get_info()
                item["agent_skills"] = info.get("agent_skills", [])
                item["usage_location"] = info.get("usage_location")
                item["best_for"] = info.get("best_for", [])
                item["supports"] = info.get("supports", {})
                item["status"] = str(tool.get_status())
            serialized.append(item)
        return serialized

    @staticmethod
    def _explicit_pin_ranking(tool: BaseTool) -> dict[str, object]:
        """Serialize an exact explicit-only pin without sending it to scoring."""
        info = tool.get_info()
        supports = info.get("supports", getattr(tool, "supports", {}))
        cost_preestimate = supports.get("cost_preestimate")
        return {
            "tool_name": tool.name,
            "provider": tool.provider,
            "weighted_score": None,
            "selection_mode": "explicit_pin",
            "cost_estimate_status": (
                "unknown" if cost_preestimate is False else "not_evaluated"
            ),
            "estimated_cost_usd": None,
            "agent_skills": info.get("agent_skills", []),
            "usage_location": info.get("usage_location"),
            "best_for": info.get("best_for", []),
            "supports": supports,
            "status": str(tool.get_status()),
        }

    def _filter_candidates(
        self,
        inputs: dict[str, object],
        candidates: list[BaseTool],
        *,
        rank_mode: bool = False,
    ) -> list[BaseTool]:
        # An endpoint requirement makes an explicit provider choice a constraint:
        # never turn a CLI request into an API-billed provider behind the caller.
        if self._requires_final_frame(inputs) and inputs.get("preferred_provider", "auto") != "auto":
            candidates = [tool for tool in candidates if tool.provider == inputs["preferred_provider"]]
        allowed = self._allowed_provider_set(inputs)
        if allowed:
            candidates = [tool for tool in candidates if tool.provider in allowed]

        candidates = [
            tool for tool in candidates
            if not self._is_explicit_only(tool)
            or (
                not rank_mode
                and self._is_exact_provider_pin(inputs, tool.provider)
            )
        ]

        # Upstream exact tool/host/model constraints apply after the fork's
        # allowed-provider and explicit-only (CLI opt-in) filtering, so a
        # preferred_tool/hosting_provider can only narrow, never re-admit an
        # explicit-only or disallowed provider.
        from tools.provider_routing import filter_explicit_route

        candidates = filter_explicit_route(inputs, candidates)
        exact_model = inputs.get("model")
        if exact_model:
            model_matches = [
                tool
                for tool in candidates
                if exact_model
                in getattr(tool, "input_schema", {})
                .get("properties", {})
                .get("model", {})
                .get("enum", [])
                or exact_model in tool.get_info().get("model_catalog", {})
            ]
            candidates = model_matches

        if self._requires_final_frame(inputs):
            candidates = [tool for tool in candidates if self._final_frame_eligible(tool, inputs)]

        # A caller-supplied custom workflow is provider-specific (ComfyUI graph
        # JSON). Route it only to custom-workflow-capable providers whose server
        # is reachable — bundled-model readiness is irrelevant in that case.
        if self._has_custom_workflow(inputs):
            return [t for t in candidates if self._custom_workflow_eligible(t, inputs)]

        operation = inputs.get("operation", "text_to_video")
        if operation == "rank":
            operation = inputs.get("target_operation", "text_to_video")

        filtered: list[BaseTool] = []
        matched_operation = False
        for tool in candidates:
            supports = getattr(tool, "supports", {})
            props = getattr(tool, "input_schema", {}).get("properties", {})

            if getattr(tool, 'provider', None) in {'openart_cli', 'openart_mcp'} and hasattr(tool, '_model_catalog'):
                entry = tool._model_catalog().get(inputs.get('model'), {}).get('modes', {}).get(inputs.get('mode'))
                if entry is None and getattr(tool, 'provider', None) == 'openart_mcp':
                    matched_operation = True
                    continue  # exact native mode is mandatory; global flags cannot admit it
                if entry is not None:
                    matched_operation = True
                    if (entry.get('production_ready') is True or self._is_mcp_qualification(inputs, tool)) and not entry.get('native_capabilities', {}).get('unreachable'):
                        caps = entry.get('native_capabilities', {})
                        if entry.get('operation') == operation or (
                                operation == 'first_last_frame' and caps.get('roles', {}).get('last_frame', {}).get('supported') is True
                                and (getattr(tool, 'provider', None) != 'openart_mcp' or (inputs.get('mode') == 'image2video'
                                    and caps.get('roles', {}).get('first_frame', {}).get('supported') is True))):
                            filtered.append(tool)
                    continue

            if operation == "first_last_frame":
                matched_operation = True
                if supports.get("first_last_frame") is True:
                    filtered.append(tool)
                continue

            if operation == "image_to_video":
                if (
                    supports.get("image_to_video")
                    or "image_url" in props
                    or "reference_image_url" in props
                ):
                    matched_operation = True
                    if self._operation_ready(tool, "image_to_video"):
                        filtered.append(tool)
                continue

            if operation == "reference_to_video":
                if (
                    supports.get("reference_to_video")
                    or "reference_image_urls" in props
                ):
                    matched_operation = True
                    filtered.append(tool)
                continue

            matched_operation = True
            if self._operation_ready(tool, str(operation)):
                filtered.append(tool)

        return filtered if matched_operation else candidates

    def _automatic_candidates(self) -> list[BaseTool]:
        return [tool for tool in self._providers() if not self._is_explicit_only(tool)]

    @staticmethod
    def _is_explicit_only(tool: BaseTool) -> bool:
        return bool(getattr(tool, "supports", {}).get("explicit_selection_only"))

    @staticmethod
    def _missing_pinned_controls(inputs: dict[str, object], tool: BaseTool | None) -> list[str]:
        """Requested controls the exact pinned tool cannot honor (fail before reserve)."""
        if tool is None:
            return []
        supports = getattr(tool, "supports", {}) or {}
        missing: list[str] = []
        if getattr(tool, 'provider', None) in {'openart_cli', 'openart_mcp'} and hasattr(tool, '_model_catalog'):
            entry = (tool._model_catalog().get(inputs.get('model'), {}).get('modes', {}).get(inputs.get('mode')) or {})
            if not entry and getattr(tool, 'provider', None) == 'openart_mcp':
                return ['exact model/mode is not an observed OpenArt MCP route']
            if getattr(tool, 'provider', None) == 'openart_mcp' and entry.get('production_ready') is not True and not VideoSelector._is_mcp_qualification(inputs, tool):
                missing.append('exact MCP model/mode is not a production-ready native route for this account')
            caps = entry.get('native_capabilities')
            if caps:
                supports = {**supports, 'first_last_frame': caps['roles']['last_frame']['supported'],
                            'multiple_reference_images': caps['roles']['reference_image']['supported'],
                            'native_audio': any(caps['params'].get(field, {}).get('binding') in {'flag', 'native_param'}
                                for field in ('generateAudio', 'generateSound', 'audio'))}
                if caps.get('unreachable'):
                    missing.append('native_mode: ' + str(caps.get('unreachable_reason')))
                roles = [a.get('role') for a in inputs.get('input_assets', []) if isinstance(a, dict)]
                for role in roles:
                    if caps['roles'].get(role, {}).get('supported') is not True:
                        missing.append(role + ': unsupported native transport')
                for name in inputs.get('native_params', {}):
                    if caps['params'].get(name, {}).get('binding') not in {'flag', 'prompt', 'native_param'}:
                        missing.append(name + ': unsupported native transport')
        if (inputs.get("last_image_path") or inputs.get("last_image_url")
                or inputs.get("endpoint_requirement_id")
                or inputs.get("operation") == "first_last_frame") and supports.get("first_last_frame") is not True:
            missing.append("first_last_frame (end-frame pin)")
        if (inputs.get("native_audio") or inputs.get("voices") or inputs.get("voice")
                or inputs.get("generate_audio")) and supports.get("native_audio") is not True:
            missing.append("native_audio")
        refs = inputs.get("reference_image_paths") or inputs.get("reference_image_urls")
        if (isinstance(refs, (list, tuple)) and len(refs) > 1
                and supports.get("multiple_reference_images") is not True):
            missing.append("multiple_reference_images")
        return missing

    @staticmethod
    def _allowed_provider_set(inputs: dict[str, object]) -> set[str]:
        values = inputs.get("allowed_providers") or []
        if not isinstance(values, (list, tuple, set, frozenset)):
            values = [values]
        return {
            value.strip()
            for item in values
            if (value := str(item).strip())
        }

    def _is_exact_provider_pin(self, inputs: dict[str, object], provider: str) -> bool:
        preferred = inputs.get("preferred_provider", "auto")
        return preferred == provider and self._allowed_provider_set(inputs) == {provider}

    def _exact_explicit_candidate(
        self,
        inputs: dict[str, object],
        candidates: list[BaseTool],
    ) -> BaseTool | None:
        preferred = inputs.get("preferred_provider")
        return next(
            (
                tool for tool in candidates
                if tool.provider == preferred
                and self._is_explicit_only(tool)
                and self._is_exact_provider_pin(inputs, tool.provider)
            ),
            None,
        )

    @staticmethod
    def _requires_final_frame(inputs: dict[str, object]) -> bool:
        operation = inputs.get("target_operation") if inputs.get("operation") == "rank" else inputs.get("operation")
        return operation == "first_last_frame" or any(
            key in inputs for key in ("last_image_url", "last_image_path", "last_frame", "endpoint_requirement_id")
        ) or any(isinstance(row, dict) and row.get("role") == "last_frame" for row in inputs.get("input_assets", []))

    @staticmethod
    def _final_frame_eligible(tool: BaseTool, inputs: dict[str, object]) -> bool:
        supports = getattr(tool, "supports", {})
        props = getattr(tool, "input_schema", {}).get("properties", {})
        if getattr(tool, 'provider', None) in {'openart_cli', 'openart_mcp'} and hasattr(tool, '_model_catalog'):
            entry = tool._model_catalog().get(inputs.get('model'), {}).get('modes', {}).get(inputs.get('mode')) or {}
            return (entry.get('production_ready') is True or VideoSelector._is_mcp_qualification(inputs, tool)) and entry.get('native_capabilities', {}).get('roles', {}).get('last_frame', {}).get('supported') is True
        if supports.get("first_last_frame") is False:
            return False
        models = getattr(tool, "first_last_frame_models", None)
        if models is not None and inputs.get("model") not in models:
            return False
        # Do not let a provider silently discard a final-frame constraint.
        for key in ("last_image_url", "last_image_path"):
            if key in inputs and not props.get(key):
                return False
        return bool(supports.get("first_last_frame") or props.get("last_image_url") or props.get("last_image_path"))

    @staticmethod
    def _rank_operation_eligible(tool: BaseTool, inputs: dict[str, object]) -> bool:
        """Honor a provider's explicit operation denial during rank preflight."""
        operation = str(inputs.get("operation", "text_to_video"))
        supports = getattr(tool, "supports", {})
        return (
            supports.get(operation) is not False
            and VideoSelector._operation_ready(tool, operation)
            and (not VideoSelector._requires_final_frame(inputs) or VideoSelector._final_frame_eligible(tool, inputs))
        )

    @staticmethod
    def _operation_ready(tool: BaseTool, operation: str) -> bool:
        checker = getattr(tool, "is_operation_available", None)
        if not callable(checker):
            return True
        return bool(checker(operation))

    @staticmethod
    def _has_custom_workflow(inputs: dict[str, object]) -> bool:
        return bool(inputs.get("workflow_json") or inputs.get("workflow_path"))

    def _custom_workflow_eligible(
        self, tool: BaseTool, inputs: dict[str, object]
    ) -> bool:
        """Whether a tool can run the caller-supplied custom workflow.

        Eligibility is based on server availability, not bundled-model readiness:
        a provider qualifies when it advertises ``custom_workflow`` support, an
        ``output_node`` is supplied, and its backend is reachable (status is not
        UNAVAILABLE).
        """
        if not self._has_custom_workflow(inputs):
            return False
        if not inputs.get("output_node"):
            return False
        supports = getattr(tool, "supports", {})
        if not supports.get("custom_workflow"):
            return False
        return tool.get_status() != ToolStatus.UNAVAILABLE

    @staticmethod
    def _is_mcp_qualification(inputs, tool):
        """Permit explicit candidate planning only in its bounded qualification pipeline."""
        if (getattr(tool, 'provider', None) != 'openart_mcp'
                or inputs.get('preferred_provider') != 'openart_mcp'
                or inputs.get('allowed_providers') != ['openart_mcp']):
            return False
        try:
            from lib.production_execution import discover_project, _read
            root = discover_project(inputs)
            marker = _read(root / 'project.json') if root else {}
            return marker.get('pipeline_type') == 'provider-qualification' and marker.get('governance', {}).get('mode') == 'strict'
        except (ValueError, OSError, TypeError):
            return False

    def _tool_selectable(self, tool: BaseTool, inputs: dict[str, object]) -> bool:
        """A provider is selectable if it is AVAILABLE, or if it can serve a
        caller-supplied custom workflow even while bundled models report DEGRADED."""
        if tool.get_status() == ToolStatus.AVAILABLE or self._is_mcp_qualification(inputs, tool):
            return True
        return self._custom_workflow_eligible(tool, inputs)
