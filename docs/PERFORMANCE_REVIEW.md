# Performance and correctness review

## Baseline

Base: `pilot-v1.2` / `8f78704cad37d77eda3727606d4fdb7c92e07bc8`.

Observed on the base before implementation:

| Flow | Work on the base |
| --- | --- |
| Boot and restore | `/api/me`, parallel `/api/config` and `/api/health`, optional `/api/settings`, then `/api/documents`; boot awaited automatic document restore and could not complete until the previous result loaded. |
| Recent documents | Each workspace's `metadata.json` and `result.json` were parsed to populate the list. |
| Open document | `GET /api/documents/{id}` parsed the full `result.json` just to compute `has_result`; opening then fetched and parsed it again from `/results`. |
| `GET /results` | Read the result and decision ledger, hashed the complete source PDF, replayed every saved decision, recalculated all page safety, deep-copied the result twice through review/recalculation, then rewrote the complete JSON on every GET. |
| One review decision | Read the full result; hashed the PDF; loaded and rewrote the decision ledger; replayed all saved decisions; searched rows repeatedly; copied the result for application and again for safety; recalculated every page; wrote the complete result; returned it in full. |
| Save and production export | The browser always PUT all rows, then GET the result, then POST export. The server reread and rewrote the result during save and GET, then read it again for export. |
| Preview and table | Page PNGs were cached by workspace/page/DPI. A cache miss rendered synchronously to the final path. `renderRows()` replaced the complete table and rebound per-cell/row listeners; a select change triggered that full render. A review response called `loadResult()`, resetting preview and selection. |
| Concurrent mutations | JSON replacement was atomic, but request-level read/modify/write was not serialized. Concurrent decisions or a long recognition job could overwrite a newer result. The job executor had one global worker and retained every completed job in memory. |
| Safety/readiness | Backend export validation used row and page policy. The browser had a smaller hand-maintained blocker implementation; review count was status-only, so edited rows could be blocking without increasing that count. |

The PDF source is copied once into each workspace and is not changed by normal app operations. At baseline, its SHA-256 was recomputed independently by result, save, review, and recognition flows. The server's review service made detached copies to preserve frozen/shared OCR DTOs; a review decision then triggered another full result copy in page safety recalculation.

The process-local job queue serves OCR/PDF work, page suggestions, sourcing, and ETM work. Worker-count changes are deferred until provider thread-safety, external rate limits, and concurrent PDF memory use are established.

Baseline validation: `python -m pytest -q` — **1043 passed, 3 failed, 2 skipped**. The only failures were the three known Windows tests requiring the intentionally absent Tesseract executable.

## Concurrency boundary

Canonical JSON files use atomic replacement. Per-document mutation locks serialize request threads in the current single-process deployment and do not block unrelated documents. These locks do not coordinate multiple server processes. A multi-process deployment requires a shared transactional store or OS/distributed locking before it can claim lost-update protection.

## Phase 2 results

The recent-list and document-open regressions are covered with counters and large-result fixtures. Listing and `GET /api/documents/{id}` now perform **zero result payload reads**; `has_result` comes from a file stat. A corrupt result remains listed as present and returns a clear 409 message on explicit open.

New uploads hash the incoming bytes in the existing 1 MiB streaming loop and store the server-computed fingerprint in metadata. Legacy workspaces compute and atomically cache it once. The fingerprint is omitted from public metadata. A current canonical `GET /results` trusts the persisted revision marker: when it matches the result projection, the GET does one result read, zero decision-ledger parses, zero replays, zero source-PDF hashes, and zero result writes. Therefore a matching GET does not detect a malformed ledger. Ledger corruption is fail-closed with a safe 409 whenever audit integrity is required: export (production and review), review mutation/read, save, or mismatch reconciliation. Mismatch recovery validates and replays the persisted ledger before writing; malformed JSON, invalid shape, or an invalid decision leaves the result, ledger, and marker untouched. If a crash leaves the revision marker ahead of a valid older ledger replacement, the ledger is reread and the marker repaired before deciding whether canonical recovery is needed. A missing ledger remains an empty legacy snapshot.

Automatic restore starts only after `bootComplete`, uses an abortable sequential metadata/result fetch, and commits document state only after both responses pass the navigation generation check. Upload, explicit open, manual mode, reset, and session cleanup invalidate it. The existing page image cache remains keyed by workspace/page/DPI; cache writes now replace the final PNG atomically so concurrent first renders cannot expose a partial file.

Focused Phase 2 validation: **49 passed** across performance-foundation, workspace/history, review-workflow, and page-range tests; compileall, JavaScript syntax, and whitespace checks passed.

## Phase 3 results

The normal human-decision path now applies the just-validated decision to a fresh canonical result while holding its document lock. It detaches only the target row (and the continuation parent when relevant), leaves the original `ocr_metadata` object untouched, recalculates only affected page safety, updates summary counters by the changed-row delta, and writes the canonical result once. Ledger reconciliation still replays saved decisions only when the persisted result revision is stale. The endpoint returns changed rows, affected page status, summary, and revisions rather than a full result.

The browser serializes review mutations and fences each response against document identity and navigation generation. It merges only the returned row/page patch, updates readiness, replaces only affected table rows, and retains preview, zoom, active row, and both scroll coordinates from the actual `.table-scroll` container. A 500-row synthetic result regression test measured a one-row response below one tenth of the complete result JSON; the same operation performed one result read, one result write, zero source fingerprint calculations, zero saved-decision replays, zero full-result copies, one detached row, and one page-safety recalculation. A continuation decision test measured two detached rows (parent and child) and one affected page; unrelated page status was unchanged. The canonical `result.json` remains a complete atomic JSON replacement because that is the existing persistence format.

Focused Phase 3 validation: **49 passed** across performance-foundation, workspace/history, and review-workflow tests; compileall, JavaScript syntax, and whitespace checks passed.

## Phase 4 results

Backend summaries now expose `review_rows` and `ready_rows` using the same canonical row blocker policy as export validation. A row with status `edited` still counts as review-required when it retains a critical blocker. The browser uses these counts and each saved row's canonical `critical_blockers`; page blockers remain independent, so a zero row-review count can still show the exact page-level reason that blocks production export. Unsaved edits are labeled provisional and the UI defers the authoritative readiness claim until save. Normal export still goes through the existing strict backend page and row validation.

An ordinary active row now uses a neutral blue highlight while review/blocker rows retain the warning palette. Clean production export skips the unconditional row PUT and result GET. When the user has edits, one PUT returns the canonical saved result, which the client uses directly before export; the browser keeps the current preview, zoom, active row, and table scroll during that save.

Production export now includes the current expected result revision. The client waits for its queued Human Review decisions, saves dirty edits once, and uses the authoritative revision returned by that save; a clean export sends the open result's revision without a save/read round trip. Under the per-document lock, both production and review exports validate the persisted review ledger, reconcile the projection against that validated ledger revision, and check the expected document revision before taking the export snapshot. Malformed ledger JSON, shape, or decisions return a safe 409 before workbook generation; a revision mismatch also returns 409. Production XLSX rows always come from the canonical saved result, so an older browser cannot export stale rows after another client saves. `WorkspaceService.read_json()` provides a fresh local result dictionary, and `ExcelExportService.export()` reads rows/page status without mutating them, so the snapshot retains those references without full-result or duplicate row copies. The document lock is released before OpenPyXL builds the workbook. Review export also carries and checks the expected revision; its documented policy remains an inspection snapshot of the submitted browser rows, including unsaved edits, and is rejected if the canonical document revision changed.

The browser keeps server `critical_blockers` separate from transient provisional review calculations. Local edits can add provisional blockers for the safety preview but cannot overwrite canonical structural blockers; only a saved backend result replaces the canonical set. Save requests omit transient preview fields, and production export still evaluates canonical rows on the backend. Human Review error toasts are also suppressed after document navigation changes, while the shared API layer continues to process 401 session expiry.

Focused Phase 4 validation: **71 passed** across product UI, row assembly, performance foundation, export, and critical-verification tests; compileall, JavaScript syntax, and whitespace checks passed.

## Phase 5 audit and decisions

The result table now attaches click, input, change, and focus handlers once to its stable `<tbody>`. A select edit patches one row instead of rebuilding the table; ordinary text input updates the row model and warning classes in place. Search renders are debounced by 120 ms. Textarea heights are measured in a batch, and transient maps index rows by ID, page, and physical references. Those maps are rebuilt when a canonical result is opened and updated on row patches; they are never serialized. The existing page PNG cache remains keyed by document/page/DPI and uses atomic replacement from Phase 2.

The job executor remains at one worker. OCR and PDF rendering have material per-job memory use; sourcing/ETM calls may have provider rate limits and shared clients. `JobService` retains completed payloads and tracebacks in its process-local dictionary without a bound. This pass does not change concurrency or retention because the repository does not establish safe provider concurrency or the maximum result size. A safe next design is bounded admission by workload class, keeping OCR/PDF concurrency capped by measured peak RSS, imposing explicit queue limits, and expiring terminal job records only after a retention window longer than the client polling window; completed large payloads should be released only after a client acknowledgement or that expiry. Provider concurrency and memory measurements are prerequisites.

## Structural before/after measurements

| Operation | Base `pilot-v1.2` | Feature branch, measured or asserted structurally |
| --- | --- | --- |
| Recent-document discovery | Parsed every workspace result payload | Zero result payload reads; metadata/stat only |
| Current `GET /results` | Result + full ledger parse/replay, PDF hash, two full-result copies, all-page safety pass, result rewrite | One result read; zero ledger parse/replay, PDF hash, deep copy, safety recalculation, or write |
| One field decision on 500 synthetic rows | Full result response, all saved decisions replayed, two full-result copies, all pages recalculated, full table replacement | One result read/write, zero PDF hashes/replays/full copies, one detached row/page recalculated; patch JSON is under 10% of full result JSON; one affected DOM row and no `loadResult()` |
| One continuation decision | Same full replay/copy/all-page/full-response path as other decisions | Two detached rows (parent and child), one affected page under the current same-page relation contract |
| Clean production export | PUT rows + GET result + export POST | Export POST only; clean save exits before any API request |
| Dirty production export | PUT rows + GET result + export POST | One PUT returning canonical result + export POST; no follow-up result GET |
| Concurrent clean export | Client rows could be exported after another client changed the saved result | Exact `expected_revision` check under the document lock; stale production and review exports return 409 before workbook creation |
| Export ledger integrity and snapshot | Export did not validate a matching marker against the persisted ledger; copied the full result and export rows/statuses | Both export types parse/validate the persisted ledger under the lock, reconcile its revision, then pass fresh local row/status references after releasing the lock; malformed history returns 409 before workbook creation |
| Search typing | Full table render for each input event | Full table render after a 120 ms debounce; no network polling |

The review-decision counter test records one result read and write, zero cached-fingerprint calculations, zero replayed decisions, zero full-result copies, one detached row, and one page recalculation. The output-size comparison uses serialized bytes from a 500-row synthetic fixture rather than wall-clock timing, which is machine-dependent.

Follow-up validation: **142 passed** across product UI, performance, review/history, support export, workbook, critical-safety, and row-assembly tests; release-tooling tests **4 passed**. Full `pytest -q`: **1079 passed, 3 failed, 2 skipped**. The only failures are `test_local_profile_resolves_tesseract_adapter`, `test_settings_processing_mode_influences_resolver`, and `test_local_mode_rejects_cloud_llm_combination`, which require the intentionally absent system Tesseract executable. `python -m compileall averon_import`, `node --check averon_import/static/app.js`, and `git diff --check` passed.
