---
title: System Grok CLI compatibility and current Imagine support
date: 2026-09-10
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# System Grok CLI compatibility and current Imagine support

## Goal Capsule

Objective: Users can update their installed Grok CLI and keep using OpenMontage without maintaining a separate old installation.
Means: Minimum-version and required-option checks, with strict result validation (KTD1).
Authority: User-approved decisions below, then repository contracts. Stop on evidence that a requirement cannot work; routine implementation choices remain local.
Execution: Implement and verify locally; the outer workflow owns review and shipping directly to the user's fork's development branch.

---

## Product Contract

### Summary

Use the installed Grok CLI for subscription media, and ship the current task's REST Imagine additions and timeout diagnostics together.

### Problem Frame

An exact 1.0.18 requirement rejects newer installations. A temporary second binary and local overrides preserve availability but make updates confusing and machine-specific.

### Key Decisions

- session-settled: One system CLI (user-directed). Reject a separate old adapter binary because users should maintain one installation. Governs R1, R5.
- session-settled: Minimum version plus interface checks (user-approved). Reject both an exact pin and unchecked version acceptance because compatible updates should work while missing requirements remain visible. Governs R2, R3.
- session-settled: Direct push to the fork's development branch (user-directed). Reject the default open-PR delivery because the user explicitly selected the destination. Governs R6.

### Requirements

R1. Resolve `grok` from PATH by default; preserve an optional explicit executable override for installations that need it.
R2. Accept numeric release versions at least 1.0.18, including future minor/major releases, when required options and enum values are advertised. Reject unparseable/older versions before media dispatch. Treat SemVer prereleases below the minimum release as too old.
R3. Check every option used by the adapter, including permission restrictions and streaming JSON, without generation. Preserve existing exact tool/argument validation, artifact containment, completion validation, timeout behavior, and no automatic retry.
R4. Report the observed version on success and failure when known; report unknown honestly if discovery fails. Retain the earlier Image 2.0, Video 1.5 first/last-frame, selector, and timeout-diagnostic changes.
R5. Remove only the temporary old-binary overrides and binary created in this task after compatibility checks pass and the local production checkout has compatible adapter code. Preserve other local changes and settings.
R6. Commit all task changes, integrate the current remote development history, and push normally to `origin/development` on `pavelhov/OpenMontage`; never force push or push upstream.

### Acceptance Examples

- Installed 1.0.25 with required options is eligible; metadata says 1.0.25.
- A newer CLI missing `--deny` fails before generation and names the missing option.
- A version/help timeout reports not dispatched; a generation timeout remains indeterminate and is not retried.
- A CLI upgrade alone does not advertise unsupported Imagine model selection or last-frame inputs.

---

## Planning Contract

KTD1. Replace the equality check in `tools/_grok_cli_media.py` with numeric minimum-version parsing and bounded `--help` inspection. Inspect declared options rather than substring matches in prose; check enum values under their owning option. Reuse the check for readiness and dispatch. A help probe proves advertised interface presence, not server entitlement or unchanged runtime semantics; result checks remain necessary.

KTD2. Pass observed version per invocation, avoiding global mutable metadata. Discovery errors carry observed-version context when available. Resolve a single executable path per dispatch so version/help/media checks use the same selected installation.

KTD3. Keep existing CLI media restrictions and REST provider separation. Update public descriptions and tests that assert exact pinning. No paid media calls are needed for this compatibility-policy change.

KTD4. Integrate in an isolated branch. Only fast-forward the local production checkout if clean, then remove the temporary overrides. The direct development push supersedes the shipping skill's default PR creation.

Sources: `tools/_grok_cli_media.py`, `tests/tools/test_grok_cli_media.py`, `tests/tools/test_grok_cli_diagnostics.py`, `docs/GROK_IMAGINE_CAPABILITIES_2026-09-10.md`; installed Grok 1.0.25 help advertises the current adapter flags.

---

## Implementation Units

### U1. System CLI compatibility

Requirements: R1–R3. Files: `tools/_grok_cli_media.py`, `tools/graphics/grok_cli_image.py`, `tools/video/grok_cli_video.py`, `tests/tools/test_grok_cli_media.py`, `tests/tools/test_grok_cli_diagnostics.py`.
Approach: Add compatibility discovery and pass actual version through results. Keep checks bounded and read-only.
Tests: minimum/newer/prerelease/malformed versions; absent options and values; help failure/timeout; no media dispatch on incompatibility; actual version in success/failure; existing result-validation suite.

### U2. Documentation and full task verification

Requirements: R4. Depends on U1. Files: existing task-modified Grok provider/selector files, tests, `docs/GROK_CLI_COMPATIBILITY.md`, `docs/GROK_IMAGINE_CAPABILITIES_2026-09-10.md`, `skills/creative/prompting/grok-prompting.md`, `.agents/skills/grok-media/SKILL.md`, `tests/contracts/test_grok_cli_compatibility.py`.
Approach: Preserve and review the earlier REST and diagnostic changes; replace current exact-version claims with minimum-version/interface requirements while clearly labeling historical audits.
Tests: Grok image/video/diagnostics and both selector regression suites; read-only installed-CLI compatibility smoke check.

### U3. Integrate and remove temporary installation

Requirements: R5–R6. Depends on U2. Files: only task-owned local override entries plus the temporary backup outside version control.
Approach: Review, commit, integrate remote development, push without rewriting history; fast-forward clean production checkout to the delivered commit and remove temporary overrides and backup.
Verification: remote development commit equals delivered commit; production and worktree checks resolve the system CLI; ignored secrets/config are absent from the commit.

---

## Verification Contract

Run the Grok CLI, diagnostics, Image 2.0, video frames, video quality, video selector, image selector, and CLI compatibility contract pytest files under `tests/tools/` and `tests/contracts/`. Run `git diff --check`. Run compatibility discovery against the installed system CLI without media generation. Browser tests do not apply because no UI behavior changes.

---

## Definition of Done

U1 passes compatibility-policy and existing protocol tests. U2 preserves all earlier task behavior and documents the new policy. U3 leaves one installed Grok CLI and delivers all task changes to the fork's development branch. No abandoned implementation, separate old binary, or task-created override remains. Report verification limits: no live media generation or entitlement check.
