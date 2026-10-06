---
name: openart-cli
description: Guarded OpenArt subscription CLI video route. Read the qualified route menu, ask for creative options, and run only fully qualified, explicitly pinned, governed attempts.
metadata:
  author: OpenMontage
  version: "1.0.0"
  tags: openart, video-generation, cli, credits, qualification
---

# OpenArt CLI

Use this skill whenever a plan touches the OpenArt CLI video route
(`provider="openart_cli"`, tool `openart_cli_video`). The authoritative
reference is `docs/OPENART_CLI.md`. Prompt guidance is in
`skills/creative/prompting/openart-prompting.md`.

## What the route is

- It is an explicit-only, credit-billed subscription CLI route, separate from Grok CLI
  and from every external API. It is never a fallback for anything else, and
  nothing falls back from it.
- Account discovery is pending as last observed. A nonspending probe on
  2026-10-06 found the official CLI 0.1.1 installed, but `account` reported a
  login is required. That is a past observation, not a live claim. Report the
  menu's current state and do not assert live auth without a fresh probe.
- Strict approval is the default. A strict original scope can preapprove a
  bounded batch of exact attempts, so a new prompt per attempt is not
  mandatory. Optional Auto-continue requires a retained user-approved policy
  activated by the decision log. Use the rooted policy derivation and normal
  governed dispatch; never supply caller-created authority or bypass quotes,
  preparation, caps or the shared credit allowance. Missing or revoked policy
  authority stops continuation. Unknown jobs are never resubmitted.

## Read the menu, never guess

Call `provider_menu_summary()` and read
`qualified_cli_video_routes`. For the OpenArt row:

1. `models` are the only production-available exact model IDs, because they
   are full, real-result qualified profiles. Use their `form`, `controls` and
   `limitations` as written.
2. `qualification_candidates` (inspected or pre_submit) are not production
   routes. Their only use is the next qualification stage. The first original
   result qualification is a separately credit-authorized attempt whose
   purpose is reported as qualification. It does not require a full profile to
   exist first.
3. `not_live` (fixtures and rejected rows) is never live.
4. `billing` is in credits with `current_quote_required: true`, and the dollar
   cost is unknown. Show it separately from Grok's subscription or usage
   billing.
5. Report `pending`, `holds`, `quarantine`, `unacknowledged_outbox` and
   `errors` as they appear. An unresolved job acceptance (pending, submitting
   or uncertain slot) blocks dispatch as `unknown_job_acceptance_unresolved`.
   An unknown-billing hold on a terminal slot does not block by itself and
   does not invalidate approved footage. Report the economics as incomplete:
   a remaining allowance and a fresh exact quote are required.
6. The OpenArt metadata runs no OpenArt CLI and writes no ledger rows. The
   Grok row's status runs the existing read-only `--version`/`--help` probe.
   Grok reports no video models (`model_policy:
   cli_managed_media_unreported`); its agent model is not a video model.

Do not invent model IDs (H3 or any other), native audio, end-frame pins, rates
or defaults. You may recommend a qualified OpenArt row when its observed,
qualified controls fit the shot's requirements; explain that control fit and
label the expectations as unmeasured. Without benchmark evidence, do not claim
comparative quality, acceptance-rate or cost superiority, and do not name
OpenArt the best route or the default.

## Talking to the user

Ask for creative options: shot description, duration, aspect ratio, and an
optional single reference image. Offer only the controls the qualified profile
lists. Do not ask the user to type job commands, IDs or CLI flags.

If the shot needs a control the route lacks (an end-frame pin, native audio or
voices, multiple reference images), say so plainly and offer two paths: a
route that has the control, or a revised creative request. A pinned OpenArt
request that needs a missing control fails with `missing_controls` before any
credit reservation. Never smuggle the missing control in through prompt
wording. Keep approved dialogue lines in the prompt anyway, and disclose that
there is no native-audio guarantee.

## Running an attempt

- Pin the route exactly with `preferred_provider="openart_cli"` and
  `allowed_providers=["openart_cli"]`, inside a strict project. Ungoverned pins
  are refused before any provider call.
- The approved settings (model, duration, aspect ratio, image, and resolution
  only where an observed profile form qualifies it) pass through unchanged. A
  required resolution that no observed form and exact preview carry stays
  unqualified. Server defaults never stand in for it. A strict scope may
  preapprove a bounded batch of exact attempts. A request that changes any
  approved setting is outside that scope and needs a new approval and a new
  quote. Nothing retries automatically.
- Every paid attempt needs a current exact quote and a credit authorization.
  Each attempt launches exactly once. Status, collect and resolve can be
  repeated safely against the original attempt and never launch again. Never
  resubmit an `uncertain` attempt. It stays held with no timeout until
  qualified original receipts resolve it, or provider history does if that
  becomes available. Do not assume provider history exists.
- Before the first image upload, the provider must have stated in a captured
  response that uploading is nonspending and has no delayed charge.

Review generated clips the same way as any other provider's: current bytes,
semantic review against the shot contract, then selection. Provider
qualification is not creative approval.
