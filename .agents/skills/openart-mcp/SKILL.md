---
name: openart-mcp
description: Generate video through the installed, signed-in OpenArt connector using native model and mode controls.
---

# OpenArt video through the connector

Use this skill when the user explicitly selects OpenArt's agent-mediated MCP route. It is separate from `openart_cli_video`; never switch between these providers silently. The connector installation and sign-in are handled by the user's environment.

## Choose and prepare a route

1. Refresh stale discovery and account observations through the signed-in connector, not HTTP or local credentials. Call `mcp__codex_apps__openart_openart_model_list({})`, call `mcp__codex_apps__openart_openart_model_form_get({model, mode})` for each listed video model/mode, and call `mcp__codex_apps__openart_openart_account_get({})`. Preserve each exact `structuredContent` result and its tool name, arguments, and observation time.
2. Retain the results with `lib.openart_mcp.retain_observation('schema', schema_observation)` and `retain_observation('account', account_observation)`. Include `version: "1"`, `transport: "openart_mcp"`, and the actual `observed_at` in each envelope. The schema envelope uses `classification: "schema_observation_only_not_dispatch_authority"`, `catalog: {tool: "openart_model_list", data: <model_list structuredContent>}`, and `forms: [{tool: "openart_model_form_get", arguments: {model, mode}, data: <form structuredContent>}, ...]`. The account envelope uses `classification: "agent_recorded_connector_observation_not_dispatch_authority"`, `tool: "openart_account_get"`, `arguments: {}`, and `data: <account_get structuredContent>`. The helper hashes and stores these observations in the private discovery directory and updates the later-session index; explicit digest overrides take precedence over that index. Never paste the private account response in chat or a public artifact.
3. Then inspect `openart_mcp_video.get_info()` and read the exact form/profile with `openart_mcp_account` actions `catalog`, `form`, and `profile`. The observed catalog contains 45 model/mode forms. A route is production-ready when the current pinned catalog row, valid exact native form/schema, current authenticated account identity, and known mode/operation are present; the exact production request must still pass native role/parameter/schema validation. A prior generated result is optional diagnostic evidence, never a production prerequisite. Account, form, and discovery evidence alone never authorizes dispatch.
4. Keep `candidate`, `supported`, and optional `qualified` evidence distinct. `candidate` means raw observation and is not production-ready. `supported` means native schema and current account readiness; it is sufficient for production eligibility. `qualified` adds optional original-result evidence and grants no extra authority. Never describe the 45 observations as 45 live-tested models.
5. Build controls from the exact form. Put its declared non-media controls in `native_params`, preserving structured settings such as Kling's `multiPrompt` when the selected branch allows them; attach source media as `input_assets` with native roles. Use the provider's real upload receipt/reference. Never fabricate a URL or reference object, discard an unsupported control, or substitute prompt text for one.
6. For a normal image-to-video shot built from a board, bind the starting board as `first_frame`. Treat the ending board as the reviewed target state for the shot. It does not require a native `last_frame` pin. Add both native `first_frame` and `last_frame` assets with operation `first_last_frame` only when the user or approved contract explicitly requires an exact pinned ending; the selected `image2video` form must expose its ending-frame control.
7. Reference-guided generation is a distinct scene treatment. Record `reference_mode: "reference_guided"` on the shot contract or its project default. Use operation `reference_to_video` with mode `element2video`, or operation `shot_video` with SmartShot's `generate-shot-video` mode. Bind cast or environment identity references through those exact native roles. They establish visual identity in a newly generated scene; they are not literal first-frame pins. Switching a boarded shot to this treatment requires a fresh approved contract and a new governed request. Do not downgrade a reference-guided request to image-to-video without approval.

## Cost, authority, and source uploads

Show the exact model/mode, native settings, account observation, and billing status before dispatch. A catalog's default cost is an estimate, never a request-specific quote. Exact-credit authorization requires fully verified account, billing unit, debit quantum, parameters, balance, and debit evidence. Otherwise Strict requires a fresh approval explicitly accepting unknown cost with no enforceable credit ceiling. Attempt caps limit the number of attempts, not the charge per attempt.

Auto-continue uses a fresh, active, user-approved Auto-continue policy for `openart_mcp`. That policy binds the observed account UID, OpenArt project ID, and exact schema-supported model/mode routes, and explicitly acknowledges unknown cost with no enforceable ceiling. Revalidate it for every attempt. An absent, stale, revoked, malformed, or mismatched policy means Strict. A policy may explicitly include both Grok CLI and OpenArt routes; approval for one route alone does not authorize the other.

The current `upload_import` connector contract makes no non-spend or no-delayed-charge promise. Treat uploads as unknown-cost, with delayed charges unknown and no enforceable credit ceiling. Before upload, the project must contain a retained source-transfer authorization; pass only its opaque ID as `billing_declaration.upload_authorization_id`. It must bind `provider: "openart_mcp"`, `status: "approved"`, `purpose: "source_transfer_only"`, current project/story revision, account UID hash, OpenArt project ID, ordered exact file paths and hashes, `no_enforceable_credit_ceiling: true`, `delayed_charges_unknown: true`, and `max_upload_batches: 1`, plus a named approver and current in-project evidence path/hash. The wrapper checks that artifact and verifies each source against the reviewed project contract. Caller-provided claims do not grant upload authority.

A single fresh human approval may cover exact source batches and a named generation policy, so do not reprompt for each clip within that scope. Keep source-transfer and generation authority distinct: old generation policies never authorize a new upload. Call the prepared upload envelope once, retain its original result, and bind the actual returned reference into the governed generation request, which still needs its own exact account, source, model/mode and request checks.

## Submit once and retain the original result

Prepare the governed request and obtain its single-use `begin` envelope. Call exactly the connector tool named in that envelope with exactly its arguments, once. Immediately pass the original structured result to `receive` for the same local attempt ID. If the call errors, returns no history ID, or the original receipt cannot be retained, mark the attempt uncertain; never repeat generation. Poll and record status only against that same attempt and provider history ID.

When the connector shows its result card and is polling by itself, retain the initial response and let that interaction finish. Do not start a second poll or wait call while the card is mounted. In text-only use, request the next poll arguments for the same recorded history ID and follow the connector's cadence. Record each original status result first. After recording terminal `COMPLETED`, call account action `download` with that same project and attempt; it fetches only the exact retained original resource URL and returns its local `downloaded_path` plus the URL/history/resource/hash receipt. Pass that path to `collect` for the same attempt. Do not present an arbitrary local MP4 as provider output. This retained download binding is agent-recorded evidence, not cryptographic provider attestation.

## Example: H3 Max Turbo, 768P

This schema-observed example shows native request fields; it does not claim that H3 Max Turbo has live production qualification. Replace each path, hash, and upload ID with retained project evidence and actual connector receipts. Set `videoCount` to `1` to request one output.

For a normal boarded shot, use the starting frame and leave the ending frame as a review target:

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

Only for an explicitly pinned ending, include the native ending asset and select the first/last-frame operation. This exact form requires both frames:

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

## Review

Collect only the output attached to the original completed history, using the `download` action followed by `collect` as described above. Preserve its file hash and attempt provenance, then review the result against the approved shot and continuity requirements. Native controls constrain the request; they do not guarantee perfect one-shot quality. Report empirical result status accurately; schema-supported production readiness does not mean the route has been live-tested or quality-reviewed.

The MCP connector route does not call the OpenArt CLI or use its credentials. The CLI has its own account evidence and approvals; its missing frame, reference, or audio controls are not inferred from the MCP forms.
