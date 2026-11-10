"""Checkpoint-local references resolve and retain their nested constraints."""

from urllib.parse import urlsplit

import jsonschema
import pytest

from lib.checkpoint import (
    CheckpointValidationError,
    _load_checkpoint_schema,
    validate_checkpoint,
)


def test_checkpoint_schema_has_absolute_id_and_valid_draft_2020_12_structure():
    schema = _load_checkpoint_schema()

    assert urlsplit(schema["$id"]).scheme
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    jsonschema.Draft202012Validator.check_schema(schema)


@pytest.fixture
def checkpoint():
    return {
        "version": "1.0",
        "project_id": "schema-reference-regression",
        "pipeline_type": "framework-smoke",
        "stage": "script",
        "status": "in_progress",
        "timestamp": "2026-01-01T00:00:00Z",
        "artifacts": {},
        "review": {
            "preauth": {
                "artifact_path": "artifacts/script.json",
                "artifact_sha256": "a" * 64,
                "review_path": "artifacts/script_review.json",
                "review_sha256": "b" * 64,
                "critical_findings": [],
            },
        },
        "metadata": {
            "approval_basis": {
                "kind": "policy",
                "decision_id": "approved-policy",
                "policy_sha256": "c" * 64,
            },
        },
    }


@pytest.mark.parametrize("references", ["preauth", "approval_basis", "both"])
def test_checkpoint_resolves_local_references(checkpoint, references):
    if references == "preauth":
        del checkpoint["metadata"]
    elif references == "approval_basis":
        del checkpoint["review"]

    validate_checkpoint(checkpoint)


@pytest.mark.parametrize(
    "container, reference, field, value",
    [
        ("review", "preauth", "artifact_path", ""),
        ("review", "preauth", "artifact_sha256", "invalid"),
        ("review", "preauth", "review_path", 1),
        ("review", "preauth", "review_sha256", "invalid"),
        ("review", "preauth", "critical_findings", ["critical defect"]),
        ("review", "preauth", "unexpected", True),
        ("metadata", "approval_basis", "kind", "manual"),
        ("metadata", "approval_basis", "decision_id", ""),
        ("metadata", "approval_basis", "policy_sha256", "invalid"),
        ("metadata", "approval_basis", "unexpected", True),
    ],
)
def test_checkpoint_rejects_malformed_local_reference(
    checkpoint, container, reference, field, value,
):
    checkpoint[container][reference][field] = value

    with pytest.raises(CheckpointValidationError, match="Checkpoint failed schema validation"):
        validate_checkpoint(checkpoint)


@pytest.mark.parametrize(
    "container, reference, required_field",
    [
        ("review", "preauth", "review_sha256"),
        ("metadata", "approval_basis", "decision_id"),
    ],
)
def test_checkpoint_requires_local_reference_fields(
    checkpoint, container, reference, required_field,
):
    del checkpoint[container][reference][required_field]

    with pytest.raises(CheckpointValidationError, match="Checkpoint failed schema validation"):
        validate_checkpoint(checkpoint)
