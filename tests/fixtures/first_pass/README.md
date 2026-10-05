# Offline first-pass evidence fixtures

These are **synthetic contract records**, not media-quality evidence. The tiny
SVG files deliberately share identical bytes across distinct roles; they depict
no real cast or action. C50-derived labels reproduce the *recorded failure
categories*, not an automated assessment of C50 footage. No generated media or
production receipts are included.

`valid_shot_contract.json` is a complete current-version example and validates
with this directory as `project_dir`. `c50_contract_negatives.json` describes
field mutations and expected blockers. The tests also materialize tiny upstream
files in a temporary directory for dependency checks.

## Public API

```python
from lib.shot_contract import validate_shot_contract

result = validate_shot_contract(
    contract,
    project_dir=project_root,
    shot_id="entry",
    story_revision=current_project["story_revision"],
    selected_upstream=current_selections,
)
assert result["eligible"], result["errors"]
```

This gates **motion only**. Authorized preproduction image jobs must remain able
to create the boards before reviews exist. Motion requires both the chosen
shot's records and the project's reviewed payoff board/late-cast references.
The executor owns authorization, project identity checks, disk loading, selected
attempt truth, and immutable input snapshots. It must not pass stale expected
selection bindings back as if they were current observations.

Recommended sidecar: `artifacts/shot_contract.json`. An optional scene-plan
pointer is `{"shot_contract":{"path":"artifacts/shot_contract.json",
"version":"1.0","story_revision":"story-1"}}`. Old scene-plan v1 artifacts
without that pointer remain structurally readable, not motion-eligible for a
strict project. Draft contracts may be incomplete and read directly as JSON;
only current matching validated evidence qualifies for motion.

## Digest projections and reviewer responsibilities

- `contract_digest(contract)` hashes the complete plan except `project_review`,
  `assets[].review`, and `shots[].review`. It retains asset hashes and expected
  upstream review hashes. Project and shot planning reviews bind this digest.
- Asset reviews bind the asset's `sha256`; the enclosing plan binds its role,
  path, cast, and usage. Equal bytes do not merge identity/endpoint roles.
- `selection_digest(selection)` binds exactly `attempt_id`, `output`, and
  `outgoing_frame`; each file record contains `path` and `sha256`. The selection
  review binds this digest and the current story revision.
- `review_digest(review)` hashes the entire review, including reviewer identity,
  review ID, subject hash, named findings, and evidence. A dependent shot's
  `upstream[]` binds this digest, attempt ID, output hash, and outgoing-frame hash.

The current selections mapping is keyed by upstream shot ID, with values:

```json
{
  "attempt_id": "entry-first-1",
  "output": {"path": "assets/video/entry-first-1.mp4", "sha256": "64 hex characters"},
  "outgoing_frame": {"path": "assets/images/entry-first-1-end.png", "sha256": "64 hex characters"},
  "review": {
    "review_id": "entry-first-1-av-review",
    "reviewer": "reviewing agent or human",
    "story_revision": "story-1",
    "subject_sha256": "selection_digest result",
    "status": "pass",
    "predicates": [{"name": "completed_action", "status": "pass", "evidence": "Full action reviewed; describe observed completion."}]
  }
}
```

The abbreviated review above also needs every `UPSTREAM_PREDICATES` member:
`cast_identity`, `cast_count`, `completed_action`, `speaker_source`, `possession`,
`transformation`, and `outgoing_frame`. Project and asset required sets are
exported by the module. Absent, failed, unknown, contradictory, or stale critical
records block; named critical predicates cannot be relabelled cosmetic.
Explicitly cosmetic custom predicates produce warnings while overall review
status remains pass. No review-round or budget override exists.

Agents or humans must actually assess the action, cast, possession, permitted
transformation and speaker coverage. Python checks declared planning fields,
review structure, named statuses, bindings and bytes. It cannot establish that
reviewer evidence is truthful or that synthetic success predicts visual quality.
