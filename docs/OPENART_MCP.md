# OpenArt video through MCP

OpenMontage can hand video requests to the user's installed, signed-in OpenArt connector. `openart_mcp_video` is a separate route from `openart_cli_video`; their accounts, controls, and approvals are independent. The observed MCP catalog contains 45 model/mode forms. A route is production-ready when the current pinned catalog row, valid exact native form/schema, current authenticated account identity, and known mode/operation are present; each exact request must also pass native role, parameter, and schema validation. Paid live-result or quality qualification is optional diagnostic evidence, not a production prerequisite. Never describe a schema-supported route as live-tested or quality-reviewed unless it is.

OpenMontage owns the governed execution; MCP and CLI are provider connections. Choose the connection while proposing the episode, based on the shot's required native controls and the connection available to the current agent environment. Agent-mediated MCP calls require the OpenArt connector to be installed in that environment; terminal Python or the CLI alone cannot invoke MCP. The official OpenArt CLI 0.1.1 is suitable for standalone automation and simple text-to-video or start-frame image-to-video requests. MCP exposes the full observed model/mode form, including end-frame, reference roles and audio controls where that exact form supports them. This is a capability distinction, not a claim that MCP produces better results for the same model. Never silently change connection, account, or billing route.

## Refresh connector observations

Reuse valid retained schema/setup while the exact model, mode and connection context is unchanged and the existing project/account/source/policy bindings remain valid. When schema observations are stale or unavailable, refresh through the user's signed-in connector: call `mcp__codex_apps__openart_openart_model_list({})`, then `mcp__codex_apps__openart_openart_model_form_get({model, mode})` only for the selected model/mode(s) needed for this work. Refresh mutable account readiness with `mcp__codex_apps__openart_openart_account_get({})`, and request-applicable quotes and source evidence as needed. Keep each exact `structuredContent` response with its tool name, arguments, and actual observation time. Do not query a browser or use local credentials to substitute for these tools.

Retain the complete observations with `lib.openart_mcp.retain_observation('schema', schema_observation)` and `retain_observation('account', account_observation)`. Include `version: "1"`, `transport: "openart_mcp"`, and the actual observation time in each envelope. The schema envelope records the `schema_observation_only_not_dispatch_authority` classification, the model-list result, and each form result with its exact `{model, mode}` arguments. The account envelope records `tool: "openart_account_get"`, empty arguments, and the `agent_recorded_connector_observation_not_dispatch_authority` classification. The helper stores private, hash-addressed observations and updates an index for later sessions; explicit digest overrides take precedence over that index. Account results can contain personal information; keep the raw observation in that private store. These observations help discovery and request binding, but do not qualify a model, approve billing, or authorize a generation.

## Pick the scene treatment

Read the selected model/mode form and use its native control names, values, and input roles. Unsupported settings stop preparation instead of being dropped or described in prompt text.

For an ordinary boarded image-to-video shot, use the starting board as the native first frame. The ending board is the reviewed target state; it does not need to be sent as a native last-frame pin. When the user or approved contract explicitly requires an exact pinned ending, use the model's verified end-frame control and provide both first- and last-frame assets under the `first_last_frame` operation.

Reference-guided modes such as `element2video`, or SmartShot forms with declared identity-reference roles, describe a different treatment. Their native reference roles bind cast or environment identity while the model creates a scene; they are not literal first-frame pins. This change needs a fresh approved contract and governed request. Preserve the requested mode or ask before changing it.

Record that treatment as `reference_mode: "reference_guided"` on the shot contract or its project default. Use operation `reference_to_video` with `element2video`, or `shot_video` with SmartShot's `generate-shot-video` mode.

## Approval and cost

The normal route is Strict. A fresh approval binds the exact request, model/mode, native parameters, sources, project, and account. A displayed default cost is only an estimate. Exact-credit authorization requires verified account identity, billing unit, debit quantum, request parameters, balance, and debit evidence. Otherwise the approval must explicitly accept unknown cost with no enforceable credit ceiling. Attempt caps limit attempts; they do not cap provider charges.

A standing one-original-video run may instead use the existing policy's
`quoted_credit_budget` MCP variant. It admits the exact request only when an
actual connector `openart_model_cost` reply covers the native pricing controls,
the same retained account observation, one video, all declared cost controls,
an affordable credit amount and a quote within `max_credit_budget`. Unknown,
foreign, conflicting or excessive quotes block before scope admission. This
variant retains a creator budget without inventing acceptance of uncapped
generation. It is quote-based admission, not the CLI debit-guarantee contract
or a promise that OpenArt enforces a backend ceiling. Use the undiscounted
quote conservatively; an optional plan discount cannot increase its allowance.

The provider term in both the actual standing setup and run policy is:

```json
{
  "id": "openart_mcp",
  "billing": "quoted_credit_budget",
  "uid_sha256": "<actual-account-uid-sha256>",
  "project_id": "<actual-openart-project-id>",
  "routes": [{"model": "<exact-model>", "mode": "element2video"}],
  "max_credit_budget": 4000,
  "cost_observation_path": "artifacts/openart_mcp_cost_observation-whole-story.json"
}
```

Use one authored shot and `max_total_attempts: 1`; the shot may describe multiple
timestamped internal cuts. Retain the cost reply once after the source-bound
native request is ready, before completing its `prepare_motion` action. The
fixed in-project path may be declared before images exist. Save the actual
host observation as `version: "1"`, `transport: "openart_mcp"`, classification
`agent_recorded_connector_quote_not_backend_ceiling`, actual `observed_at`,
`tool: "openart_model_cost"`, actual `{model, mode, params}` arguments containing
only the native pricing controls (`duration`, `resolution`, `aspectRatio`,
`videoCount`, `generateAudio` when present), the native request's exact
`account_binding`, and the complete actual
`structuredContent` as `data`. Do not recast a catalog price as this reply.
Separately retain `native_request_binding: {model, mode, params}` with the full
prepared generation params, including prompt and references. This local binding
freezes the request being admitted; it does not claim the cost tool accepted or
priced the prompt and reference fields.
Scope derivation freezes the file SHA and normal `prepare`/`begin` rechecks it;
changing even its formatting after admission blocks fresh dispatch. Recover
the same original through its existing journal. Upload authority remains its
separate bounded source-transfer term and grants no generation allowance.

Auto-continue is a separate supported policy variant, not a readiness claim. It requires a fresh, active, user-approved Auto-continue policy with strict governance safeguards, the exact provider entry `openart_mcp`, either its explicit unknown-cost acknowledgement or the single-video quoted-budget term above, a bound account UID and OpenArt project ID, and exact schema-supported model/mode routes. The policy is revalidated for every attempt. Without a current valid policy, use Strict approval. A Grok CLI subscription entry is separate: its media model and remaining quota are unreported, and its approval cannot authorize OpenArt MCP requests or vice versa.

The provider-qualification pipeline is optional for bounded, user-approved tests and diagnostics. Its live-result evidence may inform quality decisions, but it is not ordinary production onboarding. Candidate status remains insufficient for production; supported exact native routes may proceed under ordinary request, account, authorization, and scope checks without paid result qualification. Paid benchmarks still require separate approval.

## Source uploads and generation handoff

An actual Seedance 2.5 `element2video` terminal rejection observed on 2026-10-14
reported that image dimensions must be 300–6000 pixels on each side (the supplied
image was 211×374). Future real native preparation measures those source bytes
before scope admission; a matching active single-route standing upload checks
before preserving an upload batch. An invalid source blocks without resizing,
replacement generation or a consumed motion attempt. Original recovery keeps its
frozen sources readable. A terminal `FAILED` or `CANCELLED` submission reply is
retained immediately as terminal evidence; it does not create a pending polling
step or authorize another submission.

The current `upload_import` connector contract makes no promise about upload spend or delayed charges. Treat upload exposure as unknown with no enforceable credit ceiling; do not claim a fee or delay will occur. At episode proposal, name the episode and shots, exact MCP route/account/project/model/mode scope, necessary board/reference source categories, and bounded source and upload-batch counts. Show any available request-applicable generation credit estimates as estimates; the actual charge remains unknown and has no enforceable ceiling. One explicit episode approval can cover this bounded plan, including source transfers and the named generation policy; do not add a routine second billing/upload prompt after that approval.

When reviewed source bytes exist, first verify that the later batch's shots, roles, and source categories, and the cumulative source-file and batch counts, fit the upfront approved scope. The wrapper does not parse or enforce those human-approved episode boundaries. Then materialize the existing exact per-batch source-transfer authorization in retained project artifacts. It must bind `provider: "openart_mcp"`, `status: "approved"`, `purpose: "source_transfer_only"`, current project/story revision, observed account UID hash, OpenArt project ID, ordered exact file paths and hashes, `no_enforceable_credit_ceiling: true`, `delayed_charges_unknown: true`, and `max_upload_batches: 1`, plus the named approver and current in-project approval evidence `path` and `sha256` linked to the same upfront episode approval. Pass only its opaque ID as `billing_declaration.upload_authorization_id`. The wrapper independently verifies each file against the current reviewed source contract. This exact later binding does not require another human prompt while the approved scope remains unchanged. If the route, account/project, model/mode scope, source categories/counts, or other approved boundary changes, obtain fresh approval. Caller-supplied claims or approval objects cannot grant authority.

One physical source file may have multiple contract IDs and roles, such as a payoff board also serving as a shot's end frame. For source transfer, every alias at that resolved in-project path must bind the current file hash and be a required, reviewed source of at least one currently eligible shot, including its current selected upstream evidence. Sharing a cast or file hash alone supplies no authority. The upload batch counts that file once. Reusing a retained upload still verifies current source bytes and asset reviews against its immutable receipt; governed generation preparation separately validates the target shot, exact native roles, current upstream eligibility and required frame pins. Historical original replay retains its existing frozen source proof.

Local upload and generation authority artifacts remain distinct, and an older generation policy does not authorize new source transfers. Once approved, call each upload envelope once, retain its original result, and bind the returned native reference into generation. Each generation request still passes its current exact request, account, model/mode, source, and attempt checks. Attempt caps limit calls, not provider spend.

Generation preparation returns one begin envelope for the installed connector. Call its exact tool with its exact arguments once, then retain the original structured result against that local attempt. Errors, missing history IDs, or lost receipts leave the attempt uncertain and must never trigger another generation. Polling and collection stay bound to that original history ID. If the connector's result card is self-polling, let it complete without launching a second poll. Text-only polling follows the connector cadence for the same ID.

After recording a terminal `COMPLETED` status for the original history, call account action `download` to fetch its exact retained resource URL into the private download area. Then pass that action's returned `downloaded_path` to `collect` for the same attempt; collection verifies the retained download receipt and copies only those bytes to the approved output path. Do not supply an arbitrary local video as if it were the provider result. The URL/history/resource/hash binding is locally retained agent evidence, not a cryptographic attestation from OpenArt.

## H3 Max Turbo schema example

The observed form uses model `fal-h3-max-turbo`, mode `image2video`, integer `duration`, and `resolution` values including `768P`. The snippet demonstrates request shape only; it is not evidence of live production qualification. Replace placeholders with retained source evidence and actual connector upload IDs. Set `videoCount` to `1` for a one-output shot so the authorized attempt and result count remain aligned.

```json
{
  "operation": "image_to_video",
  "model": "fal-h3-max-turbo",
  "mode": "image2video",
  "native_params": {"duration": 8, "resolution": "768P", "videoCount": 1},
  "input_assets": [
    {"role": "first_frame", "source_path": "assets/shot-start.png", "source_sha256": "<retained-sha256>", "upload_id": "<actual-upload-id>"}
  ]
}
```

If an exact ending pin is required, add a native `last_frame` asset and use `operation: "first_last_frame"`; this form requires both first and last frame assets:

```json
{
  "operation": "first_last_frame",
  "model": "fal-h3-max-turbo",
  "mode": "image2video",
  "native_params": {"duration": 8, "resolution": "768P", "videoCount": 1},
  "input_assets": [
    {"role": "first_frame", "source_path": "assets/shot-start.png", "source_sha256": "<retained-sha256>", "upload_id": "<actual-start-upload-id>"},
    {"role": "last_frame", "source_path": "assets/shot-end.png", "source_sha256": "<retained-sha256>", "upload_id": "<actual-end-upload-id>"}
  ]
}
```

Keep the resulting clip review separate from transport success: native controls do not guarantee perfect one-shot quality.

The MCP route uses the agent-mediated connector and does not call the OpenArt CLI or reuse CLI credentials. Do not infer MCP controls or authority from CLI qualification.

## Audio capability

`model_catalog` reports a per-route `audio_capability` outside the immutable profile hash, so profiles and frozen requests are unchanged. It separates native output (`supported_toggle` from a form switch, `supported_default` from hosted-provider documentation, or `unknown`), the explicit switch, reference audio, connector observation (`not_tested`) and dialogue fidelity (`unverified`). `explicit_toggle.defaults` records the exact form default for each switch, or `null` when the form declares none. A required `native_audio` output only qualifies when the effective switch value is `true`. PixVerse V6 (`generateAudio` default `false`) therefore needs an explicit `true`. The H3 forms expose no audio field, and the engine never injects one. A form without a switch does not mean the route is silent. See `lib/video_route_evidence.py` for the exact routes and sources.
