# Overlapping Shot Preparation Under a Fixed Plan

Use this recipe when preparing a later shot can happen while an eligible,
already reviewed target shot moves through its existing governed path. This is
a sequencing option inside the current production contract; it does not create
independent per-shot authoring scopes or change any production gate.

## Prepare boards before waiting for motion

For new boarded episodes, prepare one shared current cast/prop inventory and
complete text prompt pack. Record unresolved board slots explicitly instead of
using absent image paths as ready motion evidence. Reuse exact current reference
bytes only with story/role applicability and the retained semantic observation;
reuse is zero generation. `lib.production_images.build_preboard_packet` is the
pre-board boundary; it does not weaken reviewed-motion source packets.

Keep the static-image DAG separate from the actual outgoing-video DAG. An end
image derived from a start image waits for imported start-image bytes, while its
text and unrelated nodes prepare immediately. Prepared reviewed start/end pairs
are the default. Chronology adds no predecessor wait; only declared actual
upstream footage/outgoing-frame requirements serialize motion. Prioritize ready
shared cast/payoff nodes by the number of shots their byte/review completion
unblocks, then let the existing canonical dry-run decide exact motion readiness.
The whole storyboard need not finish first when the target and shared
prerequisites are current under the fixed complete plan below.

Native image calls remain agent-host operations. Reserve through
`lib.production_images.reserve_native_image`, emit exactly one admitted envelope
with `begin_native_image`, invoke the real host tool once, retain its actual
return, and import attributable original bytes with `import_native_image`.
A local call ID is not a provider job ID. Unknown original acceptance stays
counted and unresolved; do not re-invoke it. The host surface is one prompt,
optional ordered reference paths and transparency. No `n`, batch, concurrency,
forced resolution or provider switch is implied. Exact scope and approved image
allowance remain binding across supported routes.

Studio's bounded `prepare_boards.py` consumer retains inventory/text once,
materializes exact source packets as dependency bytes arrive, and uses that
canonical lifecycle. Its `production_entry.py` adapter exposes `prepare-boards`,
`board-status`, `prepare-board-request`, `reserve-board-image`,
`begin-board-image`, `import-board-image`, `review-board`, `board-observations`
and `ready-video-work`. Queue candidates are preparation facts; only a fresh
canonical dry-run can permit the unchanged exact request.

Name one persistent substantive board reviewer. Reuse one exact-byte observation
for its applicable roles and transfer it unchanged into project/delivery records.
Minor aesthetic findings proceed with warnings. Essential cast, story or staging
failures stay critical and permit only a targeted correction within current
image authority; unknown/missing essential evidence remains blocking. Review
later ready boards while preparation/rendering continues. Changed board
promotion stales affected source packets, derived bindings and observations;
unchanged valid roles carry. Preparation promotion does not itself alter frozen
motion plans or grant dispatch. Record actual preparation, import, review and
promotion boundaries; absent provider timestamps remain unknown.

## Keep the canonical plan fixed

The existing shot contract can validate one requested target and its dependency
closure while a future shot still lacks its local review. This is only true
when the complete authored plan remains unchanged and schema-valid, and the
target plus all shared prerequisites pass. Project and target reviews, shared
payoff review, and required late-cast identity evidence remain mandatory. A
future shot's missing local review is not evidence that its candidate is ready
for use.

While the target proceeds, prepare a later candidate only at an unbound,
project-local location such as `assets/images/candidates/`. Keep it outside
canonical inputs. Do not edit `script`, `scene_plan`, the shot contract,
manifest, production scope, approval evidence, or any bytes bound to the
current request. Candidate preparation is not a reviewed frame or an observed
shot result.

Preparation for a declared independent later shot may overlap the current
reviewed shot's generation when it leaves that request and its dependency
closure unchanged. Keep one production owner for the active request and reuse
its current passing review. A parent verifies bindings and required-predicate
completeness and opens only a missing predicate, named dispute or changed
binding, without a second complete pixel review by default. The later shot
still needs its own current review and exact authority before promotion or
dispatch. Do not impose an episode-wide serial wait on independent, unbound
preparation; enforce real dependencies when the candidate is promoted or the
exact request needs current upstream selections and hashes.

## Operator sequence

1. Name the already approved target and identify later candidate work that does
   not change its authored request, plan, or dependency closure.
2. Preserve the complete canonical plan and verify that target-specific and
   shared reviews remain current. A canonical dry-run is the eligibility
   decision; do not infer readiness from a board, candidate, or earlier pass.
3. Immediately before dispatch, the single production owner refreshes registry
   discovery and runs the canonical `dry_run` for the exact reviewed request.
   If it passes, use only the existing governed `execute` path and its exact
   user authorization, attempt allowance, director/checkpoint rules, and
   provider-consent and cost gates. If it blocks, stop and report the blocker.
4. Keep the later candidate unbound until its own review and promotion are
   ready. Promotion that changes an authored path, hash, or semantics changes
   the canonical plan: applicable global approvals become stale, and retained
   selections or final provenance may also become stale. Run the supported
   fresh review and compilation where the installed route requires it, obtain
   applicable exact authority, and revalidate retained footage. If the route
   cannot perform that promotion, stop and return it to the production owner;
   never hand-edit frozen scopes or claim an old selection remains valid.
5. A dependent target waits for the current selected upstream attempt, matching
   output and observed outgoing-frame bytes, and required continuity review.
   A planned board, candidate, or continuity note cannot stand in for the
   selected observed outgoing frame.

Existing directors, checkpoints, Layer 3 guidance, and review gates continue
to apply. This recipe grants no image or video generation, approval,
publication, purchase, or resubmission authority. Read
[`checkpoint-protocol.md`](checkpoint-protocol.md) for approval handling and
[`../creative/visual-development.md`](../creative/visual-development.md) for
visual and continuity review.
