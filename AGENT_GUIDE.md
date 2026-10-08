# OpenMontage - Agent Guide

Start here. This is the complete operating guide and agent contract for OpenMontage.

For architecture, key files, and conventions see [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md).

## First Interaction — Onboarding

When the user's first message is vague, exploratory, or asks what you can do ("make me a video", "what can you do?", "help me create something", "I want to make content"), read the onboarding skill **before** doing anything else:

**Read:** `skills/meta/onboarding.md`

This skill teaches you to run discovery, classify the user's setup, present capabilities in plain language, and offer starter prompts tailored to their available tools. The goal: get the user from "curious" to "making a video" in under 60 seconds.

**Skip onboarding** when the user arrives with a specific, actionable request (e.g., "Make a 60-second explainer about black holes"). Go directly to Rule Zero.

## Reference Video Entry Point

When the user provides a **video URL or local video file as inspiration** — for example:

- "Can you make a video like this?"
- "I love this YouTube Short. Make me something similar."
- "Use this Reel as a reference."

— do **not** treat this as a generic web-search or prompt-writing request.

This is a first-class workflow in OpenMontage.

### Required behavior

1. **Read:** `skills/meta/video-reference-analyst.md`
2. **Run the reference analysis workflow** using the local analysis tools (`video_analyzer`, transcript extraction, scene detection, frame sampling)
3. **Produce a grounded summary** of what the reference is doing:
   - content
   - pacing
   - structure
   - style
   - what makes it work
4. **Then** run normal capability audit and pipeline selection
5. Present **2-3 differentiated concepts** for the user's version — not a carbon copy

### Important distinction

- **Reference-driven request:** "make me something like this" -> use `video-reference-analyst.md`
- **Source-footage request:** "edit this footage" / "cut this into clips" -> use `source_media_review` and the appropriate footage-led pipeline

If a model misses this distinction, it will often fall back to plain search + guesswork. That is incorrect for OpenMontage.

## Rule Zero — All Production Goes Through a Pipeline

**Every video production request MUST go through the pipeline system. No exceptions.**

When the user asks to make, create, produce, or generate any video content — a trailer, explainer, clip, animation, or any other video — the agent must:

1. **Identify the pipeline.** Match the request to one of the pipelines in `pipeline_defs/`. If unclear, ask the user.
2. **Read the pipeline manifest.** `pipeline_defs/<pipeline>.yaml` — know the stages, tools, and quality gates.
3. **Run preflight.** Discover available tools via the registry. Present the capability menu.
4. **Execute stage by stage.** For EACH stage, read the stage director skill (`skills/pipelines/<pipeline>/<stage>-director.md`) BEFORE doing any work in that stage.
5. **Read Layer 3 skills before calling tools.** Before using any tool with an `agent_skills` field, read the referenced skill in `.agents/skills/`. These contain provider-specific prompting guidance, parameter optimization, and quality techniques that dramatically improve output.

**Do NOT:**
- Write ad-hoc Python scripts to call tools directly
- Skip the pipeline and go straight to API calls
- Generate assets without reading the stage director skill first
- Use a tool without checking its Layer 3 skill for prompting guidance
- Bypass preflight, checkpoints, or review

The intelligence is in the skills, not in improvised code. An agent that reads the director skills and Layer 3 knowledge will produce significantly better output than one that calls tools directly with generic prompts.

## What OpenMontage Is

OpenMontage is an instruction-driven video production system. The AI agent IS the intelligence — it reads instructions (pipeline manifests + stage director skills + meta skills) and drives the pipeline using tools.

```
Agent reads pipeline manifest (YAML) -> reads stage director skill (MD)
-> uses tools (Python BaseTool subclasses) -> self-reviews (meta skill)
-> checkpoints (Python utility) -> presents to human for approval
```

**Python = tools, persistence, and objective governance.** Agents choose story, directing, and semantic review outcomes. Python enforces declared prerequisites, approved scope, hashes, dependencies, review completeness, and final eligibility; it does not infer character identity or understand story pixels.

Core loop:

1. Select a pipeline.
2. Run preflight.
3. Discover real tools from the registry.
4. Present the user with concepts, tool plan, production plan, and cost.
5. Execute stage by stage with checkpoints.

## Decision Communication Contract

For any meaningful production decision, the agent must communicate the decision before acting. The user should never have to infer which provider, model, or render path was chosen after the fact.

### Announce Before Execution

Before any paid or consequential generation call, state:

- the exact tool name,
- the provider,
- the model or provider variant,
- the reason it was chosen,
- whether it is a sample or a batch run.

For Auto-continue, announce before **every** generation: “Shot <id>; route
<provider / exact model or CLI-managed media>; reason <eligible route>; phase
<first pass / repair and replacement IDs>; changes <explicit approved flex or
none>; budget <OpenArt exact-quote credits against ceiling / OpenArt unknown
cost with no enforceable credit ceiling under its separately approved variant /
Grok subscription quota unknown>; remaining caps <total / per-shot / repair>;
approval <policy SHA and activation decision>.” Log the route and any compromise
before execution. Never describe unknown-cost OpenArt as free, zero-cost,
affordable, or bounded by the attempt caps.
Never infer Grok's media backend from its reported agent model.

### Ask Before Major Changes

The agent must ask the user before changing any major production choice, including:

- switching provider,
- switching model family or provider variant,
- switching from video-led to still-led treatment,
- switching composition engine when that changes the output character,
- dropping narration, music, or other approved creative elements,
- changing from sample mode to batch mode.

The sole bounded exception is a currently valid retained Auto-continue policy:
its exact approved routes/models and declared flex may proceed after announcement
and decision logging without another prompt. Recompute authority before each
attempt. Anything outside that policy still requires approval; publishing,
benchmarks, purchases and top-ups are excluded. A generic “go ahead” is not policy
authority.

Minor prompt refinements inside an already approved provider/model path do not require separate approval unless they materially change the creative direction.

### Re-log Changed Decisions (Binding)

The `decision_log` is the board's Decisions rail and the run's audit trail. It is **append-only history, not a scratchpad.** When a choice you already logged changes mid-run — the user swaps the voice, you switch provider/model/runtime/music, or a fallback overrides an earlier pick — you MUST **append a new `decision_log` entry** for the new choice, reusing the **same `category` AND the same `subject`** (e.g. `category: "voice_selection"`, `subject: "Narration TTS provider"`), with the superseded option moved into `options_considered` and `rejected_because` noting it was changed.

Editing only a downstream artifact (the `asset_manifest`, a prop) while leaving the old decision in the log is a defect: the board keeps showing the stale choice (e.g. `voice → openai_onyx` after the user moved to Chirp3). The board identifies a decision by its **(category, subject) pair** and renders the latest entry for that pair as current (tagged "revised") — so the fix is to append the new entry with an identical `subject`, never to silently mutate the old one or reword the subject (a reworded subject reads as a different decision and both will show). Keeping distinct decisions in one category (e.g. TTS vs image `provider_selection`) is exactly why the pair, not the category alone, is the key. This applies at every stage, not just `idea`.

### Present Both Composition Runtimes (HARD RULE)

When both Remotion and HyperFrames are available on the machine (check `video_compose.get_info()["render_engines"]`), the agent **MUST present both options to the user** before locking `render_runtime` at the proposal stage. The agent MAY recommend one with rationale — but silently picking a "default" is forbidden even when the pipeline manifest or a director skill suggests one.

The presentation MUST include, for each runtime:

1. A one-sentence plain-language description of what it is best at for **this specific brief**.
2. A one-sentence honest tradeoff (why it might not be the right pick here).
3. The agent's recommendation and the reason, tied to the brief's delivery_promise and visual approach.

Then wait for explicit user approval before advancing. Record the full shortlist — BOTH runtimes plus any "ffmpeg" option that applies — as `options_considered` in the `render_runtime_selection` decision logged in `decision_log`. A decision log entry with only one runtime considered when both were available is a CRITICAL reviewer finding.

Exception: if only one runtime is available on the machine, the agent proceeds with it but MUST say so explicitly ("HyperFrames isn't installed on this machine; I'm proceeding with Remotion. Install HyperFrames if you want the alternative."). The `render_runtime_selection` decision still records the unavailable option as `rejected_because: "runtime not available on this machine"`.

This rule applies to every pipeline that invokes `video_compose` — not just Wave 1. A pipeline's director skill may recommend a runtime, but that recommendation is input to the conversation with the user, not a decision.

### Composition Authoring Mode — Templated vs Atelier

Orthogonal to *runtime* is *authoring mode*: **how** the composition is built. Present it as its own proposal decision and log it in `decision_log` (`category: "composition_mode"`).

- **Templated** — assemble the stock `cut.type` scene-types (`text_card`, `stat_card`, `bar_chart`, …) into the `Explainer`/`CinematicRenderer` compositions. Fast, cheap, reliable — and the reason most videos look alike. Right for batch output, localization variants, quick drafts, and low-stakes internal clips.
- **Atelier** — **hand-author the composition from scratch**: bespoke scenes, a one-off theme, and motion written for this piece, rendered via `composition_mode: "atelier"` (see `video_compose` → `_render_via_atelier`). No reusable creative components; a fresh visual language every time.

**Default to atelier for hero work** — marketing, launches, brand pieces, any single-deliverable explainer that must impress. The deciding rule: *reuse engine knowledge, never creative components.* In atelier mode the stock scene-type catalog, `hyperframes-registry` blocks, fixtures, and finished components are **off-limits** — they are frozen looks that reintroduce sameness. Before building, route through **`skills/meta/taste-direction.md`** to set the design read and taste dials, then **`skills/meta/bespoke-composition.md`**, which sequences: art direction (`visual-style`) → motion principles (Disney 12 via `framer-motion`/`lottie-bodymovin`) → engine mechanics (`remotion-best-practices` + the stock components read *only as a mechanics codex*) → render via the atelier path. Close with a **distinctness review**: *could this be any other product's video? does it reuse a look I've made before?* — the inverse of "does it match the reference." Atelier costs more tokens and iteration than templated; say so at proposal so the user opts in knowingly.

### Escalate Blockers Explicitly

When a blocker occurs, the agent must surface it immediately using this structure:

1. What was attempted
2. What failed
3. Whether the issue is auth, provider access, tool bug, or prompt/design quality
4. What options exist next
5. Which option the agent recommends, with reasoning

Do not continue with a substitute path until the user approves.

### Recommendation Style

When asking the user to choose, do not just list options. The agent should:

- provide the shortlist,
- explain the tradeoffs briefly,
- recommend one option,
- wait for approval before proceeding.

### No Unilateral Substitutions

If the approved path is blocked, the agent may investigate and prepare alternatives, but may not execute those alternatives without user approval.

A valid retained Auto-continue policy can preauthorize only its named eligible
routes/models and explicit flex, after fresh preparation and announcement.
Blocked locks, missing native controls, exhausted caps or uncertain original
jobs stop continuation; they never authorize a substitute. All other alternatives
still need user approval.

This applies especially to:

- provider swaps,
- model swaps,
- fallback tools,
- prompt-only substitutes for reference-driven generation,
- still-image animatics in place of true motion.

## Orchestrator

The agent itself orchestrates the production state machine:

`research -> proposal -> script -> scene_plan -> assets -> edit -> compose`

The agent:

1. Reads the pipeline manifest (`pipeline_defs/*.yaml`) to know the process
2. Calls `checkpoint.get_next_stage()` to find where to resume
3. Reads the stage's director skill (`skills/pipelines/<pipeline>/<stage>-director.md`) to know HOW
4. Uses tools (`tools/`) for concrete capabilities
5. Self-reviews using the reviewer meta skill (`skills/meta/reviewer.md`)
6. Checkpoints via the checkpoint protocol (`skills/meta/checkpoint-protocol.md`)
7. Presents to human for approval when `human_approval_default: true`

Infrastructure files:

- `lib/checkpoint.py` — read/write checkpoints, stage validation
- `tools/cost_tracker.py` — budget governance
- `lib/pipeline_loader.py` — manifest loading and helpers

## Project Directory Convention

Every production run creates a project workspace under `projects/`. This directory is gitignored — all generated assets are regenerable.

```
projects/<project-name>/
├── artifacts/          # JSON artifacts from each stage (research_brief, script, scene_plan, etc.)
├── assets/
│   ├── images/         # Generated images (PNG)
│   ├── video/          # Generated video clips (MP4)
│   ├── audio/          # Narration segments + final mix (MP3/WAV)
│   ├── music/          # Background music track (MP3)
│   └── subtitles.srt   # Generated subtitles
└── renders/
    └── final.mp4       # Final rendered video (the deliverable)
```

**Naming convention**: Use kebab-case derived from the video title (e.g., `hidden-math-of-nature`, `how-music-rewires-brain`).

At pipeline initialization, before any stage runs:

1. **Initialize the workspace**: `python -c "from lib.checkpoint import init_project; init_project('<project-id>', title='<Title>', pipeline_type='<pipeline>')"` — creates the layout above and writes `project.json` (the marker the Backlot board reads).
2. **Open the board**: run `python -m backlot open <project-id>`. This starts the Backlot server if needed and opens the user's browser at the project's live board. If the command fails, continue the production — the board is an observer, never a blocker. This is the agent's ONLY board duty; the board derives everything else from disk.

All tools and agents must write outputs to these paths — **always pass an explicit `output_path` under `projects/<project-id>/`**. Assets written to the repo root, cwd, or temp dirs are invisible to the user's board and violate the workspace contract.

**This applies to atelier and HyperFrames-skill runs too**: hand-authored compositions still write the canonical artifacts they have (script or beats-plan, scene_plan-equivalent, asset manifest) plus checkpoints into `projects/<project-id>/`. The board is runtime-agnostic; only runs that skip the artifacts get a degraded board.

## Music Library

Users can place royalty-free music tracks in `music_library/` (gitignored). The asset director will check this folder before falling back to API-based music generation.

```
music_library/
├── ambient_track.mp3
├── cinematic_epic.mp3
└── ...
```

If the folder has tracks, the proposal and asset stages should present them as options alongside generated music. See the proposal-director and asset-director skills for details.

## Available Pipelines

| Pipeline | Best For | Stability |
|----------|----------|-----------|
| `animated-explainer` | Topic to fully generated explainer | production |
| `talking-head` | Footage-led speaker videos | beta |
| `screen-demo` | Screen recordings and walkthroughs | production |
| `clip-factory` | Many clips from one long source | beta |
| `podcast-repurpose` | Podcast highlights and derivatives | beta |
| `cinematic` | Trailer, teaser, and mood-led edits | production |
| `animation` | Motion-graphics and animation-first videos | production |
| `character-animation` | Local rigged cartoon characters and reusable character acting | beta |
| `hybrid` | Source footage plus support visuals | production |
| `avatar-spokesperson` | Presenter-led avatar or lip-sync videos | production |
| `localization-dub` | Subtitle, dub, and translated variants | beta |
| `framework-smoke` | Test: minimal 2-stage smoke test | test |
| `provider-qualification` | One separately approved OpenArt transport sample | beta |

> **Beta pipelines** have not been fully audited. They work, but expect rough edges. Mention this when the user selects one.

## Mandatory Preflight

Do this before any creative work. **Use `provider_menu_summary()` first — it's the human-ready rollup.** The raw `support_envelope()` dump is a firehose (megabytes of JSON on a well-configured machine); pasting it into chat will bury the user.

```bash
python -c "
from tools.tool_registry import registry
import json
registry.discover()
print(json.dumps(registry.provider_menu_summary(), indent=2))
"
```

The summary returns these fields the agent should translate into plain language:

- `composition_runtimes` — booleans for `ffmpeg`, `remotion`, `hyperframes`. This is the source of truth for the "Present Both Composition Runtimes (HARD RULE)" check.
- `capabilities[]` — one entry per capability family with `configured / total` counts and provider lists. Ready-made for the "N of M configured" menu.
- `setup_offers[]` — unavailable tools whose install is a 1-minute env-var fix. Lead with these when offering upgrades.
- `runtime_warnings[]` — specific signals like "hyperframes: npm package not resolvable". Surface these to the user verbatim — they're the kind of silent-failure bugs that break the governance contract.
- `pinned_final_frame_routes[]` — per-route ending-frame support, exact supported models, credential availability, and billing separation. For a required ending image, use the binding workflow in `skills/creative/visual-development.md`; an available CLI route does not imply endpoint support or authorize a paid API route.

Then, for deeper inspection (only when the summary isn't enough):

```bash
# Full menu — grouped available/unavailable per capability.
python -c "from tools.tool_registry import registry; import json; registry.discover(); print(json.dumps(registry.provider_menu(), indent=2))"

# Raw envelope — every tool's full contract. Slow/firehose; use for debugging only.
python -c "from tools.tool_registry import registry; import json; registry.discover(); print(json.dumps(registry.support_envelope(), indent=2))"
```

Then:

1. Read the selected manifest in `pipeline_defs/`.
2. Check every `required_tools` entry against the registry.
3. Check `fallback_tools` for unavailable tools.
4. Report one of: `passed`, `degraded`, or `blocked`.
5. Do not start production until the user understands the real capability envelope.

### Provider Menu (Mandatory at Preflight)

Already fetched via `provider_menu_summary()` above. Read that output and **present it to the user as a capability menu**, not as a flat tool list. Use `provider_menu()` directly only when you need the per-tool detail the summary collapses.

**How to present:**

```
YOUR CAPABILITIES

  Video Generation:  0/13 configured
  Image Generation:  1/7 configured
  Text-to-Speech:    1/3 configured
  Music Generation:  1/1 configured
  Composition:       3/3 configured (FFmpeg, video_stitch, video_trimmer)

  You can produce videos now with images + TTS + FFmpeg.
  Quick upgrades available — see below.
```

For EACH capability with unavailable providers, read the `install_instructions` field from the menu output and present setup options grouped by effort:

```
QUICK SETUP OPTIONS (1-minute each — set an env var in .env)

  Video Generation (0/13 -> unlock the biggest upgrade):
    Each unavailable provider lists its own install_instructions.
    Read them from the provider_menu output and present grouped by env var.
    Example: if 3 tools need FAL_KEY, group them: "FAL_KEY unlocks 3 providers"

  Image Generation (1/7 -> more style options):
    Same pattern — read install_instructions from each unavailable tool.

  Text-to-Speech (1/3):
    Same pattern.

LOCAL OPTIONS (free, needs hardware):
  Tools with runtime=LOCAL or runtime=LOCAL_GPU — read from the menu.

Already Available:
  List what's working. The user should feel good about what they have.
```

**Rules:**
- Do NOT hardcode provider names, API key names, or setup URLs in your prompts.
  Read them from the registry's `install_instructions` field on each tool.
- Always show the ratio: "X of Y configured" — this makes breadth visible.
- Group by capability, not by individual tool.
- Show what they CAN do now, then what they COULD unlock.
- If the user declines setup, proceed with the best available path — no nagging.
- If a tool shares an env var with others, group them (read from `dependencies` field).

### CLI Video Routes (Grok CLI and OpenArt CLI)

`provider_menu_summary()["qualified_cli_video_routes"]` lists the two
subscription CLI video routes. Grok comes first, then OpenArt. For OpenArt,
use current catalog/form evidence, current authenticated account readiness and
exact native CLI transport support to determine production eligibility.
`observed_candidates` lists catalog-advertised model/mode pairs with form and
transport observations where parsing succeeds; it is schema evidence, not a
quality claim. A prior paid/live result or quality review is optional diagnostic
evidence, not a production gate. The October 7 retained catalog has 17 video
model IDs and 45 mode pairs. Present the routes next to the other video
providers, using only observed fields:

- **Models.** Use exact catalog model IDs/modes with valid forms and supported
  native bindings. `candidate` means raw observation; `supported` means native
  schema and account readiness; `qualified` adds optional original-result
  evidence and grants no extra production authority. `not_live` is unavailable.
  Do not invent model IDs,
  audio, end-frame pins, rates or defaults.
- **Controls and limitations.** Read `controls` and `limitations`. When an
  exact native control is false (end-frame pin, explicit audio toggle, multiple
  references), say that control is unavailable on that route. Keep native
  audio output, an exposed on/off switch, audio-reference support and observed
  dialogue fidelity separate. A missing audio form field means no exposed
  switch, not proof of silent output. Use the canonical route's documented
  default-output evidence where available; keep unobserved output and dialogue
  fidelity explicitly unverified. A required native frame pin without a
  matching control fails before any credit reservation. Never paper over it with a prompt rewrite.
  OpenArt `native_capabilities` keeps provider form controls separate from the
  exact local CLI transport. The October 7 official CLI 0.1.1 surface supports
  prompt, duration, aspect ratio, resolution and one `--image` start frame for
  image-to-video. It has no generic native parameter flag, selectable
  element-to-video mode, end-frame pin, multi-reference, reference-video/audio
  or native audio control. Unsupported form properties stay visible and fail
  closed before reservation. Canonical asset inputs use
  `input_assets: [{role, source_path, source_sha256, upload_id}]`; legacy flat
  aliases normalize to this shape and conflicts fail. Only first-frame input
  currently binds at the local role layer. A guarded CLI preview showed
  `params.startFrame` with `label`, `type` and `url`, but no `id`; H3 Turbo's
  captured form requires all four, so its helper-level binding is not a valid
  exact native request. The official MCP integration is installed and
  authenticated; inspected schemas cover 17 video models and 45 modes. H3
  Turbo image-to-video requires `startFrame.type`, `id`, `url` and `label`,
  with optional `endFrame`, and MCP `generate_video` accepts full params.
  The MCP native route is available subject to exact form, account, request,
  and governance checks. CLI profiles and approvals do not migrate. A required
  resolution stays unqualified until an observed form and exact preview carry
  it. Grok reports no video models (`model_policy:
  cli_managed_media_unreported`), and its agent model is never a video model.
  Approved dialogue stays in the prompt even on a route with no native audio;
  disclose the missing guarantee.
- **Billing.** Report the selected OpenArt billing mode. Exact-quote mode keeps
  its current quote and ceiling requirements. The explicit unknown-cost mode
  acknowledges that no enforceable credit ceiling exists; do not call its
  charge zero, affordable or a USD estimate. Show Grok CLI separately as
  subscription or usage of unknown cost. Never add them together or convert
  either to dollars.
- **State.** Report `account_stages`, `dispatch_readiness`, pending jobs, credit
  holds, quarantine and `errors` as they appear. A qualified model does not make
  the account ready: any OpenArt blocker in `dispatch_readiness` stops dispatch,
  and every dispatch needs a fresh account refresh. Exact-quote dispatch needs
  its current quote; unknown-cost dispatch needs its separate retained
  authorization and explicit no-enforceable-ceiling acknowledgement.
  Unresolved job acceptance (pending, submitting or uncertain slot) blocks.
  An unknown-billing hold on a terminal slot does not block and does not
  invalidate approved footage. In priced mode, economics stay incomplete and
  require a remaining allowance plus a fresh quote; unknown-cost mode has no
  allowance or quote arithmetic. Account status is as last observed;
  never claim live auth without a fresh probe. The OpenArt menu part runs no
  OpenArt CLI; Grok status runs its existing read-only `--version`/`--help` probe.
- **Route suggestions.** Give a per-scene suggestion when the requested
  controls, current route support, and known cost information fit. State the
  observed empirical-result status and the basis for the suggestion. Do not
  present a heuristic as a benchmark, promise a clean first take, or silently
  switch providers.

The October 7 optional qualification record documents one collected, reference-free
PixVerse V6 text-to-video result at 1s, 16:9, 720p. It verifies transport and
the collected bytes only; creative quality was not reviewed. The Free account observation applies to that test and
does not establish general entitlement or affordability. H3 models and all
image-to-video output remain untested. Native references, audio and output
quality must follow exact form and transport support; a schema does not claim
output quality. A reviewed board ending target is
the shot's intended final composition; it does not itself demand a native frame
pin. A declared hard `pinned_final_frame`/handoff requirement does demand an
exact matching native control and approval, so CLI 0.1.1 cannot satisfy it.
Current account readiness and exact native route support must still come from
fresh retained evidence and the provider menu. Report empirical result status
separately and accurately. See
`docs/implementation/2026-10-07-openart-live-720p-test.md` and
`docs/implementation/2026-10-07-openart-model-controls.md`.

A boarded episode may mark an independent, text-only cutaway with
`shot.reference_mode: "reference_free"` for OpenArt text-to-video. The shot
cannot have local cast, visible speakers, dialogue, assets or upstream sources,
and any required frame pin or manifest reference/handoff blocks it. Existing
project boards, payoff and cast evidence remain required. This permits a
separate referenced Grok shot beside the cutaway; it does not make OpenArt a
cast/dialogue/reference replacement or fallback for that shot. See
`docs/OPENART_CLI.md#reference-free-and-provider-qualification-paths`.

### Preserve video model intent

If the user names an exact video model, preference, or goal such as value or
cleanliness, preserve that request as `model_selection_intent`. Show and approve
the exact provider/model pool and its account, billing mode, production stages
and attempt limits; ranking cannot expand that pool. `exact` never substitutes.
`prefer` can choose an already-approved eligible alternative while planning;
`auto` ranks only within the approved pool. Use the shared canonical chooser
(`lib.video_model_selection.choose_video_route`) through the selector or route
planner, rather than separate director rankings. It orders compiled eligible
requests without adding controls, changing the request or granting authority;
scene guidance is suggestive evidence, not a benchmark or ordering override.
During execution, a semantic review informs an explicit producer repair
decision only after the original attempt is terminal and the review names a
critical defect the proposed remedy can address. Record defect evidence,
failed predicates, remedy (`prompt_staging`, `model_change` or `edit`), exact
selected route and actual request delta. Validate the decision through the
canonical repair helper and existing governance path; never blindly select
the next candidate. An edit handoff retains failed predicates until the edited
bytes pass their required review and does not itself authorize generation.
For a model change, the alternate must already be in that shot's approved pool,
and the intent must permit it (`exact`
never changes), and the repair needs a fresh governed scope or valid retained
Auto-continue authority, with cumulative caps, continuity locks and all other
request checks intact. This is a reviewed repair decision, not an automatic
retry. Transport, authentication, quota and uncertain-job errors require
reconciliation of the original attempt; they do not grant new route authority.
`best_value` requires comparable request-applicable costs;
OpenArt CLI and Grok CLI costs are unknown and cannot be treated as zero or
compared as a price. `balanced` and `max_clean` use a control-fit/transport
heuristic, not a visual-quality or first-attempt benchmark. Preserve intent and
the selected exact request through the usual preparation and scope approval;
ranking grants no dispatch authority. See `docs/VIDEO_MODEL_SELECTION.md`.

An unknown-cost OpenArt Auto-continue policy is a new, separately approved
variant, with `billing: "unknown_cost_no_ceiling"`,
`exposure_acknowledgement: "no_enforceable_credit_ceiling"`, account binding,
exact model/mode routes and the approved attempt caps. It never migrates an
exact-quote policy. Do not activate it based on implementation approval alone.
Unknown cost means neither zero nor a USD estimate, and it does not establish
affordability.

Ask the user for creative choices (shot, duration, aspect ratio, reference image),
not for job commands. Using a route requires an explicit singleton pin
(`preferred_provider` plus `allowed_providers` set to that single provider)
inside a strict project. Approved settings pass through unchanged. Read
`docs/OPENART_CLI.md` and the `openart-cli` skill before any OpenArt step.
Strict approval is the default; a strict original scope may preapprove a bounded
batch of exact attempts. Each attempt launches once; status, collect and resolve
repeat safely on the original attempt. Optional Auto-continue requires a retained
user-approved policy with an active decision-log activation. Derive exact
one-attempt scopes through `lib.production_autonomy.derive_scope`; dispatch
rechecks retained planning, explicit flex, implicit cast/dialogue/story locks,
native controls, named preparation, caps and normal credit authorization.
Missing, conflicting, changed or revoked authority stops continuation. Unknown
original jobs are never resubmitted. Grok subscription quota stays unknown; no
paid Grok API, purchases or top-ups are authorized. Read `docs/OPENART_CLI.md`.

### Setup Offer Protocol

When tools are `UNAVAILABLE` but can be fixed with simple configuration, **offer the user setup help instead of silently working around the limitation.** Many tools are one env var away from working.

| Fix Complexity | Action |
|----------------|--------|
| **1-minute fix** (env var) | Offer to help configure now — read `install_instructions` from the tool |
| **5-minute fix** (install) | Explain what to install and why — read `install_instructions` from the tool |
| **Complex fix** (GPU, model download) | Note the limitation, explain what it would unlock, move on |

**Rules:**
- Always tell the user what they're missing AND what they'd gain
- Show the cost difference (free local vs. paid API)
- If the user declines setup, proceed with the best available path — no nagging
- Group related fixes (tools sharing the same env var dependency)

### Composition Runtimes (Inside video_compose)

`video_compose` has **three** render engines / runtimes. They are parallel, not ranked — the choice is made at proposal and locked in `edit_decisions.render_runtime`. Check which are available:

```bash
python -c "
from tools.tool_registry import registry
registry.discover()
info = registry._tools['video_compose'].get_info()
print('Render engines:', info.get('render_engines'))
print('Remotion note:', info.get('remotion_note'))
print('HyperFrames note:', info.get('hyperframes_note'))
"
```

| Engine | Used For | Requires |
|--------|----------|----------|
| **FFmpeg** | Video-only cuts, concat, trim, subtitle burn | `ffmpeg` binary (always available) |
| **Remotion** | React-based composition: still images → animated video, text cards, stat cards, charts, callouts, comparisons, transitions with spring physics, word-level caption burn, TalkingHead avatar | Node.js (`npx`) + `remotion-composer/` + `node_modules` |
| **HyperFrames** | HTML/CSS/GSAP composition: kinetic typography, product promos, launch reels, website-to-video, registry-block-driven scenes, SVG character rigs | Node.js ≥ 22 + FFmpeg + `npx` (consumed via `npx hyperframes`) |

`render_runtime` is **locked at proposal** (`proposal_packet.production_plan.render_runtime`) and **carried through edit_decisions unchanged**. `video_compose` routes based on this field; silent runtime swaps are forbidden. If the chosen runtime becomes unavailable at compose time, surface a structured blocker per "Escalate Blockers Explicitly" above. See `skills/core/hyperframes.md` for the Remotion-vs-HyperFrames decision matrix.

### Critical Rule: Motion-Required Requests

For any request where the deliverable inherently depends on motion rather than static coverage, treat motion as a hard requirement. Examples:

- sci-fi trailers,
- cinematic teasers built from generated clips,
- hype edits,
- avatar or agent videos,
- any brief whose promise depends on moving shots rather than still frames.

For these requests:

- The `render_runtime` chosen at proposal (Remotion, HyperFrames, or FFmpeg) must be confirmed available up front if the planned visual treatment depends on it.
- Still-image fallback is forbidden. Do not quietly convert the job into a Ken Burns teaser, animatic, or slide-based video.
- FFmpeg-only fallback is forbidden when it changes the approved deliverable from motion-led video to still-led video.
- **Silent runtime swap is forbidden.** If `render_runtime="hyperframes"` was locked and HyperFrames is unavailable, do NOT route to Remotion instead. Surface the blocker, propose options, get user approval, log a `render_runtime_selection` decision — then proceed.
- Bubble critical issues immediately. If the chosen runtime is unavailable, fails to render, or provider clip generation fails in a way that blocks the approved treatment, stop and tell the user before proceeding.
- Do not spend more tokens or time on downgraded output unless the user explicitly approves the downgrade as an animatic or proof-of-concept.

**When Remotion is available**, the agent should design production plans around it:
- Explainer videos with `flat-motion-graphics` playbook -> Remotion animated scenes, not Ken Burns
- Data-driven videos -> Remotion stat cards and charts, not static image screenshots
- Any pipeline using still images -> Remotion spring animations, not FFmpeg pan-and-zoom
- **Screen demos of a CLI/terminal/install flow -> `TerminalScene` (synthetic screen recording), not OS-level capture.** See `.agents/skills/synthetic-screen-recording/SKILL.md`. Faster, deterministic, privacy-safe. Use real capture (`screen_recorder`, `cap_recorder`, `playwright-recording`) only when the demo is a real app UI or requires unpredictable live behavior.

### Remotion scene types available in `remotion-composer/`

See `remotion-composer/SCENE_TYPES.md` for the authoritative list and their cut schemas. Current scene types usable via `cut.type`:
`text_card`, `stat_card`, `callout`, `comparison`, `hero_title`, `terminal_scene`, `anime_scene`, `bar_chart`, `line_chart`, `pie_chart`, `kpi_grid`, `progress_bar`. Overlay types include `section_title`, `stat_reveal`, `hero_title`, `provider_chip`.

These stock scene-types are the **templated** path — fast and reliable, but they are why videos look alike. For **hero work, prefer atelier mode** (hand-authored composition) over this catalog; read those types as a *mechanics codex*, not a menu to assemble. See "Composition Authoring Mode" above and `skills/meta/bespoke-composition.md`.

**When Remotion is NOT available** and `render_runtime="remotion"` was NOT locked, `video_compose` may use FFmpeg Ken Burns motion on still images. This still works but produces less engaging visuals. Mention this tradeoff in the proposal. When `render_runtime="remotion"` IS locked and Remotion is unavailable, that's a blocker — escalate, don't silently swap.

When `render_runtime="hyperframes"` is locked and HyperFrames is unavailable (Node < 22, missing `ffmpeg`/`npx`, or `hyperframes doctor` reports issues), that's also a blocker. Do not substitute Remotion or FFmpeg without user approval + a logged `render_runtime_selection` decision.

Routing is automatic — `video_compose` reads `edit_decisions.render_runtime` and dispatches to the matching engine (`_render_via_hyperframes`, `_remotion_render`, or `_render_via_ffmpeg`). But the **agent must know both Remotion and HyperFrames exist at proposal time** so it can design the visual approach intentionally. Don't default to Remotion for motion-graphics-heavy concepts that HTML/GSAP would express more naturally, and don't default to HyperFrames for briefs that reuse the existing React scene stack.

## Capability Discovery

OpenMontage uses two layers for capability choice:

- selector tools: capability-level routing such as `tts_selector` and `video_selector`
- provider tools: concrete tools discovered via the registry that call a specific backend

Always inspect the registry first:

```bash
python -c "from tools.tool_registry import registry; import json; registry.discover(); print(json.dumps(registry.capability_catalog(), indent=2))"
python -c "from tools.tool_registry import registry; import json; registry.discover(); print(json.dumps(registry.provider_catalog(), indent=2))"
```

For finalist tools inspect:

- `capability`
- `provider`
- `usage_location`
- `supports`
- `fallback_tools`
- `related_skills`

Do not rely on memory or old docs when the registry can answer it.

## Tool Families

**Do not maintain hardcoded tool lists.** Always query the registry at runtime:

```bash
# See all tools grouped by capability (TTS, video_generation, image_generation, etc.)
python -c "from tools.tool_registry import registry; import json; registry.discover(); print(json.dumps(registry.capability_catalog(), indent=2))"

# See all tools grouped by provider (elevenlabs, openai, ffmpeg, etc.)
python -c "from tools.tool_registry import registry; import json; registry.discover(); print(json.dumps(registry.provider_catalog(), indent=2))"
```

Key capability families to look for in the output:

- **tts** — Text-to-speech providers. Route via `tts_selector`.
- **video_generation** — Video generation providers (cloud, local GPU, stock). Route via `video_selector`.
- **image_generation** — Image generation providers (cloud, local GPU, stock). Route via `image_selector`.
- **music_generation** — Music and sound effect generation.
- **video_post** — Composition, stitching, trimming (FFmpeg-based, always local).
- **audio_processing** — Mixing, enhancement (FFmpeg-based, always local).
- **analysis** — Transcription, scene detection, frame sampling.
- **avatar** — Talking head and lip sync generation.
- **character_animation** — Local character specs, SVG rigs, pose libraries, action timelines, previews, and QA.
- **3d_world_generation** — Local semantic terrain, procedural biome scattering, explicit landmarks, diagnostic passes, and deterministic HyperFrames/Three.js camera fly-throughs. Route through `threejs_world` and read `skills/creative/3d-world-generation.md` plus `.agents/skills/threejs-world-generation/SKILL.md`.
- **3d_asset_acquisition** — Rights-safe GLTF/GLB catalogs for production-tier Three.js worlds. Route through `threejs_asset_catalog`; never substitute blockout primitives for a requested detailed or reference-grade environment.
- **3d_asset_generation** — Unique textured/PBR meshes from text or concept images. Route text-described hero assets through `atlas_3d`, image-conditioned assets and regional object extraction through `fal_3d`, and read `.agents/skills/3d-asset-generation/SKILL.md`. Announce provider/model/unit cost before every first paid call and sample before batching.
- **3d_world_rendering** — Production assembly, terrain, lighting, materials, camera, and image-sequence rendering in Blender. Route through `blender_world`; use Three.js for interactive/blockout review, not as a substitute for Blender when reference-grade scene density is requested.
- **enhancement** — Upscale, background removal, face enhance, color grading.

Each tool in the registry declares `best_for`, `install_instructions`, `runtime` (LOCAL, API, LOCAL_GPU, HYBRID), and `status`. Read these fields — do not assume tool strengths from memory.

### Tool Class Naming Convention

All tool classes use **PascalCase without a "Tool" suffix**. When importing tools in Python:

| Module | Class Name | NOT |
|--------|-----------|-----|
| `tools.audio.music_gen` | `MusicGen` | ~~MusicGenTool~~ |
| `tools.video.video_compose` | `VideoCompose` | ~~VideoComposeTool~~ |
| `tools.audio.audio_mixer` | `AudioMixer` | ~~AudioMixerTool~~ |
| `tools.tts.elevenlabs_tts` | `ElevenLabsTTS` | ~~ElevenLabsTTSTool~~ |
| `tools.analysis.transcriber` | `Transcriber` | ~~TranscriberTool~~ |
| `tools.subtitle.subtitle_gen` | `SubtitleGen` | ~~SubtitleGenTool~~ |

When in doubt, check: `grep "^class " tools/<path>.py`

All tools call via `.execute(params_dict)` (returns `ToolResult` with `.success`, `.data`, `.error`), NOT `.run()`.

### Selector Pattern

Three selector tools abstract multi-provider capabilities. **Selectors auto-discover providers from the registry.** Adding a new provider tool automatically makes it available through the selector — no selector code changes needed.

| Selector | Routes to | How it discovers |
|----------|-----------|-----------------|
| `tts_selector` | All tools with `capability="tts"` (ElevenLabs, Google TTS, OpenAI, Piper, Azure, fish.audio) | `registry.get_by_capability("tts")` |
| `image_selector` | All tools with `capability="image_generation"` (FLUX, Google Imagen, GPT Image, Recraft, etc.) | `registry.get_by_capability("image_generation")` |
| `video_selector` | All tools with `capability="video_generation"` | `registry.get_by_capability("video_generation")` |

Selectors route based on: user preference > availability > discovery order. They adapt input schemas between providers transparently.

## User-Facing Planning Protocol

Before committing to execution, present:

1. `4-5` concept directions when the brief is still open.
2. Recommended pipeline.
3. Recommended tool path.
4. Alternative tool paths that are actually available.
5. Cost estimate and quality tradeoffs.
6. **Music plan** — mandatory for every pipeline that has audio. See below.
7. Production plan by stage.
8. Approval gate before asset generation.

If a user prefers a specific vendor and that tool is available, surface it directly. Do not hide provider choice.

### Music Plan (Mandatory)

Music is a critical part of any video. **Surface the music situation to the user at proposal/idea time** — do not silently defer it to the asset stage where a failure becomes expensive.

Check music availability in this order and present the options:

1. **User music library:** Check `registry.get_by_capability("music_library")` and inspect `music_library/`. If tracks exist, list durations and let the user pick one.
2. **Royalty-free search:** Check `registry.get_by_capability("music_search")` for configured search/download tools. Report licensing constraints and whether a key is required.
3. **Music generation APIs:** Check `registry.get_by_capability("music_generation")`. Report status, quota, cost, and quality tradeoffs honestly.
4. **Bring your own:** Note that the user can provide a track (for example from YouTube Audio Library or Jamendo) through the `music_library/` drop path.

**Always present the user with explicit choices:**
- Use a track from their library (which one?)
- Provide a different track (drop it in `music_library/`)
- Generate one via API (if available — name the provider and cost)
- Proceed without music

**If no music source is available:** Tell the user explicitly. Do NOT let this surface as a surprise at the asset stage.

Record the music decision in the proposal/brief artifact so the asset director knows what to do.

## Pipeline Asset Expectations

Each pipeline manifest's `tools_available` field declares what tools a stage can use. Use selectors for multi-provider capabilities — the selector handles routing to whatever is available. Read the pipeline manifest for the authoritative list per stage.

## Stage Agents

Each stage produces one canonical artifact that becomes the contract for the next stage. The stage director skill teaches the agent HOW to produce it.

| Stage | Director Skill | Canonical output | Core quality bar |
|------|---------------|------------------|------------------|
| `idea` | `*-director.md` | `brief` | Clear hook, target platform, duration, tone, and user intent |
| `script` | `*-director.md` | `script` | Structured sections, valid timing, coherent narration |
| `scene_plan` | `*-director.md` | `scene_plan` | Ordered scenes, timings, asset requirements |
| `assets` | `*-director.md` | `asset_manifest` | Provenance, paths, model/tool metadata, scene linkage |
| `edit` | `*-director.md` | `edit_decisions` | Concrete cuts, overlays, subtitle/music decisions |
| `compose` | `*-director.md` | `render_report` | Output paths, encoding profile, verification notes |

Stage contract rules:

- A completed or awaiting-human checkpoint must include the stage's canonical artifact.
- Canonical artifacts must validate against the JSON schema in `schemas/artifacts/`.
- Non-canonical outputs such as media files belong in stage-specific directories.
- Tools should record seeds/model versions for reproducibility.

## Strict generative production

New Studio projects opt into strict governance through `init_project(..., governance="strict", story_revision=...)`; existing projects require deliberate `enroll_production_project`. Legacy diagnostic/editing workflows keep their existing behavior.

Before authoring a production package, read `schemas/artifacts/shot_contract.schema.json` and `tests/fixtures/first_pass/README.md`. The fixture `valid_shot_contract.json` shows the exact structure; its synthetic passing reviews are test data and must never be copied as real review evidence. Translate the portable shootable package into the schema: completed action → `endpoint_completion`; introduced late characters → `late_cast_ids`; ending speakers → `payoff_speaker_ids`; visible dialogue sources → `required_visible_speakers`. Keep unknown evidence unknown until the corresponding assets or footage are reviewed.

Store the contract at `artifacts/shot_contract.json`. The common dispatcher requires `production_scopes.json` plus request `governance: {scope_id, shot_id}`. Use `planned_request_digest` for a fixed request, or `planned_request_template` for explicitly planned `$upstream` bindings; the template freezes static asset hashes at approval time. Set an explicit duration matching the shot contract and a unique output path for every attempt. Constructing a scope or template does not authorize it: retain the user's exact approval and attempt allowance.

Use canonical registry tools and their normal `execute`/`dry_run` paths; Studio's entry defaults to a zero-call dry run. Preserve attempts, original-session reconciliation and reviewed selections through `lib.production_execution`. Selection and final certification require complete matching attempt provenance. Failures and uncertain results are evidence, never a retry allowance. See `tests/integration/test_first_pass_workflow.py` for an offline example of the entire control path, including a deliberate serial handoff.

When later-shot candidate preparation can overlap an eligible reviewed target, follow [`skills/meta/shot-preparation-overlap.md`](skills/meta/shot-preparation-overlap.md). The target still requires its complete unchanged schema-valid plan, target and shared reviews, and a fresh canonical dry-run immediately before the single production owner uses the exact authorized governed request. Candidate work stays unbound; promotion changes the canonical plan and can stale approvals and retained provenance. This sequencing guidance adds no generation or execution authority.

Current strict limits for the refreshed schema/fal/HeyGen adapters. Governance refuses these before attempt reservation: native remote or unencoded media fields (`image_uri`, `image_input`, `last_frame_uri`, `audio_uri`, `mask`, `web_url`, `link` and similar); provider-job `resume_job`, which cannot yet be reconciled as a strict attempt; and avatar generation/resume, which is not governed yet. None of these is ever treated as a new approved attempt. Avatar `list_looks`/`inspect_look` stay read-only and ungoverned. SchemaMedia/FalMedia encode only canonical local `image_path`/`image_paths`/`mask_path`. In every mode they reject at request build, which may come after reservation but always before any provider POST: raw filesystem paths in native media fields (for example `images`/`image`), and final-frame pins such as `last_image_path`/`last_frame` that the endpoint schema does not declare. Those pins are refused, never silently dropped, and the approved pin requirement stands: choose an explicitly approved route with documented first/last-frame support (see `pinned_final_frame`; qualified Grok pinned routes can be governed). Refreshed adapter native frame routes are not yet strict-compatible.

### Explicit draft continuation when audio review is unavailable

Only with explicit user authorization, a strict project may set `project.json` → `governance.draft_review` with `version: "1.0"`, `mode: "audio_unavailable_draft"`, matching `project_id` and `story_revision`, and `evidence: {path, sha256}` binding the retained approval. `draft_audio_policy_digest(project_dir)` validates this policy. A named selection review may then use `status: "provisional"` and `draft_policy_sha256` with **only** `speaker_source: unknown`; all required visual predicates must pass. Use `record_selection` normally. No missing predicate, failed speaker check, visual uncertainty, changed bytes, or incomplete attempt provenance is excused. Unknown audio remains unknown, and the policy neither enlarges approved generation scope nor authorizes corrective rerolls. Approved first-pass continuation and assembly may proceed toward a clearly labeled review draft.

Final certification remains strict and rejects provisional selections, even if a master review claims full passing AV checks. Current immutable downstream provenance also binds each upstream review hash: replacing a provisional review with a later passing review is not yet a supported automatic certification upgrade for dependent attempts. Preserve history; a future explicit monotonic review-upgrade mechanism is needed for that transition without regeneration. Record this limitation in the project's production notes; never rewrite frozen attempts or regenerate footage merely to evade it. See `tests/lib/test_draft_audio_review.py` for the offline boundary tests.

### Shot-scoped continuity after an approved static-board repair

An approved corrective repair that changes one shot's own start board changes the global `approval_plan_digest`. A retained attempt still needs its frozen scope to equal its frozen contract. If the global plan changed, the attempt stays eligible only when `lib.production_continuity.shot_planning_digest(contract, shot_id)` is unchanged. That digest covers global story, cast, payoff, shot order, and the shot's own semantics, duration, motion, static asset closure and identity bytes. Current upstream, byte and evidence checks still apply.

Unused shots continue through explicit linked successor scopes, including after another approved board repair. Build each with `derive_carried_scope` and append it with `append_carried_scope`. Each link binds the source and repair scope digests and the frozen source contract; validation replays preserved request, input and approval bytes through the original approval. Successors copy the exact requests and allowances, and reject shots consumed in any ancestor, overlapping branches, or asset deltas outside the repair targets' own boards. Preflight counts attempts across the linked scopes together, so a carry cannot reset quotas. Never rewrite an approved digest by hand. See `tests/integration/test_repair_continuity_workflow.py`.

### Planning revision for existing footage (choreography only)

When the human owner has authorized it, a planning revision can update a shot's `dominant_action` and `completed_end_state` so they describe footage that already exists. Every other contract difference fails closed. That includes dialogue, duration, boards, identity, payoff, story, shot order and other shots. New boards and generic semantic waivers are not supported.

Use `lib/production_continuity.append_planning_revision` to append the record to `production_revisions.json`. The record must bind:

- immutable prior and revised contract snapshots inside the project
- the consent document, account policy and activation evidence
- a story-protection proof bound to actual retained footage
- the exact retained scopes and their digests

The story-protection proof has this shape:

```json
{
  "version": "1.0",
  "project_id": "<project>",
  "story_revision": "<revision>",
  "prior_contract_sha256": "<prior snapshot hash>",
  "revised_contract_sha256": "<revised snapshot hash>",
  "target_shots": ["<unique changed shot id>"],
  "sources": [
    {"attempt_id": "<retained attempt>", "shot_id": "<target shot>", "output_sha256": "<actual footage hash>"}
  ],
  "review": {
    "review_id": "<review id>",
    "reviewer": "<reviewer>",
    "story_revision": "<revision>",
    "subject_sha256": "<digest of proof without review>",
    "status": "pass",
    "predicates": [
      {"name": "story_protected", "status": "pass", "severity": "critical", "evidence": "<review evidence>"}
    ]
  }
}
```

`target_shots` must be the unique exact set of changed shot IDs. Each source
must match an entry in the revision's retained attempts and name an actual
retained attempt whose output bytes match `output_sha256`; every changed target
shot needs at least one such source. The review must validate against
`schemas/artifacts/shot_contract.schema.json` `$defs.review`, bind the same
`story_revision`, and have `status: "pass"`. `subject_sha256` is the proof
digest with `review` excluded. The review's `reviewer` must equal
`protected_story_proof.reviewed_by` in the
revision record. The critical `story_protected` predicate must pass, and any
other critical predicate that is failed or unknown rejects the proof.

Compute `revision_sha256` as the digest of the record without that field. Duplicate IDs, or two revisions targeting the same plan, are rejected.

A changed shot needs a fresh selection review against the revised contract. The review must carry `planning_revision` set to `{revision_id, revision_sha256, contract_sha256}`. `selection_digest` includes this binding, so an old review cannot be reused. Downstream preflight rejects an upstream selection of a changed shot that lacks the binding. Unchanged shots continue only under the retained scopes, and quotas stay as they were. See `tests/integration/test_planning_revision_workflow.py`.

Revisions can be linked. A later revision's prior snapshot must have the same plan as an earlier revision's revised snapshot. Its retained attempts and scopes are cumulative: list every attempt and scope that must stay eligible, including those already retained by earlier revisions. A changed shot's selection binds the newest revision that changed that shot. Two revisions targeting the same plan, a cycle, or an unlinked revision are rejected.

## Reviewer Protocol

The reviewer is a meta skill (`skills/meta/reviewer.md`). Agents supply semantic judgments; the engine enforces required evidence and current bindings. Critical prerequisites block motion and final certification.

- Review after every stage execution, before checkpointing, with one substantive semantic verdict at each stage/shot boundary bound to current bytes and required predicates. Keep one persistent producer accountable; the parent checks bindings and completeness and opens only a missing predicate or named dispute, not a second complete pixel review by default.
- Load `review_focus` items from the pipeline manifest for the current stage.
- Review-round and budget limits never turn missing, unknown, unreviewed, or failed critical predicates into a pass. Stop with an honest draft and unresolved findings when the approved repair allowance is exhausted.
- Findings categorized: critical (must fix), suggestion (should fix), nitpick (nice-to-have).
- Critical findings -> diagnose, obtain applicable repair authorization, fix and re-review. Suggestions -> note and proceed. Inability to identify a fix does not downgrade a critical failure.
- A rendered file or legacy `final_review` v1 `status: pass` is diagnostic/draft evidence. Current final certification requires v2 with complete synchronized audiovisual viewing and listening, reviewer provenance, and passing transport, technical, visual, audio, and story dimensions.
- Use `lib.production_review.certify_final(project_dir, review)` for canonical final selection (`artifacts/final_review.json`). `write_checkpoint` applies the same current-byte gate to final claims and governed publish advancement. Unrelated legacy publishing remains explicitly `uncertified_legacy`. A completed compose checkpoint without certification explicitly records `metadata.release_status: draft`. Certification is not publication authorization.
- Bind final evidence to the actual master hash/duration, current story/contract, exact planned scene coverage, and current selected attempts/output/review hashes. Changed bytes or selections require fresh matching review; archived evidence remains inspectable.
- Check playbook `quality_rules` as constraints, not suggestions.

## Human Checkpoint Protocol

The checkpoint protocol meta skill (`skills/meta/checkpoint-protocol.md`) teaches the agent when to pause:

- Read `human_approval_default` from the pipeline manifest per stage. **The manifest value is binding** — never re-judge it. `lib/checkpoint.py` requires human approval or validated retained policy preauthorization for that exact gated stage. A fresh structured review must bind the current artifact, with no critical findings; policy SHA and activation decision form the approval basis.
- Typical gated stages: `idea`/`proposal`, `script`, `scene_plan`, **`assets`** (review the generated assets scene-by-scene — the Backlot board's filmstrip — before compose locks them in), and `publish` where the pipeline has one. Most pipelines auto-proceed on `edit` and `compose`, but not all (documentary-montage gates `edit`) — the manifest you loaded is the only authority.
- When approval is required and no valid retained policy preauthorizes this exact stage: write the checkpoint as `awaiting_human`, present artifact summary, review findings, and cost snapshot — then **END YOUR TURN**. Doing further pipeline work in the same response is a gate violation.
- **Approval is per-gate.** An early "go ahead" never covers later gates; explicit full-run pre-authorization must be recorded as a `decision_log` entry (`category: "approval_policy"`) to count.
- Wait for human to approve, request revision, or abort.

### Set Up Optional Auto-continue

Before activation, ask the user to choose and approve:

- Strict or Auto-continue; Strict is the default.
- Allowed provider routes and exact schema-supported OpenArt models/modes, account binding, and any per-shot model-selection intent; empirical result evidence is optional and must be reported accurately. Grok media is CLI-managed and unreported. A model preference can rank only within the approved pool.
- Locked must-haves: cast identities, dialogue occurrences, source assets, story predicates and native controls.
- Explicit flex: deterministic duration range, resolution options and exact noncast reference drops/substitutions.
- OpenArt billing choice: (a) exact-quote `credits` with the approved credit ceiling, or (b) `unknown_cost_no_ceiling` with explicit `no_enforceable_credit_ceiling` acknowledgement. Grok's remaining subscription quota is unknown.
- Total, per-shot and repair attempt caps, and the actual gated checkpoint stages to preauthorize (or none).

Retain the full approved planning, templates and policy evidence before writing
the activation decision. Do not silently approve current drift as a new baseline.
For unknown-cost approval, bind the account identity digest and exact model/mode
routes; no existing exact-quote policy migrates. The normal total, per-shot and
repair attempt caps still apply. Open scopes, failed attempts and unresolved
attempts count; these caps do not bound spend. An active unknown-cost policy
uses fresh retained evidence and rooted derivation; callers cannot supply
authorization IDs or objects. An invalid or revoked policy stops continuation;
surface the reason and obtain new approval where needed. Use this budget
explanation:

> Grok CLI uses your existing subscription; its remaining quota is unknown and is not a cost ceiling. Auto-continue never uses paid Grok API calls and never buys plans, credits or top-ups.

For OpenArt, exact-quote mode requires a current quote and uses the approved
credit ceiling. The separate unknown-cost mode has no enforceable ceiling, no
quote arithmetic and no USD value; OpenArt charges remain unknown and may exceed
the user's acceptable exposure. Do not call this free, zero-cost or affordable.
Offer it only as a separately approved billing choice with the explicit
no-ceiling acknowledgement. The October 7 PixVerse V6 result verifies one
reference-free transport/result path and collected bytes; it does not certify
creative quality or qualify H3 models, cast references, dialogue, voices,
multiple references or ending-frame controls. A live test does not itself
authorize future attempts.

## Communication Protocol

Agents coordinate through canonical JSON artifacts, checkpoints, pipeline manifests, and the tool registry.

Primary files:

- Artifact schemas: `schemas/artifacts/`
- Checkpoint schema: `schemas/checkpoints/checkpoint.schema.json`
- Pipeline manifest schema: `schemas/pipelines/pipeline_manifest.schema.json`
- Pipeline manifests: `pipeline_defs/`
- Style playbooks: `styles/*.yaml` (validated by `schemas/styles/playbook.schema.json`)
- Tool contract: `tools/base_tool.py`
- Tool registry: `tools/tool_registry.py`
- Stage director skills: `skills/pipelines/<pipeline>/<stage>-director.md`
- Meta skills: `skills/meta/*.md`

Checkpoint rules:

- Checkpoints live at `projects/<project_id>/checkpoint_<stage>.json` (the project workspace — this is what the Backlot board watches).
- `status` may be `completed`, `failed`, `awaiting_human`, or `in_progress`.
- Write an `in_progress` checkpoint on entering each stage; during `assets`/`compose`, refresh `metadata.partial_progress` after each completed scene/asset unit — this powers live progress on the board.
- `completed` and `awaiting_human` checkpoints must include the canonical artifact.
- A gated stage (`human_approval_default: true`) can only be written `completed` with `human_approved=True` — the writer raises a GATE VIOLATION otherwise.
- Superseded checkpoints are archived automatically to `projects/<project_id>/history/` — stage re-runs never destroy run history.
- Invalid checkpoints or invalid canonical artifacts are contract violations and should fail fast.

Pipeline manifest rules:

- Pipelines are declarative YAML manifests in `pipeline_defs/`.
- Stages declare: `skill` (director skill path), `produces`, `tools_available`, `review_focus`, `success_criteria`, `human_approval_default`.
- Adding a new pipeline requires a manifest + stage director skills.

Tool rules:

- Every production tool must inherit from `BaseTool`.
- Tool discovery flows through the registry, not ad hoc imports.
- Support-envelope reporting is the source of truth for capability, status, and resource requirements.

## Style Playbooks

| Playbook | Best For |
|----------|----------|
| `clean-professional` | Corporate, educational, SaaS |
| `premium-minimalist` | Investor updates, expert explainers, product narratives |
| `flat-motion-graphics` | Social media, TikTok, startups |
| `minimalist-diagram` | Technical deep-dives, architecture |
| `ink-sketch` (Ink Theater) | Hand-drawn ink-on-white doodle animation; a character that draws itself, walks, dances; contraption explainers |

For custom, atelier, brand, launch, or hero work, read `skills/meta/taste-direction.md` before choosing a playbook. Carry its `taste_profile` into the proposal so later stages can preserve the design read, visual variance, motion intensity, information density, reference strategy, and anti-patterns.

### Hand-drawn "doodle" animation → Ink Theater / Ink Puppet

For any brief that wants a **hand-drawn ink doodle** look — "a sketch that comes to life", "a pencil / stick figure that walks or dances", "a little character that acts out the idea", whiteboard-doodle explainers — use the **Ink Theater** engine + **Ink Puppet** mocap system (`skills/creative/ink-theater.md`, `ink-theater/README.md`). It is a **style + reusable engine, not a new pipeline**: illustration / contraption pieces run on the `animation` pipeline; a mocap character (draws itself → walks / dances / waves via `InkPuppet.choreograph([...])`) runs on `character-animation`. Cross-tool entry points: **`/ink-art`** (create a vector doodle from scratch) and **`/animated-drawing`** (animate a *supplied* drawing with mocap — raster; `skills/creative/animated-drawing.md`). Never hand-tune character motion — the agent only chooses named mocap clips.

## Layer Map

OpenMontage has three instruction layers:

1. `tools/`
   What exists, what is available, cost, runtime, fallback, related skills.
2. `skills/`
   How OpenMontage wants those tools used in pipelines.
3. `.agents/skills/`
   Raw vendor or technology knowledge.

Reading order:

1. registry / tool contract — discover what's available
2. relevant pipeline or creative skill (Layer 2) — know HOW to use it in this context
3. underlying vendor skill (Layer 3) — **mandatory before calling any generation tool**

**Prefer skills over source code for tool usage.** Skills exist precisely so you don't need implementation details in the common case. Layer 2 tells you *what* and *when*. Layer 3 tells you *how*. For authoring prompts, choosing parameters, or understanding usage patterns, you should be reading skills — not `.py` files.

**Exception: debugging, audits, and verifying the governance contract.** When a skill and a tool disagree, or when something behaves differently than the skill claims, reading the tool source is fair game — that's often the only way to catch a silent-availability bug or a stale doc string. An audit that refuses to look at the implementation will miss exactly the bugs that matter most. If you do read source to debug, consider whether the finding belongs in a skill update afterward so the next agent doesn't need to repeat the dive.

**Layer 3 is not optional.** Every generation tool (video, image, TTS, music) has an `agent_skills` field listing its Layer 3 skills. These skills contain provider-specific prompt engineering, parameter tuning, and quality techniques. Read them before writing prompts. The difference between a generic prompt and a skill-informed prompt is the difference between "usable" and "cinematic."

Example: Before calling `kling_video`, read its `agent_skills` → `ai-video-gen` → get Kling-specific prompt structure, camera direction syntax, and quality keywords that the model responds to best.

### Layer 3 skills, by category

The `.agents/skills/` directory is large. When you're not coming in through a tool's `agent_skills` pointer, use this table to find the right file by *what you're trying to do*:

| Category | Skills |
|---|---|
| **Composition runtime** | `remotion`, `remotion-best-practices`, `synthetic-screen-recording` (fake terminal/UI demos via Remotion TerminalScene), `threejs-world-generation` (semantic terrain and free-viewpoint HyperFrames worlds) |
| **Animation knowledge (generic)** | `gsap-core`, `gsap-timeline`, `gsap-plugins` (SplitText / MorphSVG / DrawSVG / MotionPath / Flip / CustomEase), `gsap-utils`, `gsap-react`, `gsap-performance`, `gsap-scrolltrigger`, `gsap-frameworks`, `framer-motion` (Disney 12 principles), `lottie-bodymovin` (Lottie export) |
| **Character animation** | `character-rigging`, `svg-character-animation`, `pose-library-design`, `canvas-procedural-animation`, `character-animation-qa` |
| **Image generation** | `bfl-api`, `flux-best-practices` |
| **Video generation** | `seedance-2-0` (preferred premium default — cinematic, trailer, multi-shot, synced audio, lip-sync), `gemini-omni` (conversational video editing, reference tags, timecoded beats), `ai-video-gen`, `ltx2` |
| **Audio** | `elevenlabs`, `music`, `sound-effects`, `acestep`, `text-to-speech`, `azure-text-to-speech` (optional cloud TTS — tool `azure_tts`, same Speech key as `azure_stt`), `setup-api-key` |
| **Speech-to-text** | `speech-to-text` (whisper `transcriber` — default, offline), `azure-speech-to-text` (optional cloud STT — tool `azure_stt`, preferred when `AZURE_SPEECH_KEY` is set) |
| **Avatar / lip-sync** | `avatar-video`, `heygen`, `create-video`, `faceswap`, `video-translate`, `agents` |
| **Capture** | `playwright-recording` (browser flows), `ffmpeg` (post) |
| **Visualization** | `beautiful-mermaid`, `d3-viz`, `manim-composer`, `manimce-best-practices`, `manimgl-best-practices` |
| **Media editing** | `video-edit`, `video-download`, `video-understand`, `video-toolkit`, `visual-style` |

**When in doubt, read the category's meta routing file first:**
- Picking an animation runtime? → `skills/meta/animation-runtime-selector.md` routes between Remotion primitives, GSAP plugins, framer-motion, Lottie, Manim, D3.
- Picking a screen-recording mode (real capture vs synthetic terminal)? → `pipeline_defs/screen-demo.yaml` + `skills/pipelines/screen-demo/idea-director.md`.

## Quick Lookup

| Question | Where to look |
|----------|---------------|
| What tools exist? | `tools/tool_registry.py` and `registry.support_envelope()` |
| What providers are available for a capability? | `registry.capability_catalog()` |
| What tools exist for a vendor? | `registry.provider_catalog()` |
| How does a tool actually work? | the tool's `usage_location` from the registry |
| How should this pipeline stage behave? | `skills/pipelines/<pipeline>/...` |
| What is the checkpoint/review policy? | `skills/meta/` |

## What Not To Do

- **Do not bypass the pipeline.** Never write ad-hoc scripts to call tools directly. All production goes through pipeline stages with director skills. See Rule Zero.
- **Do not call generation tools without reading their Layer 3 skill.** Check the tool's `agent_skills` field, read the referenced skill, then craft your prompts using that guidance.
- **Do not skip stage director skills.** Before executing any pipeline stage, read its director skill. The skill contains the quality bar, the workflow, and the review criteria.
- Do not use deleted legacy names such as `tts_cloud`, `tts_engine`, or `video_gen`.
- Do not hardcode provider names, API key names, or setup URLs. Read them from the registry's `install_instructions` and `dependencies` fields.
- Do not tell a restricted shared-installation user to add credentials manually, create a `.env`, or export a key. Surface the unavailable provider as an administrator setup request and continue only with centrally provisioned capabilities.
- Before offering a direct vendor credential, check the registry for an available wrapper through an already configured provider (for example, a partner model hosted by fal.ai).
- Do not begin asset generation before user approval on the production plan.
- Do not hide degraded paths. Record substitutions and blocked options explicitly.
- Do not present a single unavailable tool in isolation. Always show the full capability picture: "X of Y providers configured for this capability."
- Do not skip the Provider Menu at preflight. The user must see what they have AND what they could unlock.
- Do not change provider, model, or render path without telling the user first and getting approval when the change is material.
