---
name: openart-cli
description: Guarded OpenArt subscription CLI video route. Check schema, account, and native transport readiness; bind exact-quote or explicitly acknowledged unknown-cost authority, and run governed attempts.
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
  and from every external API. An alternative route needs authority within the
  user's approved pool, current control support, and a fresh governed request.
  Preserve exact model locks and reconcile uncertain original jobs first.
- The October 7 read-only sweep retained 17 video model IDs and 45 advertised
  model/mode pairs. Use current catalog/form evidence, current authenticated
  account readiness, and exact local native request support to decide whether a
  requested route can be produced. Prior paid or live result qualification is
  optional diagnostic evidence, not a production prerequisite. Six
  successful forms use root `anyOf`/`oneOf`; the generic capability helper and
  its offline tests evaluate those branches without promoting them to routes.
  The receipt-linked sanitized fixture and its limits are in
  `docs/implementation/2026-10-07-openart-model-controls.md`.
- The verified official CLI 0.1.1 surface carries prompt, duration, aspect
  ratio, resolution and one `--image` start frame. It selects text-to-video and
  image-to-video; it does not select element-to-video. It has no generic native
  params flag, end-frame pin, multi-reference, reference-video/audio or native
  audio argument. Form capabilities and CLI transport capabilities are
  separate: unsupported native fields remain visible but fail before credit
  reservation/dispatch. H3 Turbo image-to-video has a tested local `startFrame`
  role binding, but its form requires an `id` absent from the observed CLI
  preview, so this is not a schema-valid request. H3 Turbo output remains
  unqualified.
- Account discovery succeeded on 2026-10-06 at 23:55 UTC using official CLI
  0.1.1. The response carried an identity, plan and credit balance; no explicit
  authentication boolean. Earlier login-required evidence is superseded. The
  October 7 qualification record documents one real, collected PixVerse V6
  reference-free text-to-video sample (1s, 16:9, 720p). Its transport/result and
  output bytes are verified; creative quality was not reviewed. This is
  optional diagnostic evidence and does not gate ordinary production. The
  observed Free account is not a general
  entitlement or affordability claim. No image-to-video mode is live-result
  qualified. Report the menu's current state; a past probe never replaces a
  fresh account refresh. Do not describe unreviewed creative quality as
  verified.
- Strict approval is the default. A strict original scope can preapprove a
  bounded batch of exact attempts, so a new prompt per attempt is not
  mandatory. Optional Auto-continue requires a retained user-approved policy
  activated by the decision log. Use the rooted policy derivation and normal
  governed dispatch; never supply caller-created authority or bypass the
  applicable billing authorization, preparation or caps. Missing or revoked
  policy authority stops continuation. Unknown jobs are never resubmitted.
  Existing policies use the exact-quote ceiling mode. A separate unknown-cost
  policy variant requires its own explicit approval, account binding, exact
  model/mode routes and attempt caps; it states that no enforceable credit
  ceiling exists. Implementation approval does not activate a policy, and no
  existing policy migrates.
- Exact-quote authorization keeps its existing quote and ceiling requirements.
  This implementation also provides a separate unknown-cost opt-in that
  acknowledges there is no enforceable credit ceiling. It requires fresh
  account evidence and binds the exact route, request, settings, scope and
  bounded occurrence. It cannot reuse an older credit authorization or claim
  affordability. Unknown is never zero or a price comparator.
- Empirical samples and quality reviews are useful optional diagnostics. They
  do not expand native capabilities, and their absence does not block a
  schema-valid, currently authenticated, transport-supported request. Do not
  infer first-attempt cleanliness or comparative model quality from transport
  success.

## Read the menu, never guess

Call `provider_menu_summary()` and read
`qualified_cli_video_routes`. For the OpenArt row:

1. Use the retained provider catalog and exact model/mode form to identify
   schema-supported controls. Confirm current authenticated account readiness
   and local CLI transport support for every requested field before dispatch.
   A prior generated-result receipt or quality review is optional diagnostic
   information, never an onboarding gate.
2. `observed_candidates` is the retained provider catalog/form inventory and
   schema-derived local transport view where the form parser succeeds. It keeps
   all 17 observed video model IDs and 45 advertised mode pairs visible,
   including the six union-form captures; when a runtime form parser cannot
   analyze a row, its capabilities must remain withheld. Catalog observation
   alone is not production readiness; a supported route requires valid exact
   schema, current account, and local transport binding. It does not require a
   generated-result receipt.
3. `qualification_candidates` and empirical qualification records are optional
   diagnostics. The provider-qualification pipeline may be used for a separately
   requested test; it is not required before ordinary production. A separate
   benchmark still requires its own approval.
4. `not_live` (fixtures and rejected rows) is never live.
5. Report the billing mode truthfully. Exact-quote mode retains its current
   quote and ceiling requirements. Unknown-cost mode has no enforceable credit
   ceiling; the charge and dollar cost are unknown. Keep it separate from
   Grok's unknown subscription quota.
6. Report `pending`, `holds`, `quarantine`, `unacknowledged_outbox` and
   `errors` as they appear. An unresolved job acceptance (pending, submitting
   or uncertain slot) blocks dispatch as `unknown_job_acceptance_unresolved`.
   An unknown-billing hold on a terminal slot does not block by itself and
   does not invalidate approved footage. Report the economics as incomplete:
   a remaining allowance and fresh exact quote are required in priced mode.
   Unknown-cost mode has no allowance or quote arithmetic.
7. The OpenArt metadata runs no OpenArt CLI and writes no ledger rows. The
   Grok row's status runs the existing read-only `--version`/`--help` probe.
   Grok reports no video models (`model_policy:
   cli_managed_media_unreported`); its agent model is not a video model.

Do not invent model IDs (H3 or any other), native audio, end-frame pins, rates
or defaults. The official OpenArt MCP integration is installed and
authenticated, and its schemas have been inspected separately. That transport
is distinct from the CLI: schema discovery does not implement a local bridge,
qualify results, or migrate existing CLI modes, profiles or approvals. You may
recommend an OpenArt row when its exact schema, current account, and transport
controls fit the shot; explain the fit and label unmeasured
expectations. Recommend a scene-specific fit using supported controls and
available cost evidence, labeling the recommendation as a heuristic. Without
benchmark evidence, do not claim measured comparative quality or acceptance-rate
superiority.

## Talking to the user

Ask for creative options: shot description, duration, aspect ratio, and an
optional start image only when upload is supported and the required
source-transfer billing authorization is present. Build exact roles from the shot board
and bind them as `input_assets` entries with `role`, `source_path`,
`source_sha256` and `upload_id`; `native_params` holds only controls declared
for this exact form and actually transportable. Legacy flat aliases may be
normalized by the adapter, but conflicts fail. Do not ask the user to type job
commands, IDs or CLI flags.

If the shot needs a control the route lacks (an end-frame pin, native audio or
voices, more than one reference image, reference video/audio, or an
element-to-video mode), say so plainly and offer a route that has the control
or a revised creative request. A reviewed board ending composition is a target;
it is not automatically a native final-frame pin. A declared hard
`pinned_final_frame` or handoff requirement is an exact control requirement and
must fail closed on CLI 0.1.1. Never smuggle the missing control in through
prompt wording. Keep approved dialogue lines in the prompt anyway, and disclose
that there is no native-audio guarantee.

## Running an attempt

- Pin the route exactly with `preferred_provider="openart_cli"` and
  `allowed_providers=["openart_cli"]`, inside a strict project. Ungoverned pins
  are refused before any provider call.
- The approved settings (exact model/mode, duration, aspect ratio, start image,
  and resolution only where the observed form and exact preview carry it) pass
  through unchanged. A
  required resolution that no observed form and exact preview carry stays
  unqualified. Server defaults never stand in for it. A strict scope may
  preapprove a bounded batch of exact attempts. A request that changes any
  approved setting is outside that scope and needs a new approval and a new
  quote in exact-quote mode. Unknown-cost mode requires new applicable retained
  authorization and approval. Nothing retries automatically. Preserve any
  creator-specified model preference or goal in `model_selection_intent`; rank
  only inside the explicitly approved pool, and freeze the resulting exact
  request through ordinary preparation and scope approval. Ranking is not a
  quality benchmark or dispatch authority. Read `docs/VIDEO_MODEL_SELECTION.md`.
- Every priced attempt needs a current exact quote and a credit authorization.
  Unknown-cost attempts use the distinct retained authorization and explicit
  no-enforceable-ceiling acknowledgement, with no quote, allowance or
  affordability claim.
  Each attempt launches exactly once. Status, collect and resolve can be
  repeated safely against the original attempt and never launch again. Never
  resubmit an `uncertain` attempt. It stays held with no timeout until
  qualified original receipts resolve it, or provider history does if that
  becomes available. Do not assume provider history exists.
- If the original staged qualification attempt is held because the frozen
  tentative submit path missed an observed field such as `historyId`, use
  registered `openart_account` action `recover_original_submit` with
  `read_only: true`, the original `attempt_id` and the observed
  `json_paths.submit_job_id: "historyId"`. This repair accepts that observed
  top-level submit field only. Never supply a job ID, run a raw CLI recovery or
  submit again. The tool checks the original successful process, frozen request,
  authorization marker and ledger reservation, then reads the fresh account and
  that original creation before retaining an immutable recovery proof. It returns
  only safe schema shape, recognized status values, URL hosts and hashes. If the
  returned ID path cannot be inferred uniquely, keep the hold. An explicit
  declaration must name its unique eligible identifier field; protocol, request,
  error and billing echoes cannot qualify. Repeat the same action for current original-result
  shape, then use `qualify_result` with the observed terminal result paths/hosts.
  The frozen profile and request remain unchanged. Recovery neither settles or
  releases credits nor establishes a ceiling or creative quality.
- Before the first image upload, the provider must have stated in a captured
  response that uploading is nonspending and has no delayed charge.

Review generated clips the same way as any other provider's: current bytes,
semantic review against the shot contract, then selection. Provider
qualification is not creative approval.
