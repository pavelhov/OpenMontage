# Standalone production factory

OpenMontage is the canonical production engine and remains usable without Social Studio, an identity workspace or Drive. Its production package is the authored source for the script, ordered shots, actions, durations, cast, endings and protected invariants. With `planning_mode: "shot_local_v1"`, the engine derives the shot contract from those authored shots; do not copy those same facts into a second plan for approval.

The package's `boards` section declares only the references and image slots needed by that story. A shot may need one reviewed starting image, an ending image, a payoff image, or none of those; these are chosen from the actual action and supported treatment. Do not require three boards per shot by default. Semantic reviews and board observations must come from actual evidence. Unknown placeholders remain unknown and block only the work that depends on them.

For explicit `shot_local_v1` planning with `reference_guided` treatment, motion checks the authored consumer assets and their current reviews without requiring a literal start/end board pair. A declared reviewed `payoff_board` may bind an OpenArt MCP `reference_image` or `environment_reference`; exact native temporal-pin requirements still apply. The authored `endpoint_completion: "unknown"` remains frozen while current subject-bound `completed_action` evidence establishes planning eligibility, and dependent shots require current selected media, outgoing-frame and completion reviews with exact attempt/output/frame/review bindings. Explicit `partial` endpoints and legacy unknown endpoints remain blocked.

In a standing `shot_local_v1` run, call `lib.production_execution.advance` after
selecting an upstream take. Its mechanical `bind_selected_upstream` step fills
the authored dependency with the eligible current attempt, output, outgoing-frame
and original review hashes, preserving story and authority. The resulting
`review_planning` action requires the producer's actual verdict bound to that
planning; `prepare_motion` follows only when existing prerequisites pass. Missing
or ineligible upstream evidence remains blocking. Binding supplies no semantic
verdict and does not change `endpoint_completion` or native input requirements.

Before new standing activation writes authority, each authored guided shot must
have at least one approved compatible route after its model lock and per-shot
selection intent. Guided treatment requires OpenArt MCP `element2video` or
`generate-shot-video`, with the current native form supporting the shot's declared
identity/reference roles. An entirely frame-only pool, including Grok CLI plus
OpenArt MCP `image2video`, fails with shot/provider/model/mode details. This check
uses authored planning while boards may still be pending and unobserved; reviewed
sources, uploads, frame pins and dispatch readiness retain their later gates.
Existing retained activations and original-job collection/replay are preserved.

The account's one-time standing setup and the explicit `make_video` command bind a creator-authorized identity, source/rights scope, routes/accounts, source-transfer limits, and generation caps. The creator's command activates the current authored plan for that account. Internal planning, eligible board work, dispatch, evidence review, any already-authorized bounded repair, composition and delivery continue in the same producer run. There is no routine plan or per-shot approval step. Missing setup, rights, source permission, account readiness or scope blocks only the affected work; an expanded route, source or allowance needs new authority.

An authenticated standing setup may explicitly include complete `episode_controls`:
`max_generations_per_shot`, `alternate_repair`, `access_fallback`, and optional
`first_cut: {enabled: bool}`. Fresh activation records that exact creator-bound
snapshot through the canonical episode controls owner before activating the run.
No client default, package or command supplies omitted controls; absent `first_cut`
remains disabled. Replay preserves later creator amendments and provider restrictions
and never backfills an older activation's ignored setup fields. If publication is
interrupted, retry the same `activate_standing_run` call: only its exact retained
initial controls record can complete the activation, without appending a duplicate.
Missing or changed initial evidence stops new admission; original collection remains
under its retained authority. These controls grant no routes, billing or allowance.

The producer handles each actual returned host envelope and continues the same run. OpenMontage owns execution, original-job recovery, cumulative counts, composition and cut status; it does not invent semantic findings. A saved cut may be an honest draft with disclosed defects or unknown evidence. Certification remains a separate evidence gate. One initial take is the default; repairs use only exact retained authority and caps. Provider submission counts can be unknown for the whole run when host-mediated work or provider-managed batching obscures them. Never report unknown as zero, automatically resubmit an uncertain original, add a timer/service/ledger, or schedule or publish from this factory.

An imported native board with an actual current critical failure can receive a
targeted correction under the same standing run. The producer supplies exact
changes and optionally a complete replacement prompt; the engine retains a new
deterministic request and scope bound to the failed review, current source and
preceding attributable call. Original templates, package, scopes and bytes stay
immutable. Explicit additional reference IDs preserve the base sources and must
already belong to the standing inventory and have current applicable review,
with same-run provenance for generated boards and no cycle back to the corrected
slot. Reservation
and host emission revalidate the evidence, account, route, transfer limits and
cumulative image cap. Cosmetic, missing or unknown evidence does not authorize
this correction. Independent eligible boards continue while the producer authors
the correction; a pending or uncertain original still requires collection.

### Correct a later board in the same run

Use that existing correction path when earlier shots already have motion originals
and the failed board belongs to an ungenerated shot. With retained
`shot_local_v1` scopes, the board owner projects the corrected current bytes into
the affected shot without changing the package, treatment, authored frame asset,
standing policy or submitted requests. Unrelated original attempts and selected
takes remain retained. Changed shot planning and preparation bindings need fresh
actual evidence; image dependents whose source bytes changed become stale. A
guided-to-conditioned frame amendment is a different operation and is unnecessary
for this ordinary correction.

Continue the current action loop; independently eligible motion may precede the
failed board. When it returns the exact correction action:

```python
from lib import production_execution as execution

step = execution.advance(project_root)
action = step['action']
assert action['kind'] == 'prepare_board_correction'
assert action['subject'] == failed_slot_id
step = execution.complete_action(project_root, action, outcome={
    'changes': exact_producer_authored_changes,
    # Optional: 'replacement_prompt': complete_producer_authored_prompt,
    # Optional: 'reference_ids': ordered_existing_authorized_reference_ids,
})
```

Handle `step['host_envelope']` exactly once using the native receipt instructions
below. On interruption, `advance` returns `receive_native_image` for the same
original rather than emitting the envelope again. Complete it with the actual
receipt, then supply the actual current `review_board` verdict and any requested
`review_planning` verdict. Rebuild the affected motion preparation with new retained
IDs, current source bytes and a fresh exact dry-run. Never rewrite old compiled
requests or bypass failed reviews. Missing image/source-transfer allowance remains
a separate authority blocker, even when unrelated motion already completed.

For a creator-authorized increase of an existing standing source-transfer batch
limit, cumulative video-attempt limit or named-shot limit, retain the complete successor setup with its actual creator-message
authorization, then use the existing autonomy owner:

```python
from lib import production_autonomy as autonomy

receipt = autonomy.amend_standing_allowance(
    project_root,
    expected_policy_sha256=current_policy_sha256,
    expected_setup_sha256=current_effective_setup_sha256,
    standing_setup={'path': retained_successor_setup_path,
                    'sha256': retained_successor_setup_sha256},
)
```

This operation changes only declared `source_transfer.max_batches`,
`caps.max_total_attempts` and/or `caps.max_attempts_by_shot` limits, and only increases them. A named-shot map such as `{"S04": 3}` extends S04 alone; the global per-shot setting and every unlisted shot retain their existing limits. It
preserves the original activation, policy, image envelope, request scopes and
cumulative usage. It cannot change sources, routes, accounts, billing, file/byte
limits, global per-shot or repair allowances, or the original episode-control records. The effective controls report the authenticated named-shot override without resetting usage. Resume the same action loop after the validated
successor; do not overwrite the original setup or reactivate the episode.
Proposed setup values do not take effect before this authenticated transition.
For a terminal access failure, any already-authorized fallback is enabled through
the existing episode-controls owner; an allowance amendment does not enable it.
Preserve the failed attempt in the count and collect uncertain originals before
replacement. A provider's exhausted balance cannot be repaired by resetting counts.

For `review_board` with named missing or unknown predicates, inspect those gaps
and return a complete scoped successor verdict carrying `retained_predicates`
unchanged. Other current roles and completed findings remain retained.

Standalone delivery returns the engine cut and export report. Account clients may add identity-specific paths, attributable stills and an existing backup request. A queued backup is not verified. Account closeout must not be required by the standalone engine.

Standing activation validates `checkpoint_stages` against the selected pipeline's
actual human gates before writing authority. A setup may cover more stages than a
particular pipeline gates; the run policy names only that pipeline's gated stages.
For an unstarted run written by the older invalid-stage activation path,
`lib.production_autonomy.reconcile_standing_checkpoint_stages(project_dir,
expected_policy_sha256=...)` removes only declared ungated stages. It retains the
original policy, evidence and decision, appends a linked successor, and preserves
all other authority and allowances. It cannot amend a changed or revoked run.

## Pre-motion frame-input correction

`frame_conditioned` is an explicit `shot_local_v1` input treatment. Each shot
names `frame_input: {asset_id: "existing-board-id", role: "start_frame"}`. The
board specification must declare that exact consumer role, and motion preparation
requires a current passing observation of the same bytes in that role, with exact
cast identity/count evidence. A canonical `payoff_board` can also be the one
starting composition when both roles were authored and observed; it keeps its
canonical ID and primary payoff role. Pending board slots are valid prospective
planning, but cannot qualify motion preparation.

Grok CLI carries the exact local starting image. Five-second Grok requests use
its supported `reference_to_video` operation with one first frame; the legacy
`image_to_video` operation accepts only its native six/ten-second durations.
OpenArt MCP `image2video` uses the exact uploaded starting image. Optional ending
and payoff targets remain bound semantic evidence. Separately declared native
references and required temporal pins still have to be supported and supplied.
`reference_guided` continues to require its separately attached native reference
route; authoring or amending a frame treatment never downgrades guided inputs.

One creator-approved correction can link an existing, explicitly guided standing
run to its existing starting compositions before any motion scope, preparation,
private reservation or submission exists. The engine operation changes only that
input treatment and its exact frame bindings. It preserves story/dialogue/cast,
ordered shots, routes/models/accounts/billing/transfer terms, allowances and all
counted originals. It retains original package, board pack, policy, activation,
canonical planning and image-envelope bytes in linked immutable history. Current
same-byte board observations remain available; changed project/shot planning and
preparation subjects need actual new reviews. The operation supplies no verdict.

The producer first obtains a pure, reviewable receipt:

```python
from lib.production_frame_amendment import (
    expected_original, plan_frame_input_amendment, apply_frame_input_amendment,
)

expected = expected_original(project_root)
plan = plan_frame_input_amendment(
    project_root, expected=expected,
    proposal={"path": "notes/frame-input-correction-proposal.md", "sha256": proposal_sha256},
    authority={"path": "notes/correction-delegated-approval-source.json", "sha256": authority_sha256},
)
```

`expected` binds the exact original package, board pack, current policy/activation,
canonical artifacts, image envelope, retained source/creator evidence and
cumulative usage. `authority` is the actual retained `delegated_authorization_source`
with project/story, exact proposal SHA, relay IDs/text and originating human
message evidence. The producer verifies that actual message provenance; synthetic
fixtures are never production authority. The plan includes `plan_sha256`, the four
`changed_paths`, successor policy/decision IDs, and zero provider calls and
reservations. Planning performs no writes, observations, uploads or application.

After the engine source is installed and independently reviewed, the production
owner can apply that exact receipt under the retained creator correction authority:

```python
receipt = apply_frame_input_amendment(
    project_root, expected_plan_sha256=plan["plan_sha256"], expected=expected,
    proposal=plan["proposal"], authority=plan["authority"],
)
```

The writer uses the existing engine admission and board locks. It authenticates
and retains originals before publishing a linked successor and appending the
activation decision. An incomplete publication fails closed for new admission;
explicit replay can resume only exact original or staged successor pointer bytes.
File contents are fsynced before local atomic publication. Unique snapshot staging
names permit restart before the pointer exists. The linked pointer handles process
interruption across files; it does not make the whole operation atomic or establish
power-loss durability for directory entries.
Completed replay is read-only and preserves refreshed reviews and later production
progress. Changed, revoked or superseded successor authority cannot be revived by
replay. Original image imports and authenticated historical collection remain
available after later revocation; historical authority never admits a new host
invocation. Image and motion usage remain cumulative in the same run.

## Compact package authoring (explicit opt-in)

The existing `compile_package` / `prepare_package` boundary accepts
`authoring_form: "shot_local_v1_compact_v1"` with `planning_mode: "shot_local_v1"`.
Full packages retain their existing behavior and bytes. Compact input must replace
`boards` with `compact`; mixing forms or adding unknown fields fails closed.
The original compact file and its SHA remain the canonical source binding.
Compilation writes nothing; preparation uses the same validators, board lifecycle,
source hashes, materialization and standing-activation gates as full packages.

This complete producer example needs real current project IDs and retained hashes:

```json
{
  "version": "1.0",
  "authoring_form": "shot_local_v1_compact_v1",
  "planning_mode": "shot_local_v1",
  "project_id": "CURRENT_PROJECT",
  "story_revision": "CURRENT_REVISION",
  "title": "The stuck drawer",
  "compact": {
    "reviewer": "NAMED_REVIEWER",
    "story": {"path": "artifacts/story.txt", "sha256": "CURRENT_SHA256"},
    "approval": {"path": "notes/approval.json", "sha256": "CURRENT_SHA256", "approved_by": "CREATOR"},
    "references": [],
    "story_meaning": {
      "desire": "Open the drawer.",
      "action": "Pull the drawer handle.",
      "consequence": "The drawer opens.",
      "payoff": "The missing key is revealed."
    },
    "late_cast_ids": [],
    "payoff_speaker_ids": [],
    "payoff_asset_id": "s1-start"
  },
  "shots": [{
    "id": "s1",
    "script": "",
    "action": "The motorized drawer opens, revealing a key.",
    "duration_seconds": 5,
    "end_state": "The drawer is open and the key is visible inside.",
    "cast": [],
    "preserve": ["The handle stays attached to the drawer."],
    "motion": {
      "reference_mode": "frame_conditioned",
      "frame_input": {"asset_id": "s1-start", "role": "start_frame"},
      "initial_state": "The motorized wooden drawer is closed.",
      "endpoint_completion": "unknown",
      "required_visible_speakers": [],
      "dialogue": [],
      "allowed_transformations": [],
      "transition": {"type": "continuous", "rationale": "One continuous drawer-opening action."}
    },
    "boards": [{
      "slot_id": "s1-start",
      "role": "payoff_board",
      "prompt": "A closed motorized wooden drawer; the drawer has not yet opened.",
      "reference_ids": [],
      "image_dependencies": [],
      "required_predicates": ["asset_quality", "cast_identity"],
      "transparent_background": false,
      "cast_ids": []
    }],
    "video_dependencies": []
  }]
}
```

All example fields are required, including empty arrays, explicit story meaning,
end state, cast, invariants and motion semantics. Only top-level `approval_preview`
and the full form's shot `pinned_initial_frame` / `pinned_final_frame` declarations
are optional. References retain the existing full source/reuse-evidence shape;
generation sources remain image-generation sources. A reference explicitly declaring
`role: "identity_reference"` also requires authored nonempty unique `cast_ids`;
expansion derives its exact contract asset and shared identity role. Those retained
source bytes/reviews remain subject to existing reuse and cast gates. Other reference
roles cannot declare `cast_ids` in this compact version.
Each generated board declares its own cast IDs and sources. Role names and semantic
values remain governed by the current shot-contract schema. Every shot must name
exactly one existing start board as its frame input. This form supports only
explicit frame-conditioned treatment; other treatments and shared roles/handoffs
continue to use full authoring.

Expansion derives the board's owning shot and stable
`assets/images/candidates/<slot_id>.png` path, ordered shot-role mappings,
contract asset entries, shot asset/upstream entries, and project/shot review
scaffolding. Those reviews and every predicate are `unknown`, with zero subject
hashes and an `UNOBSERVED` evidence marker; they are never passing observations.
Prompts, dialogue, cast, continuity, dependencies, ending targets and story meaning
are never inferred. `payoff_asset_id` is required by the existing contract and may
name an explicitly authored `payoff_board`. A single board may serve both roles:
its canonical board role is `payoff_board`, while explicit `motion.frame_input`
declares its consumer `start_frame` mapping. Expansion derives that mapping only
from the exact frame declaration. This does not infer another image or certify
that a payoff is visible. Compile output exposes the full expanded spec
and projections before activation. Existing providers, scopes and creator-command
requirements still apply.

A continuous dependent shot can start from the actual selected outgoing frame of
a shot it depends on. Author its starting board as a normal compact board with
every mandatory field, plus one optional key:

```json
{"slot_id": "s02-start", "role": "start_frame",
 "prompt": "Review guidance: S01's completed end state, Tung on the sofa.",
 "reference_ids": [], "image_dependencies": [], "transparent_background": false,
 "required_predicates": ["asset_quality", "cast_identity"], "cast_ids": ["patient"],
 "upstream_source": {"shot_id": "s01", "role": "outgoing_frame"}}
```

The source shot must be in the shot's `video_dependencies`, and the board must be
that shot's `motion.frame_input` and its only `start_frame`. A shot can have at most
one reuse board, and no other board may reference or depend on it. Its `prompt` is
review guidance only and is never sent to image generation. No image attempt is
reserved for this slot, and direct `prepare_image_request`/`reserve_board_image`
calls refuse it.

After the source shot's original is selected, the existing `bind_selected_upstream`
action binds the consumer contract and the board together. The binding records
the selected attempt ID, output, outgoing frame and review hashes with origin
`reuse`. An interrupted bind is rediscovered and completed on the next pass, and an
identical replay writes nothing. The consumer's start-role review starts missing and
goes through the normal `review_board` action. It never inherits the source
selection's review. If the source selection later changes, the bound board fails
closed at native input resolution and is never silently substituted.

### Shootable prompt compiler (explicit prospective opt-in)

New compact or full packages can opt into clean provider prompts by adding two
top-level fields beside `planning_mode: "shot_local_v1"`:

```json
"prompt_compiler": "shootable_v1",
"cast_names": {"patient": "Tung", "doctor": "Sahur"}
```

`cast_names` maps every cast, required-visible-speaker and dialogue-speaker ID to
an authored short readable name. Keep full identity descriptions in references
and body invariants, where they need not repeat for every spoken line.
`{}` is valid only when no shot has cast or
speakers. Never add placeholder names. Each shot can also carry optional `camera` and
`sound` strings, which are authored direction. Without the marker, `cast_names`,
`camera` and `sound` are rejected, and legacy prompt and coverage bytes stay unchanged.

The compiled prompt contains only shootable direction:

- opening frame, the single main action and the finished ending frame
- characters in the shot by authored name
- which speakers must be visible while speaking
- keep-consistent invariants, allowed changes with the invariants they preserve,
  camera, scene framing/movement/shot style, and the continuity or cut line
- each exact spoken line with its speaker name, on-screen or off-screen source and timing
- the sound intent

Every authored value is a literal span with ordered coverage, so a dropped or
changed directive still blocks preparation. Cast IDs, `endpoint_completion`,
reference roles/IDs, hashes and reviews stay in `source_binding` and canonical
records and never appear in the text. `source_binding.prompt_compiler` records the
compiler, and the contract digest binds names, camera and sound. Editing any of them
requires re-review.

Frame and reference routing are unchanged. First-frame routes use the actual
reviewed board, and guided references use only supported native forms. A dependent
shot starts from its upstream's actual outgoing frame only when its start board
declares `upstream_source` (see above); otherwise `video_dependencies` binds
provenance and the shot starts from its own reviewed board. `sound` is a prompt
direction only. It never sets or defaults `generateAudio`, and "no background
music" with dialogue/SFX is distinct from silence.

## Reuse the existing motion-preparation builders

For the current `prepare_motion` action, use these existing functions instead of
recreating coverage, source hashes, native bindings or compiled records:

```python
from lib import production_request as request, production_execution as execution

# inputs already names the approved route/native controls, actual frame sources,
# project_dir, governance={"shot_id": action["inputs"]["shot_id"]}, unique output_path,
# and the retained safe compiled_request_id / preparation_review_id.
authored = request.compile_provider_prompt(
    project_dir, action["inputs"]["shot_id"], provider=provider,
    **({"model": inputs["model"]} if provider == "openart_mcp" else {}),
)
inputs["prompt"] = authored["prompt"]
native = request.prep_builder(provider)(inputs, profile_or_observation)
compiled, bound_review = request.prepare_compiled_request(
    inputs, native, profile, coverage=authored["coverage"], timing=authored_timing,
    review=reviewer_authored_review,
)
# Persist compiled, preserving any previously retained different bytes, at:
# artifacts/compiled_request-<inputs["compiled_request_id"]>.json
# Persist bound_review, carrying the actual reviewer judgments, at:
# artifacts/preparation_review-<inputs["preparation_review_id"]>.json
outcome = {"inputs": inputs, "provider": provider}
if provider_observation is not None:
    outcome["observation"] = provider_observation
result = execution.complete_action(project_dir, action, outcome=outcome)
```

To avoid retyping the retained request, start from the pure draft helper. It
accepts only the exact current `prepare_motion` action and projects the active
policy's retained `planned_request_template` inputs unchanged, including native
audio and route controls. It also resolves each declared `$board`/`$upstream`
frame to its current reviewed bytes and attaches the existing
`compile_provider_prompt` prompt and coverage. It writes, reserves, uploads,
reviews and times nothing, and it never calls a provider:

```python
draft = execution.draft_motion_preparation(
    project_dir, action, provider=provider,  # provider only when the template names no route
    output_path="assets/video/<unique>.mp4",
    compiled_request_id="<unique>", preparation_review_id="<unique>",
)
assert not draft["missing"]        # producer-owned fields still absent
inputs = draft["inputs"]           # add any producer-owned route control the template lacks
authored = {"coverage": draft["coverage"]}  # draft["frames"] lists resolved frame SHA-256s
```

Continue with `prep_builder`, `prepare_compiled_request` and `complete_action`
as above. The producer still supplies the actual profile or observation, the
measured timing and the actual preparation review. Admission revalidates every
field against retained authority, so the draft grants nothing. A stale or
modified action, a foreign route or changed frame bytes are refused without
writes.

`compile_provider_prompt(project_dir, shot_id, *, provider, model=None)` returns
`prompt`, `coverage` and `source_binding`. `prep_builder(provider)` returns the
existing provider builder: OpenArt receives its current supported profile; Grok
receives its fresh `{cli_version, grok_path}` observation. The `profile` supplied to
`prepare_compiled_request(inputs, native, profile, *, coverage, timing)` is the
actual applicable preparation profile. This pure helper resolves current source
bytes again, derives the planned-request digest and native binding, and validates
the compiled sidecar. Keep its returned object intact.

Supplying the optional `review=` argument returns `(compiled, bound_review)`.
Author the reviewer, status, evidence kind and predicate judgments from actual
evidence. The helper fills the omitted review ID, compiled subject hash and
generated starting-board membership from authenticated original image inputs;
do not recopy those hashes and cast memberships by hand. Conflicting supplied
bindings reject. Provided or reused boards still require their explicit evidence.
No semantic verdict is inferred, and the existing admission gate still rejects
failed or unknown preparation. Calls without `review=` keep their original
compiled-only result.

`authored_timing` remains the producer's explicit measured-audio or segmented
estimate, including real dialogue/action windows, language, margin and overlap
rationale; the builder does not invent timing. The actual preparation review must
follow `schemas/artifacts/preparation_review.schema.json`, name the exact
`inputs["preparation_review_id"]`, and bind `request.digest(compiled)`. Live reviews
use `evidence_kind: "reviewed"` and actual evidence for each named preparation
predicate. Do not turn unknown observations into passes. Reuse assigned IDs and
retained files; these helpers do not create a second ledger.

The exact current action must be passed unchanged to
`complete_action(project_dir, action, *, outcome)`. Its `prepare_motion` outcome
allows only `inputs`, `provider`, and optional `observation`; the engine derives
scope, runs the governed dry-run and invokes the registered tool. This completion
is a dispatch boundary, so perform it only within the current run's retained
route/input/allowance authority. OpenArt uploads and other native prerequisites
still follow their existing provider contracts.

### Prepare an authorized alternate without changing the retained input layout

A native packet is builder output, not the public request to copy into the next
route. For an authorized Grok-to-Mini repair whose retained template has
`image_path: {"$board": {"asset_id": "S04-start"}}`, keep that same public
frame occurrence resolved to the exact current board. The existing root aliases
`image_path` and MCP's schema-supported `first_frame` name the same occurrence;
nested layout, order and exact source hashes still bind. Retain one public
`duration`, `resolution` and `aspect_ratio`; change only the approved route and
repair details. For example, the Mini preparation inputs can include:

```python
# This example is for the retained first-frame-only template above.
inputs = {
    "project_dir": previous_public_inputs["project_dir"],
    "governance": {"shot_id": shot_id},
    "first_frame": previous_public_inputs["image_path"],
    "duration": previous_public_inputs["duration"],
    "resolution": previous_public_inputs["resolution"],
    "aspect_ratio": previous_public_inputs["aspect_ratio"],
    "model": "byte-plus-seedance-2-mini", "mode": "image2video",
    "openart_project_id": retained_openart_project_id,
    "preferred_provider": "openart_mcp", "allowed_providers": ["openart_mcp"],
    "preferred_tool": "openart_mcp_video", "hosting_provider": "openart_mcp",
    "compiled_request_id": repair_request_id,
    "preparation_review_id": repair_review_id,
    "output_path": distinct_repair_output_path,
}
# Compile the alternate's prompt and fresh preparation using the builders above.
# prep_builder("openart_mcp") resolves the exact retained account/project upload.
```

Those controls are illustrative; use the actual retained values and supported
profile. The fresh dict intentionally does not carry the old Grok operation,
CLI timeout/session/cwd, billing acknowledgement or scope ID. Use distinct safe
request/review IDs and an output path that preserves the original. Other retained
templates may require additional semantic fields; their exact delta checks still
reject missing or changed inputs rather than silently dropping them. Do not replace this template's `image_path` with `input_assets`, or copy
resolved `native_params` alongside the same public controls. Omit optional native
count/seed controls only when the exact retained form's defaults match the approved
request. Audio follows that same rule: omitting `generateAudio` does not mean off
when its native default is on. An existing retained upload avoids another
source transfer; a missing upload still follows the normal authorized upload path.

An explicit creator-approved MCP audio-off packet can be adopted through
`pa.adopt_native_audio_off_approval`. Retain an approval JSON with exactly:

```python
record = {
    "version": "1.0", "kind": "native_audio_off_approval",
    "project_id": policy["project_id"], "story_revision": policy["story_revision"],
    "policy_sha256": policy_sha, "shots": approved_shot_ids,
    "route": {"provider": "openart_mcp", "model": profile["model"], "mode": profile["mode"]},
    "controls": {"generateAudio": False}, "form_sha256": profile["form_sha256"],
    "approved_packet": {"path": approved_packet_path, "sha256": approved_packet_sha},
    "authorization": {"path": retained_creator_message_path, "sha256": retained_creator_message_sha},
}
pa.adopt_native_audio_off_approval(
    project_dir, expected_policy_sha256=policy_sha,
    approval={"path": retained_approval_path, "sha256": retained_approval_sha},
)
inputs["native_params"] = {"generateAudio": False}
```

Use the existing `creator_authorization` shape: `version: "1.0"`,
`origin: "user_message"`, the standing run's `identity_id`, the actual `message_id`
and exact `text`, and `subject_sha256 = digest(record without authorization)`.
The subject binds the exact approved packet and named project/policy/shots/route;
an audio-review setting or silent export intention grants no native control authority.
The API validates the current form switch and existing locks, then appends only
the approval binding to `production_scopes.json`. It grants only boolean False
when the original audio control was omitted. Other native controls still compare
exactly; it grants no routes or counters. Derivation and begin revalidate live
authority. Emitted originals retain their exact proof for collection after
authority changes. Repeating the same adoption is read-only and makes no calls.

The alternate needs its own current prompt, compiled request, timing and truthful
preparation review. Name the exact previous failed attempt in the existing repair
scope; keep its original request, output and failure evidence. A different route
or unused budget alone grants no repair authority. The normal replacement-evidence,
route/account/project, source, review and cumulative-cap gates still apply;
uncertain originals remain collectable rather than eligible for a blind reroll.

## Retain a native image observation directly

A current `receive_native_image` action accepts either the existing bound pointer
or the observed receipt itself under the same `host_receipt` key:

```python
execution.complete_action(project_dir, action, outcome={
    "host_receipt": observed_receipt,
    # "supplemental_to": exact_unresolved_original_receipt_sha256,
})
```

`observed_receipt` must contain the actual observed `tool_name`, `arguments`,
`result`, and optional `tool_call_id`. Record the invocation and return as observed;
never populate missing name or arguments from the expected envelope. A bare media
result is insufficient. The image owner validates the observed invocation against
the frozen submitted arguments before retaining anything, then hashes and stores
it in the existing original-call directory. Provenance remains agent-recorded,
not provider-attested. Pointer receipts `{path, sha256}` keep their original bytes
and hashes; equivalent inline replay preserves them without reformatting.

For a large image return, retain the actual observation in `functions.exec` and
save it with `scripts/native_host_receipt.js`. Load that small helper once in the
producer context, before generation (use the actual configured engine path):

Keep encoded media and full raw receipts in saved files or host memory; never cat,
dump or base64-print them into the conversation transcript. Report only the original
ID, action ID, status, local path and hash. On resume, load the compact current action
and receipt pointers from canonical records, preserve unknown originals, and keep the
exact next envelope in host storage so its consumer sees only the current envelope,
not the full producer history.

```javascript
const helper = await tools.exec_command({
  cmd: "cat /ABS/OPENMONTAGE/scripts/native_host_receipt.js",
  max_output_tokens: 6000
});
if (helper.exit_code !== 0) throw new Error("Native receipt helper unavailable");
store("native-receipt-helper", helper.output);
```

Use `consumeNativeHostAction` with the exact `advance` or `complete_action`
step and an explicit actual host callback. Keep one absolute `receiptPath` per
original subject across retries and restarts; its parent must already exist.
Changing the path changes the ownership guard. The helper retains an attempted
sentinel and atomically claims `<receiptPath>.invoked` before entering the native
callback. Existing claims or receipts fail closed when the host observation is
lost. Never remove that marker or choose a fresh path to retry a paid job.

This minimal `functions.exec` recipe uses the existing helper and engine CLI;
configure the three absolute paths and retain the exact returned step in
`factory-step` before entering it. The initial helper load above happens before
any provider call. `functions.exec` has the actual `tools` namespace; the helper
uses the existing Node REPL only for durable files, never to invoke providers:

```javascript
// @exec: {"yield_time_ms":120000}
const engine = "/ABS/OPENMONTAGE", project = "/ABS/PROJECT";
const receiptPath = "/ABS/PROJECT/notes/ORIGINAL-SUBJECT-host-return.json";
const step = load("factory-step");
const api = new Function(load("native-receipt-helper") +
  "; return {consumeNativeHostAction, consumeSavedNativeCompletion, saveNativeHostReceipt, nativeHostReceiptBinding};")();
const quote = value => "'" + String(value).replaceAll("'", "'\"'\"'") + "'";
const next = await api.consumeNativeHostAction(tools, step, {
  receiptPath,
  nativeTool: {tool_name: "image_gen.imagegen", invoke: args => tools.image_gen__imagegen(args)},
  getObservation: key => load(key), retainObservation: (key, value) => store(key, value),
  completeAction: async (action, outcome) => {
    // For OpenArt only: canonical admission consumes the actual structured payload.
    // The helper already durably saved the full raw connector return unchanged.
    if (action.kind === "receive_openart_original") {
      const payload = outcome.result.structuredContent ?? outcome.result;
      if (typeof payload?.historyId !== "string" || !payload.historyId)
        throw new Error("Actual original historyId unavailable; collect original");
      outcome = {result: payload};
    }
    const packet = api.nativeHostReceiptBinding(await api.saveNativeHostReceipt(
      tools, {action, outcome}, receiptPath + ".complete-action.json"));
    const code = [
      "import json,sys; from pathlib import Path; from lib import production_execution as e",
      "root,packet,saved=sys.argv[1:]; path=Path(saved)",
      "if path.exists(): result=json.loads(path.read_text())",
      "else:",
      " p=json.loads(Path(packet).read_text()); result=e.complete_action(root,p['action'],outcome=p['outcome'])",
      " temp=path.with_suffix('.tmp'); temp.write_text(json.dumps(result)); temp.replace(path)",
      "print(json.dumps(result))"
    ].join("\n");
    let completed = await tools.exec_command({workdir: engine, max_output_tokens: 6000, yield_time_ms: 30000,
      cmd: [engine + "/.venv/bin/python", "-c", code, project, packet.path,
        receiptPath + ".completed-step.json"].map(quote).join(" ")});
    if (completed.session_id) store("original-completion-session", completed.session_id);
    return await api.consumeSavedNativeCompletion(tools, {
      completion: completed, completionPath: receiptPath + ".completed-step.json"});
  }
});
store("factory-step", next); // Carry this exact action and any next host_envelope.
text(next);
```

For an emitted OpenArt generation envelope use its exact tool name and the real
callback `args => tools.mcp__codex_apps__openart_openart_generate_video(args)`
instead of the image callback. The completion callback above explicitly selects
`structuredContent` for the existing engine's `historyId` contract, while the
receipt retains every field of the actual connector wrapper. A direct actual
payload is accepted only when it already contains that original ID. A raised
OpenArt transport exception is saved and leaves the original uncertain; no paid
retry or automatic status loop follows.

The helper returns the exact completion step, including any once-only next
`host_envelope`. Consume that envelope directly; another `advance` would lose its
emitted handoff. `provider_calls: null` preserves unknown actual host exposure.
After an interrupted save or completion, the retained actual observation permits
only saving/completing the same original. If completion already saved its returned
step, read that file rather than overwriting it or advancing again. If the
completion CLI remains running, use `tools.write_stdin` on the retained session
until it finishes; do not start a competing completion process. After lost
host storage or a process restart without the exact completion step, call canonical
`advance` to recover the current receive/collect action; never replay a saved
emitted generation envelope. A claim without a retained return stays unresolved
and collectable; it is not evidence of zero provider calls or authority to retry.

For large image returns the saver sends only small metadata and a SHA-256
fingerprint of the actual returned data URL to the existing Node REPL. It reads
the local generated file named in the actual host `output_hint`, reconstructs its
data URL and requires exact length and SHA-256 equality before saving. Every field
survives losslessly; missing or changed cached bytes fail closed. The original
cached image stays in place. Receipt creation is atomic and a different existing
receipt is never overwritten. Use the saved `{path, sha256}` as `host_receipt`;
do not retype data URIs or split them by hand. These mechanics still require actual
semantic review and do not certify unattended creative decisions.

An accepted OpenArt original with an actual `PENDING` or `RUNNING` observation can
allow one independent board lane through begin, receive and review before its
next status refresh. Existing local journal timestamps bound that lane; missing
or invalid ordering keeps status first. Original receive, uncertain collection,
terminal download and ready selection review keep priority. Independently eligible
motion remains eligible earlier; preparing a dependent board never supplies its
missing selected upstream media. There is one current action and no added timer,
parallel completion driver or scheduling ledger.

Unknown returns stay counted and collectable. A genuinely additive completed
return can use `supplemental_to` with the exact original receipt SHA, retaining the
original and its provenance. Changed fields, collisions and different replay
returns still fail. Interrupted retention resumes the same original; imported
replay never re-emits an image envelope or consumes another allowance.

## Review the project when its declared payoff is observable

When a missing, unknown or stale project review needs an unavailable declared
payoff board, the action selector can progress that board's existing source/image
dependency instead of immediately requesting the project review. It uses only an
existing prepare, receive or review action; pending original collection keeps its
priority. This is an ordering change for that exact dependency. It does not require
all episode boards, erase failed observations, or make a shot eligible. Current
passing project evidence leaves independent motion and local shot review behavior
unchanged.

A `review_planning` outcome can supply `{"shot_review": actual_target_verdict}`
when the retained project review already passes the existing checks for the exact
current protected-plan subject and story revision. The engine carries that project
verdict unchanged. Otherwise supply both `project_review` and `shot_review` with
actual current observations. Missing, stale, failed, incomplete or unknown project
evidence cannot be omitted, and the target still needs its own genuine review.
