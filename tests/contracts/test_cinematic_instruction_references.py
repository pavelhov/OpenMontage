from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
IDEA_DIRECTOR = REPO_ROOT / "skills/pipelines/cinematic/idea-director.md"


def test_cinematic_idea_director_reference_inputs_resolve() -> None:
    """Keep the cinematic idea director's declared local inputs actionable."""
    reference_section = IDEA_DIRECTOR.read_text(encoding="utf-8").split(
        "## Reference Inputs\n", maxsplit=1
    )[1].split("\n## ", maxsplit=1)[0]
    reference_inputs = tuple(
        line.removeprefix("- `").removesuffix("`")
        for line in reference_section.splitlines()
        if line.startswith("- `") and line.endswith("`")
    )

    assert reference_inputs
    for reference in reference_inputs:
        assert (REPO_ROOT / reference).is_file(), reference
