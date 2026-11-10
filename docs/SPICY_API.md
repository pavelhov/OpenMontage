# Spicy native video provider

Spicy is an optional native video provider. Version 2 qualifies an exact model and
its current account input schema. The legacy Seedance Fast image-to-video version
1 remains available with its original controls, request digests, receipts and
same-original recovery. No existing project or original is migrated.

Standalone images belong to Social Studio. Video inputs, generated project
references and returned stills belong to the OpenMontage project. This provider
uses the official `spicyapi` Python SDK; it does not require MCP.

## Official SDK client and runtime

`lib.spicy_jobs.SpicyClient` wraps the official SDK, pinned as `spicyapi==0.3.0`
(PyPI wheel SHA-256 recorded in `requirements-spicy.lock`). The SDK requires
Python 3.11+, so the engine runtime is `.venv311` (see `README.md`/`Makefile`).
On an older interpreter, or with a different SDK version, the client refuses with
`sdk_unavailable`. The registry declares `python:spicyapi` so a missing package is
visible during discovery; the exact version is checked when constructing a client.

The SDK's public named methods own endpoints, authentication, request
serialization and envelope/error parsing (`get_balance`, `get_model`,
`list_models`, `get_usage`, `list_tasks`, `get_task`, `quote_task`, `create_task`,
`create_upload_url`, `commit_file`, `create_download_url`, `purge_task`, asset and
character reads). The engine does not keep a parallel HTTP request path. Engine
policy stays around the SDK:

- SDK automatic retries are disabled (`max_retries=0`). Once-only intents,
  counted attempts, quote caps and original-task recovery stay in the workflow
  journals. An explicit recovery replays the retained frozen body and original
  idempotency key; the SDK never invents a new key or body.
- The SDK receives a guarded byte transport through its public `transport=`
  seam. It enforces the fixed HTTPS API origin, ignores ambient proxies, does not
  follow redirects and bounds responses at 4 MiB. The SDK's default urllib
  transport would follow redirects and use environment proxies.
- Timeouts, network errors and 408/5xx responses (even with a business code)
  are uncertain outcomes, never definite rejections. Errors are sanitized; request
  correlation IDs are captured per request (`ContextVar`) so concurrent calls on a
  shared client cannot exchange metadata.
- Create and quote bodies are checked for their exact top-level fields before
  being passed to the SDK; unknown retained fields fail closed instead of being
  silently dropped.
- The SDK's `upload_file` bundles ticket, PUT and commit, so source uploads keep
  separately journaled ticket, signed-storage PUT and commit phases. The signed PUT
  and the bounded result download GET remain engine-owned: exact qualified R2
  hosts, pinned public DNS/TLS, no credential header and exact byte/hash checks.
- The SDK serializes JSON with its own key order and ASCII escaping. Those wire
  bytes may differ from the legacy client, but the decoded body, the retained
  digest and the idempotency key are unchanged, which is what idempotent replay
  uses. Existing v1/v2 requests, approvals, quotes and recovery records stay valid.
  The [provider idempotency contract](https://docs.spicyapi.ai/docs/idempotency)
  explicitly makes object field order and whitespace immaterial.
- `connection-check` and model listing send `includeSchema=1` through the SDK.

## Current-schema video routes

`lib.spicy_routes` qualifies text-to-video, image-to-video, reference-to-video,
video edit/extension/upscale, character animation, avatars, lip sync and video
translation when the exact schema can represent the request. The sanitized
22 October 2026 regression corpus has 160 video-labelled rows: 155 have a
representable video-input schema. Two analysis operations, Foley video-to-audio,
and two edits requiring unavailable analysis-task provenance are blocked. This
count is schema coverage, not account entitlement or 155 factory-ready routes.
Of those 155, only 139 even declare both prompt and duration fields: 53 I2V,
18 reference, 31 T2V, 3 edit and 34 extension routes. This is an upper bound
before actual controls, sources, quotes, account eligibility and authority checks.
The remaining 16 source-led/fixed transforms lack one or both authored fields.

The canonical factory additionally requires its authored nonempty prompt and a
positive exact `duration_seconds`. Source-only transforms without that contract,
missing duration and automatic `-1` duration remain explicit factory blockers.
Ordinary schema qualification cannot certify creative quality or output timing.

Explicit `observe_account_v2(project, client, model, credential_ref)` reads the
exact model and USD balance. Its retained observation binds the project, exact
model/schema, credential reference and digest, and expires after one hour. A
public or sanitized catalogue snapshot never enables dispatch. No observation is
silently refreshed. The policy's v2 route is exact
`{model, mode, schema_sha256, source_roles}`; each source-role entry binds
`{role, pointer, media_kind, mime}`. Legacy two-field route allowances do not
authorize a v2 request.

`native_input` contains the exact native controls. Sources are supplied only as
`input_assets: [{role, pointer, source_path, source_sha256?}]`, with RFC 6901
pointers such as `/image_url`, `/reference_image_urls/0`, `/video_url` or
`/audio_url`. Local bytes are probed and hashed. Only a committed once-only
upload journal supplies the `spicy://` URI; raw URLs, data URIs, arbitrary existing
provider URIs, source placeholders and undeclared nested fields are refused.
Image PNG/JPEG/WebP/GIF, video MP4/WebM, and audio MP3/WAV are supported subject to
the exact model's MIME, byte, individual-duration and total-duration limits.
MOV conversion is blocked because no original-to-committed conversion proof is
implemented. Timed keyframe claims remain unsupported: ordered intermediate
images do not imply timestamp pins.

The bounded schema evaluator supports nested objects/arrays, exact numeric
steps, conditionals and nonrecursive local references. Unknown assertions,
remote/dynamic/recursive references and changed `$id` scopes fail closed.
Vendor `x-ui` and MIME-array `contentMediaType` declarations are interpreted as
metadata. Object-array item defaults (for example a built-in LoRA strength) are
materialized deterministically before quote/hash binding. Top-level controls
remain explicit; a schema exposing `generate_audio` requires an authored boolean.
Conflicting carrier aliases are refused. Finite built-in LoRA enums are allowed;
external weights, web/document sources and analysis IDs remain non-emittable
controls without disabling an otherwise usable optional route. Kling intermediate
frame counts and custom-shot duration sums have explicit additional checks.

## Legacy v1 route

The unchanged v1 route is
`bytedance/seedance-2.0-fast/image-to-video`, mode `image_to_video`, with an
optional `first_last_frame` operation. It accepts authored prompt, duration 4–15,
480p/720p resolution, an explicit audio choice, supported aspect ratio, and local
first/last frames. It does not acquire v2 controls merely because its model ID is
also present in a current catalogue.

## Authority and account binding

Spicy uses quoted USD authority and currently requires strict enrollment plus a canonical scope derived from the active quoted policy. A generic production choice is not provider authority. For the one-shot factory, the identity must have its valid standing setup and `make_video` command; for a generic manifest pipeline, exact policy and manifest gates still apply. The active policy must bind the human-readable account label, an environment credential reference and digest, exact model/mode, per-attempt USD maximum, cumulative USD maximum, and a nonempty `qualified_storage_hosts` array of exact lowercase R2 hostnames. Retain the same host set in the source-transfer intent and original-task authority. The account label is a name for people, not a stable provider account ID. Keep the key in the designated environment variable or credential store; never write it into policy, logs, command output, fixtures, source control, or a provider request record.

Before a new paid attempt, the canonical production workflow must have a current authenticated account/schema observation and a quote for the exact request. The quote is account/key-bound and valid for five minutes. Compare its `maxCharge` with both the per-attempt limit and remaining cumulative allowance. The factory conservatively counts every prior Spicy reservation against the cumulative cap, including settled or failed tasks; capacity is not automatically recycled. Quotes do not reserve provider balance or guarantee future availability. `expectedCost` must bind task creation to the quoted price.

Typed source upload is a separate data-transfer side effect that happens during preparation, before a production attempt exists. New-source upload requires activated standing source-transfer authority in addition to the production route authority; a generic manifest alone does not grant transfer authority. Before uploading, the canonical source-transfer batch journal must durably retain exact batch intent, approved sources and SHA-256 hashes, standing transfer authority, target account/credential binding, qualified host set, expected roles and each upload's recovery state. Transfer authority has its own batch count and uncertainty rules. A source upload ticket is short-lived; use only its returned PUT URL and headers, with no API bearer token on the storage request, then commit and verify the returned byte count, MIME type, and SHA-256 before using the `spicy://` URI. Preserve the local original. Never let a pending or uncertain upload silently start a second upload.

`reconcile_rejected_ticket_v2` can explicitly close a local `upload_ticket_invalid`
rejection only when the durable journal proves it happened before PUT (no file ID,
ticket progress or receipt). It preserves the failed journal and its hash. A new
ticket still needs a different counted, authorized source-transfer batch; the old
batch is never recycled. Ticket-request timeouts and any PUT/commit progress remain
unresolved and cannot use this recovery. Provider UTC timestamps may include up to
nine fractional digits; the parser accepts those while retaining strict UTC checks.

Create tasks only through the registered `SpicyVideo` tool and canonical OpenMontage execution/factory path. Tools follow the engine's strict `BaseTool.execute(params)` contract. The provider adapter cannot create its own user consent, widen route/source/account authority, or replace the canonical attempt journal. Do not introduce a new background runner, fallback route, automatic retry, image tool, or alternative governance path.

## Original-task recovery and result handling

Persist the exact request, quote ID, idempotency key, credential binding, and acceptance state durably **before** the first createTask request. If the response is lost, replay only the exact same request under the same API key and idempotency key within the engine's conservative 23-hour window (Spicy documents same-key replay for 24 hours). Preserve the original task if local source files or source-upload URIs later expire: a known task can still be polled and its outputs collected with a fresh signed URL. A quote expiring or source URI expiring never authorizes a new task. Once safe same-key replay is no longer available and acceptance is uncertain, stop for explicit reconciliation; never manufacture a fresh key to guess whether the provider accepted the old request.

Poll the original task via `GET /api/v1/jobs/recordInfo?taskId=…` using the same API key. States include `queued`, `running`, `succeeded`, `failed`, `expired`, and legacy `canceled`. Terminal task state and billing settlement are separate: `cost` is final only when `settled` is true. A succeeded task can still have assets marked `pending` while storage copy finishes, or `unavailable` when an output is gone. Retry a read or refresh the same task's download URL where documented; do not regenerate as a collection retry.

`refresh_settlement(project_dir, attempt_id, client=None)` is an explicit read-only billing observation for any known original task. After collection, `resume_original(project_dir, attempt_id)` verifies the saved local original without an API call; if billing was still pending, use `refresh_settlement` to observe the same task. It updates billing observations and any sticky billing incident only; it does not change the collection-time media proof or conditioning receipt. Pending or unavailable remote assets and expired signed URLs do not invalidate a verified local original. Treat final billing as unverified until the helper returns `settlement: settled` with a final cost.

Result asset URLs are signed, temporary, and must be fetched without the API bearer token. Refresh with authenticated `POST /api/v1/common/download-url` and the original `taskId` plus asset `key`, then make a no-auth GET to the returned URL. Upload and result links use account-specific `*.r2.cloudflarestorage.com` hosts in the documented environment. The adapter must enforce its exact qualified hosts, HTTPS, public DNS, no redirects, bounded responses, and URL-query redaction; do not broaden the allowlist from a response or accept arbitrary input URLs.

Keep original provider bytes and canonical task/provenance records. Record the actual model, native controls, source hashes/roles, original task ID, cost/settled state, MIME/dimensions/duration, local output hash, and review status. A playable file is not semantic QC, certification, or publishing approval. Video stills attributable to the episode are archived beside delivery under `stills/` with source path, SHA-256, role and status. Queue the existing backup helper for the delivery and OpenMontage project at authorized closeout; queued is not verified. Identity consent, project review, delivery, certification, and publishing retain their existing separate owners and gates.


## Multiple outputs and explicit collection limits

V2 retains each observed keyed video and ancillary image, including asset key,
MIME, provider role when supplied, probed dimensions/duration, byte count and
SHA-256. Videos are first archived under `<output-stem>.outputs/`; ancillary
images are stored under sibling `stills/`. An asset refresh cannot erase prior
keys or known metadata. Keyless pending slots retain a conservative inventory
floor, so an omitted pending result cannot make another video the primary.

A single complete video can become the primary. Multiple videos require
`select_primary_output(project, attempt_id, asset_key)` after collection; the
engine never chooses the first result. Canonical `next_action`, `advance` and
original resume stop with a blocked choice and the verified exact asset keys;
they do not keep polling while the choice is ready. Call the selection helper
with the chosen key, then use normal original resume to reconcile that same
counted attempt. The selected primary must satisfy exact
authored duration, explicit audio choice, concrete width/height and concrete
aspect-ratio controls. Existing resolution checks remain, with one explicitly
unverified case: the v2 `bytedance/seedance-2.0-fast/image-to-video` route's `720p`
plus `adaptive` controls. Its retained schema and public provider documentation
name that tier without defining pixel geometry. A short-edge equality or an
invented pixel-area tolerance cannot verify it. Technically valid originals can
therefore close out as a draft with `primary_control_verification.resolution`
marked `unverified`, the observed dimensions, and a warning. That record is not
evidence of resolution compliance or final certification. This narrow v2 case
does not change v1 receipts or other model/tier checks; `adaptive` also makes no
verified claim that the source image's aspect ratio was preserved.
Previously completed v2 originals remain readable with their retained journal
and receipt unchanged. When that historical receipt predates the verification
field, a local read derives the unknown-resolution evidence separately from the
frozen native request and verified current bytes. The returned
`primary_control_verification_provenance` explicitly labels that evidence
`derived_current_read`; it is not retroactive collection-time evidence.
Joined extension footage and a newly
generated segment may differ in duration; both originals are archived before
primary selection. Every publication retains a durable intent and refuses
unreceipted destination files.

Ancillary images permit bounded same-media-kind MIME reconciliation. The native
asset's declared MIME and opaque key are preserved exactly. Collection accepts
only the supported image transport types or `application/octet-stream`, limits
image bytes to 10 MiB, and requires supported signature sniffing plus independent
image verification and decoding before publication. It retains the normalized
HTTP transport MIME, sniffed/decoded MIME and named mismatch anomalies under
`mime_reconciliation` in the original intent, output and receipt. The output's
`mime` describes verified local bytes. Canonical archive filenames continue to
use the original declaration's extension for path stability; consumers must use
the verified MIME when preparing a delivery filename. Neither a key's suffix
nor a transport header determines actual media type. Video MIME matching and the
shared downloader's default MIME policy remain strict; redirects, host/DNS/TLS,
byte/hash, original-task and immutable-request checks remain in force.

`collected_outputs(project, attempt_id)` provides credential-free current-byte
proof for collected v2 originals, including partial archives awaiting selection.
The Studio delivery bridge uses it to copy attributable returned images into
its still archive. It grants neither semantic review nor new dispatch authority.

The provider contract permits omitted output MIME/dimensions/duration. Dimensions
and duration are derived from local bytes. A keyed asset without MIME currently
returns `output_metadata_incomplete`; no video MIME or primary ordering is guessed.
A later same-original task observation supplying MIME can unblock collection.
Unsupported output MIME returns `blocked_output`. Pending storage, unavailable
outputs, an invalid media proof or missing metadata never authorizes regeneration.

`retry_basis(project, attempt_id)` prepares parent provenance only for a known
failed/expired, settled original without a billing anomaly. It makes no HTTP
request. A future retry is a new explicitly authorized, freshly quoted, counted
original under remaining authority; the factory retains its first-pass guards.
Raw `/jobs/retry` is unavailable because its `{taskId}` body cannot enforce the
required quote ID and maximum charge. No retry is automatic.

## Native account utilities

The CLI delegates to `lib.spicy_account`; local documentation search and public
service status do not resolve a credential:

```sh
./.venv311/bin/python -m lib.spicy_jobs account docs quote
./.venv311/bin/python -m lib.spicy_jobs account status
./.venv311/bin/python -m lib.spicy_jobs --credential-ref env:MY_SPICY_KEY account models --modality video
./.venv311/bin/python -m lib.spicy_jobs --credential-ref env:MY_SPICY_KEY account balance
```

It also provides native usage/task inspection and bounded waits, read-only asset
and character queries, exact-key private download URL handoff, retry handoff and
separately confirmed purge preparation/reconciliation. Signed URLs are written
only to a new private file under an owner-only directory with the explicit URL
handoff interlock. Destructive execution additionally requires the owning
workflow's locked canonical-original proof; administrative input cannot replace
that proof. Maintenance tests use synthetic keys and transports and grant no
live account, upload, quote, purge or paid generation authority.

## Use and verification

The native client accepts `env:<NAME>` credential references. A present environment
variable always wins, including an explicitly empty value (which fails). Only the
default `env:SPICY_API_KEY` may fall back, when the variable is absent, to
`~/.config/spicyapi/credentials.env`. Custom names never fall back. The file contains
only one literal `SPICY_API_KEY=...` assignment; blank/comment lines, surrounding
whitespace and matching single/double quotes are accepted. Shell commands,
interpolation, duplicate/unknown fields and multiline values are refused. The
immediate `spicyapi` directory and regular file must belong to the current user,
have owner-only access (normally directory `0700`, file `0600`), and not be symlinks.
The UTF-8 file is limited to 16 KiB. Keep it outside the project and source control.

Resolution does not change the process environment or the retained credential
reference/digest format. Each request rechecks the current credential; changing
the file or environment invalidates an existing client's binding. Retain the
creating key through original-task recovery and collection: another key cannot
read its tasks, even when both keys belong to the same team.

An explicitly requested, identity-free connection diagnostic is available:

```sh
./.venv311/bin/python -m lib.spicy_jobs connection-check
```

It uses the native client for exactly `GET /api/v1/chat/credit` and
`GET /api/v1/models?includeSchema=1`. Output contains validated USD balance
fields, catalogue counts, and enablement/availability/schema compatibility for the
exact supported video model. Available and total balances may be negative after
approved credit usage. This command does not upload, quote, generate, write project
account observations, activate identity authority or prove production readiness.
It exposes neither credentials nor raw model records/provider messages. To select
an existing custom environment reference, use `--credential-ref env:<NAME>`.

For ordinary pipeline production, select the route through the registry and the current manifest/director. For the one-shot factory, follow `docs/production-factory.md` and the identity's explicitly activated standing setup. The default is no live account observation and no paid dispatch. A public schema fixture, install, key presence, or quoted budget does not activate an identity or authorize a charge.

The implementation's offline fixtures may validate request construction, envelope parsing, quote binding, same-original recovery, URL redaction, and output-state handling without network calls. They do not prove account access, pricing, billing, provider quality, latency, or route behavior. Live observation is authenticated but read-only; generation is paid and requires the exact existing authority and current approval/standing setup. Keep those steps distinct.

For a future task that is explicitly authorized by the selected identity's active setup, use the credential reference and exact storage hosts already present in that approved provider policy. The following is only the authenticated, read-only account observation step; it does not configure a credential, authorize a source transfer, quote, or create a task:

```python
from lib import production_execution as execution
from lib.spicy_catalog import VIDEO_MODE, VIDEO_MODEL, observe_account
from lib.spicy_jobs import client_for_credential, credential_binding

credential_ref = approved_provider_policy["credential_ref"]
qualified_hosts = approved_provider_policy["qualified_storage_hosts"]
if {"model": VIDEO_MODEL, "mode": VIDEO_MODE} not in approved_provider_policy["routes"]:
    raise ValueError("Spicy route is outside the approved provider policy")
binding = credential_binding(credential_ref)
if binding["credential_digest"] != approved_provider_policy["credential_digest"]:
    raise ValueError("Spicy credential differs from the approved provider policy")
client = client_for_credential(credential_ref, qualified_storage_hosts=qualified_hosts)
observation = observe_account(project_dir, client, VIDEO_MODEL)
```

Then continue through the canonical factory preparation and `execution.complete_action(project_dir, current_action, outcome=...)` flow in [the production factory guide](production-factory.md). Use its prepared inputs and retained quote/authority rather than calling raw submit helpers. For an already-created task, use `resume_original(project_dir, attempt_id)` to recover the same original and `refresh_settlement(project_dir, attempt_id, client=None)` for a read-only billing observation; neither is a new generation path.
