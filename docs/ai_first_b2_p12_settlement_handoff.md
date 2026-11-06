# B2 P1.2 — handoff contract for Luna 6 (not implemented)

This document specifies a separate future change. The P1.1 patch does not
implement historical settlement, weaken validate_dispatch(), or change B1
financial mutation rules/schema v1. No B3–B6 API, migration or publication work
is authorized by this contract.

## Required separation

Historical settlement records the cost/outcome of an ALREADY authorized paid
attempt. It must remain possible after a new document generation, binding or
policy revision becomes active. It must not create a new reservation/permit,
revalidate today's admission budget as a condition of recording yesterday's
expense, or publish an old result into the current draft.

Keep three separate operations:

1. Validate immutable historical evidence and replay its exact Outcome into B1.
2. Independently decide whether that result can restore its current private
   checkpoint. Failure/ineligibility here cannot undo or hide a successful
   financial receipt.
3. Authorize a new POST using the existing strict fresh-permit path. Historical
   evidence and financial receipts never authorize this operation.

The existing evidence_only=True branch is unsuitable for (1): it still requires
an active run/generation, active binding and current policy revisions. Do NOT fix
this by weakening the checks used by consume()/new POST.

## Narrow entry point and inputs

Add a no-HTTP internal operation, conceptually:

    settle_saved_evidence(attempt_id, historical_context) -> B1 settlement receipt

It operates under the CURRENT valid dispatcher OS lock/lease/epoch and verified
existing ledger. It does not require an attached live client, current credential,
PDF file, active historical run, or opening a checkpoint with a new credential's
private fingerprint. It may be invoked before current-run admission/registration.
No automatic scan/queue/admin route is required: explicit attempt IDs suffice.

HistoricalSettlementContext must contain the original Run DTO and original
nonsecret OrchestrationConfig snapshot. Validate both against committed history:

- original Run equals runs.body_json, including tenant/document, generation,
  source digest, config digest, budget and request cap;
- configuration_digest(original_config, historical_binding) equals that Run's
  config_digest;
- the original pricing fingerprint equals evidence.proof.pricing_sha256.

Use the original config only as a verified preimage, never because it happens to
be today's config. Recompute cost with existing exact usage_units(original_pricing,
saved_usage); it must equal saved Outcome.actual_units. Use the original pricing
profile even when today's profile ID is unchanged but rates differ. No old API
key is necessary: its historical credential tag/binding metadata already exists.

Freeze/canonically copy the context at entry and validate/recompute from that
same snapshot. A frozen dataclass holding a mutable live pricing/config object
is insufficient; concurrent settings changes must not alter the checked facts.

**v1 limitation:** current evidence contains a pricing hash, not the pricing/config
preimage. A hash cannot reconstruct original rates. If no matching original
snapshot is available, fail closed with a safe historical_context_required error,
retain the hold/stop, and do not substitute current pricing or invent a snapshot.
Unconditional autonomous recovery of such v1 artifacts is not promised. A future
self-contained artifact format would need separate approval; do not implement
it, rewrite v1 evidence or add SQLite columns as an implicit workaround.

## Mandatory historical checks

Read/validate ResultEvidence from its dedicated private root using the existing
canonical attempt path, ACL/root identity, size/version/shape/duplicate-key and
payload-hash checks. Do not accept arbitrary uploaded JSON or a caller-chosen
filesystem path. Then verify against immutable original ledger facts:

| Fact | Required check |
| --- | --- |
| Ledger/actor | Same installation/application/schema digest and integrity checks; current owner PID/nonce/epoch/lease/clock/OS lock. A send-admission stop is not permission to bypass those checks. |
| Attempt | Exact canonical Attempt equals attempts.body_json and matching denormalized identities; run/page/attempt_no/claim_seq/request_digest/reserved tokens/money unchanged. |
| Original epoch | Artifact owner_epoch equals the stored attempt/reserve-operation epoch; it may differ from the current recovery owner epoch. |
| Reserve receipt | operations row exists for the saved reserve operation, kind/payload digest/request JSON/result identities match the original attempt and epoch. Do not call reserve() to obtain it. |
| Run | Original runs.body_json matches historical context. Source identity is the committed hash/DTO, not a requirement that an old PDF still exists. |
| Binding | Exact immutable credential_bindings body/digest at the ORIGINAL revision and its binding_quotas edges match the saved Attempt. Inactive historical bindings are valid facts; no current secret lookup. |
| Quota reservation | Exact attempt_quota_reservations edges, frozen policy revision/window/tokens/policy_json match the original attempt. Compare the historical snapshot, not today's limits. |
| Result/proof | Typed AdapterPageResult, source_document_id from original Run.source_digest, correct page; canonical result hash, exact saved usage, pricing fingerprint, proof attempt/state and proof hash match. |
| Outcome | Existing B2 deterministic settlement operation ID, attempt/request IDs, evidence_digest and terminal state match the artifact. Amount is strict int64 or None, never FLOAT. |
| Cost | Exact original pricing/config preimage checks above and usage_units equality. Missing price-sensitive usage stays None/full hold; HTTP status/timeout never establishes zero. |

No check may silently remap a historical attempt to a new billing scope, new
quota scopes, new credential generation, current request body or current run.
Do not re-render a PDF or make a provider request to reconstruct missing facts.
Corruption, missing original rows/context, SQLite busy/write error or ambiguous
commit must preserve conservative accounting and return a safe failure.

## Checks belonging to new dispatch, not historical finance

Keep validate_dispatch() strict for fresh POST authorization:

- active document/run/generation and exact registered Run;
- active binding at the approved revision plus current credential/target checks;
- current billing/quota revisions and admission stops/caps;
- fresh reserve receipt, exact request body/endpoint/model/project/headers and
  quota scope set;
- process/client/caller/thread-bound one-shot permit and current original
  attempt owner epoch.

Historical finance requires the current recovery owner, but compares ORIGINAL
attempt epoch/binding/policy snapshots to their historical records. It must not
require the original epoch to be current, old binding active, original run still
selected, or old limits equal to today's limits. Revised ceilings cannot erase,
clamp or defer an actual paid expense. B1's later admission checks remain binding.

## Mutation, idempotency and immutable v1 limits

Use unchanged B1 record_outcome() and its transaction/receipt semantics as the
only financial mutation authority. A separate short owner-validated read can
check immutable history first; state, owner and operation replay must still be
checked atomically by record_outcome() at mutation. Immutable run/binding/attempt
metadata cannot change between those transactions; concurrent outcome writers
are resolved by B1 idempotency/conflict rules. Never hold SQLite across file I/O.

- intent/unknown plus valid saved terminal facts: record the exact saved Outcome.
- exact settlement operation replay: return its prior receipt, one expense only.
- terminal/not_sent with conflicting facts or another amount: reject; do not
  overwrite the terminal record or allocate a different operation ID to evade it.
- terminal without usage cannot be refined in v1. That requires B6/schema v2.
- actual overrun remains recorded and stops admission under existing B1 rules.
- needs_reconciliation stops remain set even after valid terminal recovery.
  No B6 reconciliation exit/release API, opening-balance reset or backup restore.

Financial replay returns a receipt, never DispatchPermit/Reservation(new=True).
It does not change attempt identity/claim sequence, trigger checkpoint callbacks,
or set a historical attempt pending for another POST. Historical admission stops
must not prevent recording valid paid facts; they still prevent all new sends.

## Publication/checkpoint boundary (without implementing B4)

Persist financial settlement before any optional current-checkpoint projection.
Keep the existing current-run/source/config/checkpoint-identity fences for that
projection; stale generation or credential/policy incompatibility may cause it
to be skipped/rejected after finance succeeds. Returning a financial receipt must
not imply that a current draft was updated. Preserve the immutable artifact.

No Human Review/editor/XLSX/draft publication is added. Do not restore old OCR
over a newer run or over user decisions. Full draft CAS/merge belongs to B4.
It is acceptable to keep the existing conservative publication guard unchanged
for P1.2; it is not acceptable to use its rejection to roll back financial replay.

P1.1's checkpoint_attempts() is intentionally CURRENT-run fenced for dispatch
preparation recovery. It must NOT be the historical settlement validator. Likewise,
PageOrchestrator's source/credential/checkpoint opening must not be a prerequisite
of the independent financial entry point. Merely changing evidence_only=True
inside _recover_result() would leave those earlier blockers intact.

## Exact implementation ownership for Luna

| File/function | Authorized P1.2 change |
| --- | --- |
| coordination.py | Add a narrowly named owner-validated immutable historical-evidence/metadata predicate. Reuse _transaction/_connection. Do not weaken validate_dispatch/checkpoint_attempts or change reserve/record_outcome financial rules. |
| durable_dispatch.py / ResultEvidence | Add the permitless settle_saved_evidence entry point and historical context validation. Reuse read(), exact cost helpers and B1 receipts. Do not alter P1.1 reserve -> checkpoint -> consume -> POST ordering. No artifact format change without separate approval. |
| durable_qwen_adapter.py / _recover_result(), start() | Separate historical validation/financial replay from current checkpoint projection; delegate finance to the common entry point. Allow explicit financial replay before current-run checks. Do not reactivate an old generation or require an old API key just to settle. |
| tests/test_ai_first_b2_p12.py | New fake-only historical recovery tests listed below; reuse actual worker after_evidence and P1.1 crash tests. |

Keep P1.1 no-intent evidence rules and preparation history intact. Do not repurpose
its negative-history read as a finance reset or as authorization. No UI/API,
scheduler, producer retry, automatic bootstrap, SQL migration or B6 implementation.

## Acceptance tests

Use a real subprocess: execute fake HTTP, fsync immutable result, pause at
after_evidence before record_outcome, kill, reopen the same ledger. Under the
new owner change state BEFORE financial recovery, independently and combined:

1. Register generation+1 as active. Original attempt settles at its original
   cost; original result stays archived and does not overwrite the new checkpoint.
2. Rotate credential/binding (old revision inactive; old key removed). Settle
   using old immutable binding metadata and original nonsecret config, not a
   client authenticated with the old key. Zero transport calls in recovery.
3. Revise quota/billing policy, including a tighter ceiling below the past cost.
   Original reserved scopes/price remain authoritative; preserve actual cost
   and existing stops. New admission still obeys the tighter policy.
4. Change current pricing. Correct original snapshot settles the original units;
   current/wrong or absent snapshot rejects without releasing hold/stop.
5. Replay twice and race two recovery callers: one immutable operation/expense.
6. Terminal no-usage/conflicting outcome/not_sent and tampered hashes/identities/
   epoch/reserve operation/quota snapshot reject without history edits.
7. SQL write failure/lost commit receipt: subsequent exact replay converges;
   SQLite unavailable never triggers legacy accounting or a POST.
8. Assert immutable evidence bytes and all old/new review/raw/editor/XLSX bytes
   unchanged; no source file/credential is required to account for a valid fact.
9. Run all B2 and P1.1 regressions, then combined full Python/JS profiles against
   the same baseline/environment. VPS-specific Linux ACL/fsync remains a separate
   release gate; do not substitute mocks or claim a gate that was not run.

No commit, push, merge, SSH, deploy, paid request or concurrency increase. Stop
for independent review after the separate P1.2 implementation is authorized.
