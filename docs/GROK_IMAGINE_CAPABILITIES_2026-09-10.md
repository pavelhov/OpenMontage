# Grok Imagine capability and timeout audit — 2026-09-10

Current policy: use the system Grok CLI, minimum 1.0.18 plus required-interface
checks. The system installation was subsequently updated to 1.0.25 stable.
The table below records the initial audit; follow-up sections record the update
and removal of the temporary separate installation.

## Verified provider boundaries

| Surface | Verified capability | Limit |
| --- | --- | --- |
| Grok web/mobile | Image 2.0 is the current image Quality Mode; Video 1.5 is a separate video model | No account-level web loop rollout was verified in this audit |
| xAI REST | `grok-imagine-image-2.0` generates/edits stills; `grok-imagine-video-1.5` accepts `image` and `last_frame` | Classic `grok-imagine-video` rejects `last_frame` |
| CLI installed at initial audit | 1.0.18, build `ea950872ad51`, initially reported alpha; existing single-image and multi-reference video primitives | No exposed ending-frame field or Imagine model override found |
| Latest CLI inspected separately | 1.0.27, build `a538938e5720`, alpha; stable channel pointer was 1.0.25 | Same ending-frame and Image 2.0 selection gaps; not qualified for production by this audit |

Image 2.0 does not replace Video 1.5: they serve different media types.

Primary sources checked online:

- [Image 2.0 announcement, August 7](https://x.ai/news/grok-imagine-image-2)
- [Current image generation contract](https://docs.x.ai/developers/model-capabilities/images/generation)
- [Video request modes](https://docs.x.ai/developers/model-capabilities/video/generation)
- [First & Last frame, updated September 8](https://docs.x.ai/developers/model-capabilities/video/reference-to-video#first--last-frame)
- [REST videos reference](https://docs.x.ai/developers/rest-api-reference/inference/videos)
- [CLI reference](https://docs.x.ai/build/cli/reference)
- [Pricing](https://docs.x.ai/developers/pricing)
- [Image Quality migration notice](https://docs.x.ai/developers/migration/imagine-image-quality-nov-2)

The feature-specific September 8 reference-to-video guide supplies the new REST
field contract; the generic REST page's visible content still carries an April
13 update date. The guide says dedicated `last_frame` parameters are not yet
exposed by the Python and Vercel SDKs. The implementation therefore sends REST.

## OpenMontage contract

`grok_video` retains its classic default. New functionality requires explicit
`model: grok-imagine-video-1.5`. `operation: first_last_frame` accepts
`last_image_url` or `last_image_path`; the first frame is optional and uses
`image_url`/`image_path` or the selector's `reference_image_url`/`reference_image_path`.
These become REST `last_frame` and `image`. `image_to_video` can also carry a last
frame. Reference guidance can be combined with pins on 1.5. Local images become
data URIs directly, without uploading them through fal.ai.

Example selector request (not executed):

```json
{
  "preferred_provider": "grok",
  "allowed_providers": ["grok"],
  "model": "grok-imagine-video-1.5",
  "operation": "first_last_frame",
  "reference_image_path": "projects/example/assets/images/loop.png",
  "last_image_path": "projects/example/assets/images/loop.png",
  "prompt": "Locked camera. A gentle cyclic motion returns to the opening pose.",
  "duration": "6",
  "resolution": "720p",
  "output_path": "projects/example/assets/video/loop.mp4"
}
```

Using the same image for both endpoints is a loop-authoring technique, not a
native `loop` flag or a guarantee of smooth velocity, lighting, or audio across
the seam. Inspect the resulting clip across repeated playback before shipping.
No paid generation was performed to validate visual seam quality.

Frame pairs use the reference-to-video path, capped at 720p and 15 seconds.
Last-only requests and optional prompts are supported. The adapter deliberately
supports HTTPS URLs/data URIs and local files, not Files API `file_id` inputs or
voice-reference controls. Unsupported aliases/control fields, conflicting
sources, model/resolution mismatches, and invalid durations fail before POST.

For stills, explicitly select `grok_image`, `model: grok-imagine-image-2.0`.
`quality` accepts `low`, `medium`, or `auto`; generation/edit source limits are
validated. The classic image default is preserved for existing callers.
Do not request an image model from a video adapter or silently route an exact
Image 2.0 request through the CLI.

Current REST estimate rates: Video 1.5 is $0.08/$0.14/$0.25 per second at
480p/720p/1080p, plus $0.01 per input image. Image 2.0 costs $0.04/$0.06 for
1k/2k low, or $0.06/$0.08 for 1k/2k medium, plus $0.01 per input image. Auto
currently means low for generation and medium for edits. These are estimates,
not CLI subscription charges. Repeated first/last inputs are counted as two
submitted image slots in the estimate.

## CLI inspection and update decision

Read-only `grok --version`, `grok --help`, `grok update --help`, and
`grok update --check --json` were used. The update check reported alpha 1.0.27.
The official installer at [x.ai/cli/install.sh](https://x.ai/cli/install.sh)
documents the artifact path format and the
[stable channel pointer](https://x.ai/cli/stable).

The 1.0.27 macOS aarch64 binary was downloaded to a separate temporary audit
directory and inspected with `--version`, `--help`, and offline binary-string
inspection. No installer script was executed. SHA-256 of the uncompressed
binary: `6e85277b0432c894abcb325d9132bdc88c9715fa3089f65b7589963d5522ec80`.

Both binaries contain a four-field `ImageToVideoInput` and a six-field
`ReferenceToVideoInput`, but no `last_frame`, `end_frame`, or
`grok-imagine-image-2.0` string. The reference tool describes images as content
references without locking the first frame. Both contain
`grok-imagine-image-quality`; this does not prove a server-side alias resolves
to Image 2.0 today. The official REST migration redirects that older slug on
November 2, not as of this audit date.

Binary inspection is evidence of the exposed local contract, not a live probe
of an undocumented server feature. Upgrading to 1.0.27 was not shown to unlock
the requested controls, so the active global binary, pinned adapter version,
production checkout, and C36 assets were left unchanged. A later CLI upgrade
needs compatibility qualification, not invented arguments or a relaxed pin.

## C36 timeout evidence

Read-only evidence: `s1-video-{request,result}.json` and
`s1-video-retry-{request,result}.json` under the C36 production project's
`assets` directory, plus the two corresponding local Grok session logs.

| Attempt | Observation |
| --- | --- |
| `01a08c15-21b4-7f92-9525-c8613d42c2e9` | 600-second caller timeout. Session created 16:09:50 UTC; `turn_started` at 16:10:00. Two event records total (`mcp_config_resolved`, `turn_started`), one user-message update, no recorded media tool call or completion. |
| `01a08c23-a719-7400-8f9c-a21d67a88830` | Retry timeout was 300 seconds. Session created 16:25:41 UTC; tool started 16:25:57, completed 16:26:31, turn ended 16:26:34. Returned a native 6.041667-second H.264 clip at 720×1264. |

The media arguments and prompt were identical; only the caller's timeout
changed. Both results report CLI 1.0.18 and `grok-4.6`. The successful session
log spans about 53 seconds; the user's roughly two-minute observation may
include caller overhead not recorded in that session. The saved result JSON
does not include a complete outer wall-clock duration.

The failed log's last observation precedes any recorded media call. This does
not establish whether the cause was CLI initialization, a connection/leader
issue, model response latency, or another condition. It does not prove that no
remote submission or charge occurred. The successful run on the same versions
does not establish an update regression. Neither an automatic retry nor a paid
diagnostic generation was performed.

Timeout results now retain the configured deadline, process stage, a generated
session UUID, and bounded activity summaries from partial stdout and that exact
session's event/update files. Summaries retain only known event types/counts;
raw prompts, tool arguments, stderr, and MCP launch details are excluded.
`dispatch_status` remains `indeterminate` for a media timeout, with no retry or
fallback. Version-check timeouts remain `not_dispatched`.

## Validation scope

Offline tests cover exact REST payloads, local/URL first and last frames,
last-only mode, loop endpoint equality, pricing, rejected model/input mixes,
selector behavior, CLI fail-closed behavior, partial/missing/truncated logs,
session correlation, and credential-free diagnostics. HTTP and process
boundaries are mocked; no paid calls, live canaries, or automatic retries.

Final verification: 167 tests passed across the Grok image/video, CLI media,
CLI diagnostics, image/video selector routing, and Grok compatibility suites.
`git diff --check` passed. Final global `grok --version` still reported 1.0.18
(build `ea950872ad51`), with a stable channel label; this audit did not run an
update/install command or edit global configuration.


## Later authorized global CLI update — 2026-09-10

At the user's subsequent request, ran `grok update --stable`. The global binary
is now `grok 1.0.25 (f7e67d6988e2) [stable]`; `grok update --check --json` reports
latestVersion 1.0.25, updateAvailable false, channel stable, autoUpdate null.
Before this update, config selected alpha and had no explicit auto_update value;
that does not establish why the old binary had not updated automatically.

Preserved the qualified binary at `/Users/pavel/.grok/bin/grok-1.0.18` and set
`GROK_CLI_PATH` to that path in local, ignored `.env` files for this worktree and
`/Users/pavel/tools/openmontage`. Both media adapter readiness checks pass after
loading those settings. This preserves the exact version contract independently
of the general CLI installation. No media generation was run to qualify 1.0.25.

The official [1.0.25 changelog](https://x.ai/build/changelog) includes headless
prompt timeout and concurrent startup fixes. It does not establish Image 2.0 or
last-frame support in the native media tools.


## System CLI compatibility policy

The subsequent user-approved implementation replaces exact-version pinning
with a minimum of 1.0.18 and required-option checks. OpenMontage uses the system
CLI on PATH, accepts newer numeric releases with the advertised interface,
and reports the observed version in results. Offline dry runs report an unknown
installed version. Existing tool/argument, completion, artifact, and no-retry
checks remain active. CLI 1.0.25 passed the read-only compatibility probe.

The temporary second installation and local path overrides from the update
above are superseded by this policy and removed during delivery. The earlier
audit/version observations remain historical evidence, not install requirements.
