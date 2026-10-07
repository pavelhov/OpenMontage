# OpenArt Unknown-Cost Qualification Rehearsal

**Date:** 2026-10-07

**Scope:** Local offline implementation and rehearsal evidence only. This note records the bounded U4 outcome for [the approved plan](../plans/2026-10-07-feat-openart-unknown-cost-plan.md).

## Outcome

The repository now supports preparing and rehearsing a narrowly scoped provider-qualification sample through the canonical registered CLI path, while keeping unknown cost behind explicit human approval and durable dispatch controls. This is a beta/custom provider-qualification path, added inside U4 to make the planned exact sample reviewable. It does not expand spending authority.

The existing cinematic pipeline would require research, concepts, composition and music beyond this bounded transport test. Framework-smoke has no footage generation. The minimal qualification pipeline supplies the canonical gated path needed by U4. Its offline rehearsal demonstrates the execution and evidence lifecycle with synthetic transport replies.

No live generation, uploads, purchases, top-ups, paid API fallback, website generation, publishing, push or PR occurred. Current unknown-cost Auto-continue remains deferred. Grok quota remains unknown and unchanged.

## Sample packet and preparation

Proposed sample: PixVerse V6 text-to-video, one original, 1 second, 16:9, 720p, with no references. The brief is a short drop of one red cube onto a gray table, static camera, with no people, text or dialogue. The prepared packet is at `projects/openart-720p-qualification/artifacts/provider_qualification_packet.json` in the local workspace; it is not an active scope, approval, unknown-cost sidecar, production attempt or video output.

The packet SHA-256 is `34694b6b02bfd5f599248246a84593e5994c635cbb9300cb3bb07b98b83b355c`; its request SHA-256 is `2d08aa9b5f46a7f076f3ab0637c90fdde174514e719d8352fa0d64d0634c82a8`. Native/profile/root preparation bindings were independently reverified without provider calls. The preparation checkpoint remains `in_progress` until root's final state is recorded; the intended user-facing boundary is `awaiting_human`. No live authority/result contract exists.

## Cost evidence and limits

Six fresh registered read-only observations were recorded from `2026-10-07T14:31:01.426897Z` through `2026-10-07T14:31:07.549203Z`. The observed Free balance was 40 credits. The available price evidence was 50 credits for a 5-second, 540p request, which does not match this sample's 1-second, 720p request. It cannot establish this request's price, cap, affordability or current entitlement.

The requested charge and Free-plan entitlement remain unknown. Evidence ID: `eee31c029149aa6cbecc8f2d490e21b3e175f5c158bd2197004e6c548d5307a6`. The observed balance is an observation, not a budget ceiling or approval. Do not infer zero cost or convert it into USD.

## Rehearsal evidence

The actual proposed sample used the registered read-only preparation path. Separately, the offline integration exercised registry, adapter, compiler, human gate, native preview, SQLite ledger, journal, process launch, original-job recovery, result qualification, pinned collection and canonical report. That integrated path is covered by 12 tests (18.51s). Mocked official-shaped replies and an HTTPS socket serving explicitly synthetic bytes were used. Human and semantic approval were fixture-only.

Each dispatch scenario asserts at most one original generation; the positive collection case asserts exactly one. Repair attempts and uploads are zero. The terminal release left billing unknown. These counts and lifecycle checks describe the offline execution path; they do not establish real footage, visual quality, provider billing, or production qualification.

## Authority boundary

The proposed authority is for one original attempt and zero repairs. Preparation does not constitute approval. Any later live attempt requires a separate explicit human decision against the exact retained request, native settings and preview, with authoritative current authority and result reconciliation. A timeout or terminal result cannot authorize a substitute, repair or resubmission.

Independent review found and drove fixes for revoked human-gate spending and malformed available-price evidence. The initial reproduction had one generation despite `human_approved=false`; after fixes the same refusal path produced zero generations. Final independent Sol review reported no actionable findings or conflicts. Further detail and verified regression evidence are recorded in the local implementation history; this note does not treat mocked success as provider validation.

## Implementation and verification record

Local commits, in order:

- `4c4f44e` plan
- `00d6826` reference-free compiler
- `2348282` qualification pipeline gate
- `32c622c` typed ledger/shared guard
- `f233a7b` unknown authority/dispatch
- `d817f25` tools/report/registered integration
- `a00053a` documentation
- `d5c924e` runtime-test scope for a pipeline without composition
- `cb03215` review fixes and guarded reference-free provenance

Targeted offline verification included contract/compiler and pipeline checks, ledger/guard and tool/report checks, dispatch/recovery checks, and the integrated registered workflow. The final integrated workflow passed 12 tests in 18.51s. The report, Auto and menu checks passed 135 tests in 129.15s before latest corruption cases; the subsequent Auto/menu subset passed 80 tests in 14.11s after schema-v2 fixture repair. Root's latest report subset passed 19 tests in 0.80s. Root's latest combined tool/account/execution set passed 225 tests in 1.81s. These overlapping targeted counts are not a full-suite total.

Fresh independent review caught two consequential defects: a revoked human gate could initially spend, and a malformed available-price envelope could pass. Both were corrected and independently rechecked. The final authority/dispatch set passed 63 tests in 45.75s; late refresh revocation checks passed 2 tests in 4.02s. The refusal verifier passed 2 tests in 2.49s. Review's broader checks passed 180 tests in 65.38s. `make lint` and `git diff --check` passed.

The first full run exposed the no-compose pipeline's inclusion in the runtime-planning test. The documented pipeline exception was applied, independently reviewed, and its file passed 26 tests in 0.33s. That failed run was interrupted for the correction; its interruption produced a temporary-fixture teardown error. It is not passing verification evidence.

A bounded independent second opinion on current authority, consumed-occurrence replay and refusal release found no actionable findings. This was source/regression inspection through the requested Astra route; served-model identity remains unverified. No new tests or provider calls were made by that reviewer.

The fresh full command `.venv/bin/python -m pytest tests/ -q` passed on frozen source commit `d5c924e`: **4,222 passed, 11 skipped, 3 xfailed, 1 subtest passed in 1,200.46 seconds**. `make lint`, compilation of all 30 changed Python paths, and `git diff --check` also passed. The [structured work return](2026-10-07-openart-unknown-cost-work-return.json) retains per-unit evidence and route receipts.

## Final review and local acceptance

The LFG simplification shared the exact and unpriced acknowledgment transaction while retaining distinct fixed tables, typed binding validators and conflict checks. The relevant ledger/dispatch/workflow run passed 131 tests in 197.91s. Caching ledger instances was declined because it would omit repeated constructor safety checks.

Formal review `20261007-114027-3aa41d70` completed all selected local lenses and the sanctioned Cursor Auto peer. The peer's served identity and cross-family independence were unverified; its findings were independently validated rather than promoted as independent agreement. Its consumed terminal job was cleaned up. The review returned four actionable findings, all resolved in `cb03215`:

- Preserve the exact v1 outbox publication shape so a previously published, unacknowledged event replays without changing bytes or raising a journal conflict.
- Permit selection and final provenance only for the explicit schema-valid reference-free OpenArt text-to-video variant; all legacy asset, semantic, frozen preparation, native and current-byte guards remain.
- Reject exact scopes co-tagged with unknown-cost authority before a claim or submission.
- Label the exact default and explicit unknown-cost billing option in discovery. A fresh reviewer also caught unknown-cost being advertised for unsupported image-to-video profiles; discovery now excludes it there and declares text-to-video/reference-free applicability.

The fixes received real failing regressions before correction: three recovery/authority failures, the reference-free selection failure at missing `payoff_asset_id`, and the mode-discovery failures. The corresponding focused checks passed afterward. A fresh reviewer independently inspected the canonical provenance source and the other final diffs, tested the ledger and metadata fixes, and reported no remaining actionable findings. There are no unapplied actionable findings or settled-decision conflicts.

Parent final verification used an isolated Git archive of the exact staged tree `2b08cc4c6754f1992260defe925c0bec7b078952`, then confirmed that `cb03215` has that same tree. **582 tests passed in 277.06s**, covering exact and unpriced ledgers, exact authorization, original dispatch/recovery, the unknown-cost workflow, reference-free selection/final provenance, legacy/local provenance, compiler/contract, tools/catalog and qualification pipeline. Lint explicitly used `.venv/bin/python`; all 10 final changed Python modules compiled. Counts overlap earlier checks and are not summed.

Concurrent UI/proposal and shot-continuity edits were preserved and excluded. For the overlapping provenance file, only the reviewed canonical blob was staged; a guarded commit verified the exact owned staged-path set and source tree. The isolated run contains none of that foreign continuity implementation. This record makes no verification claim for the concurrent edits.

The `ce-test-browser mode:pipeline` scope check found no browser-rendered routes changed by this task. Browser testing is recorded as not applicable, with zero pages tested; concurrent Backlot UI changes are excluded. Shipping is local-only under R9 despite configured remotes: no push, PR or CI watch was run. A nonblocking review note remains about dedicated refreshed-evidence timeout fault injection; existing transport/deadline and authority-refusal coverage is retained. Changed OpenArt output bytes still fail closed with the existing native error type.

The actual sample packet was reverified after the fixes using retained source/native/profile evidence with provider calls forbidden. Its original packet and request hashes remain unchanged; no active scope, unknown-cost authorization sidecar, attempt, upload or output exists. This verifies preparation integrity, not current entitlement, billing or transport success.

## Current state

The local sample is prepared for human review. It is not approved for live spending, and no real generation or result qualification has occurred. The exact sample, its unresolved requested economics and the approval boundary are retained for a separate user decision. Existing exact-price behavior remains distinct; unknown-cost Auto-continue is not activated. Grok quota uncertainty is unchanged.

This receipt is recorded before the caller's final `awaiting_human` preparation checkpoint. That transition is the final pipeline action, after local acceptance and commits, and does not activate live authority.
