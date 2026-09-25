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

New uploads hash the incoming bytes in the existing 1 MiB streaming loop and store the server-computed fingerprint in metadata. Legacy workspaces compute and atomically cache it once. The fingerprint is omitted from public metadata. A current ledger revision is read from a small atomic revision marker; a matching `GET /results` does one result read, zero decision-ledger parses, zero replays, zero source-PDF hashes, and zero result writes. A ledger/result mismatch loads and replays the ledger once, updates the canonical revision, and later GETs return read-only. If a crash leaves the revision marker ahead of the ledger replacement, the ledger is reread and the marker repaired before deciding whether canonical recovery is needed.

Automatic restore starts only after `bootComplete`, uses an abortable sequential metadata/result fetch, and commits document state only after both responses pass the navigation generation check. Upload, explicit open, manual mode, reset, and session cleanup invalidate it. The existing page image cache remains keyed by workspace/page/DPI; cache writes now replace the final PNG atomically so concurrent first renders cannot expose a partial file.

Focused Phase 2 validation: **49 passed** across performance-foundation, workspace/history, review-workflow, and page-range tests; compileall, JavaScript syntax, and whitespace checks passed.

## Phase 3 results

The normal human-decision path now applies the just-validated decision to a fresh canonical result while holding its document lock. It detaches only the target row (and the continuation parent when relevant), leaves the original `ocr_metadata` object untouched, recalculates only affected page safety, updates summary counters by the changed-row delta, and writes the canonical result once. Ledger reconciliation still replays saved decisions only when the persisted result revision is stale. The endpoint returns changed rows, affected page status, summary, and revisions rather than a full result.

The browser serializes review mutations and fences each response against document identity and navigation generation. It merges only the returned row/page patch, updates readiness, replaces only affected table rows, and retains preview, zoom, active row, and table scroll position. A 500-row synthetic result regression test measured a one-row response below one tenth of the complete result JSON; the same operation performed one result read, one result write, zero source fingerprint calculations, zero saved-decision replays, zero full-result copies, one detached row, and one page-safety recalculation. A continuation decision test measured two detached rows (parent and child) and one affected page; unrelated page status was unchanged. The canonical `result.json` remains a complete atomic JSON replacement because that is the existing persistence format.

Focused Phase 3 validation: **49 passed** across performance-foundation, workspace/history, and review-workflow tests; compileall, JavaScript syntax, and whitespace checks passed.

## Phase 4 results

Backend summaries now expose `review_rows` and `ready_rows` using the same canonical row blocker policy as export validation. A row with status `edited` still counts as review-required when it retains a critical blocker. The browser uses these counts and each saved row's canonical `critical_blockers`; page blockers remain independent, so a zero row-review count can still show the exact page-level reason that blocks production export. Unsaved edits are labeled provisional and the UI defers the authoritative readiness claim until save. Normal export still goes through the existing strict backend page and row validation.

An ordinary active row now uses a neutral blue highlight while review/blocker rows retain the warning palette. Clean production export skips the unconditional row PUT and result GET. When the user has edits, one PUT returns the canonical saved result, which the client uses directly before export; the browser keeps the current preview, zoom, active row, and table scroll during that save.

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
| Search typing | Full table render for each input event | Full table render after a 120 ms debounce; no network polling |

The review-decision counter test records one result read and write, zero cached-fingerprint calculations, zero replayed decisions, zero full-result copies, one detached row, and one page recalculation. The output-size comparison uses serialized bytes from a 500-row synthetic fixture rather than wall-clock timing, which is machine-dependent.
