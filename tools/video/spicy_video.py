"""Exact Spicy native video route; paid dispatch requires canonical factory authority."""
from __future__ import annotations

import json
from pathlib import Path
from tools.base_tool import (BaseTool, Determinism, ExecutionMode, ResourceProfile,
                             ResumeSupport, RetryPolicy, ToolRuntime, ToolStability, ToolTier)


class SpicyVideo(BaseTool):
    name = "spicy_video"
    version = "0.2.0"
    provider = "spicy"
    capability = "video_generation"
    tier = ToolTier.GENERATE
    runtime = ToolRuntime.API
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.ASYNC
    determinism = Determinism.STOCHASTIC
    # Credentials are selected and checked against retained account authority at
    # preflight. Discovery must not assume the default environment variable.
    install_instructions = ("Requires Python 3.11+ and the official spicyapi==0.3.0 SDK in the engine "
                            "runtime (make setup builds .venv311 from requirements.txt). Retain exact "
                            "factory account, source-transfer and quoted USD authority; installation "
                            "alone never enables generation.")
    dependencies = ["python:spicyapi"]
    agent_skills = ["spicy-api"]
    capabilities = ["image_to_video", "first_last_frame", "text_to_video", "reference_to_video",
                    "video_edit", "video_extend", "upscale", "character_animation",
                    "talking_avatar", "video_translate", "lip_sync"]
    supports = {"image_to_video": True, "first_last_frame": True,
                "native_audio": True, "explicit_selection_only": True,
                "reference_image": True, "multiple_reference_images": True,
                "text_to_video": True, "dynamic_account_schema_required": True,
                "timed_keyframes": False}
    first_last_frame_models = frozenset({"bytedance/seedance-2.0-fast/image-to-video"})
    fallback_tools = []
    retry_policy = RetryPolicy(max_retries=0)
    resource_profile = ResourceProfile(network_required=True)
    resume_support = ResumeSupport.FROM_CHECKPOINT
    side_effects = ["external_paid_generation", "local_output_write"]
    best_for = ["explicit exact native video tasks qualified by the current account schema"]
    not_good_for = ["unapproved generation", "unregistered external sources", "automatic route fallback",
                    "source-only transforms without authored prompt/timing factory contract"]
    input_schema = json.loads((Path(__file__).resolve().parents[2] /
                               "schemas/tools/spicy_video.schema.json").read_text())

    def get_info(self):
        return {**super().get_info(), "dispatch_authority": "required_exact_active_factory_policy",
                "default_generation_enabled": False, "production_available": False,
                "cost_status": "exact_usd_quote_required",
                "account_readiness": "checked_from_supplied_inputs_at_preflight"}

    def estimate_cost(self, inputs):
        from tools.provider_pricing import PriceQuoteRequired
        raise PriceQuoteRequired("Spicy requires an account-bound exact native USD quote")

    def prepare_offline(self, inputs, checked=None):
        from lib.spicy_jobs import build_native_request
        return {"native_request": build_native_request(inputs), "network_attempted": False,
                "authority": "none"}

    def execute(self, inputs):
        # BaseTool wraps this method with the canonical locked counted gateway.
        # The provider repeats authority validation immediately before transport.
        from lib.spicy_jobs import submit_original
        return submit_original(inputs)
