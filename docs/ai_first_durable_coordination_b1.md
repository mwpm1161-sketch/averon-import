**Averon Import — B1 Durable Coordination Core, 5 ноября 2026**

Base: `d72a31b02104dd0e71092f53ffe3e52d63e385b9`. Branch: `codex/qwen-durable-coordination-b1`, separate managed worktree. No commit, push, merge, SSH, deploy or real Qwen requests. The existing tracked application files are unchanged. This is an inert library and offline tests, with no startup registration or HTTP capability.

**Implemented boundary.** Three new modules: `coordination_contracts.py` (strict immutable input DTOs and money), `coordination_schema.py` (schema v1), `coordination.py` (explicit bootstrap, ownership, transactions, reservations and trusted-core settlement facts). No generic job queue, document-state writer, provider import, scheduler, admin API or secret lookup. The fresh-process import regression proves no application/provider composition and no automatic data-directory creation. Bootstrap's core global cap defaults to 1; tests explicitly select up to 10 with fake scopes. The live factory still uses concurrency=1.

Public synchronous operations:

- `CoordinationLedger.bootstrap(root, Bootstrap, clock=...)`: create a NEW private directory using explicit installation/authorization/history attestations and opening balances.
- `CoordinationLedger(root, installation_id=...)`: open existing verified storage using SQLite URI `mode=rw`; no implicit initialization or repair.
- `ledger.dispatcher()`: nonblocking process OS lock, then durable owner epoch/nonce and restart recovery; returns a process-bound context manager with `renew()`.
- `register_run`, `put_binding`, `revise_billing`, `revise_quota`: current owner required, strict revisions and idempotent operation receipts.
- `reserve(owner, Attempt)`: one short transaction checks generation, active binding, expected policy revisions, global/per-scope capacity, account/run budget and request cap; inserts intent and all scope reservations atomically.
- `record_outcome(owner, Outcome)`: trusted-core terminal/unknown/not-sent facts; separates monetary liability from quota/HTTP terminality.
- `snapshot()`: private accounting inspection; never an authorization.
- `schema_contract()` / `require_reconciliation_schema()`: read-only capability/version declaration; schema v1 explicitly refuses reconciliation and requires schema v2.

The returned `Reservation` is a B1 ledger receipt, NOT an HTTP send permit. Replay returns `newly_created=False` and cannot create another charge. B2 must validate exact request/target/credential and enforce a one-shot transport guard, including injected transports. Future async callers must execute these synchronous calls off-loop and drain outstanding commit work on cancellation.

**Schema and integrity.** SQLite `application_id=0x41564231`, `user_version=1`; STRICT tables require SQLite 3.37+. `journal_mode=DELETE`, `synchronous=EXTRA`, `foreign_keys=ON`, with readback validation, bounded busy timeout and `BEGIN IMMEDIATE`. Each connection validates installation ID, schema/version and expected schema digest. Opening checks quick_check and foreign_key_check. Unexpected files, wrong ACL, reparse paths, missing/replaced database/lock and unsupported schema reject operation. An optional hot rollback journal may disappear during an ACL read; only that disappearance is tolerated. Existing permissive journals remain errors.

| Table | Durable role / key constraints |
| --- | --- |
| `meta` | One installation, bootstrap authorization/manifest, monotonic epoch, owner nonce, boot/high-water clock, lease deadline, explicit global concurrency 1..10. |
| `billing_scopes` | Stable scope ID, current revision/ceiling, immutable opening known/held amounts and attestation, reconciliation stop. |
| `quota_scopes` | Stable scope ID and current request/token/concurrency/window policy; immutable opening history and original window; reconciliation stop. |
| `credential_bindings` | Immutable versioned credential-generation/tag/target metadata, billing scope and digest; one active revision per binding. No key material. |
| `binding_quotas` | Immutable binding-revision to quota-scope FK edges. |
| `documents` | Tenant/document identity and active run/generation for fencing. It contains no OCR/review data and is not a document-state migration. |
| `runs` | Immutable run metadata, source/config digests, document budget and request cap. Not an executable queue. |
| `attempts` | Unique attempt ID and operation ID; unique run/page/attempt number and claim sequence; intent owner epoch, immutable request/binding metadata/reserve and terminal evidence/cost. |
| `attempt_quota_reservations` | One row per attempt/scope, frozen policy revision/window/token bound and policy snapshot. |
| `operations` | Immutable request JSON/digest, operation kind, result receipt and epoch/boot/time. Preserves bootstrap and policy authorization history. |

JSON columns contain only typed coordination metadata, never HTTP bodies, images, prompts or extraction text. Scope IDs, tags and document identifiers remain private metadata. SQL FKs and uniqueness protect references; triggers prohibit erasing operations/attempts/reservations/binding history, changing original identities/opening balances, clearing a reconciliation stop or rewriting final outcomes. STRICT INTEGER rejects persisted fractional money. API DTOs reject all Python float/bool money input.

Migration strategy: v1 creation is explicit only. Existing v0/unknown/future schemas, partial bootstrap and schema mismatch fail closed. There is no automatic ALTER, downgrade, legacy checkpoint import or financial reset. B6 requires an explicit reviewed migration to schema v2 under maintenance ownership, verified backup and one transaction; raw ledger backup restoration is not a safe code rollback. Financial history is never replaced to resume an older binary. Standalone exported SQL is in the review bundle as `schema-v1.sql` and is generated from the actual schema constant. The read-only SQL view `schema_contract` declares `reconciliation_min_schema_version=2` and `explicit_transactional_migration`; the core checks the same contract and refuses B6 on v1.

**Accounting invariants.**

1. Stable billing identity is independent of credential, target and quota identity. Different billing scopes can share a quota; different quotas can debit the same billing scope.
2. Bootstrap requires caller-supplied opening amounts, completeness assertions, manifest/evidence digests and authorization ID. Incomplete history, opening unknown liability or unknown in-flight operations blocks admission. B1 does not verify provider billing truth; B0/B5 must substantiate the attestation. Absence of a file is never converted into zero spend.
3. A credential rotation adds an immutable binding revision; it cannot reset spent/held/quota history. Binding IDs or policy revisions do not partition existing scope obligations.
4. A reservation satisfies both `opening known + opening held + SUM(actual or retained reserve) + new reserve <= billing ceiling` and the run budget. Request cap is checked in the same transaction.
5. Persisted money is nano-RUB (`10^9` units/RUB), strict signed int64. Decimal conversion rounds upward exactly using coefficient/exponent integer arithmetic, including extreme fractional tails. Inputs/amounts outside int64 reject; aggregate sums use Python integers, avoiding SQLite SUM overflow. Actual overruns are preserved rather than clamped and stop admission.
6. Intents and unknowns pin request/token/concurrency reservations. Terminal responses with no usable usage retain the full monetary reserve. Proven not-sent releases only a still-current owner's intent; a recovered unknown cannot be relabelled not-sent.
7. Terminal quota cooldown starts from recorded terminality, not admission. Changing windows uses the longer of original/current policy; opening history also retains its original window. No revision can silently age a permit out early.
8. Global outstanding intents/unknowns and each applicable quota concurrency cap are checked, even across unrelated billing/quota groups. All quota edges and the financial intent commit together, or none do.
9. New attempts require the current process-bound OS lock, nonce/epoch, nonexpired lease and active document generation. A TTL alone never steals a live owner's lock. Lease objects cannot be pickled or reused by a fork child.
10. Reopening ownership marks prior unsettled intents unknown and stops all affected billing/quota scopes. It neither retries them nor releases their liability. No B1 API clears this stop.
11. Every operation ID is immutable and payload-bound. A duplicate returns history; changed payload/kind conflicts. No error path falls back to local accounting or issues another new receipt after an ambiguous commit.
12. Before-commit expiry rolls back. If commit/fsync succeeds but the receipt is lost or the lease expires, history remains committed and replay is non-new. B1 never calls a transport in either case.

Clock contract: Windows uses the kernel boot identifier (`NtQuerySystemInformation(SystemBootEnvironmentInformation)`) plus `GetTickCount64`; Linux code uses `/proc/sys/kernel/random/boot_id` and CLOCK_BOOTTIME. UTC is not used for expiry. Same-boot backwards uptime rejects. Across boot changes, known terminal quota history receives a fresh conservative window; unresolved intents remain unknown with full holds. Fake-clock tests exercise these transitions, and real Windows subprocesses verify shared boot identity/uptime. A real Linux reboot was not performed.

**Validation and baseline.** Same Windows host, project venv, offline wrapper, separate temporary data/basetemp per run, empty AI/Vision credentials and live flag disabled. Only synthetic coordination inputs/fake Phase A transports are used. B1 subprocess helpers forbid socket connect/connect_ex. No original user workspaces, keys, rates, checkpoints or export files are opened by the core.

| Profile | Result |
| --- | --- |
| Full Python baseline d72a31b | 3628 passed, 5 failed, 2 skipped, 1 deselected; 556.76 s. |
| Reviewed B1 core baseline | 60 passed, 1 genuine Linux-only skipped; 15.28 s. |
| Final P1 + B1 / Phase A focused profile | 170 passed, 1 Linux-only skipped; 49.30 s. Includes 66 B1 core passes, six more than reviewed B1. |
| Phase A + earlier B1 focused profile | 159 passed, 1 skipped; 46.90 s; later new edge cases are included in final core/full runs. |
| Reviewed B1 full Python baseline | 3688 passed, the same 5 failed, 3 skipped, 1 deselected; 568.02 s. |
| Full Python final P1 | 3694 passed, the same 5 failed, 3 skipped, 1 deselected; 578.56 s. No new failures versus d72a31b or reviewed B1. |
| Full JS baseline | 23 passed, 1 authenticated full-app fixture skipped; includes two native headless Edge harnesses. |
| Full JS final B1 | 23 passed, 1 authenticated full-app fixture skipped; same results as baseline. |

Python suite: `tests -q -m 'not integration'`, with external socket connections rejected by the shared wrapper. The five known baseline failures are the two global CSS overflow assertions in `test_one_c_history_sourcing_ui` / `test_product_ui` and three Tesseract-unavailable cases in `test_processing_coordinator`. Final failure testcase IDs exactly match baseline: no new or resolved failures. Machine-readable XML comparison is included in review artifacts. The baseline's two skipped tests require private real XLSX/cache fixtures; one integration test needs a real engineering PDF and is excluded in both runs.

An intermediate full run was cancelled after additional exact-money/slow-commit safety changes; it is not presented as the final validation. The final full run uses the final core revision. Logs preserve the initial hot-journal race reproduction and the later verified results.

| Crash/fencing/financial scenario | Evidence / expected invariant |
| --- | --- |
| Two independent processes race for dispatcher | Real OS lock; one owner. A killed owner releases the lock; successor epoch increases. |
| Two processes repeatedly hand off ownership | Shared RPM, TPM and budget each independently capped at two accepted/settled synthetic attempts; neither credential/process restart resets history. |
| Ten concurrent caller threads/connections in one owner | Only two accepted under configured RPM, TPM, concurrency or budget limit. Owner guard and SQLite short transactions serialize authority. |
| Different billing/shared quota; shared billing/different quota | Separate tests assert correct global debit and shared quota denial, with no partial second-scope reservation. |
| Child killed before SQLite commit | Actual mutated SQLite transaction and hot journal recover to zero attempt/charge; original operation can safely create once. |
| Child killed after commit, before method receipt | Intent recovers unknown; replay is non-new, full cost retained, next admission blocked. |
| Owner killed after a durable intent | Unknown retains all obligations; not-sent cannot be inferred. |
| Lease expires before commit / during slow commit | Before commit rolls back; after commit cannot return fresh authority; replay remains history. |
| Repeated operation/outcome or conflicting payload | One expense; changed data rejects. Final outcomes and SQL history cannot be overwritten or deleted. |
| SQLite busy held by another process | Actual independent BEGIN IMMEDIATE holder causes bounded safe refusal; no attempt is inserted; retry after lock release creates exactly one. |
| Write failure | Actual SQLite query_only/READONLY plus SQLITE_FULL injection and lost-commit-receipt injection; no false new receipt. Physical disk exhaustion was not induced. |
| Bad ACL, corruption, file replacement, schema/future-version mismatch | Native Windows ACL tampering on a synthetic ledger and actual malformed/replaced SQLite files reject without repair/rebootstrap. |
| Credential/policy change | History retained, stale snapshots rejected; longer/original windows preserved; pause cannot be cleared by rotation or increased ceiling. |
| Actual above reserved bound / unknown cost | Actual retained even at int64 limit, scope stopped; missing usage remains held. |
| Import/integration isolation | Fresh subprocess import creates no data root and imports no main/Qwen client. Existing tracked files unchanged. |

**Linux evidence and remaining gates.** The independent reviewer reports all original 61 B1 tests passed on Linux in isolation with a POSIX ACL stand-in, including fork/flock and process contention. This is external review evidence, not a new local run or a VPS ACL/durability result. This host still has no usable WSL/subsystem or Docker; the real Linux-only fork/flock test remains skipped locally, without os.name emulation. New P1 tests are locally verified on Windows. Native VPS Linux ACL/fsync/storage durability and old-runtime UID egress/rollback enforcement remain unverified. To close those gates, run the updated tests with a private basetemp OUTSIDE the checkout and perform the separately approved target-specific isolation checks.

**B2–B6 gates remain open.** B0 full-path paid-deny guards and external account mapping; exact request/credential/target validation; one-shot HTTP capability and cancellation integration; actual provider usage/tariff validation; deterministic immutable result evidence writer and fsync-to-SQL recovery; durable job API/fair scheduler/resource byte cap; Human Review publication CAS/document locks/deletion; legacy data import/rollback; audited administrative reconciliation and live pilots. `record_outcome` accepts trusted typed core facts, not uploaded files or provider status guesses. Only future validators can establish real terminal/not-sent evidence. No B1 claim is made that the existing runtime's bypass paths are already closed, or that raw backup restoration is safe.

Review artifacts live under ignored `tmp/phase-b`: full patch from d72a31b, exported schema, JUnit/logs, JS results and a reproducible packaging helper. No temporary test ledger, real secret or user document is part of the patch. Work stops for independent review; live concurrency stays 1.

**P1 contract resolution: sealed v1, explicit migration before B6 (option 3).**

No existing financial trigger is weakened. `attempts_terminal` still rejects late UPDATE to a terminal attempt, including one without usage; original opening attestations and reconciliation stops remain protected. B1 does not advertise a mutable reconciliation API. The schema itself now declares the required upgrade, and `require_reconciliation_schema()` fails with `reconciliation_requires_schema_v2`. The selected mechanism is a versioned migration boundary; append-only B6 receipt/projection design is demonstrated only by test DDL, not installed in production schema v1.

The eventual B6 migration must preserve every original financial row/receipt, introduce separately audited reconciliation data/readers, and atomically change BOTH SQLite user_version and version-bound metadata/schema. Version-bound metadata may need reconstruction; ownership epoch cannot be reset. Quiesce senders and hold maintenance OS ownership. Do not restore an older ledger or relabel unknown as not-sent. Admin/evidence/exit-gate validation and the real migration implementation require B6 review. This small v1 edit finalizes an unreleased B1 schema fingerprint: earlier synthetic pre-P1 layouts fail verification rather than being repaired or replaced. No production ledger was created, migrated or reset.

Tests give an actual SQLite migration witness:

- v1 rejects terminal-cost edits and billing/quota stop clearing, and exposes a read-only minimum-v2 contract.
- A test-only v2 adds immutable cost/reopen receipts and effective views; every original attempt, policy, opening balance, operation and reservation remains identical. Original ownership metadata is archived, and the new metadata keeps its epoch while declaring v2.
- The witness can project a terminal no-usage attempt to a confirmed cost and an approved stop to an effective reopen without changing its original fields. These inserted test facts are NOT production B6 admin/evidence validation.
- Faults after DDL, metadata reconstruction and version update roll back to byte-equivalent table values and the original schema fingerprint/version.
- After a successful v2 transition, both an existing cached v1 ledger object and a newly opened v1 reader reject the database. v1 cannot ignore adjustment rows and dispatch with obsolete accounting.
- No B6 API, production migrator, approval workflow or paid retry has been implemented. Tests establish schema extensibility, transaction atomicity and history preservation, not the future administrative exit gates.

**Historical-query measurement and bounded optimization.**

The before/after benchmark uses private synthetic ledgers with 1k/5k/10k fully linked terminal attempts plus five current intents. Helpers run 25 repetitions on one connection; the admission measurement has 10 repetitions and includes reserve plus proven-not-sent close (TWO durable transactions). Each size asserts exact liability and quota usage. It uses no provider, original document or real billing data. Python 3.12.10, SQLite 3.49.1, Windows host; no concurrent regression process during the final benchmark.

| Historical attempts | `_liability` p50 ms, before → after | `_quota_usage` p50 ms, before → after | Reserve + close p50 ms, before → after |
| --- | --- | --- | --- |
| 1,000 | 2.35 → 1.25 | 4.43 → 2.87 | 53.71 → 45.07 |
| 5,000 | 13.68 → 4.84 | 69.22 → 13.47 | 132.82 → 62.33 |
| 10,000 | 28.39 → 9.18 | 143.17 → 27.03 | 247.38 → 94.04 |

At 10k, p95 changed from 29.56 to 10.90 ms for liability, 146.30 to 29.72 ms for quota, and 258.90 to 97.75 ms for the two-transaction admission/close. Database size rose from about 36.71 MB to 37.82 MB (~3%). Timings are local measurements, not a provider throughput claim or a hardware-independent acceptance threshold.

The measured scan cost justified only covering indexes and streaming accumulation: billing/run indexes include actual/reserved amounts; quota joins use covering metadata indexes instead of loading wide JSON rows. INDEXED BY is deliberate and bound to the verified schema; missing indexes fail closed. `_liability` makes one cursor pass and excludes not-sent in SQL; sums still use Python arbitrary-precision integers. A regression checks exact totals above int64, zero actual cost, retained unknown cost and not-sent exclusion. No SQL SUM, mutable cached counters, expiry pruning, tariff change or budget/quota semantic change was introduced. `_quota_usage` retains the original boot/window logic. Both algorithms remain O(history); revisit further optimization only with larger measured workloads.

Raw benchmark JSON, the reviewed code baseline and the incremental follow-up patch are included in the review bundle under `artifacts/p1`; the full patch, exported schema and final comparison are under `artifacts`. Migration tests are part of both patches. The incremental patch is checked against the exact reviewed B1 patch applied to a clean d72a31b archive; it reconstructs the same files as the full final patch.
