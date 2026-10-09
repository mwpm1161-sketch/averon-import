# M3D: controlled internal multi-provider orchestration

M3C is FINAL APPROVED at exact base
`91b827056bc8d8c3c6999ab7e6d168763a46f972`, on
`feature/manual-tender-xlsx-v1`. M0 through M3C contracts and legacy production
behavior remain unchanged.

## Explicit boundary and scope

`averon_import.services.manual_tenders.multi_provider_orchestration` provides:

- `capture_multi_provider_tender_snapshot(repository, tender_id, owner_id)`:
  server-only capture of a confirmed workspace into an immutable typed snapshot;
- `execute_multi_provider_tender_run(...)`: explicit synchronous internal call;
- immutable `MultiProviderTenderRunResult` with `run_id`, terminal `status`, the
  exact persisted `DurableTenderSourcingRunV2`, and an optional closed failure code.

There are no callers from main/API, existing search methods, runtime composition,
v1 lifecycle, UI or export. No automatic provider selection/expansion, feature
flag, background job, scheduler, polling, retry, reconciliation or provider probe
is introduced. An internal caller must supply the captured snapshot, immutable
`ProviderSelection`, selected IDs, explicit `SourcingSourceMode.PROVIDER_ONLY`,
the existing runtime `SourcingService`, and the workspace's run store.

Only provider-only runs are supported. Both `one_c_only` and
`one_c_then_provider` are rejected before understanding, runner execution or
lease acquisition. This isolates the provider-only v2 contract: history-safe
rows are never fabricated as provider evaluations or silently omitted from a
completed mixed run. Production 1C/history routing, SAFE zero-live decisions,
fallback semantics and history leases remain authoritative and unchanged.

Allowed runtime keys are ETM, Lemana and Local. The actual configured runner
still checks availability for the entire explicit selection before any adapter
execution. Unknown keys/absent runner fail closed; no provider is appended.
There are no duplicate clients, authentication/quarantine states, or VI support.

## Fences and lease

The snapshot captures resolved workspace path, tender ID, owner, source SHA,
revision and the same source/manifest integrity fields used by legacy manual
sourcing: rows, mapping, manifest, logical edge, future output columns, counts,
sheet and header. No provider payloads are admission inputs.

Before any live call, orchestration acquires the existing repository sourcing
lease, checks exact workspace path, calls `verify_sourcing_snapshot`, and compares
the captured fingerprint. Source bytes, owner, revision and source facts must
still agree. Mutation fails with a bounded code and zero runner calls.

Selected IDs must be a nonempty immutable tuple of distinct valid source IDs,
at most the existing 500 items. The existing row adapter checks membership and
item type against the verified metadata. Every selected physical Excel row is
validated before execution. Foreign/missing/section/duplicate IDs fail before
provider calls. Input ID ordering cannot reorder provider execution: rows execute
in physical source order. M3B canonicalizes persisted rows by source ID as before.

The existing activity registry prevents workspace deletion/cleanup while active.
Exactly one `finally` releases an acquired lease on success, evaluation failure,
build/storage failure and exception. No new lease implementation is introduced.

## Exact row chain

For each selected source row, sequentially:

1. `TenderSourcingRowAdapter` -> existing `service.understand_row` -> ProductIntent;
2. `service.execute_provider_selection` -> existing ProviderRunner;
3. `service.evaluate_provider_execution` -> ProviderMatchEvaluator;
4. `service.evaluate_provider_commercial_evidence` -> approved M2A evaluator;
5. `service.select_provider_commercial_winner` -> approved M2B selector;
6. `DurableRowIdentity` + `project_durable_provider_row_v2` -> complete durable row.

The exact runtime objects from these calls go to the projector. Orchestration
does not dump/reconstruct phase objects, compute decisions, resolve commercial
facts independently, or splice equivalent chains. M3B validation witnesses and
decision closure remain in effect.

One fresh `ProviderExecutionScope` belongs to each invocation. Its cache may
deduplicate equivalent retrieval inside that invocation; reused outcomes retain
request_count=0. Different rows have separate intents/evaluation chains, including
when their offer IDs coincide. Independent runs never share that execution scope
or commercial results. Existing understanding-cache semantics remain unchanged.

Runner execution, adapter retries, ETM quarantine, Lemana mirror/search and Local
zero-outbound accounting are unchanged. There is no row worker pool, provider
fanout, speculative check or retry around suppression.

## Lifecycle and errors

Orchestration allocates one secure server UUID hex at entry. A server clock owns
created/start time and terminal completion time; values require an offset and
completion cannot precede start. The optional clock dependency is for internal
tests, never a client timestamp. CREATED/STARTED share the captured start instant.

Active state stays in memory. No running-v2 or shadow-v1 file is created.

- Expected provider FAILURE/TIMEOUT/SUPPRESSED/EMPTY outcomes continue through
  all approved phases. ETM success + Lemana timeout can select ETM while retaining
  the timeout, counts and partial_failure. All failures can still produce a
  truthful completed evaluated row with no offers/winner. Approved partial_failure
  semantics are preserved (total failure does not masquerade as partial success).
- REVIEW/REJECT and identity ambiguity are preserved. An incomplete/invalid/
  noncomparable/tied strongest cohort produces NO_SAFE_WINNER; there is no cheaper
  weaker or best-available fallback and no market-completeness claim.
- COMPLETED requires every selected row to have a fully projected durable row.
- Unexpected row evaluation/projection corruption stops the loop. With at least
  one fully projected row, M3B may build a truthful FAILED run containing only
  those rows and all selected IDs, with the same UUID and terminal timestamp.
  The internal result reports `MULTI_PROVIDER_EVALUATION_FAILED`. With zero
  complete rows, no record is written and a bounded error is raised.
- Capacity errors are not converted into a smaller FAILED snapshot. A final
  builder error, invalid terminal clock, or invalid failed snapshot leaves no
  new record. No missing row is invented.
- M3C closed TOO_LARGE/ALREADY_EXISTS/METADATA_MISMATCH/STORAGE_FAILED errors
  propagate as closed storage errors, never success. Unexpected storage exceptions
  map to STORAGE_FAILED. There is no second write attempt or replacement UUID.

Custom orchestration errors expose only a closed code, safe UUID and fixed message;
raw exceptions, paths, credentials, payloads and supplier response bodies are
suppressed. There is no INTERRUPTED/cancellation/resume behavior added.

The sole build/write path is `DurableRunMetadata` ->
`build_durable_tender_sourcing_run_v2` -> `TenderSourcingRunStore.persist_durable_v2`.
M3B/M3C enforce 786,432 bytes and existing repository quotas. No row/candidate
dropping, truncation, sidecar, splitting, compression, v1 fallback or cap increase
is introduced. Existing M3C atomic no-clobber publication and pruning apply.
Production avoids redundant read-back; E2E tests verify exact stored bytes and
correlated `read_durable_v2` DTOs.

A process crash before terminal persistence can leave **no run record**. Restart
must not invent a completed/failed record from absent state. A terminal file
successfully published by M3C remains a terminal v2 record. Resume is a later phase.

## Proof and test scope

The focused module uses real confirmed XLSX workspaces, real ProviderRunner,
outcome-native fake adapters returning ProviderSearchOutcome/Offer, and the
unchanged matcher, commercial resolvers, selector, M3B and M3C. Supplier network
connections are forbidden; local socketpair is allowed only for Windows asyncio
in-process public-route tests.

Approved Lemana evidence remains INCOMPLETE with unknown VAT. Consequently
cross-provider ETM+Lemana tests exercise incomplete/invalid cohorts, partial/all
failures and the same offer_id=123 under distinct composite references. Unique
lowest-price, exact Decimal tie and noncomparability tests use two equally strong
COMPLETE ETM offers under an ETM+Lemana selection with Lemana EMPTY. No fake
resolver changes the approved supplier trust registry. The weaker-cheap test
uses the real matcher: ETM explicit model MATCH at 1000 versus Lemana title-only
LIKELY_MATCH at 1.

E2E tests cover source ordering, exact object identity at projection, one terminal
write, all rows for COMPLETED, partial FAILED, zero-row/corrupt-build no-write,
same ID conflicts, bounded storage errors, snapshot mutation, selected-row gates,
lease busy/release once, no v1 placeholder, Local zero counts, ETM suppression,
reused zero counts, independent runs, per-row offer isolation and byte determinism.
Public v1 provider/history route tests monkeypatch M3D to raise and still pass.

The representative 370x2 run retains all 740 decision offers and stores
**727,847 bytes**. The larger representative 500x2 input explicitly fails the
unchanged M3B budget after retrieval, without a fallback file or lost rows.

API/request/response schema, app.js/styles, history behavior, TenderPriceResolver,
XLSX export and v1 lifecycle/recovery are unchanged. No deploy, merge or tag.

## Completed gates

- M3D: **53 passing cases** in the final focused, regression and full runs.
- Focused provider contracts/capabilities/runner/adapters/multi-runtime/matching/
  commercial/selection and durable read/projection/storage through M3D:
  **1,001 passed**, short system-Temp `mdf84e6b` basetemp.
- Broad manual tender/D4/1C/history confirmation/provider/export/warehouse/XLSX/
  repository/recovery/pruning regression: **1,733 passed, 1 skipped**, short
  system-Temp `mdc4fbc6` basetemp.
- Full `tests/`: **2,636 passed, 3 skipped, 3 accepted baseline failures**, short
  system-Temp `md8194b2` basetemp. JUnit inspection confirms no other failures
  or setup errors. Heavy suites ran sequentially with separate AVERON_DATA_DIR.
- **8/8 JS lifecycle scripts passed**, each with app.js/styles.css arguments:
  Excel Tender run pinning, review navigation, manual history decisions/search,
  manual tender lifecycle, 1C history lifecycle, recent tender lifecycle and
  sourcing modal lifecycle.
- `compileall`, `node --check averon_import/static/app.js` and Git diff checks
  passed. Production callsite search finds only the two new function definitions.
- Git comparison against the exact approved base confirms all previously
  tracked production files, including API/UI, sourcing runtime/decisions,
  M3B/M3C, history, repository, v1 lifecycle and export, remain unchanged.

The only full-suite failures are the explicitly accepted unavailable-Tesseract
environment baseline in `tests/test_processing_coordinator.py`:

- `test_local_profile_resolves_tesseract_adapter`
- `test_settings_processing_mode_influences_resolver`
- `test_local_mode_rejects_cloud_llm_combination`

The sole regression/full warning is the intentional duplicate ZIP member in
the XLSX preflight rejection fixture. No supplier traffic or production workspace
was used. Temporary workspaces belong exclusively to these synthetic test runs.
