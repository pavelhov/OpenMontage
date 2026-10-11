# Standalone production factory

OpenMontage is the canonical production engine and remains usable without Social Studio, an identity workspace or Drive. Its production package is the authored source for the script, ordered shots, actions, durations, cast, endings and protected invariants. With `planning_mode: "shot_local_v1"`, the engine derives the shot contract from those authored shots; do not copy those same facts into a second plan for approval.

The package's `boards` section declares only the references and image slots needed by that story. A shot may need one reviewed starting image, an ending image, a payoff image, or none of those; these are chosen from the actual action and supported treatment. Do not require three boards per shot by default. Semantic reviews and board observations must come from actual evidence. Unknown placeholders remain unknown and block only the work that depends on them.

The account's one-time standing setup and the explicit `make_video` command bind a creator-authorized identity, source/rights scope, routes/accounts, source-transfer limits, and generation caps. The creator's command activates the current authored plan for that account. Internal planning, eligible board work, dispatch, evidence review, any already-authorized bounded repair, composition and delivery continue in the same producer run. There is no routine plan or per-shot approval step. Missing setup, rights, source permission, account readiness or scope blocks only the affected work; an expanded route, source or allowance needs new authority.

The producer handles each actual returned host envelope and continues the same run. OpenMontage owns execution, original-job recovery, cumulative counts, composition and cut status; it does not invent semantic findings. A saved cut may be an honest draft with disclosed defects or unknown evidence. Certification remains a separate evidence gate. One initial take is the default; repairs use only exact retained authority and caps. Provider submission counts can be unknown for the whole run when host-mediated work or provider-managed batching obscures them. Never report unknown as zero, automatically resubmit an uncertain original, add a timer/service/ledger, or schedule or publish from this factory.

Standalone delivery returns the engine cut and export report. Account clients may add identity-specific paths, attributable stills and an existing backup request. A queued backup is not verified. Account closeout must not be required by the standalone engine.

Standing activation validates `checkpoint_stages` against the selected pipeline's
actual human gates before writing authority. A setup may cover more stages than a
particular pipeline gates; the run policy names only that pipeline's gated stages.
For an unstarted run written by the older invalid-stage activation path,
`lib.production_autonomy.reconcile_standing_checkpoint_stages(project_dir,
expected_policy_sha256=...)` removes only declared ungated stages. It retains the
original policy, evidence and decision, appends a linked successor, and preserves
all other authority and allowances. It cannot amend a changed or revoked run.
