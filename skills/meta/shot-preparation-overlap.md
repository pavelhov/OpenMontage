# Overlapping Shot Preparation Under a Fixed Plan

Use this recipe when preparing a later shot can happen while an eligible,
already reviewed target shot moves through its existing governed path. This is
a sequencing option inside the current production contract; it does not create
independent per-shot authoring scopes or change any production gate.

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
