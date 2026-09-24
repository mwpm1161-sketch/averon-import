from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
APP_JS = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "averon_import" / "templates" / "index.html").read_text(encoding="utf-8")


def _function(name: str, next_name: str) -> str:
    start_match = re.search(rf"(?m)^(?:async )?function {re.escape(name)}\(", APP_JS)
    assert start_match
    marker_patterns = (
        rf"(?m)^(?:async )?function {re.escape(next_name)}\(",
        rf"(?m)^const {re.escape(next_name)}\b",
    )
    endings = [match.start() for pattern in marker_patterns if (match := re.search(pattern, APP_JS[start_match.end() :]))]
    assert endings
    return APP_JS[start_match.start() : start_match.end() + min(endings)]


def test_api_error_keeps_legacy_message_and_structured_export_metadata():
    assert "class ApiError extends Error" in APP_JS
    assert "super(message || \"Ошибка запроса\")" in APP_JS
    assert "this.status = status" in APP_JS
    assert "this.code = code" in APP_JS
    assert "this.incident_id = incidentId" in APP_JS
    assert "this.reportable = Boolean(reportable)" in APP_JS
    assert "this.payload = payload" in APP_JS
    assert "incidentId: typeof error.incident_id === \"string\" ? error.incident_id : null" in APP_JS


def test_user_and_admin_report_entry_are_capability_gated_and_boot_is_lazy():
    boot = APP_JS.split("async function boot()", 1)[1].split("function updateCloudStatus", 1)[0]
    assert '"#settings-button").hidden = !isAdmin' in boot
    assert 'if (currentUser?.capabilities?.admin_reports === true)' in boot
    assert '"#admin-reports-button"' in boot
    assert "/api/support/reports" not in boot
    assert "/api/admin/support/reports" not in boot
    assert '<button class="button ghost" id="admin-reports-button" hidden>' in HTML
    assert 'if (state.currentUser?.capabilities?.admin_reports !== true) return;' in _function(
        "openAdminSupportReports", "renderAdminSupportReports"
    )


def test_both_export_flows_share_reportable_error_handling_and_clear_old_state():
    production = _function("downloadExcel", "reviewExportFilename")
    review = _function("downloadReviewExcel", "resetApp")
    shared = _function("exportExcelFile", "openSupportReportModal")
    failure = _function("showExportFailure", "exportExcelFile")
    assert "clearExportError();" in production
    assert "clearExportError();" in review
    assert 'exportExcelFile(payload, "Excel сформирован")' in production
    assert 'exportExcelFile(payload, "Проверочный Excel сформирован")' in review
    assert "showExportFailure(error)" in shared
    assert "error.reportable && incidentId" in failure
    assert "state.support.pendingIncident = {incidentId}" in failure
    assert "export-reportable-error" in HTML
    assert "Сообщить о проблеме" in HTML
    assert '"#export-modal").close()' in shared


def test_report_form_uses_incident_from_state_and_has_bounded_required_fields():
    fio = re.search(r'<input[^>]+id="support-reporter-fio"[^>]*>', HTML)
    description = re.search(r'<textarea[^>]+id="support-report-description"[^>]*>', HTML)
    assert fio and 'required' in fio.group() and 'minlength="3"' in fio.group() and 'maxlength="200"' in fio.group()
    assert description and 'required' in description.group() and 'minlength="20"' in description.group() and 'maxlength="5000"' in description.group()
    assert "Опишите, что вы ожидали получить и что произошло при экспорте." in HTML
    submit = _function("submitSupportReport", "SUPPORT_STATUS_LABELS")
    assert 'api("/api/support/reports"' in submit
    assert "incident_id:incidentId" in submit
    assert "state.support.pendingIncident?.incidentId" in submit
    assert "SUPPORT_REPORT_EXISTS" in submit
    assert "support.reporter" not in APP_JS
    assert not re.search(r'localStorage\.(?:setItem|getItem)\([^\n]*(?:reporter_fio|description|incident_id)', APP_JS)
    assert 'status === 422' in submit
    assert "Pydantic" not in submit


def test_admin_list_is_opened_on_demand_and_uses_bounded_pagination():
    opener = _function("openAdminSupportReports", "renderAdminSupportReports")
    loader = _function("loadAdminSupportReports", "loadAdminSupportReport")
    assert "loadAdminSupportReports();" in opener
    assert '`/api/admin/support/reports?' in loader
    assert 'limit:String(admin.limit)' in loader and 'offset:String(admin.offset)' in loader
    assert "admin.limit" in _function("renderAdminSupportReports", "loadAdminSupportReports")
    assert all(label in HTML for label in ("Открытые", "В работе", "Решённые", "Все", "Назад", "Далее"))
    assert '"/api/admin/support/reports?' not in APP_JS.split("async function boot()", 1)[1].split("function updateCloudStatus", 1)[0]


def test_admin_detail_snapshot_status_and_document_actions_are_lazy_and_versioned():
    detail = _function("loadAdminSupportReport", "renderAdminSupportReportDetail")
    renderer = _function("renderAdminSupportReportDetail", "loadAdminSupportSnapshot")
    snapshot = _function("loadAdminSupportSnapshot", "renderAdminSupportSnapshot")
    update = _function("updateAdminSupportStatus", "downloadExcel")
    open_document = _function("openReportDocument", "updateAdminSupportStatus")
    assert '`/api/admin/support/reports/${encodeURIComponent(reportId)}`' in detail
    assert "snapshot.addEventListener(\"toggle\"" in renderer
    assert "snapshot.open && !state.support.admin.snapshotRequested" in renderer
    assert '`/api/admin/support/reports/${encodeURIComponent(reportId)}/snapshot`' in snapshot
    assert "SNAPSHOT_UNAVAILABLE" in snapshot and "Снимок состояния недоступен." in snapshot
    assert 'method:"PATCH"' in update and "version:report.version" in update
    assert "STALE_REPORT_VERSION" in update
    assert "Обращение уже было изменено. Данные обновлены." in update
    assert "loadAdminSupportReport(report.report_id), loadAdminSupportReports()" in update
    dirty_confirmation = 'if (state.dirty && !confirm("Несохранённые правки будут потеряны. Открыть документ обращения?")) return;'
    assert dirty_confirmation in open_document
    assert "state.document?.document_id !== documentId" not in open_document
    assert open_document.index(dirty_confirmation) < open_document.index("await openExistingDocument(documentId)")
    assert "openExistingDocument(documentId)" in open_document
    assert '"#admin-reports-modal").close()' in open_document


def test_user_controlled_report_and_snapshot_values_use_text_nodes():
    field_renderer = _function("addSupportField", "openAdminSupportReports")
    list_renderer = _function("renderAdminSupportReports", "loadAdminSupportReports")
    detail_renderer = _function("renderAdminSupportReportDetail", "loadAdminSupportSnapshot")
    snapshot_renderer = _function("renderAdminSupportSnapshot", "openReportDocument")
    assert "content.textContent = supportValue(value)" in field_renderer
    assert "name.textContent = supportValue(report.reporter_fio)" in list_renderer
    assert "error.textContent = supportValue(report.public_message)" in list_renderer
    assert "description.textContent = typeof report.description === \"string\" ? report.description : supportValue(report.description)" in detail_renderer
    assert "cell.textContent = supportValue(value)" in snapshot_renderer
    assert ".innerHTML" not in field_renderer + list_renderer + detail_renderer + snapshot_renderer


def test_support_dialog_ids_exist_have_close_actions_and_no_support_polling():
    html_ids = set(re.findall(r'\bid="([^"]+)"', HTML))
    required_ids = {
        "support-report-modal", "support-report-form", "support-reporter-fio", "support-report-description",
        "close-support-report", "cancel-support-report", "support-report-submit", "admin-reports-modal",
        "admin-reports-button", "close-admin-reports", "admin-report-status-filter", "admin-report-list",
        "admin-report-detail", "admin-report-previous", "admin-report-next", "admin-report-page",
        "export-reportable-error", "open-support-report", "contextual-support-report-button",
    }
    assert required_ids <= html_ids
    events = _function("setupEvents", "setZoom")
    for selector in ("admin-reports-button", "support-report-form", "close-support-report", "cancel-support-report", "close-admin-reports", "admin-report-status-filter", "admin-report-previous", "admin-report-next"):
        assert f'const {"reportsButton" if selector == "admin-reports-button" else "supportReportForm" if selector == "support-report-form" else "closeSupportReport" if selector == "close-support-report" else "cancelSupportReport" if selector == "cancel-support-report" else "closeAdminReports" if selector == "close-admin-reports" else "statusFilter" if selector == "admin-report-status-filter" else "previousReports" if selector == "admin-report-previous" else "nextReports"} = $("#{selector}")' in events
    assert 'if (reportsButton) reportsButton.addEventListener' in events
    assert 'if (supportReportForm) supportReportForm.addEventListener' in events
    assert '"#support-report-modal").close()' in events
    assert '"#admin-reports-modal").close()' in events
    support_code = APP_JS[APP_JS.index("function clearExportError("):APP_JS.index("async function downloadExcel(")]
    assert "setInterval" not in support_code
    assert "setTimeout" not in support_code
