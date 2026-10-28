# AI-first multiworker design proposal

The current live Qwen runtime intentionally uses `concurrency=1`. The current
RPM/TPM limiter, mutation locks, and job coordination are process-local. Raising
the number of workers or running another app process today would create
uncoordinated reservations, duplicate billing risk, lost updates, and unfair
provider usage. Keep concurrency at one until the shared coordination layer is
implemented and validated.

## Target components

1. **Durable job/page queue.** Persist a document job and one task per page in
   SQLite (or another transaction-capable shared store). Enforce unique
   `(document_id, run_id, page_number, extraction_fingerprint)` and explicit
   states including `queued`, `leased`, `completed`, `failed`, and
   `unknown_outcome`.
2. **Leases and fencing.** Workers acquire expiring leases with monotonically
   increasing fencing tokens. A late worker may store diagnostics, but cannot
   publish a page result after its lease or extraction revision is stale.
3. **Provider-global quota and budget reservations.** Atomically reserve RPM,
   TPM, and the maximum request cost before dispatch in shared storage. Commit
   actual usage on response; conservatively retain or explicitly release
   reservations when safe. Scope limits by provider credential fingerprint
   without persisting the credential itself.
4. **Single dispatch boundary.** Record a dispatch intent before sending. A
   process crash after dispatch is treated as `unknown_outcome`; it is never
   retried automatically. An operator can explicitly authorize a new attempt.
5. **Per-page checkpointing.** Persist page status, safe diagnostics, measured
   latency, and returned usage independently. Publish a completed extraction
   only when the provider response, schema validation, page identity, and source
   PDF integrity checks pass.
6. **Fair scheduling and bounded resources.** Use per-user queues with weighted
   fair scheduling, global worker caps, bounded rendered-image memory, and a
   render semaphore distinct from provider request slots. Cancel queued work
   immediately; in-flight cancellation must retain unknown-outcome semantics.
7. **Observability.** Emit IDs, safe error codes, timings, token counts, and
   quota reservation outcomes. Do not log PDF bytes, Base64, prompts containing
   user context, OCR text, API request bodies, or secrets.

## Delivery stages

- **M1 — State model:** durable job/page schema, migrations, state transitions,
  fencing tokens, and restart recovery tests. No parallel live calls.
- **M2 — Shared limiter:** transactional RPM/TPM/budget reservation across
  processes; race, clock-boundary, restart, and failed-dispatch tests.
- **M3 — Queue workers:** deterministic fake adapter with multiple documents
  and users, cancellation, fairness, bounded memory, and stale-lease tests.
- **M4 — Controlled provider pilot:** operator-gated small concurrency with
  explicit quota confirmation/risk acceptance, cost ceiling, no automatic retry,
  and audited unknown outcomes.
- **M5 — Scale validation:** load/fault tests and operational dashboards before
  considering a production worker increase.

No stage should increase live concurrency by configuration alone. The release
gate for M4 is proof that two application processes cannot oversubscribe the
same provider RPM/TPM or cost budget.
