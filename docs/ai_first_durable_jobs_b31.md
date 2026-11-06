# B3.1 — registered SQLite schemas and explicit migration

Base: `94d6ef391f198fb323a6b86abf0c6425570baa76`. This library-only step
implements no scheduler, command API, execution staging or publication. There
is no application startup import, migration CLI with a force flag, production
bootstrap or provider call. The existing live composition remains unchanged.

## Version and capability contract

`coordination_schema.py` and its schema v1 SQL are unchanged. Default open and
explicit bootstrap still select only v1. `coordination_schema_v2.py` registers
one additional exact schema: financial model v1 plus the durable job/snapshot/
page/command/scheduler tables, immutable migration receipts and a constant
capability view. Unknown versions or extra/missing/changed SQL fail closed.

v2 requires an explicitly provisioned private maintenance control root and
`schema_version=2` on open. Neither opening nor importing creates/migrates a
ledger. Bootstrap refuses v2 before creating a target directory. Capability
checks validate actual storage/digest and use a compiled allowlist, not version
arithmetic or operator flags. Both v1 and v2 deny B6. v2's contract reserves v3
for separately approved reconciliation; no v3 reader or mutation is implemented.

The new schema contains only immutable financial Run/config/pricing/Binding
preimages and metadata/hash references for private execution artifacts. There
is no execution prompt, key, image, OCR or HTTP body column. Snapshot JSON
columns reject unknown top-level fields, including user-context/key/body fields.
Complete typed DTO admission belongs to B3.2; this step supplies constraints,
not a snapshot writer or job state machine implementation.

## Maintenance exclusivity

`MaintenanceLayout.provision()` is a separate explicit operator/library action
into a NEW private control directory outside the dedicated ledger directory.
It binds installation and canonical ledger path. Ordinary clients never create
or repair it. Every v2-aware ledger holds a native shared OS lock for its whole
lifetime, including idle connections. `close()` refuses while a connection or
dispatcher owner is alive. Objects must be explicitly closed; GC is only a
best-effort fallback. A B2 caller closes the dispatcher, then its ledger client.

The order is maintenance guard -> original dispatcher OS lock -> SQLite.
Migration obtains the exclusive maintenance guard, the existing dispatcher
lock directly, and `BEGIN EXCLUSIVE` with DELETE journaling/EXTRA synchronous.
It never calls `dispatcher()` or changes owner/clock metadata as a side effect.
API readers, API writers and standby clients cannot enter while maintenance is
exclusive. Native locks are process-bound; a fork child must not unlock its
parent's shared lock. Windows uses LockFileEx; POSIX uses flock.

**Required external gate:** pre-B3 clients do not participate. A reviewed host
implementation of `LegacyQuiescenceGuard.verify()` must prove all such readers/
writers stopped, handles closed and restart inhibited throughout the session.
No production host adapter is shipped or presumed by B3.1. A missing guard or a
boolean is rejected. SQLite's exclusive transaction detects active readers/
writers, but cannot prove absence of an idle legacy handle. The test-only
SyntheticQuiescence guard covers subprocesses the fixture exclusively controls;
it is NOT VPS-wide process/ACL evidence and must never be used operationally.
The independent VPS legacy-quiescence adapter/restart barrier remains a gate.

## Atomicity and receipt

`MigrationRequest.v1_to_v2(migration_id, installation_id)` binds the UUIDs and
exact compiled source/target schema digests. The caller supplies an already-held
MaintenanceSession. The migration reads only an existing verified ledger.

Before DDL it validates installation/application/version, exact schema,
quick_check and foreign keys. A streaming typed/canonical manifest covers every
row in billing_scopes, quota_scopes, credential_bindings, binding_quotas,
documents, runs, attempts, attempt_quota_reservations and operations. Separate
digests cover all meta columns except schema_version and the original financial
table/index/trigger SQL. No original private row is put in a receipt or log.

One transaction replaces only meta's version CHECK while copying every other
column unchanged, replaces schema_contract, creates the B3 additions, seeds the
nonfinancial scheduler counter, inserts the receipt and sets user_version=2.
It uses individual execute() calls; executescript() is forbidden here because
of its implicit commit. Manifests and exact target digest must match before
commit. No opening balance, stop, revision, epoch, nonce, boot/high-water/lease
history, existing attempt state or operation row is reset. Original immutable
financial triggers retain their exact SQL.

`MigrationReceipt` version 1 contains:

- migration_id and installation_id;
- source_schema_digest and target_schema_digest;
- financial_digest, metadata_digest and immutable_schema_digest;
- quiescence_digest identifying the trusted host proof.

The append-only `schema_migrations` row binds request_digest, canonical
receipt_json and receipt_digest. `MigrationResult` separately reports
newly_applied and commit_receipt_recovered; these flags do not modify a receipt.

Before-commit failure/kill leaves v1 through normal SQLite transaction recovery.
On commit error the migrator retains BOTH OS guards, closes the original
connection, opens only an exact registered source/target and examines the
receipt/history. An unchanged source reports migration_not_committed; a valid
target with the exact receipt reports recovered success. No DDL is retried in
that error handler. Unknown state, lost host proof or mismatched history denies
the operation. A repeat ID returns its original receipt; a different ID on an
already-migrated DB is rejected. Replay after subsequent legitimate v2 expense
does not compare against or restore the historical financial balance.

A real original reader from the base SHA is exercised in a subprocess and must
reject migrated v2. Old prevalidated idle handles are excluded by maintenance
quiescence, not by this later version check. A successful migration has no
downgrade/financial-backup restore operation.

## Offline evidence and remaining gates

`tests/test_ai_first_coordination_migrations.py` and its worker use actual
SQLite connections/native locks. The worker pauses during DDL, before real
commit and after real commit for subprocess kill; a connection subclass also
commits and then raises to lose its receipt. Actual readers/writers, idle
participating clients, competing maintenance processes, an un-stopped legacy
process, bad ACL, corrupted schema/DB and immutable-row constraints are covered.

Migrated v2 tests execute B1 reserve, idempotency, outcome, quota/window,
unknown liability and fencing. They also execute the actual B2 client/permit/
injected transport boundary with 2/4/10 fake HTTP calls and kill after evidence
fsync before settlement, then recover finance with P1.2 and no additional HTTP.

Full baseline/final JUnit, failure-ID comparison, exported SQL, hashes and a
review ZIP are review artifacts under ignored `tmp/phase-b3`. All databases,
credentials, PDFs and responses used by tests are synthetic and private.

Linux fork/flock, Linux VPS ACL/fsync/filesystem behavior and the host-specific
legacy process/restart-inhibition proof must be reported separately. A Windows
native lock test is not evidence for those gates. B0 account/path proof, real
provider quotas, B3 scheduler/API/execution inputs and B4-B6 remain outside this
step. Live concurrency remains 1. No commit is created by this task.
