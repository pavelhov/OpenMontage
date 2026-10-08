# Practical CLI generation: proposed credit-policy amendment

Status: **DRAFT — not approved or active.** No live generation is authorized by
this document. The accepted OpenArt plan and Auto-continue amendment remain
unchanged until the user approves this amendment.

## Why the live route stops

The registered OpenMontage account tool successfully connected to OpenArt CLI
0.1.1 and validated a PixVerse V6 text-to-video preview at 1s, 16:9, 720p.
The account is Free with 40 credits. The cost command returned 50 credits for
a different configuration: 5s, 540p, one output, audio false. The 1s/720p
charge and free-account generation entitlement remain unknown.

The credit implementation requires maximum-charge, exact-request-hash and
billing-scope fields that the observed CLI responses do not supply. It cannot
qualify those responses. Closed-shot preparation also rejects text-to-video.
These are separate from authentication, which succeeded.

Grok CLI 1.0.34 exposes retrospective session usage, while OpenMontage reports
remaining media quota and media cost as unknown. Its existing explicit
unknown-cost approval does not promise a spending ceiling.

## Proposed behavior

1. Preserve the existing exact-quote mode and its credit ceiling. Existing
   approvals and Auto-continue policies keep their meaning and fail closed.
2. Add an explicitly selected **unknown-cost mode**. Approval binds the account,
   provider/model/mode, settings, request, references when applicable, and
   attempt limits. Reports state that an enforceable credit ceiling is
   unavailable. Unknown cost is never zero or a USD estimate.
3. Capture a fresh account balance and available price evidence before every
   launch. Classify a settings-matched estimate separately from a price for
   other settings. A mismatched default price cannot establish affordability
   or insufficient funds for the requested configuration.
4. Preserve one active generation per account, durable prelaunch receipts and
   original-attempt recovery. Use a typed unpriced ledger claim, never a fake
   zero-credit reservation. Pending or uncertain submission blocks another
   launch. A terminal result can release the account slot while billing remains
   explicitly unknown; it must not fabricate credit settlement.
5. Capture provider-reported billing when available. Otherwise record before
   and after balances as an **unattributed balance change**. Other account
   activity prevents reliable per-job attribution. A soft spending threshold
   can stop future attempts, but cannot guarantee the charge of the next call.
6. Preserve exact route pins, preparation reviews, creative locks and actual
   footage review. No automatic resubmission, purchase, top-up, paid API
   fallback, publication or benchmark is authorized.

## Implementation and acceptance

- Add the authorization variant, unpriced account claim, dispatch branch and
  truthful reporting. Verify account drift, changed requests, exhausted caps,
  uncertain original jobs and attempts to reuse unknown-cost approval as a
  hard ceiling are refused. Existing exact-quote tests must remain valid.
- Add guarded text-to-video preparation for a shot that requires no image
  reference or frame pin. Do not fabricate an approved start board, upload
  evidence or a result qualification. Reference-required shots retain their
  requirements. Image-to-video and uploads need their own actual compatibility
  evidence and explicit charge authority if nonspending cannot be proved.
- Independently review the changes and rehearse the exact sample with zero
  provider generation calls before proposing its live launch.
- The first live sample is one original PixVerse V6 text-to-video request at
  1s, 16:9, 720p, with no upload and zero repairs. Retain the final reviewed
  prompt and native preview before live approval. Its price is unknown; the
  current 40-credit balance is not a proven spending limit. A provider-confirmed
  insufficient-credit or unavailable-plan result stops the sample. Ambiguous
  errors remain uncertain and are never retried.
- Collect that original job, verify the downloaded bytes, play/review the
  result and qualify only its observed result contract. A one-second sample
  proves transport and collection; it does not establish production quality.
- After real qualification, offer bounded production batches with explicit
  unknown-cost approval and attempt caps. Optional Auto-continue needs an
  explicit new policy variant carrying these semantics, activated only for
  real-qualified routes. Do not migrate existing hard-ceiling policies.

Approval of this amendment permits local implementation and checks. The live
sample still requires approval of its retained exact prompt/request and unknown
charge exposure. The existing H3 paired benchmark is separate and unchanged.

Evidence: `docs/implementation/2026-10-07-openart-cli-smoke.json`.
Changed requirements: accepted plan R8/R9 and KTD5/KTD6; an optional later
Auto-continue activation also needs the corresponding R15/R16 policy variant.
