# AI-first bulk review: owned-copy performance patch

Date: 2026-11-03. Base: `1d10c6de652228a42e08b256596d2cbad0754cc8`.
That accepted stage-2 commit is local on `codex/ai-first-bulk-review`.
This follow-up is an uncommitted experiment on `codex/ai-first-bulk-perf` in a
separate worktree. No production, cloud requests, configuration or data migration.

## Measurement before changing runtime code

`tests/benchmark_ai_first_bulk_review.py` uses synthetic nine-column tables with
25, 50 and 100 rows, and 0 or 1,000 pre-existing ledger events. Both measurements
used the same Windows machine, Python 3.12.10, environment and benchmark script,
three repetitions per case, run sequentially without a competing test suite.
The synthetic draft/ledger SHA-256 matches before and after for all six cases.

Each sample uses a fresh short system TEMP directory and the real
`AIWorkspaceStore` journal. Setup, input hashing, result checking and replay are
outside the timed calls. Preview and commit time `execute_bulk_review()`, so
commit includes token calculation, validation and disk publication. A lightweight
wrapper counts and times deep `AIReviewState.model_copy` calls; it is identical
in both runs. Timings include that instrumentation and are not a server SLA.

Before this patch, item batches clone the entire ledger `1 + 2 * row_count`
times; service batches clone it `1 + 3 * row_count` times. In the 100-row,
1,000-event item case, 201 copies consumed a median 7,894.578 ms in preview and
7,936.743 ms in commit: approximately 96.6% and 95.5% of the corresponding total
time. This supports removing repeated copies rather than changing storage or
indexing the entire review framework.

## Narrow runtime change and guarantees

The bodies of the existing two row operations are reused as private in-place
applicators. Public `record_row_role_classification()` and `record_row_decision()`
still deep-copy their input and return a separate state; signatures are unchanged.
The private applicators are used only on the owned deep copy created by
`prepare_bulk_review()`, never on its input or a shared/persisted review object.

The loop order, guards, role snapshots, dependent invalidations and event
creation are unchanged. Final full-state validation still runs. No store write
occurs until all actions succeed. Any late apply/validation error discards the
working copy. The existing redo journal, lock, editor consumption, receipt,
request digest and replay path are unchanged. All 100-row batches now perform
one deep ledger copy, including service-role batches. The final serialization
and validation remain full-ledger operations.

No memoization, monkey-patching, copy suppression flag or global/shared mutable
working state is introduced in runtime. Measurement/test patches are confined
to the benchmark and regression tests. No persisted format changes; existing
review files, event history and checkpoints require no migration.

## Results

Medians of three repetitions, milliseconds:

| Rows | Prior events | Preview before | Preview after | Speedup | Commit before | Commit after | Speedup |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 25 | 0 | 74.300 | 5.231 | 14.2x | 118.845 | 50.388 | 2.4x |
| 50 | 0 | 290.455 | 9.153 | 31.7x | 372.497 | 64.089 | 5.8x |
| 100 | 0 | 1,222.840 | 27.185 | 45.0x | 1,274.540 | 80.140 | 15.9x |
| 25 | 1,000 | 1,769.074 | 55.959 | 31.6x | 1,886.259 | 138.343 | 13.6x |
| 50 | 1,000 | 3,700.339 | 64.935 | 57.0x | 3,786.504 | 152.111 | 24.9x |
| 100 | 1,000 | 8,168.406 | 111.294 | 73.4x | 8,311.912 | 180.848 | 46.0x |

Local aggregate JSON reports retain every run, deep-copy count/time, Python
version, runtime-code hash and synthetic input hash. The earlier hardening
measurements (9.56/8.72 seconds) and these fresh baseline values are different
runs; speedups above use this experiment's matched before/after medians only.

Reproduce in each checkout using the same script and a different output path:

```powershell
python -B tests/benchmark_ai_first_bulk_review.py --repeat 3 --output <new-report.json>
```

The script refuses to overwrite an existing report, checks source-state
immutability and preserved history, reopens persisted results, and verifies
idempotent replay outside the timed calls. It contains only synthetic data.

## Regression verification

`tests/test_ai_first_bulk_review_performance.py` compares the optimized batch
against the sequential public operations with the former per-action copying.
Only generated event IDs and timestamps are controlled to make exact comparison
possible. It compares complete persisted ledgers, event order, dependent roles
and decisions, receipt/return token, consumed editor and replay results for:

- corrected standalone items;
- components linked to a confirmed parent;
- changing a parent into a service row, invalidating dependent decisions;
- a manual row on `no_specification`.

Additional tests assert exactly one ledger copy for valid 100-row item and
service batches with 1,000 historical events, nested output/input isolation,
unchanged public single-row immutability and no publication/input changes after
an exception following the second in-memory decision. Existing stage-2 tests
cover saved corrections, unrelated buffers, auth, concurrent tabs, interrupted
journal publication, hard process exit and replay after restart.

The targeted new and stage-2 profile passed: 42 tests. The full AI-first profile
passed 245 tests in 120.30 seconds, with zero failures, errors or skips. All 11
AI-first JS scripts passed, including native Edge at 1280/1000 pixels.
No full application/browser smoke is claimed
for this backend-only follow-up; the existing native Edge UI regression is run
as part of the JS suite.

## Remaining limits

The 100-row bound, process-local locking and supported one-worker deployment
remain unchanged. Token/receipt checks, final validation, serialization and disk
I/O still scale with history size. Larger ledgers and concurrent server load
need their own measurements; this patch introduces no latency guarantee or new
multi-process transaction claim. The private applicators require an owned
working copy, enforced by the current call sites and isolation regressions.
