# One-shot production factory

This is the compact creator-command path for an identity with an explicitly
authorized standing setup. It reuses the existing OpenMontage project, records,
producer and delivery owners. It does not replace pipeline manifests for other
projects. Generic pipelines keep their own stage, review, approval and route rules.

## Contract

The producer writes one complete plan, prepares its declared images, requests one
video per planned slot, collects those original jobs and assembles the playable
results in story order. Follow the action returned by `make_video`, `advance` or
`complete_action` immediately: once a clip's declared images are ready, prepare
and dispatch it before unrelated boards, then prepare other images while originals
render. Do not wait for every episode image before the first video request.
Each clip uses every declared supported image input. Adjacent shots
may share prepared boundary-photo bytes when that is what the plan declares; this
does not make an unsupported native frame pin available. Unsupported required
controls block the affected slot before dispatch.

Before image prompt authoring, query
`registry.describe_video_inputs(provider, model=None, mode=None, operation=None)`
for the exact selected route. The read-only answer describes start/end pins, timed
middle frames and untimed references, requiredness, known limits, timing rules and
exact native schema/readiness provenance. Missing maxima remain explicitly unknown.
Declare useful images only; optional capacity is not a requirement to fill.

The compact package supports optional shot `roles` entries with `asset_id`,
canonical `role` (`start_frame`, `end_frame`, `timed_keyframe` or `reference_image`)
and `time_seconds` for a `timed_keyframe`. Explicit roles replace
the default local-board consumers. Reuse an existing asset ID to share a boundary
between shots without a second image generation; a shot consuming existing nodes
may have `boards: []`. A semantic payoff board is a literal end pin only through
an explicit `end_frame` consumer. Source identity references remain distinct from
native reference consumers. `build_video_request_templates(package, settings)`
derives native requests from these bindings and rejects unsupported roles/settings
before image attempts. Keep one authored plan and no duplicate binding manifest.
Legacy start-only inputs and retained requests keep their original interpretation
and bytes.

There are no corrective takes, creative revisions, video route fallbacks or board
corrections in this factory. The sole image exception is the creator-authorized
native content-policy fallback described below. Do not add another producer,
controller, timer, daemon or approval loop. Use the existing producer and current
project pointers. A new creator-command episode starts through the identity's
existing `make video` entry; a resume continues that same project's current state.

## Native images and the policy exception

Use native Codex image generation first. An explicit content-policy refusal may use
one OpenArt image for that same board, prompt and references when the current
creator command retains `image_policy_fallback` and the standing OpenArt account,
source permissions and remaining image allowance cover it. Access errors, 403s,
timeouts, lost replies and unknown failures do not qualify. Both the denied native
call and fallback count; never retry the fallback or change the scene to obtain
another attempt. This is an exception in the existing image journal, not an
alternate production workflow.

`prepare_policy_fallback_image` validates the refusal and returns either the exact
generation envelope or the missing reference uploads. Transfer only missing files
through `derive_standing_upload_authority` and the existing OpenArt upload receipt
helpers with `policy_fallback_call_id`; reuse valid receipts for already uploaded
sources. Keep the original `generate_image` return with `import_native_image`.
For an asynchronous result, retain the actual status of that same `historyId`
and collect its downloaded image through `record_openart_image_collection`.
Resume the existing producer to promote the board and prepare its video. Never
rewrite a pending generation return as a completed return.

Retain model forms once in the authorized command; native-success runs need no
extra fallback discovery or approval. The ordinary path remains native boards,
one original video per clip, then assembly and factual issue disclosure.

## Original jobs and delivery

After interruption, inspect and recover the original job using its retained request,
receipt and count. Unknown, failed or terminal jobs never authorize a replacement.
Do not resubmit an uncertain envelope. The existing `advance` / `complete_action`
loop returns the applicable same-job recovery action, including
`poll_openart_original`, `download_openart_original`, `collect_grok_original` or
`collect_openart_original`; complete it with the actual host result. Continue
independent eligible slots within the existing account, source, route and generation
limits.

Once originals are collected, assemble every technically playable clip in planned
order, including semantically poor footage. Report the affected shot, observed
defect, missing coverage and actual duration. Missing or corrupt footage produces
an honest partial cut; no usable clip produces an incomplete result. Findings are
disclosures, not acceptance or repair gates. Preserve original media, rejected or
superseded historical media, records and derived edits.

This factory adds no background music and has no audio-review gate. Keep returned
provider audio as produced. The producer may state “no background music” in the
prompt and disclose an observed mismatch, but must not claim listening or audio
review that did not occur. Optional visual observations and any separate
certification claim follow the identity's QC document; neither gates this cut's
delivery.

## Authority and boundaries

The standing setup and exact creator command bind identity, source and rights,
accounts, routes, transfer limits and generation caps. The existing
`validate_standing_setup` check validates current bindings before new dispatch.
Missing authority or readiness blocks new work; configuration changes do not
prevent collection of already-authorized original jobs. Unknown cost
or quota stays unknown. This document grants no purchase, top-up, expanded source,
account or route authority.

Keep existing historical projects byte-for-byte. Read their saved requests,
receipts, outputs and records through the source-only recovery/collection path;
do not migrate their policies, counters, approvals or media, and do not generate
for them as part of documentation cleanup. Historical evidence remains useful but
does not act as a current instruction.

The factory saves a cut and reports evidence. It does not publish or schedule.
Account delivery, still attribution, backup and publishing confirmation remain
with the identity's delivery owner and Studio policy. The existing
`deliver_standalone` path handles delivery; a queued backup is not a verified
backup. Cut outcomes use the engine's `complete`, `partial`, `incomplete` or
`render_failed` status.

For the Studio entry and identity-owned authority, see Social Studio's
`docs/video-production-factory.md`. For a concrete identity, follow its `AGENTS.md`,
`AUTOPILOT.md`, consent, semantic-QC and delivery owners. For any other production
request, follow Rule Zero in [`AGENT_GUIDE.md`](../AGENT_GUIDE.md) and the selected
pipeline.
