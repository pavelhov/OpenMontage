# OpenArt subscription CLI (U1: transport and account inspection)

OpenMontage drives the official OpenArt CLI (`openart`, see
https://github.com/OpenArt-AI/cli) as a sibling of the Grok CLI route: one
subscription account, authenticated by the CLI's own OAuth flow. U1 adds only a
read-only transport (`tools/_openart_cli.py`) and a nonspending inspector tool
(`tools/openart_account.py`). **No generation surface is enabled.**

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
`-o/--output`, `--image`, `--`, `upload`, `creation wait` and auth commands.
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

`status`, `collect`, `resolve_attempt`, `submit` and `upload` always return
`success: false` and are delegated to later units. Results carry `cost_usd: 0`,
`reservations: 0` and `paid_submission: false`.

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
`end_frame_pin`, `reference_images`. The v0.1.1 help shows no end-frame, audio,
rich-reference or prompt-expansion flags. `--image` auto-uploads local files, so
`image2video` stays refused until upload billing is qualified nonspending.

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
