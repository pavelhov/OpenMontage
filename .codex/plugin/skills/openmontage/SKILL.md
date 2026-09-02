---
name: openmontage
description: Use OpenMontage to plan, generate, edit, resume, or review a governed video production from any Codex workspace. Apply when the user explicitly names OpenMontage or requests its pipeline for a video deliverable; do not route ordinary social strategy or copywriting here unless it includes video production.
---

# OpenMontage

Use the live OpenMontage checkout as the production engine. This plugin is an entry point; it must not duplicate or replace the repository's pipeline, tool, provider, checkpoint, or Backlot contracts.

Resolve the engine root before acting:

1. Prefer the current workspace or an ancestor when it contains `AGENT_GUIDE.md`, `pipeline_defs/`, and `tools/tool_registry.py`.
2. Otherwise, use `OPENMONTAGE_ROOT` when it points to a directory with those files.
3. Otherwise, inspect the enabled `openmontage` entry from `codex plugin list` and use its marketplace source checkout when it is valid.
4. If no live checkout can be resolved, ask the user for its location. Do not run a production inside an opaque plugin-cache copy.

Before doing OpenMontage work, read the resolved checkout's `AGENT_GUIDE.md` completely and follow every manifest, director, meta skill, and provider skill it routes the current request to.

Run OpenMontage Python commands with `<engine-root>/.venv/bin/python` when that executable exists. This keeps cross-workspace tasks on the checkout's configured dependencies instead of the caller's unrelated Python environment. If the checkout virtual environment is absent, follow the repository setup documentation or report the missing dependency; do not silently install packages.

For video production:

- Run the repository's capability preflight before creative work.
- Declare and follow a pipeline, stage directors, checkpoints, and approval gates.
- Write production artifacts only to the project workspace required by the guide.
- Open Backlot as the canonical review surface. It observes project files and is not a second source of production state.
- Preserve explicit provider selection and never silently fall back.
- Treat `grok_cli` and the xAI API as distinct providers.
- Obtain the approvals required by OpenMontage before paid or consequential generation.
- Surface auth, spending-limit, headless, provider-capability, and runtime failures directly.

Do not modify OpenMontage source merely to complete a production. When the user explicitly requests engine development or a defect blocks the approved run, read `AGENTS.md`, preserve unrelated work, and obey the checkout's branch and test requirements.

If the Creative Production board is directly available and the user requests it, it may supplement image review. Backlot remains authoritative. Do not invoke the Creative Production board indirectly through a generic executor merely to simulate its mounted UI.
