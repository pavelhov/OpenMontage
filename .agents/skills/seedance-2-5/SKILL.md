---
name: seedance-2-5
description: |
  Prepare ByteDance Seedance 2.5 video through the exact registered provider route, including OpenArt reference mode. Covers labelled character/prop/setting/staging references, timed shot text, continuity, native input distinctions and evidence-qualified prompt recipes. Existing production authority governs every generation.
---

# Seedance 2.5

Seedance 2.5 extends the Seedance 2 family to longer clips and larger multimodal
reference sets. It is hosted; there are no local model weights in OpenMontage.
Read the shared [visual-development](../../../skills/creative/visual-development.md)
owner for the quality-first reference/image preparation method and two-route
story-fit/cost comparison. This provider skill translates that method into the
selected route's actual inputs. Examples and vendor capabilities never expand
the current production contract, image allowance or source-transfer authority.

## Choose a supported route

| Route | Tool call | Notes |
|-------|-----------|-------|
| OpenArt MCP | `openart_mcp_video`, model `byte-plus-seedance-2-5`, mode `element2video` | Separate, untimed reference inputs through the current exact connector form; see [OPENART_MCP](../../../docs/OPENART_MCP.md) |
| fal.ai | `seedance_video`, `model_version: "2.5"` | T2V, I2V, and reference-to-video |
| Volcengine Ark | `seedance_ark`, `model: "2.5"` | First-party model ID `doubao-seedance-2-5-260628`; custom token price required for cost estimates |
| Runway | `runway_video`, `model: "seedance2_5"` | T2V, I2V, V2V; 480p/720p |
| ComfyUI Partner Node | `comfyui_video`, `model_family: "seedance_2.5"` | Hosted and paid despite running in a ComfyUI graph |

Do not invent a Replicate, HeyGen, or Higgsfield identifier when their current
public API schema does not list Seedance 2.5.

> **Route evidence, not a universal capability promise.** The OpenArt MCP
> `element2video` form checked 2026-10-15 exposes 4–30 second integer durations,
> 480p/720p/1080p, aspect ratios including 9:16, and `generateAudio` (default true).
> The current fal native API also documents 1080p; a registered adapter may expose
> a narrower subset. Recheck the registry and exact selected form before authoring.
> Never import another provider's resolution, mode or controls by analogy.

## Reference inputs, labels and limits

- ByteDance's published model ceilings are 30 reference images, 10 reference
  videos and 10 reference audio clips. These are not proof of the selected
  connector's accepted maximum or of consumption in a particular result.
- Keep combined reference video/audio duration within the provider's documented
  ceiling; Runway caps it at 30 seconds.
- For Runway video-to-video, the source video consumes one video slot.

Reference inputs are provider-specific. fal.ai documents `image_urls`,
`video_urls` and `audio_urls`, addressed in its prompt as `@Image1`, `@Video1`
and `@Audio1` in array order. Runway uses `references`, `referenceVideos` and
`referenceAudio`; Ark uses typed content entries with roles. Always call the
registered OpenMontage tool instead of constructing a provider payload manually.

For OpenArt MCP, the current `element2video` form requires `visualReferences`
with at least one entry. Each native entry requires `type`, `id`, `url` and
`label`; no `maxItems` is published in that observed form. Retain actual upload
labels, even unhelpful names such as `0.png`. The existing compiler's ordered
`Reference 1`, `Reference 2`, … mapping binds those native labels to source IDs/files and cast IDs.
Use that mapping to tell the prompt which file supplies which subject and job.
Do not assume OpenArt's web examples using `@Image 1` create MCP token bindings.
Preserve exact labels/order and the prepared native request; never rename a
retained upload or invent a new token convention as a provider feature.
Use the emitted native prompt unchanged at the governed host handoff; do not
rewrite its mapping after the source-bound request is frozen.

Capacity is not a quota. Use enough clean evidence to establish the needed
identities, props, setting and difficult action geometry. More views of one
character/object may help; state that they depict the same subject so they do
not imply duplicates. Exclude unrelated people, backgrounds and incompatible
states through narrow inherit/ignore directions. Do not impose one image per
element as a maximum or add files just to fill slots.

`element2video` references are **untimed guidance**, not start/end or intermediate
frame pins. A staging image can explain a grip or final geometry while timed
shot text describes when it happens. Separate `image2video` endpoint controls do
not turn reference-mode images into native pins. Use the registry's exact input
description; required native timing/endpoint controls cannot be replaced by prose.

Submission is also distinct from observed consumption. A saved request with five
references and a history card showing two thumbnails proves neither a two-image
limit nor that all five were used. Retain consumption as unknown unless actual
provider evidence resolves it; do not rerun to investigate under production authority.

---

## Prompting

Lead with the shot structure, then subject, action, camera, lighting, and audio.
For multi-shot work, give each beat an explicit time range and use quoted text
for dialogue. Reference each supplied asset by a stable role in the prompt.
Thirty seconds is a ceiling, not a target: use a shorter generation when the
scene has only one meaningful action.

### Section order

The following is an optional organizing recipe for complex shots, not required
syntax or a guarantee. A simple action can use a shorter prompt with clear
reference jobs, opening geometry, ordered action, camera and sound direction.

```
GLOBAL STYLE
  Genre, color grade, film stock or digital look, aspect ratio, shutter behavior,
  and anything that must not appear. Everything below must match this.

SCENE
  A one-line logline: what happens, where, in what mood.

CHARACTERS
  Face, hair, build, wardrobe. If a reference covers this, just name which
  reference is which character.

LOCATION
  The space and its props, separate from the people in it. A vague location is
  the single most common reason a multi-shot sequence drifts between cuts.

FIRST FRAME AND BLOCKING
  Exact starting positions: who is where, facing which direction, as the clip
  opens. Something fixed before any motion starts.

Shot 1: [shot type] [action in one or two sentences]. Hard cut.
Shot 2: [...]. Hard cut.
Shot 3: [...].
  Pacing is built here. State camera distance and framing per shot: vague
  transitions are the most common cause of a sequence collapsing.

OPTICS / CAMERA
  Focal length, camera height, handheld or dolly or crane, per shot.

PHYSICS
  Fabric, smoke, hair, liquid. Anything that would look wrong moving like a solid.

LIGHTING
  Where the light is motivated from, its direction, how it falls on faces.

AUDIO
  Ambience, specific effects, and what must not be heard. Default:
  "No music, no discernible dialogue", unless the scene needs otherwise.
```

Use the sections that carry real direction for this brief. CHARACTERS can be
short when references establish appearance. Give multi-shot work explicit beat
timing and cuts, but do not force internal cuts into a continuous action. Preserve
the brief's audio choice and the exact route's actual audio controls.

### Genre, era, and lighting carry more than prose

Some vendor web recipes use UI selectors for genre and lighting. If the exact
registered route exposes none, write the intended genre, era, grain/color,
motivated light and emotional register in the prompt. Those descriptions guide
appearance; they are not hidden API controls or proof of a particular result.

---

## Locks: continuity across cuts

For complex continuity, named blocks can make the preserve list easier to read.
These are prompt organization, not enforced locks or native frame controls.
Default to compact, clear actions. The coordinates, lens degrees and event tracks
below are optional examples for a shot that benefits from that precision, not
required fields or additional authoring chores. Use only relevant directions.

| Optional block | Intended direction |
|---|---|
| `FIRST FRAME AND SPATIAL BLOCKING` | Positions as x/y percentages of frame, plus the reverse-angle note: when the camera crosses, who ends up on which side, so they never swap |
| `SCREEN DIRECTION` | The axis. "Sea always screen-right, dune line always screen-left, never reverses" |
| `LENS LOCK` | One focal per segment in degrees of diagonal field of view: 84° wide, 47° normal, 29° portrait, 18° telephoto. Plus "no lens drift mid-segment" |
| `SINGLE-LENS LOCK` | One focal for the whole piece; framing changes because the operator walks. This is what reads as documentary |
| `COUNT LOCK` | "Exactly four, never five, no background double, no mirror or reflection showing an extra copy" |
| `STILLNESS LOCK` | The list of what does not happen: nobody stands, nobody hugs, no hand leaves the blanket |
| `POSITIVE LOCKS` | Closing block restating in positive terms what holds across cuts |
| `IDENTITY / NO-IP LOCK` | "A wholly original, invented face." No logos, no readable text, no recognizable melody |

**State progression.** Describe changes that should not reset across cuts:
accumulating wetness, or a cookie progressing from whole to bitten to finished.
Inspect the generated progression rather than assuming the wording enforces it.

**One grammar per action.** When an action repeats, define one way to perform it and
forbid the rest, then close the list: exactly five sword actions in the piece, each with
its timestamp. Same for slow motion — allow-list it to named moments and nowhere else.

**Event tracks.** For a relevant ambient beat, describe its intended time and
visible effect. Avoid micromanaging every wave, raindrop or crowd member; text
timestamps remain direction rather than guaranteed event timing.

**Substitute, don't only forbid.** If you forbid something, supply the concrete
replacement. For example, describe orange light, sparks and ash for an invented
fantasy effect instead of relying only on an exclusion. This remains intent,
not an enforceable simulation rule.

---

## Assets for reference mode

Supply the useful sources **directly to Seedance's supported reference inputs**:

- Character: readable silhouette, costume, material and face/detail views where
  those facts matter. Additional views must retain the same subject and proportions.
- Prop: construction, scale and critical working parts; keep its owner and count
  explicit, particularly when several angles depict one object.
- Setting: scene layout, landmarks, light and a useful view of the action space.
- Action staging when useful: the actual grip, contact, stance, separation or
  target state that text alone leaves ambiguous. Identify the spatial fact to
  inherit and the pose/state that must change during the clip.

Select or author these through the existing image plan and allowance. Inspect
the actual bytes for their declared role before use. A role label does not prove
the image has correct contact, anatomy or geometry. There is no compulsory face
removal, character-sheet layout, image count, trial count, empty-location video
or payoff image. Multiple useful views are welcome; unnecessary tests and new
video generations are not preparation requirements.

Seedance reference mode can compose a scene from these elements; it need not
receive a composite opening instead of them. If the brief selects literal
image-to-video, use the actual composed start through that different native mode.
Do not silently change treatments. H3's composed-start method and the same-action
example live in the shared visual-development owner linked above.

For a reference-mode prompt, assign narrow jobs first, then direct the action:
character appearance only; parcel construction only; counter layout and light;
optional staging view for grip/relative scale only. State where the character and
parcel begin, how the base contacts the counter before the hands release, and
what remains still at the finish. Express these jobs against the **actual route's
ordered labels**, not unbound illustrative names. Timed text can pace approach,
contact and release; the supplied stills remain untimed reference guidance.

---

## Voice

A consistent conditioning sentence can describe a character's language, accent,
tempo and manner. Reuse it across the sequence when continuity matters. A
supported, authorized audio reference is a separate input and must keep its
actual role and speaker binding. Prefer clean, isolated voice/music references
when the brief and available sources permit; neither text nor audio guarantees
identical performance. Do not invent audio-reference support on another route.

---

## Known difficult cases

| Risk | Preparation to consider within existing authority |
|---|---|
| **Singing or exact voice performance** | Use an authorized audio reference only if the selected route accepts it; state performance and speaker roles. Neither a label nor a supplied recording guarantees exact audio reproduction. |
| **Sustained physical contact** | Favor clear body/prop geometry and simple ordered contact. A rights-cleared staging or motion reference can help when supported; it is not proof that the generated contact is correct. |
| **Multi-character scenes** | Bind each character's references and screen position explicitly, avoiding ambiguous pronouns and competing background faces. Inspect identity and interactions over the whole clip. |

---

## Chaining and correction

Extension and editing are model/provider capabilities, not instructions to use
them in every workflow. They are outside the one-shot factory's once-only video
path, which recovers only the same original job and never opens a replacement or
correction route. Generic pipelines may use them only when the exact registered
route, manifest and retained authority explicitly permit that operation.

Where such an edit is already supported and authorized, describe the narrow
change and preserve list, for example:

```
Around the 2-second mark, change only [X] from [before] to [after].
Keep the characters, movement, camera, lighting, composition and timing unchanged.
```

This is an illustrative edit recipe, not a new repair entitlement. Preserve
original bytes, attempts and provenance. Do not infer a production-wide ban on
starting frames from another creator's workflow; select native roles for the
current brief and route.

---

## Cost and verification

All supported routes are paid. Confirm the exact provider/model before calling
and review the result for identity continuity, cuts, lip sync, audio artifacts,
and prompt adherence. ComfyUI Partner Nodes use prepaid Comfy credits and are
not an offline fallback.

Preparation and verification:

- Bind the useful current references and their jobs before dispatch. Each request
  must carry its own necessary context.
- Choose resolution and duration for the shot and authorized route. Cheap draft
  modes, upgrades and experiments require their own existing allowance; they are
  not automatic preliminary steps or a reason to cap preparation quality.
- Keep actual request, reference order/bindings and result evidence in the existing
  records. A transport success is not evidence that every reference was followed.
- **Judge the whole clip, never a single frame.** A good still can come from a generation
  that falls apart at second 9.
- Let cut timing serve the action and edit rhythm; a cut on movement is an option,
  not a requirement for every scene.

---

## Primary guidance and worked examples

Checked 2026-10-15. Keep the evidence classes separate:

- [ByteDance launch and reference demonstrations](https://seed.bytedance.com/en/blog/one-take-creation-flexible-referencing-introducing-seedance-2-5)
  (2026-07-31) pairs prompts with vendor-selected demos: the concert example
  assigns venue, performers, instruments and audience to separate images; the
  clay-render examples separate blocking/camera guidance from appearance. These
  demonstrate a reference-led approach, not a guaranteed success rate. ByteDance
  also acknowledges remaining instability in complex motion and interactions.
- [OpenArt prompt guide](https://openart.ai/blog/seedance-2-5-prompt-guide/)
  (2026-08-10) supplies illustrative recipes for narrow reference roles, multiple
  angles of the same object, and timed action. Its two-character cafe example
  separates character photos, cafe and camera feel. Treat its `@Image 1` web
  syntax as surface-specific; these are recipes, not this project's empirical QC.
- [OpenArt cinematic guide](https://openart.ai/blog/cinematic-ai-videos-with-seedance-2-5/)
  (2026-08-12) discusses directly attached character/product/style references,
  simple physical staging and checking live surface limits rather than assuming
  the full native model capabilities.
- [fal reference-to-video API](https://fal.ai/models/bytedance/seedance-2.5/reference-to-video/api)
  documents its array fields, no-space `@Image1`/`@Video1`/`@Audio1` syntax and
  task controls. Its request example is an API usage example, not proof of a
  verified multi-reference production result or OpenArt's MCP behavior.

No new live test was performed for this guidance. Provider claims, illustrative
prompts, curated demos, an accepted native schema and actual per-result review
are different kinds of evidence; none substitutes for the others.

## Optional prompt reference

[`reference/techniques.md`](reference/techniques.md) — ten prompt archetypes, the settings
each used, and the technique each one illustrates. Consult a relevant archetype
when helpful; its stronger performance claims are vendor recipes, not required
steps, API guarantees or authority for extra generation.

## Provenance

The optional section/lock patterns and linked archetypes were adapted from the
Higgsfield team's published prompting and production material. These are
first-party workflow accounts and selected examples, not a model specification
or independently reproduced quality evidence. No prompt is reproduced in full.

- <https://higgsfield.ai/blog/seedance-2-5-prompting-guide>
- <https://higgsfield.ai/original-series/cully-hill-boys/full-film>

The route table, provider field names, and cost guidance in this file are OpenMontage's own
and take precedence over anything a vendor blog implies about API availability.
