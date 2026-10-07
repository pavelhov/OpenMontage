# OpenArt alongside Grok: implementation handoff

The local implementation is committed and reviewed. OpenArt generation is
paused at live qualification: the official CLI exposes working account and
model discovery, but the observed credit prices cover 480P defaults rather than
the approved benchmark's 768P requests. No real generation, upload, credit
reservation, purchase or publication occurred.

## The video-making experience

Continue the creative conversation in Social Studio. OpenMontage supplies the
provider choices through the existing planning and review workflow:

1. Describe the video, story, cast and references. Inspect the boards and shot
   plan before generation.
2. Review an exact provider mapping for the shots. Grok CLI uses the existing
   subscription and reports no selectable video model; OpenArt lists only
   models with complete, verified qualification. OpenArt credits and Grok's
   unknown remaining quota appear separately.
3. Choose Strict approval or approve an optional Auto-continue policy. The
   policy names the allowed routes/models, locked cast/dialogue/story,
   permitted changes, attempt caps, credit ceiling and checkpoint stages.
4. OpenMontage prepares the exact request and references, checks the native
   controls and retained approval, then launches each approved attempt once.
   Collection and reconciliation continue on that original attempt.
5. Review the footage and assembled video against the story. Failed or
   uncertain attempts remain visible. An assembled file becomes certified
   delivery only after the required current audiovisual review.

Auto-continue announces and logs each route and approved compromise. Missing
controls, changed approvals, exhausted caps or an uncertain original job stop
continuation. It cannot authorize benchmarks, purchases, top-ups or publication.
The referenced C75 chat supplied operating context; this work does not migrate
or repair that episode or add a new Social Studio interface.

## Implemented

- Official CLI transport, private receipts, staged account/model qualification,
  durable jobs, original-attempt collection and provenance.
- Shootable prompt compilation, timing and reference coverage, native request
  binding and preparation review before spending.
- A shared account credit ledger, exact quote authorization, crash-safe
  dispatch and a guard against duplicate same-shot video attempts.
- Qualified provider menus, explicit provider pins, bounded Auto-continue,
  approval-aware checkpoints and reports of choices, attempts and credit state.
- A fixed paired benchmark manifest and reporting path. Offline fake-CLI
  workflows include actual local FFmpeg assembly; their audiovisual judgments
  are synthetic test evidence.

## Live observations

On 2026-10-06 at 23:55 UTC, official CLI 0.1.1 returned an account identity,
plan and integer credit balance. Values remain private; it returned no explicit
authentication boolean. Earlier login-required evidence is superseded.

The actual catalog lists `fal-h3-max` and `fal-h3-max-turbo` for `text2video`.
Their observed `jsonSchema` forms allow 5 through 15 seconds, and their exact
read-only previews carry 5 seconds and literal `768P`. The adapter now reads
that wrapper consistently for controls, defaults and native preparation;
mismatched metadata and settings outside the declared schema are rejected.
This establishes form/preview compatibility, not real-result qualification.

The returned cost configurations are 5s, 16:9, 480P and one output: Max 125
credits and Turbo 75 credits. The provider states that price changes with
settings and is finalized at generation time. These are not 768P quotes or
conservative credit ceilings. The installed cost-command help exposes model
and mode flags, with no duration/resolution/configuration pricing flag. The
[versioned official documentation](https://github.com/OpenArt-AI/cli/blob/v0.1.1/README.md)
also documents model/mode pricing; undocumented behavior has not been qualified.

See [the sanitized live discovery report](2026-10-06-openart-live-contracts.json)
for command receipt digests, observed schemas and the limits of these findings.
No production profile was promoted. Neither inspected text-to-video form
contains native audio or an ending-frame field. That bounded observation does
not establish capabilities of other modes or models.

## Verification and local commits

The implementation passed a complete repository run of 3,891 tests, contract
checks, syntax/lint checks and independent review before live discovery.
The subsequent observed-wrapper repair passed 277 focused tests; the parent
independently inspected all eight changed files and ran 22 relevant cases.
The saved Strict source packet, prompt, native request and compiled request
still reproduce their original canonical bytes exactly. The final complete
repository run after the wrapper repair passed **3,910 tests**, with 11 skipped,
3 expected failures and one passing subtest, in 14:13. The final contract run
passed **1,293 tests**, with 7 skipped. Lint, explicit syntax and whitespace
checks passed.

Canonical commits include `8aaa6b7` (qualified route workflow), `4247fa3`
(Auto-continue), `f2d194f` (offline benchmark runtime) and `8204950` (observed
form wrapper and native schema validation). Earlier unit commits and authentic
failed/interrupted check history are retained in
[the progress record](2026-10-06-openart-factory-progress.json).

## Remaining gates

The approved plan requires provider evidence covering exact generation settings
and a conservative credit maximum before a paid attempt. Current responses do
not supply that contract. Image uploads additionally require explicit captured
nonspending and no-delayed-charge guarantees before the first upload; those
guarantees remain unobserved.

Once those prerequisites exist, demonstrate complete audiovisual review on
existing media, freeze the exact benchmark requests and quote evidence, and
obtain its separate credit approval. The proposed comparison is twelve
original attempts in six alternating Turbo/Max pairs at 5s/768P, with no extra
qualification or corrective attempts. The proposed 1,800 credits is not an
authorization or a current quote. Real result and billing qualification must
come from those original approved attempts.

U1–U5P are locally implemented; U6's offline code is committed, but its live
operational qualification is incomplete. No push, PR or later LFG shipping
stage ran. The structured return is
[the implementation envelope](2026-10-06-openart-factory-return.json).
