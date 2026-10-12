# One-shot production factory

This is the compact creator-command path for an identity with an explicitly
authorized standing setup. It reuses the existing OpenMontage project, records,
producer and delivery owners. It does not replace pipeline manifests for other
projects. Generic pipelines keep their own stage, review, approval and route rules.

## Contract

The producer writes one complete plan, prepares its declared images, requests one
video per planned slot, collects those original jobs and assembles the playable
results in story order. Each clip uses its declared image input. Adjacent shots
may share prepared boundary-photo bytes when that is what the plan declares; this
does not make an unsupported native frame pin available. Unsupported required
controls block the affected slot before dispatch.

There are no corrective takes, creative revisions, route fallbacks, board
corrections or extra image generations in this factory. Do not add another producer,
controller, timer, daemon or approval loop. Use the existing producer and current
project pointers. A new creator-command episode starts through the identity's
existing `make video` entry; a resume continues that same project's current state.

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
