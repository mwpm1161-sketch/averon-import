# Averon Import B3.2 durable job storage

B3.2 adds an inert durable job store on the already accepted B3.1 SQLite schema v2. It does not alter the schema, app startup, API routes, scheduler, OCR factory, HTTP transport, legacy `JobCoordinator`, or publication path. Schema v2 grants only the `durable_jobs_v1` capability; B6 reconciliation remains denied.

## Submission and execution-input state

```text
absent -- T0 SQLite transaction --> preparing_input / execution_state=pending
preparing_input -- private stage + file fsync + no-overwrite install --> preparing_input
preparing_input -- T1 SQLite transaction --> queued / execution_state=ready
preparing_input -- exact cancellation --> cancelled
preparing_input or queued -- missing/corrupt input --> blocked
```

T0 atomically stores the original B1 `Run` ID and body, the canonical `OrchestrationConfig` with Decimal pricing, the immutable credential-binding metadata, configuration/pricing digests, the snapshot digest, job/page rows, and the stable `submit_begin` receipt. The database is the only authority for job identity and financial history. The stored run ID is the original B1 run ID, not a generated replacement.

After T0 commits, B3.2 writes a versioned canonical execution specification under an explicitly provisioned private document-storage root. The deterministic artifact identity is pinned by `execution_id`, byte length, and SHA-256 in SQLite; the filesystem name is derived from the job ID and execution ID. The file stores source document identity/hash/stat, selected pages and crops, OCR resource settings, server-selected provider/model/schema identifiers, and prompts. It contains no API key, authorization header, image, or serialized HTTP body. JSON parsing rejects duplicate keys and unknown fields. File and directory permissions, reparse points, canonical locations, exact size, canonical encoding, and hashes are checked before use; POSIX file reads use `O_NOFOLLOW` where supported.

T1 is committed only after the final file is verified. On POSIX, installation uses same-directory no-overwrite hard-linking, file `fsync`, and directory `fsync`; recovery accepts only the exact pinned bytes and removes a safe duplicate `.pending` name when both names remain after a crash. A partial pending file is blocked. If the final file is absent or fails validation after T1, recovery marks the job blocked; it never reconstructs a prompt or substitutes active settings.

If the process loses a T0/T1 commit receipt, it reads the immutable command and receipt back from SQLite. T0 recovery is explicit through `recover_preparing`: it validates the original financial snapshot, current authorized generation/source identity, and exact staged hash before finishing. No recovery path creates a paid attempt or dispatches HTTP.

## Commands, isolation, and status

Submission idempotency is scoped by `(tenant_id, owner_id, operation_id)` and the canonical request digest. Exact replay returns the recorded receipt before consulting current settings; a changed payload for the same operation ID is rejected. Global and per-owner active job/page counts, one-active-job-per-document, snapshots, job/page rows, and the T0 receipt are checked and committed in one `BEGIN IMMEDIATE` transaction. Active-job scans and status pages are explicitly capped.

`cancel` and `resume` are durable optimistic-revision commands. B3.2 does not claim that cancellation interrupts a future HTTP request. Resume accepts only a restart-paused job with a valid execution specification and no unknown page outcome; it does not mint a permit, attempt, or retry. Status reads use one bounded SQLite read transaction and return only page state, revisions, byte counts, safe error codes, and `publication_state="not_published"`. Prompts, OCR text, credential metadata, financial snapshots, and private paths are excluded.

Default storage queue ceilings are 256 active jobs / 20,000 active pages globally and 32 active jobs / 2,000 active pages per owner. A single job is limited to 1,000 selected pages; public status pagination is limited to 100 pages. These are storage admission ceilings, not provider concurrency settings. Production concurrency remains 1.

## Deferred phases and release gates

B3.3 still owns dispatcher/scheduler recovery, fairness, cancel/send fencing, and the atomic B1/B2 send gate. B3.4 owns publication and stale-generation protections for OCR drafts/Human Review. B3.5 owns API authentication, tenant/document authorization wiring, and UI polling/progress. This B3.2 code is not composed into the live runtime.

Windows `MoveFileExW(MOVEFILE_WRITE_THROUGH)` is used for no-overwrite installation, but B3.2 does not claim the POSIX directory-fsync power-loss guarantee on Windows. The real Linux/VPS ACL, filesystem, directory-fsync, reboot, and crash-recovery gates remain open. No real provider request or production ledger migration is part of B3.2.
