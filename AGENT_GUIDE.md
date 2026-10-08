# OpenMontage — Agent Guide

Read this entry point before responding. Load the current task/stage owner below;
other pipeline, provider and historical material is available when relevant.
[PROJECT_CONTEXT.md](PROJECT_CONTEXT.md) owns architecture and repository conventions.
Software maintenance follows its approved brief; it is not production consent.

## First action

- A vague first production request (“make me a video”, “what can you do?”): read
  [onboarding](skills/meta/onboarding.md) before discovery.
- A video URL/file supplied as inspiration: read
  [video-reference-analyst](skills/meta/video-reference-analyst.md), analyze actual
  pacing/content/style with the local analysis tools, then select the pipeline.
  Source footage to edit instead uses `source_media_review` and a footage-led pipeline.
- A concrete video request: follow Rule Zero. Use the current brief and supplied
  choices; develop concepts/openers only while that creative decision is open.
- A resume: inspect the project's next checkpoint, originals, current selections
  and authority before doing the remaining stage. Do not restart completed work.

## Rule Zero — All Production Goes Through a Pipeline

Every video production request uses `pipeline_defs/<pipeline>.yaml`. Select the
matching manifest; ask only if the request leaves the pipeline unclear. Run
preflight, then execute its stages in order. Read the **current stage** director
(`skills/pipelines/<pipeline>/<stage>-director.md`) before work in that stage.
Before calling a generation tool, read its registry `agent_skills` in
`.agents/skills/`: Layer 3 provider prompting and controls are mandatory.
Use registered tools and their normal `execute`/`dry_run`; no ad-hoc provider runner.

The manifest owns stage tools, canonical outputs, review focus, success criteria
and human gates. Discover pipelines from the manifests, skills from
[skills/INDEX.md](skills/INDEX.md), and tools from the registry rather than a
copied catalog. Mention a selected beta pipeline's limitations. Raw historical
prompts/learning logs are consulted only for relevant evidence, never auto-loaded
as current policy. Preserve useful cast/style/provider knowledge and history.

## Owner routing

| Current work | Read its existing owner |
| --- | --- |
| Checkpoints, resume and stage approval | [checkpoint-protocol](skills/meta/checkpoint-protocol.md) |
| Semantic evidence and final certification | [reviewer](skills/meta/reviewer.md), current director/manifest |
| Board image lifecycle and overlapping preparation | [shot-preparation-overlap](skills/meta/shot-preparation-overlap.md) |
| Cast, start/end/identity roles and native frame pins | [visual-development](skills/creative/visual-development.md) |
| Route intent, primary/alternate planning and legacy repair diagnosis | [VIDEO_MODEL_SELECTION](docs/VIDEO_MODEL_SELECTION.md) |
| Grok/OpenArt CLI authority, billing, original jobs and Auto-continue | [OPENART_CLI](docs/OPENART_CLI.md), selected tool's Layer 3 skill |
| OpenArt connector forms, uploads, account/project and host handoff | [OPENART_MCP](docs/OPENART_MCP.md), selected tool's Layer 3 skill |
| Retaining an approved trim and its new review | [PRODUCTION_DERIVED_EDITS](docs/PRODUCTION_DERIVED_EDITS.md) |
| Runtime mechanics | [HyperFrames](skills/core/hyperframes.md), selected runtime's Layer 3 skill |
| Bespoke/hero composition | [taste-direction](skills/meta/taste-direction.md), [bespoke-composition](skills/meta/bespoke-composition.md) |

Studio owns creative judgment, episode defaults, board queue, defect communication
and creator commands. The engine owns exact native capabilities/input/provenance,
execution, cumulative counts, uncertainty, composition and certification. Identity
consent owns exposure and authority; QC owns evidence; preparation owns DAGs;
delivery defaults own export/handoff. An identity policy cannot bypass engine gates
or apply its voice/defaults to another identity.

## Mandatory Preflight

Discover the registry and call `registry.provider_menu_summary()` first. Present
its human-ready capability ratios, configured routes, setup offers, runtime warnings,
account/dispatch readiness and exact native-control limitations. Use `provider_menu()`
only for necessary per-tool detail; `support_envelope()` is a debugging firehose.
Read the selected manifest's required/fallback tools and report passed, degraded or
blocked before production. Do not hardcode models, credential names, prices or setup
URLs: use registry contracts and `install_instructions`.

Offer simple setup when useful and honor a decline. Restricted shared-installation
users get administrator setup requests, never instructions to add personal keys.
Check an already-configured wrapper before offering direct vendor credentials.
Current schema/transport/account evidence establishes support; an optional prior
live result is separate empirical evidence, not creative certification or new authority.
Unknown cost/quota is unknown, never free, zero or a fabricated USD ceiling.

## Decision Communication Contract

Announce before each paid/consequential generation: shot, exact tool/provider/model
(or CLI-managed unreported media), sample/batch/repair phase, reason, approved changes,
billing exposure, remaining caps and exact authority. Retain the route and compromises.
Grok's agent model is never its media model; subscription quota remains unknown.
Auto-continue announcements bind current policy SHA and activation decision; it
never buys plans/credits/top-ups or invokes paid Grok API calls.

Major provider/model/treatment/runtime or approved audio changes require approval
unless the existing exact retained policy already permits them. Use the approved
pool and declared flex; ranking or a generic “go ahead” grants no dispatch authority.
Preserve `model_selection_intent`: `exact` never substitutes; `prefer`/`auto` rank
only eligible approved routes. Announce and log changes inside authority without
asking again for already-approved routine work.

Append changed choices to `decision_log` using the same `(category, subject)` pair,
with the superseded choice in `options_considered` and its rejection reason. Never
silently mutate history or reword the subject into a competing current choice.

### Present Both Composition Runtimes (HARD RULE)

Run a fresh availability check. Reuse an already-approved current runtime and its
complete retained options/decision without repeat approval. When the runtime lock
is new or unresolved and preflight reports both Remotion and HyperFrames available,
present both with brief-specific pros/cons and a recommendation, obtain approval, then log both in
`render_runtime_selection.options_considered` (and FFmpeg where applicable).
If only one is available, disclose the limitation and record the unavailable option.
Carry locked `render_runtime` from proposal into edit/compose. Motion-required
briefs cannot silently become still animatics or Ken Burns. An unavailable locked
runtime remains a blocker. The runtime owner's decision matrix is guidance for
this choice, never approval to substitute.

### Composition Authoring Mode — Templated vs Atelier

Choose and log `composition_mode` separately from runtime. Templated stock scenes
fit routine/batch drafts. Prefer atelier for hero/launch/brand work; disclose its
extra authoring effort. Follow taste-direction → bespoke-composition, reuse engine
knowledge rather than finished creative components, and review distinctness. The
stock catalog/registry is mechanics knowledge, not an atelier creative menu.

### Escalate Blockers Explicitly

State what was attempted, what failed (auth/access/tool/design), concrete options
and the recommendation. Unsupported controls, exhausted authority and uncertainty
are honest blockers. Prepare alternatives but execute only those already approved
and eligible; otherwise obtain the missing exact approval. A terminal missing
result's bounded alternate rules below do not permit substituting usable footage.

## Orchestrator

The agent follows the manifest state machine, commonly
`research → proposal → script → scene_plan → assets → edit → compose`.
Use `checkpoint.get_next_stage()`, current director, registered tools, reviewer and
checkpoint owner. Python enforces declared prerequisites, scope, hashes, review
completeness and eligibility; the producer/reviewer supplies semantic judgment.

Initialize `projects/<kebab-case-id>/` with `lib.checkpoint.init_project`, then
`python -m backlot open <id>`. Board failure is nonfatal: it is an observer.
All outputs, including atelier/HyperFrames artifacts and explicit `output_path`,
belong under that project's `artifacts/`, `assets/` and `renders/`; root/temp media
is invisible to its board. New Studio generation uses strict governance and a
story revision; enrollment of existing projects is deliberate, never automatic.

## Stage Agents

Each stage produces its manifest's canonical schema-valid artifact. Typical outputs:
idea/brief, script/script, scene_plan/scene_plan, assets/asset_manifest,
edit/edit_decisions, compose/render_report. Keep noncanonical media in its project
asset/render directories and retain actual provider/version/seed provenance.
Read only the active director and relevant tool guidance; do not preload every stage.

## Strict generative production

Before translating a shootable package, read
[shot_contract.schema.json](schemas/artifacts/shot_contract.schema.json) and
[first-pass fixture guide](tests/fixtures/first_pass/README.md). Fixtures' synthetic
passing reviews are never real evidence. Map completed action to
`endpoint_completion`, late characters to `late_cast_ids`, ending speakers to
`payoff_speaker_ids`, visible sources to `required_visible_speakers`.
Store `artifacts/shot_contract.json`, exact `production_scopes.json` authority and
request `governance: {scope_id, shot_id}`. Fixed requests use
`planned_request_digest`; declared dynamic sources use `planned_request_template`
and canonical `$upstream`. Freeze approved static hashes, duration and unique
attempt output paths. Constructing a scope/template does not authorize it.

Reuse canonical `lib.production_execution` attempts, original-session reconciliation
and reviewed selections. A fresh exact governed dry-run immediately precedes
execution. Unknown originals are collected/reconciled, never resubmitted. Preserve
original/rejected bytes and cumulative allowances. Prepared independent frame
pairs do not wait on unrelated videos; actual upstream footage/outgoing-frame
needs remain serial and require current eligible selections. The preparation owner
covers immutable prompt packs, source rehashing, one real host image envelope per
call and one substantive reviewer. Python never invokes/impersonates an agent host tool.

Current strict adapter limits remain: unencoded/remote native media fields,
`resume_job`, and ungoverned avatar generation/resume are refused. Avatar inspect/list
remains read-only. SchemaMedia/FalMedia encode canonical local image/mask paths;
raw filesystem paths in native fields and undeclared final-frame pins reject before
provider POST. No unsupported control is silently dropped. Inspect the actual tool
and visual-development owner for exact supported native bindings.

### Episode controls and first cut

`lib.episode_production_controls.record_episode_controls` retains creator-approved
`max_generations_per_shot`, `alternate_repair`, `access_fallback` and optional
`first_cut: {enabled: bool}` against exact approval evidence. Engine `first_cut`
absent means disabled; existing episodes retain their behavior. Two is a configurable
future Studio default, never a permanent engine ceiling. `generation_usage`,
`effective_controls` and `episode_production_status` report canonical state.
Submitted/accepted/terminal/uncertain original jobs count once across scopes;
proven never-submitted work, review, status, download and collection use no generation
slots. Amendments never reset usage. Prospective `restrict_episode_provider` stops
new submissions while preserving original collection, footage and history.

Opted-in first-cut mode assembles attributable playable candidates before elective
creative replacement. Usable cosmetic/story defects remain disclosed draft findings;
missing footage produces an honestly incomplete preview, not invented coverage.
Candidate inclusion is separate from strict selection/upstream eligibility. No
automatic `critical_review` creative reroll of existing footage is allowed in this
mode. Confirmed terminal no-output access fallback may use only an already-approved
compatible alternate within cumulative allowance and existing exact/account/billing
locks; uncertain originals require collection. Cosmetic or unknown AV findings never
create automatic repair authority.

`lib.production_draft` owns composition, current-cut bindings and first-cut acceptance;
acceptance preserves the disclosed export and does not certify or publish it.
`lib.production_repair_batches.record_creator_repair_batch`, `prepare_repair_item`,
`resume_repair_batch` and `repair_batch_status` own exact creator-selected batch items
bound to the current cut, original attempt, route, changes and retained evidence.
The creator may name multiple repairs, including an exact same-model item, without
fabricated critical severity. Existing route/model intent, scope, account/billing,
input and cumulative allowance gates still apply; dependencies do not auto-expand
the batch. MCP resume returns the exact original host handoff once; Python never
calls the connector. Studio's delivery owner documents its creator-facing commands.

### Retained reviews and planning changes

Explicit `audio_unavailable_draft` authority can permit provisional selection with
only required `speaker_source: unknown`; actual critical visual evidence must pass.
Final certification rejects provisional selections. Actual synchronized same-media
listening can append a monotonic successor through
`lib.production_review_successors.record_review_successor`; preserve frozen selection,
source/request/media hashes and unresolved visual defects. A master pass cannot waive
unknown selected-clip evidence.

Approved static-board repairs use `lib.production_continuity` shot digests and linked
`derive_carried_scope`/`append_carried_scope`; they do not reset quotas or hand-edit
approved digests. Existing-footage planning revisions can change only
`dominant_action`/`completed_end_state`, bound to immutable contracts, exact consent,
activation, retained footage/scopes and a named protected-story proof. Changed shots
need fresh revision-bound selection review. Other story/dialogue/cast/board/shot-order
changes remain outside that narrow bridge. Consult canonical continuity functions
and their focused integration tests for the actual record shapes.

## Reviewer Protocol

[reviewer](skills/meta/reviewer.md) owns semantic evidence and certification. Follow
current manifest focus/playbook constraints with one substantive current-byte verdict
per boundary. The parent checks bindings/completeness and reopens only a missing
predicate, changed binding or named dispute. Critical failed/missing/unknown evidence
blocks strict motion prerequisites or certification; review-round/budget limits do
not grant a pass. Suggestions/cosmetic findings proceed with honest warnings.

A rendered file or legacy final_review v1 pass is draft/diagnostic evidence.
`lib.production_review.certify_final` requires v2 current master hash/duration,
current story/contract/coverage/selections and complete synchronized audiovisual
viewing/listening with provenance. A completed compose without certification records
`metadata.release_status: draft`; certification is never publishing authority.
First-cut draft inclusion/acceptance does not relax this gate.

## Human Checkpoint Protocol

[checkpoint-protocol](skills/meta/checkpoint-protocol.md) owns manifest-gated approval.
`human_approval_default` is binding. A fresh artifact/review and exact current retained
policy may preauthorize only its named gated stage; otherwise write `awaiting_human`,
present summary/findings/cost and **end the turn**. Approval is per gate, never inferred
from an earlier generic go-ahead. Do not ask again for valid existing preauthorization.
Strict is the default; optional Auto-continue requires separately approved exact
routes/models/account/billing, protected locks, flex, caps and activation evidence
under the CLI/MCP authority owners. Unknown-cost OpenArt is a distinct explicitly
acknowledged no-enforceable-ceiling mode; exact-quote approval never migrates into it.

## Communication Protocol

Canonical JSON artifacts, manifests, schemas and checkpoints are the shared contract.
Use `schemas/artifacts/`, `schemas/checkpoints/`, `schemas/pipelines/` and `styles/`.
Write `in_progress` on entering a stage and update actual scene/asset progress during
long assets/compose work. Completed/awaiting-human checkpoints include canonical
artifacts; superseded checkpoints archive to project history. Fail schema violations
honestly rather than copying synthetic passing evidence.

Tools inherit `BaseTool`, use PascalCase class names without a `Tool` suffix and call
`.execute(params)` returning `ToolResult`, not `.run()`. Discover capabilities/providers
via the registry; selectors discover their providers automatically. Prefer skills
for ordinary tool use. Source inspection is appropriate for debugging/auditing a
mismatched contract, and useful lessons belong back in the existing owner.

## Creative choices when applicable

Concepts and opener variants help an open brief; honor a selected premise or exact
supplied opener without forcing another brainstorm. Continuation hooks apply to a
serialized brief. Music planning applies when the current brief or selected pipeline
requires/offers music: its proposal/asset directors own library/search/generation,
licensing and cost choices. Preserve an explicit no-added-music choice; do not force
a music menu, generated track or second master into every episode. Never silently
drop approved narration/music; output audio still needs honest review.

Choose style from current playbooks, with taste-direction for custom/hero work.
Hand-drawn doodles route to [Ink Theater](skills/creative/ink-theater.md): animation
for illustration/contraptions, character-animation for mocap acting; supplied drawings
use [animated-drawing](skills/creative/animated-drawing.md). Choose named mocap clips,
never hand-tune motion. Terminal-flow demos may use `synthetic-screen-recording`;
real UI/unpredictable capture follows screen-demo's director. Detailed 3D worlds
route to [3d-world-generation](skills/creative/3d-world-generation.md) and relevant
registry tools; requested production density cannot be replaced by blockout primitives.
These paths and provider vocabulary remain available through the skill index and
registry without compulsory reading on unrelated tasks.
