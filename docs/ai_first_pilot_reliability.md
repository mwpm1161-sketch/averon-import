# AI-first pilot: draft, run and region safety

Scope: isolated changes based on `bf20477c8ad3f08f9aa53baf678778e0563379ef`.
No production rollout or paid OCR call is part of this change.

## Confirmed causes and evidence

- `saveAIFirstReview` used to reject browser edits. The save endpoint received no
  buffer and only rewrote already persisted files. The former guard has been
  replaced by saving actual editor input, without creating confirmations.
- A document always used `ai_first/checkpoint` and a fixed run identity.
  Changing DPI, context, model parameters or output limits correctly invalidated
  that checkpoint, but there was no explicit path to a separate run.
- Polling and review/preview callbacks used the global current document after
  asynchronous waits. A late response could therefore affect a different view.
  Document/auth generations now guard every publication, and navigation clears
  document-bound state and rendered inputs. Navigation saves outstanding editor
  input first; a failed save stops navigation.
- The AI-first request did not contain the selected crop. Rendering always used
  the entire PDF page. A validated page-specific region now reaches the renderer.
- Selected-page pilot XLSX already uses selected-page guards. Synthetic API tests
  verify page 10 of 20 with the other 19 pages pending. Such coverage does not
  require whole-document completion. The UI now states what to select, opens the
  successful page, and keeps only concise export counts and test/scope marking.

The production incident for an unrelated empty workspace has not been traced on
the server. The cross-document race is reproduced offline; it is not asserted to
be the proven cause of every historical production error.

## Saved editor input and decisions

`editor-draft.json` is a separate version-1 private atomic file. Typed input stores
row values, provisional roles, unresolved issue notes and an unfinished manual
row. Each save retains a private history snapshot and uses an optimistic token
bound to the extraction revision, review ledger and editor revision. Conflicting
writes fail without replacing either server state or local input. The UI reloads
current server state, retains local input, and requires a new explicit save.
Typing while a request is in flight remains unsaved until a subsequent save.

Saving this file does not alter `review.json`, raw transcription or review
confirmations. On reopen the buffer is restored over the corresponding source
cells. Existing explicit value/role/issue/add-row actions consume only their own
saved buffer; value and role edits cannot erase each other.

Preview and XLSX use a temporary, unconfirmed view of buffered values. A buffered
row cannot enter `Спецификация`; original values and buffered corrections remain
available in the source/audit sheets. Editor audit snapshots are filtered to selected pages; private buffers and history from other pages stay in protected storage. An unfinished manual row removes its page's
completeness only from this temporary export view. It never creates a real row or
silently changes the review ledger. A stale editor revision blocks export.

The standard review schema and existing checkpoints are not migrated. Legacy
clients may still save without an editor payload; new clients submit the current
review token for mutations. Protected file/DACL checks are unchanged.

## Runs and crop

The default resumes the active compatible run. A mismatch is detected locally
before job submission or HTTP, with an explicit `Создать новый запуск` action.
There is no automatic retry or fallback. A new run uses its own private
`runs/run-<random-id>/checkpoint`, and snapshots the prior transcription, ledger,
report and buffered edits. Original checkpoint directories are never removed or
rewritten. `active-run.json` records the current run; changing settings never
silently adopts a different checkpoint.

Explicit new runs and cropped extractions bind the page extraction revision to
their trusted run/configuration context, even when model text is identical.
Reconciliation invalidates old confirmations and completeness. Saved buffer
history remains recoverable, but is not applied to the new extraction.

Crop is normalized to the displayed PDF page, tied to its document and page, and
converted to PDF points. It is applied through PyMuPDF `clip` before pixmap
allocation. Projected pixel limits, resulting bytes, Qwen HTTP size checks, input
reservation and output caps remain enforced. Coordinates and scope are retained
in the source envelope and checkpoint processing identity. Other selected pages
retain their full-page scope. Full-page requests retain the old processing
fingerprint when no region is supplied.

The UI confirms completeness of the extracted region, not the entire sheet. The
pilot workbook marks the partial area and stores normalized/PDF coordinates on
an audit sheet. The full-document export guard rejects region-only results.
Model bbox candidates remain unverified and are not used for false highlighting.

## Session ownership and compatibility

Session uploads record a server-owned uploader identifier that is hidden from public metadata. Another user cannot claim that upload by starting AI-first first. New AI-first workspaces bind `access.json` to that owner (or the authenticated starter for legacy uploads without uploader metadata).
Other users receive 404 for their state, mutations, preview/XLSX, metadata and PDF
page routes; their recent-document list omits the workspace. Administrators retain
access. Existing AI-first workspaces without `access.json` use server-authored
reviewer history for access. An old workspace with no trustworthy reviewer
history requires administrator inspection/ownership assignment before ordinary
session users can reopen it. No ownership is inferred from client input.
Legacy shared documents that have no AI-first directory retain their prior policy.

## Update and rollback

1. Review this branch and complete the manual browser smoke. No deployment is
   included here; the automatic browser stopped on its URL-policy check.
2. Before any approved rollout, take a verified consistent backup of the database
   and document directories including `ai_first`, review files, all run snapshots,
   checkpoints and configuration; record the previous deployed SHA.
3. Inventory legacy AI-first workspaces lacking ownership evidence in session mode.
   Resolve access through an administrator before pilot users return to them.
4. Deploy the reviewed SHA with the existing production authentication and one
   worker. Do not change the Qwen prompt, schema, credentials, tariffs, concurrency
   or paid-request budget as part of this patch.
5. Verify save/reopen and page-scoped XLSX with synthetic data, then manually check
   an authorized saved result. No new cloud recognition is needed for this check.
6. Roll back code and the matched private workspace backup together. Old code
   ignores editor/access/run files and does not understand crop-only completeness;
   it must not be allowed to export a new cropped or buffered workspace. Disable
   the AI-first flag until the matched backup is restored. Preserve new run/editor
   files for recovery; do not delete checkpoints to make an old binary accept them.

SQL migrations: none. Legacy OCR, its crop processing, commercial exporter,
Excel-tender and sourcing implementations are unchanged. Concurrency is still 1;
process-local provider quota limitations remain. Restoring a previous run snapshot
is currently an administrator/offline operation, not a new history-selection UI.

## Validation for this patch

- AI-first/Qwen offline profile: 368 passed, 0 failed, 0 errors, 0 skipped.
- Dedicated new API/storage/crop regressions: 17 passed, including rotated pages
  at 0/90/180/270 degrees, invalid/stale buffers, explicit run changes and two real
  session users. All external OCR transport is blocked by the fixtures.
- The combined AI-first/Qwen and account/auth profile passed 457 tests.
- All 17 JavaScript regression scripts passed. They cover browser editor buffers,
  in-flight typing, failed saves, stale document/job/preview responses, row/role
  actions, partial-page selection, and the existing legacy UI scripts.
- Actual loopback HTTP smoke used synthetic page 10 of a 20-page PDF. It saved
  and reopened buffers, produced an unconfirmed and a one-position checked XLSX,
  compared all 12 raw cells in order, checked corrected/empty/zero values, sheet
  structure and zero formulas. A fresh server process preserved the review,
  editor buffer, corrections, confirmations and completeness. PDF PNG returned 200.
- The wider legacy/auth/export/tender/ETM/VI profile returned exactly the same
  781 passed, 2 failed, 1 skipped on the base tree and this branch in the same
  environment. Both failures are in existing manual-tender download tests:
  `test_d4c_manual_search_snapshot_fence_no_match_confirm_restart_revoke_and_export`
  and `test_tender_price_export_job_persists_owner_bound_verified_artifact`.
  Their ASGI helper immediately returns `http.disconnect`: with Starlette 1.10.0
  the FileResponse body is empty under the helper's default ASGI 2.0 scope.
  A standalone reproduction returns 0 bytes under that scope and the complete
  valid workbook under ASGI 2.4; actual loopback HTTP downloads are intact.
  No legacy product code or test assertions were changed to conceal these failures.
  Earlier long temporary paths additionally hit Windows path-length limits;
  equal short, extended temporary paths remove those failures on both trees.
- Automated Edge smoke was stopped by the Computer Use URL-policy check before
  page interaction. No alternate browser automation was used to bypass it.
  No visual/click/scroll verification of this patch is claimed.

All fixtures and workbook samples are synthetic. Test environments and runtime
artifacts live outside the repository. No customer PDF, transcription, credentials
or technical report is included in this patch. No SQL migration, paid request,
production update, merge or deploy was executed.
