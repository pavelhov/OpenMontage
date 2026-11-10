---
name: spicy-api
description: OpenMontage contract and governed workflow for the Spicy native current-schema video routes.
---

# Spicy API — OpenMontage video

Use Spicy only for an exactly qualified native video route. Read [the provider contract](../../../docs/SPICY_API.md), the current manifest and stage director, the registered `SpicyVideo` tool contract, and the active identity's consent/setup owner before routing work.

## Route check

- Use the exact fresh project/key-bound v2 model observation and schema descriptor; a public/static catalogue is not account readiness. The regression corpus represents 155 of 160 video-labelled schemas, with named analysis/audio/provenance exclusions. Only 139 of those even declare both authored prompt and duration, an upper bound before account, controls, source and authority checks; this is not a factory-readiness count.
- Native v2 controls live in `native_input`. Every media field is populated only from explicit local `input_assets` role/pointer/hash bindings and its once-only committed upload journal. Typed image/video/audio inputs retain exact MIME, size, duration limits and historical transfer-host proof.
- Require authored prompt and positive exact duration for canonical factory work. Source-only transforms, unknown/automatic duration, MOV conversion, remote weights/web/document/analysis provenance and timed keyframe claims remain explicit blockers. Built-in finite LoRA selectors are ordinary controls.
- Top-level native controls and audio choice remain explicit. Deterministically freeze permitted object-array item defaults before quote/hash. Never silently drop an unsupported or conflicting control.
- The legacy `bytedance/seedance-2.0-fast/image-to-video` v1 route retains its original controls and `first_last_frame` compatibility, digests, receipts and recovery. It is not upgraded in place.
- OpenMontage has no standalone Spicy image tool. Standalone images belong to Social Studio; video and attributable project references/returned stills remain within the project and delivery archive.

## Authority and dispatch

1. Start from the selected pipeline or explicitly activated factory. Never bypass the registered `BaseTool.execute(params)` and canonical execution path with direct HTTP or an ad-hoc script.
2. Require the active policy to bind the human account label, `env:` credential reference and credential digest, exact route (v2 includes schema SHA and exact source role/pointer/MIME bindings), per-attempt cap, cumulative cap, and a nonempty exact `qualified_storage_hosts` hostname list. Preserve that host set in source-transfer and original-task authority. The display label is not provider identity. Never expose or persist the API key.
3. Require a current authenticated model/schema observation, at most one hour old, for the same credential binding. Public catalogue data alone is not account readiness.
4. Before source transfer, durably retain the batch intent, source roles, paths and hashes, rights/transfer authority, destination account binding, exact qualified host set, and per-upload recovery state. Upload only reviewed project sources. Use the returned presigned URL and permitted content headers; omit API credentials from the storage PUT. Commit and verify returned SHA-256, byte count and content type before using the returned `spicy://` URI.
5. Quote the complete exact native request. Compare `maxCharge` with the per-attempt cap and cumulative remaining allowance. Charge the full maximum against the cumulative cap for every prior reservation, including settled or failed attempts; do not automatically recycle allowance. A quote lasts five minutes and reserves no funds.
6. Durably store the quote binding, exact create body and idempotency key before creating a task. Pass the quote's `quoteId` and `expectedCost`. The original request/key is replayed only with the same credential for up to 23 hours (inside Spicy's documented 24-hour idempotency window). After that, unresolved acceptance blocks; never send a new key to guess whether the original was accepted.

## Resume and collect

- A known task ID is enough to resume polling/collection. It does not need a fresh quote or unexpired source-upload URI. Use the same bound credential and preserve the original task ID.
- `refresh_settlement(project_dir, attempt_id, client=None)` makes an explicit read-only billing observation for any known original task. After collection, `resume_original(project_dir, attempt_id)` is a zero-API-call local verification of the saved original; if billing was still unsettled, call `refresh_settlement` on that same task. It updates billing observations/incidents only; it leaves the collection-time media proof and conditioning receipt unchanged. Final billing remains unverified until the refresh reports `settlement: settled` with a final cost.
- Poll `queued`, `running`, `succeeded`, `failed`, `expired`, or legacy `canceled`. Task completion is separate from billing: only `settled: true` makes `cost` final.
- For a succeeded task, inspect each `output.assets` item. Wait on `pending`; record `unavailable` as a terminal missing output. Do not create a replacement task to fix an asset-copy or download failure.
- Refresh a temporary output URL only for the same task using its asset key. Fetch the signed URL without API auth. Enforce the configured exact R2 hosts, HTTPS, public DNS, no redirects, response-size limits, and no signed URL in logs.
- Archive every returned video and ancillary still. Partial refreshes cannot shrink known output identity. Multiple videos require an explicit exact asset-key selection; never assume result order. Validate duration and concrete visual controls only on the selected primary. Missing output MIME returns a named metadata blocker until the same original supplies it; never guess.
- `collected_outputs` exposes verified current-byte provenance, including partial archives. `retry_basis` prepares lineage only for failed/expired settled originals; a retry needs new explicit remaining authority, fresh quote and a new counted attempt. Never call raw `/jobs/retry`.
- Retain original provider bytes and provenance. Semantic review/certification, delivery backup, and publishing have their own gates; a playable original grants none of them.

## Validation boundaries

Offline fixtures may exercise request serialization, schema checks, API envelope parsing, quote and cost guards, idempotency replay, polling, pending/unavailable outputs, settlement, URL sanitization and collection. They must never contain real keys, live signed URLs, or live operation IDs. Do not run account observation, source uploads, quote creation against live inputs, or paid generation unless the current user request and identity-specific authority explicitly authorize that exact action. Never infer standing setup from this skill or from installed software.
