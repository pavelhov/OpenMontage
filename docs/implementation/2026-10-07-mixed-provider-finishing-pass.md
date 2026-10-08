# Mixed provider finishing pass — October 7, 2026

This note records the bounded local workflow and its evidence. It does not activate an Auto-continue policy, approve another generation, or certify visual quality.

## Model intent and planning

Studio preserves a creator's exact model request, preference, or goal in `model_selection_intent`. `video_selector` plans only within an explicit `approved_pool`:

- `exact` pins one named provider/model route and never substitutes.
- `prefer` selects the requested model if eligible; planning may use only an eligible alternative already in the approved pool.
- `auto` ranks eligible entries from that approved pool; it never expands the pool.

For a planning-only request, use `operation: "rank"` with the target generation operation. The selector returns `data.model_selection` and a singleton `data.planned_request` with `dispatch_status: "not_dispatched"`. Approve the exact plan, provider/model pool and its limits through the ordinary production approval path; then freeze/derive the exact request through the normal preparation, dispatch, review, repair and assembly flow. Ranking does not refresh account status, reserve credits or grant dispatch authority.

Example rank request, assuming the user has explicitly approved both routes and their billing/attempt limits:

```json
{
  "operation": "rank",
  "target_operation": "text_to_video",
  "prompt": "A quiet, reference-free cutaway of a red cube settling on a gray table.",
  "duration": "1",
  "aspect_ratio": "16:9",
  "resolution": "720p",
  "model_selection_intent": {
    "mode": "prefer",
    "goal": "balanced",
    "provider": "openart_cli",
    "model": "pixverseV6",
    "approved_pool": [
      {"provider": "openart_cli", "model": "pixverseV6", "tool": "openart_cli_video"},
      {"provider": "grok_cli", "tool": "grok_cli_video"}
    ]
  }
}
```

The sample request describes the qualified PixVerse V6 reference-free control path recorded below; it is not permission to execute it again. For `exact`, name a provider/model that resolves to one approved route. For `prefer`, include the named model and only the alternatives the user has approved. For `auto`, include every allowed route. Grok CLI is managed media and has no selectable video model; do not supply the agent's language model as a video model. See [`VIDEO_MODEL_SELECTION.md`](../VIDEO_MODEL_SELECTION.md) for goal and ranking semantics. `max_clean` and `balanced` use a control-fit/transport heuristic, not a quality benchmark or one-shot guarantee. `best_value` compares only comparable known costs when comparison is needed; OpenArt and Grok CLI cost remains unknown and is never treated as zero.

## Unknown-cost Auto-continue

The separately approved OpenArt policy variant is:

```json
{
  "id": "openart_cli",
  "billing": "unknown_cost_no_ceiling",
  "exposure_acknowledgement": "no_enforceable_credit_ceiling",
  "account_id_sha256": "<approved account identity digest>",
  "workspace": "__unobserved_workspace__",
  "routes": [{"model": "pixverseV6", "mode": "text2video"}]
}
```

This is a schema illustration; the digest and route must come from the user's separately approved, retained policy. It explicitly means there is no enforceable credit ceiling. Charge and USD cost remain unknown. Existing exact-quote policies keep their quote/credit-ceiling behavior and do not migrate. Implementation approval does not activate this policy.

The existing `max_total_attempts`, `max_attempts_per_shot`, and `max_repair_attempts` caps apply to priced and unknown-cost attempts. Each derived one-attempt scope counts, including open scopes, failures and unresolved attempts; these caps limit attempts, not spend. Optional `model_selection_intents` map approved shot IDs to exact/prefer/auto intent and cannot exceed the policy's approved routes. Under an active unknown-cost policy, the caller supplies retained `unknown_cost_evidence_id`; rooted `derive_scope` creates the occurrence-bound authorization. Caller-supplied authorization IDs or objects, including values injected through the planned template, are refused. The report keeps exact accounting in `openart_credits` and exposes a separate `openart_unknown_cost` section with `cost_status: "unknown"`; each unknown attempt's `requested_charge` remains `unknown`.

## Shot-level reference-free cutaway

A board-backed episode may mark one independent OpenArt text-to-video cutaway with `shot.reference_mode: "reference_free"`. That shot must have no local cast IDs, visible speakers, dialogue, asset IDs, or upstream sources. It cannot satisfy a required frame pin; manifest reference or handoff obligations block preparation. Its request must use OpenArt text-to-video and contain no reference or native-audio controls. The rest of the episode retains its normal project boards, payoff, cast evidence, and requirements for other shots.

This allows an independent OpenArt cutaway beside a separate referenced Grok shot. It does not let OpenArt replace the same shot's cast, dialogue, or reference continuity, drop required pins when Grok is unavailable, or inherit another shot's approval. The existing project-level `reference_mode: "reference_free"` remains the distinct all-text, noncast contract. Unknown-cost authority does not loosen either boundary.

## Evidence and limits

- **One live OpenArt sample:** [`2026-10-07-openart-live-720p-test.md`](2026-10-07-openart-live-720p-test.md) records one real PixVerse V6 reference-free T2V submit and collect at 1 second, 16:9, 720p. The result is H.264 1280×720 at 24fps with 1.041667 seconds of decoded video. One original submission, zero uploads, zero repairs. Transport/result reconciliation and collected bytes are verified. Creative quality was not reviewed, production certification is false, billing is unknown, and the observed Free account is specific to that test. H3 Max/Turbo are candidates only. Cast references, native dialogue/voices, multiple references, and ending-frame controls are not qualified; there is no perfect-one-shot or comparative-quality claim.
- **Mixed episode integration:** [`tests/integration/test_mixed_cli_episode.py`](../../tests/integration/test_mixed_cli_episode.py) passed 12 tests in 63.76s, as reported in `/tmp/mixed-cli-episode-tests-2026-10-07.md`. It exercises canonical selector/rank, policy scopes, OpenArt/Grok request preparation, original-job handling, output review/selection, and a real local 2-second FFmpeg assembly. CLI programs, provider receipts/media, human approvals, and semantic reviews are synthetic fixtures. This is offline workflow evidence, not live qualification.
- **Unknown-cost policy:** [`tests/integration/test_unknown_policy_authority.py`](../../tests/integration/test_unknown_policy_authority.py) passed 18 tests, and the worker reported 447 related broad regression tests passing in 325s. Tests cover rooted authority, tamper/revocation refusal, attempt accounting and unknown-cost reporting; they do not replace root's final combined regression acceptance. Details are in `/tmp/unknown-cost-autonomy-finish-2026-10-07.md`.
- **Final acceptance:** the root's combined regression passed **777 tests in 423.08s**, with exit code 0. Repository lint, compilation of the changed Python modules, and `git diff --check` passed. Independent reviews accepted model-intent validation, the shot-level reference-free preparation/provenance bridge, and the unknown-cost authority/report changes. Existing planning-revision and repair-continuity tests passed alongside this work; their implementation belongs to the concurrent Studio changes. No live provider call, upload, purchase, policy activation, commit or push occurred in this finishing pass.

## Reproduce the final regression

Run from the repository root with the existing virtual environment:

```bash
.venv/bin/python -m pytest \
  tests/lib/test_unknown_cost_autonomy.py \
  tests/lib/test_production_autonomy.py \
  tests/lib/test_production_autonomy_report.py \
  tests/lib/test_openart_unknown_cost.py \
  tests/lib/test_production_video_guard.py \
  tests/lib/test_production_video_guard_unpriced.py \
  tests/lib/test_provider_credit_ledger_unpriced.py \
  tests/integration/test_openart_auto_continue.py \
  tests/integration/test_autonomy_runtime_boundaries.py \
  tests/tools/test_video_model_selection.py \
  tests/tools/test_video_selector_routing.py \
  tests/tools/test_openart_unknown_cost_tools.py \
  tests/contracts/test_openart_cli_contract.py \
  tests/integration/test_openart_unknown_cost_workflow.py \
  tests/integration/test_unknown_policy_authority.py \
  tests/lib/test_production_retime.py \
  tests/lib/test_shot_contract.py \
  tests/lib/test_production_request.py \
  tests/lib/test_shot_reference_mode.py \
  tests/integration/test_openart_reference_free_selection.py \
  tests/integration/test_mixed_cli_episode.py \
  tests/integration/test_repair_continuity_workflow.py \
  tests/integration/test_planning_revision_workflow.py \
  -q --tb=short
```

These are offline software tests. Provider responses, account evidence, human approvals and semantic reviews are isolated synthetic fixtures; media encoding, governance, retained authority, collection, selection and assembly use the actual local implementation.
