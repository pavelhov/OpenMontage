"""Proposal estimates disclose unknown billing without inventing a USD total."""

from copy import deepcopy

import jsonschema
import pytest

from lib.checkpoint import write_checkpoint
from schemas.artifacts import validate_artifact
from tests.contracts.test_phase0_contracts import sample_artifact


def unknown_proposal(*, mixed=False):
    proposal = deepcopy(sample_artifact("proposal_packet"))
    proposal["cost_estimate"] = {
        "total_estimated_usd": None,
        "unknown_cost_notes": "Subscription USD cost and quota deduction are unknown.",
        "budget_verdict": "unknown_cost",
        "line_items": [{
            "tool": "subscription_video",
            "operation": "Approved first-pass clips",
            "quantity": 8,
            "estimated_usd": None,
            "notes": "Existing subscription; USD cost and remaining quota unknown.",
        }],
    }
    if mixed:
        proposal["cost_estimate"]["line_items"].append({
            "tool": "local_stitch", "operation": "Assembly", "estimated_usd": 0,
        })
    return proposal


def test_legacy_numeric_proposal_remains_valid():
    validate_artifact("proposal_packet", sample_artifact("proposal_packet"))


@pytest.mark.parametrize("mixed", [False, True])
def test_explicit_unknown_estimates_are_valid(mixed):
    validate_artifact("proposal_packet", unknown_proposal(mixed=mixed))


@pytest.mark.parametrize("field", ["total_estimated_usd", "estimated_usd"])
def test_negative_estimates_are_rejected(field):
    proposal = deepcopy(sample_artifact("proposal_packet"))
    cost = proposal["cost_estimate"]
    if field == "estimated_usd":
        cost["line_items"][0][field] = -1
    else:
        cost[field] = -1
    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("proposal_packet", proposal)


@pytest.mark.parametrize("location", ["total", "line"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_unknown_estimates_require_explanation(location, value):
    proposal = unknown_proposal()
    target = proposal["cost_estimate"] if location == "total" else proposal["cost_estimate"]["line_items"][0]
    key = "unknown_cost_notes" if location == "total" else "notes"
    if value is None:
        del target[key]
    else:
        target[key] = value
    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("proposal_packet", proposal)


def test_known_total_cannot_hide_unknown_line():
    proposal = unknown_proposal(mixed=True)
    proposal["cost_estimate"]["total_estimated_usd"] = 0
    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("proposal_packet", proposal)


@pytest.mark.parametrize("verdict", ["within_budget", "near_limit", "over_budget", "no_budget_set"])
def test_unknown_total_cannot_claim_budget_verdict(verdict):
    proposal = unknown_proposal()
    proposal["cost_estimate"]["budget_verdict"] = verdict
    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("proposal_packet", proposal)


def test_unknown_proposal_passes_normal_checkpoint_validation(tmp_path):
    proposal = unknown_proposal(mixed=True)
    proposal["production_plan"]["pipeline"] = "cinematic"
    path = write_checkpoint(tmp_path, "unknown-cost", "proposal", "awaiting_human", {"proposal_packet": proposal})
    assert path.exists()
