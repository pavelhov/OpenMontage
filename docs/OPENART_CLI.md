# OpenArt subscription CLI (guarded video route)

OpenMontage owns the governed execution; the CLI and MCP are provider connections. Select the connection during episode proposal by matching the required controls to current route and agent-environment availability. The official CLI 0.1.1 remains useful for standalone automation and simple text-to-video or start-frame image-to-video. MCP uses the agent-installed connector and supports the full controls declared by each exact form, including end frames, reference roles, or audio where available. This difference describes controls, not better quality for the same model. Never silently switch accounts, billing routes, or transports.

OpenMontage drives the official OpenArt CLI (`openart`, verified version 0.1.1, see
https://github.com/OpenArt-AI/cli) as an explicit-only sibling of the Grok CLI route.
The transport is `tools/_openart_cli.py`. Setup, qualification and recovery go
through `tools/openart_account.py`. Generation goes through the governed adapter
`tools/video/openart_cli_video.py`. Generation is possible only through the
guarded path below. Dispatch runs automatically only inside an approved strict
scope with a matching billing-mode authorization and a fresh account refresh. It never
retries automatically and never falls back to another provider.

## Current guarded capabilities

- **Account discovery succeeded; empirical evidence is optional.** Fresh
  nonspending probes on 2026-10-06 at 23:51 and 23:55 UTC succeeded. The later
  response carried a nonempty `user.uid`, a `plan` string and integer `credits`;
  identity values remain private. No explicit authentication boolean was
  returned. These observations supersede the earlier login-required response.
  No generation, reservation or upload ran. Account access alone does not make
  a route production-available. The menu derives
  `account_discovery` from retained account evidence. A retained row never replaces the fresh account
  refresh that every dispatch requires. On October 7, a separate bounded
  qualification run generated and collected one reference-free PixVerse V6
  clip. This is optional empirical evidence for that test, not a prerequisite
  for other schema-supported production routes or evidence of creative quality.
- **Binary.** The official binary 0.1.1 is verified (`version --json`).
- **Observed catalog and mode capabilities.** The retained October 7 read-only
  catalog sweep exposes 17 video model IDs and 45 advertised model/mode pairs
  through `observed_candidates`. Current exact form/schema, authenticated
  account identity and native transport support establish production
  eligibility; prior generated-result evidence is optional. Each observed pair
  keeps its catalog status, form status and
  `native_capabilities` when its captured form and transport surface are
  recognized. The generic capability helper handles object forms and root
  `anyOf`/`oneOf` branches; the coverage tests exercise all six captured union
  forms through that helper. Runtime catalog rows remain visible even when a
  form cannot be parsed, with capabilities withheld. These candidate rows are
  schema evidence, not a quality claim. A form plus matching native transport
  support is sufficient for route readiness when the current account is
  authenticated. The complete receipt-linked structural coverage is in the
  [dated controls note](implementation/2026-10-07-openart-model-controls.md).
  Candidate details and enum/default bytes are kept bounded or hashed in public
  menu data; inspect the private receipt path only through registered actions.
- **CLI 0.1.1 transport.** The verified video command selects `text2video` and
  `image2video`, and carries prompt, duration, aspect ratio, resolution and one
  `--image` start frame. That flag selects image-to-video and binds only to the
  form's start/first-frame role. The CLI has no generic native-parameter flag,
  end-frame pin, multi-reference image/video/audio argument or native audio
  setting. Endpoint fields stay visible with an unsupported binding; they are
  rejected before reservation and dispatch. For example, H3 Turbo's observed
  image form has start and end frame fields, but only the start image binds in
  the local capability helper. A guarded preview observed that `--image`
  produces `params.startFrame` with `label`, `type` and `url`, but no `id`.
  H3 Turbo's captured form requires `type`, `id`, `url` and `label`, so its
  helper-level role binding is not an exact compatible preview. The exact request body for H3 Turbo image-to-video is unsupported because its
  required `id` field is absent. `element2video` appears in the provider catalog
  but cannot be selected by CLI 0.1.1. These are capability distinctions, not
  claims that provider endpoints lack those features.
- **Uploads.** Image-to-video uploads stay refused until the provider itself
  states, in a captured response, that uploading is nonspending and has no
  delayed charge. That statement must exist before the first upload. A caller
  claim or an unchanged balance does not count.
- **Priced approval.** Each paid attempt in exact-quote mode needs a current quote for the
  exact settings (`quote_required`), kept as a raw private receipt. A quote for
  other settings is not reused. The observed model/mode cost responses quote
  5s, 16:9, 480P and one output: Max 125 credits and Turbo 75 credits. The
  provider says prices vary with settings and are finalized at generation time.
  Those responses do not establish the required exact 768P quote or conservative
  ceiling, and the proposed benchmark remains blocked before spending.
- **Unknown-cost approval.** This implementation provides a separate, explicit
  opt-in for an attempt when an enforceable OpenArt credit ceiling cannot be
  established. Under Auto-continue, the separately approved policy must bind
  the account identity digest, exact model/mode routes, and the explicit
  `no_enforceable_credit_ceiling` acknowledgement. The active unknown-cost
  policy accepts fresh evidence only; rooted derivation creates an authorization
  bound to the exact request, native settings, account, profile, references and
  one bounded occurrence. Callers cannot supply authorization IDs or
  authorization objects. Exact-quote policies do not migrate. The charge and
  USD cost remain unknown; this does not establish affordability. The existing
  total, per-shot and repair attempt caps apply, and failed or unresolved
  attempts count against them. An unpriced account claim shares the account's
  single active slot with priced claims. An uncertain original job remains held
  and is never resubmitted; terminal proof can release the slot while billing
  remains unknown. Balance changes are not attributed to a particular job.
  Completion reports keep priced `openart_credits` totals exact-only and put
  the unknown branch in a separate `openart_unknown_cost` summary with
  `cost_status: "unknown"`; each attempt's requested charge is also `unknown`.
  Current CLI transport reference verification accepts only its existing
  verified `--image` upload binding; canonical role-bound `input_assets` are
  rejected as unverified transport. Source-bound unknown-cost I2V is therefore
  not an available qualified path.
  Earlier authenticated account evidence showed 40 credits. A separate 50-credit
  price response was associated with default 5s/540p settings; it does not price,
  prove affordability for, or prove insufficiency of the 1s/720p request.
- **Optional empirical result.** The October 7 qualification record
  documents one real, collected PixVerse V6 reference-free text-to-video sample:
  1 second, 16:9, 720p, yielding 1280×720 H.264 at 24fps and 1.041667 seconds
  of decoded video. There was one original submission, no repair and no upload.
  Transport, result reconciliation and output bytes are verified; creative
  quality was not reviewed. Do not claim quality testing or certification.
  The Free account observation is specific to that run, not a general current
  entitlement or affordability claim. Unknown billing still has no enforceable
  ceiling and no USD value. H3 Max/Turbo and all observed image-to-video modes
  remain unqualified for live output, including upload billing and exact native
  image wire mapping. Cast references, dialogue, voices,
  multiple references and ending-frame controls also remain unqualified. See the
  [dated sample record](implementation/2026-10-07-openart-live-720p-test.md).
- **Optional staged diagnostics.** A profile moves through `inspected`, then
  `pre_submit`, then `full`. Inspection and `pre_submit` rows are
  diagnostic states. A `full` profile records optional real-result evidence
  from one original, separately credit-authorized attempt. Fixture profiles are
  never live and do not grant production readiness.
- **Governed jobs.** Each attempt launches exactly once, inside strict project
  governance with a ledger reservation. Status, collect and resolve can be
  repeated safely against that original attempt, and they never launch again.
  An ungoverned OpenArt pin is refused before any provider call.
- **Private ledger.** The credit ledger is a real SQLite database in private
  state outside Git (see Private state). Menus and dry runs read it only through
  `provider_credit_ledger.read_existing_snapshot()`. That call never initializes
  or migrates the ledger and writes no ledger rows. An absent state root creates
  no files. For an existing database, SQLite may still update its own WAL/SHM
  reader bookkeeping files.
- **Unknown outcomes.** An uncertain submission stays `uncertain` with its credit
  hold. Nothing expires it on a timer, and it is never resubmitted
  automatically. It is resolved only from qualified original receipts, or from
  provider history if such history becomes available. OpenMontage does not
  assume provider history exists.
- **Approval modes.** Strict approval is the default. A strict original scope
  can preapprove a bounded batch of exact attempts, so a new prompt per attempt
  is not mandatory. Optional Auto-continue requires a retained, user-approved policy and an active
  decision-log entry. Each dispatch derives one exact scope, rechecks the
  approved planning, native controls, preparation review, attempt caps and
  applicable billing authority: exact quote and remaining credit allowance in
  priced mode, or separate no-ceiling evidence and authorization in
  unknown-cost mode. Invalid, changed or revoked policy authority stops
  continuation. It never resubmits an unknown job.

The preflight menu (`provider_menu_summary()["qualified_cli_video_routes"]`)
shows current readiness truthfully. `observed_candidates` carries the retained
catalog inventory and schema-derived transport capabilities. A current exact
form, authenticated account and supported native CLI binding establish
production readiness; empirical result evidence is optional and reported
separately. The menu also reports account stages,
pending jobs, holds, quarantine and errors. The adapter's model catalog reads each actual-qualified `full` profile's
reference-only receipts (`{kind, receipt_id, receipt_sha256}`) from private
state, verifies their digests and reapplies the form and preview parsers. It
shows only safe creative values (duration, aspect ratio, resolution); prompts,
image URLs and any other parameter appear only as `value_redacted` plus a
SHA-256. A profile whose receipts cannot be verified drops out of the empirical
profile catalog and reports `model_catalog_unverified` or `model_catalog_error`;
it cannot supply empirical result evidence. The separate readiness check uses
the current native schema, authenticated account and exact CLI binding.
A `pre_submit` profile with an actual binary, authenticated account, exact
form and native dry-run binding is production-ready. A `full` profile adds
optional empirical result evidence. Fixtures are never production-ready. A
resolution flag present in argv but absent from the effective preview is
reported as `argv_flag_without_effective_preview`, with no guessed support.

`dispatch_readiness` is separate from qualification, because a qualified model
does not make an account available. It always requires a fresh prelaunch
account refresh. A pending, submitting or uncertain job slot blocks it
(`unknown_job_acceptance_unresolved`), as do quarantine and unacknowledged outbox
events. A retained unknown-billing hold on a terminal slot does not block by
itself. In priced mode it marks economics as incomplete
(`incomplete_unknown_billing_hold`) and requires a remaining allowance and a
fresh exact quote. Unknown-cost mode has no allowance or quote arithmetic.
Holds and quarantine
are filtered to the exact OpenArt account. Facts from other providers never
enter the OpenArt row. Billing is listed in credits with
`usd_cost_status: unknown`.

The Grok CLI row is reported separately with `model_policy:
cli_managed_media_unreported`, `model_selection: not_supported`, no model list
and billing `subscription_quota_unknown`. The pinned Grok agent model is not a
video model and is never shown as one. Grok controls are operation-specific
(native audio, voices, reference image, first/last frame) according to the
actual tool.

The OpenArt part of the menu runs no OpenArt CLI and creates or mutates no
ledger rows. Grok `get_status` does run the existing read-only `--version` and
`--help` compatibility probe (`status_probe: readonly_cli_version_and_help`).
The menu recommends no default and never offers a paid API fallback.

## Reference-free and provider qualification paths

Reference-free work has two scopes. The existing project-level
`reference_mode: "reference_free"` still describes an all-text, noncast
contract. A board-backed episode can also mark an independent shot with
`shot.reference_mode: "reference_free"`; that shot must have no local cast,
visible speakers, dialogue, asset IDs or upstream sources. The episode retains
its board-backed project payoff, cast references, review and other ordinary
shot requirements. The marked shot is prepared as OpenArt text-to-video only,
with no image/reference/native-audio controls. A required frame pin or manifest
reference/handoff obligation blocks it. This permits a standalone OpenArt
cutaway beside a separate referenced Grok shot; it does not make OpenArt a
replacement or fallback for the same shot's cast, dialogue or reference
continuity. Both routes still require their own approved exact scope. Unknown-
cost authority does not relax this boundary.

A reviewed ending board states the intended final composition. It does not by
itself require an exact native final-frame pin. If a scene card or approved
handoff declares `pinned_final_frame: {required: true}` (or an equivalent
requirement ID), that is a hard native-control obligation: the submitted
`last_frame` must match the approved target bytes and role. CLI 0.1.1 has no
end-frame transport, so that request is blocked before reservation. A
start-image request may preserve its reviewed source and ending target in the
shot contract, but it cannot claim the exact ending frame is guaranteed.

The provider-qualification pipeline is an optional diagnostics/testing path
with prepare and generate stages; it is not production onboarding. Preparation
has a human gate bound to the exact prepared packet. The packet can describe a reference-free request or a source-bound
request with `native_no_reference: false` and `input_assets` entries carrying
each approved `role`, project-relative `path`, source `sha256` and retained
`upload_id`. The validator checks those exact source bytes, native preview,
profile and frozen request. CLI 0.1.1 currently transports one start image;
end-frame, multireference, reference-video/audio and native-audio inputs stay
blocked. The provider's captured response must still establish nonspending and
no delayed charge before any upload, and the exact provider image wire key must be supported by the selected native
transport before a referenced route is usable. Current native reference
verification also rejects role-bound `input_assets` as an unverified transport,
so that binding remains blocked. The packet shape alone does not establish
transport support. Generation uses the original registered
`openart_cli_video` and `openart_account` paths, permits one original generation
and allows no repairs. Status, collection and recovery may repeat against that
original attempt without resubmission. This validates transport and
reconciliation behavior; it does not establish creative quality. Its absence
does not block ordinary production when current schema, account, transport and
ordinary governance checks pass. The separate October 7 sample is
reference-free and documented in the dated record.
See the [approved local plan](plans/2026-10-07-feat-openart-unknown-cost-plan.md)
for scope and acceptance criteria.

Current nonspending discovery evidence, command receipt digests and remaining
qualification gates are retained in
[`2026-10-06-openart-live-contracts.json`](implementation/2026-10-06-openart-live-contracts.json).
That sanitized report contains no raw account values, credentials or signed URLs.
The later 45-form sweep is separately recorded in the dated controls note and
its sanitized test fixture. It does not replace account refresh or a full
result-contract qualification.

For explicit-only CLI providers (such as `openart_cli`), the video selector
treats `preferred_provider` as an explicit pin. Ordinary provider preferences
keep the existing scoring behavior. When the preferred provider is
explicit-only and `allowed_providers` is anything other than exactly that
provider, the scopes are crossed and the request fails as `not_dispatched`
before any provider call. A singleton pin that asks for an unsupported control,
or an unavailable or unqualified model, is rejected before scoring and never
falls back.

Missing controls stay visible. The v0.1.1 surface can send one uploaded first
image with `--image`; it has no end-frame, second/multiple reference, reference
video/audio or native audio control. A hard ending-frame or other unsupported
reference requirement fails before any credit reservation with
`missing_controls` or a typed native-transport refusal. A prompt rewrite cannot
stand in for an approved frame or reference, and the route never substitutes
another provider. Form-supported but CLI-unavailable controls remain visible in
`native_capabilities` with an unsupported binding.

## Native controls and input binding

Use `native_params` only for values declared by the exact model/mode form and
marked expressible by the installed CLI surface. For CLI 0.1.1, the only
ordinary settings are `duration`, `aspectRatio` and `resolution`; prompt remains
the top-level prompt input. The canonical media shape is `input_assets`, a list
of `{role, source_path, source_sha256, upload_id}` records. Roles include
`first_frame`, `last_frame`, `reference_image`, `reference_video` and
`reference_audio`, but declaring a role does not make it transportable. Current
The local CLI binding supports one `first_frame`/`startFrame` in
`image2video`; that mapping does not by itself prove the exact provider request
body or result path. All other roles are refused unless a separately verified
transport surface later supports them. Paths, source-byte digests and retained upload proofs stay bound to the
exact request. Existing flat aliases such as `image_path`,
`last_image_path` and `reference_image_paths` normalize to the same canonical
shape; disagreement between aliases and `input_assets` is rejected.

The governed shot flow starts with the approved board and requirements, selects
an exact qualified provider/model/mode that supports those requirements,
freezes the exact prompt, native controls and source bytes, then obtains the
applicable billing authorization and bounded attempt scope. One original job is
submitted through the registered tool and its result is collected once. Review
checks the actual bytes and shot contract, then accepted continuity can feed
later shots. An endpoint schema, candidate route or original transport success
does not skip approval or review. A hard last-frame requirement cannot be
replaced by prompt wording on CLI 0.1.1.

## Official OpenArt MCP discovery

The official [OpenArt MCP page](https://openart.ai/mcp/) advertises broader
catalog access and start/end image controls through separate OAuth
authentication against the same credit account. The official integration is
installed and authenticated; inspected schemas cover 17 video models and 45
modes. The retained schema-only observation is
`/Users/pavel/.openmontage/openart/mcp-discovery/2026-10-07-schema-observation.json`
(SHA-256 `d407e39696e0d0ddef64e1a82d08cfd972d4e7d84236a02437b5be75b0fc856f`).
H3 Turbo image-to-video requires `startFrame.type`, `id`, `url` and `label`,
with optional `endFrame`; MCP `generate_video` accepts full params. The local
MCP route uses the exact native schema and authenticated account for request
readiness; paid result qualification is optional. MCP auth and transport are
separate from CLI profiles: existing CLI modes, profiles and approvals do not
migrate through schema inspection.

## Binary and credentials

Resolution order: `OPENART_CLI_PATH` (explicit, must be an executable file,
otherwise `missing_binary`), then `PATH`, then `~/.local/bin/openart`.

Credentials stay opaque. OpenMontage never reads `~/.openart/cli-credentials.json`
and strips `OPENART_TOKEN`/`OPENART_API_KEY` from the child environment, so a stray
token cannot silently switch away from the subscription OAuth route. Login is a
human step run outside OpenMontage.

## Read-only grammar

`run_readonly(argv)` accepts exactly these forms and always appends `--json --no-input`:

| Command | Accepted arguments |
| --- | --- |
| `version`, `account`, `model list`, `creation list`, `model cost` | none |
| `model cost` | `--model <id> --mode <mode>` in that order (v0.1.1 has no settings flags) |
| `model form` | `<model-id> <mode>` |
| `creation get` | `<creation-id>` |
| `generate video` | `<prompt> --model <id> [--duration N] [--aspect-ratio R] [--resolution R] --dry-run` |

Anything else is refused with `not_read_only` before any process starts. That
includes unknown, duplicate or `--flag=value` forms (`--dry-run=false`), `--async`,
`-o/--output`, caller-supplied `--image`, `--`, `upload`, `creation wait` and auth commands.
Raw `run_readonly` callers cannot add `--image`. Governed native binding can
construct its frozen preview/submit argv only after resolving one retained
first-frame `upload_id` against its exact source-byte snapshot and verified
profile; a local path is never allowed to trigger an implicit CLI upload.
IDs must match `^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$`, and prompts must not start with `-`.

## Private state

State lives in `OPENMONTAGE_OPENART_STATE_DIR`, defaulting to
`~/.openmontage/openart`. The path must sit outside the checkout and must not be
a symlink. It is created with mode 0700. Every entry, including receipts, stream
files, the lock and any SQLite `-wal`/`-shm`/`-journal` files, must be owned by
the current user and have no group or world bits. Any existing unsafe entry fails
as `unsafe_state` before a call. Each call holds one shared transport lock: a
thread lock plus `flock` on `transport.lock`. Every CLI call is serialized because
an OAuth refresh may rewrite credentials.

Child stdout/stderr go straight to private 0600 files under `streams/`. Stdout is
bounded at 1 MiB (`output_too_large`). Other error kinds are `missing_binary`,
`nonzero_exit` (returncode plus a redacted stderr tail), `timeout`, `malformed_json`
and `offline_only`. The raw receipt is kept only in private state under
`receipts/`. Public results carry only an opaque `receipt_id` and
`receipt_sha256` (integrity digest of the private receipt file). They never carry
an absolute path. Downstream units resolve the ID with `receipt_path(receipt_id)`
under the private root and verify the digest. Read-only timeouts must be finite
numbers in (0, 300] seconds. Booleans, strings, NaN, infinity and negative values
raise `invalid_argument` before launch. Public copies (`redact`) drop
secret-named keys, JWT/`sk-`/bearer
strings and all URL query strings, which covers signed URLs.

## Account tool

`openart_account` (tier `analyze`, never governed as generation) requires
`read_only: true`:

- `inspect`: `version` + `account`.
- `quote`: `model cost --model --mode`. The result always has `covers_settings: false`
  and `qualification: unqualified_for_dispatch`. A model/mode price is never bound
  to arbitrary duration, resolution or ratio settings as an exact quote.
- `form`: `model form`, returned as an unqualified schema observation; defaults
  do not make properties required. The generic capability helper parses object
  forms and root `anyOf`/`oneOf` branches. The discovery row keeps advertised
  forms visible if its runtime parser cannot analyze one, but withholds
  `native_capabilities` in that case. A form response never grants dispatch
  authority.
- `native_dry_run`: `generate video ... --dry-run`, returning `{endpoint, body_sha256, body}`.
- `readiness`: gate table, plus an optional `version` probe.
- `status`: reconcile one original attempt from its durable OpenArt event/job receipt.
- `recover_original_submit`: for an original real staged production or optional
  `result_contract_qualification` attempt held because the frozen submit path
  missed the observed response field (for example `historyId`). Pass `attempt_id`
  and `json_paths: {"submit_job_id": "historyId"}` only when that top-level field
  was observed in the original response; this compatibility repair accepts no
  other submit path. The tool extracts the candidate internally,
  checks the successful original process, frozen request and matching original
  ledger authority (plus the marker for a diagnostic qualification), then reads the current account and that
  original creation. It returns safe field paths/types, recognized status enums,
  URL hostnames and receipt hashes; no job ID or URL is returned. A unique returned
  eligible identifier-field match establishes its path. Otherwise it keeps the
  hold and reports shape. An observed `result_job_id` may be declared, but cannot
  override ambiguity or qualify a protocol, request, error or billing echo.
  The observed `history.id` result identity is eligible; media `resources[].id` and
  `resources[].generation.historyId` remain outside the job identity selector.
  Resource URL fields are reported as safe paths/types and hostnames only.
  The immutable private recovery proof binds the canonical original acknowledgement
  without rewriting the profile, launch or approved request. Repeating the action
  reads the same original creation for current safe shape and repairs interrupted
  acknowledgement publication. Conflicting declarations fail closed. Ordinary
  collection uses the verified identifier paths for that same attempt without
  result promotion; its status, URL and native-control declarations stay frozen.
  The optional diagnostic route keeps `qualify_result` for strict result promotion.
  Recovery never resubmits, settles billing, releases the slot, supplies a credit
  ceiling or establishes creative quality.
- `collect`: call only `collect_openart_attempt(project_dir, attempt_id, request_sha256, timeout)`;
  it writes the original project's result and never submits or reserves again.
- `verify`: verify the retained collection receipt against the original frozen profile. Pure.
- `qualifications`: list qualification summaries without provider calls.
- `upload`: use the registered approval lookup and jobs helper for one approved reference.
  Upload proceeds only with a real nonspending upload contract and matching approved source;
  absent evidence fails closed. It does not reserve generation credits.
- `native_dry_run` accepts `image_upload_id` for a profile-bound retained upload. It resolves
  the private URL internally, uses the guarded `--image` dry-run path, and redacts URL query
  credentials from published argv and request bodies. Local paths are never passed as `--image`.

- `qualify_quote` and `refresh_quote`: capture and retain an exact settings quote
  for an approved request.
- Unknown-cost preparation and dispatch use a separate authorization and typed
  unpriced claim through the registered account and generation tools. This path
  requires a fresh account observation and the explicit no-enforceable-ceiling
  acknowledgement. Exact action/input names are defined by the registered tool
  schemas; do not construct caller-side authority or infer an API from this
  guide.
- `resolve_attempt`, `repair_outbox` and `qualify_resolution_contract`: resolve
  or repair one original attempt from retained receipts. They never launch a
  new job.

Only `submit` is disabled as an account action. Paid submission goes through the
governed adapter. No account action reserves credits or submits paid work. Auth identity, model IDs,
settings quotes, upload billing, async results, exhaustive history and generation controls
remain unqualified until backed by captured account evidence. Status, collection and
verification do not authorize retries or release account-credit holds.

## Offline preparation hook

In strict governed dry-runs, `BaseTool` calls an optional
`prepare_offline(inputs, governed_result)` inside `offline_preparation()`. The
hook's return value is attached as `offline_preparation`, and the governance keys
cannot be overridden. While the context is active, the OpenArt transport refuses
to launch (`offline_only`). Tools without the hook return exactly the previous
governed result. The OpenArt hook calls `dispatch.offline_readiness(inputs)`.
It loads the `pre_submit` profile and prepares and validates the native
request. In priced mode it looks up the retained exact quote and reports
`retained_quote` or `quote_required`; unknown-cost mode uses its distinct
authorization path and never represents the unknown charge as a quote.
Preparation itself makes no provider generation call and reserves no credits.

## Current readiness and native limits

The October 7 sweep observed 17 catalog video model IDs, all 45 advertised
video mode pairs and their retained forms. Use exact schema observations,
current authenticated account readiness and native transport support to decide
production eligibility. Generated-result or quality qualification is optional.
Account identity is refreshed before each dispatch. Exact settings quotes and
billing authorization remain required where applicable. Native end-frame, multireference and audio controls also remain
untransportable in CLI 0.1.1. Its only selectable modes are text-to-video and
image-to-video, with one retained-upload first frame for the latter; local paths
never trigger an implicit provider upload. See the [model controls evidence
note](implementation/2026-10-07-openart-model-controls.md) for the form/CLI/live
qualification boundary.

## Live discovery process (root-owned)

1. Verify the binary (`version --json` returns `{arch,commit,date,os,userAgent,version}`).
2. Human completes OAuth. Then `openart_account inspect` captures the account shape.
3. `model list`, then `model form <id> <mode>` and `model cost --model --mode` for candidate models.
4. `native_dry_run` with the chosen settings, which records the exact request body digest.
5. Record captured receipts as optional diagnostics. Fixtures prove local
   behavior, not provider shapes. A prior paid result is not required when the
   current schema, account and exact transport checks support the request.
6. The new reference-free path is limited to OpenArt text-to-video requests with
   no cast, dialogue or source assets and no pinned upstream assets. It is an
   explicit request mode; it does not change legacy board requirements. Any
   paid sample or paired benchmark is optional and requires its own approval.

Observed nonspending dry-run (unauthed dummy model, explicitly unqualified):
`{"endpoint":"POST /api/cli/v1/generate","body":{"model","media":"video","mode":"text2video","params":{"aspectRatio","duration","prompt"}}}`.
Server creative defaults absent from `params` are not represented, so bind
`model form` defaults separately.

## Interface for later units

- `native_video_argv(prompt, model=, mode=, duration=, aspect_ratio=, resolution=)`
  builds the creative argv. `native_dry_run_argv` is exactly that list plus `--dry-run`.
  A future submit must append its own frozen flags (for example `--async`) to the same
  creative list and prove the dry-run `body_sha256` matches.
- `dry_run_request(parsed)` returns `{endpoint, body, body_sha256}` (canonical sorted JSON).
- `state_dir()`, `private_dir(*names)`, `write_private(path, bytes)` (exclusive,
  full write, fsync), `verify_private_state()`, `transport_lock()`.
- `run_readonly` is read-only by construction. Spending transport must be a separate,
  separately reviewed entry point that reuses the lock, environment filter, stream
  files and redaction.


## Optional first-account diagnostics

Setup uses `lib/openart_setup.py` and additive `openart_account` actions. All
require `read_only: true`, return opaque profile identifiers/hashes and levels,
and create no generation credit reservations. Captured raw receipts remain
private. Caller declarations identify JSON paths; they do not establish observed
provider success, billing guarantees or result eligibility.

1. `qualify_inspection` takes `model`, `mode`, and `json_paths`. It executes actual
   `version`, `account`, and `model form` commands and saves an `inspected` profile.
   Paths include `account_id`, `account_tier`, `submit_job_id`, `result_job_id`,
   `status`, `urls`, distinct `status_terminal_ok`/`status_terminal_fail` strings,
   and exact `url_hosts`. Future result paths remain unqualified declarations.
2. For image-to-video, `qualify_upload` takes `model`, `mode`, `json_paths` with
   `upload_url`, exact `url_hosts`, and `guarantee`. The guarantee declares an
   allowed readonly `argv` plus `nonspending` and `no_delayed_charge` assertions,
   each `{path, expected}`. Nonspending requires native `true` or integer `0`;
   no delayed charge requires native `true`. Both must match the actual captured
   provider response. Missing, false or absent provider evidence refuses upload.
   An unchanged balance, a label, or a caller assertion is insufficient.
3. `upload` executes the first approved reference upload through the jobs helper.
   Its immutable private source snapshot and raw receipt must preserve the exact
   source hash, account, host, URL and upload argv. The stable inspected contract
   also binds version, tier, form/defaults and the captured guarantee receipt.
4. `qualify_preview` takes `model`, `mode`, `prompt`, optional creative controls,
   and the retained `image_upload_id` for image-to-video. It rechecks the current
   version/account/tier/form/defaults, validates the retained upload, executes the
   real native readonly preview, and saves a `pre_submit` creative profile. Its
   image URL must equal the exact retained upload URL. No manual profile edits
   or fabricated submit/result receipts are needed.
5. `qualify_result` targets `attempt_id` plus declared result `json_paths`. Only
   the original separately authorized qualification attempt may establish a
   result proof through `promote_result_contract`. This proof is immutable and
   bound to the original `pre_submit` profile SHA; it never rewrites that creative
   profile to bless older artifacts. A verified original submit recovery supplies
   its immutable `historyId` and returned identity paths to this promotion.
   This result-contract diagnostic is optional. Ordinary production instead
   requires current account readiness, exact schema/transport support, and the
   normal governed request and billing approvals.

The helpers are `inspect_qualification(model, mode, *, json_paths, timeout)`,
`qualify_upload_guarantee(model, mode, *, guarantee, json_paths, url_hosts, timeout)`,
`promote_upload_contract(model, mode, *, image_upload_id)` (pure retained upload
promotion), and `qualify_preview(model, mode, *, prompt, duration, aspect_ratio,
resolution, image_upload_id, timeout)`. `validate_upload_guarantee(profile)` is
pure and raises `OpenArtCLIError` on unsupported or altered evidence.

Offline executable fake-CLI tests prove this staging and private transport
behavior. Their synthetic field names and guarantees are not provider evidence.


## Optional Auto-continue

Strict remains the default. A user-approved `artifacts/autonomy_policy.json`
retains the full approved planning and request templates in a SHA-bound approval
file. Activate its canonical policy SHA through the decision log; selecting
Strict revokes that authority. An empty checkpoint-stage list permits provider
continuation only. Listed stages must be actual gated pipeline stages, with a
fresh structured passing review of the current artifact. Publishing and
benchmarks cannot be preauthorized.

Only explicitly declared deterministic duration, resolution and noncast
reference flex can change approved settings. Duration changes shift subsequent
scene and script timing coherently while preserving dialogue text, speakers,
sources and order. Cast identity, required payoff assets, story facts and native
pins stay locked; a reviewed composite start board must name each required
member's bytes. Explicit dialogue locks address ordered occurrence indices,
because dialogue records have no line IDs. Every candidate needs canonical
root-derived prompts and a fresh named native preparation review.

OpenArt Auto-continue retains two separate billing variants. The existing
`billing: "credits"` variant uses its shared allowance and normal current exact
quote/authorization checks; it remains unchanged. The separately approved
`billing: "unknown_cost_no_ceiling"` variant requires
`exposure_acknowledgement: "no_enforceable_credit_ceiling"`, account identity
binding and exact model/mode routes. It accepts fresh unknown-cost evidence, not
caller-supplied authorization; rooted derivation creates the occurrence-bound
authorization. Neither the evidence nor attempt caps sets a spending ceiling.
Total, per-shot and repair caps still count open scopes, failed attempts and
unresolved attempts. The unknown-cost charge and USD cost remain unknown.

Grok uses the existing subscription; remaining quota is unknown. No paid Grok
API, purchases or top-ups are authorized. Repairs name actual failed attempts
and verified failed-review evidence; first pass cannot authorize rerolls.
Pending or uncertain motion attempts block another provider for the same shot;
a terminal failure can use only a separately authorized repair occurrence within
the approved cap. Completion reports read actual journals, private credit state
and current certification, preserve history, and make no AV quality claim beyond
the retained reviews. For model-intent planning, see
[`VIDEO_MODEL_SELECTION.md`](VIDEO_MODEL_SELECTION.md).
