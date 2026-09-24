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


def test_contextual_action_is_available_to_authenticated_users_with_a_document_only():
    visibility = _function("updateContextualSupportButton", "setView")
    set_view = _function("setView", "bytes")
    opener = _function("openSupportReportModal", "clearSupportReportState")
    events = _function("setupEvents", "setZoom")
    assert 'id="contextual-support-report-button" type="button" hidden>Сообщить о проблеме' in HTML
    assert 'state.authState !== "authenticated" || !state.document' in visibility
    assert 'activeView?.id === "manual-view"' in visibility
    assert "capabilities" not in visibility and "admin_reports" not in opener
    assert "updateContextualSupportButton();" in set_view
    assert 'openSupportReportModal("user_reported")' in events
    assert 'mode === "user_reported" && !state.document?.document_id' in opener


def test_boot_opening_a_document_and_opening_the_manual_form_make_no_support_requests():
    boot = APP_JS.split("async function boot()", 1)[1].split("function updateCloudStatus", 1)[0]
    open_document = _function("openExistingDocument", "uploadFile")
    opener = _function("openSupportReportModal", "clearSupportReportState")
    assert "/api/support/incidents" not in boot
    assert "/api/support/reports" not in boot
    assert "/api/support/incidents" not in open_document
    assert "/api/support/reports" not in open_document
    assert "api(" not in opener
    assert "modal.showModal();" in opener


def test_manual_submission_validates_then_creates_one_minimal_incident_and_reuses_it_on_retry():
    submit = _function("submitSupportReport", "SUPPORT_STATUS_LABELS")
    assert submit.count('api("/api/support/incidents"') == 1
    assert submit.count('api("/api/support/reports"') == 1
    assert submit.index("form.reportValidity()") < submit.index('api("/api/support/incidents"')
    assert submit.index('api("/api/support/incidents"') < submit.index('api("/api/support/reports"')
    assert "state.support.sending) return" in submit
    assert "state.support.pendingIncident?.incidentId" in submit
    assert "state.support.contextualPendingIncident?.incidentId" in submit
    assert 'mode === "user_reported" && !state.support.contextualPendingIncident?.incidentId' in submit
    assert "JSON.stringify({document_id:documentId, stage:currentSupportStage()})" in submit
    assert "JSON.stringify({incident_id:incidentId, reporter_fio:reporterFio, description})" in submit
    assert "incident.incident_id" in submit
    assert "rows" not in submit and "snapshot" not in submit and "localStorage" not in submit and "sessionStorage" not in submit
    assert 'error.code === "SUPPORT_REPORT_EXISTS"' in submit


def test_stage_mapping_is_stable_and_falls_back_to_document():
    stage = _function("currentSupportStage", "SUPPORT_INCIDENT_KIND_LABELS")
    assert 'if ($("#export-modal")?.open) return "export"' in stage
    for view, value in (
        ('"upload-view":"document"', "document"),
        ('"pages-view":"pages"', "pages"),
        ('"processing-view":"recognition"', "recognition"),
        ('"review-view":"review"', "review"),
    ):
        assert view in stage and value in stage
    assert 'return stageByView[activeView?.id] || "document";' in stage


def test_export_and_contextual_incident_state_are_isolated_and_auth_cleanup_clears_both():
    opener = _function("openSupportReportModal", "clearSupportReportState")
    clear = _function("clearSupportReportState", "submitSupportReport")
    protected_memory = _function("clearProtectedMemory", "clearProtectedUi")
    events = _function("setupEvents", "setZoom")
    submit = _function("submitSupportReport", "SUPPORT_STATUS_LABELS")
    export_failure = _function("showExportFailure", "exportExcelFile")
    assert "state.support.pendingIncident = {incidentId}" in export_failure
    assert 'const panel = $("#export-reportable-error");' in export_failure
    assert "if (panel) panel.hidden = false;" in export_failure
    assert 'mode === "user_reported"' in opener
    assert "pendingIncident = null" not in opener
    assert "pendingIncident =" not in opener
    assert "export-reportable-error" not in opener
    assert 'state.support.contextualPendingIncident = null' in clear
    assert 'if (previousMode === "user_reported") state.support.contextualPendingIncident = null;' in clear
    assert "state.support.pendingIncident" not in clear
    assert '$("#export-reportable-error")' not in clear
    assert "state.support.reportMode = null" in clear
    assert "state.support.sending = false" in clear
    assert 'supportReportModal.addEventListener("close",clearSupportReportState)' in events
    assert ".close(" not in clear
    assert "state.support.pendingIncident = null" in protected_memory
    assert "state.support.contextualPendingIncident = null" in protected_memory
    assert "state.support.reportMode = null" in protected_memory
    assert "state.support.sending = false" in protected_memory
    assert 'mode === "export_failure" && !state.support.pendingIncident?.incidentId' in submit
    assert not re.search(r"(?:localStorage|sessionStorage)\.(?:getItem|setItem)\([^\n]*pendingIncident", APP_JS)


def test_report_success_cleans_only_the_incident_for_its_mode():
    submit = _function("submitSupportReport", "SUPPORT_STATUS_LABELS")
    export_clear = _function("clearExportError", "showExportFailure")
    success = submit.split('await api("/api/support/reports",', 1)[1].split("} catch (error)", 1)[0]
    duplicate = submit.split('error.code === "SUPPORT_REPORT_EXISTS"', 1)[1].split("} else if", 1)[0]
    for branch in (success, duplicate):
        assert 'if (mode === "export_failure") clearExportError();' in branch
        assert 'else state.support.contextualPendingIncident = null;' in branch
    assert "state.support.pendingIncident = null" in export_clear
    assert 'const panel = $("#export-reportable-error");' in export_clear
    assert "if (panel) panel.hidden = true;" in export_clear


def test_manual_report_failure_retains_contextual_incident_and_retry_reuses_it():
    submit = _function("submitSupportReport", "SUPPORT_STATUS_LABELS")
    incident_guard = 'if (mode === "user_reported" && !state.support.contextualPendingIncident?.incidentId) {'
    incident_start = submit.index(incident_guard)
    report_post = submit.index('await api("/api/support/reports",')
    manual_create = submit[incident_start:report_post]
    assert submit.count('api("/api/support/incidents"') == 1
    assert submit.count('api("/api/support/reports"') == 1
    assert 'state.support.contextualPendingIncident = {incidentId:incident.incident_id};' in manual_create
    assert incident_start + manual_create.index('api("/api/support/incidents"') < report_post
    assert 'const incidentId = mode === "user_reported"\n      ? state.support.contextualPendingIncident?.incidentId\n      : state.support.pendingIncident?.incidentId;' in submit
    failed_report_branch = submit.split('message.textContent = "Не удалось отправить обращение. Повторите попытку.";', 1)[1].split("\n    }\n  } finally", 1)[0]
    assert "contextualPendingIncident = null" not in failed_report_branch
    assert 'mode === "user_reported" && !state.support.contextualPendingIncident?.incidentId' in submit
    assert submit.index(incident_guard) < submit.index('api("/api/support/incidents"') < report_post


def test_admin_incident_kinds_and_stages_are_localized_and_manual_nulls_are_omitted():
    labels = _function("supportIncidentKindLabel", "supportValue")
    listing = _function("renderAdminSupportReports", "loadAdminSupportReports")
    detail = _function("renderAdminSupportReportDetail", "loadAdminSupportSnapshot")
    snapshot = _function("renderAdminSupportSnapshot", "openReportDocument")
    assert 'export_failure:"Ошибка экспорта"' in APP_JS
    assert 'user_reported:"Сообщение пользователя"' in APP_JS
    assert 'supportIncidentKindLabel(kind)' in listing
    assert "supportValue(report.error_code)" in listing
    assert 'if (kind === "export_failure" && report.public_message)' in listing
    assert 'if (incident.incident_kind === "export_failure")' in detail
    assert 'addSupportField(fields, "Код ошибки", incident.error_code)' in detail
    assert 'addSupportField(fields, "Тип обращения", supportIncidentKindLabel(incident.incident_kind))' in detail
    assert 'addSupportField(fields, "Этап", supportStageLabel(incident.stage))' in detail
    assert 'document:"Документ"' in APP_JS
    assert 'pages:"Страницы"' in APP_JS
    assert 'recognition:"Распознавание"' in APP_JS
    assert 'review:"Проверка"' in APP_JS
    assert 'export:"Экспорт"' in APP_JS
    assert 'incident.incident_kind === "user_reported"' in detail
    assert '"Состояние на момент отправки обращения"' in detail
    assert "incident_kind" not in labels


def test_contextual_and_admin_values_are_rendered_as_text():
    field_renderer = _function("addSupportField", "openAdminSupportReports")
    listing = _function("renderAdminSupportReports", "loadAdminSupportReports")
    detail = _function("renderAdminSupportReportDetail", "loadAdminSupportSnapshot")
    snapshot = _function("renderAdminSupportSnapshot", "openReportDocument")
    assert "content.textContent = supportValue(value)" in field_renderer
    assert "name.textContent = supportValue(report.reporter_fio)" in listing
    assert "error.textContent = supportValue(report.public_message)" in listing
    assert "description.textContent = typeof report.description === \"string\" ? report.description : supportValue(report.description)" in detail
    assert "cell.textContent = supportValue(value)" in snapshot
    assert ".innerHTML" not in field_renderer + listing + detail + snapshot
