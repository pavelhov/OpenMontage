---
title: OpenArt Factory - Auto-continue Amendment
type: feat
date: 2026-10-05
amends: docs/plans/2026-10-05-1616-feat-openart-factory-plan.md
artifact_readiness: implementation-ready (revision 3, accepted by root and merged into base plan)
---

# Auto-continue Amendment

**Authority.** The user explicitly approved an optional Auto-continue ("YOLO") setting, relayed from source chat `01a10cfc-7487-7c52-9f56-d27f126d304f`. This amendment adds R15–R18, AE9–AE22 and unit U5P. U1–U6, KTD1–KTD10 (including KTD6's lock order), the accepted commits (c4533a7, 90a131e + 8f22270, fcec5aa) and U4's in-progress ledger keep their meaning. It grants no subscription purchase, benchmark, publication, push or shipping authority, and never authorizes paid production by default.

**Reconciliation.** The base plan's blanket rules apply to **Strict mode, which remains the default**: "switching provider/model requires matching authorization", "no hidden fallback", AGENT_GUIDE's "do not continue with a substitute path until the user approves", and per-gate approval. With no active valid policy, behavior stays the same apart from R18's explicitly accepted same-shot video duplicate guard. An active policy is a retained, explicit, pre-approved envelope. Inside it, a listed provider/model or flex compromise is an approved choice that is logged and announced before use. Anything outside it stops and asks. Selector scoring and fallback never choose; the agent picks from the existing registry and pins each derived canonical request exactly (singleton `allowed_providers`).

## Requirements additions

- R15. An optional per-project policy (`mode: auto_continue`) is approved once, up front, through retained evidence bytes plus a `decision_log` `approval_policy` entry. It binds an immutable per-shot **baseline** and names allowed providers and exact models, locked must-haves with exact values, explicit flex dimensions, an exact OpenArt credit ceiling, attempt caps (total, repair, per shot) and pre-authorized checkpoint stages. An absent, revoked, stale, conflicting or invalid policy means Strict.
- R16. Within the policy, the agent may choose an eligible route, revise the exact request only along flex paths relative to the bound baseline, compile and preparation-review it, then derive an exact scope (plus OpenArt credit terms) and continue without asking. Derived artifacts are recomputed from the immutable policy and baseline at every preflight and are never trusted by their own bytes.
- R17. Must-haves are proven from bindings, never from caller booleans. Baseline cast, dialogue, source and story bindings are preserved even when the explicit `locked` maps omit them. Locked native controls require the exact value as a real native argument. A locked control that no eligible route supports requires a user decision. Missing or failed quality evidence stays visible, and an exhausted cap produces an honest uncertified draft.
- R18. Scope: strict **motion/video** attempts for the same shot only. Image, audio, avatar and local render phases, and other shots, are unaffected, so distinct phases can still run in parallel. Unknown paid acceptance for a shot's video attempt blocks derivation and dispatch of another video attempt for that shot on **every** provider until the original attempt is resolved through KTD7 or `resolve_attempt` evidence. An active or pending original attempt likewise blocks a same-shot video duplicate. Pending includes a private ledger outbox reservation that has not yet been journaled; it counts before journals do. A TTL, empty history, worst-case debit or human attestation cannot release either block. Strict preflight enforces this under the project lock.

## Policy artifact

New closed schema `schemas/artifacts/autonomy_policy.schema.json` (`additionalProperties: false` at every level) for `artifacts/autonomy_policy.json`:

| Field | Meaning |
|---|---|
| `version`, `policy_id`, `mode` | `"1.0"`; a stable ID; `auto_continue`. Strict is represented by absence or revocation. |
| `project_id`, `story_revision` | Must equal the strict marker. A story revision change invalidates the policy. |
| `providers[]` | `openart_cli`: `{models:[exact IDs], billing:"credits", ceiling:"<decimal at provider precision>", account_id_sha256, workspace}`. `grok_cli`: `{models:[exact IDs], billing:"subscription_quota_unknown"}` with no price or ceiling field. Only these two provider IDs are allowed. The paid Grok API provider (`grok`), any USD-billed route and any purchase or top-up action are schema-rejected. No USD/credit conversion is stored. |
| `caps` | `max_total_attempts` and `max_attempts_per_shot` are integers ≥ 1. `max_repair_attempts` is an integer ≥ 0, where 0 means first-pass only. Failed, uncertain, pending (including outbox-only) and open-scope attempts all count. |
| `shots` | `{shot_id: baseline}`; explicit IDs, no wildcard (see Baseline). |
| `locked` | Additions to the implicit baseline locks: `cast: {character_id: [{sha256, role}]}`, `dialogue: {line_id: {text_sha256, speaker_id}}`, `sources: [sha256]`, `story_predicates: [{id, sha256}]`, `controls: {name: {value} \| {value_sha256}}`. The value field holds the exact native value, or the hash of the bound frame or audio bytes. |
| `flex` | The only paths that may change: `duration_s: [values]`, `resolution: [values]`, `references: {droppable_roles: [], substitutes: [{sha256, role}]}`. Substitutes must be approved asset bytes with explicit roles. |
| `checkpoint_stages` | A subset of the manifest's gated stages. `publish` is rejected. |
| `evidence` | `{path, sha256}` for `approvals/autonomy-policy-<sha>.json`, whose exact content is `{"kind":"openart_autonomy_policy","policy":<policy minus evidence>,"baselines":{shot_id:<frozen baseline bytes>}}`. |

**Baseline (per shot, immutable).** Each baseline binds:

- `request_sha256` of the approved canonical request, or `planned_request_template_sha256` of U3's planned upstream template.
- Frozen `static_input_assets: [{sha256, role, binding}]`.
- `$upstream` handoff declarations by name and producing shot. Their output values are never pinned.
- `approval_plan_digest`, `script_snapshot_sha256` and `dialogue_coverage_sha256`.
- `static_source_packet_sha256`, the hash of a **planning projection** of U3's source packet. The projection holds the contract, scene, script, cast pack, dialogue lines with text and speakers, source hashes, story order, end states, durations and static assets. It excludes every future `$upstream` value, resolved output, review and native provider form.

No full source packet hash exists at approval time, so there is no approval cycle. At derivation, the actual resolved upstream outputs and their reviews are bound into the candidate. Each must be the selected and reviewed output of its declared producer, and its hashes enter `derived_from_policy.resolved_upstream`. The full baseline bytes are copied into the evidence file, so later edits to working artifacts cannot change what the policy approved.

**Conflict rule.** The validator rejects the whole policy, falling back to Strict and surfacing a reason, when any of these holds: a flex path intersects a locked or implicitly locked path; a substitute's role is locked in a baseline cast binding; a duration or resolution flex contradicts a locked control value; or a listed shot has no baseline. Rejection is all-or-nothing, never per field.

`policy_sha256` is the canonical hash of the evidence content, following the pattern of `credit_authorization_digest`. Activation appends `category: approval_policy`, `subject: "Production autonomy policy"`, `selected: "auto_continue:<policy_sha256>"`, `user_approved: true`. Revocation appends the same pair with `selected: "strict"`. The latest entry for that pair is authoritative. This reuses the repository's existing trusted retained-approval boundary. It adds no new human-proof service and no UI.

## Helper `lib/production_autonomy.py` (pure; zero real provider calls)

- `load_active_policy(root) -> (policy|None, reasons)` checks the schema, the evidence bytes and hash, the latest decision pair, the marker project and story revision, the conflict rule, and that no `provider_benchmark` manifest exists. Any defect returns `None`, which means Strict.
- `contract_delta(policy, shot_id, candidate_packet)` compares the candidate's static planning projection against the bound baseline only. No caller-supplied "approved" object or prompt is accepted. It fails if any path outside declared flex differs, including `$upstream` names, static bindings, cast, dialogue, story and controls.
  - A duration flex is accepted only as a **declared retiming**. The shot's contract, scene and script durations and timing marks change by the deterministic `retime(baseline, new_duration)` function, while cast, line text, speakers, line order, sources, story order and end states stay identical. Any other timing edit fails.
  - Reference flex reruns U3 preparation review against the approved bytes and roles.
- **Canonical prompt.** The candidate prompt is never taken from the caller. The chosen provider's prep builder recompiles it from the validated retimed packet and the route, and the result must equal the prompt in the candidate request. Prompt differences from the baseline are therefore only those implied by the allowed flex and route.
- `lock_proof(policy, shot_id, compiled_request, prep_review, source_packet, submitted_inputs)` checks:
  - Cast: the shot's closed cast pack hashes and roles match the baseline plus `locked.cast`. Each member is either a frozen submitted input in its role, or is carried by an approved **composite start board**. A board counts when its named binding (`start_frame` sha), approved preparation review and frozen prep all list that member. Image2video's single `start_frame` is therefore valid for multi-cast shots, and the proof never requires forwarding every cast asset.
  - Dialogue: every baseline and locked line appears in U3 coverage with an identical text hash, and its speaker maps to the bound cast role.
  - Sources and story: source hashes are frozen inputs, and story predicate IDs and hashes are unchanged in the current contract.
  - Controls: each locked control is an actual native argument with the exact value or value hash. Prompt wording never counts (AE1).
  It returns a `lock_digest`. Any missing coverage, binding or board review means the lock is unproven, which blocks.
- `eligible_routes(policy, shot_id, menu)` intersects policy providers and models with real-account qualification: U1 `openart_qualification` records, plus existing `grok_cli` discovery on the subscription path. Fixture-origin evidence is ineligible in a live project. Qualification records stay additive with their origin SHA, and history is never mutated. Each excluded route returns a reason.
- `derive_scope(policy, shot_id, candidate) -> (scope, credit_authorization|None, decisions[])` emits a normal v1.0 scope with `approved_by: "policy:<sha>"`, the policy evidence, and `attempts_per_shot = 1`. Its phase is `first_pass`, or `repair` with `replaces_attempt_ids` when failed-review evidence exists. Provenance is `derived_from_policy: {policy_sha256, decision_id, derivation_index, baseline_sha256, lock_digest, compiled_request_sha256, preparation_review_id}`. For OpenArt it also emits terms with `allowance_id = "policy:<sha>:openart"` and `allowance` equal to the policy ceiling, plus the retained `openart_credit_authorization` capture, so `validate_credit_authorization` runs unchanged.
- `validate_derived_scope(root, scope, inputs)` recomputes policy, baseline, delta, lock proof, compiled request, review, caps and the derived hashes, then requires equality.
- `cross_provider_block(journals, ledger, shot_id) -> reasons` is a pure reader limited to strict motion/video attempts for that shot. It reads ledger private `outbox` rows **first**: a reserved-but-unjournaled row counts as pending until U4 replay settles it. It then reads journals for uncertain, unresolved, pending or running attempts on any provider.
- `completion_report(root, policy_sha256)` writes a new `artifacts/autonomy_reports/<utc>.json`. For each actual request it records provider, model, duration, resolution, references, attempt IDs and job IDs; the compromises against the baseline; OpenArt credits reserved, settled and unresolved; Grok as "subscription quota unknown, N attempts"; and quality status as certified or draft with findings. Earlier reports are never rewritten.

**Budget boundary.** OpenArt spend is bounded by the exact credit ceiling. The ledger already aggregates per account key (account + workspace) and `allowance_id` across every authorization: `reserve_prepared` sums `_totals(db, account_key, allowance_id)`, and the allowances table key is `(account_key, allowance_id)`. All derived OpenArt scopes share one atomic ceiling, alongside any other allowance on that account. Grok CLI runs on a fixed subscription with an unknown quota, so its only bound is the attempt caps, and it may never add a USD charge. Setup copy states this verbatim: *"Grok CLI uses your existing subscription; its remaining quota is unknown and is not a cost ceiling. Auto-continue never uses paid Grok API calls and never buys plans, credits or top-ups."*

## Integration seams

| File | Change |
|---|---|
| `lib/production_execution.py` | Strict preflight runs under the project lock. For motion/video it always calls `cross_provider_block` for the shot (R18), whether or not the scope is derived, and fails closed. Every dispatch path that reaches video submission goes through this preflight, including direct canonical dispatch; none may bypass it. If `derived_from_policy` exists, preflight also calls `validate_derived_scope` and checks policy-wide caps. Existing uncertainty and first-pass checks compare prior attempts for the same shot and production kind; a missing or unclassifiable kind fails closed. This narrow phase correction prevents an approved image/audio/local-render phase from being mistaken for a video duplicate. Exact scope counts, repair replacement IDs and all phase-specific approvals remain binding. Strict avatar generation stays unsupported. |
| `lib/checkpoint.py` | Adds an optional `approval_basis` (see below). The `human_approved=True` path is unchanged. |
| `lib/production_request.py` (U5P-owned, narrow) | U3's `source_packet` calls `_check_required_native_controls` unconditionally, `prepare_compiled_request`/`_validate_compiled` call the OpenArt `jobs.native` form, and `validate_timing` requires native duration == shot duration. U5P splits out a provider-neutral core: `build_static_source_packet` (generic coverage, source and timing without the native-control check), `retime`, and a `prep_builder(provider)` dispatch. The default OpenArt request bytes, digests and observable guards are preserved, including required end-pin/audio rejection; regression tests check these after the internal refactor. `validate_timing` compares native duration against the *candidate* (retimed) contract duration, and only `contract_delta` can produce that. |
| `tools/_grok_cli_media.py`, `tools/video/grok_cli_video.py` (U5P-owned, only as needed) | Expose a pure Grok prep builder and argument validator that emit the actual qualified `grok_cli` argument form (prompt, duration, resolution, single start image) and a receipt proof of the submitted args and input hashes. It never fabricates an OpenArt native form or native audio. Locked controls that Grok cannot carry make the route ineligible (AE13). |
| `lib/openart_credit.py`, `lib/provider_credit_ledger.py` | No edits. U5P consumes U4's existing allowance aggregation and outbox replay. |
| New | `lib/production_autonomy.py`, the schema above, `tests/lib/test_production_autonomy.py`, `tests/lib/test_checkpoint_policy_preauth.py`, `tests/integration/test_openart_auto_continue.py` |
| Guidance | Add a "Strict default / Auto-continue envelope" paragraph to `AGENT_GUIDE.md` (Decision Communication, No Unilateral Substitutions, Human Checkpoint). Update `skills/meta/checkpoint-protocol.md`, `docs/OPENART_CLI.md` and `.agents/skills/openart-cli/SKILL.md` with the setup questions, the budget copy and the announcement format. |

**Checkpoint preauthorization (actual code seam).** Today `write_checkpoint` raises GATE VIOLATION for a gated `completed` without `human_approved`, and `review` is an untyped dict. The new parameter is `approval_basis={"kind":"policy","decision_id","policy_sha256"}` with `review.preauth={"artifact_path","artifact_sha256","review_path","review_sha256","critical_findings":[]}`. `write_checkpoint` verifies all of the following:

- The policy is active, and its hash and decision ID match.
- The stage is in `checkpoint_stages` and is not `publish`, and the project is not a benchmark project.
- `artifact_path` is the stage's canonical artifact, and its sha equals current bytes on disk.
- `review_sha256` equals the current review artifact, which names that same artifact hash.
- The review's own `critical_findings` list exists and is empty. The caller's list is only cross-checked against it.

If every check passes, it writes a truthful `completed` with `human_approved: false` and `metadata.approval_basis` plus the bound hashes. If any check fails, it **raises GATE VIOLATION and writes nothing**; it never silently downgrades. The agent then writes `awaiting_human` through the existing checkpoint protocol and presents the gate. Final certification and draft labeling are unchanged.

**Derivation and dispatch order.** KTD6 is unchanged: project lock first, then a short ledger transaction, with no network call under either lock. The transport lock never acquires the project lock. For each shot and attempt:

1. Run `load_active_policy` and `cross_provider_block` as read-only pre-checks.
2. Choose an eligible route.
3. Revise within flex, then run `contract_delta` against the baseline.
4. Compile with U3 and run feasibility and preparation review.
5. Run `lock_proof`.
6. For OpenArt, run the read-only quote refresh outside all locks.
7. Under the project lock only, recheck the policy, `cross_provider_block`, caps and the at-most-one-open-undispatched-derived-scope rule per shot. Then append the scope (and credit authorization) to `production_scopes.json` and the decisions to `decision_log.json`, append-only, with no ledger transaction and no network. Release the lock.
8. Run normal KTD6 dispatch. Strict preflight takes the project lock and reruns `validate_derived_scope` and `cross_provider_block`. A short ledger reservation checks the OpenArt allowance and writes the outbox entry. Both locks are released, then the submit runs.

First-attempt preparation, real native controls, exact quotes, the credit ledger and the pure offline zero-call rehearsal are unchanged.

**Decision logging.** Each actual provider or model change appends `provider_selection` with the original route's subject, such as `"Shot S3 video route"`. Each flex compromise appends `downgrade_approval` with subject `"Shot S3 request settings"`, or `budget_tradeoff` when the ceiling forced it. The superseded choice goes in `options_considered`. Entries set `user_approved: false`, and `reason` cites `policy:<sha>` and the activation decision ID. The agent announces each change before dispatch; inside the envelope, no reply is required.

**Strict-only zones.** These remain Strict:

- U6 benchmark projects, which reject any policy unless a separate exact benchmark approval changes this.
- Image, audio and avatar phases.
- Providers other than `openart_cli` and `grok_cli`, including paid `grok` and any USD-billed route.
- `publish`, subscription or plan changes, and episode, pool, scoring and router behavior.

## Acceptance examples

- AE9. **Finish the video.** The policy allows OpenArt H3 Turbo/Max and Grok CLI, flexes duration {5, 6} and resolution {768p, 720p}, and locks cast and dialogue. OpenArt is ineligible at 6s, so the agent derives a Grok CLI attempt at 6s. Delta, timing, review and lock proof pass, the decision is appended and announced, dispatch proceeds, and the report lists the compromise and "quota unknown".
- AE10. A revision drops a baseline line that is absent from `locked.dialogue`, swaps a speaker role, changes prompt text, or replaces a cast hash. `contract_delta` or `lock_proof` fails, and nothing is reserved or submitted. A caller `locks_ok: true` or a caller-supplied approved request is ignored.
- AE11. An OpenArt submission is uncertain, or its reservation sits in the outbox without a journal entry. A Grok dispatch for the same shot fails in strict preflight under the project lock, including through direct canonical dispatch, until KTD7, `resolve_attempt` or U4 replay resolves it. Elapsed time or empty history does not unlock it.
- AE12. The OpenArt ceiling, aggregated by the ledger across all authorizations on the allowance, or `max_total_attempts` would be exceeded. Derivation stops, and the agent delivers an uncertified draft with its findings.
- AE13. A locked `end_frame` value hash is unsupported by every eligible route. The agent stops and escalates with the base plan's blocker structure.
- AE14. A listed gated stage completes, truthfully recorded with `human_approved: false`, when its canonical artifact hash, current review hash and empty review findings all match. A revoked or stale policy, an unlisted or `publish` stage, critical findings, a missing or caller-only empty list, a stale artifact or review hash, changed evidence bytes or a benchmark project each raise GATE VIOLATION with no write. The agent then writes `awaiting_human`. Without `approval_basis`, behavior is byte-identical to today.
- AE15. A hand-edited derived scope (model, duration, `approved_by`, credit terms, baseline hash), or a scope left after revocation, a story change or a baseline edit, fails `validate_derived_scope`. Fixture-only capability evidence makes a route ineligible in a live project.
- AE16. A two-character shot uses one approved composite start board. Lock proof passes through the board's named `start_frame` binding, its review and its frozen prep. If the board review omits one cast member, or the board hash differs, the proof fails.
- AE17. A policy that flexes resolution while locking a `resolution` control value, or lists a substitute in a locked cast role, is rejected whole, and the project stays Strict.
- AE18. An OpenArt attempt for S3 is submitted and running. Deriving or dispatching any S3 attempt on either provider fails until that attempt is terminal.
- AE19. A policy naming `grok`, a USD route, or a purchase or top-up is schema-rejected.
- AE20. Duration flex 5→6s: the candidate packet is `retime(baseline, 6)`, U3 timing passes against 6s, and the prompt is recompiled by the builder. A candidate that also reorders a line, changes an end state or supplies its own prompt fails. With no policy, OpenArt still rejects a required end-pin/audio, and a 6s native duration on a 5s contract still fails.
- AE21. Grok CLI derivation uses the Grok builder and receipt proof. No OpenArt `jobs.native` form appears, and a locked `native_audio` makes Grok ineligible.
- AE22. While S3's video attempt is uncertain, S3 image/audio work, a local render and S4's video can all proceed. A private outbox reservation for S3 video with no journal blocks a second S3 video attempt. `max_repair_attempts: 0` is valid and allows no repair.

## U5P. Auto-continue policy (new unit)

**Order:** U4 → U5 → **U5P** → U6. U5P needs U4's allowance aggregation and outbox replay, and U5's menu metadata. One integration owner edits `production_request.py`, the Grok tool files, `production_execution.py` and then `checkpoint.py` in sequence. Nothing writes U4-owned files.
**Requirements:** R15–R18, R2, R9, R14; AE9–AE22. AE1 and AE6–AE8 still pass with and without a policy.
**Tests (offline, network disabled, zero REAL provider calls; executable fake `openart`/`grok` CLI calls are expected and asserted):**

- Schema coverage: closed fields, Grok subscription only, and rejection of `grok`, USD and purchase entries.
- Evidence bytes and baseline copy; revocation, story change and baseline drift.
- The conflict rule matrix.
- `contract_delta` against the baseline, including a caller-supplied approved object that is ignored, non-flex prompt and binding changes, and reference flex with review rerun.
- Lock proof: positive and negative cases per binding, the multi-cast composite board, and control value hashes.
- Derivation determinism and tamper rejection.
- R18 in strict preflight for uncertain, outbox-first and running originals, across providers and through direct dispatch, plus non-blocking image, audio, local render and other-shot cases.
- Retiming and canonical prompt recompilation, OpenArt default guard regressions, and Grok builder/receipt proof without an OpenArt native form.
- Static planning projection with no upstream values, resolved upstream rebinding at derivation, and `max_repair_attempts: 0`.
- Caps that count failed, uncertain, pending and open scopes.
- Allowance aggregation across derived scopes and other authorizations on the same account key.
- The KTD6 lock order: no network under the project lock or ledger, and the transport lock never takes the project lock.
- The checkpoint matrix, including GATE VIOLATION with no write on an invalid basis, and hash binding.
- Benchmark rejection.
- Decision append with the same category and subject.
- The mixed Grok/OpenArt report.
- A Strict regression: with no policy, results are identical apart from R18's video duplicate guard and narrow production-kind isolation of prior attempts, which are asserted separately. Unknown kinds fail closed; no phase gains scope or repair authority.

**Verification:** The base Verification Contract rows plus the tests above. A model other than the implementer adversarially reviews the baseline delta, lock proof, R18 and checkpoint authority.

## Risks and open seams

1. **Unstructured reviews:** A stage without a structured review artifact that carries `critical_findings` and the artifact hash cannot be preauthorized. This is safe, but it falls back to human gates until reviewers emit that structure.
2. **Grok usage:** Grok usage is bounded only by attempts. If the subscription quota runs out mid-run, attempts fail visibly and count against the caps.
3. **Coverage IDs:** Lock proof depends on U3 coverage carrying line, speaker and role IDs, and on board reviews naming cast members. Any gap means the lock is unproven, and the attempt blocks.
4. **R18 strictness:** R18 also tightens Strict mode by blocking same-shot video duplicates across providers. Root accepted this narrow behavior change and the associated production-kind isolation when merging the amendment. The existing preflight gathered prior attempts by shot alone; tests must prove that explicitly approved other phases remain independent while unknown kinds and same-phase uncertainty fail closed.
5. **Trust boundary:** The boundary matches current scopes: retained local approval bytes. It is documented here and not strengthened.
