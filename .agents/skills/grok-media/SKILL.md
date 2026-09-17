---
name: grok-media
description: xAI Grok media guide covering the REST API and the separate, explicit-only local Grok CLI route.
metadata:
  author: OpenMontage
  version: "1.1.0"
  tags: xai, grok, image-generation, video-generation, media
---

# Grok Media

Use this skill when working with xAI media models in OpenMontage. First identify
the selected tool's provider contract. The two routes are not interchangeable:

- `provider="grok"` uses the xAI REST API and `XAI_API_KEY`.
- `provider="grok_cli"` uses a locally installed, already signed-in Grok CLI
  OAuth/subscription session. It does not use `XAI_API_KEY`.

Never substitute one route for the other. The CLI providers are explicit-only:
the selector request must set `preferred_provider="grok_cli"` and make
`allowed_providers` exactly `["grok_cli"]`.

## Local Grok CLI contract

OpenMontage uses `grok` from PATH (or an explicit `GROK_CLI_PATH`) with model
`grok-4.6`. It requires CLI `1.0.18` or newer and verifies required command
options and values with `--help` before dispatch. Compatible updates need no
separate binary or version-pin change. These checks establish advertised CLI
compatibility, not media entitlement. Results record the observed CLI version. `agent_model` (and legacy `model`)
identifies the coding agent; `media_model=null` / `media_model_status=unreported`
means the native receipt did not report the Imagine backend. Do not describe
`grok-4.6` as the video model or infer a server-resolved model from it.
The adapter runs
one sealed native media-tool call through streaming JSON, disables web search
and subagents, denies shell/project-file/MCP access, validates the returned
session artifact with `ffprobe`, and never retries or falls back. Trailing
newline drift on sealed prompt strings is treated as equivalent; other argument
mutations still reject.

Grok CLI 1.0.18 classifies all four native media operations, including
text-to-image, under read permissions. The adapter therefore omits the generic
`Read(*)` denial for these four operations only. Filesystem Read is still not
exposed: `--tools` allows exactly the selected media operation, with shell,
write, edit, search, web, MCP, and subagents excluded. Do not use
`--always-approve` or broaden the tool allowlist to fix media permissions.

Supported native operations:

- `image_gen`: prompt plus aspect ratio.
- `image_edit`: prompt plus 1-5 local image paths.
- `image_to_video`: one local first-frame image, 6 or 10 seconds, 480p/720p.
  The source conditions frame one, not guaranteed geometry throughout motion. There is no `aspect_ratio`
  argument. Near-9:16 drifts such as `720x1264` are common and must be
  normalized at compose/stitch to exact TikTok-safe `720x1280` (or
  `1080x1920`) before shipping.
- `reference_to_video`: 1-7 local reference images and/or 1-3 preset `voices`,
  1-15 seconds, 480p/720p, and an explicit aspect ratio. References guide subjects
  and style; they do not pin frame one. Requested aspect ratio is not a guarantee
  of exact output pixels: inspect and normalize delivery geometry before shipping.
  Preset voices were qualified from native tool descriptions in installed stable
  CLI 1.0.25 (f7e67d6988e2), with offline dispatch tests; no live voice generation
  was performed for qualification. The adapter requires 1.0.25 or newer.
  Pass preset identifiers in `voices`
  and tag them as `<AUDIO_0>`, `<AUDIO_1>`, etc. in the prompt; tag image references
  as `<IMAGE_0>`, etc. No uploaded audio or voice cloning is exposed. Voice-only
  reference generation is supported; it is not a pinned-first-frame operation.
  Presets do not guarantee speaker attribution or lip sync; review both.

The CLI adapter does not expose direct text-to-video, video edit, extend, or
upscale. Do not emulate those operations with a hidden multi-step workflow.
For TikTok delivery after `image_to_video`, package through `video_stitch` /
`video_compose` with `profile="tiktok_720p"` (or `compose_target`
`720x1280`) — never raw ffmpeg concat `-c copy` of off-geometry clips.

CLI media pricing cannot be pre-estimated from the coding-agent transcript.
Treat it as unknown subscription media cost and require the user's explicit
approval before setting `allow_unknown_cost=true`. A positive terminal
`total_cost_usd` is coding-agent cost only, not the Imagine media charge.

Authentication is human-managed outside production runs. If the adapter
reports `auth`, ask the user to sign in with `grok login`; never initiate login,
open a browser, or fall back to REST. Treat `spending_limit`, `tier`,
`zdr_storage`, `headless`, `capability`, `version`, `protocol`, `artifact`, and
timeout failures as terminal for that dispatch.

For CLI prompts, request one shot and one main motion idea. Map each reference
image to a specific role in plain language. Keep the full prompt at or below
4096 characters and preserve continuity constraints from the scene plan.

The remaining sections describe the REST API route only.

## Pinned video endpoints (REST Video 1.5)

REST video supports pinned endpoints on `model="grok-imagine-video-1.5"`.
Use `operation="first_last_frame"`, `last_image_url` or `last_image_path`, and
optionally `image_url`/`image_path` (selector aliases: `reference_image_url` or
`reference_image_path`). The adapter sends REST `last_frame` and `image`.
Use the same image for both endpoints to author a loop. There is no native
`loop` switch; review motion and audio at the seam. Duration: 1–15 seconds;
frame pairs/reference guidance: 480p or 720p. Prompts and first frames are
optional in last-frame requests. Reference images can accompany the frame pins.

Classic `grok-imagine-video` cannot accept a last frame. Never silently change
the model, omit the last frame, or replace endpoint control with a prompt.
The CLI contract exposes neither ending-frame pins nor Imagine model selection.
CLI coding model `grok-4.6` is not an Imagine media model identifier.

See the [official first/last-frame guide](https://docs.x.ai/developers/model-capabilities/video/reference-to-video).

## REST API models

- `grok-imagine-image` for image generation and image editing
- `grok-imagine-video` for classic text-to-video, image-to-video, and reference-image video
- `grok-imagine-video-1.5` for video including pinned first/last frames

## REST API authentication

- Env var: `XAI_API_KEY`
- Base URL: `https://api.x.ai/v1`
- Header: `Authorization: Bearer $XAI_API_KEY`

## Image API

### Text-to-image

- Endpoint: `POST /images/generations`
- Core fields:
  - `model`
  - `prompt`
  - `n`
  - `aspect_ratio`
  - `resolution`

### Image edit

- Endpoint: `POST /images/edits`
- Use `image` for one source image
- Use `images` for multi-image compositing
- Each source image can be:
  - a public HTTPS URL
  - a base64 data URI

### Image prompting

- Grok responds well to direct natural language
- For edits, describe only the intended change and preserve everything else implicitly
- For multi-image merges, explicitly name how each source contributes
- Prefer one strong scene description over long style-stacking

## Video API

### Generation

- Endpoint: `POST /videos/generations`
- Polling endpoint: `GET /videos/{request_id}`
- Success state: `status == "done"`
- Failure states to handle explicitly: `failed`, `expired`

### Modes

- Text-to-video:
  - prompt-only generation
- Image-to-video:
  - use `image: {"url": ...}`
  - this anchors the starting frame
- Reference-to-video:
  - use `reference_images: [{"url": ...}, ...]`
  - this influences who/what appears in the video without locking the first frame
  - prompts can reference inputs with placeholders like `<IMAGE_1>`, `<IMAGE_2>`

### Video constraints

- Grok video is best treated as short-form generation
- Classic/frame-pair/reference output is `480p` or `720p`; 1.5 text/image-to-video also supports `1080p`
- Reference-image video supports multiple images and is useful for product placement, wardrobe transfer, and identity consistency
- Download outputs promptly; provider URLs may be temporary

## Pricing

- `grok-imagine-image`: `$0.02` per generated image
- `grok-imagine-image` edits/composites: add `$0.002` per input image
- `grok-imagine-video`:
  - `480p`: `$0.05` per second
  - `720p`: `$0.07` per second
- `grok-imagine-video` image-conditioned requests: add `$0.002` per input image

- `grok-imagine-video-1.5`: $0.08/s at 480p, $0.14/s at 720p, $0.25/s at
  1080p, plus $0.01 per input image (including each endpoint slot).
  These are REST estimates, not CLI media charges. Verify
  [current pricing](https://docs.x.ai/developers/pricing) before production.

## Grok-Specific Prompt Guidance

### Images

- Start with subject, action, setting
- Add one style anchor, not five
- For edits:
  - describe the desired modification
  - keep the rest of the image stable by omission, not by writing a giant preservation list

### Video

- Keep prompts scene-local: one shot, one main motion idea, one emotional beat
- For reference-conditioned video, explicitly map source images to roles:
  - person from `<IMAGE_1>`
  - jacket from `<IMAGE_2>`
  - product from `<IMAGE_3>`
- Camera and pacing language helps:
  - slow push-in
  - handheld follow
  - locked-off medium shot
  - high-energy whip pan transition

## Good Fits

- Image style transfer
- Image compositing from multiple sources
- Reference-conditioned short video
- Product-led motion clips
- Character-consistent scenes without hard first-frame lock

## Weak Fits

- Long-form clip generation
- Heavy reliance on deterministic seeds
- Overloaded prompts with multiple scene changes

## Failure Handling

- If generation submission succeeds but polling expires, surface it as a provider/runtime issue
- If a request fails, preserve the endpoint, mode, and prompt summary in the error
- Do not silently substitute a different provider after xAI was selected without user approval

## Production endpoint handoff

For an approved pinned ending, use the machine-readable sidecars and artifact
bridge in `skills/creative/visual-development.md` → Required Pinned Final
Frames. Keep `endpoint_requirement_id` on the selector request and retain
returned `endpoint_conditioning` in the output asset's prompt audit. Preflight
must distinguish the existing CLI OAuth session from REST API credential
availability and API charges. A working CLI session does not enable REST or
prove final-frame support. Endpoint pins constrain boundaries, not intervening
physics; inspect the generated ending and motion before accepting the asset.
