---
name: minimax-h3
description: |
  Generate MiniMax H3 family video through OpenArt MCP, the official MiniMax v2 API, fal.ai, Runway, ComfyUI Partner Nodes, or local open weights in ComfyUI. Match prompting and reference preparation to the exact variant and route; their controls differ.
---

# MiniMax H3

MiniMax H3 is the Hailuo 3.0 family. The first-party API identifier is
`MiniMax-H3`; fal.ai exposes the family as `hailuo-03`, and Runway uses
`hailuo3`. Do not substitute one provider's identifier into another API.

## Choose a route

| Route | Tool call | Execution |
|-------|-----------|-----------|
| OpenArt MCP, H3 Max Turbo | `openart_mcp_video`, `model: "fal-h3-max-turbo"` | Hosted OpenArt `text2video` or `image2video`; see the exact envelope below |
| MiniMax direct | `minimax_video`, `model: "MiniMax-H3"` | Hosted first-party v2 API; global or mainland-China region |
| fal.ai | `minimax_fal_video` | Hosted gateway; T2V, I2V, reference-to-video |
| Runway | `runway_video`, `model: "hailuo3"` | Hosted; 768P or 2K, 5–15 seconds |
| ComfyUI Partner Node | `comfyui_video`, `model_family: "minimax_h3_api"` | Hosted and billed in Comfy credits |
| ComfyUI open weights | `comfyui_video`, `model_family: "minimax_h3_local"` | Local GPU with official workflow and model stack |

For local ComfyUI, export the official workflow in API format and pass
`workflow_json` or `workflow_path` plus `output_node`. OpenMontage reports the
required diffusion model, Qwen3-VL text encoder, video VAE, and audio VAE; it
does not silently download large weights.

## Operations and prompting for the wider H3 family

These modes describe the wider family, not a promise that every route or variant
accepts them. In particular, OpenArt H3 Max Turbo has no full-reference mode.

- Text-to-video: concrete ratio; do not use `adaptive` without visual input.
- Image-to-video: provide a first frame.
- First/last-frame: provide both images and describe the motion between them.
- Reference-to-video: images, videos, and audio can be combined. Audio needs at
  least one visual reference.

Write prompts as subject + action + camera path + environment + lighting +
audio intent. MiniMax responds well to explicit camera direction. Keep the
requested motion achievable within 4–15 seconds and inspect native audio as
carefully as the image track.

## Provider differences

The direct MiniMax v2 route currently outputs 2K and supports 4–15 seconds.
Runway's Hailuo 3.0 route supports 768P/2K and documents 5–15 seconds. Partner
Nodes require network access and credits. Only the open-weight ComfyUI route is
local/offline after all models are installed.

## OpenArt H3 Max Turbo: exact route and frame roles

Read [OpenArt MCP](../openart-mcp/SKILL.md) for account, native form, image
objects, execution and pricing discovery. Select the exact model from live
`model_list` and inspect its form; do not substitute direct fal field names.
The OpenArt form checked on **2026-10-15 (America/New_York)** exposes:

| Control | OpenArt `fal-h3-max-turbo` |
| --- | --- |
| Modes | `text2video`, `image2video` |
| I2V required inputs | `prompt`, one `startFrame` image object |
| I2V optional endpoint | One `endFrame` image object or null |
| Duration | Integer 5–15 seconds |
| Resolution | `480P`, `768P`, `1080P` |
| Output count | `videoCount` 1–8; capacity is not a batch instruction |
| Prompt expansion | `promptExpansionMode: "balanced"` only, also the default |
| Separate reference arrays | No `visualReferences` or independent identity/prop/style inputs |
| Other I2V fields | No `aspectRatio` or audio-input field; additional properties are rejected |

The absence of an audio-input field does not mean the output is silent and does
not change the current audio plan.

A `startFrame` is the literal opening composition. An `endFrame` is the literal
ending composition, not a spare identity, style or prop reference. Naming hidden
source sheets in the video prompt does not send them to the motion backend.
Preserve their influence by building the actual frame from them first. Keep the
source-to-image provenance distinct from the images actually attached to video.

Direct fal's [Turbo I2V schema](https://fal.ai/models/minimax/h3-max-turbo/image-to-video/api)
is a different surface: it exposes `image_url`, `end_image_url`, optional
`target_audio_url` and more prompt-expansion choices, including end-only requests.
Those capabilities do not establish equivalent OpenArt controls. It describes
1080P as latent refinement of a native 768P source; a larger output is not evidence
of more reliable action or contact. Do not infer 2K, multimodal references or local
weights for Max Turbo from the base H3 documentation.

## Prepare images for the action, then animate

Use the shared [selected-route preparation method](../../../skills/creative/visual-development.md#prepare-references-for-the-selected-video-route)
for reference roles, casting, composition and motion handoff. Apply that method
to this two-frame envelope:

1. Choose the useful character, prop and look inputs for **image generation**.
   A distinct second character or important object can need its own source;
   one useful image per role is a sound starting point. Omit redundant views and
   unrelated backgrounds. Available input capacity is never a quota to fill.
2. Generate one coherent opening scene from those inputs. Identify which source
   supplies each character, object or look and which parts to ignore. Stage the
   intended action before its first contact: readable participants, correct
   relative scale, a clearly placed grip/contact surface, clear object ownership,
   reachable target and room for movement. When rigid body construction matters,
   use articulated motion under the shared method. Show the actual opening, not a collage of source
   sheets or a premature completed payoff.
3. Add an end image only when its exact landing composition materially helps the
   shot. Prefer editing the start scene, preserving cast, costume, set geometry,
   scale, camera and light; change the necessary pose, contact and object state.
   Independent endpoint illustrations can introduce a contradictory scene.
   A natural reaction or open ending often needs only the start image and prompt.
4. Write the motion between the real states: the dominant action, material
   behavior only when important, the visible result and a brief purposeful
   decision or realization. Keep dependent movements few; omit incidental taps,
   regrips and camera shifts. Let the frame carry appearance and staging; add
   camera direction only when it helps clarity.

Plan the intended finished shot and its selected resolution from the outset;
Turbo speed does not call for a low-resolution trial followed by a keeper rerun.
This is preparation within the current authorized plan, not a new approval or
reroll stage. A well-staged first image improves the starting conditions; it does
not guarantee identity retention, physical contact or endpoint accuracy. Keep
route, exposure and once-only attempt rules with their existing owners.

## H3 motion wording and worked examples

MiniMax's official [base prompt guide](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_base_en.md)
distinguishes motion developing from an opening image from a continuous path
between two endpoints. Its examples include a passenger handling a letter and a
cyclist opening an umbrella between frames. The latter illustrates an explicit
physical path, rather than two disconnected still descriptions. It recommends a
single continuous shot for first/last-frame work and natural camera verbs, with
speed and travel extent when those details matter. These are family-level writing
guidelines, not verified OpenArt Turbo results or extra native fields.

The official [animation shot spec](https://github.com/MiniMax-AI/MiniMax-H3/blob/main/skills/3d-animation-short-generator/references/shot-table-spec.md)
also separates action beats from camera direction. For a quick gag, use a few
concise action windows under the shared preparation method, rather
than asking the model to be “4x faster.” OpenArt exposes no native playback-speed,
FPS or shutter control here; a sharp-shutter look is an aesthetic request, not a
timing fix. The early target below is an authoring heuristic, not measured Turbo
reliability or frame-accurate guarantees.

The separate [full-reference guide](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_ref_en.md)
distinguishes reusable subject information from concrete frame anchors. Its
multi-source labeling format belongs to reference-capable routes; do not treat
those labels as an OpenArt Turbo reference-array feature.

**Illustrative five-second gag — not generated or independently tested:**
Prepare a medium-wide clay kitchen opening: a small robot leans over a toaster,
buttered toast already rising from its slot toward the robot's face along a clear
path, one palm against the toaster's side and clear counter space beyond it.
Use relevant robot, toaster and look sources to build that coherent scene;
only the finished scene becomes `startFrame`. Resolve the face-to-slot geometry
in this image rather than adding a catch or repositioning to the video prompt.

Illustrative video prompt:

```text
One continuous static medium-wide shot in the clay kitchen shown in the opening
image. Brisk real-time physical comedy, clear robot silhouette and toast edges.
The already-rising buttered toast hits the leaning robot's face within the first
second. The butter makes the toast stick over its eyes;
the robot pushes the toaster away with that palm, then releases it as it stops
on the clear counter, a firm refusal. End as that decision lands.
Spring clack and sticky slap;
no background music.
```

Use the brief's actual duration and audio intent; this example preserves a
no-background-music choice. Choose enough meaningful action for the route's
minimum duration, then end when the payoff lands; do not stretch a reaction or
fill time with incidental gestures. A reaction needs no compulsory `endFrame`.
When an exact endpoint matters, derive it from the same scene under the method
above. Preparation adds no reroll or approval stage.

## Quality evidence and source limits

The [fal H3 Max showcase](https://fal.ai/minimax-h3-max) includes published
prompt/clip examples and a balloon first/last-frame ascent. fal identifies those
showcase clips as H3 Max, so they are useful worked references for staging and
motion, not independent tests of OpenArt Max Turbo. fal markets its post-training
as improving adherence/aesthetics and reports fast Turbo inference; its own
benchmark measures backend denoising time, separate from queueing, rewriting and
encoding. Speed is not a quality verdict, and provider rankings are not universal
superiority guarantees. Use the selected route's actual output as evidence.

[OpenArt's release page](https://openart.ai/suite/whats-new) separately lists H3,
H3 Max and H3 Max Turbo. Do not inherit a sibling's controls, temporary promotion
or direct fal price. Resolve the current OpenArt quote for the exact mode,
duration, resolution and count through the existing MCP owner before dispatch.
Keep the dated cross-route cost comparison in the shared
[selected-route preparation owner](../../../skills/creative/visual-development.md#prepare-references-for-the-selected-video-route),
rather than treating this skill as a price catalog.
