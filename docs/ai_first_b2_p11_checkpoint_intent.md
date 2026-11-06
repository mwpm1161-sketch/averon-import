# B2 P1.1 — SQLite intent before checkpoint projection

Scope: only P1.1 plus a documentation-only P1.2 handoff. Existing B2 worktree
codex/qwen-durable-dispatch-b2 is retained; HEAD remains
413dce7b5b5a02230e96e743ee38e8795c30259a. No commit, push, merge, SSH, deploy,
production/data/UI/Human Review/XLSX change or real Qwen request. Live concurrency
remains 1. No full Python/JS rerun in this step; combine those profiles after P1.2.

## Fixed sequence

Durable dispatch now performs:

1. Serialize/validate the exact request and prepare its bounded caller context.
2. B1 reserve(): commit the real atomic financial/quota reservation and intent;
   only its fresh successful receipt can produce the existing one-shot permit.
3. Invoke the existing checkpoint callback and fsync dispatch_intent_at.
4. Consume/revalidate the permit, current owner/run/binding/policy and cancellation.
5. Mark actual transport delegation and call the injected or standard transport.

QwenVisionClient delegates its before_dispatch callback to DurableDispatcher for
this path. Legacy mode keeps its original callback position. Replay/lost commit
receipt never reaches the checkpoint callback. Callback waiting/SQLite waiting
is excluded from the actual HTTP deadline and latency; the clock starts at the
actual delegation marker. No retry, quota, price or financial settlement rule is
relaxed. B1 reserve()/record_outcome(), validation for new POST, result evidence
format and schema v1 are unchanged.

CheckpointStore.open(recover_running=False) loads durable running records without
independently rewriting them to pending/unknown. Its default True preserves the
existing legacy recovery behavior. PageOrchestrator selects False only for the
explicit durable adapter. The same checkpoint process/execution locks, atomic
writer, ACL and fsync remain in place.

## SQLite-first recovery

The new narrow checkpoint_attempts(owner, expected_run) read is protected by B1's
existing verified connection/owner transaction, exact current Run metadata and
active generation fence. It reads immutable admission history; it neither
settles money nor grants a permit. A failed/busy/fenced read is not absence.

| SQLite + checkpoint facts | Action before scheduling |
| --- | --- |
| No committed page attempt; interrupted running preparation; no markers/sent evidence | Preserve the interrupted preparation as a failed history entry, mark the page pending, then use normal fresh B1 admission. This is evidence of no previous authorization, not a replay permit. |
| Committed intent/unknown, with missing marker or missing page checkpoint | Project unknown_outcome from SQLite; retain holds/stops; do not call prepare()/POST again. |
| Historical terminal attempt without a recoverable result | No automatic POST; retain accounting and mark missing page outcome conservatively. Existing completed checkpoint is retained only with a matching terminal ledger state. |
| Proven not_sent ledger attempt | Failed/non-replayable page; no new permit from its consumed identity. |
| Old checkpoint dispatch marker/unknown/completed claims but no ledger attempt | Fail closed on checkpoint_ledger_history_mismatch; never erase historical ambiguity or infer zero spend. |
| Database/schema/owner/generation unavailable | Abort before checkpoint classification or HTTP. |

When a reserve error is followed by a successful owner/current-run read proving
no history for that page, this preparation is classified failed/not dispatched,
not a phantom unknown. Admission remains stopped in that dispatcher; no fallback
or implicit retry is introduced. If the intent exists or the read cannot prove
absence, the previous conservative ambiguity is preserved. Physical lost-commit
receipt tests still retain the committed intent and never authorize a POST.

Recovery may project dispatch_intent_at when observing an existing SQLite
intent. That timestamp is the observation time, not proof/time of a paid POST.
No SQLite financial row is created from checkpoint claims.

Proven uncommitted preparations remain in checkpoint history. If a later valid
result recovers after another crash, that non-dispatched prefix is preserved;
historical dispatched/ambiguous prefixes are not silently discarded or replayed.
The checkpoint format remains v2, and B1 still has one paid attempt/page/run for B2.

Absence checks assume the same verified existing financial ledger and its B1
durability contract. They do not authorize empty bootstrap, restore an old ledger,
clear a reconciliation stop or substitute a process-local financial authority.
Production bootstrap/rollback and VPS-specific ACL/fsync remain release gates.

## Reproducible fault tests

tests/test_ai_first_b2_p11.py uses the real B2 client, real SQLite and the shared
fake-only subprocess worker. New worker windows:

- before_intent_commit: pause a real mutated SQLite transaction before commit;
  another connection sees no attempt, checkpoint has no dispatch marker, zero
  POSTs. Kill/restart permits only a newly authorized first POST, with no unknown.
- after_intent_commit: pause after reserve() returned its real committed fresh
  receipt, before the checkpoint callback. The intent exists, checkpoint has no
  marker, zero POSTs. Kill/restart retains unknown/hold and never reaches prepare,
  including when the page checkpoint file is removed.

Other P1.1 cases cover preparation -> later evidence-fsync crash -> recovery with
history intact; historical ambiguity without ledger data; no independent
checkpoint normalization before authority; callback/fsync failure; cancellation
during callback; callback time outside HTTP deadline; owner/generation fencing;
known budget denial and a real failed pre-commit SQLite transaction. All files,
credentials and HTTP responses are synthetic; external sockets/live transport
are denied. No historical-settlement predicate has been implemented.

| Profile | Result |
| --- | --- |
| Red reproducer against original B2 | 3 expected failures at the old early checkpoint marker; 8 deselected; 13.20 s. |
| Initial P1.1 green | 11 passed; 16.70 s. |
| Initial necessary B2/B1/Phase A/all Qwen regressions | 464 passed, 1 real Linux-only skip; 157.82 s. |
| Final necessary regression profile | 466 passed, 1 real Linux-only skipped; 150.64 s. Includes all 13 P1.1 cases and unchanged 71 B2 cases. |

Run with the existing project Python and a private basetemp outside the checkout:

    python tmp/phase-b2/offline_validate.py <checkout> tests/test_ai_first_b2_p11.py -q --basetemp=<private-temp>

The bundle includes the exact command/metadata, JUnit and logs. Only the new
P1.1 subprocess windows were run against old B2 for the red check. Full combined
Python/JS profiles are deliberately deferred to the merged P1.1+P1.2 revision.

## P1.2 remains unimplemented

validate_dispatch(..., evidence_only=True), record_outcome(), _recover_result()
and the live _settle() financial code are unchanged. The existing historical
recovery restrictions remain until Luna's separately authorized work.

Read ai_first_b2_p12_settlement_handoff.md before that implementation. Its safe
v1 contract requires a verifiable original nonsecret config/pricing snapshot;
v1 stores a pricing hash, not its preimage. Without the matching historical
context, retain the hold and stop rather than applying today's pricing. No SQL
migration is needed for validated existing intent/unknown -> terminal facts;
late refinement of sealed terminal costs and reconciliation exits remain B6.
