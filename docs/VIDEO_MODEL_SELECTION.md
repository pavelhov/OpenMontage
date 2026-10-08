# Video model intent

Studio should preserve what the creator asked for when planning a video route. A creator can name an exact model, name a preferred model and allow a planned alternative, or ask OpenMontage to choose within an explicitly approved pool. The agent translates that intent into a structured request and shows the resulting route and limits. The creator does not need to write CLI commands or model-selection JSON.

## Intent modes

`video_selector` accepts `model_selection_intent` with an explicit `approved_pool`:

```json
{
  "mode": "prefer",
  "goal": "balanced",
  "provider": "openart_cli",
  "model": "pixverseV6",
  "approved_pool": [
    {"provider": "openart_cli", "model": "pixverseV6"},
    {"provider": "grok_cli"}
  ]
}
```

The example is illustrative; a route belongs in a real pool only when the user has approved that provider/model scope and its applicable billing mode, account, stages and attempt limits.

- `exact` requires the instructed provider or model to identify one route from the approved pool. It never substitutes another route. If the same model name exists under multiple providers, name the provider too. A managed-media route such as Grok CLI has no selectable video model; do not put the agent's language model in its video `model` field.
- `prefer` requires a model. Planning chooses that model when it is eligible; if it is unavailable or fails native control/readiness checks, planning may choose an eligible alternative already in the approved pool. During execution, an alternative can be used only as a semantic repair after the original attempt is terminal, review names a critical defect the alternative can address, the route is already in the approved pool, and a fresh governed repair scope or valid Auto-continue authority covers it. `exact` intent never changes. This is not an automatic retry.
- `auto` chooses among eligible entries in the approved pool. It does not discover approval for a new provider or model, and it does not widen the pool.

`approved_pool` is required for every mode. Each item names a `provider` and may include a `model` and exact `tool`. Exact and preferred model requests must identify an item in the pool.

## Goals and limits

The optional `goal` is `best_value`, `max_clean` or `balanced`; the current default is `balanced`.

- `best_value` compares known costs in the same unit when it must choose among pool candidates. For ordinary providers, this may use their declared cost estimate, labeled as an estimate rather than a benchmark; for a CLI route it would require a quote applicable to the exact request. OpenArt CLI and Grok CLI costs are currently unknown, so an automatic choice or a fallback comparison involving either blocks with `cost_not_comparable`. If a preferred route is eligible, `prefer` honors that route without claiming it is cheapest. Unknown is never treated as zero. An exact user instruction can still be planned with cost marked unknown, subject to that route's independent billing authorization.
- `max_clean` and `balanced` use an explicit control-fit and transport-evidence heuristic. This is not a visual-quality benchmark, a clean-first-attempt predictor or a guarantee. Ties follow the approved pool order; no vendor quality ranking is implied.

Planning rejects unavailable providers, unsupported settings, missing controls, and routes without current schema, account, and transport readiness. It preserves requested settings rather than normalizing or silently dropping them. A route's observed controls and transport support establish native request readiness only; they do not establish aesthetic quality, continuity, native dialogue or comparative superiority. Paid result qualification is optional diagnostic evidence, not a production gate.

## Planning, approval and dispatch

For a rank-only request, the selector returns `data.model_selection`, a singleton `data.planned_request`, and `dispatch_status: "not_dispatched"`. The plan includes the selected route, eligible and rejected candidates, the original intent, cost evidence status and limitations. Ranking makes no account call, reserves no credits and grants no dispatch authority.

For governed production, retain the original intent and the exact planned request in the approved plan. Derive a bounded scope through the existing governance path and send the singleton request through the usual preparation, dispatch, review, repair and assembly flow. An alternative provider/model can be selected during planning only when it is already in the user-approved pool and the retained per-shot intent allows it. A semantic-review repair may choose an already-approved alternative only when the original result is terminal, the review records a named critical defect that route can address, the intent is not `exact`, and a fresh governed repair scope or valid retained Auto-continue authority covers the request under cumulative attempt caps, continuity locks, and all other checks. Transport, authentication, quota or uncertain-job errors require reconciliation of the original attempt and do not grant route authority. No unresolved attempt is resubmitted or replaced.

An OpenArt unknown-cost choice explicitly means there is no enforceable credit ceiling; its charge and USD cost remain unknown. It is never represented as free, zero-cost or affordable. Exact-quote approval retains its existing quote and ceiling rules. The Auto-continue policy variant uses `billing: "unknown_cost_no_ceiling"` and requires `exposure_acknowledgement: "no_enforceable_credit_ceiling"`, a bound `account_id_sha256`, `workspace: "__unobserved_workspace__"`, and exact `{model, mode}` routes. The existing `max_total_attempts`, `max_attempts_per_shot` and `max_repair_attempts` caps apply to priced and unknown-cost attempts alike. Every derived one-attempt scope counts, including open scopes, failures and unresolved attempts; these caps limit attempts, not spend. When that policy is active, the caller supplies only an `unknown_cost_evidence_id`; the rooted derivation creates the policy-approved occurrence-bound authorization. Caller-supplied authorization IDs or objects are refused. Implementation approval alone does not activate a policy, and no exact-quote policy migrates. Optional per-shot `model_selection_intents` must name approved shots and can include only providers/models in the policy's approved routes. Do not infer that an unknown-cost OpenArt route is lower value than a priced route or use Grok's unknown subscription quota as a price comparator.

There is no forced provider mix. Use multiple approved routes only when the creator's intent and retained scope allow it, the shot requirements fit, and ordinary review accepts the results. Never add a provider silently, promise automatic quality gains, or ask the creator to manage raw provider commands.

## Current OpenArt evidence

The October 7, 2026 qualification record documents one real, collected reference-free PixVerse V6 text-to-video result: 1 second, 16:9, 720p, producing a 1280×720 H.264 clip at 24fps with 1.041667 seconds of decoded video. There was one original submission, no upload and no repair. Transport and collected bytes were verified; creative quality was not certified. The observed Free account and result apply to this specific test and do not establish a general entitlement, price or affordability.

This evidence does not establish creative quality, qualify comparative model ranking, or predict first-attempt cleanliness. Native control support is determined from the exact model/mode form and transport; missing bindings remain blocked. See the [dated result record](implementation/2026-10-07-openart-live-720p-test.md) for the evidence and its limits. Current account readiness and exact native route support must still come from fresh retained evidence and the provider menu.

## Reference-free OpenArt cutaways

In a board-backed episode, set `reference_mode: "reference_free"` on an individual shot only when it is a genuinely independent text-to-video cutaway. That shot cannot carry local cast IDs, visible speakers, dialogue, asset IDs or upstream bindings. Its request must use OpenArt text-to-video and contain no reference or native-audio controls. Required frame pins or asset-manifest reference/handoff obligations block the shot. The episode keeps its project-level boards, payoff, cast evidence and all normal requirements for its other shots.

The existing project-level `reference_mode: "reference_free"` remains the distinct all-text, noncast contract. Shot-level reference-free mode does not let OpenArt replace Grok for the same cast/dialogue/reference-dependent shot, drop required pins when Grok is unavailable, or inherit another shot's approval. A separate Grok reference-led shot and an OpenArt text-only cutaway may appear in one episode only with each route and shot independently covered by the approved policy. See [the OpenArt route contract](OPENART_CLI.md#reference-free-and-provider-qualification-paths).

OpenArt MCP has a separate Auto-continue provider variant: `id: "openart_mcp"`,
`billing: "unknown_cost_no_ceiling"`, explicit
`exposure_acknowledgement: "no_enforceable_credit_ceiling"`, an immutable
`uid_sha256`, the exact OpenArt `project_id`, and named `{model, mode}` routes.
Only modes supported by the current account, exact schema and transport are
eligible. A fresh retained policy can
include Grok CLI, OpenArt CLI and MCP together; existing CLI approval never
authorizes MCP or migrates to it. MCP body, form, native JSON types, source
roles/bytes, preparation and story locks are replayed before the original begin.
The total, per-shot and repair caps apply to all originals and open scopes,
including uncertain ones. Costs remain unknown; caps do not bound credits.
Caller billing objects and flags cannot activate the policy.

Include MCP models only through the user's explicit model choice or freshly
approved selection pool. OpenArt's hosted Grok catalog does not change the
approved Grok CLI preference or create an automatic default. A missing control,
exhausted quota or unresolved original blocks generation; it never authorizes
provider substitution or a replacement submit. Each generation still announces
the exact route, policy hash, phase, remaining caps and no-ceiling exposure.
