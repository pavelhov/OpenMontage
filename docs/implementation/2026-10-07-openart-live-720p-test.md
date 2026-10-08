# OpenArt CLI 720p live test — October 7, 2026

The separately approved provider-qualification sample generated and collected a real video through the registered OpenMontage OpenArt CLI route. The account observed at collection and the frozen original profile both report Free. This verifies this exact PixVerse V6 test on this account; it is not a general Free-plan/model entitlement claim.

Requested: `pixverseV6`, `text2video`, 1 second, 16:9, 720p, one red cube dropping onto a gray table, no reference uploads or dialogue. Actual collected file: H.264, 1280×720, 24fps, 1.041667 seconds, 109,322 bytes. Full decode completed without errors.

- Project: `projects/openart-720p-qualification`
- Original attempt: `697e2c37-c630-4a25-b810-1f2bb5c4f750`
- Canonical report: `artifacts/provider_qualification_report.json`
- Report SHA: `5e3670ce4e455edd0323192c4ea72e316d0105a5f5eb47eb8b324232aa1a906d`
- Output: `assets/video/cube-drop-original-1.mp4`
- Output SHA: `09c473672307b4881c65afd83f8f01d813f15a28db7f50f46dc72cb4554be15e`
- Result contract SHA: `cc930acc66c4f1df90fac715f68de993edbc9b1654ff1264538e624d827242d3`
- Origin profile SHA: `82d0d8faa12d21a9ea32dd766f1b505825f16e0a5cdbfaf622ed5f8580722476`

There was exactly one original submit process, zero repairs and zero uploads. Original receipt, frozen authority and output bytes remain retained. The ledger slot resolved terminal; credit billing stays unknown, with no USD amount or enforceable ceiling. No purchase, top-up, paid API, browser generation, benchmark or publishing occurred.

## Compatibility repairs discovered by the actual test

The official CLI returned top-level `historyId` and `PENDING`, while the staged profile had tentatively declared `creationId`. Original readback returned `history.id`, `history.status=completed` and `resources.0.url` on `cdn.openart.ai`. These observed response differences initially held the original safely; no replacement generation occurred.

Commit `80b0351` adds registered read-only original submit recovery with immutable proof, strict original/process/account/native/authority correlation, safe schema reporting, restart checks and proof-aware later ordinary parsing. Commit `9f25f47` accepts the observed `history` identity wrapper while excluding resource IDs and generation echoes. Original launch/profile/request bindings were preserved.

Independent parent checks passed 112 tests in 228.64 seconds, followed by 2 privacy/marker delta checks and 18 history/resource checks. Worker broader checks passed 190 tests, with targeted followups. Final peer recovery/adversarial checks passed 69 tests. Counts overlap and are not added together. Owned Python compilation, `make lint RUN_PYTHON=.venv/bin/python` and diff checks passed. Ruff was unavailable and is not claimed as a completed check.

## Qualification limits and creator workflow

Transport/result success and collected bytes are verified. Creative quality is not production reviewed, and production certification is false. The failure-state string `failed` remains a declared, unobserved value; the passing original proves terminal success rather than failure semantics.

The Studio premise/cast/reference/dialogue/serial-shot/review/targeted-repair/assembly workflow remains the target. OpenArt is an optional route for control fit and quality intent, including continuity after Grok quota exhaustion when the planned route and retained scope cover the unfinished shots. It does not inherit a Grok approval or replace an uncertain original. A bounded approved first-pass batch can cover its exact clips without per-clip confirmation.

Current OpenArt qualification covers reference-free text-to-video. Cast references, native dialogue/voices, multiple references and end-frame controls are not qualified for this workflow. Comparative model rankings and guaranteed clean first attempts have not been established. Unknown-cost Auto-continue remains unavailable. The result checkpoint pauses for the test clip's human review; further generation is not authorized by that review alone.
