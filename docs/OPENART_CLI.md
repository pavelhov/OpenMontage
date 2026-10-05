# OpenArt subscription CLI (U1–U2: transport, account and job recovery)

OpenMontage drives the official OpenArt CLI (`openart`, installed version 0.1.1, see
https://github.com/OpenArt-AI/cli) as a sibling of the Grok CLI route: one
subscription account, authenticated by the CLI's own OAuth flow. OpenMontage adds
a read-only transport (`tools/_openart_cli.py`) and a nonspending account and
recovery tool (`tools/openart_account.py`). **No generation submission surface is enabled.**

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

`resolve_attempt` and `submit` remain unavailable. All actions return
`cost_usd: 0`, `reservations: 0` and `paid_submission: false`. Auth identity, model IDs,
settings quotes, upload billing, async results, exhaustive history and generation controls
remain unqualified until backed by captured account evidence. Status, collection and
verification do not authorize retries or release account-credit holds.

## Offline preparation hook

In strict governed dry-runs, `BaseTool` calls an optional
`prepare_offline(inputs, governed_result)` inside `offline_preparation()`. The
hook's return value is attached as `offline_preparation`, and the governance keys
cannot be overridden. While the context is active, the OpenArt transport refuses
to launch (`offline_only`). Tools without the hook return exactly the previous
governed result. `offline_quote_status(evidence, request_sha256)` is the pure
helper a hook uses: it returns `retained` only for matching evidence with
`covers_settings: true`, and `quote_required` otherwise.

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
`{"endpoint":"POST /api/cli/v1/generate","body":{"model","media":"video","mode":"text2video","params":{"aspectRatio","duration","prompt","resolution"}}}`.
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
