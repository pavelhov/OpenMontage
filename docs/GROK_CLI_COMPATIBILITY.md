# Grok CLI Compatibility Audit

**Audit date:** 2026-09-02
**Audited binary:** `/Users/pavel/.grok/bin/grok`
**Installed version:** `1.0.13` stable
**Route:** cached Grok OAuth/subscription session, separate from `XAI_API_KEY`

## Decision

The installed Grok CLI has a workable, automatable Imagine tool surface for a
**version-pinned, explicit-only provider adapter**. It must remain separate from
the existing xAI REST providers and must never be selected as an automatic or
silent fallback.

It is also absent from automatic ranking. When the caller exactly pins
`grok_cli` and requests `operation: rank`, selector preflight may show an
unscored explicit-pin row with its actual readiness and an unknown media-cost
status. That informational row does not make the CLI route automatically
selectable or invoke a media operation.

This supersedes the earlier blocker conclusion. Historical local sessions now
provide live success and failure evidence for the exact headless route. No new
paid media generation was run during this audit.

## Live evidence on this machine

Historical session `01a0552a-bd9b-7d32-9af1-7b24a6af352d` contains completed
tool calls and durable local artifacts for all four relevant primitives:

| Primitive | Observed inputs | Observed artifact |
|---|---|---|
| `image_gen` | prompt, aspect ratio | 1280x720 JPEG |
| `image_edit` | prompt, two local image references, aspect ratio | 1280x720 JPEG |
| `image_to_video` | local source image, motion prompt, 6 seconds, 480p | 6.04-second H.264/AAC MP4 |
| `reference_to_video` | up to seven local references in the observed run, 9:16, 15 seconds, 480p; another run proved four references at 720p | 15.04-second H.264/AAC MP4 |

Two additional headless runs used a native Grok model with only
`reference_to_video` allowlisted and returned absolute artifact paths under the
CLI session directory. Both outputs validate as 15.04-second, 720x1280 H.264/AAC
MP4s. The terminal tool result is machine-readable and includes `type`, `path`,
`filename`, and `session_folder`.

Historical failures provide exact negative contracts:

- HTTP 403 `personal-team-blocked:spending-limit` for exhausted credits or a
  missing subscription;
- HTTP 400 `invalid-argument` when a reference-to-video prompt exceeds the
  installed route's 4096-character limit.

These observations qualify this installed version and account. They are not a
promise that a future CLI version will preserve the same wire contract, so the
adapter must fail closed on an unqualified version.

## Capability boundary

| Capability | CLI status | OpenMontage treatment |
|---|---|---|
| Text to image | Proven | `image_gen` through a separate `grok_cli` image adapter |
| Image editing / compositing | Proven | `image_edit` through the same image adapter |
| Image to video | Proven | `image_to_video` through a separate `grok_cli` video adapter |
| Reference to video | Proven | `reference_to_video`; reference images and preset voices are supported by the observed/current tool surface |
| Direct text to video | Not exposed as a primitive | Do not advertise it. `/imagine-video` is an agent workflow that first makes an image and then animates it. |
| Video editing or extension | No installed CLI primitive found | Fail explicitly; use no substitute automatically. |
| 1080p or post-generation upscale | Not exposed by installed CLI tools | Fail explicitly. Installed video primitives expose 480p and 720p. |

The slash commands `/imagine` and `/imagine-video` are useful interactive
workflows, but they are not the provider contract. OpenMontage should call the
single allowlisted primitive through headless mode so the operation, inputs,
tool count, and artifact are auditable.

## Headless execution contract

The qualified route uses:

- native `grok-4.6`, not a CCX model;
- a prompt file containing exact JSON arguments and an instruction to call one
  named media tool exactly once;
- `streaming-json` output;
- a hard turn limit;
- no subagents or web search;
- a built-in tool allowlist containing only the requested media primitive;
- MCP/tool denial where supported;
- non-interactive permission mode and closed stdin;
- a hard process timeout with no automatic retry.

Success requires all of the following, not merely process exit code zero:

1. exactly one call to the requested media tool;
2. a completed terminal tool update with the expected media result type;
3. a terminal stream result;
4. an absolute artifact path contained under the configured Grok sessions root;
5. a non-empty, decodable image or playable video;
6. a verified copy at the caller's requested OpenMontage `output_path`.

The observed 480p image-to-video artifact was 736x400, not a canonical 854x480.
Validation therefore checks a positive, playable video stream and the requested
resolution bucket/aspect tolerance instead of hard-coding one pixel size.

## Auth, spend, and failure behavior

The CLI route can use the user's cached Grok OAuth/subscription session and does
not require `XAI_API_KEY`. Installation or login alone does not prove media
entitlement, remaining weekly allowance, Extra Usage Credits, or ZDR readiness.

The CLI has no enforceable per-media-call dollar cap and OAuth/pool responses may
omit complete cost data. Missing cost data means **unknown**, never free. Published
Imagine API prices may be shown as context, but they must not be presented as the
actual CLI subscription charge.

The adapter must classify and stop on:

- missing binary or unsupported CLI version;
- missing/expired auth;
- subscription, tier, credit, or spending-limit failure;
- ZDR/output-storage restrictions;
- missing tool capability or headless limitation;
- invalid arguments or prompt length;
- timeout, malformed stream, wrong/multiple tool calls, or missing artifact;
- artifact validation/copy failure.

Every failure is terminal for the selected `grok_cli` route. There is no browser
automation, provider hopping, REST retry, or silent fallback.

## xAI REST tools remain separate

`tools/graphics/grok_image.py` and `tools/video/grok_video.py` are the existing
xAI REST integrations. They use `XAI_API_KEY`, provider `grok`, and direct API
request/poll/download contracts. The CLI adapters use provider `grok_cli` and
must not replace or mutate the REST tools.

## Verification ladder

1. Offline fake-executable tests pin argv, stdin, version, NDJSON parsing,
   failures, artifact containment, copy, and media validation.
2. Selector tests prove `grok_cli` is excluded from automatic ranking,
   estimates, and fallbacks. An exact, exclusive rank preflight may report an
   unscored informational row; it is admitted for generation only by an
   explicit, exclusive provider request.
3. Registry/contract tests prove REST and CLI identities remain distinct.
4. After the adapter is complete, a new live canary may be run only with explicit
   user approval. It should use one shortest qualified operation, stop after one
   tool call, and report cost as exact only when the terminal stream says it is
   complete.
