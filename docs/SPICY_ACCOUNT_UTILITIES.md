# Native Spicy account utilities

Use the existing native applications; no MCP installation is needed. Studio owns
standalone image operations. OpenMontage owns video projects, source transfers,
generation and collection. These utilities do not activate an identity or project.

Run from the OpenMontage checkout. These commands use the project-managed Python
3.11 runtime even if another environment is active:

```sh
./.venv311/bin/python -m lib.spicy_jobs account docs schema
./.venv311/bin/python -m lib.spicy_jobs account status
./.venv311/bin/python -m lib.spicy_jobs account models --modality video --provider ByteDance
./.venv311/bin/python -m lib.spicy_jobs account models --model MODEL_FROM_CURRENT_CATALOG
./.venv311/bin/python -m lib.spicy_jobs account balance
./.venv311/bin/python -m lib.spicy_jobs account usage --from 2026-10-01 --to 2026-10-08
./.venv311/bin/python -m lib.spicy_jobs account tasks --from 2026-10-01 --to 2026-10-08 --state failed --limit 20
./.venv311/bin/python -m lib.spicy_jobs account task ORIGINAL_TASK_ID
./.venv311/bin/python -m lib.spicy_jobs account wait ORIGINAL_TASK_ID --timeout 60 --interval 5
```

Social Studio exposes the same `account` group through
`python3 ops/media/spicy_images.py` from its own checkout; that CLI automatically
selects Studio's `.venv-spicy` runtime. OpenMontage's
`--credential-ref` option and Studio's `--key-env`
retain their existing precedence. The default native credential can use
`~/.config/spicyapi/credentials.env`; secrets never belong in either repository.

`docs` searches a small local index of first-party guide titles and summaries,
dated 22 October 2026. It does not fetch or mirror the full vendor documentation.
`status` sends anonymous GETs to fixed `/healthz` and `/readyz` endpoints. A 404
readiness probe is compatible with an operational public service. Neither command
loads a credential. Other inspection commands use the existing authenticated
native client and redact provider messages, task inputs, generated text, names,
notes and signed media URLs.

The catalog is current account discovery, not production qualification. Its
`inputSchema` view projects recognized machine constraints while omitting prose,
examples, defaults and unsupported schema keywords. It is not a complete schema
or a standalone validator. The owning image/video workflow must qualify the exact full schema
before production. Prices are account-specific USD tiers; a starting price does
not replace a quote for the exact request. This utility never treats an image,
audio, text or analysis listing as a callable OpenMontage video route.

The inspection surface maps to these native endpoints. Paths below are relative
to `https://api.spicyapi.ai`. Authenticated reads go through a fixed allowlist of
named official SDK methods (`list_models`, `get_model`, `get_balance`, `get_usage`,
`list_tasks`, `get_task`, `list_assets`, `get_asset`, `asset_usage`,
`list_characters`, `get_character`, `create_download_url`, `purge_task`), checked
before any attribute lookup; quote, create, upload, commit, retry and asset writes
are refused. The SDK parses the `{code,msg,data,request_id}` envelope with
`max_retries=0`; the anonymous health probe stays a small bounded public GET.

| Command | Native operation | Returned view |
| --- | --- | --- |
| `status` | Anonymous `GET /healthz` and `GET /readyz` | Health/readiness status only; no provider body |
| `docs` | Local guide-index search | Guide titles, summaries and official links; no remote search |
| `models` | `GET /api/v1/models`, or `GET /api/v1/models/{model}` | Model IDs, flags, task classes, prices and the optional schema projection |
| `balance` | `GET /api/v1/chat/credit` | USD `available`, `held`, `total` strings; approved credit can make available/total negative |
| `usage` | `GET /api/v1/usage` | Current-key calls and settled USD spending, grouped by day and model |
| `tasks` | `GET /api/v1/jobs` | Current-key task metadata and the next cursor |
| `task`, `wait` | `GET /api/v1/jobs/recordInfo?taskId=…` | Exact original's state, settlement and asset metadata; no prompts, output text or signed links |
| `assets`, `asset-usage` | `GET /api/v1/assets`, `GET /api/v1/assets/{id}`, `GET /api/v1/assets/usage` | Redacted library metadata and numeric usage/capabilities |
| `characters` | `GET /api/v1/assets/entities`, or `GET /api/v1/assets/entities/{id}` | Character IDs and ordered redacted asset metadata |

Task history and usage are scoped to the calling API key; the web app and other
keys' work are excluded. Task list pagination returns `nextCursor`; reuse the
exact same dates and filters on the next call. Supply explicit `--from` and
`--to` dates when paging, so the server's moving default window does not change
at midnight UTC. A list request returns one page and never follows cursors itself.
Dates use UTC, with `from` inclusive and `to` exclusive; explicit ranges may span
at most 92 days. Without dates, the server defaults to seven days through tomorrow
UTC. Wait polls only the same original task, with a zero-to-300-second local wait
budget, a one-to-30-second interval and a separate maximum call count. An in-flight
HTTP read can exceed that local budget. A terminal task does not prove final
settlement or that every asset is ready to download. Stopping a wait does not
cancel the original, and a failed read never starts or retries work.

The additional read surface is:

```sh
./.venv311/bin/python -m lib.spicy_jobs account assets --kind video --source upload,task_output --sort recent
./.venv311/bin/python -m lib.spicy_jobs account assets --asset-id ASSET_ID
./.venv311/bin/python -m lib.spicy_jobs account asset-usage
./.venv311/bin/python -m lib.spicy_jobs account characters --asset-id ASSET_ID
./.venv311/bin/python -m lib.spicy_jobs account characters --entity-id CHARACTER_ID
```

Assets and characters can be workspace or personal-library scoped, unlike
key-scoped tasks. Names, notes and private preview links are omitted. Usage emits
numeric usage/limits and Boolean scope capabilities, omitting workspace names
and identifiers, plan names and string-valued top-up amounts. Stable `spicy://a/`
references, private previews and character names/notes are not emitted by these
inspection commands. Lists support their documented filters and cursors. Saving,
renaming, favoriting or deleting library assets is outside this utility; remote
character/folder creation and edits have no documented native API here.

## Private download URL handoff

Use the owning collector for actual downloads: it validates qualified hosts,
bytes, MIME, dimensions and provenance. This account command only issues an exact
asset's fresh URL into a new `0600` JSON file in an existing user-owned `0700`
directory. It requires an observed succeeded original and exact output key:

```sh
./.venv311/bin/python -m lib.spicy_jobs account download-url ORIGINAL_TASK_ID \
  --key EXACT_OUTPUT_KEY --private-file /ABSOLUTE/PRIVATE/DIRECTORY/link.json \
  --allow-private-url
```

The URL and path are not printed. The handoff contains only the exact task, asset
key and URL; it is temporary access, not an archived result. Links normally last
20 minutes and cannot extend output retention. Pending or unavailable assets have
no usable download key; query the same original again when copying is pending.
Existing files, shared directories and symlink
paths are refused. No media bytes are downloaded, uploaded or registered by this
command. A private URL file is an access grant; keep it outside shared records.

## Exact original purge

Purge permanently removes a terminal task's remote prompt, result and artifacts.
It leaves billing records and local originals, gives no refund, and cannot
cancel a running task. Use it only after a human confirms the exact displayed
batch. Software installation and this feature's availability provide no such
confirmation.

First select an existing local identity/project root, canonical original records,
and the exact matching task IDs. The preview is local and does not delete:

```sh
./.venv311/bin/python -m lib.spicy_jobs account purge-preview \
  --owner-root /ABSOLUTE/OPENMONTAGE/PROJECT --scope-kind openmontage-project \
  --record production_attempts/ATTEMPT_ID/spicy_job.json \
  --task-id ORIGINAL_TASK_ID
```

The immutable preview includes exact request bodies, record hashes, original-key
bindings, effects and a `binding` digest. Present its task batch and effects to
the human. Retain their confirmation as a JSON object with these exact fields:

```json
{
  "actor": "human",
  "action": "purge",
  "binding": "EXACT_PREVIEW_BINDING",
  "task_ids": ["EXACT_DISPLAYED_TASK_ID"],
  "evidence": "Retained evidence of the human's exact-batch confirmation."
}
```

This example is a schema, not consent. The real task list must match the
preview's ordered list, and the evidence must represent the actual human
confirmation. Retain it with `purge-authorize --owner-root ... --scope-kind ...
--binding ... --confirmation-file /ABSOLUTE/confirmation.json --allow-delete`.
That command writes an immutable confirmation record and still deletes nothing.

Future execution uses `purge-execute` with the same owner, scope, binding,
`--authorization-record` returned by authorization, and a fresh `--allow-delete`
interlock. The native application's canonical-original resolver, unchanged local
record hashes, current original API key, and a fresh matching terminal task/model
observation must all pass. OpenMontage uses scope `openmontage-project` and records
`production_attempts/ATTEMPT_ID/spicy_job.json`. Studio restricts execution to an
immediate identity under its selected `--root`.

An exclusive immutable per-task intent is durably synced before each POST, with
separate immutable result records. A lost response or existing intent is never
resubmitted. `purge-reconcile` accepts the same execution arguments and uses only
GET to inspect the same original. `contentState: purged` proves logical removal;
`expired` does not. Physical media erasure remains unverified, including when
deletion is queued. An unresolved task stops further new POSTs in that execution.
Administrative records live under identity `assets/generated/spicy-admin/` or
project `artifacts/spicy-admin/`; original operation records are not rewritten.
The remote action is exactly `POST /api/v1/jobs/purge` with `{ "taskId": "…" }`.
Deleting a saved library asset is a separate `/assets/{id}` operation, outside
this utility. Local media is retained.

## Retry ownership and remaining limits

`account retry-handoff --owner-root ... --scope-kind ... --record ...` inspects
one known failed/expired/canceled local original and returns its bound owner
handoff. It never dispatches. The provider's raw `/jobs/retry` accepts only
`taskId`, with no `quoteId`/`expectedCost` protection, so it is deliberately not a
paid execution path here. Use Studio's `prepare-retry` or OpenMontage's governed
new-attempt path to prepare separately counted, freshly quoted and human-approved
work with parent provenance. A new original is not recovery of an uncertain
original, and source expiry does not authorize reupload.

This utility does not implement chat streaming, media generation, raw source
uploads, asset mutations, automatic retry, account/key administration or the
vendor web Studio's Auto-model, Enhance, Canvas and timeline workflows. Those
surfaces are not established as equivalent native orchestration endpoints by
the public endpoint inventory.

Contract evidence: [API index](https://docs.spicyapi.ai/docs/api-reference),
[models](https://docs.spicyapi.ai/docs/models),
[tasks](https://docs.spicyapi.ai/docs/tasks),
[billing](https://docs.spicyapi.ai/docs/billing),
[media](https://docs.spicyapi.ai/docs/media),
[retention](https://docs.spicyapi.ai/docs/retention),
[task actions](https://docs.spicyapi.ai/docs/task-actions), and the dated Studio research receipt,
`docs/receipts/2026-10-22-spicy-expansion-contract.md` in the Studio workspace.
The official links remain usable in a standalone OpenMontage checkout.
