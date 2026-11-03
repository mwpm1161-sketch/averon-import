# AI-first: atomic role and value review, stage 2

Base: `85c13cd4000a07c93d30410923147a677bbb73be`. Isolated experiment;
no production activation, OCR change, page-region change or export permission.

## One operation endpoint

`POST /api/documents/{document_id}/ai-first/review/bulk-role-decision`
uses the existing authentication, document ownership/activity and experimental
feature-flag dependencies. `mode` is `preview` or `commit`. Preview performs the
same preparation and validation but does not publish new decisions or consume
editor buffers. A store read can finish a previously committed redo journal.

Required request fields:

| Field | Contract |
|---|---|
| `operation_id` | 32 lowercase hexadecimal characters, newly generated for one explicit operation |
| `review_token` | Current 64-character hexadecimal token, mandatory |
| `draft_revision_id` | Exact current extraction revision |
| `editor_revision` | Current saved editor-buffer revision, including zero |
| `record_ids` | 1–100 distinct IDs; only visible selected rows enter the UI request |
| `role` | `standalone_item`, `component`, `header`, `column_numbering`, `section`, `continuation`, `note` |
| `intent` | `confirm_values` for item/component; `exclude_from_positions` for service roles |
| `provenance` | Per-row page number, extraction fingerprint and source table ID (null for a manual row) |
| `values` | Full explicit per-row column snapshot, including empty values |
| `parents` | Per-row parent ID for components only |

DTOs reject extra fields, coercion, duplicate IDs, missing row snapshots and
incompatible intent. IDs are bounded to 256 characters; each row has at most 64
fields, keys at most 64 characters, values at most 2,000 characters and at most
30,000 characters in total. The whole value snapshot is limited to 1,000,000
characters. No text is silently truncated or normalized.

Preview returns `can_commit`, row count, role, intent, changed-cell count,
invalidated decision/role IDs and safe errors. It grants no export eligibility.
Commit rechecks the token, editor revision and every row under the same lock.
Success returns `saved`, `replayed`, `operation_id`, current `review_token` and
the same summary. Validation failures use 422 or safe 409 errors; no rows are
written. Existing complete-page and issue-resolution actions remain explicit.

## Transaction and persistent idempotency

`_mutate_ai_first_state()` holds one existing document mutation lock from read
through publication. All selected rows and parents are checked first. The
existing `record_row_role_classification`, `record_row_decision` and
`assert_no_open_review_conflicts` operate on an in-memory copy. The resulting
review state is validated again before writing.

Each row retains ordinary typed role events and decision events. The final
event's extensible `payload.bulk_operation` contains a versioned receipt:
operation ID, canonical request SHA-256, authenticated reviewer ID and safe
summary. The receipt and all row changes therefore share one `review.json`.
It does not duplicate document text. Old DTOs already preserve extensible event
payloads; existing review files and checkpoints need no migration.

The digest includes the complete request except `mode`, and the server-derived
reviewer identity. A replay is checked before the old token is rejected: same
ID, actor and payload returns `replayed=true`, without events or editor changes.
Same ID with another payload/actor is rejected. Receipts remain in audit history
and are not evicted. A historical replay never reapplies decisions invalidated
by later extraction or structural changes; the UI loads the current state.

`consume_editor()` consumes values and roles only for selected IDs, retaining
other buffers and editor history. `write_many()` publishes the ledger and editor
through its existing private redo journal. The journal is atomically installed
before targets change. An I/O failure or process exit after that point has an
unknown HTTP outcome; the next store read completes the whole recorded bundle
before returning data. Before that point, the old state remains. An interrupted
publication is rolled forward, not represented as a failed validation rollback.

These guarantees use the supported **one application process / one worker**
topology and application reads through `AIWorkspaceStore`. Locks remain
process-local. This stage does not claim coordination between workers, or new
durability guarantees for power loss/filesystem corruption. The existing
64 MiB transaction ceiling remains fail-closed. Receipt lookup is linear in
existing review history, like the current review-token calculation.

## Safety and UI behavior

Current page/revision, superseded rows, column provenance, open document/page/
table/row/cell issues and current manual rows are checked. A component requires
an active, human-reviewed standalone parent with confirmed values, compatible
current provenance and no unresolved evidence. Parents cannot belong to the
same uniform component batch. Existing role-change invalidations are included
in preview and audit. No model role becomes human-verified automatically.

Standalone/component values are explicitly confirmed. Service roles record
exclusion without deleting the observation; supplied corrections remain in the
ledger. Page completeness is never added by this action. Export guards and
sourcing are unchanged. Ambiguous/manual column snapshots are rejected rather
than guessed; a manual row must have a snapshot matching its saved value keys.

The existing bulk role control gains the combined action and, for components,
a parent selector. Before commit a compact confirmation shows count, role,
intent, changed cells and dependent decisions to review. Any changed selection,
values or token while preview is loading invalidates that preview. Only one
mutating request is sent; role and value writes are never separate HTTP calls.

Stage-1 `reviewSyncRequired` still blocks mutations after an unknown outcome.
The client keeps the operation ID and selected-buffer snapshot in memory, does
not repeat POST, and offers GET recovery. The receipt in returned history proves
whether the batch was applied. Only unchanged local buffers submitted by that
batch are consumed after such recovery; newer/unrelated edits remain. Existing
scroll/focus/caret restoration and selection reconciliation are reused.

## Review hardening

Saved editor corrections must match the corresponding submitted cell values,
even when both the review token and editor revision are current. A mismatch
returns `bulk_editor_values_mismatch` (409 on commit, a blocking error in preview)
before any decision or editor-buffer publication. Replacement requires an
explicit save through the existing editor flow and a new current snapshot;
editor history is retained. Matching corrections are consumed only for the
selected records. Replaying an old receipt cannot consume newer or unrelated
editor buffers.

An active manual row on a current `no_specification` page is a valid review
object, matching the existing add-row contract. Model table rows still require
`extracted`; manual rows on `failed` or `incomplete` pages remain blocked. The
batch does not mark any page complete.

Dependency preview lists each affected source page, table and row (or manual
row), its current human-reviewed role and whether the role, value decision or
both will be invalidated. A missing source mapping blocks the operation instead
of silently omitting that dependency. No preview API or receipt-format change
is needed. Legacy bulk controls and the common review-POST path are blocked
while preview is loading.

A successful batch POST plus state GET now performs exactly one full review
render. The selection is synchronized separately, and the existing view snapshot
restores scroll, focus and caret after controls unlock. Unrelated pending edits
are retained; unknown-outcome and failed-refresh recovery remain unchanged.

Measured offline execution of a valid 100-row, nine-column packet on this
Windows environment (fixture construction excluded):

| Prior history events | Preview | Commit | Journal bundle publications |
|---|---:|---:|---:|
| 0 | 1,208.601 ms | 1,341.663 ms | 1 |
| 1,000 | 9,560.962 ms | 8,717.893 ms | 1 |

Preview publishes nothing; replay adds no publication. These are local
measurements, not a latency guarantee. The accumulated-history case takes about
18.3 seconds for preview plus commit and remains a pilot limitation. No review
ledger or persistence optimization was introduced in this narrow patch.

## Offline verification

`tests/test_ai_first_atomic_bulk_review.py` covers preview/no writes, item and
service intents, component/parent guards, strict bounds, stale/unknown/superseded
rows, issue scopes, editor revisions, idempotency/actor binding, late validation
failure, publication failure, hard process exit and replay in a new process,
concurrent ASGI tabs, feature flag and session ownership. Hardening adds saved
correction conflicts through the real ASGI route, matching-buffer consumption,
newer-buffer preservation on replay, manual rows on `no_specification`, failed
and incomplete page rejection, and valid 100-row packets with 1,000 prior events.
Existing foundation,
workflow, orchestration and pilot-export profiles check the surrounding guards.

`tests/js/ai_first_atomic_bulk_review.cjs` checks the actual UI handler with fake
API responses: visible-only snapshots, one mutating request and full render,
concrete dependency labels, preview cancellation and errors, in-flight edits,
parent/intent, unknown outcome, unrelated edits, post-unlock view restoration
and legacy mutation blocking during preview.
Native browser regressions are separate from a full local app smoke; results
and environment limitations are reported with the review handoff.

Validation on 2026-11-03: the full `tests/test_ai_first_*.py` profile finished
with **237 passed, 0 failed, 0 errors, 0 skipped** in 123.70 seconds. All **11 AI-first JS scripts**
passed, including the native Edge regression at 1280 and 1000 pixels. Use a
short unique directory under the system temporary directory on Windows: four
existing run-archive tests also fail on the unchanged stage-1 base when the
test root is nested under the long visualization path. No ACL changes were
needed; the short isolated root allowed the entire profile to pass.

The earlier real local ASGI/browser smoke used a separate synthetic two-page workspace,
local development auth and a forbidden cloud transport. It exercised two-row
item confirmation, a corrected service-row exclusion, a component assignment,
filtered selection and preview cancellation, then restarted the app and reopened
the same state before this hardening. The three committed operations left exactly
three receipts, one standalone item, one component and one excluded row. All
three OCR rows and six raw cells remained; page completeness stayed unconfirmed.
PDF rendering returned HTTP 200. An unrelated pending edit survived the first
batch. The temporary server was stopped after inspection.
