# B3.4 Durable Fair Scheduler — Independent Architecture and Review

**Status:** design/review artifact only. No production source code, executable tests, schema, migrations, runtime composition, or live concurrency was changed; only this design artifact was revised.

| Item | Value |
|---|---|
| Accepted base | `47551da77488bf88a34291633025889447171a7` |
| Worktree | `C:\Users\Andriianov\.codex\worktrees\b34-r3-3-legacy-recovery\averon-import` |
| Branch | `review/b34-r3.3-legacy-recovery` |
| Artifact | `artifacts/B34-deepseek-architecture.md` |
| Revision | **3.3.1** — legacy terminal page/B1 recovery closure, R-30 invariant, and case-specific receipt requirements; see §16.7 |
| Live concurrency | remains **1**; no SSH/VPS, no paid provider calls, no schema v3 |
| Review method | source-first inspection of the accepted base, not trust in prior summaries |

> **Review-basis note.** Luna 6's unpublished prose was not available as a tracked artifact in this worktree. The Luna assessment below therefore evaluates the **load-bearing claims of the B3.4 proposal as represented in the task packet** against the accepted source. Each claim is labelled as a claim, not quoted. GPT-6 findings are treated the same way: each is independently confirmed, refuted, or qualified with file/line evidence.

---

## 0. Executive verdict

**B3.3 is a correct atomic send gate, but it is not a durable page/job lifecycle. Revision 3.3 additionally closes four legacy page/B1 combinations that the accepted B3.3 recovery path could leave behind: `claimed + not_sent`, `cancelled + not_sent + attempt_id IS NULL`, `send_committed + terminal`, and `claimed + unknown + attempt_id IS NULL`. Their repair is projection-only and must not be confused with new B3.4 settlement.** The accepted base has no production scheduler, no production caller of `_claim_page_for_dispatch`, no page-completion projection, no job roll-up, no path that produces `paused_restart`, and no production trusted document resolver. A B3.4 that only adds a fair queue on top of the current seams would schedule pages into a lifecycle that cannot finish them.

The correct B3.4 scope is therefore **two coupled deliverables**, and I recommend splitting the implementation approval accordingly:

| Sub-phase | Goal | Why it cannot be deferred |
|---|---|---|
| **B3.4a — lifecycle and recovery correctness** | Make `claimed → intent → send_committed → terminal` real; project page outcomes and job roll-up; unify job-managed attempt identity; classify faults; recover pre-gate `not_sent` strictly; make evidence recovery job-aware | Without this, a scheduler creates permanently stuck pages and blocks shared billing/quota scopes |
| **B3.4b — fair scheduler and bounded workers** | Persisted two-level round-robin, adaptive admission, global worker pool, internal resolver integration | This is the stated B3.4 feature, but it is only safe after B3.4a |

**Do not implement B3.4b first.** A scheduler that claims pages before page completion and recovery exist will convert every crash into a stuck job and, in the post-gate case, into a permanently blocked credential scope.

The single most important correctness rule is:

> **A page may be declared `not_sent` only while it is still possible to prove that the gate never committed.** After the dispatcher acquisition transaction has blanket-converted `intent → unknown`, that proof is gone forever because the schema trigger forbids `unknown → not_sent`.

---

## 1. Independent Luna 6 assessment

### 1.1 What the proposal gets right

1. **The gate belongs at the B1/B2 boundary.** `commit_job_send_gate` is the right linearization point: it validates the B1 intent, reserve receipt, active run, binding, billing/quota policies, job/page fences, and cancellation revision in one `BEGIN IMMEDIATE` transaction. The accepted tests demonstrate a single SQLite winner between cancel and gate (`tests/test_ai_first_b33_atomic_gate.py:147-190`).
2. **Fairness must be persisted, not process-local.** The base already contains `durable_scheduler_users` and `durable_scheduler_state` (`coordination_schema_v2.py:103-109`) and `durable_jobs.last_document_turn` (`:64`). These are sufficient for a two-level round-robin without schema v3.
3. **Job-managed evidence is the right recovery artifact.** The evidence envelope is immutable, hash-sealed, private-ACL checked, and written before B1 outcome settlement (`durable_dispatch.py:206-253`). That is the correct shape for crash recovery.
4. **Capacity must be global, not per runner.** `PAGE_CAPACITY`, `RENDER_CAPACITY`, `SYNC_RENDER_CAPACITY`, the single renderer process, and the 10-thread HTTP pool already establish process-wide bounds (`dispatch.py:97-99`, `pdf_render_worker.py:72`, `qwen_vision_adapter.py:41-42`).
5. **Deferral is not failure.** `DeferredWithoutIntent` is raised before any B1 intent exists; `prepare()` deliberately does not set `_failed` for it (`durable_dispatch.py:497-499`). The scheduler must treat it as backpressure, not as a fatal dispatcher error.

### 1.2 Load-bearing claims and verdicts

| # | Proposal claim | Verdict | Source-grounded reason |
|---|---|---|---|
| L1 | B3.3 already implements `claimed → intent → send_committed` | **Refuted** | `_reserve` inserts the B1 attempt as `intent` but never updates the page (`coordination.py:654-661`). `_claim_page_for_dispatch` sets page `claimed` (`durable_job_store.py:546-551`). The gate updates page `claimed → send_committed` (`coordination.py:1019-1031`). No production write sets page `intent`. |
| L2 | A scheduler can be layered on B3.3 without changing B1/B3.3 core | **Refuted** | There is no page-completion projection and no job roll-up; `record_outcome` only updates a page for one cancel/not-sent case (`coordination.py:1074-1090`). Pages stay `send_committed` after success. A scheduler would run out of schedulable pages and never finish a job. |
| L3 | Existing `settle_saved_evidence` works for job-managed attempts | **Refuted** | It requires `attempt_no == 1`, `claim_seq == 1`, and `attempt_id == uuid5(run_id, "page:N:attempt:1")` (`durable_dispatch.py:379-381`). Job-managed IDs are `uuid5(_PAGE_NAMESPACE, f"{run_id}:{page}:1")` (`durable_job_store.py:517`). |
| L4 | One owner-fenced transaction boundary covers commands and dispatch | **Qualified / refuted as stated** | `DurableJobStore._write` uses `ledger._connection()` + `BEGIN IMMEDIATE` with no owner lease (`durable_job_store.py:97-119`). That is intentional and necessary for API-side cancel/resume while the dispatcher holds the OS lock. The correct model is **two boundaries** (command and dispatch), with the gate as the meeting point. |
| L5 | `durable_scheduler_users`/`durable_scheduler_state` suffice for persisted fairness | **Supported, with semantics work** | The tables exist but are unused. `last_document_turn` is per job, and there is one active job per document, so it is effectively per-document. A two-level user→job round-robin is implementable without schema change. |
| L6 | Existing capacity constants permit bounded 10-page concurrency | **Refuted as a promise** | `PAGE_CAPACITY=10` and the HTTP pool are 10, but the renderer is one process (`max_workers=1`, `RENDER_CAPACITY=1`) and B1 `global_concurrency` defaults to 1 (`coordination_contracts.py:192`). B3.4 must not promise 10-page parallel rendering or live concurrency > 1. |
| L7 | A trusted resolver can be added at the edge without changing B3.2 contracts | **Partially supported** | `AuthorizedDocumentContext` is already sealed and private (`durable_job_contracts.py:104-144`), so a resolver can construct it. But the only constructor today is `_verified_document_context`, used by tests (`tests/b32_test_support.py:45`, `tests/test_ai_first_durable_jobs.py:396`). No production caller exists. |
| L8 | Ambiguous outcomes can be reconciled later without blocking shared scopes | **Refuted** | Any `unknown` outcome calls `_block_attempt`, setting `billing_scopes.blocked=1` and `quota_scopes.blocked=1` (`coordination.py:709-713`). No production path sets `blocked=0`; `record_outcome` explicitly says it is not an administrative release API (`:1045`). B6 owns reconciliation. |
| L9 | `paused_restart` is an existing, reachable recovery state | **Refuted** | No production code sets `paused_restart`. `resume` requires it (`durable_job_store.py:756-757`, `:774-775`), so resume is unreachable except by direct DB manipulation in tests. |
| L10 | A terminal page outcome is already projected to the job | **Refuted** | No production code updates `durable_job_pages` to `completed`, `incomplete`, `failed`, or `unknown_outcome`; no code updates `durable_jobs` to `completed`, `completed_with_errors`, or `failed`. The only terminal page update is `record_outcome` for `not_sent` + cancel (`coordination.py:1074-1090`), and that update leaves `attempt_id` NULL. |

### 1.3 Overall assessment

Luna's direction — an atomic send gate, persisted fairness, immutable evidence, bounded workers, and a trusted resolver — is sound. The proposal's **completeness claim** is not. B3.4 must be a corrective lifecycle phase, not merely a scheduler phase. The most dangerous hidden assumption is that the page state machine is already implemented; it is not. The second most dangerous is that a dispatcher restart can conservatively mark everything `unknown`; that destroys the only strict pre-gate `not_sent` proof and permanently blocks shared scopes.

**Recommendation:** approve B3.4a first, with a hard stop after its tests. Approve B3.4b only after B3.4a proves that a crash at every pre-gate and post-gate point produces a terminal page/job or a quarantined job — never a stuck page.

---

## 2. GPT-6 findings: confirmed, refuted, qualified

The table below is the independent adjudication. “Confirmed” means the finding is true against the accepted base. “Confirmed with nuance” means the mechanism is real but the remedy or severity needs correction. “Refuted” means the finding is false or misstates the accepted design.

| ID | GPT-6 finding (paraphrased) | Verdict | Evidence |
|---|---|---|---|
| G1 | Page `intent` is never used; reserve leaves page `claimed`; gate goes `claimed → send_committed`; a crash after reserve leaves a stuck page | **Confirmed** | `coordination.py:654-661` inserts attempt `intent` only; `durable_job_store.py:546-551` sets page `claimed`; `coordination.py:1019-1031` gate updates `claimed → send_committed`; `_claim_page_for_dispatch` rejects any existing attempt (`durable_job_store.py:524-525`). |
| G2 | `dispatcher()` marks all old `intent` attempts `unknown` and blocks scopes; a single ambiguous attempt can permanently block a shared scope | **Confirmed with nuance** | `coordination.py:450-454`, `:709-713`. The blocking is conservative liability behavior, not an accidental write. The defect is doing it **before** job-managed pre-gate proof, and the absence of a B6 unblock path. |
| G3 | `unknown → not_sent` is forbidden by trigger, so pre-gate proof must happen before the blanket update | **Confirmed** | `coordination_schema.py:123-125`: `OLD.state='unknown' AND NEW.state='not_sent'` aborts. `intent → not_sent` is allowed. |
| G4 | `settle_saved_evidence` and adapter recovery hard-code the legacy attempt ID and `claim_seq == 1`, breaking job-managed recovery | **Confirmed** | `durable_dispatch.py:379-381`; `durable_qwen_adapter.py:130-131`, `:136`, `:196-200`. |
| G5 | `prepare()` sets `_failed` on any reserve error, including deterministic stale-claim/cancel-race errors | **Confirmed** | `durable_dispatch.py:500-502`. `DeferredWithoutIntent` is the only exempt path (`:497-499`). `_failed` is checked by every subsequent call (`:430-434`). |
| G6 | `_settle()`/`finish()` set `_failed` on any settlement error | **Confirmed** | `durable_dispatch.py:578-587`, `:593-621`. A deterministic `outcome_conflict` would kill the dispatcher. |
| G7 | `register()` calls `ledger.register_run()` for an already-registered B3.2 run and can raise `generation_conflict` | **Confirmed conditionally** | `durable_dispatch.py:339-344` derives `uuid5(UUID(run.run_id), "register").hex` (`:342`). `coordination.py:522-541` replays only an exact `(operation_id, kind, request_digest)` match (`:526-529`) and otherwise rejects `run.generation <= documents.generation` (`:530-533`). The defect reproduces whenever the original registration used any other operation ID. The accepted fixtures that register a run use a non-deterministic or arbitrary ID: `tests/b32_test_support.py:95` and `tests/test_ai_first_durable_jobs.py:695` use `uuid4().hex`; `tests/test_ai_first_coordination.py:68` and `tests/ai_first_coordination_worker.py:47` use `run.run_id`. If the original registration used the same deterministic ID, `register()` is a normal replay. Reproducer and safe adoption are in §2.2 and §5.4. |
| G8 | `DurableJobStore._write` bypasses the owner-fenced `_transaction`; “single owner-fenced boundary” is false | **Confirmed as a model mismatch; not a functional bug by itself** | `durable_job_store.py:97-119` vs. `coordination.py:340-429`. The command path must run without the dispatcher lease. The gate’s `cancel_revision` and page predicates make the race linearizable. The design must document two boundaries and strengthen `_write`’s crash-receipt handling. |
| G9 | Scheduler tables exist but are unused | **Confirmed** | `coordination_schema_v2.py:103-109`; no production reference outside the schema. |
| G10 | `durable_job_pages` has no retry/backoff columns; one B1 attempt per page is effectively enforced | **Confirmed** | `coordination_schema_v2.py:73-91`; `_claim_page_for_dispatch` rejects any attempt (`durable_job_store.py:524-525`); `Attempt` requires `attempt_no >= 1` and the table has `UNIQUE(run_id,page_number,attempt_no)` (`coordination_schema.py:84`). |
| G11 | Renderer is single-process; do not promise 10-page parallel rendering | **Confirmed** | `pdf_render_worker.py:72`; `dispatch.py:98-99`; `RENDER_CAPACITY=1`. |
| G12 | No production trusted resolver; `_verified_document_context` is test-only | **Confirmed** | `durable_job_contracts.py:139-144`; only test call sites (`tests/b32_test_support.py:45`, `tests/test_ai_first_durable_jobs.py:396`, `tests/ai_first_durable_jobs_worker.py:28`). |
| G13 | `DeferredWithoutIntent` is a proven no-intent refusal; scheduler must release/backoff, not fail | **Confirmed** | `coordination.py:616-617`, `:649-652`; `durable_dispatch.py:497-499`. |
| G14 | `record_outcome` never unblocks scopes | **Confirmed** | No `blocked=0` update exists; `record_outcome` docstring says it is not an administrative release API (`coordination.py:1041-1046`). |
| G15 | There is no page-completion projection or job roll-up | **Confirmed (additional)** | `record_outcome` terminal branch only updates the attempt (`coordination.py:1069-1073`); no `durable_job_pages`/`durable_jobs` terminal update exists. |
| G16 | `resume` is unreachable because nothing sets `paused_restart` | **Confirmed (additional)** | Grep of production `durable_jobs` updates finds no `paused_restart` writer; `resume` requires it (`durable_job_store.py:756-757`). |
| G17 | Terminal outcomes with `result is None` have no evidence file, so they cannot be recovered | **Confirmed (additional)** | `_settle` writes evidence only when `result is not None` (`durable_dispatch.py:614-617`), but can record `terminal` for an HTTP 1xx–4xx with `result=None` (`:600-613`). `settle_saved_evidence` then raises `historical_evidence_missing` (`:361-363`). |
| G18 | `ResultEvidence.write` is not idempotent across a crash between link and pending-unlink | **Confirmed (additional)** | `durable_dispatch.py:213-227`; a retry after a crash can fail on existing `final` or `pending`. |
| G19 | The gate is not atomic because reserve and gate are separate transactions | **Refuted** | The gate itself is one `BEGIN IMMEDIATE` with all predicates (`coordination.py:916-1039`). The split is intentional: reserve creates liability; gate authorizes send. The bug is the missing page `intent` state, not gate atomicity. |
| G20 | Cancel can revoke a send after gate commit | **Refuted** | After gate, page is `send_committed` with `attempt_id`; cancel only cancels pages with `attempt_id IS NULL` and state `queued/quota_wait/blocked` (`durable_job_store.py:726-728`). Test `test_gate_then_cancel_retains_send_commit_and_liability` confirms `send_committed` after cancel. |
| G21 | `DeferredWithoutIntent` should fail the dispatcher | **Refuted** | It is emitted before any intent (`coordination.py:616-617`, `:649-652`) and is explicitly exempt from `_failed` (`durable_dispatch.py:497-499`). Failing closed on backpressure would make quota windows fatal. |
| G22 | Settlement can clear `billing_scopes.blocked` on recovery | **Refuted** | No unblock API exists; B6 requires schema v3 (`coordination.py:1161-1166`). Settlement must not clear blocked scopes. |
| G23 | Page `claimed` is safe to reclaim even if an attempt exists | **Refuted** | `_claim_page_for_dispatch` rejects `attempts[0] != 0` (`durable_job_store.py:524-525`). A page with any attempt must be terminalized or quarantined, not reclaimed. |
| G24 | Renderer capacity can simply be raised to 10 to match `PAGE_CAPACITY` | **Refuted** | The renderer is intentionally one process (`pdf_render_worker.py:1-2`, `:72`; `RENDER_CAPACITY=1`). Raising it changes memory/CPU behavior and is outside B3.4. |
| G25 | Evidence is written atomically with the B1 outcome | **Refuted as strict atomicity** | Evidence is written first (`durable_dispatch.py:614-617`), then `record_outcome` (`:618-621`). The file write is atomic, but file and DB are not one transaction. This yields recoverable orphan evidence, or an unrecoverable response if crash occurs before the file write. |
| G26 | `last_document_turn` is a tenant-wide document fairness primitive | **Qualified / refuted** | It is a per-job column (`coordination_schema_v2.py:64`). With one active job per document (`:70-71`), it approximates document fairness, but it is not a tenant-wide counter and must not be described as one. |
| G27 | B3.3 already projects page completion | **Refuted** | The accepted B3.3 test asserts page `send_committed` after gate (`tests/test_ai_first_b33_atomic_gate.py:140-144`). There is no later projection. |

### 2.1 The three findings that must gate scheduler implementation

1. **G1 + G2 + G3**: reserve/gate/page-state/restart form a single correctness chain. Fixing only one link is worse than fixing none.
2. **G4 + G15 + G17 + G18**: evidence recovery, page completion, and job roll-up are one contract. A job-managed scheduler that cannot settle evidence after restart is not durable.
3. **G7 conditionally**: if the run was originally registered with any operation ID other than `uuid5(run_id, "register")` — which is what the accepted run-registration fixtures cited above do — the first `register()` raises `generation_conflict`. This is an integration blocker for those runs, not for a run whose original registration already used the deterministic ID. Safe verified adoption is mandatory either way.

### 2.2 G7 re-verification and reproducible test

G7 is a **conditional** defect, not an unconditional one. The outcome is determined entirely by the operation ID used for the original `register_run`:

| Original B3.2 registration operation ID | `DurableDispatcher.register()` behaviour |
|---|---|
| `uuid5(UUID(run_id), "register").hex` | `register_run` finds the exact replay (`coordination.py:526-529`) and succeeds |
| any other ID (`uuid4().hex`, `run.run_id`, …) | no replay; `run.generation <= documents.generation` → `generation_conflict` (`:530-533`) |

A minimal, reproducible ledger-level test proves the mechanism without any timing or fixture ambiguity:

```python
from uuid import UUID, uuid4, uuid5
import pytest
from averon_import.services.ai_first.coordination import CoordinationError

def test_g7_generation_conflict_after_random_registration(ledger, owner, run):
    # This is exactly what tests/b32_test_support.py:95 does today.
    ledger.register_run(owner, uuid4().hex, run)
    # This is the operation ID DurableDispatcher.register() always uses.
    with pytest.raises(CoordinationError, match="generation_conflict"):
        ledger.register_run(owner, uuid5(UUID(run.run_id), "register").hex, run)

def test_g7_replay_when_original_id_is_deterministic(ledger, owner, run):
    operation_id = uuid5(UUID(run.run_id), "register").hex
    first = ledger.register_run(owner, operation_id, run)
    second = ledger.register_run(owner, operation_id, run)
    assert first == second          # exact replay, no generation conflict
```

The dispatcher-level reproducer is the same sequence with `dispatcher.register(client)` in place of the second `register_run`; `DurableQwenAdapter.start` calls it (`durable_qwen_adapter.py:119`). The test must also assert that the failed call left `documents.active_run_id`, `documents.generation`, and `runs` unchanged, because `register_run` raises before any write (`coordination.py:530-533`).

**Conclusion:** the accepted tests register runs with `uuid4().hex` (`tests/b32_test_support.py:95`, `tests/test_ai_first_durable_jobs.py:695`) or `run.run_id` (`tests/test_ai_first_coordination.py:68`), so the conflict is reproducible in the accepted suite. It is not a universal property of every B3.2 run. The remedy in §5.4 is safe for both cases and does not depend on which verdict is taken.

---

## 3. Source-grounded current reality

### 3.1 What actually happens today (durable path)

```text
submit (T0) ──> durable_jobs=preparing_input, pages=queued
      │
      └─ T1 ──> durable_jobs=queued, execution_state=ready

(no production scheduler; _claim_page_for_dispatch has no caller)

test-only claim ──> page=claimed
      │
      └─ reserve ──> attempts=intent, page stays claimed
      │
      └─ gate ──> page=send_committed, attempt_id set

POST ──> finish ──> evidence file + attempts=terminal
      │
      └─ record_outcome ──> attempt terminal
                           page stays send_committed   <-- missing projection
                           job stays running           <-- missing roll-up
```

### 3.2 The stuck-page crash window

```text
claim:  page=claimed, attempt_id=NULL, claim_seq=1
reserve: attempts(intent), page still claimed
CRASH
restart (legacy/B2 `dispatcher()` path) -> attempts(intent -> unknown), scopes blocked
durable jobs instead use `dispatcher_for_jobs()` -> old intents remain intent until recovery classifies them
         page still claimed, attempt_id=NULL
scheduler: _claim_page_for_dispatch -> attempts[0] != 0 -> durable_job_attempt_already_exists
```

The page is now permanently unschedulable. It is not in `_UNKNOWN_PAGES` (`durable_job_store.py:28`), so `resume` may accept the job, but the claim will still be rejected. The job can never complete. This is the concrete failure that B3.4a must eliminate.

### 3.3 The two identity namespaces

| Context | Identity |
|---|---|
| Legacy B2 | `uuid5(run_id, f"page:{page}:attempt:1")` (`durable_dispatch.py:455-456`) |
| Job-managed candidate | `uuid5(_PAGE_NAMESPACE, f"{run_id}:{page}:1")` (`durable_job_store.py:517`) |

The job-managed ID is stored in `durable_job_pages.candidate_attempt_id` and must equal the B1 `attempt_id` when an attempt exists (`coordination_schema_v2.py:75-86`). Any recovery code that assumes the legacy ID is wrong for every job-managed page.

### 3.4 The two transaction families

| Family | Example | Owner lease? | Purpose |
|---|---|---|---|
| Command | `submit`, `cancel`, `resume`, `_block_execution_input` via `_write` | No | API-side durable commands must work while the dispatcher holds the OS lock |
| Dispatch | claim, reserve, gate, settlement via `_transaction(owner)` | Yes | Fenced to the current dispatcher epoch/lease |

The gate is the only safe meeting point. It reads `job.cancel_revision` and page state, so a command cancel and a dispatch gate have one SQLite winner. This is the correct model; the documentation must stop claiming a single owner-fenced boundary.

### 3.5 No production composition

`DurableJobStore` is defined but not instantiated in the runtime. `_claim_page_for_dispatch` has no production caller. `OrchestrationRunner` still builds a per-run `asyncio.Queue` and `config.concurrency` workers (`orchestration.py:1066`, `:1114-1115`, `:1496-1515`). There is no global fair scheduler. B3.4 is not “connect an existing scheduler”; it is “build one and fix the lifecycle underneath it”.

---

## 4. Page and job state machines

### 4.1 Job state machine

States are the accepted schema states (`coordination_schema_v2.py:60-61`).

```text
absent
  │ T0
  ▼
preparing_input ──T1──> queued ──first claim──> running
  │                       │                       ├─ all pages success ──> completed
  │ cancel (no attempts)  │ cancel (no attempts)  ├─ all terminal, some error ──> completed_with_errors
  ▼                       ▼                       ├─ all failed ──> failed
cancelled             cancelled                   ├─ safe recovery ──> paused_restart
                                                  ├─ cancel, no unresolved attempt ──> cancelled
                                                  ├─ cancel, attempt unsettled ──> cancelling
                                                  │                                  ├─ all settle, no unknown ──> cancelled
                                                  │                                  └─ unknown financial result ──> blocked (B6)
                                                   └─ unknown/reconciliation/quarantine ──> blocked (B6)
                                                       cancel while unknown: remain blocked
                                                       input/source failure ──────────┘
```

| From | To | Trigger | Guards / side effects |
|---|---|---|---|
| absent | `preparing_input` | T0 submit | One active job per document/generation; snapshots, pages, `submit_begin` receipt atomic |
| `preparing_input` | `queued` | T1 ready | Execution artifact verified; `execution_state=ready`; `submit_ready` receipt |
| `preparing_input` | `cancelled` | Cancel with no B1 attempt on any page | Exact pre-intent cancel; pages `queued/claimed → cancelled` only where `attempt_id IS NULL` and no B1 attempt exists |
| `queued` | `cancelled` | Cancel with no B1 attempt on any page | Same exact pre-intent cancel; no liability exists, so no `cancelling` phase |
| `preparing_input` / `queued` | `blocked` | Missing/corrupt execution input or source mismatch | `execution_state=blocked`; only pages with no attempt |
| `queued` | `running` | First scheduler claim | Sets `authorized_owner_epoch`, bumps revision |
| `running` | `running` | Claim/settlement/roll-up or prior-epoch adoption | Owner-fenced adoption updates `authorized_owner_epoch` to the acquired epoch before recovery/claims; B1 attempt epochs remain immutable |
| `running` | `cancelled` | Cancel leaves every page terminal/cancelled, with no unresolved B1 attempt, no `unknown_outcome`, and no job-relevant scope requiring reconciliation | No-attempt pages are cancelled immediately. A reserved `intent` is not settled by the command; it must first receive owner-fenced `not_sent` settlement |
| `running` | `cancelling` | Cancel with any `send_committed` or unresolved attempt-bearing page, no `unknown_outcome`, and no job-relevant scope requiring reconciliation | `cancel_revision+1`; gate rejects if it has not committed; safe no-attempt pages are cancelled immediately; sent work waits for settlement |
| `running` | `paused_restart` | Recovery policy explicitly pauses a safely adopted job after attempts are classified, with schedulable no-attempt pages remaining | New epoch alone does not pause the job: adopt it and keep `running`. Pause only in an owner-fenced transaction after no unknown/unsettled attempt remains; scheduler waits for explicit resume |
| `running` | `blocked` | Unknown financial outcome, policy/billing quarantine, source/generation change, invariant violation | Page-level `unknown_outcome`/`blocked`; no auto-resume; B6 owns reconciliation and scope unblock |
| `running` | `completed` | All pages `completed` | `finished_at` set |
| `running` | `completed_with_errors` | All terminal, at least one `incomplete`/`failed`/`blocked`/`cancelled` but at least one success | `finished_at` set |
| `running` | `failed` | All pages `failed`, or unrecoverable internal failure with no success | `finished_at` set |
| `cancelling` | `cancelled` | Every page is terminal/cancelled, no page is `unknown_outcome`, every B1 attempt is settled, and no job-relevant scope requires reconciliation | `finished_at` set; a terminal page with a retained `attempt_id` is allowed; late results cannot overwrite settlement |
| `cancelling` | `blocked` | Any page has `unknown_outcome`, any financial result is unknown, or any job-relevant scope requires reconciliation | Never mark cancelled; B6 alone reconciles and unblocks scopes |
| `paused_restart` | `queued` | Explicit `resume` | Reject if any nonterminal page has an attempt or `unknown_outcome` |
| `paused_restart` | `cancelling` / `cancelled` | Cancel | Same cancel rules; cancel with only no-attempt pages goes straight to `cancelled` |
| `paused_restart` | `blocked` | Source/input changed before resume | No retry |
| `blocked` | `cancelled` | Cancel on a non-financial block with no unknown, unresolved attempt, or job-relevant blocked scope | Safe no-attempt pages are cancelled; no unknown/reconciliation liability may remain |
| `blocked` | `blocked` | Cancel while any financial outcome is `unknown_outcome` or any job-relevant scope requires reconciliation | `cancel_revision+1`; safe no-attempt pages are cancelled; job/scope remain blocked for B6 |
| `blocked` | `cancelling` | Cancel while a `send_committed` result is pending, with no unknown and no job-relevant scope requiring reconciliation | `cancel_revision+1`; safe pages are cancelled; wait for settlement, then cancelled or blocked |
| terminal states | — | — | No B3.4 transition; B6 may reconcile `unknown_outcome` |

**Roll-up precedence (MUST):**

1. If any page is `unknown_outcome`, any financial result is unknown, or any job-relevant billing/quota scope (§4.1) is blocked/reconciliation-required (including overage): `blocked`, even when cancellation was requested; never `cancelled` while financial uncertainty remains.
2. Else if job is `cancelling`: remain `cancelling` while any `send_committed`/attempt-bearing page remains unsettled; become `cancelled` only when all pages are terminal/cancelled and all attempts are settled.
3. Else if an invariant is violated: `blocked`.
4. Else if any page is nonterminal: keep `running` (or `paused_restart` only when the recovery transaction explicitly chose that safe state).
5. Else if every page is `completed`: `completed`.
6. Else if at least one page is `completed`/`incomplete`: `completed_with_errors`.
7. Else: `failed`.

**Job-relevant billing/quota scopes (normative):** derive `job_relevant_scope_ids` only from the immutable `binding` snapshot on this job's B1 attempts, joining `durable_job_pages` to `attempts` by `(run_id, page_number)` and selecting each attempt's `billing_scope_id` plus all persisted `quota_scope_ids`. Do not use the current credential, resolver, or policy to reconstruct scope membership. If any such billing/quota scope is `blocked=1` or its B1 settlement reports reconciliation-required (including overage), the job roll-up is `blocked` even if all pages are otherwise terminal and cancellation was requested. Schema v2 has a shared blocked bit rather than per-job block provenance, so any blocked scope touched by one of this job's attempts is conservatively treated as requiring reconciliation; B3.4 does not clear it.

### 4.2 Page state machine

States are the accepted schema states (`coordination_schema_v2.py:78-79`).

```text
queued ──claim──> claimed ──reserve──> intent ──gate──> send_committed ──settle──> completed
  │                 │                    │                  │                     ├─> incomplete
  │                 │                    │                  │                     ├─> failed
  │                 │                    │                  │                     └─> unknown_outcome
  │                 │                    ├─ owner-fenced pre-gate settlement ──> failed / cancelled
  │                 │                    └─ invariant ──────> blocked
  │                 ├─ stale release ──> queued
  │                 ├─ deferral ───────> quota_wait ──retry──> claimed
  │                 └─ cancel ─────────> cancelled
  ├─ cancel ───────> cancelled
  └─ input/source ─> blocked
```

| From | To | Trigger | Guards / side effects |
|---|---|---|---|
| `queued` | `claimed` | Scheduler claim | No attempt exists; `claim_seq = max(attempt.claim_seq)+1`; `owner_epoch` current; page revision+1; job `running` |
| `queued` | `cancelled` | Cancel | No attempt |
| `queued` | `blocked` | Source/input/policy | No attempt |
| `claimed` | `quota_wait` | `DeferredWithoutIntent` (quota/global capacity) | No attempt; clear `owner_epoch`; keep `claim_seq` |
| `claimed` | `queued` | Stale claim release / scheduler shutdown | No attempt; clear `owner_epoch` |
| `claimed` | `intent` | **B3.4a reserve** | Same transaction as B1 `attempts=intent`; set `attempt_id`; revision+1 |
| `claimed` | `cancelled` | Cancel before reserve | `attempt_id IS NULL` **and** no B1 attempt exists for `(run_id, page_number)`; `safe_code='cancelled'` |
| `claimed` | `blocked` | Source/input/policy before reserve | No attempt |
| legacy `claimed` | `failed` | Projection-only repair of exact B1 `not_sent` | Exact attempt/page/claim and reserve/settlement receipts; no final or pending evidence; attach candidate ID; B1 remains immutable |
| legacy `claimed` | `blocked` | B1 `unknown`, or identity/receipt/evidence contradiction | Preserve B1 state and scope blocks; attach candidate ID only when identity is exact; no settlement/unblock |
| legacy `cancelled` | `cancelled` | Projection-only repair of cancel-winning B1 `not_sent` | Historical cancel branch, exact reserve and outcome settlement receipts, exact durable cancel command receipt, and identity prove cancel won; attach ID; B1 unchanged |
| legacy `send_committed` | `completed` / `incomplete` / `failed` | Projection-only repair of B1 `terminal` | Exact reserve and terminal outcome receipts plus valid matching final evidence; page/job only; B1 unchanged |
| `quota_wait` | `claimed` | Retry after backoff | No attempt |
| `quota_wait` | `cancelled` / `blocked` | Cancel / policy | No attempt |
| `intent` | `send_committed` | Gate commit | Attempt must be `intent`; reserve receipt valid; run/binding/policies valid; job `queued/running`; `cancel_revision` matches; page revision matches; immutable `attempt_id` preserved; **the cancel/send linearization point** |
| `intent` | `failed` | Current dispatcher or new-owner recovery settles proven pre-gate `not_sent` | Attempt is `intent`; gate did not commit; B1 settlement and page/job projection are owner-fenced and atomic; preserve `attempt_id`; no scope block; `safe_code='not_sent_pre_gate'` |
| `intent` | `cancelled` | Owner-fenced pre-gate settlement after cancel won | Command cancel leaves page and B1 attempt untouched. Gate rejects on `cancel_revision`; current dispatcher settles B1 `intent → not_sent` and page `intent → cancelled` atomically, retaining `attempt_id` and using `safe_code='cancelled_pre_gate'`; after crash, new-owner recovery does this |
| `intent` | `blocked` | Invariant/policy/source | No send proof; quarantine |
| `send_committed` | `settlement_pending` | Optional response/evidence staging | Only if a two-step settlement is introduced; not required if settlement is atomic with outcome |
| `send_committed` | `completed` | B1 terminal + extraction `extracted`/`no_specification` | Valid final evidence; page `attempt_id` matches and is retained; job roll-up same transaction |
| `send_committed` | `incomplete` | B1 terminal + extraction `incomplete` | Evidence valid; partial result retained |
| `send_committed` | `failed` | Definitive B1 terminal failure with valid final evidence | No retry; safe code; B1/page/job projection is atomic |
| `send_committed` | `unknown_outcome` | B1 unknown, missing/invalid final evidence, or transport ambiguity | Scope blocked; job becomes `blocked` (including from `cancelling`); B6 alone reconciles/unblocks |
| `send_committed` | `cancelled` | — | **Forbidden.** Cancel never revokes an authorized send; the page must be settled by B1 (`durable_job_store.py:726-728`) |
| `settlement_pending` | terminal | Settlement finishes | Must match B1 outcome |
| `failed` + B1 `not_sent` | — | — | Stable pre-gate settlement only with the exact canonical receipt/digest and retained attempt ID; recovery is read-only exact replay/no-op. Mismatch is an integrity contradiction, never a second settlement |
| legacy `claimed` + B1 `not_sent` | projection-only repair | — | Repair to `failed` only after exact attempt/page/run/claim identity and canonical historical outcome settlement receipt match, with no final or pending evidence; B1 remains `not_sent` |
| legacy `cancelled` + B1 `not_sent` + `attempt_id IS NULL` | projection-only repair | — | Keep `cancelled` and attach the immutable attempt ID only when the exact legacy cancel-winning branch, reserve and outcome settlement receipts, and durable cancel command receipt prove cancellation won; otherwise quarantine |
| legacy `send_committed` + B1 `terminal` | projection-only repair | — | Repair page/job only if exact identity, immutable reserve receipt, canonical terminal outcome receipt, outcome digest, and matching valid final evidence agree; missing/mismatched facts quarantine |
| legacy `claimed` + B1 `unknown` + `attempt_id IS NULL` | quarantine projection | — | Verify identity and immutable reserve receipt; no outcome receipt exists or is required for the legacy blanket conversion. Attach ID only if identity is exact, set page/job `blocked`; preserve B1 `unknown`, scope blocks, and evidence for B6; never settle/unblock |
| `blocked` + B1 `unknown` | — | — | Stable quarantine; preserve B1/page/job/scope state and attempt ID. Repeated recovery creates no operation, does not unblock, and cannot project success/`not_sent` |
| `unknown_outcome` + B1 `unknown` | — | — | Stable unknown; preserve existing B6-only behavior for valid, absent, or invalid late evidence; no B3.4 settlement or unblock |
| other terminal/quarantine pair | — | — | No B3.4 transition. Any unlisted page/B1 pairing is an integrity contradiction: quarantine/fail closed; never infer success or `not_sent` |

### 4.3 Invariants

1. **New B3.4 writes:** once a B1 attempt is created, `page.attempt_id = page.candidate_attempt_id = attempts.attempt_id` is written in the same reserve transaction and remains non-NULL/immutable through every later state. Before any B1 attempt exists, `page.attempt_id IS NULL`.
2. **Accepted B3.3 legacy rows:** three historical projections may legitimately have `attempt_id IS NULL`:
   - `claimed + not_sent` and cancel-winning `cancelled + not_sent`, because the old `record_outcome()` projection did not attach it;
   - `claimed + B1 unknown`, because the legacy blanket `dispatcher()` conversion selected every B1 `intent` attempt, called `_block_attempt`, and set `state='unknown'` directly, without an outcome operation receipt and without updating `durable_job_pages` (`coordination.py:430-454` at the accepted base).

   Recovery may attach the candidate ID only after exact attempt/page/run/claim identity and the case-specific immutable receipt/proof validation required in §5.6.2 and §6.2; a mismatch is quarantined and never scheduled. Once attached, it is immutable. For `claimed + B1 unknown`, the absence of an outcome settlement receipt is expected, not an integrity mismatch.
3. `page.claim_seq = attempts.claim_seq` whenever the B1 attempt exists; new B3.4 attempt and page identity projection are created atomically. Legacy repair validates the existing sequence and never rewrites it.
4. `attempt.run_id/page_number` equal page/job/run; `attempt.attempt_no = 1`; one attempt per page in B3.4.
5. `job.cancel_revision` is monotonic; gate requires exact equality.
6. After the startup adoption barrier, `job.authorized_owner_epoch` equals the current dispatcher epoch for every `running` job. A newly acquired epoch is not serving authority until adoption and recovery complete. Adoption changes only the job authorization projection; immutable `attempt.owner_epoch` is never rewritten.
7. `job.execution_state='ready'` for `queued`/`running`.
8. No terminal page may be rescheduled; no page with any attempt may be reclaimed.
9. No scope may be unblocked by B3.4 settlement; only B6.
10. No `unknown → not_sent` update is attempted; pre-gate proof happens before any unknown transition.
11. `DurableJobStore.cancel` never mutates a B1 attempt, calls `record_outcome`, or changes `intent → not_sent`; it increments `cancel_revision`, cancels only pages with no `attempt_id` and no matching B1 attempt, and leaves every attempt-bearing page and its `attempt_id` unchanged.
12. A page with any B1 attempt is never cancelled by the no-attempt predicate. The current owner settles a gate-rejected pre-gate intent; recovery under a new epoch does so after a crash.
13. `unknown_outcome` is not a settled financial result for job cancellation: any such page forces job `blocked`; no cancel/roll-up may mark that job `cancelled` until B6 resolves the financial uncertainty.
14. Recovery recognizes stable results: `blocked + B1 unknown`, `unknown_outcome + B1 unknown`, and `failed + B1 not_sent` with a retained attempt ID. Matching receipts, where the originating path created one, are verified and exact-replayed/read-only; mismatches quarantine without changing the B1 result or unblocking scopes. Legacy `claimed + not_sent`, `cancelled + not_sent + NULL attempt_id`, `send_committed + terminal`, and `claimed + unknown + NULL attempt_id` are repair inputs, not stable pairs.
15. Legacy projection repair changes only page/job projection in an owner-fenced transaction; it does not call settlement, create an operation receipt, alter B1 financial facts, or unblock a scope. Exact target state plus the immutable receipt(s) required for that specific case is the idempotency marker (outcome receipt for R-23/R-25/R-27, plus reserve receipt throughout, plus cancel command receipt for R-25; reserve receipt only for R-30); repeats are read-only no-ops.
16. Scheduler claims are disabled from owner acquisition until all prior-epoch running-job adoption, publication recovery, unsettled-attempt recovery, stale no-attempt claim release, and epoch-consistency checks have completed.
---

## 5. Transaction and atomic-boundary model

### 5.1 The two boundaries

**Command boundary (`_write`)** — `BEGIN IMMEDIATE`, no owner lease, optimistic `revision`/`cancel_revision` fences, immutable command receipt keyed by `(tenant_id, owner_id, operation_id)`. Used by submit/T0/T1, cancel, resume, block. It must not take the dispatcher OS lock. It must still be strengthened for crash-receipt handling (`OSError` after commit is currently collapsed to `durable_receipt_uncertain` without read-back; `durable_job_store.py:118-119`).

**Dispatch boundary (`_transaction(owner)`)** — `BEGIN IMMEDIATE`, owner epoch/nonce, lease expiry, clock regression, storage verification, commit-receipt uncertainty (`coordination.py:340-429`). Used by claim, reserve, gate, settlement, recovery.

The gate is the only place where a command fence (`cancel_revision`) and a dispatch fence (`owner_epoch`, page revision, attempt state) are checked together. That is what makes cancel-vs-send linearizable without a single global boundary.

**Acquisition is mode-specific.** Preserve `CoordinationLedger.dispatcher()` and its existing blanket `intent→unknown` behavior for the legacy B2 dispatcher. Durable-job mode MUST acquire through a distinct non-mutating method (`dispatcher_for_jobs()`): it takes the same OS lock/lease and advances the owner epoch, but does not change any old B1 attempt or job row. `DurableDispatcher` must select this path explicitly when constructed for job-managed work; the durable job runner must not call the legacy acquisition path. Acquisition closes scheduler admission. Before serving, the new owner performs owner-epoch adoption (§5.2 A3), publication recovery, and `recover_unsettled`; job-managed intents therefore remain classifiable until recovery has inspected page identity and final evidence.

### 5.2 Atomic-boundary table

| ID | Operation | Code boundary | Transaction | Atomic writes | Fences | Crash before commit | Crash after commit, before receipt | Idempotency |
|---|---|---|---|---|---|---|---|---|
| A1 | Durable-job owner acquisition | `DurableDispatcher(..., job_managed=True)` → `CoordinationLedger.dispatcher_for_jobs()` | OS lock + `_transaction(acquiring=True)` | Owner epoch/nonce/lease only; **no B1 attempt or job authorization mutation** | Storage identity, OS lock, owner epoch | No owner acquired | Read back/acquire under the same lock protocol; old `intent` rows and job authorization rows remain unchanged | New owner epoch; scheduler admission remains closed |
| A2 | Legacy B2 owner acquisition | Legacy `DurableDispatcher(..., job_managed=False)` → `CoordinationLedger.dispatcher()` | Existing OS lock + `_transaction(acquiring=True)` | Existing legacy blanket `intent→unknown` + scope blocks + owner epoch | Existing B2 behavior | No owner acquired | Existing B2 behavior | Preserve unchanged; B2 compatibility test |
| A3 | Adopt prior-epoch durable jobs | `DurableJobRunner.start` → `DurableJobStore.adopt_owner_epoch(owner)` | `_transaction(new_owner)` per bounded job batch; one job row per guarded update | For each `state='running'` row whose non-NULL `authorized_owner_epoch` is older than current owner: set it to current epoch and increment job revision; do not touch attempts/pages/receipts | Current owner nonce/epoch/lease; job state/revision; `authorized_owner_epoch < current_epoch`; active run/generation identity | No adoption for that job; startup remains closed | Re-read row: current epoch means adopted; otherwise repeat the guarded update | Idempotent per job/current epoch; null/future epoch is quarantine; no claim is eligible until all rows adopted and recovery completes |
| T0 | Submit begin | `DurableJobStore.submit` → `_write(create)` | `BEGIN IMMEDIATE` | Snapshot, job `preparing_input`, pages `queued`, `submit_begin` receipt | Active job/document, generation, capacity, request digest | No job | Read back `submit_begin`; never create a second T0 | Command PK + request digest |
| T1 | Submit ready | `_commit_ready` → `_write(commit)` | `BEGIN IMMEDIATE` | `execution_state=ready`, job `queued`, `submit_ready` receipt | Job state/revision, execution digest | Job stays `preparing_input` | Verify state + receipt | `submit_ready` PK |
| C1 | Claim page | `DurableJobStore._claim_page_for_dispatch` | `ledger._transaction(owner)` | Job `queued→running`, page `queued/quota_wait/claimed→claimed`, `claim_seq`, `owner_epoch`, revision; scheduler turn counters (B3.4b) | Job generation/run active, execution ready, cancel revision, no attempt, page revision | No claim | Re-read page; claim is the receipt | Revision + `claim_seq` |
| R1 | Reserve B1 intent | `CoordinationLedger._reserve(job_claim=...)` | `_transaction(owner)` | `attempts=intent`, quota holds, `reserve` receipt, **page `claimed→intent` + immutable `attempt_id` + page/job projection/revision** | Job/page claim and revision, `cancel_revision`, owner epoch, binding, billing/quota, run cap, sequence | No attempt, page stays `claimed` (safe release) | Replay `reserve` receipt; page is `intent` with same attempt ID and job/page projection | `attempt.operation_id`; B1 attempt, page identity and job/page projection commit/rollback together |
| G1 | Send gate | `CoordinationLedger.commit_job_send_gate` | `_transaction(owner)` | **Page `intent→send_committed`** | Attempt body/receipt, run, binding, billing/quota, job `queued/running`, page `intent` + attempt_id, cancel revision, page revision | Page stays `intent` → strict `not_sent` recovery | Page is `send_committed` → ambiguous if no evidence | Page revision + SQL predicates |
| P1 | HTTP POST | `DurableDispatcher.post` → client transport | None (outside SQLite) | Response in memory | Gate already committed; `_check()` before/after | May be unsent (if before POST) or unsent with `send_committed` (gate committed, no transport) | Same | No exactly-once; evidence/outcome |
| S1 | New B3.4 settlement | `DurableDispatcher.finish` / owner-fenced B1 settlement | Final evidence publication (outside SQLite), then `_transaction(owner)` | In one dispatch transaction: B1 `intent→terminal/not_sent/unknown` as justified, page projection retaining `attempt_id`, job roll-up, settlement receipt | Current owner epoch, attempt/page identity, attempt state, exact canonical settlement payload digest | No settlement commit; a verified pending file may be publication-recovered, never directly settled | Read back deterministic receipt; exact replay only, conflict never overwrites | `uuid5(UUID(attempt_id), "settlement").hex` + canonical payload digest |
| C2 | Cancel | `DurableJobStore.cancel` → `_write` | `BEGIN IMMEDIATE` | Job fence `cancel_revision+1`, cancel only pages with `attempt_id IS NULL` **and** no B1 attempt, read-only join to job-relevant scope flags, cancel receipt; choose `cancelling`/`blocked`/`cancelled` from post-state | Job revision, cancel revision, page state, `NOT EXISTS` B1 attempt, immutable scope IDs from attempts | No cancel | Command receipt read-back | Command PK + request digest; exact replay does not increment twice |
| C3 | Resume | `DurableJobStore.resume` → `_write` | `BEGIN IMMEDIATE` | Job `paused_restart→queued`, `resume` receipt | Revision, no unknown/nonterminal attempt | No resume | Command receipt read-back | Command PK |
| R2 | Recovery classification | Owner adoption, publication recovery, then `CoordinationLedger.recover_unsettled(owner)` | Adoption and B1/page/job actions use `_transaction(owner)` per bounded batch; publication repair is filesystem-only | Adopt prior-epoch `running` job authorization first; promote only validated pending publication; settle prior-epoch attempts under current owner fence using persisted immutable attempt identity/epoch; B1/page/job projection is atomic; recognize stable `blocked+unknown`, `unknown_outcome+unknown`, and `failed+not_sent` outcomes by exact receipt/no-op | Current owner nonce/epoch/lease; prior attempt epoch remains immutable; page/B1 identity, case-specific exact canonical receipt(s) per §5.6.2, page state, legacy gate proof where required, final evidence digest | No adoption/classification commit; startup remains closed | Re-read and exact-replay/no-op by canonical operation ID + digest; never overwrite | Per-job adoption and per-attempt settlement IDs/payloads are deterministic; retries are idempotent |
| LR1 | Legacy page/job projection repair | `recover_unsettled(owner)` after exact legacy receipt/evidence validation | Owner-fenced `_transaction(owner)`; no B1 settlement call | Page/job projection only; B1 attempts, operations, evidence digests and scope flags are read-only | Current owner fence; exact immutable attempt/page/run/claim identity; **case-specific immutable proof:** R-23 reserve + historical outcome receipt; R-25 reserve + `not_sent` outcome receipt + durable cancel command receipt; R-27 reserve + terminal outcome receipt + valid final evidence; R-30 reserve receipt + exact persisted B1 attempt/page/run/claim identity and original `owner_epoch` (no outcome receipt); expected page/job revisions | No projection repair | Re-read exact target projection and immutable B1 facts; if already exact, read-only no-op | No new B1 operation ID/receipt; exact target projection plus the case-specific immutable receipt(s) is the marker (outcome receipt for R-23/R-25/R-27; reserve receipt for R-30); any mismatch quarantines |
| R3 | Job roll-up and startup release | Same transaction as S1/R2/C2/LR1, then runner barrier | `_transaction(owner)` for projection; scheduler enabled only after a read-only all-clear check | Recompute job state/revision/finished_at from committed pages and relevant scopes; verify every `running` job has `authorized_owner_epoch=current`, no prior-epoch unsettled attempt remains unclassified, and no stale no-attempt claim remains | Current owner fence; all job/page/attempt projections | Previous projection; scheduler remains closed | Read back committed projection; barrier can be rerun | Deterministic from persisted state; scheduler barrier is process-local and is not persisted authority |

### 5.3 Gate linearization proof

The gate is a single `BEGIN IMMEDIATE` transaction. It:

1. Re-reads the attempt and requires its full canonical body and state `intent` (`coordination.py:932-945`).
2. Re-reads the reserve receipt and requires exact operation kind, request digest, body, result, and owner epoch (`:947-957`).
3. Re-reads the run/document/generation/active run (`:959-973`).
4. Re-reads the active binding and quota edges (`:975-988`).
5. Re-reads billing/quota policies and holds (`:990-1016`).
6. Re-validates the job/page claim (`:1018`).
7. Updates the page from `intent` to `send_committed` with all revision/fence predicates (`:1019-1033`).

A concurrent cancel writes `cancel_revision+1` in its own `BEGIN IMMEDIATE`. Whichever transaction commits first defines the winner:

- Cancel first: gate sees a different `cancel_revision` and aborts; page and B1 attempt remain `intent`. The current dispatcher must settle the pre-gate `not_sent` through the owner-fenced B1 settlement path; after a crash, the new owner does so during recovery. The command transaction never changes either row.
- Gate first: page is `send_committed` with `attempt_id`; cancel cannot cancel that page (`durable_job_store.py:726-728`); send stands.

This proof is valid **only if** page `intent` is written by reserve and gate requires `intent`. The accepted legacy implementation instead gates from `claimed`; its historical rows are not covered by this new-contract proof and may use `not_sent` only under the executable legacy-contract proof in §6.3. If that exact history cannot be established, quarantine; never infer from `claimed` alone.

### 5.4 Required changes to the boundaries

- **Reserve must update page and job projection in the same transaction.** Insert the attempt, then `UPDATE durable_job_pages SET state='intent', attempt_id=candidate_attempt_id, revision=revision+1 WHERE ... state='claimed' AND attempt_id IS NULL AND claim_seq=? AND owner_epoch=? AND revision=? AND EXISTS (SELECT 1 FROM durable_jobs j WHERE j.job_id=? AND j.state='running' AND j.cancel_revision=? AND j.revision=? AND j.authorized_owner_epoch=?)`; validate/update the corresponding job projection/revision under the same predicates. If any row count is not exactly one, raise `job_dispatch_claim_stale`; attempt and all projection writes roll back together. Every later projection preserves this ID; the B1 attempt, page identity, and job/page projection cannot be committed separately.
- **Gate must validate and update `intent → send_committed`, not `claimed → send_committed`.** `_validate_job_page_claim` needs a `for_gate=True` mode accepting `state='intent'` and `page.attempt_id == claim.candidate_attempt_id`.
- **`Reservation` must return the page revision** (and/or the updated claim) so the gate uses the post-reserve revision. The reserve receipt (`operations.result_json`) must include `page_revision`, `claim_seq`, the original `claim_cancel_revision`, and the attempt's `owner_epoch` for job-managed attempts; otherwise a replayed reserve/recovery cannot reconstruct the gate input or the canonical settlement payload. `DispatchCall.job_claim` can be replaced with the updated claim after `prepare`.
- **`record_outcome` must project the page and roll up the job in the same transaction**, or a new `record_job_outcome` must. Do not use two transactions (B1 terminal + page still `send_committed` is an invalid visible state). The projection must return relevant blocked scope IDs/reconciliation causes (unknown or overage) so the job can become `blocked` rather than falsely `completed` or `cancelled`.
- **`register()` must adopt an already-registered B3.2 run idempotently, without depending on the operation ID used originally.** Proposed algorithm:
  1. Validate the client and grant exactly as today (`durable_dispatch.py:330-337`).
  2. Read-only verification first: under the ledger connection, load `runs` by `run.run_id` and `documents` by `(tenant_id, document_id)`. Adopt only if **all** hold: `runs.body_json == canonical(asdict(run))`, `runs.run_id`/tenant/document match, `documents.active_run_id == run.run_id`, and `documents.generation == run.generation`.
  3. If the verification passes, the run is already durably registered: mark the client registered and do **not** call `register_run`. No second ledger write is needed.
  4. If no run row exists, call `ledger.register_run(owner, uuid5(UUID(run.run_id), "register").hex, run)` as a genuinely new registration.
  5. If a run row exists but any verification fails — different body, different generation, `documents.active_run_id` points elsewhere, or the document is newer — raise `generation_conflict`/`run_identity_conflict`. Never overwrite an active run or adopt a stale run.
  6. Re-run the read-only verification after a `register_run` conflict before surfacing the error, so a concurrent identical registration is adopted rather than failed.
  This preserves exact-replay behaviour when the original ID was deterministic, and makes adoption work when it was not. `register_run` itself must not be made to accept a stale generation; the adoption decision belongs to `register()`.

- **Cancel must implement the transaction rules in §5.5.** It increments `cancel_revision` and immediately cancels only pages with `attempt_id IS NULL` and no B1 attempt. It must not mutate attempts/pages that already have a B1 attempt, invoke `record_outcome`, or settle `intent`; owner-fenced current-dispatch settlement handles gate rejection, and new-owner recovery handles a crash.
- **Owner adoption and `recover_unsettled` must complete before serving**, and must not blanket-`unknown` job-managed intents before classification. A running job whose authorization epoch is prior to the acquired epoch is first adopted through A3; the scheduler remains closed through adoption, evidence publication recovery, attempt classification, and the final epoch-consistency barrier.
- **Durable-job acquisition must use `dispatcher_for_jobs()` from the actual `DurableDispatcher` constructor/runtime path.** Keep `dispatcher()` unchanged for B2 only. The durable runner's startup order is: acquire non-mutating owner → adopt prior-epoch running jobs → publication recovery → classify/settle old attempts → release stale no-attempt claims → verify epoch barrier → enable claims. No constructor or runtime helper may invoke legacy blanket conversion or schedule claims before that barrier.
- **`_write` must not be described as owner-fenced.** Add explicit crash-receipt read-back for command commits; do not pretend it is the same boundary as `_transaction`.

### 5.5 Cancellation transaction rules (normative)

#### 5.5.1 What the accepted `cancel()` actually does

`DurableJobStore.cancel()` (`durable_job_store.py:685-739`) runs one `BEGIN IMMEDIATE` command transaction and currently:

1. Replays the command receipt for the same `operation_id`, or rejects a terminal job and a stale `expected_revision` (`:696-713`).
2. Computes `safe_preintent = state == 'preparing_input' AND every page.attempt_id IS NULL` and `safe_blocked = state == 'blocked' AND every page is non-unknown AND page.attempt_id IS NULL` (`:715-717`).
3. Chooses `cancelled` only for those two cases; otherwise `cancelling` (`:718`).
4. Always bumps `cancel_revision` (`:721`).
5. Updates pages **only when the target is `cancelled`**, with `state IN ('queued','quota_wait','blocked')` and `attempt_id IS NULL` (`:725-728`). `claimed` is excluded. Intent pages are never touched. A `running` job therefore never cancels its already-safe pages immediately; they wait for recovery/roll-up.
6. Writes the job row and the command receipt in the same transaction, then removes pending execution artifacts only if the job reached `cancelled` (`:722-738`).

This is safe with respect to B1 authority: it does not revoke a send or mutate an attempt, and leaving `intent` untouched is correct. It is incomplete because it leaves no-attempt `claimed` pages in a `cancelling` job, fails to cancel other safe pages immediately, and does not consistently derive the job target from unresolved financial outcomes. Revision 3 keeps the command/settlement separation and makes the current dispatcher or recovery own `intent` settlement.

#### 5.5.2 Normative cancel transaction

Cancel is one command-side `BEGIN IMMEDIATE` transaction. It must not take the owner lease, call `record_outcome`, mutate B1 `attempts`, open a nested transaction, or perform filesystem I/O. It owns cancellation intent (`cancel_revision`) and no-attempt page cancellation; it does **not** own B1 settlement. The transaction performs these steps in order:

1. **Replay / guards.** Exact command replay returns the prior receipt and does not bump anything. Otherwise require the job to be nonterminal and `revision == expected_revision`.
2. **Fence first.** Increment `cancel_revision` and job revision before any page decision. The gate reads `cancel_revision`; after this commit, a gate not already committed must fail. A gate that committed first remains authorized and must proceed to transport/settlement.
3. **Cancel no-attempt pages immediately.** For every page where `attempt_id IS NULL` **and** `state IN ('queued','quota_wait','claimed','blocked')` **and** no row exists in `attempts` for `(run_id, page_number)`: set `state='cancelled'`, `safe_code='cancelled'`, `revision=revision+1`. The `NOT EXISTS` test is mandatory: a legacy page can be `claimed` with `attempt_id IS NULL` while a B1 `intent` already exists, and that page must not be cancelled here.
4. **Never mutate an attempt-bearing page or its B1 attempt.** If a page has a non-NULL `attempt_id` or a matching B1 attempt exists, leave its page state, `attempt_id`, and B1 row unchanged. This explicitly includes `intent`, `send_committed`, `unknown_outcome`, and a legacy `claimed` page with a B1 intent. Cancel never calls `record_outcome` and never changes `intent → not_sent`.
5. **Choose the job target from the post-step state.** Read (do not mutate) the job-relevant billing/quota scopes as defined in §4.1. If any page is `unknown_outcome`, any financial result is otherwise unknown, or any job-relevant scope is blocked/reconciliation-required (including overage), set/keep the job `blocked`; do not report `cancelled`. Else, if any attempt-bearing page is unsettled, including `intent` or `send_committed`, set the job `cancelling`. Set `cancelled` only after every B1 attempt is settled, every page is terminal/cancelled, and no relevant scope requires reconciliation. This applies equally to `preparing_input`, `queued`, `running`, `paused_restart`, and `blocked`.
6. **Receipt and cleanup.** Write the command receipt in the same transaction. Remove pending execution artifacts only after the job is safely `cancelled`; never delete evidence.

After cancel commits, an already-running dispatcher observes the new `cancel_revision`. If its reserved attempt is still `intent`, the gate rejects and the dispatcher **itself** settles B1 `intent → not_sent` through the owner-fenced settlement API, atomically projecting page `cancelled` (retaining `attempt_id`) and the job roll-up. If the process crashes first, recovery under the newly acquired owner epoch performs that B1 settlement. Neither path is part of the command transaction; neither may resend the request.

#### 5.5.3 Scenario matrix

“Immediate” means in the cancel transaction. “Deferred” means B1 settlement/recovery/B6.

| # | Snapshot at cancel | Immediate page actions | Job target | Deferred dependency |
|---|---|---|---|---|
| C-01 | `preparing_input` or `queued`; every page has `attempt_id IS NULL` and no B1 attempt | All pages in `queued/quota_wait/claimed/blocked → cancelled` | `cancelled` | None. A worker holding a render claim may finish its current render; its reserve fails on state/revision/cancel revision and it exits |
| C-02 | `running`; some `claimed` pages with `attempt_id IS NULL` and no B1 attempt; no attempt anywhere; no `send_committed` | All no-attempt pages `→ cancelled`, including `claimed` | `cancelled` | The worker’s claim is logically revoked; `release_claim` must tolerate an already-cancelled page and must not resurrect it |
| C-03 | `running`; one or more `intent` pages with matching `intent` attempts | Cancel only no-attempt pages; leave every `intent` page and B1 attempt unchanged | `cancelling` | Gate rejects on `cancel_revision`; current dispatcher owner-fenced settles `not_sent` and atomically projects page `cancelled`; recovery does so after crash |
| C-04 | `running`; `claimed` page with an existing B1 `intent` (legacy) | Do **not** cancel or mutate the attempt-bearing page; cancel only no-attempt pages | `cancelling` | Recovery R-02 may prove pre-gate `not_sent` only with positive legacy-gate proof; otherwise R-20 quarantines; retain `attempt_id` |
| C-05 | Any nonterminal state; one or more `send_committed` pages | Cancel no-attempt pages; leave send-committed page/attempt unchanged | `cancelling`, unless a relevant scope already requires reconciliation | Authorized POST settles through B1; definitive outcomes lead to `cancelled` only if no relevant scope remains blocked; unknown/overage changes job to `blocked` |
| C-06 | Any nonterminal state; an `unknown_outcome` page | Cancel safe no-attempt pages; leave unknown page/attempt unchanged | `blocked` | B6 alone reconciles and unblocks. Cancel never reports this job as `cancelled` while financial uncertainty remains |
| C-07 | `blocked`; every page non-unknown, `attempt_id IS NULL`, no B1 attempt | All pages `→ cancelled` | `cancelled` | None |
| C-08 | Cancel races `reserve` | SQLite serial order. Cancel first: reserve fails and inserts no attempt. Reserve first: page and B1 attempt atomically become `intent`; cancel increments revision but leaves both unchanged | `cancelled` if no attempt was created; otherwise `cancelling` | If reserve committed first, gate rejects on cancel revision; dispatcher or recovery settles `not_sent`; command never owns the settlement |
| C-09 | Cancel races `gate` | Cancel first: gate aborts; page/attempt remain `intent` until owner-fenced `not_sent` settlement. Gate first: page is `send_committed` and C-05 applies | `cancelling`, then `cancelled` on definitive settlement or `blocked` on unknown | Exactly one SQLite winner; no partial page state or duplicate POST |
| C-10 | Cancel while a worker is rendering a claimed page with no B1 attempt | Page `→ cancelled`; `cancel_revision+1`; no B1 row exists | `cancelled` if no other attempt remains | Worker’s reserve predicate fails; no attempt, no POST; stale release cannot resurrect the page |
| C-11 | Cancel after a job attempt settled with overage and a relevant billing/quota scope is blocked | Increment `cancel_revision`; cancel only safe no-attempt pages; do not clear or rewrite any scope | `blocked`, never `cancelled` | Scope membership comes from this job's persisted B1 attempt bindings; B6 reconciliation/unblock only |

**Replay/idempotency:** a repeated cancel with the same `operation_id` returns the original receipt and must not bump `cancel_revision` again or re-cancel pages. A different `operation_id` on an already-terminal job is `job_not_cancellable`.

**Alignment requirement:** B3.4a must change `DurableJobStore.cancel()` to this matrix. The accepted tests that assert `send_committed` survives cancel remain valid; the current exclusion of `claimed` and the current “only when target is cancelled” page update do not.

### 5.6 Settlement winner, `finish()` idempotency, and owner fencing

For each B1 attempt, there is one authoritative settlement winner. The winner is the first valid owner-fenced B1 settlement transaction that changes the attempt from its expected prior state and atomically writes the matching page projection, job roll-up, and immutable settlement receipt. Reserve, gate, cancel, finish, and recovery serialize through SQLite plus the dispatcher owner epoch; cancellation is a gate fence, not a financial settlement.

`finish()` MUST obey these rules:

1. **First commit wins.** A pre-gate `not_sent` settlement, a post-gate terminal settlement, or a post-gate `unknown` settlement are mutually exclusive outcomes for the same attempt. The transaction predicates on attempt state, immutable `attempt_id`, page identity/revision, and current `owner_epoch`; it cannot replace one committed outcome with another.
2. **Public `finish()` is single-use.** Preserve the `DispatchCall.finished` guard: after the first public call, another `finish()` on the same `DispatchCall` raises `dispatch_call_finished` before invoking settlement. Public finish replay is not the idempotency contract and must never perform a second settlement call.
3. **B1 settlement is independently idempotent.** At the B1 settlement/receipt layer, an exact repeat with the same canonical operation ID and payload returns the existing receipt read-only. This is the replay path for ambiguous commit receipts and recovery; it is tested separately from the public `finish()` guard.
4. **Digest conflict never overwrites.** Replaying the same attempt/operation ID with a different canonical settlement digest returns `historical_outcome_conflict` (or equivalent integrity error). The original receipt, B1 state, page projection, and job roll-up remain unchanged. A different operation ID may not be invented for the same attempt settlement.
5. **Late worker after recovery is fenced.** Once recovery owns a newer epoch, the old worker cannot write settlement, project a page, or overwrite the recovery receipt. Recovery validates the attempt's persisted prior `owner_epoch` as immutable identity but performs any write under the current owner's nonce/epoch; it never rewrites `attempt.owner_epoch` to the new epoch. A read-only exact-receipt check may return the already-committed result if its digest is identical; a different digest is a conflict. If there is no matching receipt, the stale owner fails closed. Late final evidence after recovery classified `unknown` is B6 input, not permission for the old worker to settle.
6. **Cancel/reserve/gate order:** cancel first prevents reserve or gate by page/job predicates and `cancel_revision`; reserve first atomically creates B1 `intent` plus page identity, then cancel leaves both untouched and gate rejects; gate first commits `send_committed`, after which cancel cannot revoke the authorized POST.
7. **Finish/recovery order:** the current dispatcher can finish only while its owner epoch is valid. A successor cannot acquire the lease until the old owner loses it. If old-owner finish commits first, new-owner recovery reads the exact receipt at the B1 layer and proceeds without a second public finish. If recovery wins after owner loss, stale finish is fenced; an exact B1-layer replay is read-only or a differing payload conflicts. No second POST is allowed in either order.
8. **Publication is not settlement.** Final evidence publication can precede the settlement transaction and be replayed, but only B1 + page/job transaction commits the financial outcome. A `.pending` file never wins settlement; verified publication recovery must first create and validate final.
9. **Legacy projection repair is not settlement.** For historical `claimed + not_sent`, cancel-winning `cancelled + not_sent + NULL attempt_id`, `send_committed + terminal`, and `claimed + unknown + NULL attempt_id` rows, a dedicated repair validates the case-specific immutable B1 facts and receipts (§5.6.2) and changes only page/job projection. It never invokes `record_outcome`, creates a settlement operation, or changes B1 attempt/receipt/evidence fields. New B3.4 settlement remains S1 and is atomic across B1/page/job.

### 5.6.1 Canonical settlement identity and payload

Every B3.4 B1/page/job settlement for one attempt uses exactly one deterministic operation ID:

```text
uuid5(UUID(attempt_id), "settlement").hex
```

No separate `recovery-not-sent` operation namespace is permitted. Current-dispatcher settlement after a cancel-winning gate rejection and new-owner recovery of the same pre-gate `not_sent` outcome MUST pass the identical canonical request payload and this same operation ID. A retry after `CommitReceiptUncertain` first reads the receipt using that pair; only if no matching receipt exists and the attempt is still in the expected prior state may it retry the same pair. A distinct ID for the same logical settlement is forbidden.

The canonical payload is deterministic JSON (sorted keys, normalized scalar encodings) over the immutable attempt identity and logical projection, at minimum:

```text
version, operation_id, attempt_id, run_id, job_id, generation,
page_number, attempt_no, claim_seq, request_digest,
prior_owner_epoch, expected_attempt_state, resulting_attempt_state,
claim_cancel_revision,
proof_or_evidence_digest,
page_projection { state, safe_code, result_bytes }
```

For pre-gate `not_sent`, `prior_owner_epoch` comes from the persisted B1 attempt, not the caller; `claim_cancel_revision` comes from the immutable reserve claim/receipt; `expected_attempt_state=intent`; `resulting_attempt_state=not_sent`; `proof_or_evidence_digest` is the deterministic pre-gate proof digest; and the page projection is derived from the durable cancellation decision (`cancelled/cancelled_pre_gate` when cancel won, otherwise `failed/not_sent_pre_gate`). Thus current worker and recovery produce byte-identical payloads for equivalent outcomes. The aggregate job roll-up is recomputed and committed atomically from the transaction snapshot, but is not part of the attempt settlement digest because other pages can settle independently. Exclude the caller path, settling owner epoch, current job revision/cancel revision, wall-clock/boot settlement timestamps, retry count, and filesystem publication path; those vary across exact replays and are not settlement identity.

The B1 operation receipt binds `fingerprint(canonical_payload)`. Exact payload replay returns that receipt without changing settlement timestamps or page/job revisions. Any payload difference with the deterministic ID is a conflict, not a second settlement.

### 5.6.2 Legacy projection-repair idempotency (separate from settlement)

A legacy repair has **no new B1 operation ID or settlement receipt**. Its deterministic input is the existing immutable B1 attempt, page identity, expected page/job revisions, and the immutable receipt(s)/proof required for that specific case — not a blanket settlement receipt:

| Case | Required immutable input | Outcome settlement receipt |
|---|---|---|
| R-23 | Immutable reserve receipt + canonical historical `not_sent` outcome receipt | Required |
| R-25 | Immutable reserve receipt + canonical `not_sent` outcome receipt + historical cancel-winning proof, including the durable cancel command receipt | Required |
| R-27 | Immutable reserve receipt + canonical terminal outcome receipt + valid matching final evidence | Required |
| R-30 | Immutable reserve receipt + exact identity of the persisted B1 attempt/page/run/claim and its original `owner_epoch` | **Absent and not required** |

The legacy blanket `dispatcher()` converted `intent → unknown` with a direct SQL update after `_block_attempt`, without `record_outcome()` and without updating `durable_job_pages` (`coordination.py:430-454` at the accepted base). For R-30 that absence is expected, not an integrity mismatch. If an outcome receipt is unexpectedly present for such an attempt, its meaning must be verified against the immutable attempt facts; an unsupported or contradictory combination quarantines rather than being guessed at.

Its transaction is owner-fenced and uses compare-and-set predicates over the exact legacy source states. It writes only the target page projection and recomputed job roll-up; it does not write or replay a B1 outcome.

If the commit receipt is lost, recovery rereads the page, job, B1 attempt, and the case-specific immutable receipt(s). If the page/job equal the exact derived target and all immutable B1 facts and required receipts still match, the repair is complete and the retry is read-only. If the old source state remains and all preconditions still match, the same projection-only CAS may be retried. Any partial/conflicting projection, missing/mismatched required receipt (other than the expected-absent R-30 outcome receipt), evidence conflict, or changed revision quarantines; it never causes a new settlement operation. This target-state contract makes repair restartable without weakening the new settlement atomicity rule.

---

## 6. Recovery

### 6.1 Recovery phases

1. **Acquire the durable-job dispatcher lease through `CoordinationLedger.dispatcher_for_jobs()`**, called by `DurableDispatcher`'s `job_managed=True` constructor/runtime path. This path locks and advances the owner epoch without mutating any B1 attempt or job row. Do not call legacy `dispatcher()` here: its B2 blanket `intent→unknown` conversion is preserved for B2 only and must not run before durable-job recovery. Close scheduler admission.
2. **Adopt prior-epoch `running` jobs** in bounded owner-fenced transactions (A3). For each row with non-NULL `authorized_owner_epoch < current_epoch`, condition on the current row revision/state and current active run/generation, then update only `authorized_owner_epoch=current_epoch` and job revision. Do not rewrite B1 attempts, page identity, or cancellation state. A NULL/future epoch, unrecognized job state, or active-run mismatch is an integrity contradiction: keep admission closed and quarantine/fail closed. `cancelling` jobs need no scheduler authorization adoption; recovery processes their attempts under the current owner fence while retaining cancellation. A crash before a job's adoption commit leaves it eligible next run; a crash after commit is recognized by the epoch equality. A later owner epoch repeats the same guarded operation safely.
3. **Publication recovery (filesystem only):** inspect `.pending` candidates; validate private path/ACL, envelope, identity, size and hash. If valid and conflict-free, safely publish the immutable final path (or verify an identical final already exists), validate the final path independently, then remove the pending link. Do not update B1/page/job state in this phase.
4. **Financial evidence indexing:** scan only validated final files into `EvidenceIndex[attempt_id]`. A `.pending` file alone is never indexed as financial evidence. Never hold SQLite while hashing/reading files.
5. **Classify prior-owner attempts** in bounded `_transaction(current_owner)` batches. New B3.4 financial settlement requires valid final evidence; strict pre-gate `not_sent` is an owner-fenced control settlement. First recognize the exact legacy terminal and quarantine states in §6.2: these may receive only projection repair, never another B1 settlement. The `attempt.owner_epoch` remains its original immutable value and is checked against persisted reserve identity, not replaced with the current owner epoch.
6. **Settle or repair, without conflating them.** New settlement commits B1/page/job together. Legacy projection repair writes page/job only after validating the case-specific immutable B1 fact and receipts/evidence/cancel proof from §5.6.2 — an already-terminal B1 fact plus its outcome receipt for R-23/R-25/R-27, or the immutable `unknown` fact plus reserve receipt for R-30, where no outcome receipt exists. B1 is read-only. Repeated recovery recognizes stable exact receipt/state pairs and performs only read-only no-op for already repaired rows.
7. **Release prior-epoch stale claims with no attempt** (`claimed → queued`, clear page `owner_epoch`). Never reclaim a claim in the current epoch; a live worker may still own it.
8. **Run the startup barrier**: verify every `running` job has `authorized_owner_epoch=current_epoch`; all prior-epoch attempts are in a stable matching receipt/state pair or explicitly quarantined; no stale no-attempt claim remains; publication scan and recovery batch are complete. Only then enable scheduler claims. Construction/acquisition alone never enables claims.
9. **Crash during adoption/recovery** → next acquisition repeats from the beginning with its newer epoch. Per-job adoption is a guarded idempotent update; per-attempt settlement uses the canonical operation ID/payload; already stable rows are not re-settled. Scheduler admission remains closed until the new barrier passes.

### 6.2 Recovery decision table

Precedence is top-to-bottom. “Final evidence” means a valid, hash-sealed envelope at the immutable final path whose embedded identity matches the B1 attempt and page. `.pending` is considered only in publication recovery before this table; it never satisfies the Evidence column by itself.

| # | Page state | B1 attempt state | Evidence | Decision | B1 action | Page action | Job action | Idempotency |
|---|---|---|---|---|---|---|---|---|
| R-01 | `claimed` | none | none | Stale pre-intent claim; safe release | none | `claimed → queued`, clear `owner_epoch`, revision+1 | Keep `running`/`queued`; scheduler may reclaim | — |
| R-02 | `claimed` | `intent` | no valid final | **Strict pre-gate `not_sent` only under the executable positive legacy-gate proof in §6.3.** The schema-v2 rows alone do not identify the historical binary/contract; absent verified deployment provenance or exact matching contract, quarantine as R-20 | New-owner-fenced `intent → not_sent` using the single canonical settlement ID/payload only when every §6.3 predicate passes; no scope block | Attach immutable attempt ID; `claimed → failed`, or `→ cancelled` if cancel already won | Roll up; if cancelling and no other unsettled attempt, `cancelled`; otherwise safe `paused_restart`/error; no auto-retry | — |
| R-03 | `intent` | `intent` | no valid final; no unresolved pending publication | **Strict pre-gate `not_sent`** only when every common §6.3 predicate and the verified new-gate `intent` marker proof hold | New-owner-fenced `intent → not_sent` with the canonical ID/payload and proof digest; no scope block | Preserve attempt ID; `intent → failed`, or `→ cancelled` if cancellation is pending | Roll up; if cancelling and all attempts settle without unknown, `cancelled`; otherwise `paused_restart`/error | — |
| R-04 | `intent` | `intent` | valid final | **Contradiction** (final response evidence conflicts with page proving no gate) | Do not treat final as ordinary success; quarantine as unknown | Preserve attempt ID; `intent → blocked` / `unknown_outcome` | `blocked`; B6 | — |
| R-05 | `send_committed` | `intent` | valid final | Final evidence proves the response; settle it | B1 `intent → terminal`, outcome from final evidence, actual units | Preserve attempt ID; `send_committed → completed/incomplete/failed` | Atomic B1/page/job projection; if cancel pending, `cancelled` only after all attempts settle | — |
| R-06 | `send_committed` | `intent` | no valid final / invalid final | Send authorized; financial outcome unknown | B1 `intent → unknown`, block scopes | Preserve attempt ID; `send_committed → unknown_outcome` | Job `blocked` even if cancelling; B6 | — |
| R-07 | `send_committed` | `unknown` | valid final | Late evidence for unknown is B6 reconciliation input only | No B3.4 settlement or B1 state change | Keep `unknown_outcome` | Keep job/scope blocked until B6 | — |
| R-08 | `send_committed` | `unknown` | none/invalid | Already ambiguous | Leave `unknown` | `send_committed → unknown_outcome` | `blocked` | — |
| R-09 | `completed`/`incomplete`/`failed` | `terminal` | valid | Page terminal; only roll-up may be missing | none | none | Recompute roll-up | — |
| R-10 | `completed`/`incomplete`/`failed` | `terminal` | missing/invalid | Terminal B1 but no reconstructible result | Leave terminal | If the page is claiming success, downgrade to `failed` with `safe_code='evidence_missing'`; otherwise keep terminal | Roll up as error (`completed_with_errors`/`failed`) | — |
| R-11 | `unknown_outcome` | `unknown` | any (valid, absent, or invalid) | Stable unknown; valid late evidence remains B6-only reconciliation input | No B3.4 settlement or state change | Keep `unknown_outcome`, retain attempt ID | Keep job/scopes blocked until B6 | — |
| R-12 | `cancelled` | `not_sent` | none | Already settled pre-gate cancellation | No B1 mutation; verify deterministic receipt/digest | Keep `cancelled` and retain `attempt_id` | Roll up only if needed | — |
| R-13 | `intent` | `intent` | no valid final; pre-gate proof holds | Job `cancelling` | New-owner fenced `intent → not_sent`; preserve attempt ID; no retry | `intent → cancelled` with cancellation safe code | `cancelled` only after every attempt settles and no unknown; otherwise `cancelling` | — |
| R-14 | `blocked` | none | none | Policy/source block | none | Keep `blocked` | `blocked`; explicit new generation | — |
| R-15 | `queued`/`quota_wait` | none | none | Schedulable | none | none | Keep active | — |
| R-16 | none (legacy, no page) | `intent` | none | Legacy B2 conservative path | `intent → unknown`, block | none | Not a durable job | — |
| R-17 | `send_committed` | `intent` | valid final | Job `cancelling` | Settle from valid final under new owner; preserve attempt ID; no retry | Project definitive terminal page | `cancelled` only after every attempt settles and no unknown | — |
| R-18 | `claimed` | `intent` | valid final | Contradiction: legacy claimed state is the pre-gate proof candidate, but final response evidence contradicts it | Do not ordinary-settle success or `not_sent`; quarantine `intent→unknown` and block relevant scopes | Attach immutable candidate attempt ID; `claimed→blocked`, `safe_code='recovery_evidence_conflict'` | `blocked`; preserve final and route to B6 | — |
| R-19 | `blocked` | `intent` | any (including none) | State/attempt mismatch is not pre-gate proof; quarantine, never infer no-send or success from `blocked` | `intent→unknown`; block relevant scopes; no terminal financial settlement | Retain/attach immutable attempt ID; keep page `blocked` with recovery-conflict safe code | `blocked`; B6 only | — |
| R-20 | `claimed` | `intent` | no valid final | Claimed/intent mismatch without positive legacy gate proof; no pre-gate inference | `intent→unknown`; block relevant scopes; no terminal financial settlement | Attach immutable attempt ID; `claimed→blocked`, `safe_code='recovery_state_conflict'` | `blocked`; B6 only | — |
| R-21 | `blocked` | `unknown` | any (valid, absent, or invalid) | Stable quarantine result, whether produced by B3.4 or inherited; never reclassify as success/not_sent | Verify the deterministic unknown settlement receipt/digest if present; exact read-only replay/no-op only. If receipt is absent/mismatched, retain B1 `unknown`, quarantine/alert; create no new settlement operation | Keep `blocked`, preserve attempt ID and safe code; do not project success or `not_sent` | Keep job/scopes blocked; never clear scope flags; B6 only | — |
| R-22 | `failed` | `not_sent` | none; invalid/unresolved evidence or valid final is contradiction | Stable pre-gate settlement only if the single canonical settlement receipt and digest match the `not_sent` result/page projection | Verify canonical receipt/digest; exact replay/no-op only. Missing/mismatched receipt is integrity quarantine; do not create a second ID or change B1 financial state. Conflicting evidence is preserved for B6 quarantine | Keep `failed` and immutable attempt ID unless a contradiction requires blocking the job; never turn into success/cancelled | Recompute only a consistent roll-up; contradiction stays blocked/quarantined | — |
| R-23 | legacy `claimed`, `attempt_id IS NULL` | B1 `not_sent` | no final evidence and no `.pending` candidate | **Projection-only repair** to ordinary pre-gate failure. Verify exact candidate/attempt ID, run/page/job/generation, claim sequence, original owner epoch, full immutable attempt body, immutable reserve receipt, and canonical historical outcome settlement receipt (`operation_id=uuid5(UUID(attempt_id), "settlement").hex`, `kind=outcome`, exact canonical request/payload digest and `result_json` for `not_sent`). B1 `not_sent` is immutable | No B1 write/replay and no new operation. In one owner-fenced SQLite transaction attach `attempt_id=candidate_attempt_id`, set page `failed`/`not_sent_pre_gate`, retain claim identity, and recompute job roll-up | `claimed→failed`, attempt ID attached; no retry | Derived roll-up; no scope unblock | Deterministic page/job target is repair marker; repeat is read-only no-op |
| R-24 | legacy `claimed`, `attempt_id IS NULL` | B1 `not_sent` | final evidence exists, `.pending` exists, or receipt/identity is missing or mismatched | Integrity contradiction; preserve immutable B1 `not_sent`; quarantine page/job | No B1 mutation, no settlement ID/receipt creation, no success projection | Set/keep page/job `blocked` only under the quarantine transaction; preserve artifacts; attach attempt ID only if persisted attempt identity itself is exact | `blocked`; no retry | Repeated recovery verifies the same conflict read-only |
| R-25 | legacy `cancelled`, `attempt_id IS NULL` | B1 `not_sent` | no final or pending evidence; exact B1 reserve and `not_sent` outcome receipts plus historical cancellation proof | Projection-only repair of the legacy cancel-winning branch. Verify full immutable attempt/page/run/claim identity, immutable reserve receipt, canonical `not_sent` outcome receipt, exact old `record_outcome` contract that wrote `cancelled`, and a matching immutable durable cancel command receipt/cancel revision proving cancel won | No B1 write/replay/new operation. Owner-fenced transaction attaches `attempt_id=candidate_attempt_id`, preserves page `cancelled`, and recomputes job roll-up | Keep `cancelled`, attach immutable ID; B1 remains `not_sent` | Roll up only from consistent pages/scopes | Derived target is repair marker; repeat is read-only no-op |
| R-26 | legacy `cancelled`, `attempt_id IS NULL` | B1 `not_sent` | cancellation proof/receipt absent or mismatched, conflicting final/pending evidence, or identity mismatch | Quarantine; do not infer that cancel won from the page label alone | Preserve B1 `not_sent`; no new settlement operation | Page/job `blocked`; do not rewrite B1 or evidence; link attempt ID only if identity independently matches | `blocked`; no retry | Repeated recovery is read-only conflict verification |
| R-27 | legacy `send_committed` | B1 `terminal` | exact immutable reserve receipt, canonical terminal outcome receipt, and valid final evidence matching attempt, outcome and evidence digest | **Projection-only repair**, not new B3.4 settlement. Verify page/B1 identity, run/page/claim sequence, reserve receipt, terminal outcome receipt and final evidence; derive only the page terminal classification and result metadata from that evidence | No B1 write/replay/new operation; preserve attempt state, owner epoch, evidence digest and receipt. Owner-fenced transaction writes page/job projection only | `send_committed→completed/incomplete/failed`, attempt ID unchanged | Recompute job roll-up | Exact derived target plus same receipts/evidence is read-only no-op on repeat |
| R-28 | legacy `send_committed` | B1 `terminal` | final evidence missing, corrupt, wrong-attempt, or outcome-inconsistent | Quarantine the page/job; a terminal B1 row alone does not prove which page result can be projected | Preserve B1 terminal state, owner epoch, evidence digest and receipt; no new settlement | Page/job `blocked`; preserve all evidence/materials; never project success | `blocked`; no retry | Repeated recovery verifies immutable facts only |
| R-29 | legacy `send_committed` | B1 `terminal` | receipt missing/mismatched, request digest/result differs, or evidence digest differs | Integrity quarantine, even if a result file parses | Preserve B1 terminal state and all existing receipts; create no replacement receipt/operation | Page/job `blocked`; preserve evidence and metadata | `blocked`; no retry | Repeated recovery is read-only; no repair until external investigation |
| R-30 | legacy `claimed`, `attempt_id IS NULL` | B1 `unknown` after accepted legacy blanket conversion | any final/absent/invalid evidence | Recovery-only quarantine projection. Verify the candidate/attempt ID, job/run/page/generation, claim sequence, original owner epoch, B1 attempt body and immutable reserve receipt exactly. **No outcome settlement receipt exists or is required**; its absence is not an integrity mismatch. If one is unexpectedly present, verify its meaning against the immutable attempt facts; an unsupported/contradictory combination quarantines | Preserve B1 `unknown` and existing scope blocks; no outcome settlement, operation ID, receipt, `unknown→not_sent`, or unblock | Attach `attempt_id` only when attempt identity is exact; set page `blocked`, recompute job `blocked`; preserve/index final evidence for B6 only | `blocked`; no retry | After the first exact projection, repeated recovery is read-only identity/state verification; no financial write |

**Stable pair versus repair input versus contradiction:** R-07/R-08/R-11/R-21 are stable quarantine pairs; R-12/R-22 are stable pre-gate terminal pairs with retained attempt IDs. R-23/R-25/R-27 are legacy projection-repair inputs for already-terminal B1 results and require an existing canonical outcome settlement receipt; R-30 is an unknown quarantine projection input and expects no outcome receipt because the legacy blanket conversion never wrote one. None is a new settlement. Exact matching inputs receive only the specified page/job projection repair. Any receipt/digest mismatch or unlisted page/B1 pair is an integrity contradiction: do not infer `not_sent` or success, do not mint another operation ID, do not unblock; quarantine/fail closed while preserving the B1 result. A later valid final for B1 `unknown` remains B6 input.

### 6.3 Pre-gate `not_sent` proof (normative)

A pre-gate `not_sent` classification is valid only if **all** common predicates and the applicable page-state proof below hold. This is a positive proof, not a default interpretation of missing evidence.

**Common predicates:**

1. B1 attempt state is exactly `intent` (not `unknown`; the trigger forbids `unknown → not_sent`). The persisted attempt body, run/page identity, `attempt_id`, `attempt_no`, `claim_seq`, request digest, binding and original `owner_epoch` are internally consistent.
2. The immutable reserve operation receipt, when required by that reserve contract, is present and its operation kind, request digest, attempt body/result, attempt ID, run/page identity and original owner epoch match the persisted B1 intent exactly. A missing or mismatching financial reserve receipt is not proof of no-send.
3. Publication recovery is complete. There is no valid final evidence and no unresolved `.pending` candidate that could still be promoted. Pending-only is not financial proof; unresolved evidence contradiction is quarantined.
4. The applicable gate contract has an irreversible durable gate marker: a successful gate commit atomically moves the page out of the pre-gate state, and no legal later transition can restore the same page/claim to that pre-gate identity. Where the contract writes a successful gate receipt, that receipt is also checked; absence of a receipt alone is never proof.
5. Settlement is written by the current owner under its live owner fence. For prior-owner attempts, validate (but never rewrite) the attempt's immutable original `owner_epoch`; do not call a prior-owner-only `record_outcome` path. B1 `intent→not_sent`, retained page attempt ID, page projection, job roll-up and deterministic receipt commit atomically with no scope block.
6. The proof digest is deterministic and caller-independent: `fingerprint({attempt_id, run_id, page, claim_seq, prior_owner_epoch, page_state, reason:"pre_gate_not_sent"})`. Current-dispatcher and recovery callers use the same canonical serializer, operation ID and payload (§5.6.1).

**New B3.4 page contract (`page=intent`):** The known job gate requires the exact page `intent` + matching `attempt_id`/candidate identity and atomically commits `intent→send_committed`. The persisted page is still `intent` with the same attempt identity, the B1 row is still `intent`, and no gate commit marker exists. That exact state under the verified new-gate contract proves the gate transaction did not commit. `send_committed`, `unknown_outcome`, `blocked`, or any identity mismatch fails this proof.

**Legacy R-02 page contract (`page=claimed`):** A `claimed + B1 intent` pair is eligible only when the exact historical gate implementation is identified as follows:

1. Use trusted, immutable deployment provenance for this ledger/installation (for example, an archived signed release manifest or immutable deployed image digest) to identify every build/source revision that could have executed the gate for this attempt, using its installation, original owner epoch and deployment interval. It is not enough to identify only the binary that created the B1 attempt: a later binary may have executed the gate. If more than one build could have gated it, every possible build must implement the same qualifying contract. Resolve the provenance to the archived gate implementation; do not infer it from the currently running code, the database schema version, or `page.state` alone. No schema-v2 column is added for this purpose.
2. The identified source must prove that the successful gate transaction used the matching attempt/run/page/job/owner/cancel predicates and atomically updated the durable page with the irreversible marker `claimed → send_committed`, conditional on the exact legacy pre-gate SQL predicate `page.state='claimed' AND page.attempt_id IS NULL` (plus the persisted claim identity/revision predicates); it must not permit any post-gate transition back to that same `claimed + attempt_id IS NULL` claim. If the verified contract has a durable gate receipt, validate its exact operation/body/result and require no successful receipt for this attempt. The atomic page marker is the required proof when that legacy contract has no gate receipt.
3. In the same SQLite snapshot, verify the persisted page remains exactly that legacy pre-gate row (`claimed`, `attempt_id IS NULL`, matching job/run/page/generation, claim sequence and owner epoch; page revision must match the persisted claim/reserve revision when that identified contract records it) and the matching persisted B1 `intent` plus reserve receipt pass the common identity checks above. Because every possible identified gate transaction would have irreversibly changed this row on successful commit, the exact persisted pre-gate marker proves that gate did not commit. A lone `page.state == 'claimed'` does not establish the contract, attempt identity, absence of a committed-then-restored marker, or financial receipt integrity and is insufficient.
4. If provenance is absent/ambiguous, the archived contract does not match this exact predicate/atomicity, a marker could have been restored, or a financial reserve receipt mismatches, positive proof is unavailable: follow R-20, quarantine as `unknown`/blocked, preserve evidence and attempt identity, and route to B6. Do not optimistically settle `not_sent`.

This definition is deliberately conservative: existing schema-v2 rows alone do not identify which binary/contract wrote a legacy page. If trusted external deployment provenance is unavailable, the legacy `claimed + intent` case cannot be positively proven and MUST quarantine.

The proof is **not** valid after the attempt has been marked `unknown`, and it is **not** valid for `send_committed`. That is why recovery must classify before any blanket update.

### 6.4 Unknown outcomes and quarantine

- A genuine post-gate unknown (`send_committed`, no valid final evidence) becomes `page=unknown_outcome`, `job=blocked`, and `billing_scopes.blocked=1` + `quota_scopes.blocked=1`. This rule overrides a pending cancel: `cancelling → blocked`, never `cancelled`.
- B3.4 must not attempt to clear `blocked`; `require_reconciliation_schema` returns “requires schema v3” (`coordination.py:1161-1166`). B6 owns the administrative release.
- The scheduler must skip a blocked job/user/scope without spinning. A blocked scope is a quarantine, not a retry signal.
- Late final evidence for an `unknown_outcome` page is a B6 reconciliation input; B3.4 may preserve/index it but must not auto-settle the B1 attempt, page, job, or scope.

### 6.5 Idempotency and bounded recovery

- Every B3.4 settlement (including pre-gate `not_sent` by current dispatcher or recovery) uses the single §5.6.1 ID `uuid5(UUID(attempt_id), "settlement").hex` and the same canonical-payload function. No caller-specific recovery operation ID is allowed.
- After an ambiguous commit receipt, read back by that operation ID and canonical payload digest before retrying. If exact receipt exists, return it; if no receipt exists and the attempt remains eligible, retry only with the same ID/payload; otherwise fail closed.
- Recovery closure is mandatory: `page=blocked + B1=unknown` and `page=unknown_outcome + B1=unknown` are stable quarantines (no settlement operation, B1/page/job mutation, success/`not_sent` projection or unblock); `page=failed + B1=not_sent` is stable pre-gate settlement (verify the canonical receipt/digest, then exact replay/read-only no-op only). A missing/mismatching receipt is integrity quarantine, not permission to mint another ID.
- Re-running recovery on any already settled attempt is a no-op or exact receipt replay only when the settlement digest matches. For legacy projection repair, validate only the case-specific immutable receipt(s) from §5.6.2 — outcome receipt for R-23/R-25/R-27, reserve receipt for R-30, plus the durable cancel command receipt for R-25 — and use the projection-only idempotency contract; never call settlement again. A conflicting evidence/settlement digest is `historical_outcome_conflict`; never overwrite B1 outcome, page projection, or receipt.
- R-30 (and the RC-36/RC-37 legacy blanket `intent → unknown` path) must not create an outcome settlement receipt and must not expect one: the historical conversion wrote none (`coordination.py:430-454`). The idempotency marker is the exact target `blocked` projection plus the immutable reserve receipt and persisted attempt/page/run/claim identity with its original `owner_epoch`. An unexpected outcome receipt for such an attempt is verified against the immutable facts; an unsupported/contradictory combination quarantines rather than being reconciled by guesswork.
- A late worker from a prior owner epoch cannot write or replace settlement. It may receive a read-only exact-replay result if the deterministic receipt and digest already match; a mismatch is a conflict, and otherwise owner fencing rejects it.
- Cancel never performs settlement. After a cancel wins, the current dispatcher settles gate-rejected pre-gate `not_sent` under its owner epoch; after crash, recovery under the new epoch does so.
- Process at most `RECOVERY_BATCH` (e.g., 50) attempts per transaction to bound lock time.
- Never hash a 1 GB PDF inside a SQLite transaction.
- Recovery must be restartable at any point; no in-memory-only state is authoritative.

---

## 7. Job-managed evidence recovery contract

### 7.1 Identity

`EvidenceIdentity` MUST be explicit, not inferred from a string convention:

```text
attempt_id           # 32 lowercase hex; equals page.candidate_attempt_id for job-managed
run_id
page_number
attempt_no           # 1 in B3.4
claim_seq            # 1 in B3.4; >1 only if a prior attempt identity exists (not supported by the no-retry rule)
job_id               # optional for legacy
generation           # optional for legacy
```

The evidence file name is `<attempt_id>.json` (`durable_dispatch.py:193-196`). The embedded `payload.attempt.attempt_id` must equal the requested ID (`:249`). For job-managed pages, the page row and the B1 attempt row must both agree with this identity.

### 7.2 Envelope

Current v1 envelope (`durable_dispatch.py:206-210`):

```json
{
  "payload": {
    "version": 1,
    "attempt": { ... },
    "owner_epoch": 7,
    "result": { ... },
    "outcome": { ... },
    "proof": {
      "attempt": "...",
      "state": "terminal",
      "usage": { ... } | null,
      "status": 200,
      "response_sha256": "...",
      "result_sha256": "...",
      "pricing_sha256": "..."
    }
  },
  "sha256": "..."
}
```

**Required B3.4 evidence contract changes:**

1. Allow `result: null` for a terminal error where no `AdapterPageResult` exists. The proof, outcome, usage, safe code, and HTTP status remain mandatory. This closes G17.
2. Add a `settlement` object with `page_state` (`completed`/`incomplete`/`failed`), `safe_code`, and `result_bytes` so recovery can project the page without guessing.
3. Keep `version=1` only if the reader is made tolerant of `result: null`; otherwise introduce `version=2` and read both. Do not silently reinterpret v1.
4. `write` must be idempotent: if `final` exists and the envelope is byte-identical, return; if `final` exists and differs, `result_evidence_conflict`; if `pending` exists, validate it and complete/remove it only under the same crash-safe protocol. This closes G18.
5. **Separate publication recovery from financial settlement.** A verified `.pending` envelope may be used only to safely finish publication: after strict identity/ACL/size/hash validation, publish without overwrite to the final path (or require an identical existing final), independently read/validate final, then remove the pending link. A collision with different bytes is an integrity conflict; preserve both artifacts and quarantine.
6. A `.pending` file by itself is never financial evidence and must never be passed to settlement. B1 terminal financial settlement accepts only a valid immutable final file. If publication recovery cannot produce and validate final, classify from B1/page state without trusting pending; a `send_committed` attempt without valid final becomes `unknown_outcome`/blocked.

### 7.3 Validation sequence

1. `verify_private_path(root, directory=True)` and root identity `(st_dev, st_ino)` unchanged (`:188-191`).
2. File name is exactly `[0-9a-f]{32}.json`; no symlink/reparse; private ACL.
3. Size ≤ `max_bytes` (16 MB); read no more than `max_bytes + 1`.
4. Envelope keys exactly `{payload, sha256}`; `sha256 == fingerprint(payload)`.
5. Payload version/shape exactly as above. For v2, allow `result: null` and require the additional `settlement` object; for v1, preserve the exact shape and do not invent fields.
6. `payload.attempt.attempt_id == attempt_id`; embedded `Attempt` fields match B1 attempt (`run_id`, page, attempt_no, claim_seq, request_digest, binding, tokens, reserve_units).
7. `payload.outcome` matches the proof-derived `Outcome`; `evidence_digest == fingerprint(proof)`.
8. `payload.proof` exact key set; `proof.attempt == attempt_id`; `proof.state == "terminal"`; usage/result hashes/status consistent.
9. B1 `historical_evidence_facts` matches the immutable attempt/run; `not_sent` is not an acceptable final-response evidence state.
10. For new settlement, page row matches `attempt_id`/`candidate_attempt_id`, `claim_seq`, run, page, and job generation; page state is `send_committed` (or `unknown_outcome` for B6). For legacy `send_committed + terminal` projection repair, apply the same identity checks but do not call settlement; exact reserve and terminal outcome receipts plus final evidence are mandatory, and the write is page/job-only.
11. Pricing snapshot and `reserve_units` match the historical `Run`/config; no current settings are consulted.
12. Settlement callers may reach this step only from a valid final file. `.pending` is not an accepted input. Only then call `record_job_outcome` (B1 terminal + page projection + job roll-up) in one owner-fenced transaction. **Legacy `send_committed + terminal` projection repair is not a settlement caller:** validate the already-existing immutable reserve receipt, canonical outcome receipt and final evidence, then use the separate page/job-only repair contract (§5.6.2); never call `record_job_outcome` or `record_outcome` for that repair.

**Legacy B1 receipt validation (projection repair only):** this validation applies only to the cases that historically produced an outcome receipt — R-23, R-25, and R-27. Use the historical receipt format, not the proposed new B3.4 settlement payload. Require the deterministic existing operation ID `uuid5(UUID(attempt_id), "settlement").hex`, `operations.kind='outcome'`, `request_digest == fingerprint(parsed canonical request_json)`, exact canonical `request_json` equal to the expected historical `Outcome`, and exact canonical `result_json == {attempt_id, state}`. Match the receipt outcome to the immutable B1 attempt's current state, `actual_units`, and `evidence_digest`; validate the operation epoch as a real persisted owner epoch. For `not_sent`, the receipt epoch must equal the attempt's original owner epoch, as required by the historical B1 predicate. For `terminal`, do not require equality with `attempt.owner_epoch`, because a prior B2 evidence recovery could have settled under a later owner. Validate the original reserve operation receipt and body against the B1 attempt as well.

**R-30 has no outcome receipt to validate.** The legacy blanket `dispatcher()` converted `intent → unknown` directly, without `record_outcome()` and without updating `durable_job_pages` (`coordination.py:430-454`). Requiring an outcome receipt for R-30 would therefore produce a false integrity mismatch. Validate instead only the immutable reserve receipt plus the exact persisted B1 attempt/page/run/claim identity and original `owner_epoch`. If an outcome receipt is unexpectedly present, verify its meaning against the immutable attempt facts; an unsupported or contradictory combination quarantines instead of being interpreted as a settlement.

For legacy `cancelled + not_sent + attempt_id IS NULL`, schema v2 did not persist `claim_cancel_revision` in the reserve receipt. Do not invent or reconstruct that value. A cancel-winning repair requires the exact historical `record_outcome` source contract proving that its only `claimed→cancelled` write was guarded by `job.state IN ('cancelling','cancelled') AND job.cancel_revision > claim.cancel_revision`, the atomic B1 outcome/page commit, the page's legacy `cancelled` marker, and a matching immutable durable cancel command receipt for the same job. If that source provenance or receipt evidence is unavailable or ambiguous, quarantine.

For legacy `send_committed + terminal`, validate that the final evidence envelope's embedded attempt, `Outcome`, proof, and result match the page/B1 identity; `Outcome.operation_id`, request digest, terminal state, actual usage/cost, and evidence digest must match both the B1 row and the exact existing operation receipt. Derive page state/result bytes from this validated final evidence only. Repair remains page/job-only.

### 7.4 Settlement

`settle_saved_evidence(attempt_id, historical_context, *, expected_identity)` MUST:

- Accept a job-managed identity and the persisted `claim_seq` (do not hard-code `1`); remove the `attempt_no == 1 && claim_seq == 1 && legacy id` conjunction (`durable_dispatch.py:379-381`).
- Reconstruct the page projection from the evidence `settlement` object, not from the legacy attempt number.
- Use `record_job_outcome` with a reconstructed page claim (`job_id`, run, page, candidate_attempt_id, claim_seq, page_revision, owner_epoch, cancel_revision) from the page row; do not require the original `JobPageDispatchClaim` object. Require the current owner fence for any write.
- Be idempotent: an already terminal B1 attempt with the same evidence digest returns the existing receipt; a different digest is a conflict.
- Never create a new B1 intent, permit, or HTTP request.

### 7.5 Orphan, pending, and missing evidence

| Situation | Recovery behavior |
|---|---|
| Valid final evidence exists, B1 `intent`, page `send_committed` | Settle terminal + project page/job atomically under current owner fence |
| Valid final evidence exists, B1 `unknown`, page `send_committed`/`unknown_outcome`/`blocked` | B6-only reconciliation; B3.4 does not mutate B1/page/job or clear scope block |
| Valid final evidence exists, B1 `terminal`, page already terminal | Verify exact settlement digest; exact B1 receipt read-only; roll-up only if consistent; conflicting digest is quarantined, never overwrite |
| Valid final evidence exists, B1 `terminal`, legacy page `send_committed` | R-27 projection-only page/job repair after exact identity, reserve/terminal outcome receipts, outcome and digest validation; no B1 settlement call or mutation |
| Valid final evidence exists, B1 `not_sent`, page `failed`/`claimed`/`cancelled` | Contradiction; preserve final, quarantine; do not replace the pre-gate financial result or project success |
| Invalid/unresolved evidence exists, B1 `not_sent`, page `failed` | Evidence contradiction; preserve artifacts and quarantine; do not rerun settlement or reinterpret `not_sent` |
| Valid final evidence exists, page `intent` | Contradiction; quarantine unknown/blocked, do not settle as success |
| Page `blocked`, B1 `intent`, any evidence | State/attempt mismatch; quarantine as unknown/blocked; never treat as `not_sent` or settle evidence as ordinary success |
| Page `claimed`, B1 `intent`, valid final | Legacy pre-gate contradiction; quarantine as unknown/blocked, preserve final for B6; neither `not_sent` nor successful settlement |
| Page `claimed`, B1 `intent`, no valid final but no positive legacy gate proof | R-20 quarantine; page state alone is insufficient to classify `not_sent` |
| Legacy page `claimed`, B1 `unknown` after blanket conversion | R-30 verify identity and immutable reserve receipt, attach ID only on exact identity, set page/job `blocked`; preserve B1 `unknown`, scope blocks and evidence for B6; no outcome receipt exists or is required; repeat recovery is read-only |
| Page `blocked`, B1 `unknown`, any evidence | Stable R-21 quarantine; repeated recovery is receipt verification/no-op only; never unblock or project success/`not_sent` |
| Page `unknown_outcome`, B1 `unknown`, any evidence | Stable R-11 unknown; preserve B6-only reconciliation behavior and blocked scopes |
| Page `failed`, B1 `not_sent`, no final evidence | Stable R-22 only when the page retains the attempt ID and canonical receipt/digest verifies; exact replay/read-only no-op; receipt mismatch is integrity quarantine |
| Legacy page `claimed`, B1 `not_sent`, exact reserve and outcome receipts plus identity and no final or pending evidence | R-23 projection-only repair to `failed`, attach immutable attempt ID, recompute job; B1 remains `not_sent`; no settlement call or new operation |
| Legacy page `claimed`, B1 `not_sent`, missing/mismatched receipt or any conflicting final/pending evidence | R-24 quarantine; preserve B1 `not_sent`; no success, retry, receipt minting, or B1 mutation |
| Legacy page `cancelled`, B1 `not_sent`, `attempt_id IS NULL`, exact reserve and outcome receipts, cancel command receipt and cancel proof | R-25 attach immutable ID and preserve `cancelled`; repair page/job only; B1 remains `not_sent` |
| Legacy page `cancelled`, B1 `not_sent`, `attempt_id IS NULL`, cancel proof missing/mismatched | R-26 quarantine; never infer cancel-winning from the state label alone |
| Legacy page `send_committed`, B1 `terminal`, exact reserve and terminal outcome receipts plus valid matching final | R-27 page/job projection-only repair; no repeat settlement |
| Legacy page `send_committed`, B1 `terminal`, evidence or receipt missing/corrupt/mismatched | R-28/R-29 quarantine; preserve B1 terminal and all evidence; never infer page success |
| Valid `.pending` exists, final missing | Publication recovery may promote it and independently validate final; only that resulting final file may then enter the financial evidence index |
| `.pending` only, invalid pending, or promotion conflict | Pending is not financial proof; no settlement. If `send_committed`, mark `unknown_outcome`/job `blocked`; preserve artifacts for B6 |
| Final missing, B1 `terminal` | If page is legacy `send_committed`, R-28 quarantine page/job as `blocked` and preserve terminal B1; if the page is already terminal, apply the existing terminal-row rule and never infer success without evidence; no retry |
| Final corrupt / wrong ID / hash mismatch | Quarantine unknown/blocked; preserve file; B6 |
| Orphan evidence with no B1 attempt | Ignore for settlement; index for B6; never create an attempt |

### 7.6 Evidence lifecycle

- Evidence is immutable. Do not rewrite, truncate, or delete during B3.4.
- Evidence root must be local private storage; `os.link` is used for no-overwrite publication (`:224`). Do not place it on a filesystem that does not support hard links.
- Retention/GC is not B3.4. With 20,000 active pages and a 16 MB cap, the theoretical footprint is large; a retention policy is required before B5.
- Evidence is not encrypted; private ACL is the only protection. Do not put secrets in the envelope.

---

## 8. Adaptive fair scheduling

### 8.1 Ownership

- Exactly one process per installation owns the dispatcher OS lock and B1 lease (`CoordinationLedger.dispatcher`).
- The scheduler runs inside that process. API processes only submit/cancel/resume/status via command transactions; they never claim a page.
- `durable_scheduler_state`/`durable_scheduler_users` are updated only by the owner in the claim transaction. No second writer.
- `DurableDispatcher` is process-bound (`__reduce__` raises; `_pid` check), so the scheduler must not be forked to a child.

### 8.2 Eligibility

A job is eligible iff:

- `execution_state='ready'`;
- `state IN ('queued','running')`;
- `generation = documents.generation` and `documents.active_run_id = job.run_id`;
- `cancel_revision` is stable for the selection;
- source identity still matches the pinned execution input (resolver/stat check);
- its owner/scopes are not blocked/quarantined.

A page is eligible iff:

- state `queued` or `quota_wait`;
- `attempt_id IS NULL`;
- no B1 attempt exists for `(run_id, page_number)`;
- the job is eligible;
- its `quota_wait` backoff (if any) has expired.

`claimed` pages with no attempt are released to `queued` during recovery; they are not selected directly. Pages with any attempt are never eligible.

### 8.3 Two-level round-robin

Persisted state:

- `durable_scheduler_state.turn_seq` — global monotonic turn counter.
- `durable_scheduler_users.last_turn` — last turn in which a user was served.
- `durable_jobs.last_document_turn` — last turn in which the job/document was served.

Selection:

1. **User level:** among users with at least one eligible page, choose the smallest `last_turn` (missing = 0), tie-break by stable hash of `(tenant_id, owner_id)`.
2. **Job level:** within the chosen user, among eligible jobs, choose the smallest `last_document_turn`, tie-break by `job_id`.
3. **Page level:** choose the lowest eligible `page_number` in the chosen job. Deterministic page order preserves user-visible progress and simplifies evidence/recovery.
4. **Commit:** in one `BEGIN IMMEDIATE`:
   - read `durable_scheduler_state.turn_seq`; migration v2 already seeds the singleton row `(1, 0)` (`coordination_migrations.py:351`), so the read must return a row. An `INSERT OR IGNORE INTO durable_scheduler_state(id,turn_seq) VALUES(1,0)` is allowed as a defensive no-op, but it is not required and must not be described as the seed mechanism;
   - `turn_seq += 1`;
   - upsert the selected user (`INSERT ... ON CONFLICT(tenant_id,owner_id) DO UPDATE SET last_turn=excluded.last_turn`) and set `last_turn = turn_seq`;
   - selected job `last_document_turn = turn_seq`;
   - page `queued/quota_wait → claimed`, `claim_seq = max(attempt.claim_seq)+1`, `owner_epoch = current`, revision+1;
   - job `queued → running` if needed, revision+1.

**Fairness bound:** with `A` eligible users and one dispatcher, each eligible user is served within at most `A` claims, assuming at least one eligible page remains. Within a user, each eligible job is served within at most `J` claims. A large job does not get multiple consecutive pages while another job/user is waiting.

**Why not FIFO:** a 1,000-page job would monopolize the head of the queue. Two-level round-robin bounds per-user and per-job wait independent of document size.

**Why `last_document_turn` is per job, not per document:** the schema has one active job per document (`coordination_schema_v2.py:70-71`), so job-level and document-level turns coincide in B3.4. Do not describe it as a tenant-wide document counter.

### 8.4 Capacity model: independent limits, not `render + 1`

The renderer being one process does **not** imply a maximum of two in-flight pages. A single renderer can prepare page N+1 while page N is already in HTTP or settlement. Capacity must therefore be expressed as independent stage limits, not as an arithmetic sum around the renderer.

| Limit | Symbol | Source / default | Bounds |
|---|---|---|---|
| Render concurrency | `R` | `RENDER_CAPACITY = 1`, `max_workers=1` (`dispatch.py:98`, `pdf_render_worker.py:72`) | Pages simultaneously inside the render stage |
| HTTP concurrency | `H` | `_HTTP_CAPACITY.limit = 10` (`qwen_vision_adapter.py:41-42`) | Simultaneous POSTs |
| B1 global concurrency | `B` | `global_concurrency`, default `1`, max `10` (`coordination_contracts.py:192-200`) | Attempts between reserve and settlement |
| Provider/quota concurrency | `Q` | `quota_scopes.concurrency` for each active scope; `+∞` only when the policy publishes no limit | Attempts against that provider/scope |
| Resident image/request memory | `M` | `max(1, floor(resident_image_budget_bytes / peak_page_image_bytes))`; both measured, not guessed | Rendered images and request bodies held in memory |
| Page worker count | `W` | Configured page-task pool size, `W >= 1` (distinct from the renderer’s `max_workers=1`) | Concurrent page tasks (render → settle) |
| Page slots | `P` | `PAGE_CAPACITY = 10` (`dispatch.py:97`) | Pages from claim through settlement |

Derived limits:

```text
render_stage = R                                  # semaphore around render only
admission    = min(W, M)                          # page tasks holding an image/body
page_slots   = P                                  # separate PAGE_CAPACITY semaphore
send_stage   = min(B, Q, H)                       # pages between reserve and settlement
```

- `admission` is the render-ahead width. It may be greater than 1 with `R=1`: the renderer serializes the render step, while already-rendered pages wait for a send slot or are in HTTP. `M` bounds how many rendered images may be resident; `W` bounds active tasks. `P` is a separate page-slot semaphore, so total claimed pages are bounded by `min(admission, P)`.
- `send_stage` is the only paid/authorized width. `B` is the B1 global fence; `Q` is the provider/quota fence; `H` is the HTTP pool. It is **not** derived from the renderer.
- `render+1` is removed. It under-counts a render-ahead pipeline and incorrectly turns a renderer implementation detail into an HTTP concurrency cap.
- **Live clamp:** `admission = 1` and `send_stage = 1` until B5. This preserves the “live concurrency remains 1” rule: at most one attempt and one POST are in flight. It does not claim that the architecture is limited to two pages.
- Keep `RENDER_CAPACITY=1` and `max_workers=1`. Do not raise renderer parallelism. Raising `admission` in a future phase requires a measured `peak_page_image_bytes` and an explicit memory budget; otherwise `M=1`.

Adaptation:

| Signal | Action |
|---|---|
| `429` with `Retry-After` | Decrease `send_stage` by 1 (floor 1); set scope backoff to `Retry-After`; do not claim from that scope until expiry |
| `429` without header | Decrease `send_stage` by 1; backoff one rolling quota window (`quota_scopes.window_ms`) |
| Repeated `5xx`/timeout | Exponential per-scope backoff `1s → 60s` with jitter; do not reduce global concurrency on the first failure |
| N consecutive successes | Increase `send_stage` by 1 up to `min(B,Q,H)`, at most once per success window |
| Renderer busy | Queue in the render stage; do not reduce `admission` below `M`/`W`, and do not equate renderer busy with HTTP capacity |
| `billing_scopes.blocked` / `quota_scopes.blocked` | Skip user/scope; mark job `blocked` or page `quota_wait`; never spin |
| `billing_budget_exhausted` / `request_cap_exhausted` | Job `blocked`; no retry |
| `ledger_busy` | Bounded retry with backoff; do not count as a page failure |

**Do not** infer provider capacity from unpublished headers or model names. Only status codes, `Retry-After`, and persisted quota policies are authoritative. If `Q` is unknown, leave it out of the `min` (equivalently `+∞`) and rely on `B`, `H`, and 429 adaptation; do not invent a number.

### 8.5 Backpressure and quota

- `DeferredWithoutIntent("global_capacity_exhausted")` → release the claim to `queued`; wait for an in-flight slot.
- `DeferredWithoutIntent("quota_exhausted")` → release to `quota_wait`; compute the earliest retry from the quota window and `attempts.ended_ms`; skip that scope/user without blocking others.
- A quota-exhausted user must not head-of-line block another user. This is the main reason fairness is at the user level, not the page level.
- `quota_wait` has no persisted `not_before` column. Use a process-local advisory deadline; on restart it resets, but the next reserve will defer again. The DB remains the authority. Do not add a schema column in B3.4.
- `PAGE_CAPACITY` is acquired before claim; if no slot, do not claim. This avoids claim leaks.
- The 30-second claim-to-gate target is a local watchdog/SLO deadline, not a durable claim lease. See §8.9; do not release or steal a claim on a timer.

### 8.6 Scheduling invariants

1. No page is claimed without a free admission slot (`min(W,M)`), a free page slot (`P`), and a configured worker; the render slot is acquired only for the render stage.
2. No user gets two consecutive turns while another eligible user waits.
3. No job gets two consecutive pages while another eligible job for the same user waits.
4. No claim is held across a render/HTTP wait without a bounded deadline.
5. A cancel/pause signal stops new claims immediately; in-flight gates are allowed to settle.
6. A blocked scope is skipped, not retried.
7. A nonterminal page with an attempt is never reclaimed; it must be recovered/terminalized.
8. The scheduler never creates a B1 attempt; only `prepare` does.
9. The scheduler never POSTs; only the adapter/dispatcher does.
10. The scheduler never unblocks a scope.

### 8.7 Pseudocode

```python
async def run_once(owner):
    if not capacity.available():
        return
    selection = select_eligible()          # read-only snapshot
    if selection is None:
        await backoff()
        return
    actor = resolver.resolve_job(selection.job_id)   # source identity revalidation
    if actor is None:
        store.block_job(selection.job_id, "source_unverified")
        return
    claim = store.claim_page(owner, actor, selection.job_id, selection.page_number)
    try:
        await worker_pool.submit(PageTask(claim, actor))
    except CapacityFull:
        store.release_claim(claim)         # only safe before reserve
```

```python
async def page_pipeline(task):
    image = await render(task)             # RENDER_CAPACITY=1
    body = build_body(image)
    call = dispatcher.call(client, task.page, digest(body), estimate(image), job_claim=task.claim)
    try:
        result = await dispatch_post(call, body)   # reserve -> gate -> POST
    finally:
        dispatcher.finish(call, result, error)     # evidence -> outcome -> page/job
```

### 8.8 Persistence and restart

- Turn counters are the only scheduler state that must survive restart. They are updated in the claim transaction.
- On restart, missing user rows default to `last_turn=0`; stale rows for users with no active jobs may be deleted opportunistically.
- The scheduler does not auto-resume `paused_restart` or `blocked` jobs. It only schedules `queued`/`running` jobs after recovery.
- `last_document_turn` is per job; a new generation gets a new job row with `last_document_turn=0`. This is correct: a new generation should not inherit the old job’s fairness debt.

### 8.9 Claim deadline, local watchdog, and no claim theft

**What 30 seconds means.** 30 seconds is a scheduling SLO and a local watchdog budget for the interval between claim and gate. It is not a durable visibility lease, not a persisted claim expiry, and not permission to reassign a claim. The value is chosen from the expected p99 render + body-build time plus margin; it is an alarm threshold, not a correctness fence. Correctness comes from ownership epochs, page revision predicates, and the single dispatcher lock.

**Why time alone cannot justify theft.** While a worker is alive it may still be rendering, reserving, or about to gate. Without a durable lease row that the worker must renew, the database cannot distinguish “slow but alive” from “dead”. Reclaiming a `claimed` page on a timer would allow a second worker to reserve and send while the old worker still holds a rendered image and a valid claim object. The result would be a duplicate paid attempt. Therefore B3.4 has **no time-based claim reaper**.

**Local watchdog protocol.** Each claim carries a monotonic deadline, checked at stage boundaries and immediately before `reserve`/gate:

1. **Deadline expires before reserve, page still `claimed` with `attempt_id IS NULL`:** the owning worker calls `release_claim(owner, claim, expected_revision=...)` in its own dispatch transaction. The update is predicated on `state='claimed'`, `attempt_id IS NULL`, the same `owner_epoch`, the same `claim_seq`, and the expected revision. On success the page becomes `queued` (or `quota_wait`) and the owner epoch is cleared. No attempt exists, so no financial liability and no evidence.
2. **Deadline expires with an `intent` attempt:** the worker must not release the page as if it were free. It aborts before the gate if it still can, and leaves the `intent` for its own pre-gate settlement or for the next owner’s recovery. The attempt is already recorded; releasing the claim would create a page with an attempt in `queued` and violate the identity invariants.
3. **Deadline expires after the gate:** the send is authorized. The worker continues to a bounded transport timeout and settlement. It must not release, retry the POST, or mark the page `cancelled`.
4. **Watchdog is local and advisory.** Missing the deadline never mutates another worker’s claim and never disables the dispatcher. It increments an internal counter and triggers the owner’s own release/abort path.

**No-double-claim guarantee.** A page can become claimable again only through one of these paths:

| Release path | Proof required | Can it run while the old worker is alive? |
|---|---|---|
| Owner `release_claim` | same owner epoch, claim_seq, revision; `attempt_id IS NULL` | Yes, but it is the same worker/process relinquishing its own claim, so there is no second claimant |
| Owner transitions to `quota_wait`/`queued` before reserve | same predicates | Yes, same owner only |
| Recovery of a prior epoch | new dispatcher holds the OS lock and B1 lease; page belongs to the prior `owner_epoch`; no attempt or proven pre-gate intent | Not against a worker in the same epoch. A prior-epoch survivor is fenced: its reserve/gate/settlement writes fail the `owner_epoch` predicate, and it must not POST after `_check()` reports lease loss |
| Settlement/terminal projection | B1 terminal + page attempt identity | Not a reclaim |

A live old worker **in the same epoch** cannot be superseded because:

- `claimed` pages are not eligible for selection (§8.2); only `queued`/`quota_wait` are.
- The same process is the only claimer; a second process cannot acquire the dispatcher OS lock/lease.
- Every claim write is fenced by `owner_epoch`, `claim_seq`, `revision`, and `attempt_id IS NULL`; a stale worker’s write fails rather than overwriting.
- Recovery runs only under a new epoch and starts after the old lease is gone.

A prior-epoch worker thread that somehow survives a same-process lease change is **fenced, not trusted**: it cannot reserve, gate, or settle under the old epoch. If it already gated, the send is authorized and the new owner must settle from evidence; it must not retry the POST. The only non-acknowledged release is loss of the owner epoch/lease (normally process death), detected by lock/lease acquisition, not by a 30-second timer. Tests WK-13/WK-14 assert that a slow-but-alive worker is never double-claimed and that a stale timer never disables the dispatcher.

---

## 9. Trusted document resolver

### 9.1 Why B3.4 needs an internal resolver

`AuthorizedDocumentContext` is the only accepted proof that a server component may use a document (`durable_job_contracts.py:104-144`). Today it can only be constructed by `_verified_document_context`, which is private and used only by tests. `DurableJobStore._check_actor` rejects anything else (`durable_job_store.py:74-78`). A scheduler that accepts caller-supplied tenant/owner/path values would be an authorization bypass.

B3.4 therefore needs a **trusted resolver usable by the scheduler, recovery, and tests**, not a new public HTTP surface. Public route composition, request authentication, and startup wiring remain B3.5/B5. This keeps the B3.4 change reviewable without committing an auth design that has not been reviewed.

### 9.2 Interface (B3.4 internal scope)

```python
class TrustedDocumentResolver:
    def resolve_job(self, *, job_id: str) -> AuthorizedDocumentContext: ...

    def resolve_claimed(
        self, *, claim: JobPageDispatchClaim,
    ) -> AuthorizedDocumentContext: ...
```

- `resolve_job` is for scheduler/recovery. It reads the persisted job identity, loads `access.json`, verifies the document still belongs to the same tenant/owner, verifies source bytes/stat, and builds a sealed context **without broadening access**. It must not accept a user-supplied path.
- `resolve_claimed` is for settlement/recovery. It reconstructs the context from the page/job/run and verifies the claim identity; it does not re-check a live user session.
- `resolve_request(user=..., document_id=...)` is **explicitly out of B3.4**. It is the B3.5 entry point for API handlers and must not be added or wired in this phase.

### 9.3 Validation

1. `document_id` passes `validate_document_id` (`workspace.py:18`).
2. `WorkspaceService.get(document_id)` rejects symlinks/reparse points and non-directories (`workspace.py:95-103`).
3. `source.pdf` is the exact file; canonical path is inside `documents_dir`; no path traversal.
4. `access.json` owner/legacy reviewers must include the persisted job owner (`main.py:322-339` is the existing ACL model); the resolver does not trust a caller-supplied identity and does not bypass the admin path.
5. Active run/generation must match `durable_jobs`/`documents`; stale generation is `document_generation_changed`.
6. Source digest, size, mtime, and page count must match the pinned execution input. Stat check on every claim; full hash on start, source change, or suspicious stat. Do not hash 1 GB per page.
7. `authorization_digest` is deterministic over `(tenant, owner, document, generation, source_digest, size, mtime, page_count, access_revision)`.
8. The context is sealed with `_AUTH_SEAL`; no public constructor.

### 9.4 Failure codes

`document_not_found`, `document_authorization_mismatch`, `document_generation_changed`, `document_source_changed`, `document_source_unverified`, `document_context_invalid`, `server_authorization_context_required`.

### 9.5 Placement and wiring

- New file: `averon_import/services/ai_first/document_resolver.py`.
- B3.4 consumes it only from the scheduler, recovery, settlement, and tests. No HTTP endpoint and no startup composition.
- **Do not wire `main.py` in B3.4.** The existing recognition route and `_assert_ai_first_document_access` stay unchanged. B3.5 owns `resolve_request`, route/auth composition, and any additional tenant checks.
- Keep `_verified_document_context` as a test seam, but production scheduler/recovery code must not call it directly except through the resolver.
- The resolver may be constructed explicitly with the persisted tenant/owner identity. It must verify that identity against the workspace/`access.json`; it must never accept a caller-supplied path or broaden access.
- Static guard: in B3.4, `document_resolver` may be imported by the scheduler/recovery/tests, but not by `main.py` or route modules. A test asserts this import boundary.

---

## 10. Bounded worker lifecycle

### 10.1 Process and thread model

- One dispatcher process owns the OS lock, the B1 lease, the scheduler, and the worker pool.
- `DurableDispatcher` is thread-affine: `_pid`, `CALLER`, `DispatchPermit` thread checks (`durable_dispatch.py:519-532`). A page pipeline must run `prepare/consume/post/finish` on the same thread that owns its permit. Use `drain_thread` per pipeline, not a shared mutable call across threads.
- The renderer remains a single spawned process (`pdf_render_worker.py:72`). The HTTP pool remains a global 10-thread executor (`qwen_vision_adapter.py:41-42`).
- The scheduler may use an asyncio loop, but all dispatcher calls must be serialized/drained correctly; do not call `_check()` from a forked child.

### 10.2 Capacity acquisition order

Acquire in this order, release in reverse, always in `finally`. The stage semaphores are independent; do not collapse them into one `render+1` number.

```text
admission slot (min(W, M))             # render-ahead + in-flight
  -> PAGE_CAPACITY (P=10)
     -> render slot (R=1)             # release immediately after image bytes exist
        -> build body / hold image    # memory M still bounds resident images
           -> send slot (min(B, Q, H))  # B1 global / provider quota / HTTP
              -> reserve -> gate -> POST -> settle
```

- `PAGE_CAPACITY` is held from before rendering through settlement (`dispatch.py:95-97`). `admission` and `P` are separate semaphores; a claim requires both.
- The **render slot is released as soon as the rendered image is produced**, not held through HTTP or settlement. Holding it would serialize HTTP behind the renderer and recreate the `render+1` bottleneck.
- `send_stage` is acquired after rendering and before `reserve`; it covers `B`, `Q`, and `H`. Do not acquire `_HTTP_CAPACITY` before rendering.
- The body/image must remain valid until the POST body is built. `M` (resident image budget) is what bounds render-ahead; a page waiting for a send slot still holds memory.
- If any acquisition fails/cancels, release everything already held; never leak a semaphore.
- Do not claim a page before an admission slot exists.

### 10.3 Worker loop

```text
loop:
  wait for task or cancel
  if cancel before reserve: release/cancel claim; exit
  acquire PAGE
  acquire render slot
  render
  release render slot
  check source signature
  build body
  acquire send slot (B1/quota/HTTP)
  prepare (reserve; page -> intent)
  if cancel wins before gate: gate rejects on cancel_revision; current owner settles B1 not_sent and projects page/job; no POST
  consume (gate; page -> send_committed)
  if cancel after gate: do not cancel; POST already authorized; settle
  POST
  finish (evidence -> outcome -> page -> job)
  release capacities
```

### 10.4 Cancel and drain

- Cancel is cooperative: set `cancel_event`, increment `cancel_revision` via the command transaction. The transaction applies §5.5: it cancels only pages with no `attempt_id` and no B1 attempt; it does not change any B1 attempt or attempt-bearing page. `intent` stays `intent` until the current dispatcher settles a rejected gate as `not_sent`, or new-owner recovery does so after a crash. `send_committed` stays authorized and must settle.
- New claims stop immediately. A worker that has not reserved must observe `cancelled`/`cancel_revision` and exit without `reserve`.
- Pages `intent` with no gate commit are proven `not_sent` only by the owner-fenced B1 settlement path; the page retains its attempt ID and becomes `cancelled` when cancellation won. The command transaction never performs this settlement. No POST follows a rejected gate.
- Pages `send_committed` are already authorized; let the in-flight POST settle. Do not try to revoke.
- Graceful shutdown: stop the producer, cancel tasks that have no intent, wait for in-flight calls up to a bounded grace, call `finish` for every call, then close. If the grace expires, leave intents for recovery; do not fake a settlement.
- `DurableDispatcher.close()` waits for `_calls == 0` (`:316-325`); a hung transport can block shutdown. Add a bounded drain policy and a clear “leave intents for recovery” path, rather than an unbounded wait.
- On `KeyboardInterrupt`/process kill, the OS releases the lock; the next dispatcher recovers. Never `os._exit` the main process while holding an intent; the renderer child already has a parent watcher for its own lifecycle (`pdf_render_worker.py:19-31`).
- Claim deadlines are local watchdogs only; see §8.9. Expiry never causes another worker to cancel or steal a claim.

### 10.5 Lease loss

- Heartbeat failure sets `_failed` and stops renewing (`durable_dispatch.py:308-314`).
- Before gate: a worker must check `_check()`; if the lease is lost, do not gate, do not POST. Leave the `intent` for the new owner to classify as pre-gate `not_sent`; the old owner cannot record a `not_sent` outcome after losing the lease (`record_outcome` would reject the prior epoch at `coordination.py:1063-1064`).
- After gate: the send is authorized and may be in flight. The old owner cannot record an outcome after losing the lease (`_transaction` owner-fenced). It may publish evidence; the new owner must perform publication recovery, then scan valid final evidence before classifying the attempt. Do not retry the POST.
- If a valid final exists before post-gate classification, the new owner settles it. If no valid final exists and the new owner commits `unknown`, any later evidence is B6-only; it cannot reopen B3.4 settlement.
- The exact orphan-response helper remains an implementation detail (see §14.15); these publication, final-validation, owner-fencing, and B6 boundaries are normative.

### 10.6 No unbounded work

- Fixed worker count; fixed queue size; no `create_task` per page without a slot.
- Per-user in-flight cap: at most `ceil(send_stage / active_users)` or 1 in live mode. This prevents one user from occupying all send slots.
- Evidence ≤ 16 MB; page image is ephemeral; do not retain all page images.
- Status/queue queries are bounded and indexed (`durable_ready_pages` on `(state, job_id, page_number)`, `durable_owner_status`).
- Scheduler selection is O(active users + active jobs), not O(all pages). Do not scan 20,000 pages to pick one.

### 10.7 Fault taxonomy

Not every error may disable the dispatcher, and not every page error may be retried. The classifier must run before any `_failed`/terminal decision.

| Class | Examples | Dispatcher effect | Page/job effect | Retry policy |
|---|---|---|---|---|
| **Page-local deterministic** | `job_dispatch_claim_stale`, `dispatch_gate_rejected` (after independently classifying its persisted cause), `job_not_cancellable`, `document_generation_changed`, `billing_budget_exhausted`, `request_cap_exhausted` | Stay alive only when persisted rows are internally consistent; if no attempt exists, release/abandon only this claim. A gate rejection with B1 `intent` is `not_sent` only after every §6.3 predicate passes | Page `failed`/`cancelled`/`blocked` with a safe code; after attempt creation retain `attempt_id`; job roll-up per §4.1 | No automatic paid retry; explicit new generation or operator action |
| **Dispatch intent/receipt mismatch** | `dispatch_intent_mismatch`, stale in-memory `Attempt`/claim differing from persisted B1 row | Stop the current attempt before POST. Re-read attempt, page and immutable reserve receipt under the current owner fence. If persisted identity/receipt are internally exact and §6.3 positively proves pre-gate, settle only the canonical `not_sent` operation; otherwise fail closed and quarantine. Do not blindly retry reserve/gate with the mismatching object | Proven pre-gate → atomic B1 `not_sent` + page/job projection. Any persisted identity/receipt conflict or absent gate proof → retain immutable identity, quarantine/unknown and block relevant scopes/job; do not overwrite a terminal/unknown B1 result | No automatic retry or POST. Exact settlement only with independent persisted proof; otherwise B6/manual |
| **Financial reserve receipt mismatch** | `dispatch_reserve_receipt_mismatch`, persisted reserve operation receipt disagrees with B1 intent/body/digest/owner | Treat as integrity failure, not page-local deterministic. Stop this dispatch and fail closed for this attempt. A mismatch between a cached caller object and the independently reread canonical DB receipt may proceed only if the DB receipt exactly matches the immutable B1 intent and all §6.3 gate-not-committed proof passes; this is a proven `not_sent` settlement, not a retry | If independent exact persisted receipt + pre-gate proof exist, atomically settle canonical `not_sent`; otherwise quarantine/unknown, block relevant scope/job and preserve receipt/evidence. Never convert a mismatching financial receipt into an ordinary safe settlement | No retry, no new receipt ID, no POST; B6/manual for persisted mismatch |
| **Retryable busy/backpressure** | `ledger_busy`, bounded `database is locked`, `DeferredWithoutIntent`, `429`, `5xx`/timeout, dispatcher acquisition busy | Stay alive; bounded backoff | No terminal page; `queued`/`quota_wait`; no `_failed` | Bounded retry; backpressure never kills the dispatcher |
| **Process/owner/ledger fatal** | lease loss, `owner_epoch` mismatch, clock regression, ledger corruption, storage replacement, permit misuse, `_failed` already set | Fail closed: stop claiming and POSTing; close/drain; do not fake a settlement | Leave `intent`/`claimed` for recovery; no terminal projection | Only after the next owner acquires the lock/lease and recovery completes |
| **`CommitReceiptUncertain`** | command or dispatch commit read-back uncertain | Fail closed for that boundary; no POST; do not retry in the same boot | Preserve the pre-commit or committed state; recovery reads back the receipt | Retry only after verified read-back or a new acquisition |
| **Integrity/security violation** | evidence hash/identity mismatch, persisted `dispatch_intent_mismatch`, persisted `dispatch_reserve_receipt_mismatch`, `result_evidence_conflict`, authorization mismatch, sealed-context violation, source replaced, schema/trigger invariant | Do not silently continue; stop this attempt, fail closed, quarantine the affected job/scope; alert. Other work is allowed only after the ledger/owner boundary remains verified and isolated | Page `unknown_outcome`/`blocked` as permitted by the B1 state machine; preserve any existing B1 terminal/unknown result and evidence; job `blocked` | B6/manual only; never auto-resolve or retry |

Rules:

- A stale claim is **page-local deterministic**, not fatal (corrects G5). It must not set the dispatcher `_failed`, and it must not fail the whole job if other pages can proceed.
- A pre-gate `dispatch_gate_rejected` caused by the winning `cancel_revision` is not a bare claim release: the B1 `intent` is settled `not_sent` under the current owner fence in the same transaction as the retained page identity/projection and job roll-up. If that settlement cannot be confirmed, fail closed and let recovery read back/classify; do not POST.
- A rejection after `send_committed` can never be converted to `not_sent`; the authorized send must settle from valid final evidence or become `unknown_outcome`/blocked.
- `dispatch_intent_mismatch` and `dispatch_reserve_receipt_mismatch` are never classified solely by exception name. A proven pre-gate `not_sent` requires an exact persisted attempt/reserve identity, an irreversible gate-not-committed marker under the identified contract, no valid final/unresolved pending evidence, and the canonical settlement identity. If any predicate fails, quarantine/unknown and fail closed; a financial receipt mismatch is never an ordinary retry signal.
- `CommitReceiptUncertain` is **not** a page-local retry. It means the process cannot know whether a write committed; retrying a POST from that state can duplicate a paid attempt.
- A real integrity violation must stay fail-closed. “Do not kill the dispatcher on a stale claim” must not be generalized to “continue after a hash mismatch”.
- `outcome_conflict`/`historical_outcome_conflict` is idempotent only when the digest is identical; a different digest is an integrity violation, not a retry signal.
- The classifier returns one of: `retry`, `retry_after`, `page_terminal`, `job_blocked`, `fatal`. `prepare`/`finish`/`_settle` must branch on it instead of the current blanket `_failed` assignment (`durable_dispatch.py:500-502`, `:578-587`, `:593-621`).

---

## 11. Future file/class/method list

This is a design inventory, not an implementation. Names are proposals; final naming must follow the surrounding code style.

### 11.1 New files

| File | Classes / functions | Purpose |
|---|---|---|
| `averon_import/services/ai_first/durable_scheduler.py` | `FairScheduler`, `SchedulerPolicy`, `SchedulerSnapshot`, `SchedulerError`, `select_user`, `select_job`, `select_page`, `claim_next`, `record_turn` | Two-level round-robin, adaptive admission, backoff |
| `averon_import/services/ai_first/durable_worker.py` | `PageWorkerPool`, `PageTask`, `PageResult`, `CapacitySet`, `PagePipeline`, `DrainController` | Bounded worker lifecycle, capacity order, cancel/drain |
| `averon_import/services/ai_first/durable_recovery.py` | `RecoveryPlanner`, `RecoveryDecision`, `RecoveryAction`, `UnsettledAttempt`, `EvidenceIndex`, `recover_unsettled` | Acquisition-time classification and settlement |
| `averon_import/services/ai_first/durable_evidence.py` | `EvidenceIdentity`, `EvidenceEnvelope`, `EvidenceStore`, `scan`, `read`, `write_idempotent`, `recover_publication`, `validate` | Job-managed evidence v1/v2, idempotent final publication, separate pending-publication and final-financial-evidence recovery |
| `averon_import/services/ai_first/durable_settlement.py` | `PageOutcomeProjection`, `JobRollup`, `record_job_outcome`, `project_page`, `rollup_job` | Atomic B1 + page + job projection |
| `averon_import/services/ai_first/document_resolver.py` | `TrustedDocumentResolver`, `ResolvedDocument`, `ResolverPolicy`, `resolve_job`, `resolve_claimed` (internal only; `resolve_request` deferred to B3.5) | Sealed `AuthorizedDocumentContext` construction for scheduler/recovery/tests |
| `averon_import/services/ai_first/durable_job_runner.py` | `DurableJobRunner`, `JobExecution`, `start`, `run_page`, `finish`, `cancel` | Per-job orchestration without per-job global queues |

### 11.2 Modified files

| File | Required changes |
|---|---|
| `coordination.py` | `_reserve` atomically creates B1 attempt + page `claimed→intent` + immutable attempt ID; `_validate_job_page_claim` gate mode; gate checks `cancel_revision` and updates `intent→send_committed`; owner-fenced B1/page/job settlement including cancel-winning pre-gate `not_sent`; recovery under new owner; preserve legacy `dispatcher()` blanket behavior for B2 and add `dispatcher_for_jobs()` non-mutating acquisition; do not add unblock |
| `coordination_schema.py` | No schema change. Keep `attempts_terminal`; document that `intent→not_sent` is allowed and `unknown→not_sent` is not |
| `coordination_schema_v2.py` | No schema change. Use existing scheduler tables and page states |
| `durable_job_store.py` | `adopt_owner_epoch` as guarded per-job owner-fenced adoption in A3; `claim_next_page`, `release_claim`, `complete_page`, `rollup_job`, `mark_paused_restart`, `mark_blocked`, `list_eligible_jobs`, `load_page_identity`; **replace `cancel()` page logic with §5.5** (increment `cancel_revision`; cancel only no-attempt pages; never mutate an attempt-bearing page/B1 attempt); strengthen `_write` crash receipt handling; make `resume` reject any nonterminal page with an attempt; implement `paused_restart` writer |
| `durable_dispatch.py` | `DurableDispatcher(..., job_managed=True)` selects `dispatcher_for_jobs()`; legacy B2 default selects unchanged `dispatcher()`; durable runner recovery-before-claims startup barrier; `register` exact-replay-or-verified-adoption; `prepare` fault taxonomy (§10.7); preserve public `DispatchCall.finished` single-use guard and specify B1 settlement-layer replay/conflict/owner fencing (§5.6); job-aware `settle_saved_evidence` accepts valid final only; `EvidenceStore` publication recovery; bounded drain |
| `durable_qwen_adapter.py` | `start` job-aware page identity; `_recover_result` uses `EvidenceIdentity`; `register` only once per job; no legacy `attempt_id`/`claim_seq==1` assumptions |
| `durable_job_contracts.py` | Add `PageOutcomeProjection`, `EvidenceIdentity` (or import from evidence module), `Reservation` page revision fields; keep `AuthorizedDocumentContext` sealed |
| `orchestration.py` | Remove per-run global scheduling from the durable path; use `DurableJobRunner`/`PagePipeline`; keep legacy path unchanged; clamp durable `admission`/`send_stage` to 1 live |
| `dispatch.py` | Expose the independent capacity constants/policy (`R`, `H`, `B`, `Q`, `M`, `W`, `P`); no renderer increase; no `render+1` |
| `main.py` | **No B3.4 change.** `TrustedDocumentResolver.resolve_request` and route/auth/startup composition are B3.5/B5 |
| `workspace.py` | Add resolver-safe canonical path helper; no public behavior change |

### 11.3 Interfaces (signatures)

```python
# coordination.py
def reserve_job_managed(self, owner, attempt: Attempt, job_claim) -> Reservation: ...
def commit_job_send_gate(self, owner, attempt: Attempt, *, expected_run: Run, claim) -> None: ...
def record_job_outcome(self, owner, outcome: Outcome, *, job_claim,
                       page: PageOutcomeProjection) -> dict: ...
def recover_unsettled(self, owner, *, evidence: EvidenceIndex,
                      batch_size: int = 50) -> RecoveryReport: ...
def dispatcher_for_jobs(self) -> DispatcherLease: ...  # acquire/advance owner; never blanket-settle intents
def settle_pre_gate_not_sent(self, owner, *, attempt_id: str,
                             claim, proof: str) -> dict: ...  # B1/page/job, owner-fenced
def recover_pre_gate_not_sent(self, owner, *, attempt_id: str,
                              proof: str) -> dict: ...      # new-owner recovery equivalent

# durable_job_store.py
def adopt_owner_epoch(self, owner, *, batch_size: int = 50) -> AdoptionReport: ...
def claim_next_page(self, owner, *, policy: SchedulerPolicy) -> JobPageDispatchClaim | None: ...
def release_claim(self, owner, claim, *, target: str = "queued") -> None: ...
def complete_page(self, owner, claim, projection: PageOutcomeProjection) -> None: ...
def rollup_job(self, owner, job_id: str) -> str: ...
def mark_paused_restart(self, owner, job_id: str) -> None: ...
def mark_blocked(self, owner, job_id: str, safe_code: str) -> None: ...

# durable_dispatch.py
def __init__(self, root, *, installation_id, evidence_root,
             job_managed: bool = False, **ledger_options): ...
# job_managed=True selects dispatcher_for_jobs(); B2 default preserves dispatcher()
def register(self, client) -> None: ...          # exact replay OR verified adoption; no stale overwrite
def settle_saved_evidence(self, attempt_id, historical_context, *,
                          expected_identity: EvidenceIdentity) -> dict: ...

# document_resolver.py — B3.4 internal only
def resolve_job(self, *, job_id: str) -> AuthorizedDocumentContext: ...
def resolve_claimed(self, *, claim: JobPageDispatchClaim) -> AuthorizedDocumentContext: ...
# resolve_request(...) is B3.5; do not add it in B3.4
```

---

## 12. Test matrix

The matrix is intentionally adversarial. Every crash window and every fence gets a test. Tests must use real schema-v2 SQLite and the real resolver; no `_verified_document_context` in production-path tests.

### 12.1 Lifecycle and state machine

| ID | Scenario | Expected |
|---|---|---|
| SM-01 | Submit → T1 → claim → reserve → gate → terminal → settle | Page `completed`; job `completed`; one attempt `terminal` |
| SM-02 | All pages `incomplete` | Job `completed_with_errors`; pages `incomplete` |
| SM-03 | Mixed success/failure | Job `completed_with_errors`; failed pages `failed` |
| SM-04 | All pages fail | Job `failed` |
| SM-05 | Cancel while `preparing_input`, no attempts | Job/page `cancelled`; no attempt |
| SM-06 | Cancel while `queued`, no attempts | Job `cancelled`; pages `cancelled` |
| SM-07 | Cancel while `running`, page `intent` | Cancel leaves B1/page intent unchanged; gate rejects; owner-fenced settlement records `not_sent`, retains attempt ID, projects page `cancelled`; no POST |
| SM-08 | Cancel after gate | Page `send_committed`; attempt settles terminal→job `cancelled`, or unknown→job `blocked`; no revoke; never cancelled with unknown financial result |
| SM-09 | `claimed → quota_wait → claimed` | No attempt created; claim_seq stable; no stuck page |
| SM-10 | `claimed` stale release | Page `queued`; owner cleared; reclaim works |
| SM-11 | Resume from `paused_restart` with only schedulable pages | Job `queued`; pages schedulable |
| SM-12 | Resume with any nonterminal page having an attempt | `unknown_outcome_not_resumable`; no state change |
| SM-13 | Resume with `unknown_outcome` | Rejected; job remains blocked/paused |
| SM-14 | Illegal transition (e.g., `completed → running`) | Rejected; no row mutation |
| SM-15 | Job roll-up after last page settles | Job terminal in same transaction; revision/finished_at set |
| SM-16 | Cancel `queued` job with all pages no-attempt | Job goes directly to `cancelled` (not `cancelling`); all pages `cancelled`; no attempt |
| SM-17 | Cancel `running` job with `claimed` pages and no attempt | `claimed` pages `→ cancelled` immediately; job `cancelled` if no attempt remains; no `cancelling` limbo |
| SM-18 | Cancel `running` job with an `intent` page | Cancel transaction leaves attempt/page intent untouched and job `cancelling`; gate rejects; owner-fenced settlement records `not_sent`; page ID retained and page becomes `cancelled`; no POST |
| SM-19 | Cancel `running` job with `send_committed` + `queued` pages | `queued` pages cancelled immediately; sent page untouched; job `cancelling`; definitive settlement→`cancelled`, unknown→`blocked` |
| SM-20 | Cancel `claimed` page that already has a legacy B1 intent | Page is **not** cancelled by the no-attempt predicate; job `cancelling`; recovery R-02 settles only with positive legacy-gate proof, otherwise R-20 quarantines |
| SM-21 | Cancel job with `unknown_outcome` page | Safe pages cancelled; unknown page/attempt untouched; job is/remains `blocked`, never `cancelled`; B6 only |
| SM-22 | Repeat cancel with same `operation_id` | Same receipt; `cancel_revision` not bumped twice; pages not re-cancelled |
| SM-23 | Attempt identity across every terminal projection | Once B1 attempt exists, `page.attempt_id == candidate_attempt_id == attempts.attempt_id` remains non-NULL through `not_sent`, `cancelled`, `failed`, `blocked`, `completed`, `incomplete`, and `unknown_outcome`; no-attempt pages remain NULL |
| SM-24 | Reserve commits B1 attempt and page projection | One dispatch transaction exposes both `attempts=intent` and `page=intent` with matching immutable ID, or neither; no partial identity |
| SM-25 | Cancel command observes an `intent` page | Only `cancel_revision`/job command receipt and no-attempt page transitions change; B1 attempt and attempt-bearing page remain byte-for-byte/state-identical |
| SM-26 | Unknown financial result during cancellation | Job `cancelling→blocked`; it cannot become `cancelled` until B6 reconciliation resolves unknown |
| SM-27 | Cancel after a job attempt settled with overage on a job-relevant scope | Derive scopes from persisted B1 attempt bindings; job stays/becomes `blocked`, not `cancelled`; no scope unblock |

### 12.2 Gate and crash windows

| ID | Scenario | Expected |
|---|---|---|
| GT-01 | Cancel after reserve, before gate | Cancel leaves attempt/page `intent`; gate rejected on `cancel_revision`; current owner settles `not_sent` atomically with page/job projection; transport not called |
| GT-02 | Gate then cancel | Page `send_committed`; attempt settles terminal or unknown; liability retained; cancel cannot revoke; unknown job becomes `blocked` |
| GT-03 | Concurrent cancel/gate barrier | Exactly one SQLite winner; no partial page state |
| GT-04 | Reserve commit receipt loss | Attempt intent or no attempt; no POST; recovery classifies |
| GT-05 | Gate commit receipt loss | Page `intent` or `send_committed`; no POST if `intent`; conservative unknown if `send_committed` |
| GT-06 | Crash after reserve, before gate (page `intent`) | Recovery `intent→not_sent`; page failed with attempt ID retained; no scope block; `paused_restart` only if other schedulable pages remain, otherwise terminal error |
| GT-07 | Crash after reserve, before gate (legacy page `claimed`) | Recovery `claimed + intent→not_sent` only when legacy gate/receipt proves gate could not commit; otherwise quarantine/blocked; no stuck page |
| GT-08 | Crash after gate, before POST | Page `send_committed`, no evidence → `unknown_outcome`; scope blocked; job blocked |
| GT-09 | Crash after POST, before evidence | Attempt unknown; no evidence; scope blocked; no retry |
| GT-10 | Crash after evidence, before outcome | Evidence valid; recovery settles terminal; page terminal; job roll-up |
| GT-11 | Legacy inconsistent-row recovery: B1 terminal but page remains `send_committed` from pre-atomic implementation | Do not classify as a new B3.4 settlement. Verify exact reserve and terminal outcome receipts, terminal outcome, identity, and final evidence; atomically repair page/job projection only if all agree; otherwise quarantine/blocked. New B3.4 transactions cannot create this split |
| GT-12 | Double gate attempt | `dispatch_permit_consumed`/`dispatch_gate_rejected`; no second send |
| GT-13 | Gate with stale cancel revision | `job_dispatch_claim_stale`/`DispatchGateRejected`; no send |
| GT-14 | Gate with wrong attempt body/receipt | `dispatch_intent_mismatch`/`dispatch_reserve_receipt_mismatch`; no send |
| GT-15 | Cancel races reserve | One winner. Cancel first: no attempt; page cancelled. Reserve first: page and B1 attempt atomically `intent`; cancel leaves them untouched; gate then fails and owner settlement records `not_sent`; no POST |
| GT-16 | Cancel races gate | One winner. Cancel first: gate aborts; page/attempt remain intent until owner settlement. Gate first: page `send_committed`; cancel cannot revoke |
| GT-17 | Cancel replay after partial work | Same operation ID returns original receipt; no second revision bump; no second page transition |
| GT-18 | Cancel/reserve/gate barrier with both commit orders | Every ordering has one result: no-attempt cancel, owner-fenced pre-gate `not_sent`, or authorized send; page/B1 identity atomic; at most one POST |
| GT-19 | Cancel races `finish()` after gate | `send_committed` is not revoked; finish atomically settles B1/page/job; definitive result leads to cancelled only after all settle; unknown leads to blocked |
| GT-20 | Finish races new-owner recovery | Owner lease serializes writers; one settlement receipt/digest wins; no duplicate POST or competing page projection |
| GT-21 | Old worker calls finish after recovery | Old epoch cannot write. Identical committed receipt may be returned read-only; different digest conflicts; no B1/page/job overwrite |
| GT-22 | Cancel command over `intent` page | Command transaction does not call `record_outcome`, update B1 attempt, or perform `intent→not_sent`; cancel revision makes gate reject |
| GT-23 | New B3.4 settlement visibility barrier | A concurrent reader sees either pre-settlement B1 `intent` + page `send_committed` + prior job projection, or after commit B1 terminal/unknown + matching page/job projection; never B1 terminal with page still `send_committed` |
| GT-24 | Cancel races a settlement that discovers overage on a relevant scope | If cancel commits first, settlement makes job `blocked`; if overage settlement commits first, cancel preserves `blocked`; never silently roll up `cancelled` while relevant scope requires reconciliation |
| GT-25 | `dispatch_intent_mismatch` caused only by stale caller object; persisted B1 intent/reserve receipt are exact, new-contract page remains `intent`, and no final/pending evidence exists | Reread canonical rows under owner fence; if every §6.3 predicate passes, one canonical `not_sent` settlement with atomic B1/page/job projection; no POST and no retry |
| GT-26 | `dispatch_intent_mismatch` with page/gate proof or persisted attempt identity not verifiable | No `not_sent` inference; fail closed, quarantine B1 as `unknown` when legal, block relevant scopes/job; preserve IDs; no retry or POST |
| GT-27 | `dispatch_reserve_receipt_mismatch` is only a stale cached receipt; independent DB reread returns the exact immutable receipt matching the B1 intent and §6.3 proves gate did not commit | Resolve as proven pre-gate `not_sent` using the same canonical settlement ID/payload; no reserve/gate retry and no POST |
| GT-28 | Persisted financial reserve receipt conflicts with B1 attempt body/digest/owner epoch | Integrity quarantine/fail-closed; do not treat as ordinary deterministic error, do not create `not_sent` settlement without separate exact proof, do not retry or POST |

### 12.3 Recovery and evidence

| ID | Scenario | Expected |
|---|---|---|
| RC-01 | `intent` attempt, page `intent`, no final evidence/pending publication | `not_sent`; no block; page failed with attempt ID retained; `paused_restart` only if another page remains schedulable, otherwise terminal error |
| RC-02 | `intent` attempt, page `send_committed`, no evidence | `unknown`; scopes blocked; page unknown; job blocked |
| RC-03 | `intent` attempt, page `send_committed`, valid evidence | Terminal settlement; page completed; no block (new acquisition order) |
| RC-04 | `unknown` attempt/page with valid late final evidence | Evidence preserved for B6 only; B3.4 does not settle or unblock; job/scope remain blocked |
| RC-05 | `intent` attempt, page `intent`, evidence present | Contradiction → unknown/blocked; no settlement |
| RC-06 | Corrupt evidence hash | `unknown`; blocked; file preserved |
| RC-07 | Wrong attempt ID in evidence | `result_evidence_identity_mismatch`; blocked |
| RC-08 | Valid `.pending`, final missing | Publication recovery may validate/promote it; it is not indexed or settled until final is independently re-read and validated |
| RC-09 | Evidence write crash between final link and pending unlink | Publication replay verifies identical final, removes pending; no duplicate settlement or file overwrite |
| RC-10 | Terminal outcome with `result: null` | Evidence v2 validates; settlement uses proof/outcome; page failed |
| RC-11 | Legacy B2 attempt (no page) | `intent→unknown`; blocked; no durable job roll-up |
| RC-12 | Recovery restart mid-batch | Idempotent; no duplicate settlement; no lost page |
| RC-13 | `unknown→not_sent` attempted | Trigger aborts; test asserts the code never tries it |
| RC-14 | Orphan evidence, no B1 attempt | Ignored for settlement; indexed for B6 |
| RC-15 | Evidence for terminal attempt with different digest | Conflict; no overwrite |
| RC-16 | Evidence root replaced/symlinked | `evidence_storage_replaced`/private path failure; no settlement |
| RC-17 | 50-attempt batch bound | Lock time bounded; all classified across batches |
| RC-18 | Recovery after current owner settled cancel-winning `intent → not_sent` | Exact receipt replay; no duplicate B1/page/job write, no scope block, attempt ID retained |
| RC-19 | Recovery sees legacy `claimed` + B1 intent after cancel | R-02 may record `not_sent` only with positive legacy-gate proof; otherwise R-20 quarantines as unknown/blocked; page is never treated as no-attempt and retains/receives immutable attempt ID |
| RC-20 | `.pending` only after publication recovery cannot promote | Never financial evidence; `send_committed` becomes unknown/job blocked; preserve artifact |
| RC-21 | Valid `.pending` promotes to final before financial scan | Only the independently validated final is accepted for settlement; pending alone never satisfies settlement |
| RC-22 | Cancelling job has `send_committed` + valid final evidence | B1/page/job settle atomically; job remains `cancelling` until all attempts settle, then `cancelled`; unknown instead becomes `blocked` |
| RC-23 | Page `blocked` + B1 attempt `intent` | State mismatch is not a pre-gate proof: do not write `not_sent` or settle success; quarantine attempt as `unknown`, block relevant scopes/job, retain attempt ID, and route to B6 |
| RC-24 | Legacy page `claimed` + B1 `intent` + valid final evidence | Evidence contradicts the legacy claimed/pre-gate state; do not ordinary-settle success or mark `not_sent`; quarantine as B1 `unknown`/page `blocked` with recovery-conflict safe code, block job/scope, preserve final for B6 |
| RC-25 | Page `claimed` + B1 `intent` + no final but legacy gate semantics/receipt cannot be positively verified | Do not infer `not_sent` from `claimed`; quarantine B1 as `unknown`, page/job `blocked`, preserve attempt ID; B6 only |
| RC-26 | R-21 transition has committed; restart recovery repeatedly for `page=blocked + B1=unknown` with/without late evidence | Stable quarantine: no new settlement operation/receipt, no B1/page/job financial-result mutation, no success/`not_sent` projection, no scope unblock; exact read-only receipt verification only |
| RC-27 | R-22 transition has committed; restart recovery repeatedly for `page=failed + B1=not_sent` with matching canonical receipt/digest | Stable pre-gate settlement: exact receipt replay/read-only no-op; no new operation ID, no timestamp/revision rewrite, no transition to success/cancelled |
| RC-28 | R-11 transition has committed; restart recovery repeatedly for `page=unknown_outcome + B1=unknown` with valid/absent/invalid late evidence | Preserve existing B6-only behavior; no B3.4 settlement/state change/unblock and no success/`not_sent` projection |
| RC-29 | Positive legacy gate proof: trusted installation deployment provenance resolves to the exact archived atomic gate contract; matching reserve receipt and persisted `claimed`/NULL attempt marker satisfy its predicates | Gate could not have committed under that contract; one canonical `not_sent` settlement is allowed; attempt owner epoch remains immutable |
| RC-30 | Legacy `claimed + intent` has no trustworthy deployment provenance or the matching reserve receipt is missing | Schema-v2 rows alone are insufficient; R-20 quarantine to unknown/blocked; no `not_sent`, no receipt minting |
| RC-31 | Provenance identifies a historical gate contract whose predicate/atomic marker differs from the required legacy contract | Contract mismatch is not positive proof; quarantine/unknown and block, preserve evidence/attempt; no `not_sent` |
| RC-32 | `page=failed + B1=not_sent`, but the canonical operation receipt is absent or its digest does not match | Integrity quarantine; preserve B1 `not_sent`; do not mint another operation ID, mutate the page to success, or retry settlement |
| RC-33 | Legacy `claimed + B1 not_sent + attempt_id IS NULL`, exact identity, reserve and outcome receipts, no final or pending evidence | Owner-fenced projection-only repair to `failed`; attach candidate attempt ID; preserve B1 `not_sent`, exact receipts and digest; recompute job; no settlement call or new operation |
| RC-34 | Legacy `claimed + B1 not_sent + attempt_id IS NULL`, missing or mismatched required receipt(s) (separate subcases); include conflicting final/pending evidence subcase | Quarantine page/job; preserve B1 `not_sent`; never infer success/cancel, mint receipt, retry, or change financial result |
| RC-35 | Legacy cancel-winning `cancelled + B1 not_sent + attempt_id IS NULL`, exact reserve and outcome receipts, exact cancel command receipt/cancel revision | Attach immutable attempt ID and preserve `cancelled`; projection-only repair; B1 unchanged; missing/mismatched cancel proof or receipts are rejected |
| RC-36 | Legacy `claimed + B1 unknown` after accepted blanket conversion | Verify exact attempt/page/run/claim identity and immutable reserve receipt; attach attempt ID, page/job `blocked`; B1 unknown and scope blocks unchanged; preserve/index evidence for B6 only; no outcome receipt exists or is created; no settlement or POST |
| RC-37 | Repeat recovery after RC-36 unknown projection repair | Idempotent read-only no-op; no new settlement operation, no revision churn, no scope unblock, and no POST |
| RC-38 | Legacy `send_committed + B1 terminal` with exact immutable identity, reserve and terminal outcome receipts/digest, and valid matching final evidence | Owner-fenced projection-only page/job repair to the evidence-derived terminal state; B1 terminal/epoch/evidence digest/receipt unchanged; no settlement call |
| RC-39 | Legacy `send_committed + B1 terminal` with missing, corrupt, or wrong-attempt final evidence | Quarantine page/job; preserve B1 terminal and all evidence; no inferred success, settlement, retry, or POST |
| RC-40 | Legacy `send_committed + B1 terminal` with missing/mismatched required receipt(s), request digest, outcome, or evidence digest | Quarantine page/job; preserve immutable B1 terminal and receipt; no replacement receipt/operation or B1 mutation |
| RC-41 | Repeat recovery after a successful RC-33, RC-35, or RC-38 projection repair | Exact target state and the case-specific immutable receipt(s) revalidated; read-only no-op; attempt ID and B1 facts unchanged; no new operation, POST, scope unblock, or page/job revision change |

**Common assertions for RC-33…RC-41:** Every case checks B1 state immutability, attempt ID linkage/immutability where identity is independently exact, the case-specific immutable receipt(s) required by §5.6.2 (outcome receipt for RC-33/35/38, reserve receipt for RC-36) and the absence of any newly minted receipt, owner fencing, consistent job roll-up, no HTTP/POST, no scope unblock, and no new settlement operation. For RC-36/37, the blanket `intent→unknown` conversion has no outcome settlement receipt and must not create one; verify only the immutable reserve receipt and exact persisted identity. For quarantine cases, page/job quarantine is the only permitted projection change; B1 attempt state, owner epoch, evidence digest, operation receipt, and all existing scope blocks remain unchanged. These are architecture test cases only; do not create or modify executable tests in this revision.

### 12.4 Fairness and scheduler

| ID | Scenario | Expected |
|---|---|---|
| FS-01 | Two users, one has 100 pages, one has 1 | User turns alternate; small user served within bound |
| FS-02 | Two jobs same user, one large | Job turns alternate; no consecutive pages for large job |
| FS-03 | Three users, one quota-exhausted | Exhausted user skipped; others progress; no head-of-line block |
| FS-04 | Blocked billing scope | Job/user skipped; no spin; no new reserve |
| FS-05 | Restart preserves `last_turn`/`last_document_turn` | Ordering resumes fairly; no starvation |
| FS-06 | New job after long-running job | New job gets a turn within one round; no inherited debt |
| FS-07 | Cancel removes user/job from queue | No ghost turns; next user served |
| FS-08 | `DeferredWithoutIntent("quota_exhausted")` | Page `quota_wait`; backoff; other users progress |
| FS-09 | `DeferredWithoutIntent("global_capacity_exhausted")` | Page `queued`; no `_failed`; retry when capacity frees |
| FS-10 | Deterministic `job_dispatch_claim_stale` | Dispatcher stays alive; claim released; retry/next job |
| FS-11 | `ledger_busy` | Bounded retry; no page failure; no dispatcher death |
| FS-12 | `CommitReceiptUncertain` | Dispatcher fails closed; no POST; recovery |
| FS-13 | 256 active jobs / 20k pages | Selection bounded; no full table scan; index used |
| FS-14 | One user occupies all worker slots | Per-user in-flight cap prevents starvation |
| FS-15 | Quiet cancel during long page | New claims stop; in-flight settles |
| FS-16 | Stale claim (`job_dispatch_claim_stale`) while other jobs wait | Dispatcher stays alive; only that claim is released; other jobs continue |
| FS-17 | `CommitReceiptUncertain` during settlement | Dispatcher fails closed for that boundary; no POST/retry; recovery reads back |
| FS-18 | Integrity mismatch (`result_evidence_conflict`) | Job/scope quarantined; dispatcher does not silently continue; no auto-retry |
| FS-19 | Bounded `ledger_busy` during claim | Backoff; no page failure; no dispatcher death |

### 12.5 Resolver and authorization

| ID | Scenario | Expected |
|---|---|---|
| AU-01 | Valid job owner, internal `resolve_job` | Sealed context; claim works |
| AU-02 | Wrong owner / tenant | `document_authorization_mismatch`; no context |
| AU-03 | Legacy reviewer | Allowed only per `access.json` |
| AU-04 | Admin | Allowed per existing admin path |
| AU-05 | Deleted document | `document_not_found` |
| AU-06 | Symlink/reparse `source.pdf` | Rejected; no path escape |
| AU-07 | Source digest mismatch | `document_source_changed`; job blocked |
| AU-08 | Size/mtime/page count mismatch | `document_source_changed`/`invalid` |
| AU-09 | Stale generation | `document_generation_changed`; no claim |
| AU-10 | `resolve_job` for another owner’s job | Rejected; no broadened access |
| AU-11 | `resolve_claimed` for tampered claim | Rejected |
| AU-12 | No production `_verified_document_context` call | Static test/import guard |
| AU-13 | Resolver import boundary | `document_resolver` imported only by scheduler/recovery/tests; not by `main.py`/routes |
| AU-14 | No B3.4 public resolver route/startup wiring | Static test: `main.py` and route modules unchanged; no `resolve_request` in B3.4 |

### 12.6 Worker and capacity

| ID | Scenario | Expected |
|---|---|---|
| WK-01 | 3 pages, `send_stage=1` | One render, one POST at a time; no overlap |
| WK-02 | 3 pages, `R=1`, `send_stage=4` | Renderer serializes renders; up to 4 pages may be rendered/waiting; HTTP overlap is bounded by `send_stage`, not by 2 |
| WK-03 | HTTP capacity 10 | Never >10 HTTP calls |
| WK-04 | PAGE capacity 10 | Never >10 page slots |
| WK-05 | Cancel before reserve | Claim cancelled/released; no attempt; no POST |
| WK-06 | Cancel after gate | POST allowed to finish; settle; no retry |
| WK-07 | Lease lost before gate | No gate/POST; pre-gate recovery |
| WK-08 | Lease lost after gate | Evidence may be written; new owner settles; no retry |
| WK-09 | Graceful shutdown with in-flight | Bounded drain; all calls finished; no leaked slot |
| WK-10 | Hard kill with intent | Recovery classifies; no permanent stuck page |
| WK-11 | Renderer BrokenProcessPool | Renderer restart; page failure/retry policy; no orphan process |
| WK-12 | No unbounded tasks | Queue/slot counts bounded under 20k pages |
| WK-13 | Claim watchdog expires while worker is slow but alive | Owner releases its own no-attempt claim; no second worker claims it; no duplicate attempt |
| WK-14 | Same-epoch scheduler sees an old `claimed` page | It does not reclaim it; only owner release or prior-epoch recovery can free it |
| WK-15 | Independent limits | `R=1` does not clamp HTTP to 2; `send_stage` is `min(B,Q,H)`; `admission` is `min(W,M)` and `P` is a separate page-slot cap |
| WK-16 | Memory budget | Rendered images never exceed `M`; admission waits rather than exhausting memory |
| WK-17 | Render slot release | Render slot is released before HTTP; a slow POST does not block the next render |
| WK-18 | Claim theft attempt with stale epoch | Update fails the `owner_epoch`/revision predicate; no double claim |

### 12.7 Integration and regression

| ID | Scenario | Expected |
|---|---|---|
| IN-01 | B3.2 submit → B3.4 runner → all pages completed | Status shows terminal pages/job |
| IN-02 | Restart mid-job, resume explicit | No duplicate paid attempts; no stuck page |
| IN-03 | Two jobs in one process | Fair interleave; separate clients/permits; no `register_run` conflict |
| IN-04 | One active job per document | Second submit rejected `durable_document_busy` |
| IN-05 | 1,000 selected pages | Queue limits, status pagination, evidence retention behavior |
| IN-06 | B3.3 gate tests still pass | No regression in cancel/send linearization |
| IN-07 | B1/B2 legacy tests still pass | No owner/ledger regression |
| IN-08 | Schema v2 capability unchanged | No schema v3; `durable_jobs_v1` only |
| IN-09 | Live concurrency = 1 | `admission=1`, `send_stage=1`; no more than one in-flight POST |
| IN-10 | No secrets in status/evidence/logs | Static scan + runtime assertion |
| IN-11 | No production `_verified_document_context` | Import/call guard |
| IN-12 | `register()` on existing B3.2 run registered with `uuid4().hex` | Verified adoption succeeds; no `generation_conflict` |
| IN-13 | `register()` exact replay when original ID was deterministic | Evidence/receipt replay; no duplicate `runs` row |
| IN-14 | G7 reproducer (ledger-level) | Random original ID → `generation_conflict`; deterministic ID → replay; failed call leaves DB unchanged |
| IN-15 | Cancel matrix regression | SM-16…SM-27 and GT-15…GT-24 pass with the accepted `send_committed` test |
| IN-16 | No public resolver composition | `main.py`/routes unchanged; resolver internal only |
| IN-17 | Actual durable-job runtime acquisition path: `DurableJobRunner.start` → `DurableDispatcher.open(..., job_managed=True)` with an old job-managed B1 `intent` | Constructor invokes `dispatcher_for_jobs()`, never legacy `dispatcher()`; owner acquisition leaves the intent unchanged; scheduler is disabled until publication recovery and `recover_unsettled` finish; old intent is classified from page/final evidence before any `unknown` transition |
| IN-18 | Actual legacy B2 dispatcher construction/default | `DurableDispatcher(..., job_managed=False)` still calls unchanged `CoordinationLedger.dispatcher()` and preserves legacy `intent→unknown` behavior; job-managed mode cannot accidentally fall through to this path |
| IN-19 | Cancel during overage-blocked settlement for job-relevant scope | Derive relevant billing/quota scope IDs only from this job's persisted B1 attempt bindings; if any such scope requires reconciliation, cancellation stays/becomes `blocked` |
| IN-20 | New dispatcher epoch acquires while a job is `running` with `authorized_owner_epoch` and a B1 `intent` attempt from the prior epoch | Scheduler remains closed; owner-fenced adoption updates only job authorization to the new epoch; recovery settles/classifies the old attempt under the new owner fence while preserving immutable `attempt.owner_epoch`; scheduler opens only after barrier |
| IN-21 | New dispatcher epoch acquires while a `running` job has no unsettled B1 attempts (queued pages and/or stale no-attempt claims only) | Adoption updates `authorized_owner_epoch`; recovery releases only prior-epoch no-attempt claims; no B1 settlement is invented; the job stays safe/running and claims resume only after all active jobs pass the epoch barrier |
| IN-22 | Crash after some per-job owner-epoch adoption transactions commit, before recovery/barrier completes; next dispatcher acquires a later epoch | Already-adopted jobs are safely adopted again from the intervening epoch; unadopted jobs are adopted once; no B1 attempt epoch changes; scheduler stays closed until recovery and final barrier complete |

### 12.8 Settlement idempotency and financial winner

| ID | Scenario | Expected |
|---|---|---|
| SF-01 | Call public `finish()` twice on the same `DispatchCall` | First call uses the single-use guard; second raises `dispatch_call_finished` before invoking B1 settlement; exactly one public settlement call |
| SF-02 | Exact replay at the B1 settlement/receipt layer after evidence final exists | Same deterministic operation ID + canonical payload returns the stored receipt read-only; no B1/page/job revision change, file rewrite, or duplicate POST |
| SF-03 | Conflicting digest passed directly to the B1 settlement/receipt layer for same deterministic operation ID | `historical_outcome_conflict`; original B1 result, page projection, job roll-up, evidence, and receipt remain unchanged; this is not tested by reusing a finished public `DispatchCall` |
| SF-04 | Old worker finishes after recovery committed settlement under a new owner epoch | Owner fence prevents writes; identical digest may read/replay receipt only; conflicting digest is rejected without overwrite |
| SF-05 | Current-owner `finish()` and recovery classification race | Owner lease plus SQLite transaction yields one settlement winner and one immutable receipt; no competing terminal state or second POST |
| SF-06 | Cancel, reserve, gate, finish, and recovery interleaving | Exactly one allowed financial outcome per B1 attempt; page/B1 ID is atomic and permanent; cancellation never settles; stale owner cannot overwrite; no duplicate send |
| SF-07 | Recovery terminalizes pre-gate attempt after dispatcher crash | New owner records one `not_sent` settlement and page/job projection; late prior-owner `finish()` cannot replace it |
| SF-08 | Current dispatcher and recovery produce equivalent cancel-winning pre-gate `not_sent` | Same `uuid5(UUID(attempt_id), "settlement").hex` and byte-identical canonical payload; ambiguous commit read-back returns exact receipt; no recovery-specific operation ID exists |

---

## 13. Limitations before B5/B6

1. **No production composition.** `DurableJobStore`, the scheduler, and the resolver are not wired into the live runtime. B5 owns runtime composition and auth/tenant wiring.
2. **No reconciliation or scope unblock.** A genuine `unknown_outcome` blocks `billing_scopes`/`quota_scopes` permanently in B3.4. B6 requires schema v3 and an administrative release. B3.4 must not fake it. A cancel request may increment `cancel_revision` and cancel safe no-attempt pages, but the job becomes/remains `blocked`, never `cancelled` or `cancelling`, while a financial result is unknown; B6 resolves it.
3. **No automatic paid retry.** `durable_job_pages` has no retry/backoff/attempt-number columns. One B1 attempt per page is the effective limit. A `not_sent` or terminal page cannot be retried without a new run/generation or a schema change.
4. **No schema v3.** All B3.4 changes must fit schema v2. Fairness uses existing tables; page `intent` uses existing columns; evidence v2 is filesystem, not SQLite.
5. **Single dispatcher, no HA.** One OS lock and one lease per installation. A second dispatcher gets `dispatcher_busy`. There is no cross-process scheduler coordination beyond that lock.
6. **Renderer is single-process.** No 10-page parallel rendering. `PAGE_CAPACITY=10` is a pipeline cap, not a renderer promise. `RENDER_CAPACITY=1` and `max_workers=1` remain. This does **not** cap the send stage at two pages: render, HTTP, B1, provider quota, memory, and worker count are independent limits (§8.4).
7. **Live concurrency remains 1.** B1 `global_concurrency` defaults to 1; `OrchestrationConfig.concurrency` may be up to 10, but the durable path clamps `admission` and `send_stage` to 1 until B5.
8. **Windows power-loss guarantees are weaker.** POSIX directory `fsync` is not available on Windows; the accepted B3.2 doc already flags the Linux/VPS gate. B3.4 does not close it.
9. **No automatic recovery of `unknown_outcome`.** B3.4 preserves evidence and blocks; it does not reconcile, guess, or resend.
10. **`paused_restart` is explicit.** The scheduler never auto-resumes a job with a terminal error or a pre-gate `not_sent` page. The user must resume or create a new generation.
11. **No page-level visibility lease.** Claims are process-local and fenced by `owner_epoch`/revision. The 30-second target is a local watchdog/SLO, not a durable lease; there is no time-based claim theft. A crashed worker’s claim is reclaimed only by prior-epoch recovery, and only when no attempt exists or pre-gate proof is available.
12. **No persisted quota backoff.** `quota_wait` has no `not_before` column; backoff is process-local and resets on restart. The DB remains the authority, but a restart may cause one extra deferral.
13. **No multi-tenant fairness weighting.** `durable_scheduler_users` is keyed by `(tenant_id, owner_id)`; there is no tenant-level weight/priority. Multi-tenant policy is B3.5+.
14. **No evidence encryption/retention.** Evidence is private-ACL only; no GC/retention policy. A large job can create many 16 MB envelopes.
15. **No UI progress/polling.** B3.5 owns API/UI progress. B3.4 exposes no public scheduler metrics.
16. **No provider-specific adaptive signal.** Adaptation uses HTTP status/`Retry-After` and persisted quota policies, not model-specific headers or speculative capacity.
17. **Command path is not owner-fenced.** `_write` is a separate boundary; it is revision/cancel-revision fenced, not lease fenced. This is intentional but must be documented and crash-tested.
18. **Checkpoint and durable page state are not one store.** The adapter checkpoint remains a separate projection. B3.4 must reconcile them at start and settlement; it does not merge them.
19. **No `recover_blocked` command.** A `blocked` job can be cancelled, but not repaired in place. Source/input repair requires a new generation or a future explicit recovery API.
20. **`register()` adoption is required; `register_run` idempotency is not universal.** `register_run` replays only the exact operation ID. Runs registered with any other ID need the verified-adoption path in §5.4. Do not “fix” this by weakening `register_run`’s generation check.
21. **No measured memory budget yet.** `M` is a formula, not a promise. Until `peak_page_image_bytes` and `resident_image_budget_bytes` are measured, B3.4 must use `M=1`; a future increase requires evidence, not a guess.
22. **Legacy gate proof depends on trusted deployment provenance.** Schema v2 does not identify the historical gate binary/contract. If an immutable external deployment record cannot identify the exact atomic gate contract, legacy `claimed + intent` is quarantined as `unknown`; B3.4 does not guess `not_sent` and adds no schema field.
23. **Legacy projection repair is narrower than settlement.** `claimed + not_sent`, cancel-winning `cancelled + not_sent + NULL attempt_id`, `send_committed + terminal`, and `claimed + unknown + NULL attempt_id` are repaired only with exact immutable identity and the case-specific receipt/evidence/proof in §6.2. `claimed + unknown` uses the existing reserve receipt only, because the legacy blanket conversion created no outcome receipt. Repair changes page/job projection only; any uncertainty quarantines, and B1 state/receipt is never rewritten.
24. **Owner adoption has no persisted startup cursor.** Adoption is a guarded, idempotent owner-fenced update per running job; after any crash the next owner rescans all active jobs and repeats any job whose `authorized_owner_epoch` is older. The scheduler remains closed until recovery and the final epoch barrier pass.

---

## 14. Open questions

1. **Should pre-gate `not_sent` pages be automatically retried?** They are financially free, but one attempt per page is enforced by schema. Recommendation: no auto-retry in B3.4; mark `failed` and require explicit new generation. If automatic retry is required, B3.4 must add a page-attempt table (schema v3) and a new identity namespace.
2. **Should `paused_restart` be automatic after a dispatcher restart?** B3.2 says resume is explicit. Recommendation: keep explicit; the scheduler only schedules `queued`/`running`.
3. **Who owns the resolver: `main.py` auth or `WorkspaceService`?** Current `access.json` is owner-based, not multi-tenant. Recommendation: B3.4 implements `TrustedDocumentResolver` in `ai_first` with internal `resolve_job`/`resolve_claimed` only; it is consumed by the scheduler/recovery/tests and is not wired into `main.py`. B3.5/B5 owns `resolve_request`, public route/auth composition, and the ACL backend.
4. **Can B3.4 add a page-attempt/retry table without schema v3?** No under the stated constraint. If retries are mandatory, the scope must change.
5. **How should terminal API errors with `result: null` be evidenced?** Recommendation: evidence v2 with nullable `result`, mandatory proof/outcome/safe_code. Do not fabricate an `AdapterPageResult`.
6. **Can B3.4 unblock a scope after late evidence?** No. Recommendation: B6 reconciliation; B3.4 preserves evidence and leaves the scope blocked.
7. **What does cancel do in `paused_restart`/`blocked`?** Semantics are fixed by §5.5: increment `cancel_revision`; cancel only no-attempt pages; never mutate B1 attempts or attempt-bearing pages. Pending `send_committed` results wait in `cancelling`; an unknown financial result forces/remains `blocked` until B6.
8. **Should scheduler run in the API process or a dedicated worker?** Recommendation: dedicated worker process; API never holds the dispatcher lock. B5 composes it.
9. **Is tenant-level fairness required, or owner-level?** Current tables are owner-level. Recommendation: owner-level in B3.4; tenant weighting is B3.5+.
10. **What is the claim watchdog budget?** Recommendation: 30 s as a local SLO/watchdog deadline (§8.9), not a durable lease. Tune after measuring p99 render + body-build. No time-based claim theft at any value.
11. **Should `quota_wait` persist a `not_before`?** No column. Recommendation: process-local advisory; accept one extra deferral after restart.
12. **What is the exact `completed_with_errors` definition?** Recommendation: at least one `completed`/`incomplete` and at least one `failed`/non-financial `blocked`/`incomplete`. All failed → `failed`. Cancelled jobs become `cancelled` only after all attempts settle and no financial unknown remains.
13. **Does a job with cancelled pages after cancel become `cancelled` or `completed_with_errors`?** Recommendation: `cancelled` wins after all financial outcomes are settled; partial results remain visible. Any `unknown_outcome` instead makes the job `blocked` until B6.
14. **Should `blocked` be resumable after source restore?** B3.2 resume only accepts `paused_restart`. Recommendation: no in-place resume; require a new generation or a future `recover_blocked`.
15. **How should an orphan response after lease loss be handled?** The policy is fixed in §6.1/§6.4: the old owner may publish final evidence but cannot settle; the new owner repairs validated pending publication and scans final evidence before classifying the prior-epoch attempt. If no valid final exists after gate, it marks unknown; late evidence after that is B6-only. IN-20 covers adoption plus prior-epoch recovery.
16. **Is `os.link` reliable on all target filesystems?** It is required for no-overwrite evidence publication. Recommendation: document local-private-disk requirement; reject network shares.
17. **Who cleans `.pending` files?** Recovery owns publication repair: validate pending, publish/verify final, independently validate final, then unlink pending. Pending alone is never financial evidence.
18. **What is the evidence retention/GC policy?** Not B3.4. Recommendation: define before B5; 20k pages × 16 MB is a large upper bound.
19. **Can one user monopolize `PAGE_CAPACITY`?** Recommendation: per-user in-flight cap; live = 1 makes this moot, but the architecture must state it.
20. **What observability is required before B5?** Recommendation: internal counters for queue depth, per-user wait, claim-to-send latency, deferrals, unknown/blocked counts, evidence recovery outcomes. No public API in B3.4.
21. **What is `peak_page_image_bytes` and the resident memory budget?** Until measured, `M=1`; the admission width cannot be raised by assumption. This must be measured on the target renderer before B3.4b raises `admission` above 1.
22. **Does every provider quota scope publish a usable concurrency?** If not, omit `Q` rather than inventing it; `B`, `H`, and 429 adaptation remain authoritative. The exact policy extraction belongs to B3.4b design.
23. **What exact internal helper signature owns current-owner pre-gate settlement?** The semantics are fixed: the current dispatcher settles a gate-rejected `intent → not_sent` under the B1 owner fence, with atomic page/job projection; recovery performs the equivalent under a newly acquired epoch. Only helper naming/argument shape remains an implementation detail. The command-side `_write` must never perform it.
25. **Can schema-v2 state alone prove a legacy `claimed + intent` attempt is pre-gate?** No. §6.3 requires exact trusted deployment provenance, the matching immutable reserve receipt, and the identified irreversible page marker contract; otherwise R-20 quarantine is mandatory.

---

## 15. Recommendation and stop point

1. **Do not implement B3.4b on the current base.** First implement B3.4a: page `intent`, reserve/gate page transitions, the §5.5 cancel transaction, page/job settlement projection, job-aware evidence identity, idempotent evidence write, recovery classification, `paused_restart`, and `register()` exact-replay-or-adoption.
2. **Make the crash matrix a release gate.** For every point between claim and settlement, a test must prove one of: a matching stable terminal/receipt pair, a positively proven pre-gate `not_sent`, or a quarantined unknown (`unknown_outcome` or stable `blocked + B1 unknown`) with blocked scope/job. No stuck `claimed` page with an existing attempt; no `send_committed` page without a terminal/unknown projection.
3. **Then implement B3.4b**: persisted two-level round-robin, independent capacity admission (§8.4), bounded worker pool, internal resolver, and observability. Do not add a public resolver route or startup wiring; that is B3.5/B5.
4. **Keep live concurrency at 1.** Set `admission=1` and `send_stage=1` in B3.4, regardless of scheduler capacity. This is a send/POST clamp, not a claim that the renderer limits the pipeline to two pages.
5. **Stop after Revision 3.3.1.** Await independent GPT-6 review and explicit user permission before implementation. No commit, push, PR, merge, deployment, schema migration, or live concurrency change has been performed.

---

### Appendix A — Exact source evidence index

| Topic | Evidence |
|---|---|
| Blanket `intent→unknown` + block | `coordination.py:450-454`, `:709-713` |
| Reserve inserts `intent` only | `coordination.py:654-661` |
| Claim validation requires `claimed`, no attempt | `coordination.py:672-706` |
| Gate `claimed→send_committed` | `coordination.py:916-1039`, especially `:1019-1031` |
| `record_outcome` terminal branch does not project terminal page/job | `coordination.py:1041-1094`, especially `:1061-1073` |
| Legacy cancel-winning `not_sent` page update leaves `attempt_id` NULL and writes only `cancelled` | `coordination.py:1074-1090` |
| Legacy dispatcher blanket `intent→unknown` does not update durable pages and writes no outcome operation receipt | `coordination.py:430-454`, `:709-713` |
| Legacy `_settle` can produce `not_sent` when not delegated; page remains `claimed` except cancel-winning update | `durable_dispatch.py:593-621`; `coordination.py:1074-1090` |
| `attempts_terminal` trigger | `coordination_schema.py:123-125` |
| Attempt states/checks | `coordination_schema.py:81-89` |
| Job states/`last_document_turn` | `coordination_schema_v2.py:60-64`, `:70-71` |
| Page states/checks | `coordination_schema_v2.py:73-91` |
| Scheduler tables | `coordination_schema_v2.py:103-109` |
| Scheduler seed row `(1,0)` | `coordination_migrations.py:351` |
| Candidate identity | `durable_job_store.py:25`, `:287`, `:517` |
| Claim rejects existing attempt | `durable_job_store.py:520-532` |
| Cancel state/target logic | `durable_job_store.py:714-728` |
| Cancel page predicate (excludes `claimed`) | `durable_job_store.py:725-728` |
| Resume requires `paused_restart`, rejects unknown | `durable_job_store.py:741-786` |
| `_write` not owner-fenced | `durable_job_store.py:97-119` |
| Legacy attempt identity | `durable_dispatch.py:454-456` |
| Deterministic register operation ID | `durable_dispatch.py:342` |
| `register_run` exact replay vs `generation_conflict` | `coordination.py:522-541`, especially `:526-533` |
| G7 fixture operation IDs | `tests/b32_test_support.py:95`; `tests/test_ai_first_coordination.py:68`; `tests/test_ai_first_durable_jobs.py:695` |
| Evidence hard-coded legacy identity | `durable_dispatch.py:346-428`, especially `:379-381` |
| `prepare` fatal on any reserve error | `durable_dispatch.py:487-517`, especially `:500-502` |
| `finish` fatal on settlement error | `durable_dispatch.py:578-587` |
| `_settle` state mapping | `durable_dispatch.py:593-621` |
| Evidence write/read | `durable_dispatch.py:176-253` |
| Adapter start legacy assumptions | `durable_qwen_adapter.py:110-175` |
| Adapter recovery legacy assumptions | `durable_qwen_adapter.py:177-216` |
| Sealed context / test-only resolver | `durable_job_contracts.py:104-144` |
| Test-only resolver call sites | `tests/b32_test_support.py:45`; `tests/test_ai_first_durable_jobs.py:396`, `:687`, `:791` |
| Per-run queue/workers | `orchestration.py:1066`, `:1114-1115`, `:1496-1515` |
| Durable start path | `orchestration.py:1017-1024` |
| Capacities | `dispatch.py:95-99` |
| Single renderer | `pdf_render_worker.py:68-74` |
| HTTP pool 10 | `qwen_vision_adapter.py:41-42` |
| B1 concurrency | `coordination_contracts.py:192-200` |
| Gate/cancel tests | `tests/test_ai_first_b33_atomic_gate.py:95-190` |

### Appendix B — What this document deliberately does not do

- It does not modify production code or tests.
- It does not add schema v3, a second ledger, a new Human Review, B3.5, or B4.
- It does not add a public resolver route, API/auth composition, or `main.py` startup wiring in B3.4.
- It does not bypass B1/B2/B3.3.
- It does not delete Luna artifacts or touch Luna's worktree/branch.
- It does not claim that `artifacts/` existed before this artifact.
- It does not authorize implementation; GPT-6 review and user approval are required next.

---

## 16. Revision changelog

### 16.1 Revision 2 — second-review changes applied

| # | Required correction | Resolution in this revision |
|---|---|---|
| 1 | Re-verify G7 and provide a reproducible test for registration with a different operation ID | §2 G7 is now **confirmed conditionally**, with the exact replay-vs-conflict split and a ledger-level reproducer in §2.2. The accepted fixtures’ operation IDs are cited (`tests/b32_test_support.py:95`, `tests/test_ai_first_durable_jobs.py:695`, `tests/test_ai_first_coordination.py:68`). §5.4/§11.2 replace unconditional idempotency with exact replay **or** verified adoption. Tests IN-12…IN-14. |
| 2 | Exact cancellation transaction rules and state-machine alignment (Revision 2 wording; superseded in Revision 3) | Revision 2 §5.5 described command-side `intent → not_sent`; Revision 3 §5.5/§5.6 corrects this authority boundary: cancel does not touch B1 or attempt-bearing pages, and owner-fenced dispatch/recovery performs settlement. |
| 3 | Replace `render_capacity + 1` with independent limits | §8.4 now uses `R`, `H`, `B`, `Q`, `M`, `W`, `P`, with `render_stage=R`, `admission=min(W,M)`, `page_slots=P`, `send_stage=min(B,Q,H)`. `RENDER_CAPACITY=1` and `max_workers=1` are unchanged; the render slot is released before HTTP. §10.2 and tests WK-15…WK-17 reflect stage semaphores. Live `admission=send_stage=1`. |
| 4 | Internal trusted resolver; no new public API/startup wiring in B3.4 | §9 now scopes B3.4 to `resolve_job`/`resolve_claimed` for scheduler/recovery/tests. `resolve_request`, route/auth composition, and `main.py` wiring are explicitly B3.5/B5; §11.2/§11.3 and AU-13/AU-14 enforce the boundary. |
| 5 | Correct the scheduler seed-row statement | §8.3 now states that migration v2 seeds `durable_scheduler_state` with `(1,0)` (`coordination_migrations.py:351`); `INSERT OR IGNORE` is only a defensive no-op, not the seed mechanism. |
| 6 | Refine the fault taxonomy | New §10.7 separates page-local deterministic, retryable busy/backpressure, process/owner/ledger fatal, `CommitReceiptUncertain`, and integrity/security violations. A stale claim is explicitly not fatal; uncertainty and integrity violations remain fail-closed. FS-16…FS-19. |
| 7 | Justify 30 s and guarantee no watchdog claim theft | New §8.9 defines 30 s as a local SLO/watchdog deadline, not a durable lease; documents the owner-only release paths; explains why a live old worker cannot be superseded; and gives the no-double-claim release table. WK-13/WK-14/WK-18. |

Preserved unchanged in Revision 2: the B3.4a/B3.4b split; the test matrix and crash recovery decision table (rows were added, none removed); G17/G18 and the other additional findings; the no-schema-v3, no-source, no-test, no-commit constraints; `RENDER_CAPACITY=1` and `max_workers=1`; live send concurrency 1.

### 16.2 Revision 2 remaining open questions

1. **Memory budget.** `M` is a formula, not a measured number. Until `peak_page_image_bytes` and `resident_image_budget_bytes` are measured on the target renderer, B3.4 must keep `M=1`; raising `admission` above 1 needs evidence.
2. **Provider quota concurrency.** If a quota scope does not publish a usable concurrency, omit `Q` and rely on `B`, `H`, and 429 adaptation. The exact extraction policy is B3.4b design.
3. **Owner-fenced pre-gate settlement API.** Revision 3 closes the authority question: settlement belongs to the current dispatcher after gate rejection or to new-owner recovery after crash. Only the internal method/signature remains open (§14.23); the command transaction is never a B1 settlement writer.
4. **Pre-gate retry.** `durable_job_pages` has no retry/backoff columns. Auto-retrying a free `not_sent` page would need schema v3 and a new attempt identity; the current recommendation remains no auto-retry.
5. **Observability contract.** Internal counters (queue depth, per-user wait, claim-to-send latency, deferrals, unknown/blocked, recovery outcomes) are proposed but not specified as a stable interface; B3.5 owns exposure.
6. **Public resolver/auth composition.** `resolve_request`, route authentication, tenant weighting, and startup composition remain B3.5/B5 and were deliberately not designed here.

### 16.3 Revision 3 — third-review corrections incorporated

The following requested architecture corrections are incorporated as design changes. This records the authoring change only; it does **not** claim independent GPT-6 acceptance.

| # | Area | Revision 3 change |
|---|---|---|
| 1 | Permanent `attempt_id` invariant | §4.3 now defines `attempt_id = candidate_attempt_id = B1 attempt_id` from atomic reserve onward and immutable through every terminal state, including `cancelled`, `failed`, `blocked`, and `not_sent`; pages without a B1 attempt retain `NULL`. §5.2/§5.4 and R1 make B1 attempt creation and page identity projection one dispatch transaction. State machines, recovery, and SM-23/24 cover the invariant. |
| 2 | `cancelling` versus `blocked` | §4.1 roll-up and transitions now keep jobs `cancelling` while authorized `send_committed` work settles; a financial unknown immediately makes the job `blocked`, including on cancel. A job is never `cancelled` with unknown financial liability. B6 alone owns reconciliation and scope unblock; late final evidence for an already-unknown attempt is B6-only. §5.5, §6, §7, §10 and SM/GT/RC tests align. |
| 3 | `.pending` publication versus settlement | §6.1 and §7 separate filesystem publication recovery from financial settlement. A fully verified `.pending` may safely complete immutable final publication, but pending itself is never indexed or settled. Only independently validated final evidence can prove financial settlement. RC-08/09/20/21 cover promotion, crash replay, and pending-only failure. |
| 4 | Cancel command authority | §5.5 makes `DurableJobStore.cancel`/`_write` increment `cancel_revision` and cancel only pages with no `attempt_id` and no B1 attempt. It never mutates B1 attempts, calls `record_outcome`, or performs `intent → not_sent`; it leaves `intent`, `send_committed`, and `unknown_outcome` untouched. Gate checks `cancel_revision`; the current dispatcher settles gate-rejected pre-gate `not_sent` through owner-fenced B1 settlement, and new-owner recovery does so after crash. B1/page/job projection is atomic. C/SM/GT/RC/SF tests specify both commit orders and no POST after rejected gate. |
| 5 | `finish()` idempotency and races (Revision 3 wording; refined in Revision 3.1) | Revision 3 introduced single-winner settlement, receipt replay/conflict and owner fencing. Revision 3.1 clarifies that public `finish()` is single-use and that exact replay/conflict belongs to the underlying B1 settlement/receipt layer; see §5.6. SF-01…SF-08 and GT-19…GT-23 reflect the distinction. |
| 6 | Tests, limits, and remaining questions | Updated lifecycle, cancel, recovery, evidence, worker, and integration cases; added permanent identity and settlement race cases. Removed the old command-boundary proof question; §14.23 leaves only helper naming/signature open. B3.4a/B3.4b, schema v2 only, live concurrency 1, no production wiring/API/SSH/VPS/provider calls, and no implementation/commit remain unchanged. |

**Document precedence:** Revision 3 sections §4–§15 supersede conflicting Revision 2 prose summarized in §16.1. Revision 3.1 (§16.4) supersedes conflicting Revision 3 wording; Revision 3.2 (§16.5) supersedes conflicting Revision 3.1 wording; Revision 3.3 (§16.6) supersedes conflicting Revision 3.2 wording; Revision 3.3.1 (§16.7) supersedes conflicting Revision 3.3 wording. These are authoring changes only; no independent reviewer acceptance is asserted.

### 16.4 Revision 3.1 — final consistency fixes

| # | Area | Revision 3.1 change |
|---|---|---|
| 1 | Durable-job acquisition | §5.1/§5.2/§5.4 and §6.1 require the actual `DurableDispatcher(..., job_managed=True)` / `DurableJobRunner.start` path to use non-mutating `dispatcher_for_jobs()` and recover before claims. Legacy `dispatcher()` blanket behavior remains B2-only. IN-17/18 exercise the real constructor/runtime selection and legacy preservation. |
| 2 | Canonical settlement operation | §5.6.1 and §6.5 use one `uuid5(UUID(attempt_id), "settlement").hex` and one canonical payload for all settlement callers, including equivalent current-owner/recovery `not_sent`; ambiguous commit read-back uses the same pair. Caller-specific recovery IDs are forbidden. SF-08 covers identical payload/receipt replay. |
| 3 | Public `finish()` versus settlement replay | §5.6 preserves the single-use `DispatchCall.finished` guard: second public call raises before settlement. Exact replay and digest conflict are separately specified at the B1 settlement/receipt layer. SF-01/02/03 distinguish the guard from B1 idempotency. |
| 4 | Settlement atomicity and GT-11 | New settlement always commits B1 attempt, page, and job projection in one transaction. GT-11 is now only a legacy inconsistent-row repair case; GT-23 asserts no new partial visibility. |
| 5 | Overage during cancellation | §4.1 defines job-relevant scopes from immutable billing/quota IDs on this job's persisted B1 attempt bindings. Roll-up and §5.5 cancel target selection keep the job `blocked` if a relevant scope is blocked/reconciliation-required, including overage. C-11, SM-27, GT-24, and IN-19 cover it; no scope is unblocked. |
| 6 | Recovery state/evidence contradictions | §6.2/§6.3 and §7.5 quarantine `blocked + intent`, `claimed + intent + valid final`, and `claimed + intent` without positive legacy-gate proof. They are never treated as ordinary `not_sent` or successful settlement. R-18…R-20 and RC-23…RC-25 specify B1 `unknown`, page/job block, evidence preservation, and B6 ownership. |

Preserved: schema v2; B3.4a/B3.4b split; live concurrency 1; no automatic paid retry; no scope unblock; no public API/runtime route wiring; no source/test/schema/migration changes; no commit or push. No GPT-6 acceptance is claimed.

### 16.5 Revision 3.2 — recovery closure and owner-epoch consistency

| # | Area | Revision 3.2 change |
|---|---|---|
| 1 | Recovery closure | §4.2, §4.3, §6.2/§6.5 and §7.5 define stable `blocked + unknown`, `unknown_outcome + unknown`, and `failed + not_sent` pairs. Repeated recovery verifies receipts/digests and performs only exact replay/read-only no-op; it cannot mint another operation, mutate the financial result, unblock scopes, or project success/`not_sent`. Duplicate §7.5 rows were removed. RC-26…RC-28 test restart after each stable transition; RC-32 tests a missing/conflicting receipt. |
| 2 | Executable legacy gate proof | R-02 and §6.3 require trusted external deployment provenance resolving to the exact historical gate source/contract, matching immutable reserve receipt/identity, and the irreversible atomic `claimed + attempt_id IS NULL → send_committed` marker semantics. Schema-v2 rows alone are insufficient; absent/mismatching provenance or receipts quarantine as unknown. No schema field is added. RC-29…RC-31 cover positive proof, missing proof, and contract mismatch. |
| 3 | Owner-epoch adoption | §4.1/§4.3, §5.1/§5.2 A1/A3/R2/R3 and §6.1 define a closed startup barrier. A new owner adopts each prior-epoch `running` job in a guarded owner-fenced transaction, leaves attempt epochs immutable, recovers prior attempts under the current owner fence, releases only stale no-attempt claims, then verifies epoch consistency before scheduling. Crash/restart safely repeats adoption. IN-20/IN-21 cover prior-epoch attempts and jobs with no unsettled attempts; IN-22 covers a crash during adoption. |
| 4 | Mismatch fault taxonomy | §10.7 removes `dispatch_intent_mismatch` and `dispatch_reserve_receipt_mismatch` from benign page-local deterministic errors. Only exact persisted identity/receipt plus independent §6.3 proof can permit canonical pre-gate `not_sent`; otherwise fail closed/quarantine, with no retry or POST. GT-25…GT-28 cover proven and integrity-mismatch outcomes. |
| 5 | Consistency and test matrix | §4–§7, §10, §12–§15 and this changelog align the new recovery/adoption/proof rules; limitations/open questions state the provenance boundary. Preserved schema v2, B3.4a/B3.4b, the single settlement ID/payload, public `finish()` single-use, atomic B1/page/job projection, overage-aware cancellation, no automatic paid retry, no scope unblock, live concurrency 1, and no public API/route wiring. |

Only `artifacts/B34-deepseek-architecture.md` was edited for Revision 3.2. No implementation, executable test, schema, migration, commit, push, PR, or merge was performed. No independent acceptance is claimed.

### 16.6 Revision 3.3 — legacy terminal page/B1 recovery closure

| # | Area | Revision 3.3 change |
|---|---|---|
| 1 | Legacy `claimed + not_sent` | §4.2/§4.3, §5.2/§5.6.2, §6.2/§6.5 and §7.5 define exact-identity, exact-receipt projection-only repair to `failed`. B1 `not_sent` is immutable; missing/mismatched receipt or conflicting final/pending evidence quarantines. |
| 2 | Legacy cancel-winning `cancelled + not_sent + NULL attempt_id` | Recovery now requires the exact old cancel-winning projection contract, matching immutable cancel command receipt/cancel revision, and canonical B1 settlement receipt before attaching the immutable attempt ID while preserving `cancelled`; otherwise quarantine. Revision 3.3.1 (§16.7 item 2) refines this to reserve + outcome + cancel command receipts. |
| 3 | Legacy `send_committed + terminal` | GT-11 is backed by explicit R-27…R-29 and §7.5 rows. Exact B1 terminal receipt plus matching valid final evidence permits only page/job projection repair. Missing/corrupt/mismatched evidence or receipt quarantines while preserving the terminal B1 fact. |
| 4 | Repair/settlement separation | §5.6.2 defines an owner-fenced, compare-and-set, page/job-only repair idempotency contract. It creates no B1 settlement operation/receipt and never changes B1 attempt state, owner epoch, evidence digest, receipt, or scope blocks. New B3.4 settlement remains atomic across B1/page/job. |
| 5 | Regression matrix and consistency | Added unique RC-33…RC-41 (nine IDs); total unique Test IDs are 177. Updated §4.2, §4.3, §5, §6.2/§6.5, §7.3/§7.5, §12, §13–§16 and appendix evidence index. All Revision 3.2 constraints remain. |

Revision 3.3 changes only this architecture document. No source code, executable tests, schema, migration, runtime, UI, production, commit, push, or PR was changed or created. B3.4a/B3.4b remain separate; no implementation is authorized or claimed, and independent GPT-6 acceptance is not asserted.

### 16.7 Revision 3.3.1 — R-30 invariant and case-specific legacy receipts

Independent GPT-6 review confirmed the overall direction of Revision 3.3 but found two remaining internal inconsistencies. This revision corrects only those; it introduces no new assumption about the old implementation beyond the cited source.

| # | Area | Revision 3.3.1 change |
|---|---|---|
| 1 | R-30 permanent invariant | §4.3 invariant 2 now names the third historical `attempt_id IS NULL` case: `claimed + B1 unknown`. Source evidence is the legacy `dispatcher()` at the accepted base (`coordination.py:430-454`): it selects every B1 `intent` attempt, calls `_block_attempt`, sets `state='unknown'` directly, creates no outcome operation receipt, and does not update `durable_job_pages`. R-30 policy is unchanged: B1 `unknown` stays immutable, scopes stay blocked, the attempt ID is attached only on proven identity, and recovery never sends or performs settlement. |
| 2 | Case-specific receipt requirements | §5.2 LR1, §5.6 rule 9/§5.6.2, §6.2 R-23/R-25/R-27/R-30, §6.5, §7.3, §7.5 and §12.3 no longer require one blanket canonical settlement receipt. They now separate: R-23 = immutable reserve receipt + canonical historical outcome receipt; R-25 = reserve receipt + canonical `not_sent` outcome receipt + historical cancel-winning proof including the durable cancel command receipt; R-27 = reserve receipt + canonical terminal outcome receipt + valid final evidence; R-30 = immutable reserve receipt + exact persisted B1 attempt/page/run/claim identity and original `owner_epoch`, with no outcome receipt. |
| 3 | R-30 absence is not an integrity mismatch | §5.6.2, §6.2, §6.5 and §7.3 state explicitly that no outcome settlement receipt exists or is required for R-30. If an outcome receipt is unexpectedly present, its meaning is verified against the immutable attempt facts; an unsupported or contradictory combination quarantines rather than being guessed at. |
| 4 | LR1 idempotency contract | The idempotency marker is the exact target state plus the case-specific immutable receipt(s): outcome receipt for R-23/R-25/R-27; reserve receipt for R-30; the cancel command receipt is additional evidence for R-25. No new table, operation ID, or B1 receipt is added. |
| 5 | Consistency and preserved assertions | §4.2/§4.3, §5.2/§5.6, §6.2/§6.5, §7.3/§7.5, §12.3 and §16.6/§16.7 were cross-checked. Preserved explicitly: R-36/RC-37 for the legacy blanket `intent → unknown` must not create an outcome receipt and use the existing reserve receipt. |
| 6 | Test IDs, constraints, and table integrity | No RC ID was added or removed. **Counting method:** a Test ID is a unique first-column ID in the §12 test matrix (§12.1–§12.8), regardless of prose mentions. That matrix contains **177** unique IDs: SM 27, GT 28, RC 41, FS 19, AU 14, WK 18, IN 22, SF 8; every series is contiguous with no gaps or duplicate definitions. A document-wide count of unique ID-bearing table rows is **218**, which is not a Test ID count: 177 §12 Test IDs + 11 `C-xx` cancel-scenario rows in §5.5.3 + 30 `R-xx` recovery-decision rows in §6.2. The §5.6.2 case table reuses `R-23/R-25/R-27/R-30`, so raw ID-like rows total 222 while unique IDs total 218. Schema v2, the B3.4a/B3.4b split, live concurrency 1, B6 ownership of reconciliation/unblock, the strict R-30 policy, and the R-23–R-41 scope are unchanged. Revision 3.3.1 refines the over-general receipt wording recorded in §16.6 items 2 and 4 and in Revision 3.3 §5.6.2/LR1/§7.3. It also declares the ninth `Idempotency` column that R-23…R-30 already used in §6.2 and pads R-01…R-22 with an empty cell, so those per-case idempotency notes are no longer dropped at render time; no row semantics changed. |

Revision 3.3.1 changes only this architecture document. No source code, executable tests, schema, migration, runtime, UI, production, commit, push, or PR was changed or created. No independent GPT-6 acceptance is asserted.
