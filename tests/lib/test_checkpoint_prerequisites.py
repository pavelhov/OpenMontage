import json

import pytest
from tests.contracts.test_phase0_contracts import sample_artifact

from lib.checkpoint import (
    CheckpointValidationError,
    init_project,
    write_checkpoint,
)


def _script_artifact() -> dict:
    return {
        "version": "1.0",
        "title": "Smoke",
        "total_duration_seconds": 1,
        "sections": [
            {
                "id": "s1",
                "text": "One second.",
                "start_seconds": 0,
                "end_seconds": 1,
            }
        ],
    }


def test_later_stage_cannot_skip_a_missing_predecessor(tmp_path) -> None:
    init_project(
        "run",
        title="Run",
        pipeline_type="framework-smoke",
        pipeline_dir=tmp_path,
    )

    with pytest.raises(CheckpointValidationError, match="PREREQUISITE VIOLATION"):
        write_checkpoint(
            tmp_path,
            "run",
            "script",
            "completed",
            {"script": _script_artifact()},
            pipeline_type="framework-smoke",
            human_approved=True,
        )


def test_later_stage_rejects_unapproved_gated_predecessor(tmp_path) -> None:
    project_dir = init_project(
        "run",
        title="Run",
        pipeline_type="framework-smoke",
        pipeline_dir=tmp_path,
    )
    predecessor_path = write_checkpoint(
        tmp_path,
        "run",
        "research",
        "awaiting_human",
        {"research_brief": sample_artifact("research_brief")},
        pipeline_type="framework-smoke",
    )
    predecessor = json.loads(predecessor_path.read_text(encoding="utf-8"))
    predecessor["status"] = "completed"
    predecessor["human_approved"] = False
    predecessor_path.write_text(json.dumps(predecessor), encoding="utf-8")

    with pytest.raises(CheckpointValidationError, match="completed without required approval"):
        write_checkpoint(
            tmp_path,
            "run",
            "script",
            "completed",
            {"script": _script_artifact()},
            pipeline_type="framework-smoke",
            human_approved=True,
        )


def test_malformed_predecessor_cannot_forge_completion(tmp_path) -> None:
    project_dir = init_project(
        "run",
        title="Run",
        pipeline_type="framework-smoke",
        pipeline_dir=tmp_path,
    )
    (project_dir / "checkpoint_research.json").write_text(
        json.dumps({"status": "completed", "human_approved": True}),
        encoding="utf-8",
    )

    with pytest.raises(CheckpointValidationError, match="incomplete or missing"):
        write_checkpoint(
            tmp_path,
            "run",
            "script",
            "completed",
            {"script": _script_artifact()},
            pipeline_type="framework-smoke",
            human_approved=True,
        )


def test_in_progress_heartbeat_is_not_blocked_by_prerequisites(tmp_path) -> None:
    init_project(
        "run",
        title="Run",
        pipeline_type="framework-smoke",
        pipeline_dir=tmp_path,
    )

    path = write_checkpoint(
        tmp_path,
        "run",
        "script",
        "in_progress",
        {},
        pipeline_type="framework-smoke",
    )

    assert path.exists()


def test_unknown_style_playbook_fails_before_project_creation(tmp_path) -> None:
    with pytest.raises(CheckpointValidationError, match="style_playbook"):
        init_project(
            "run",
            title="Run",
            pipeline_type="framework-smoke",
            pipeline_dir=tmp_path,
            style_playbook="does-not-exist",
        )

    assert not (tmp_path / "run").exists()


def test_marker_derived_unknown_playbook_blocks_later_writes(tmp_path) -> None:
    project_dir = tmp_path / "run"
    project_dir.mkdir()
    (project_dir / "project.json").write_text(
        json.dumps({
            "version": "1.0",
            "project_id": "run",
            "pipeline_type": "framework-smoke",
            "style_playbook": "does-not-exist",
        }),
        encoding="utf-8",
    )

    with pytest.raises(CheckpointValidationError, match="style_playbook"):
        write_checkpoint(tmp_path, "run", "research", "in_progress", {})


def _supplied_basis(project_dir, script=None):
    import hashlib
    path = project_dir / "supplied" / "script.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(script or _script_artifact()), encoding="utf-8")
    return {"kind": "supplied_script_scenes", "artifacts": [
        {"path": "supplied/script.json", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]}


def test_supplied_basis_completes_research_without_fabricated_brief(tmp_path) -> None:
    project_dir = init_project("run", title="Run", pipeline_type="framework-smoke", pipeline_dir=tmp_path)
    basis = _supplied_basis(project_dir)
    path = write_checkpoint(tmp_path, "run", "research", "completed", {},
        pipeline_type="framework-smoke", human_approved=True, metadata={"supplied_basis": basis})
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "research_brief" not in saved["artifacts"]
    assert saved["metadata"]["supplied_basis"] == basis
    write_checkpoint(tmp_path, "run", "script", "completed", {"script": _script_artifact()},
        pipeline_type="framework-smoke", human_approved=True)


def test_supplied_basis_must_be_current_and_valid(tmp_path) -> None:
    project_dir = init_project("run", title="Run", pipeline_type="framework-smoke", pipeline_dir=tmp_path)
    with pytest.raises(CheckpointValidationError, match="research_brief"):
        write_checkpoint(tmp_path, "run", "research", "completed", {},
            pipeline_type="framework-smoke", human_approved=True)
    bad = _supplied_basis(project_dir, {"version": "1.0"})
    with pytest.raises(CheckpointValidationError, match="supplied basis"):
        write_checkpoint(tmp_path, "run", "research", "completed", {},
            pipeline_type="framework-smoke", human_approved=True, metadata={"supplied_basis": bad})
    for mutate in (lambda b: b["artifacts"][0].update(sha256="0" * 64),
                   lambda b: b["artifacts"][0].update(path="../outside.json"),
                   lambda b: b.update(kind="invented_research"),
                   lambda b: b.update(kind=["supplied_script_scenes"]),
                   lambda b: b.update(kind={"name": "chosen_premise"}),
                   lambda b: b.update(artifacts=[])):
        basis = _supplied_basis(project_dir)
        mutate(basis)
        with pytest.raises(CheckpointValidationError, match="supplied basis"):
            write_checkpoint(tmp_path, "run", "research", "completed", {},
                pipeline_type="framework-smoke", human_approved=True, metadata={"supplied_basis": basis})
    basis = _supplied_basis(project_dir)
    write_checkpoint(tmp_path, "run", "research", "completed", {},
        pipeline_type="framework-smoke", human_approved=True, metadata={"supplied_basis": basis})
    (project_dir / "supplied" / "script.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CheckpointValidationError, match="PREREQUISITE VIOLATION"):
        write_checkpoint(tmp_path, "run", "script", "completed", {"script": _script_artifact()},
            pipeline_type="framework-smoke", human_approved=True)
