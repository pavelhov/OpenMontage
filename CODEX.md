# OpenMontage - Codex Agent Instructions

> **Start here:** See [`AGENT_GUIDE.md`](AGENT_GUIDE.md) — the complete operating guide and agent contract.
> **Project context:** See [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md) for architecture, key files, and conventions.

## Optional Codex Plugin

Opening this repository directly requires no plugin. To make OpenMontage discoverable from other Codex workspaces, install the plugin bundled with this repository:

```bash
codex plugin marketplace add /path/to/OpenMontage
codex plugin add openmontage@openmontage
```

Start a new Codex task after installation and invoke `$openmontage`. The isolated package under `.codex/plugin/` is a routing layer only; this repository remains the authoritative engine and Backlot remains its canonical review surface. Cross-workspace runs require the checkout's `.venv311/bin/python` (Windows: `.venv311\Scripts\python.exe`) at Python 3.11 or newer; if it is missing or incompatible, run `make setup`. Never fall back to `.venv` or silently install packages.
