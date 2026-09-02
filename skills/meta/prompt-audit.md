# Prompt Audit — Generation Readiness and Rewrite

> **Attribution:** Materially adapted on 2026-09-01 from Visual Skills by Serge
> Shima (<https://github.com/smixs/visual-skills/tree/3c554715b5eb30f54de78fac3c0df4a7105e4955>),
> © 2026 Serge Shima, licensed CC BY 4.0. This adaptation is not endorsed by
> the author. See `THIRD_PARTY_NOTICES.md` and `licenses/visual-skills-CC-BY-4.0.txt`.

## When to Use

Run this lightweight, no-tool audit immediately before each image, video, or
other visually directed generation request. Read `creative/visual-development`
first when the request has a scene or motion job, then read the selected tool's
Layer-3 skill for provider syntax and limits. This audit improves a prompt; it
does not override approved creative direction, tool contracts, or a provider's
actual capabilities.

## Three Passes

1. **Pre:** Allocate a stable `attempt_id`, preserve the first usable draft,
   and name the planned asset role, scene, provider, model/operation, and
   existing reference asset IDs. Do not invent an output asset ID yet.
2. **Critique:** Identify what cannot yet be rendered or safely passed to the
   selected provider. Check the following.
3. **Post:** Produce the stronger, provider-native prompt or request payload.
   Make omissions explicit when they are intentional (for example, a static
   diagram has no camera move).

### Critique Checklist

- **Dramaturgical function:** Is the subject's desire, obstacle/pressure,
  geometry, controlled gaze, and edit/rhythm job clear enough for the shot?
  Does the shot change emotion, action/information, or pressure?
- **Physical direction:** Are environment pressure, body/action, subject-object
  interaction, framing, and camera motivation concrete rather than adjective
  placeholders?
- **Reference-role completeness:** Does every supplied reference have a
  provider-neutral role (`identity`, `look`, `composition`, `location`,
  `motion`, `end_state`, or `audio`), and is that role actually needed?
- **Continuity risk:** For a clip or extension, do entry state, exit/end state,
  screen direction, pose/object state, light/grade, and the intended transition
  agree with adjacent clip records?
- **Provider capacity and syntax:** Is the chosen provider/model/operation
  available, capable of the requested input/reference types and duration, and
  written using its documented Layer-3 syntax? Remove unsupported parameters or
  reference roles; do not invent a workaround.
- **Preservation:** For edits or image-to-video work, are the required
  `preserve`, `change`, and `constraints` instructions separated so the model
  cannot mistake a protected element for a requested change?

## Persistent Record

Asset objects are closed schema. Every audit starts in working state under
`asset_manifest.metadata.prompt_attempts[attempt_id]`; this attempt ID is an
audit identifier, not an asset ID. If generation succeeds, first create the
canonical `asset_manifest.assets[]` record, then finalize the audit under
`asset_manifest.metadata.prompt_audits[asset_id]`, retaining its `attempt_id`.
If capability, auth, budget, or provider checks block the call before an output
exists, keep the record only in `prompt_attempts[attempt_id]`. Never fabricate
an asset record merely to store a blocked audit, and never write `prompt_audit`
or arbitrary metadata inside an asset item.

```json
{
  "metadata": {
    "prompt_attempts": {
      "attempt-scene-03-01": {
        "attempt_id": "attempt-scene-03-01",
        "scene_id": "scene-03",
        "status": "sent",
        "resolved_asset_id": "asset-17"
      }
    },
    "prompt_audits": {
      "asset-17": {
        "attempt_id": "attempt-scene-03-01",
        "asset_id": "asset-17",
        "scene_id": "scene-03",
        "provider": "selected provider from registry",
        "operation": "selected documented operation",
        "pre": "first complete draft",
        "critique": {
          "dramaturgical_function": [],
          "reference_roles": [],
          "continuity_risks": [],
          "provider_capacity_and_syntax": [],
          "preservation": []
        },
        "post": "stronger request actually sent",
        "reference_asset_ids": ["asset-04"],
        "status": "passed | revised"
      }
    }
  }
}
```

If the provider check fails, record `blocked` in
`asset_manifest.metadata.prompt_attempts[attempt_id]`, explain the exact
unsupported need, and follow the normal blocker/approval protocol. Do not
silently replace the provider, model, source medium, or approved motion
treatment.

## Stronger Rewrite Pattern

Keep the rewrite short enough for the provider, but make its facts visible:

```
Function and change: [what becomes clearer / more pressured / resolved].
Subject and action: [specific body/object action in order].
Geometry and gaze: [where subject, pressure, and focus live; what the eye sees].
Camera and rhythm: [framing, motivated motion or static rationale, duration/cut].
Physical anchors: [environmental pressure, micro-action, sound/motif].
References: [asset ID + role, only if supported by this operation].
Continuity: [entry state -> required end state / bridge].
Change / Preserve / Constraints: [for edits or keyframe-to-motion].
```

The post version is the only version sent to the tool. Preserve the pre and
critique so a reviewer can diagnose a failed generation without guessing.
