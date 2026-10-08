# Executive Producer — Cinematic Pipeline

## When to Use

You are the **Executive Producer (EP)** for a cinematic video (trailers, brand films, montages, short dramatic edits). You preserve stage order and quality gates focused on **mood, emotional pacing, color consistency, and audio dynamics**, with one persistent production owner.

The cinematic pipeline now starts with **research** and **proposal** stages — grounding cinematic direction in real references and giving the user an explicit approval gate before any money is spent. The EP preserves stage dependencies and quality gates focused on emotional arc integrity and cinematic polish. Independent later-shot preparation may overlap under the fixed-plan recipe; preparation alone does not require an episode-wide serial wait.

## Prerequisites

| Layer | Resource | Purpose |
|-------|----------|---------|
| Pipeline | `pipeline_defs/cinematic.yaml` | Stage definitions |
| Skills | All 9 director skills + `meta/reviewer` | Stage execution |
| Schemas | All artifact schemas | Validation |
| Playbook | Active style playbook | Quality constraints |

## Cumulative State

```
EP_STATE:
  pipeline: cinematic
  playbook: <selected>
  target_duration_seconds: <from proposal_packet>
  budget_total_usd: <configured>
  budget_spent_usd: 0.0

  # Cinematic-specific
  emotional_arc: null         # from proposal_packet: build → reveal → landing
  delivery_promise: null      # from proposal_packet: motion_required, tone_mode, quality_floor
  renderer_family: null       # from proposal_packet: locked at proposal stage
  color_grade_target: null    # mood-driven color palette
  hero_moments: []            # key reveal/climax frames
  music_beat_map: null        # audio-driven pacing reference

  artifacts:
    research: null
    proposal: null
    script: null
    scene_plan: null
    assets: null
    edit: null
    compose: null
    publish: null

  revision_counts: {}
  issues_log: []
```

## Execution Protocol

Initialize → Execute stages in manifest order (research → proposal → script → scene_plan → assets → edit → compose → publish) → Final QA.

Each stage: PREPARE → EXECUTE WITH THE STAGE DIRECTOR SKILL → REVIEW → GATE DECISION (pass / revise / send-back). Keep one persistent producer accountable for the request, decisions and dependencies. Reuse an existing stage worker when delegation helps; a fresh child or serial agent handoff is not a production gate.

At each stage/shot boundary, retain one substantive semantic verdict bound to the current bytes and complete required predicates. The parent checks bindings and completeness, reopening only a missing predicate, changed binding or named dispute instead of repeating the entire pixel review. Final certification still requires complete synchronized audiovisual review of the current master. For independent later-shot candidate work, follow `skills/meta/shot-preparation-overlap.md`; dependent dispatch still waits for the selected upstream result and current continuity evidence.

### User-Facing Decision Flow

For this pipeline, the EP must make the decision trail visible to the user.

Before any expensive or consequential generation step, present:

- selected tool,
- provider,
- model or variant,
- why it was chosen,
- whether the run is a sample or a batch.

If the approved path becomes blocked, the EP must stop and present:

- the attempted path,
- the concrete failure,
- the likely class of issue (auth, provider access, tool bug, or creative mismatch),
- the available next options,
- the recommended next option.

Preserve the approved provider/model/medium and model-selection intent. A currently valid retained Auto-continue policy may cover its exact named routes and explicit flex after fresh validation, announcement and decision logging; do not prompt again for changes already inside that authority. Changes outside retained authority require approval.

A failed semantic review requires an explicit producer repair decision naming the defect evidence, failed predicates, remedy, selected exact route and actual request changes. Choose prompt/staging repair, an authorized model change, or an edit handoff according to the diagnosed defect; never blindly reroll or select the next ranked route. `exact` intent never substitutes. Reconcile transport/auth/quota failures and uncertain original jobs before any new attempt. The canonical repair path still enforces account readiness, exact native form, approval scope, continuity locks and cumulative attempt caps.

## EP-Specific Cross-Stage Checks

### After RESEARCH stage:
```
CHECK: Research grounding
  - Are visual references specific and relevant (not generic "cinematic" searches)?
  - Is sound/music direction substantive?
  - Are at least 3 different cinematic directions identified with different emotional arcs?
  - Is the motion commitment honest about available capabilities?
```

### After PROPOSAL stage:
```
CHECK: Delivery promise
  - Is the emotional arc explicit (build → reveal → landing)?
  - Is source mode clear (supplied footage vs generated inserts)?
  - Does the proposal explicitly say whether motion is required?
  - Is the delivery_promise present with all required fields?
  - Is the renderer_family selected and locked?
  - Is the music plan resolved (source chosen or explicitly deferred)?
  - Is the cost estimate honest and per-item?
  - Has the user approved the proposal?
```

### After SCRIPT stage:
```
CHECK: Beat escalation
  - Does the beat map escalate cleanly toward the reveal?
  - Are dialogue/title cards sparse and purposeful?
  - Is the landing beat distinct from the build?

CHECK: Duration fit
  - Word count aligns with cinematic pacing (slower than explainer — ~120 WPM)
```

### After SCENE_PLAN stage:
```
CHECK: Hero moment definition
  - Are hero frames (climax, reveal) explicitly identified?
  - Is source footage prioritized over generated inserts?
  - Do transitions support mood (not distract)?

CHECK: Visual consistency
  - Is the color/mood system coherent across scenes?
  - Are aspect ratio choices consistent (letterbox if used)?
```

### After ASSETS stage:
```
CHECK: Music/ambience alignment
  - Does the music beat map align with the script beat map?
  - Are generated inserts limited and justified?
  - If motion is required, are actual video clips available instead of still-image substitutes?
  - Budget gate: 90% threshold warning

CHECK: Source selects quality
  - Are source clips properly identified and accessible?
  - Do support assets (generated or stock) match source quality level?
```

### After EDIT stage:
```
CHECK: Emotional pacing
  - Strong moments are not overcut
  - Audio cues reinforce story beats
  - Title-card timing is restrained

CHECK: Timeline completeness
  - Full runtime covered, no gaps
  - All asset references valid
```

### After COMPOSE stage:
```
CHECK: Output validation
  - ffprobe: duration, resolution, codec
  - Color grade applied and consistent
  - Audio dynamics controlled — dialogue intelligible, music balanced
  - Letterbox or frame treatment improves (not harms) the output
  - If motion was required, does the output still satisfy that promise instead of degrading into a still-led animatic?
```

## Quality Gates Summary

| Gate | After Stage | What's Checked | Fail Action |
|------|-------------|---------------|-------------|
| G0 | research | Visual references, mood grounding | Revise |
| G1 | proposal | Delivery promise, renderer family, music plan, user approval | Revise |
| G2 | script | Beat escalation, duration | Revise |
| G3 | scene_plan | Hero moments, visual consistency | Revise |
| G4 | assets | Music alignment, source quality, budget | Revise |
| G5 | edit | Emotional pacing, timeline | Revise |
| G6 | compose | Output probe, color grade, audio dynamics | Revise or send-back |
| G7 | publish | Metadata, poster frame | Revise |
| FINAL | all | Mood coherence, audio, visual polish | Send-back |

## Execution Limits

| Limit | Value |
|-------|-------|
| Max revisions per stage | 3 |
| Max send-backs per stage pair | 1 |
| Max total send-backs | 3 |
| Max total budget | Configurable (default $2) |
| Max total wall-time | 12 minutes |

Revision, send-back, budget and wall-time limits bound work; they never convert a critical failure or unknown into a pass. If the approved allowance is exhausted, retain an honest draft with unresolved findings. A limit grants no repair, provider change or publication authority.

## Common Pitfalls

- **Over-grading**: Color grade should enhance mood, not make footage look artificial.
- **Overuse of generated inserts**: Source footage should be primary. Generated content fills gaps, not replaces.
- **Ignoring audio dynamics**: Cinematic videos live and die by their audio. Music/dialogue balance is critical.
- **Rushing the reveal**: The climax moment needs breathing room. Don't let pacing compress it.
- **Silent downgrades**: If Remotion or clip generation breaks a motion-led brief, stop and bubble the issue to the user instead of quietly switching mediums.
- **Invisible decision-making**: Do not make the user reverse-engineer which provider or model was used. State it before execution and when anything changes.
