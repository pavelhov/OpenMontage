# Creator-accepted duplicate-bill draft exception

`lib.production_draft.append_creator_draft_exception(project_dir, record)`
records explicit creator acceptance of one duplicate-bill defect in footage
that already exists. It grants no generation, retry, billing, quota, planning
revision, or final-certification authority. Keep the original critical failure.

The record has exactly these fields:

- `version`: `1.0`
- `mode`: `creator_accepted_existing_footage_draft`
- `project_id`, `story_revision`, `shot_id`, `attempt_id`
- `contract`: `{path, sha256}` of an immutable current-contract snapshot
- `output`, `outgoing_frame`: exact selected `{path, sha256}` records
- `failed_review`: `{path, sha256}` of the original canonical attempt rejection
- `failed_predicate`: `prop_body_invariants`, `possession` or `transformation`
- `defect`: `duplicate_bill`
- `accepted_by`: named creator
- `acceptance_evidence`: `{path, sha256}` of retained explicit creator acceptance
- `outgoing_review`: separate named passing review bound to outgoing-frame SHA,
  with every upstream predicate and `prop_body_invariants` passing critically

The helper checks original attempt provenance and exact current planning before
atomically appending a content-addressed, immutable exception record. Its return
value is a `{path, sha256}` reference. The selection review must be `provisional`
and include that reference as `creator_draft_exception`, retaining the original
failed predicate object exactly; all other visual predicates must pass critically. Only an independently valid
audio-unavailable draft policy can also retain `speaker_source: unknown`.
Bind that review to `selection_digest(selection)` and use normal
`production_execution.record_selection`.

Current and frozen upstream gates replay the exception, every retained rejection,
creator evidence and actual output/outgoing bytes. Another original critical
FAIL or UNKNOWN blocks; only the exact accepted predicate object and separately
authorized unknown speaker evidence may remain. Replay preserves full target
shot planning via `production_continuity.shot_planning_digest`; legitimate
later upstream observations do not require contract-hash rewrites. Changed
story, invariants or other authored semantics reject the exception. A failed or
unreviewed outgoing state blocks downstream motion. Original scopes, attempts,
failures and counters remain unchanged.

The audio-unavailable draft authority remains separate. Both references may
appear on one provisional review; the existing audio policy is validated
independently and grants only its original unknown-speaker exception. Final certification rejects provisional selections and
critical failures even if the master review claims PASS. Produce a clearly
labeled draft, preserving the unresolved finding. This mechanism does not
provide automatic review upgrades for already-frozen downstream dependencies.
