# OpenArt Prompting

Use this when the chosen provider is `openart_cli` (tool `openart_cli_video`).
Pair it with the `openart-cli` skill and `docs/OPENART_CLI.md`.

## Scope comes from the menu

The explicit `reference_free` request mode is a narrow option for OpenArt
text-to-video when there are no cast members, dialogue, source assets or pinned
upstream assets. It does not waive production contracts on other routes or
legacy board requirements.

The only creative controls are those the qualified profile lists in
`provider_menu_summary()["qualified_cli_video_routes"]`. The current v0.1.1
surface exposes:

- the prompt
- an exact qualified model
- duration
- aspect ratio
- resolution, only when an observed profile form and exact preview qualify it
  (the menu reports `native_controls: not_yet_qualified` until then)
- one reference image (only through the retained-upload path)

It has no end-frame pin, native audio or voice control, multiple references,
negative prompt or prompt expansion. Do not claim those controls in wording,
such as "ends exactly on frame X" or "match these three references".

Approved dialogue is different. A missing native-audio control does not erase
approved dialogue from the scene or the prompt. Keep the approved line verbatim
so the shot contract's dialogue coverage still holds, and tell the user plainly
that the route gives no native-audio guarantee. When the approved shot requires
a native audio control, the pinned request fails with `missing_controls` before
any credit reservation. In that case choose a route that has the control or
change the shot.

## Best prompt shape

```
[subject] + [single clear action] + [setting] + [camera] + [light / style anchor]
```

- Keep one action per shot and state it concretely, for example: "the courier
  places the parcel into the waiting hands".
- Name the camera behavior once ("slow push-in", "static medium shot").
- Keep continuity cues that the reviewer will check (wardrobe, prop, possession
  of the object, screen direction) explicit and consistent with the upstream
  shot.
- For a reference image, describe the motion and change. Do not re-describe
  everything the image already shows.

## Asking the user

Offer creative choices: the shot description, mood, duration, framing and an
optional reference image. Do not ask the user for model IDs they have not seen
in the menu, or for CLI commands. Present the selected billing mode plainly.
Exact-quote mode retains its exact quote and ceiling requirements. The separate
unknown-cost mode explicitly acknowledges that there is no enforceable credit
ceiling; never describe the unknown charge as zero, affordable or a USD
estimate. Keep OpenArt credits separate from Grok's unknown subscription quota.

## Approval is exact

Once the user approves a prompt and settings, they are sent unchanged. Any
wording or setting change is a new request that needs new applicable billing
authority and approval, unless an active user-approved Auto-continue policy
explicitly authorizes that deterministic flex. Exact-quote mode needs a new
quote; unknown-cost mode needs its applicable retained authorization. Compile
the prompt from retained/rooted
planning and obtain a fresh named preparation review. Preserve dialogue wording,
speaker/source/order, identity, story facts and native frame pins. A repair needs
actual failed-review evidence and exact replacement attempt IDs; uncertain jobs
never authorize a retry.
