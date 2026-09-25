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
