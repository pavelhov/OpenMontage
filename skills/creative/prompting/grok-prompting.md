# Grok Prompting

Use this when the chosen provider is `grok_image` or `grok_video`.

## When Grok Is The Right Pick

- You need to edit an existing image instead of generating from scratch
- You need to merge multiple source images into one output
- You need a short video influenced by reference images without locking the first frame
- You want one provider for both image and video generation with similar prompt language

## Grok Image

### Best Prompt Shape

```
[subject] + [action or change] + [setting] + [one style anchor] + [lighting]
```

### Edit Prompts

For image edits, describe the intended transformation directly:

- "Render this as a pencil sketch with detailed shading."
- "Replace the plain t-shirt with a dark green bomber jacket."
- "Combine these two people into the same sunny park scene."

Do not over-specify every unchanged detail unless preservation is critical.

### Multi-Image Composites

Tell Grok how to combine the inputs:

- who comes from which source
- what should stay separate
- where the final scene takes place

Example:

```
Place the person from image 1 and the person from image 2 on the same subway platform at dusk,
standing shoulder to shoulder, cinematic sodium-vapor lighting, realistic photography.
```

## Grok Video

### Best Prompt Shape

```
[shot] + [camera movement] + [subject] + [main motion beat] + [environment] + [lighting] + [tone]
```

### Reference-Image Video

Grok supports prompts that refer to source images with placeholders like `<IMAGE_1>`.
Use that when you need identity, wardrobe, or product consistency.

Example:

```
Medium full shot, slow push-in. The model from <IMAGE_1> walks onto a clean white runway wearing
the jacket from <IMAGE_2>. Soft studio lighting, premium fashion campaign, confident expression.
```

### Image-to-Video vs Reference-to-Video

- Use image-to-video when the source image should act like the opening frame.
- Use reference-to-video when the source images should influence the content but not freeze the composition.

### Defaults and model choice

Prefer the current Imagine models by default:

- REST video: `grok-imagine-video-1.5`
- REST stills: `grok-imagine-image-2.0`
- CLI video (>=1.0.34): native `image_to_video` / `reference_to_video` (Video 1.5-class backend; no model override)
- CLI stills: native `image_gen` / `image_edit` (no Image 2.0 override)

Do not steer users toward classic `grok-imagine-video` or `grok-imagine-image` unless they explicitly ask for the older cheaper route.

### Pinned endpoints and loops

REST: use `grok_video` (defaults to `grok-imagine-video-1.5`) with
`operation="first_last_frame"`. Provide `last_image_path`/`last_image_url` and
optionally a first image using `image_path`/`image_url`.

CLI (>=1.0.34): explicitly select `grok_cli_video` with
`operation="first_last_frame"` and local `last_image_path` (optional local first
via `image_path`/`reference_image_path`). Native `reference_to_video` also accepts
`first_frame`/`last_frame`/`keyframes`. URLs and a native `loop` switch are not
supported on the CLI route.

The same image at both ends is useful for a loop. Describe a cyclic action, a
stable camera, and the return to the initial pose; inspect velocity and sound
across repeated playback. Keep frame pairs at 480p/720p.

Prepare stills with Image 2.0 on REST (`grok-imagine-image-2.0`). CLI `image_gen`
does not expose an Image 2.0 model override. Read `grok-media` for the verified
route and pricing.

## Common Mistakes

- Treating Grok reference images like strict storyboards. They are influence inputs, not exact frame locks.
- Writing multiple scene changes into one clip request.
- Combining too many style labels with too little scene information.
- Using vague edit prompts like "make it better" instead of naming the change.

## OpenMontage Guidance

- For image edits or compositing, prefer `grok_image` over the selector's default workhorse tools.
- For reference-conditioned video, prefer `grok_video` when the brief depends on carrying people, clothing, or products from input images into motion.
- If the deliverable is pure cinematic motion without reference constraints, compare Grok against Runway, Veo, and Kling before locking the provider.
