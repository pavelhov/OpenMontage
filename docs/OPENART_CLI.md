# OpenArt subscription CLI (guarded video route)

OpenMontage drives the official OpenArt CLI (`openart`, verified version 0.1.1, see
https://github.com/OpenArt-AI/cli) as an explicit-only sibling of the Grok CLI route.
The transport is `tools/_openart_cli.py`. Setup, qualification and recovery go
through `tools/openart_account.py`. Generation goes through the governed adapter
`tools/video/openart_cli_video.py`. Generation is possible only through the
guarded path below. Dispatch runs automatically only inside an approved strict
scope with a matching credit authorization and a fresh account refresh. It never
retries automatically and never falls back to another provider.

## Current guarded capabilities

- **Account discovery is pending (as last observed).** A nonspending root
  account probe on 2026-10-06 found the official CLI installed (0.1.1), but its
  `account` command exited 1 with a login-required signal
  (`authentication_required: true`). No generation, reservation or upload ran.
  This is the last observed state, not a live claim: it changes only when a
  human signs in and a later probe captures the account shape. Until
  `openart_account inspect` succeeds, no route is production-available. The menu derives
  `account_discovery` from retained qualification rows (`inspected`,
  `pre_submit` or `full`). A retained row never replaces the fresh account
  refresh that every dispatch requires.
- **Binary.** The official binary 0.1.1 is verified (`version --json`).
- **Resolution.** In one dummy, unauthenticated v0.1.1 dry-run probe, a
  caller-passed `--resolution` flag stayed in argv, but the provider's returned
  preview body did not include a resolution field. That single probe does not
  show the CLI ignores the flag, or that every account lacks the control. A
  required resolution stays unqualified until an observed `model form` and an
  exact preview carry it. A server default never stands in for a required value.
- **Uploads.** Image-to-video uploads stay refused until the provider itself
  states, in a captured response, that uploading is nonspending and has no
  delayed charge. That statement must exist before the first upload. A caller
  claim or an unchanged balance does not count.
- **Exact quote contract.** Each paid attempt needs a current quote for the
  exact settings (`quote_required`), kept as a raw private receipt. A quote for
  other settings is not reused.
- **Staged qualification.** A profile moves through `inspected`, then
  `pre_submit`, then `full`. Inspection and `pre_submit` rows are
  qualification candidates only. A `full` profile needs a real result proof from
  one original, separately credit-authorized attempt. Fixture profiles are never
  live.
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
  credit allowance. Invalid, changed or revoked policy authority stops
  continuation. It never resubmits an unknown job.

The preflight menu (`provider_menu_summary()["qualified_cli_video_routes"]`)
shows this state truthfully. That covers exact qualified model IDs, forms,
controls and limitations, account stages, pending jobs, holds, quarantine and
errors. The adapter's model catalog reads each actual-qualified `full` profile's
reference-only receipts (`{kind, receipt_id, receipt_sha256}`) from private
state, verifies their digests and reapplies the form and preview parsers. It
shows only safe creative values (duration, aspect ratio, resolution); prompts,
image URLs and any other parameter appear only as `value_redacted` plus a
SHA-256. A profile whose receipts cannot be verified drops out of the catalog,
the row reports `model_catalog_unverified` or `model_catalog_error`, and that
row is kept but marked not production-ready (`full_profile_receipts_unverified`).
Only actual-qualified `full` real profiles are production-ready. `inspected`
and `pre_submit` rows are candidates, and fixtures are never ready. A
resolution flag present in argv but absent from the effective preview is
reported as `argv_flag_without_effective_preview`, with no guessed support.

`dispatch_readiness` is separate from qualification, because a qualified model
does not make an account available. It always requires a fresh prelaunch
account refresh. A pending, submitting or uncertain job slot blocks it
(`unknown_job_acceptance_unresolved`), as do quarantine and unacknowledged outbox
events. A retained unknown-billing hold on a terminal slot does not block by
itself. It marks economics as incomplete (`incomplete_unknown_billing_hold`)
and requires a remaining allowance and a fresh exact quote. Holds and quarantine
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

For explicit-only CLI providers (such as `openart_cli`), the video selector
treats `preferred_provider` as an explicit pin. Ordinary provider preferences
keep the existing scoring behavior. When the preferred provider is
explicit-only and `allowed_providers` is anything other than exactly that
provider, the scopes are crossed and the request fails as `not_dispatched`
before any provider call. A singleton pin that asks for an unsupported control,
or an unavailable or unqualified model, is rejected before scoring and never
falls back.

Missing controls stay visible. The v0.1.1 surface has no end-frame pin, native
audio or multiple reference images. An explicit OpenArt pin that requests any
of them fails with `missing_controls` before any credit reservation. It never
substitutes a prompt or another provider.

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
Only `native_dry_run` can add `--image` internally after resolving a qualified `image_upload_id`.
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
- `form`: `model form`, parsed as JSON Schema (defaults inline; defaulted
  properties are absent from `required`). Any other shape is `form_shape: unqualified`.
- `native_dry_run`: `generate video ... --dry-run`, returning `{endpoint, body_sha256, body}`.
- `readiness`: gate table, plus an optional `version` probe.
- `status`: reconcile one original attempt from its durable OpenArt event/job receipt.
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
It loads the `pre_submit` profile, prepares and validates the native request
and looks up the retained exact quote. It returns `provider_calls: 0`,
`reservations: 0` and `credit_state` of either `retained_quote` or
`quote_required`.

## Unsupported gates (all `unqualified` until live account evidence)

`account_identity`, `model_ids`, `form_schema`, `settings_exact_quote`,
`async_result_contract`, `upload_billing`, `exhaustive_history`, `native_audio`,
`end_frame_pin`, `reference_images`. The v0.1.1 video command exposes prompt, model,
duration, aspect ratio, resolution and a single `--image`; it shows no end-frame, audio,
rich-reference or prompt-expansion flags. `--image` auto-uploads local files, so
image-to-video is permitted only through the separately qualified retained-upload path.

## Live discovery process (root-owned)

1. Verify the binary (`version --json` returns `{arch,commit,date,os,userAgent,version}`).
2. Human completes OAuth. Then `openart_account inspect` captures the account shape.
3. `model list`, then `model form <id> <mode>` and `model cost --model --mode` for candidate models.
4. `native_dry_run` with the chosen settings, which records the exact request body digest.
5. Record the captured receipts as evidence. Only that evidence may qualify a gate.
   A passing fixture test is not a qualification. Fixtures prove transport
   behavior, not provider shapes.
6. A paid benchmark is a separate, exact, explicitly approved step (U4).

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


## First-account staged qualification

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
   the original separately credit-authorized benchmark attempt may establish a
   result proof through `promote_result_contract`. This proof is immutable and
   bound to the original `pre_submit` profile SHA; it never rewrites that creative
   profile to bless older artifacts. Ordinary paid production still requires full
   qualification and the later ledger/approval gates.

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

OpenArt uses the exact policy credit ceiling through a shared allowance and
normal retained quote/authorization checks. Grok uses the existing subscription;
remaining quota is unknown. No paid Grok API, purchases or top-ups are authorized.
Repairs name actual failed attempts and verified failed-review evidence; first
pass cannot authorize rerolls. Pending or uncertain motion attempts block another
provider for the same shot. Completion reports read actual journals, private
credit state and current certification, preserve history, and make no AV quality
claim beyond the retained reviews.
