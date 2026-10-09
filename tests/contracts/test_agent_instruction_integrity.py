from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_music_plans_discover_all_music_capabilities() -> None:
    from lib.pipeline_loader import get_stage_skill, load_pipeline

    guide = _read("AGENT_GUIDE.md")
    assert "current stage" in guide
    assert "Music planning applies when the current brief or selected pipeline" in guide
    assert "proposal/asset directors own" in guide
    assert "Preserve an explicit no-added-music choice" in guide
    instruction_files = [
        "skills/pipelines/cinematic/idea-director.md",
        "skills/pipelines/cinematic/proposal-director.md",
        "skills/pipelines/documentary-montage/idea-director.md",
        "skills/pipelines/explainer/proposal-director.md",
    ]
    # Follow the current manifest to its real music owner instead of requiring
    # a duplicate source-discovery procedure in every-task boot instructions.
    owner = "skills/" + get_stage_skill(load_pipeline("cinematic"), "proposal") + ".md"
    assert owner in instruction_files
    assert "Music Plan (when unresolved)" in _read(owner)

    for relative_path in instruction_files:
        text = _read(relative_path)
        for capability in ("music_library", "music_search", "music_generation"):
            assert f'get_by_capability("{capability}")' in text, (
                f"{relative_path} omits the {capability!r} music source"
            )


def test_explainer_directors_do_not_reference_fictitious_submit_functions() -> None:
    for stage in ("idea", "script", "scene"):
        text = _read(f"skills/pipelines/explainer/{stage}-director.md")
        assert "handle_explainer_" not in text
