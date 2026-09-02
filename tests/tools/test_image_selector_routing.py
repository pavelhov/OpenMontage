"""Offline routing tests for explicit-selection-only image providers."""

from __future__ import annotations

from typing import Any

import pytest

from tools.base_tool import ToolResult, ToolStatus
from tools.graphics.image_selector import ImageSelector


class _StubImageTool:
    capability = "image_generation"

    def __init__(
        self,
        name: str,
        provider: str,
        *,
        explicit_only: bool = False,
        cost: float = 0.10,
        runtime: float = 10.0,
        execute_success: bool = True,
        estimate_error: str | None = None,
        status: ToolStatus = ToolStatus.AVAILABLE,
    ) -> None:
        self.name = name
        self.provider = provider
        self.best_for = [name]
        self.quality_score = None
        self.supports = {
            "text_to_image": True,
            "explicit_selection_only": explicit_only,
        }
        self.input_schema = {"properties": {"prompt": {}}}
        self._cost = cost
        self._runtime = runtime
        self._execute_success = execute_success
        self._estimate_error = estimate_error
        self._status = status
        self.execute_calls = 0
        self.estimate_calls = 0

    def get_status(self) -> ToolStatus:
        return self._status

    def get_info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "agent_skills": [],
            "best_for": self.best_for,
            "supports": self.supports,
            "quality_score": self.quality_score,
        }

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        self.estimate_calls += 1
        if self._estimate_error:
            raise ValueError(self._estimate_error)
        return self._cost

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return self._runtime

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        self.execute_calls += 1
        return ToolResult(
            success=self._execute_success,
            data={},
            error=None if self._execute_success else "provider failed",
        )


class _Score:
    def __init__(self, tool_name: str, provider: str, weighted: float) -> None:
        self.tool_name = tool_name
        self.provider = provider
        self.weighted_score = weighted

    def explain(self) -> str:
        return self.tool_name

    def to_dict(self) -> dict[str, Any]:
        return {"tool_name": self.tool_name, "provider": self.provider}


@pytest.fixture()
def rankings(monkeypatch):
    table: list[_Score] = []
    monkeypatch.setattr("lib.scoring.rank_providers", lambda candidates, context: list(table))
    return table


@pytest.mark.parametrize(
    "inputs",
    [
        {"prompt": "x"},
        {"prompt": "x", "preferred_provider": "private"},
        {"prompt": "x", "allowed_providers": ["private"]},
        {
            "prompt": "x",
            "preferred_provider": "private",
            "allowed_providers": ["private", "public"],
        },
    ],
)
def test_explicit_only_image_provider_requires_exact_singleton_pin(inputs):
    public = _StubImageTool("public_image", "public")
    private = _StubImageTool("private_image", "private", explicit_only=True)

    assert private not in ImageSelector()._filter_candidates(inputs, [public, private])


def test_rank_mode_excludes_explicit_only_image_even_with_exact_pin(monkeypatch):
    public = _StubImageTool("public_image", "public")
    private = _StubImageTool("private_image", "private", explicit_only=True)
    seen: list[_StubImageTool] = []

    def fake_rank(candidates, task_context):  # noqa: ANN001
        seen.extend(candidates)
        return []

    monkeypatch.setattr("lib.scoring.rank_providers", fake_rank)
    selector = ImageSelector()
    selector._providers = lambda: [public, private]  # type: ignore[assignment]
    result = selector.execute({
        "prompt": "x",
        "operation": "rank",
        "preferred_provider": "private",
        "allowed_providers": ["private"],
    })

    assert result.success is True
    assert private not in seen


def test_exact_explicit_image_pin_bypasses_scoring_and_has_no_alternatives(monkeypatch):
    private = _StubImageTool("private_image", "private", explicit_only=True)
    monkeypatch.setattr(
        "lib.scoring.rank_providers",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not score")),
    )
    selector = ImageSelector()
    selector._providers = lambda: [private]  # type: ignore[assignment]

    result = selector.execute({
        "prompt": "x",
        "preferred_provider": "private",
        "allowed_providers": [" private ", "private"],
    })

    assert result.success is True
    assert private.execute_calls == 1
    assert result.data["alternatives_considered"] == []
    assert result.data["fallback_tools"] == []


def test_automatic_image_estimate_skips_explicit_unknown_estimator(rankings):
    public = _StubImageTool("public_image", "public", cost=0.25)
    private = _StubImageTool(
        "private_image", "private", explicit_only=True,
        estimate_error="unknown media cost",
    )
    rankings.append(_Score("public_image", "public", 0.80))
    selector = ImageSelector()
    selector._providers = lambda: [public, private]  # type: ignore[assignment]

    assert selector.estimate_cost({"prompt": "x"}) == pytest.approx(0.25)
    assert private.estimate_calls == 0


def test_exact_explicit_image_estimate_propagates_unknown_cost(monkeypatch):
    private = _StubImageTool(
        "private_image", "private", explicit_only=True,
        estimate_error="unknown media cost",
    )
    monkeypatch.setattr(
        "lib.scoring.rank_providers",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not score")),
    )
    selector = ImageSelector()
    selector._providers = lambda: [private]  # type: ignore[assignment]

    with pytest.raises(ValueError, match="unknown media cost"):
        selector.estimate_cost({
            "prompt": "x",
            "preferred_provider": "private",
            "allowed_providers": ["private"],
        })


def test_image_discovery_surfaces_and_fallbacks_omit_explicit_only_provider():
    public = _StubImageTool("public_image", "public")
    private = _StubImageTool("private_image", "private", explicit_only=True)
    selector = ImageSelector()
    selector._providers = lambda: [public, private]  # type: ignore[assignment]

    assert selector.get_status() == ToolStatus.AVAILABLE
    assert "private" not in selector.provider_matrix
    assert "private_image" not in selector.fallback_tools
    assert "private_image" not in selector.fallback_tools_for({"prompt": "x"})
    assert selector.fallback_tools_for({
        "prompt": "x",
        "preferred_provider": "private",
        "allowed_providers": ["private"],
    }) == []

    selector._providers = lambda: [private]  # type: ignore[assignment]
    assert selector.get_status() == ToolStatus.UNAVAILABLE


def test_exact_explicit_image_runtime_estimate_bypasses_scoring(monkeypatch):
    private = _StubImageTool("private_image", "private", explicit_only=True, runtime=77.0)
    monkeypatch.setattr(
        "lib.scoring.rank_providers",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not score")),
    )
    selector = ImageSelector()
    selector._providers = lambda: [private]  # type: ignore[assignment]

    assert selector.estimate_runtime({
        "prompt": "x",
        "preferred_provider": "private",
        "allowed_providers": ["private"],
    }) == pytest.approx(77.0)


def test_explicit_image_failure_is_terminal_and_reports_no_fallbacks(monkeypatch):
    private = _StubImageTool("private_image", "private", explicit_only=True, execute_success=False)
    public = _StubImageTool("public_image", "public")
    monkeypatch.setattr(
        "lib.scoring.rank_providers",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not score")),
    )
    selector = ImageSelector()
    selector._providers = lambda: [private, public]  # type: ignore[assignment]

    result = selector.execute({
        "prompt": "x",
        "preferred_provider": "private",
        "allowed_providers": ["private"],
    })

    assert result.success is False
    assert private.execute_calls == 1
    assert public.execute_calls == 0
    assert result.data["alternatives_considered"] == []
    assert result.data["fallback_tools"] == []
