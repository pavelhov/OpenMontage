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
Retain each full host response directly with the existing receipt helper; surface
only its ID, saved path and status. Do not print encoded media or repeat full
receipt/status inventories between actions. Save each reply under its attempt ID
and check that the write succeeded before importing it.
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

For one reference-guided whole-story request, author one shot with the route's
supported total duration and the timestamped internal cuts in its action text.
Declare a small ordered set of `reference_image` consumers; descriptive source
filenames and the actual upload receipt labels retain their identities. Internal
cuts are instructions within this one original, not additional video slots.
Native references guide objects/cast/style rather than pinning exact cut frames.
The optional single-video `quoted_credit_budget` policy retains a creator budget
against an exact host-observed connector quote before scope admission; its shape
and honest price/ceiling distinction are owned by [OPENART_MCP](OPENART_MCP.md).

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
An omitted compact `motion.frame_input` is derived only from one explicit
effective `start_frame` consumer; ambiguity or a conflicting explicit value is
an error. A source-free outgoing-frame reuse board may omit its administrative
`prompt`; expansion names the declared reuse without requesting an image.
Legacy start-only inputs and retained requests keep their original interpretation
and bytes.

Clients that derive story/command wrappers use `preflight_package(project, package,
package_binding=...)` before publishing them. It runs the same structural core as
`compile_package`, reading actual reference bytes and checking existing output
conflicts. It grants no authority: retained source validation, publication and
live dispatch admission still run normally. New shootable prompts use the neutral
`End of clip:` label; exact historical labels and coverage remain readable.

For a continuous continuation, declare the consumer's source-free `start_frame`
board with `upstream_source: {shot_id: <earlier-shot>, role: outgoing_frame}` and
include that shot in `video_dependencies`. The normal `advance` loop waits for
that exact original, samples its final decoded video frame with the registered
sampler, and binds those bytes to the consumer's first-frame input. It never
requests a new image for this board or uses the authored ending illustration as
if it were the actual outgoing footage. Independent boards and clips remain
eligible while the upstream original runs.

The immutable `factory_outgoing_sampling.json` in the original attempt directory
retains the source video hash, decoded timestamp evidence, exact sampler request,
actual result and frame hash. Interrupted sampling/binding reuses the same
original and retained receipt; publishing the factual selection again adds no
history or generation count. The selection and reused-board observations remain
explicitly `unknown`, never a passing semantic review. Source ancestry describes
only the actual inputs to that original, with no claim that identity or continuity
survived generation. Current source paths/hashes, native roles, authority and
transfer limits still validate normally. Generic manifest selection review gates
remain strict.

A terminal failed upstream original or a retained local sampling failure leaves
its dependent clip missing. The factory assembles other playable originals,
including a playable upstream clip whose frame could not be sampled, and reports
the partial result. It never replaces the original or holds that cut indefinitely
for a dependent image that cannot be produced.

### Input preparation and prompt roles

Initialize a new project through the current client entry before writing its
assets or notes. Check provided images against the exact native dimension limits
before freezing path/hash bindings. When necessary, prepare mechanical resize/pad
copies preserving full-image content, originals and provenance; local input
preparation is not a corrective image generation.

Author against the actual bound role, not just the operation name. A literal
`start_frame` supplies initial composition; an untimed `reference_image` guides
appearance and does not pin the opening. Put static scene facts in a first-frame
board and describe change chronologically in motion, ending with `End of clip:`.
Reference-guided internal cuts instead need their changing poses, possession and
completed states in the timed action text. Source identity references remain
separate from these native consumers.

These brand-neutral examples separate a first-frame scene from its motion:

| Brief | Board establishes | Motion describes |
| --- | --- | --- |
| Product demo | Red latch on a closed silver case; hand beside it; close view | “The hand flips the latch and opens the lid. Locked camera. End of clip: case open, latch attached.” |
| Physical comedy | Performer beside a tiny trolley with one visibly bent wheel; full view | “The performer pushes; the bent wheel jams and the trolley tips. Wide shot. End of clip: performer catches the handle.” |
| Quiet drama | Two people across a table; one blue envelope between them | “One slides the envelope across; the other rests a hand on it. Slow push in. End of clip: both hands still.” |

For recurring appearance, an image node can retain original cast `reference_ids`
and use `image_dependencies` on the first suitable scene board. For example:

```json
[
  {"slot_id": "R1", "reference_ids": ["cast-a", "cast-b"], "image_dependencies": []},
  {"slot_id": "R2", "reference_ids": ["cast-a", "cast-b"], "image_dependencies": ["R1"]},
  {"slot_id": "R3", "reference_ids": ["cast-a", "cast-b"], "image_dependencies": ["R1"]}
]
```

These are image-node excerpts, not another package. R2 and R3 depend on R1,
not each other, and state their own composition. Input ancestry establishes
which images were supplied; it does not prove appearance or motion quality.
An end pin should derive from its own start scene when the intent preserves that
world/camera and changes only the result. Use supported pins selectively; a shared
prepared boundary expresses intended state, whereas the outgoing-frame edge above
binds actual decoded footage. Neither guarantees continuity or carries velocity.
The brief and relevant [creative skills](../skills/INDEX.md) guide action, pacing
and boundary strategy; an identity may supply its own story owner.

### Preparation handoff

Use `draft_motion_preparation(project, action)` for the current `prepare_motion`
action. It supplies the retained prompt/coverage, exact native roles, stable
sidecar IDs and source-transfer arguments. For OpenArt `input_assets` and supported
root frame aliases, it validates and pins existing uploads by receipt SHA; shared boundary bytes are
transferred once. `upload_authority_kwargs.files` contains only missing sources.
Do not copy all `frames` into another upload batch.

```python
draft = execution.draft_motion_preparation(project, action)
args = draft['upload_authority_kwargs']
if args['files']:
    authority = autonomy.derive_standing_upload_authority(project, **args)
    upload = jobs.prepare_upload(project, files=args['files'],
        billing_declaration={'upload_authorization_id': authority['upload_id']},
        openart_project_id=args['openart_project_id'])
    # Invoke upload['tool'] with upload['arguments'] exactly once in the host.
    # Pass the actual returned structured payload, retaining the raw response.
    jobs.record_upload_receipt(project, upload_id=upload['upload_id'], result=actual_result)
    draft = execution.draft_motion_preparation(project, action)
```

An empty `files` list skips both authority derivation and upload. The helper's
returned key is `upload_id`; the billing declaration's key is
`upload_authorization_id`. Missing receipt cache for a consumed or recorded batch
names its original upload ID and retained batch file; recover that original
instead of transferring the sources again. Root frame inputs retain their
governed path layout and use the existing `image_upload_id`/`end_image_upload_id`
selectors. Only the native control projection converts them into ordered
first/last frame rows. Historical frozen requests remain unchanged.

For an actual source-upload HTTP send failure, `jobs.retry_upload_after_transport_error`
accepts the retained upload ID and `observed_error={'path': ..., 'sha256': ...}`
binding the saved full host response. It reserves one extra source-transfer
invocation against remaining standing `max_batches` before returning the same
snapshot paths and Project envelope. Invoke that returned envelope once, then
record the actual result with the same upload ID. The first outcome stays
uncertain; the second receipt is labeled a transport retry, with no claim of
backend deduplication. Unbound errors, access/policy denials, standalone one-batch
authority, exhausted allowance, and another retry remain blocked. This grants
no image or video generation retry.

For a current singleton Grok `prepare_motion` action, complete it with
`outcome={"preparation": {"timing": timing, "review": judgment}}`. The existing
`complete_action` drafts the exact retained inputs, observes the registered CLI,
builds the native request, validates the authored timing and exactly bound
judgment, and retains both immutable sidecars before normal scope admission and
dispatch. Supply actual image judgment, including unknowns or failures; semantic findings
are report-only in this activated factory. This convenience creates no predicates
or timing estimates. Modified/stale actions reject, and retrying after dispatch does
not regenerate. Interrupted preparation retains the same IDs and exact records.
Fresh observations at later admission/dispatch boundaries remain required.
Other routes continue with their explicit prepared inputs and provider.

Pass `draft['inputs']` to `prep_builder(draft['provider'])` with the exact selected
profile. Supply the producer's timing estimate and actual preparation judgment
to `prepare_compiled_request(..., coverage=draft['coverage'], timing=timing,
review=judgment, persist=True)`. This retains both validated sidecars under the
IDs in the inputs using the exclusive immutable writer; exact existing records
are reused, and a conflicting retained record rejects before either publication.
Each file is atomic; an interrupted pair can be completed by repeating the same
preparation without a provider call. Its returned bound review fills the canonical
review ID, subject hash and authenticated generated-carrier membership. The default
`persist=False` remains pure. Do not hand-copy
their binding fields or turn unknown/failed observations into passes. Then
complete the same action with those inputs and provider; Grok also requires the
actual CLI observation as `outcome["observation"]`.

When omitted, the compiler binds action-window source hashes and dialogue text
hashes/word counts from the exact plan. Intervals, rates and rationale remain
authored estimates, and supplied conflicting bindings reject.

For native image returns, load the existing
[`scripts/native_host_receipt.js`](../scripts/native_host_receipt.js) in the agent
host and use `consumeNativeHostAction` from the outer `functions.exec` in Codex,
with its real `tools` bridge and exact emitted envelope. Pass the actual image
tool call as `nativeTool.invoke`, for example
`args => tools.image_gen__imagegen(args)`. The Node REPL bridge is only for
filesystem receipt RPC; it has no ambient tools and cannot emulate the image
tool. The helper checks that bridge before claiming ownership or observing the
native action. It retains the actual observation before saving/completion. Its
`saveNativeHostReceipt` verifies the local cached image against the original data
URL and writes the full receipt atomically through a small Node RPC. Never embed
the multi-megabyte image data URL in a shell or Node script. Keep the receipt path
fixed per original and carry the helper's exact next action; lost saving or
completion replies recover the retained original without another image call.
Both completion consumers reject empty or malformed step objects. Use
`consumeSavedNativeCompletion` to drain the owned completion command and read its
saved JSON; valid saved state wins over truncated stdout, with exposed initial
and final command diagnostics preserved. A running owned session stays pending.
If a terminated completion lost its unsaved next-image handoff, retain that
evidence through the current `receive_native_image` action's
`completion_handoff_lost` outcome. The call stays consumed with submission unknown;
independent work proceeds and delivery reports the missing original. An absent
caller-selected invocation marker proves nothing. Never rebuild or invoke the
lost envelope; a later authentic original remains collectable.

Map the helper's actual error to the existing outcome as follows. Here
`completionPath` is the same caller-supplied absolute path used by the owned
completion command; `completeAction(outcome)` is the caller's existing bridge to
`complete_action(project_dir, current_receive_action, outcome=outcome)` for that
begun original. Running or unknown completion remains pending: propagate the
error and recover the same completion, without rerunning it.

```javascript
const nextStep = await consumeSavedNativeCompletion(tools, {completion, completionPath})
  .catch(error => {
    if (error.completion_state !== 'terminated'
        || !['missing', 'invalid'].includes(error.saved_step)) throw error;
    return completeAction({completion_handoff_lost: {
      kind: 'unsaved_next_handoff',
      completion_path: completionPath,
      saved_step: error.saved_step,
      diagnostics: error.completion_diagnostics
    }});
  });
```

Pass the exposed initial/final metadata unchanged; do not supply invented exit
codes, timestamps, host results or invocation envelopes.

For bridge diagnosis, optionally pass a synchronous `observeTiming: event => ...`
callback to `consumeNativeHostAction`. It receives local timestamps for native
return, retention, receipt preparation, receipt RPC and completion, plus Node
read/hash/write/sync clocks through that same RPC. Keep these observations outside
the canonical receipt. Missing or unmatched clocks remain unknown; RPC wall time
includes host dispatch and return, and clocks do not establish a speed improvement.
Observer exceptions do not change original recovery or completion.

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

`resume_original_attempt` also collects an exact Grok CLI native download transport
failure when the retained failed update binds one original `vidgen.x.ai` video URL
to the frozen call, arguments and session. Dry-run validates that evidence without
network access. Collection performs only a direct GET of that original resource,
validates the native media contract and retains an immutable download receipt.
Original failure records and session logs stay unchanged; repeated resume revalidates
the collected bytes without another GET or generation. If source publication was
interrupted before its receipt, recovery repeats only the same original GET and
requires the orphan bytes to match that newly validated download. Foreign/ambiguous URLs,
changed evidence, incomplete sessions and other native failures remain blocked.

Once originals are collected, assemble every technically playable clip in planned
order, including semantically poor footage. Report the affected shot, observed
defect, missing coverage and actual duration. Missing or corrupt footage produces
an honest partial cut; no usable clip produces an incomplete result. Findings are
disclosures, not acceptance or repair gates. Preserve original media, rejected or
superseded historical media, records and derived edits.

Ready independent originals can dispatch concurrently. Before final assembly,
collect each exact CLI original: a qualified nonterminal stream returns bounded
`pending` with the same collection action, while an interrupted terminal result
is recovered without another generation. Nonterminal evidence alone does not
prove a worker is still running. Terminal failure requires the existing strict
native failure evidence; uncertainty is never relabeled as failure. If an older
partial cut's missing-shot disposition changes while its placed media stays the
same, `first_cut_missing_status` supplies current disclosure rows without
reencoding the export; the retained cut remains the record at render time.

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
