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
