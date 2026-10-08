> Publication note: This copy redacts private account correlators and local workspace/temp paths; technical schemas, control evidence, test outcomes, and non-account provenance are retained.

# OpenArt model controls — October 7, 2026

This note records the observed catalog and the local OpenArt CLI control boundary. Catalog forms describe provider-side model endpoints. They do not establish which values the installed CLI can send, which controls have been verified by an original result, or which routes are production-qualified.

## Evidence captured

The guarded, read-only catalog sweep captured 17 video model IDs and 45 advertised model/mode form pairs on October 7, 2026. Every form response is retained in the private OpenArt receipt store; the catalog receipt is `2026-10-07T221310-f9045d15` (SHA-256 `c01d39e6930a996366da8fcc9a4ed6cd56cb65e3cd9413c405e8a0b2f0f734ac`). The checked-in [structural fixture](../../tests/fixtures/openart/live_form_schemas.json) is derived from the 45 exact form receipts. It keeps receipt IDs and hashes, model/mode identity, JSON Schema types, properties, required fields, enums, bounds, item limits and root combinators. It strips defaults, examples, descriptions and provider sample text. Six receipts use root `anyOf`/`oneOf` schemas: three Kling 3 Omni forms, two Kling V3 forms and Smart Shot. Those are retained successful responses whose root-union shape needed dedicated parsing; they are not CLI form command failures.

The test fixture is a sanitized structural copy, not a live catalog refresh. Tests analyze all 45 pairs with the verified local CLI 0.1.1 surface. This checks schema interpretation and control binding offline; it does not establish that a model accepts a setting, that an upload is free, or that generated output satisfies a visual requirement.

## Three separate control layers

**Endpoint form controls.** The captured forms describe each provider endpoint. For example, H3 Turbo image-to-video has `startFrame` and `endFrame`; multiple BytePlus and other element-to-video forms expose `visualReferences`. Some forms also expose audio-related settings such as `generateAudio` or `audio`. These are observations about form schemas only.

**Local CLI transport.** OpenArt CLI 0.1.1 documents `--duration`, `--aspect-ratio`, `--resolution`, and a single `--image`, with the image selecting image-to-video. A guarded preview receipt (`2026-10-07T223851-592bce16`, SHA-256 `879ae5d89474f542d2cade476fef7d9f6fbe719b60c54a800e7f09f1dd74bae0`) showed `params.startFrame` with `label`, `type` and `url`, but no `id`; no URL value is retained in the fixture or this note. H3 Turbo's captured image form requires `type`, `id`, `url` and `label`. Its helper-level role binding therefore does not produce a schema-valid exact request, and the capability test verifies the mismatch instead of implying H3 image-to-video readiness. The observed command surface has no generic native-parameter flag, no end-frame flag, no repeatable or rich reference-image/video/audio argument, and no explicit audio-control flag. A form property can remain visible while binding fails closed as unsupported by the transport. Catalog element-to-video forms with visual references remain unreachable through this CLI because it cannot select that mode. No provider command, upload or generation is run by these tests.

**Live route qualification.** A schema and a local binding are not a real-result qualification. The recorded live media test qualifies only the precise PixVerse V6 reference-free text-to-video attempt described in the [720p test record](2026-10-07-openart-live-720p-test.md). The 45 form receipts do not qualify every catalog model, image-to-video, ending-frame control, references, generated audio, quality, billing or production readiness. The H3 Turbo test is an offline control-path test, not an H3 generation test.

## MCP discovery

OpenArt's [official MCP page](https://openart.ai/mcp/) advertises broader video controls, including start and end images, and access to the full model catalog through an MCP integration. It uses separate OAuth authentication against the same credit account. The official integration is installed and authenticated; inspected schemas cover 17 video models and 45 modes. The retained schema-only observation is `<HOME>/.openmontage/openart/mcp-discovery/2026-10-07-schema-observation.json` (SHA-256 `d407e39696e0d0ddef64e1a82d08cfd972d4e7d84236a02437b5be75b0fc856f`). H3 Turbo image-to-video requires `startFrame` fields `type`, `id`, `url` and `label`, with optional `endFrame`; MCP `generate_video` accepts full params. This establishes endpoint schema controls only. A local production bridge and separately governed live-result qualification remain pending; no CLI mode, profile or approval migrates or becomes qualified through MCP discovery.

The current CLI's `native_reference_digest` still rejects role-bound `input_assets` as an unverified transport. Although the qualification packet can encode source-bound roles and unknown-cost authorization checks a reference digest when one is available, this does not yet permit a referenced production qualification through the current CLI path. The H3 Turbo CLI preview also omits the form-required `startFrame.id`. Both transport and exact-preview gaps remain blockers; the inspected MCP form does not close them for the CLI.

## Verification and remaining limits

Run the offline catalog tests with:

```bash
.venv/bin/python -m pytest tests/lib/test_openart_catalog_coverage.py -q --tb=short
```

Result: **8 passed**. Coverage verifies all 45 receipt-backed fixture entries and 17 distinct model IDs, schema sanitization, all captured form pairs against CLI 0.1.1, the six union forms, H3 Turbo role binding and the separate missing-`id` schema mismatch, refusal of end-frame and multimodal reference paths, and transport rejection of form-level audio settings. It does not make network calls or read private receipts at test time.

Provider published prices are outside this implementation note and were not compared. Exact settings quotes, upload charge timing, per-model result contracts, output quality and broader production qualification remain separate evidence gates.
