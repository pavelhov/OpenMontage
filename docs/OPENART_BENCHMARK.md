# OpenArt paired benchmark contracts

The six fixed cases are identity/action, exact dialogue, speaker handoff,
completed prop action, continuity, and payoff/reference sensitivity. Each is
visited once as an adjacent Turbo/Max pair: twelve original occurrences at
5 seconds and 768p, alternating the starting model, three starts each. Model
labels are not provider IDs. Both exact model IDs must come from observation.
The first result-contract qualification for each model consumes its first
occurrence within these twelve. There is no extra qualification or corrective
video attempt. Optional auto-continue never changes this frozen benchmark.

## Frozen fixture manifest

`provider_benchmark.schema.json` version `draft-2` is closed and explicitly
`fixture_only`. Each model profile binds its original pre-submit creative SHA,
account, workspace, CLI version, tier, form, defaults, and observed credit
quantum. Later result proof attaches separately and cannot change that origin.
Each of twelve logical `occurrence_id` entries freezes its own native body,
argv and control SHAs, compiled request, preparation review, semantic and
reference byte SHAs, quote evidence, exact quoted amount and ceiling. Paired
semantics/reference bytes and account/version/tier/workspace/quantum must match.

Predeclared deliverables separately identify their original occurrence IDs and
story revision. A runtime `attempt_id` is a distinct identifier supplied by the
actual attempt ledger; it is not forced to equal the logical occurrence ID.
Every change to any frozen field changes `approval_digest`. Live approval,
current bytes/evidence checks and the exact ledger reservation remain required
outside this fixture contract.

Credits accept nonnegative integers, exact decimal strings, or integer
numerator/positive denominator objects. Floats, booleans, nonfinite values,
negative amounts and quantum rounding fail. Arithmetic uses `Fraction`, with
no Decimal precision context; outputs use JSON rational objects with string
numerator/denominator. The proposed 1,800 credits is not an authority or default.
The fixture manifest requires observed allowance evidence and all twelve exact
ceilings must fit it. Runtime quote validation uses the retained qualified quote contract; fresh eligibility is an explicit operation.

## Fixture metrics and resume

`validate_fixture_records(manifest, records)` checks every row, including its
model/case/occurrence against the frozen contiguous prefix. Runtime attempt IDs
must be unique. Repeated accepted output bytes cannot count twice. Unknown
fields/models, retries, gaps, reordering and substitutions fail. All attempts,
including transport failures, contribute to their model's attempted denominator.

Acceptance is derived from closed fixture v2 synchronized AV/story facts: named
reviewer/evidence, original bytes and story binding, full watch/listen interval,
all dimensions and all required predicates. Speaker/endpoint failures, missing
AV review and provisional diagnostics cannot count as accepted. These facts
are explicitly synthetic; an `accepted` boolean is not a permitted shortcut.

`summarize_metrics(manifest, records, certifications=[])` counts accepted source
seconds once per original runtime attempt. Separate closed fixture certification
records bind predeclared deliverables to accepted original attempts plus full
v2 delivery review and provenance SHA. Fixture certification is reported as
`fixture_certified_original_deliverables`; live `certified_original_deliverables`
is always zero, and `live_certification_ready` is false. A passing clip alone
never counts as a certified deliverable.

Unknown billing retains the exact ceiling as a hold. Known charges and held
amounts are shown separately. Any unknown charge in a model makes its economics
incomplete and its credits-per-accepted-second ratio null. Zero accepted seconds
also yields null. Fixture `credits_per_accepted_second` uses its known gross charges; fixture rows have no refund field. Partial/unbalanced or nonterminal results have no completed
comparison; `comparison_conclusion` is always null in this diagnostic helper.
Six pairs do not establish provider-wide reliability or an automatic default.

Observed known debits above the quote/ceiling or outside the observed quantum
remain full exact billing facts. They are never clamped, erased, relabeled as
unknown, or made a reason to suppress the entire report. `billing_violations`
identifies each original occurrence/runtime attempt/model, full charge, quote,
ceiling, quantum and each violated boundary. Known totals include the whole
debit. Its model economics are flagged, its cost ratio is null, and the benchmark
comparison is incomplete. `billing_quarantined` signals that no resume candidate
is permitted, even with an eligible fixture proof claiming inflated remaining
credits. A negative available balance safely halts. Malformed numeric facts,
known billing without a charge, and known billing with a hold still fail structurally.

Every unaccepted terminal/complete result requires an actionable primary
failure category consistent with its declared failed AV/story checks. Missing
review requires `review_availability`; failed speaker and completed-action
checks require the corresponding dialogue/speaker or action endpoint category.
Passing complete results cannot carry a failure category. Pending, running and
unknown acceptance are counted and labeled with both IDs in `job_states` and
`unresolved_acceptance`, separately from terminal failure categories. Authoritative
fixture known billing, including a zero charge, does not resolve job acceptance
or allow resume. Job and billing states remain independent.

`next_resumable_attempt` requires exact current approval digests and a closed
`fixture_resume_proof` with ledger SHA, eligibility state and exact remaining
allowance. Remaining credits cannot exceed the observed allowance after known
charges/holds and must cover every untouched frozen ceiling. Pending, running,
and unknown submission acceptance pause. A terminal closed attempt with unknown
billing alone does not pause if the retained hold and remaining eligibility
proof permit continuation. The only next candidate is the next contiguous
original occurrence. Any observed billing anomaly quarantines that selector.
No caller boolean establishes live permission.

## Runtime authority and reporting

The separate closed `runtime_manifest` definition (`runtime-1`) carries a
`runtime` plan with the same twelve-occurrence arithmetic contract, twelve
ordered source references, and a retained exact benchmark approval receipt.
`bind_runtime_manifest(plan, sources, approval)` validates it without provider
calls or ledger writes. Each source binds its canonical strict project root,
current input JSON path/raw SHA, logical occurrence and strict scope index.
The approval JSON must equal `{kind: "openart_paired_benchmark", plan, sources}`.
This capture supplements actual strict production scopes and credit approval;
it cannot create either authority. Active Auto-continue decisions and derived
or corrective scopes are rejected.

Every source is reread through actual preparation, source/reference byte
checks, real observed pre-submit creative qualification, retained raw quote
proof and `validate_credit_authorization`. The manifest profile freezes the
creative origin independently of later result promotion. Its semantic SHA is
the canonical digest of source packet occurrences with their generated
occurrence IDs removed; its reference SHA is the digest of each retained
reference's id, role, cast IDs and current byte SHA. The allowance observation
SHA binds the ordered actual account receipt records plus each approved
allowance ID and amount. All twelve originals must share one immutable ledger allowance ID. A missing compilation validator or preparation,
unqualified profile, stale quote or changed approval fails closed.

`summarize_runtime_metrics(manifest)` reads the existing private ledger with
`read_existing_snapshot`: it never initializes state, refreshes a provider,
settles billing or writes certification. Actual original reservations must
match the full `Binding`, immutable private dispatch and current ready journal,
frozen preparation and creative native request. Holes, substitutions, extra
motion journals and continuation after unresolved submission are rejected.
Prepared-but-unjournaled reservations are counted as pending with their private dispatch receipt and actual hold; ready and later states require the current ready journal. The report retains request path/raw SHA when present, original private snapshot SHA,
reservation binding and authoritative ledger debit evidence SHA. Gross debits, refunds and net debits are reported separately. Terminal slot release alone is labeled terminal-unselected; only the qualified original failure record establishes provider failure. Retained rejection predicates supply actionable categories after their schema/story/output binding validates; missing full review is review-availability.

Current selected outputs pass `validate_attempt_provenance` and current byte
checks. A semantic-only selection is not full AV evidence. With the current
repository's review contracts, accepted seconds conservatively require a
current eligible canonical v2 synchronized final AV review covering the
original scene. No final-review receipt means unknown AV acceptance and zero
accepted seconds. Actual source duration uses the existing final-review timing tolerance (0.05 seconds); accepted seconds are capped at the planned duration so encoding padding adds no credit. A qualified original outside that tolerance is retained with its exact observed duration, zero accepted seconds and technical-transport failure. Unavailable or unusable duration metadata on an unchanged original is similarly a technical source outcome, with observed duration unknown and the probe reason retained. Its debit or hold remains in the report and allowance arithmetic; remaining original occurrences may proceed within the existing approval. Byte/provenance tampering still fails closed. Runtime `credits_per_accepted_second` uses net debit after refunds, with gross cost reported separately. Certification separately requires the predeclared
deliverable's exact ordered original attempts, matching story, existing
canonical `artifacts/final_review.json`, `validate_final_review` and
`assert_final_eligible`. A selected clip never becomes a certified deliverable
by inference. Fixture reports still label `fixture_only` and report zero live
certifications. No live OpenArt qualification or provider result is claimed by
these offline software tests.

Runtime billing facts come from the actual ledger; unknown billing retains its
actual conservative quote hold (`reserved_units`), independently of job state.
This differs from the pure fixture contract's ceiling hold. Authoritative
settled charges are reported in full, even above quote or ceiling. Physical
quarantine remains the existing validated
`openart_dispatch.resolve_attempt` → `CreditLedger.settle` debit path; reporting
never manually changes SQLite or clears an existing quarantine. Unsupported
billing cannot be inferred from an account balance change and stays unknown.

`next_runtime_attempt(manifest, timeout=30)` is an explicit nonspending provider
refresh through the existing qualified credit API. It saves private fresh
capture receipts, then rereads the actual ledger claims, account quarantine,
all account holds, allowance debits/refunds/holds, immutable quantum and fresh
balance bounds. Remaining allowance must cover all untouched frozen ceilings;
account availability must cover the next exact quote. A terminal unknown debit
can continue only while its actual hold fits these bounds. A caller eligibility
boolean is never accepted. The returned next contiguous candidate is
`dispatch_authorized: false`: normal dispatch still performs its own atomic
reservation and final transport checks. Neither API submits a generation.

The runtime tests include a positive public twelve-source 5s/768p envelope, a pending prefix, terminal unknown-billing resume and a prepared-outbox crash report using an executable fake CLI and actual production, compiler, retained receipt and ledger mechanisms. A local FFmpeg/ffprobe test verifies container-padding tolerance. Their creative and semantic
facts are explicitly synthetic. Full live qualification, paid twelve-attempt
benchmark execution and a comparative provider conclusion remain gated and
unperformed.
