# B2 — Durable Dispatch Integration

Review base: `413dce7b5b5a02230e96e743ee38e8795c30259a`.
Branch: `codex/qwen-durable-dispatch-b2`. Report date: 6 November 2026.
No commit, push, merge, SSH, deploy, production change or real provider request.
The existing production composition remains sequential with `concurrency=1`.

## Boundary and scope

B2 adds an explicitly selected library path through the existing PageOrchestrator
and QwenVisionClient. It does not register a route, scheduler, startup hook or
production factory. B1 schema v1, its fingerprint, terminal immutability and
reconciliation stops are unchanged. B6 still requires an explicit schema v2
migration; B2 does not implement it.

Authorization is at most once per attempt, not a remote delivery guarantee.
A crash can leave zero POSTs and a retained unknown hold. No provider-side
idempotency is assumed; an unknown outcome never authorizes a retry.

The opt-in requires BOTH `AVERON_AI_DURABLE_DISPATCH=1` in the dispatcher process
and `enabled=True` when opening DurableDispatcher. The environment flag alone
does not select the new production path. Instead, it excludes legacy provider
POST paths from that process. Activation is process-sticky: unsetting the flag
cannot revive a legacy POST after durable mode has been observed. Invalid flag
values fail closed. Normal mode, with the flag unset, retains existing behavior.

DurableDispatcher.open() is the cancellation-safe async factory. Its synchronous
constructor is for synchronous callers; never abandon a bare asyncio.to_thread
constructor on cancellation. Async close drains active calls and settlements
before releasing the OS lock; the heartbeat remains active while it drains.
Neither factory creates a missing ledger or evidence directory. The ledger must
already exist with B1 bootstrap attestations, opening liabilities and approved
bindings. Evidence uses a separately provisioned private directory outside the
repository, containing only canonical attempt JSON/pending files. Ledger and
evidence roots cannot overlap.

Composition is explicit:

```python
async with await DurableDispatcher.open(
    existing_ledger_root, installation_id=approved_installation_id,
    evidence_root=existing_private_evidence_root, enabled=True,
) as dispatcher:
    client = QwenVisionClient(approved_config, dispatch_boundary=dispatcher)
    adapter = DurableQwenVisionAdapter(
        client, dispatcher=dispatcher, dispatch_binding=approved_dispatch_binding,
        **existing_adapter_settings,
    )
    report = await PageOrchestrator(
        adapter, renderer, config, checkpoint, durable_dispatch=adapter,
    ).run(pdf_path)
```

The example assumes operator-approved objects; it is not bootstrap/enablement
tooling. Run UUID, tenant/document, generation, source hash, immutable config
digest, budget, request cap, credential binding and policy revisions are explicit
contracts. The durable adapter has a distinct public descriptor fingerprint, so
it cannot silently open a legacy checkpoint. The checkpoint identity/source
inspection remains mandatory. Request run/source/image-page identity is checked
again inside the adapter. Current document generation is checked before send
and before returning a successful late result.

## B0 provider-path map and exclusions

| Paid/account path in this checkout | Durable-process handling |
| --- | --- |
| AI-first QwenVisionAdapter -> QwenVisionClient; sequential live factory; QwenVisionProvider; pilot/controlled-pilot tools using that client | Only a client attached to DurableDispatcher with its authorized adapter call context can delegate a POST. All legacy clients fail before injected or standard transport invocation. |
| ai/provider.py OpenAICompatibleProvider: AI processing/validation, provider diagnostics, sourcing ProductUnderstanding/ranking | Explicitly excluded before urllib invocation and before its auth loop can send. Existing Bearer/Api-Key fallback remains available only in normal mode. |
| YandexVisionProvider._request_with_retry, including injected HTTP client | All submission POSTs are excluded in durable mode. Its separate OCR credential/account is not assumed financially independent without B0 account evidence. Status GETs do not create OCR submissions. |
| Local OCR, PDF rendering and deterministic processing | No AI paid POST and no durable financial authority; implementation unchanged. The generic legacy AI provider path is conservatively excluded even for local models inside the dedicated durable process. |

The guarded Qwen boundary is before delegation to client._transport.post, not
inside _HttpsTransport alone. Tests use actual QwenVisionClient serialization and
injected transports with real B1 transactions. There is no auth/schema retry on
the durable path. Repeated body/schema errors do not buy another POST.

This is a guard for current code in explicitly configured durable processes.
It cannot stop an older binary, an unconfigured separate legacy process or
arbitrary Python code opening its own socket. All participating processes must
use the same ledger/account mapping and opt-in; live coexistence with a legacy
runtime requires B0/B5's independently verified paid-deny/OS egress mechanism.
Do not infer that gate from the B2 two-process tests.

Custom transports are trusted implementations of the existing single-post
interface. B2 guarantees at most one delegation for an attempt, including an
injected transport. It does not sandbox arbitrary transport code that internally
opens multiple sockets/retries. Production uses the existing single-request
_HttpsTransport, which has no redirect/auth/retry fallback. A future custom live
transport needs separate review; arbitrary plugin execution is not an authority
boundary.

## Authorization and accounting contract

1. PageOrchestrator retains its existing bounded page queue, PAGE_CAPACITY,
   independent PDF worker and HTTP executor/capacity (maximum ten). Its durable
   branch does not instantiate _BudgetLedger or _RateGate and does not acquire
   QwenVisionQuotaRegistry permits. Local checkpoint counters are projections,
   not an additional account authority. Three end-to-end tests prohibit those
   local limiter constructors while processing 2/4/10 actual fake HTTP calls.
2. Exact serialized body SHA-256, method, full Run digest, target digest and binding fingerprint
   determine request identity. Target includes exact endpoint, model, project
   and auth scheme. Credential HMAC is domain/generation-bound; the actual
   Authorization and project headers are compared at the boundary, never
   persisted. No request image, prompt or credential is written to the ledger.
3. B2 permits one attempt per page/run: deterministic UUIDv5 run/page/attempt-1
   and reserve operation identity; attempt_no=claim_seq=1. It never manufactures
   another identity after an ambiguous attempt. Durable retry_pages is rejected
   before checkpoint mutation. An explicitly approved new run/generation is
   required for another attempt; global account history survives it. Legacy
   explicit retry behavior is unchanged.
4. The real B1 reserve commits intent plus all account/quota holds atomically.
   Only newly_created=True can mint DispatchPermit. Replay returns no permit.
   Ambiguous/lost commit receipt marks the dispatcher unusable and never creates
   a capability, even if the row actually committed.
5. Permit is process-bound, unpickleable, bound to exact client/caller context,
   worker thread, owner epoch and complete Attempt. Consumption is locked and
   burns it before revalidation. B1 validates exact immutable attempt metadata,
   full registered Run metadata, active binding, policy revisions/stops and current document generation in a
   short transaction. SQLite error or changed metadata cannot fall back to local
   authorization. The transaction is closed before HTTP.
6. The dispatcher owns its OS lock through every registered call, thread drain,
   result fsync and settlement. It cannot hand ownership to a second process
   merely because an HTTP request outlasts a lease. Heartbeat failure denies
   new delegation. The lease/epoch is B1's existing authority, not another lock
   or TTL-stealing mechanism.
7. Financial calculations are exact integer/rational arithmetic, rounded upward
   once to nano-RUB, independently of Decimal context. Persisted amounts remain
   int64; aggregation remains B1 Python integers. Rate representation is bounded
   to 4096 digits and exponent magnitude 4096 to bound arithmetic work. Excess
   representation rejects; rates and budget values are never replaced/clamped.
   This avoids losing a fractional tail before rub_units() can round it. Frozen
   pricing fingerprints reject mutation; checkpoint amounts use the same fixed
   nano-RUB projection. Reservation uses the more expensive configured input/cache
   tier; missing price-sensitive cached usage retains the full hold. Normal-mode
   TokenPricing behavior is unchanged.

## Outcome, cancellation and crash contract

| Observed boundary | Durable result |
| --- | --- |
| Cancel/validation failure before confirmed reservation | No transport and no financial intent created by that call. |
| Confirmed current-owner intent, proven no delegation | not_sent with an immutable operation receipt; no expense inferred. The already-used operation cannot mint another permit. |
| Reserve commit succeeds but receipt is lost | No permit/POST; committed intent remains held. Restart makes it unknown and stops its scopes. No not_sent guess. |
| Timeout, transport exception, cancellation during delegated call, killed process | unknown; full hold and reconciliation stop. No automatic retry. |
| HTTP 5xx | Conservatively unknown even if the server reports usage. |
| Complete HTTP 1xx–4xx without complete validated usage | Terminal fact may be recorded, but cost stays reserved. HTTP error does not establish zero cost. |
| Complete validated usage on a known terminal result/error | Exact nano-RUB settlement, including explicit zero usage if the provider actually supplied it. Overrun is preserved and B1 blocks admission. |
| Validated page result | Immutable private evidence is fsynced before SQL settlement and before returning success. |
| New generation while HTTP is in flight | Preserve immutable evidence and financial settlement; reject returning the old page as current success. |

Cancellation drains already submitted SQLite/fsync work and the existing bounded
HTTP thread. Cancellation of async open closes a newly acquired owner before it
propagates. Cancellation of async close does not release the lock early. A
successful completed response is retained when cancellation arrives during its
artifact/settlement. Unknown/failed neighbors do not discard other successful
pages. None of these operations publish into Human Review or editor data.

## Immutable result evidence and recovery

Path is `<evidence_root>/<attempt_id>.json`; temporary pending path is the same
attempt identity with `.pending`. Exclusive creation, private ACL before writing,
file fsync, no-overwrite hard-link installation and POSIX directory fsync precede
settlement. A partial pending file is not trusted or deleted to justify a retry.
It is bounded diagnostic/reconciliation evidence, not another executable queue.

Versioned JSON contains the exact Attempt DTO, original owner epoch, validated
AdapterPageResult, Outcome and proof metadata. A canonical payload hash detects
corruption; Outcome.evidence_digest binds the result hash, usage, pricing digest
and response fact hash. Reads reject duplicate JSON keys, unknown shape/version,
oversize content, wrong ACL/replaced root, hash mismatch and identity mismatch.
Only typed, validated page results are stored; transport headers/keys/images and
request bodies are absent. OCR text remains private immutable machine evidence.

On resume, normal source/checkpoint validation runs first. Recovery verifies the
artifact and corresponding original B1 attempt, active generation/binding and
fixed configuration before replaying its exact outcome operation. This also
finishes settlement after a crash between result fsync and SQL commit. It repairs
only that run's independent page checkpoint, then skips the page without a POST.
Completed checkpoint extraction must match evidence; it is never overwritten on
mismatch. A recovered unknown may become terminal on evidence, but the B1 stop
remains set. Missing/invalid evidence never grants another send.

The SQLite schema/fingerprint is unchanged, and immutable B1 operations retain
the reserve and settlement history. No old financial backup is restored. This
is neither production migration tooling nor a general administrative import API.

## Reproducible tests and regression comparison

Use the same project Python environment as baseline. Basetemp and synthetic data
must be outside the checkout because B1 refuses repository-contained ledgers.
All fixtures use synthetic credentials/documents and fake responses. Test and
worker socket guards reject external connections; standard live transport is
explicitly forbidden in B2 tests. Windows asyncio's loopback self-pipe is allowed.

```text
python tmp/phase-b2/offline_validate.py <checkout> tests/test_ai_first_durable_dispatch.py -q --basetemp=<private-temp-outside-checkout>
python tmp/phase-b2/offline_validate.py <checkout> tests -q -m "not integration" --basetemp=<separate-private-temp>
node tmp/phase-b2/js_validate.cjs <checkout> <results-json>
```

The ZIP's artifacts directory mirrors ignored tmp/phase-b2. In another checkout,
use the extracted wrapper/helper paths instead; no wrapper installation is
required for the standalone pytest B2 module and its worker.

The subprocess worker has explicit modes: hold, before_post, during_http,
after_evidence and busy. Tests wait for its JSON window marker, kill that actual
process, reopen the same SQLite ledger and assert POST counts/holds/results.
The lost-commit-receipt test uses a real sqlite3.Connection subclass: commit is
performed before its receipt exception. Readonly and busy errors use actual
SQLite query_only and an independent BEGIN IMMEDIATE holder. Reserve is not a
mock authorization oracle.

All following test names are in test_ai_first_durable_dispatch.py:

| Guarantee / window | Reproducible test |
| --- | --- |
| Real orchestrator, SQLite intent before custom delegation, 2/4/10 overlapping calls, no local financial limiter | test_actual_orchestrator_boundary_and_single_financial_authority |
| Two independent processes, only one OS owner, successor epoch/history | test_two_processes_only_one_real_dispatcher_and_financial_authority |
| Concurrent duplicate operation/attempt, one POST | test_simultaneous_duplicate_operation_and_attempt_send_once |
| Burned permit; client/thread/claim/binding/quota/run mismatch | test_one_permit_cannot_be_consumed_twice; test_forged_permit_metadata_is_rejected_before_transport; test_borrowed_permit_is_rejected_from_another_thread |
| Exact body/model/project/credential/endpoint/caller/epoch/generation | test_mismatch_at_real_client_boundary_never_calls_custom_transport |
| Source/image/run identity and caller activation | test_document_image_identity_mismatch_is_denied_before_intent; test_unregistered_caller_cannot_impersonate_existing_run_metadata |
| Kill before POST / during HTTP; restart remains unknown without resend | test_real_process_death_never_reissues_paid_post |
| Kill after result fsync; recover result/settlement, keep B6 stop | test_real_death_after_result_fsync_recovers_settlement_and_page_without_post |
| Actual commit receipt lost; real SQLite busy/readonly | test_lost_real_sqlite_commit_receipt_never_authorizes_post; test_real_sqlite_busy_process_never_calls_transport; test_actual_sqlite_readonly_failure_has_no_local_fallback |
| Cancel before/after dispatch, during reserve, after result fsync | test_cancel_before_boundary_is_proven_not_sent; test_cancel_after_delegation_drains_and_retains_unknown; test_cancel_during_slow_reservation_drains_commit_and_proves_no_post; test_cancel_after_result_fsync_preserves_success_and_settlement |
| Shared RPM/TPM/account budget at 2/4/10 | test_real_quota_and_budget_admission_for_fake_bursts |
| Exact cost, fractional tails, cache maximum, incomplete usage | test_exact_nano_rub_cost_is_independent_of_decimal_context_and_tail; test_reservation_covers_configured_cache_tier_even_when_more_expensive; test_missing_price_sensitive_cached_usage_keeps_reservation |
| Draining close/open; real heartbeat beyond original deadline | test_async_close_waits_for_inflight_http_and_settlement; test_cancelled_async_open_closes_the_new_owner_instead_of_leaking_it; test_real_heartbeat_keeps_owner_valid_beyond_initial_http_lease |
| Neighbor timeout and checkpoint failure do not lose successful page | test_unknown_page_does_not_lose_other_successful_page; test_checkpoint_failure_recovers_success_without_reposting |
| Fsync failure/corrupt evidence fail closed; crop recovery idempotent | test_result_fsync_failure_retains_liability_and_closes_resources; test_corrupt_immutable_result_evidence_blocks_resume_without_post; test_region_result_recovery_is_idempotent |
| Late generation preserves cost/evidence but does not return stale success | test_superseded_generation_cannot_return_success_but_keeps_cost_and_evidence |
| Legacy auth fallback and injected OCR exclusion | test_legacy_ai_auth_fallback_is_excluded_before_urlopen; test_yandex_ocr_injected_path_is_excluded_in_durable_process |
| Adjacent synthetic review/raw/editor/XLSX snapshots | test_human_review_raw_observations_and_exports_are_unchanged, plus the unchanged existing domain/UI/export regression suites |

| Profile | Result |
| --- | --- |
| Full Python baseline 413dce7 | 3694 passed, 5 failed, 3 skipped, 1 deselected; 605.58 s. |
| Existing B1/Phase A/Qwen initial compatibility profile | 294 passed, 1 Linux-only skipped; 53.51 s. |
| Intermediate expanded B2 core | 58 passed; 30.04 s. |
| B2+B1+Phase A/Qwen before final exact-money edge cases | 353 passed, 1 skipped; 81.90 s. |
| Additional success-preservation/checkpoint/crop recovery | 3 passed, 59 deselected; 7.96 s. |
| Final focused B2+B1/Phase A/Qwen | 364 passed, 1 Linux-only skipped; 81.02 s. |
| Additional real short-lease heartbeat | 1 passed, 70 deselected; 3.90 s. Fake HTTP completes beyond the initial lease deadline. |
| Final B2 core (within full profile) | 71 passed, no failures or skips. |
| Full final Python | 3765 passed, 5 failed, 3 skipped, 1 deselected; 604.95 s. Failure IDs exactly match baseline. |
| Full JS baseline/final | Both 23 passed, 1 authenticated full-app fixture skipped; same Node/Edge environment. |

Five baseline failures are two CSS overflow assertions (one_c_history_sourcing_ui
and product_ui) and three unavailable-Tesseract resolver cases in
processing_coordinator. Final acceptance requires exactly the same failure IDs,
not just the same count. One integration test requiring a real engineering PDF
is deselected in both profiles; existing private XLSX/cache fixtures and the
Linux-only fork test retain genuine skips. Authenticated full-app JS fixture is
not available. Logs/JUnit, comparison JSON, patch hashes and a reproducible
packaging helper belong to the ignored review bundle, not the source patch.
The pre-exact-money full run and a later cancelled intermediate run are retained
as diagnostics; they are not presented as the final financial revision's profile.

Windows path-length reproduction: a full run with a longer basetemp produced 16
extra FileNotFoundError cases in unchanged manual-tender history temp files. All
16 were reproduced on base 413dce7 with the same long basetemp (262-character
file paths). The same 16 passed on BOTH baseline and B2 with shorter roots. The
final full run uses fresh data/basetemp names of exactly the baseline lengths.
No XLSX/history code or Windows settings were changed. Raw long-path failures,
three targeted reproduction profiles and path-length-comparison.json are kept
in the bundle so these failures are explained rather than hidden.

## Remaining release gates

- Run updated B2/B1 tests on real Linux, then target-specific VPS POSIX ACL,
  directory fsync/hard-link/storage/power-loss checks. Local Windows process kills
  and file fsync do not prove power-loss durability. Previously reviewed Linux
  B1 tests do not substitute for the new B2/VPS gate.
- Complete B0 remote billing/quota scope, credential/project/model-limit and
  tariff evidence. Verify every participating process's ledger/configuration and
  B5 old/unconfigured-runtime paid-deny/egress isolation before any live pilot.
- B3 authenticated scheduler/API/fairness and operational ownership/lifetime
  composition are separate. B2 adds no universal queue or endpoint.
- B4 atomic OCR draft publication CAS/tenant lifecycle/Human Review merge remain
  separate. B2's immutable evidence and private checkpoint repair do not replace
  publication fencing. Existing review/raw/editor/XLSX bytes are not modified.
- B5 production bootstrap/import/rollback tooling remains separate; never roll
  financial history back with code. B6 audited reconciliation and schema v2
  migration remain separate; no B2 API clears stops or refines sealed v1 costs.
- Only after those approvals may controlled live pilots at 2, then 4, then 10
  pages be considered. B2's concurrency tests are fake requests, not a live
  concurrency or production readiness claim. Current live concurrency stays 1.

Stop after review artifacts. Do not create a commit before independent review.
