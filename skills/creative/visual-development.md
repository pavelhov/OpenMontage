# Visual Development — Dramaturgy, References, and Motion Handoff

> **Attribution:** Materially adapted on 2026-09-01 from Visual Skills by Serge
> Shima (<https://github.com/smixs/visual-skills/tree/3c554715b5eb30f54de78fac3c0df4a7105e4955>),
> © 2026 Serge Shima, licensed CC BY 4.0. This adaptation is not endorsed by
> the author. See `THIRD_PARTY_NOTICES.md` and `licenses/visual-skills-CC-BY-4.0.txt`.

## When to Use

Use this shared Layer-2 skill before scene planning for narrative/camera-led
generated visuals, reference-led work, continuity-sensitive clips, keyframe
handoffs, or controlled image/video edits. It is not a stage director and does
not add a pipeline stage. Pair it with the selected provider's Layer-3 skill:
this skill establishes what must be shown; the provider skill establishes how
that provider expresses it.

## Start With a Filmable Dramatic Job

Choose one route before drafting a prompt:

- **Narrative/camera-led:** state the five items below. A scene without these
  answers may be decorative, but it is not ready for this route.
- **Deterministic/data/text:** do not invent desire, obstacles, body acting, or
  camera drama. Record the canonical `information_role`, focal hierarchy,
  transition intent, and any explicit N/A rationale instead.

1. **Desire:** What does the subject/viewer need right now?
2. **Obstacle or pressure:** What visibly prevents, threatens, complicates, or
   resists that desire?
3. **Spatial geometry:** Where are the subject, pressure, key object, and exit
   or resolution in the frame? Preserve screen direction across adjacent clips.
4. **Controlled gaze:** What receives the eye first, second, and last? Use
   contrast, blocking, focus, motion, or a cut deliberately.
5. **Edit rhythm:** Why does this shot last this long, and what changes at the
   cut? Let rhythm serve emotion and comprehension before speed.

Every narrative/camera-led shot should change emotion, advance action/information, or increase
pressure. Delete a shot with none of those jobs. Give a moving camera a stated
reason—the change it reveals or the pressure it follows—or keep it static.
Describe physical evidence rather than labels: an environmental pressure, a
micro-action, and a sound anchor or recurring visual motif are more useful than
words such as “cinematic,” “epic,” or “moody.”

## Sidecars, Not Closed Artifact Items

`scene_plan.scenes[]` and `asset_manifest.assets[]` are closed schemas. Keep
their canonical fields unchanged. Store rich visual-development data only in
the artifact's existing top-level `metadata` object, keyed by existing IDs:

```json
{
  "scene_plan": {
    "metadata": {
      "visual_development": {
        "shot_cards": { "scene-03": { "scene_id": "scene-03", "dramaturgy": {}, "reference_requirements": [] } },
        "continuity_ledger": { "scene-03": { "scene_id": "scene-03", "entry_state": {}, "exit_state": {} } },
        "animatic_keyframes": { "scene-03": { "scene_id": "scene-03", "keyframe_requirements": [] } }
      }
    }
  },
  "asset_manifest": {
    "metadata": {
      "reference_assets": { "asset-17": { "asset_id": "asset-17", "roles": ["look", "composition"] } },
      "motion_handoffs": { "asset-17": { "asset_id": "asset-17", "approved": true } },
      "edit_attempts": { "edit-attempt-01": { "edit_attempt_id": "edit-attempt-01", "source_master_asset_id": "asset-17" } },
      "edit_contracts": { "asset-18": { "asset_id": "asset-18", "source_master_asset_id": "asset-17" } }
    }
  }
}
```

ID-keyed production sidecars must already exist in their owning canonical
collection: scene-keyed maps use existing `scene_plan.scenes[].id` values and
asset-keyed maps use existing `asset_manifest.assets[].id` values. The explicit
exception is an attempt/history map such as `prompt_attempts[attempt_id]` or
`edit_attempts[edit_attempt_id]`: its stable unique attempt ID is audit identity
and must never masquerade as an asset ID. Do not invent future asset IDs during scene planning; describe the
requirement, then bind the real ID after the Asset Director creates the
canonical asset record. The sidecars
clarify and cross-reference; they never replace `description`,
`required_assets`, `prompt`, or a tool contract. Do not add arbitrary keys to
individual scene or asset objects.

## Shot-Card Sidecars

Create `scene_plan.metadata.visual_development.shot_cards[scene_id]` for each
narrative/camera-led or otherwise high-stakes scene. Deterministic/data/text
scenes use a minimal sidecar only when needed: `mode: deterministic`, canonical
`information_role`, focal hierarchy, transition intent, and explicit N/A
rationale. This is an extension/derived review view, not a
second scene ontology. Canonical scene fields remain authoritative for type,
description, timing, framing, movement, transitions, `overlay_notes`,
`shot_language`, shot/information/narrative intent, hero status, and required
assets. A sidecar may point to or summarize those values but must not redefine
them; resolve any conflict in favor of the canonical scene item.

A practical sidecar records only missing structured direction:

| Field | Direction to capture |
|---|---|
| `dramaturgy` | desire, obstacle/pressure, and visible change; map the shot job to canonical `shot_intent`, `narrative_role`, and `information_role` |
| `spatial_relationships` | power/threat/escape relationships and screen-direction continuity not already explicit in canonical framing/shot language |
| `gaze_path` | first/second/final attention target and the mechanism that controls each |
| `physical_details` | environmental pressure, body micro-action, sound anchor or motif |
| `movement_reason` | what changed to motivate the canonical camera movement, or why the shot remains static |
| `cut_reason` | eye-trace and state change motivating canonical timing/transitions |
| `structured_overlays` | exact content/placement extending canonical `overlay_notes` |
| `reference_requirements` | needed role, subject binding, inherit/ignore rules, scope, and provider capability; realized asset IDs are bound later in the asset manifest |
| `constraints` | approved non-negotiables, including exact text or safety/brand constraints |

The card may mark an item `not_applicable` when the scene is deterministic
motion graphics or a diagram. Explain why rather than silently omitting it.

## Provider-Neutral Reference Roles

References are evidence with a declared job, not a pile of images. Scene-plan
shot cards declare stable reference requirement IDs. After the Asset Director
creates or inventories the real reference asset, record it under
`asset_manifest.metadata.reference_assets[asset_id]` and bind that record back
to the requirement ID and scene ID. Assign one or more roles:

- `identity` — repeatable person, character, product, or object traits.
- `look` — palette, material, lighting, texture, or grade.
- `composition` — framing, geometry, and eye path.
- `location` — place, architecture, spatial layout, or environmental facts.
- `motion` — action cadence, camera logic, or transition behavior.
- `end_state` — target final frame for a clip boundary or extension.
- `audio` — rhythm, sound cue, or dialogue-performance reference.

The reference record must say what to preserve, what to change, and which
provider capability accepts it. Check the selected tool's real support envelope
and Layer-3 syntax before passing any reference; a role does not imply that a
provider can ingest that media type. Each record should include the canonical
`asset_id`, media type, role(s), subject or object binding, attributes to
inherit, attributes/background/people to ignore, scene or time-range scope,
priority, and temporal use (`first_frame`, `last_frame`, `key_state`, or
`style_only`) when relevant.

## Continuity Ledger for Clip Boundaries

For a sequence of clips, maintain
`scene_plan.metadata.visual_development.continuity_ledger[scene_id]`. Each
entry owns an `entry_state` and `exit_state` with the observable facts the next
clip needs: identity/costume, pose and hand/object state, screen direction,
camera position and lens impression, lighting/weather/time, background
landmarks, palette/grade, and sound/rhythm. Include `previous_scene_id`,
`next_scene_id`, `match_strategy`, and `boundary_risk`.

Before generating the next clip, compare its entry state with the prior exit
state. If a mismatch is intentional, state the editorial bridge (cutaway,
match cut, dissolve, reset, or visible transition); do not disguise an
uncontrolled discontinuity as style.

## Animatic and Approved-Keyframe-to-Motion Handoff

Use `scene_plan.metadata.visual_development.animatic_keyframes[scene_id]` to
hold the small, reviewable plan before costly motion: beat timing, framing,
keyframe requirements, focal shift, action path, and transition intent. Do not
assign a future asset ID here. Once the Asset Director creates the keyframe,
its canonical `asset_manifest.assets[].id` becomes the stable binding. A
keyframe is ready to animate only after its dramatic job, geometry, gaze, and
continuity state are approved.

For each planned panel capture: panel/timecode and hold duration, story beat and
function, one focal anchor, foreground/midground/background jobs, implied
camera movement, emotion translated into an object or physical state, motion
shown as a frozen consequence (trajectory, differential blur, residue, or
deliberate stillness), lighting/palette, sound beneath the hold, and a
continuity/production note.

For every approved keyframe that becomes motion, create
`asset_manifest.metadata.motion_handoffs[keyframe_asset_id]` containing:

- `asset_id` and `scene_id` (existing IDs), approval status and reviewer note;
- `preserve`: identity, composition anchors, lighting/grade, and entry state;
- `change`: ordered action, camera behavior, and time-bounded evolution;
- `constraints`: duration, aspect ratio, prohibited changes, exact text, and
  provider/tool capability limits;
- `end_state` plus the next scene ID or final-frame intent.

Write motion instructions as **Change / Preserve / Constraints**, not as a
second image prompt. This gives the motion provider a stable starting frame and
prevents it from reinventing the scene.

## Edit Contract

For image or video edit operations, allocate a stable `edit_attempt_id` before
the call and create `asset_manifest.metadata.edit_attempts[edit_attempt_id]`.
Name the existing `source_master_asset_id`, exactly one concrete change for the
pass, the complete preserve list, catch-all constraints for all unmentioned
material, provider/model operation, and planned output path. Keep blocked or
failed attempts here without fabricating an output asset.

On success, create the canonical output asset record and finalize the same
contract under `asset_manifest.metadata.edit_contracts[output_asset_id]`,
retaining `edit_attempt_id` plus source/output revision lineage. Never overwrite
the source master or treat a re-roll as a controlled edit.

## Readiness Check

Before a generation request leaves the assets stage, verify:

- narrative/camera-led work has concrete dramaturgy, physical details, and
  reasons to move/cut; deterministic work has information role, focal hierarchy,
  transition intent, and honest N/A fields instead;
- reference roles are complete for the intended use and supported by the tool;
- neighboring clip states either match or have an explicit bridge;
- any approved keyframe has a Change / Preserve / Constraints motion handoff;
- any image/video edit has a pre-call `edit_attempts` record and, on success, a
  finalized contract with one change, repeated preserve list, constraints, and
  explicit source/output asset lineage;
- all rich records live in top-level metadata sidecars keyed by existing IDs.
