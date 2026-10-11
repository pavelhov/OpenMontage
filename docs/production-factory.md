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
