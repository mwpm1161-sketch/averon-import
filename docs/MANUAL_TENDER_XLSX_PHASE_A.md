# Excel tender workspace — Phase A

Phase A provides the official Averon tender workbook, bounded XLSX preview and
mapping, and an owner-bound read-only workspace for imported rows. It does not
start sourcing and does not create a price or total export.

The XLSX parser runs on the existing `DOCUMENT_PROCESSING` job lane. The lane
is process-local and currently permits one running job plus two queued jobs.
Jobs are not persisted: after a process restart, queued or analyzing previews
are marked interrupted and require an explicit reupload. Confirmed workspaces
and their original source file are persisted separately under
`DATA_DIR/manual_tenders/workspaces`.

Preview workspaces expire after 20 minutes. Confirmed workspaces expire after
24 hours idle or 72 hours absolute, whichever comes first. Initial limits are
three confirmed workspaces per user and twenty globally; each upload is at
most 5 MiB, aggregate XLSX contents are at most 20 MiB, and parser analysis
metadata is capped at 10 MiB per workspace. A source plus old/new atomic
metadata files bounds each storage root to 500 MiB at the twenty-workspace
global limit; previews and confirmed workspaces use separate roots. Cleanup is
lazy and never removes a workspace held by an in-process activity lease.

Leases and job coordination are process-local, matching the current single
application-process deployment. A future multi-process deployment must use a
shared lease/quota mechanism before running these operations across workers.

The parser does not evaluate formulas. Quantity is trusted only when read from
a positive finite numeric cell or a conservatively parsed numeric string.
Resource references remain separate from article evidence. Unit parsing records
a tender-specific basis and does not infer package or set ratios. The uploaded
source workbook is copied into the confirmed workspace and is never modified
in Phase A.

The generated official template carries the defined-name schema marker
`AveronTenderSchemaVersion=1`. Its table must start at A1, have the exact seven
Phase A headers, and contain no more than 500 data rows. A recognizable
official workbook with a missing or unsupported marker is rejected instead of
being treated as a generic workbook. The official template accepts only item
rows or invalid rows; the generic fallback parser may classify a pure
name-only row as a section. Invalid required item facts are shown by physical
Excel row and safe reason codes in the preview, and confirmation returns
`TENDER_INVALID_ROWS` until the source workbook is corrected and uploaded again.

The workbook manifest includes a SHA-256 fingerprint of normalized cell
formatting properties (number format, font, fill, border, alignment, and
protection). The fingerprint describes source formatting without depending on
workbook-local style IDs. Preview polling follows job state, stops on terminal
or interrupted previews, and checks the current UI generation before each
request; interrupted parsing is never replayed automatically. Repository
metadata uses same-directory unique temporary files and serialized
read/modify/write operations for workspace touches.

`TenderTemplateService` generates the workbook and caches its immutable bytes
in process memory. No operator-managed XLSX file or server-side template folder
is required.
