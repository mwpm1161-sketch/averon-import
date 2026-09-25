from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
APP_JS = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "averon_import" / "templates" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "averon_import" / "static" / "styles.css").read_text(encoding="utf-8")


def _function(name: str, next_name: str) -> str:
    start_match = re.search(rf"(?m)^(?:async )?function {re.escape(name)}\(", APP_JS)
    assert start_match
    marker_patterns = (
        rf"(?m)^(?:async )?function {re.escape(next_name)}\(",
        rf"(?m)^const {re.escape(next_name)}\b",
    )
    endings = [
        match.start()
        for pattern in marker_patterns
        if (match := re.search(pattern, APP_JS[start_match.end() :]))
    ]
    assert endings
    return APP_JS[start_match.start() : start_match.end() + min(endings)]


def test_logout_is_in_sidebar_utility_and_keeps_session_auth_semantics():
    top_actions = re.search(r'<div class="top-actions">(.*?)</div>', HTML, re.S)
    utility = re.search(r'<div class="sidebar-utility">(.*?)</div>', HTML, re.S)
    sidebar = re.search(r'<aside class="sidebar">(.*?)</aside>', HTML, re.S)
    assert top_actions and utility and sidebar
    assert 'id="logout-button"' not in top_actions.group(1)
    assert 'id="logout-button"' in utility.group(1)
    assert sidebar.group(1).index('class="sidebar-card"') < sidebar.group(1).index('class="sidebar-utility"')
    assert sidebar.group(1).index('class="sidebar-utility"') < sidebar.group(1).index('class="sidebar-footer"')

    logout = re.search(r'<button[^>]+id="logout-button"[^>]*>', HTML)
    assert logout
    assert 'type="button"' in logout.group()
    assert 'hidden' in logout.group()
    assert 'title="Выйти"' in logout.group()
    assert 'aria-label="Выйти"' in logout.group()
    assert "<svg" in utility.group(1)
    assert ".logout-utility-button:focus-visible" in CSS
    assert "prefers-reduced-motion: reduce" in CSS

    boot = APP_JS.split("async function boot()", 1)[1].split("function updateCloudStatus", 1)[0]
    events = _function("setupEvents", "setZoom")
    logout_flow = _function("logoutSession", "accountErrorMessage")
    assert '$("#logout-button").hidden = authMode !== "session"' in boot
    assert '$("#logout-button").addEventListener("click", logoutSession)' in events
    assert 'state.authMode !== "session"' in logout_flow
    assert 'api("/api/auth/logout", {method:"POST"})' in logout_flow


def test_support_actions_are_distinct_from_admin_reports_and_keep_event_modes():
    contextual = re.search(
        r'<button[^>]+id="contextual-support-report-button"[^>]*>(.*?)</button>', HTML, re.S
    )
    admin = re.search(r'<button[^>]+id="admin-reports-button"[^>]*>', HTML)
    export_support = re.search(
        r'<button[^>]+id="open-support-report"[^>]*>(.*?)</button>', HTML, re.S
    )
    assert contextual and admin and export_support
    assert 'class="button support-action"' in contextual.group(0)
    assert "Оставить обращение" in contextual.group(1)
    assert 'class="button support-action"' not in admin.group()
    assert 'class="button support-action"' in export_support.group(0)
    assert ".button.support-action" in CSS
    assert ".button.support-action:focus-visible" in CSS
    assert 'openSupportReportModal("user_reported")' in APP_JS
    assert 'openSupportReportModal("export_failure")' in APP_JS


def test_page_inspection_is_on_demand_and_does_not_change_page_selection():
    thumbnails = _function("renderThumbnails", "togglePage")
    preview = _function("showCropPreview", "resetPageInspection")
    opener = _function("openPageInspection", "closePageInspection")
    boot = APP_JS.split("async function boot()", 1)[1].split("function updateCloudStatus", 1)[0]
    high_detail_calls = re.findall(r"documentPageImageUrl\([^\n]*,\s*200\)", APP_JS)

    assert "documentPageImageUrl(state.document.document_id, page, 72)" in thumbnails
    assert "documentPageImageUrl(state.document.document_id, page, 120)" in preview
    assert len(high_detail_calls) == 1
    assert "documentPageImageUrl(documentId, page, 200)" in opener
    assert 'const documentId = state.document?.document_id;' in opener
    assert "Number(state.previewPage)" in opener
    assert "!documentId || !Number.isInteger(page) || page < 1" in opener
    assert "selectedPages" not in opener and "togglePage(" not in opener
    assert "200" not in thumbnails + preview + boot
    assert '<dialog class="page-inspection-modal" id="page-inspection-modal"' in HTML
    assert 'id="page-inspection-open" type="button" disabled aria-label="Развернуть страницу для просмотра"' in HTML
    assert 'id="page-inspection-image" alt="Страница"' in HTML
    assert 'alt = `Страница ${page}`' in opener
    assert 'dialog.showModal();' in opener
    assert "image.onerror" in opener and "Не удалось загрузить страницу." in opener


def test_inspection_close_and_document_navigation_reset_only_viewer_state():
    reset = _function("resetPageInspection", "renderPageInspectionImage")
    cancel_navigation = _function("cancelDocumentNavigation", "beginDocumentNavigation")
    events = _function("setupEvents", "setZoom")
    close_handler = '$("#page-inspection-modal").addEventListener("close",() => resetPageInspection({closeDialog:false}));'

    assert 'image.removeAttribute("src")' in reset
    assert 'state.pageInspection = {documentId:null,page:null,zoom:1,fitWidth:true,requestId}' in reset
    assert "previewPage" not in reset and "selectedPages" not in reset
    assert "resetPageInspection();" in cancel_navigation
    assert close_handler in events
    assert '$("#page-inspection-close").addEventListener("click",closePageInspection)' in events
    assert '<dialog class="page-inspection-modal"' in HTML
    close = _function("closePageInspection", "positionCropBox")
    assert "if (dialog.open) dialog.close();" in close
    assert "beginDocumentNavigation()" in _function("openExistingDocument", "uploadFile")
    assert "beginDocumentNavigation()" in _function("uploadFile", "renderThumbnails")
    assert "cancelDocumentNavigation();" in _function("openManualWorkspace", "returnToStartFromManual")
    assert "cancelDocumentNavigation();" in _function("resetApp", "setupEvents")
    assert "cancelDocumentNavigation();" in _function("clearProtectedMemory", "clearProtectedUi")


def test_inspection_zoom_changes_scrollable_layout_and_has_no_background_work():
    zoom = _function("setPageInspectionZoom", "fitPageInspectionToWidth")
    render = _function("renderPageInspectionImage", "setPageInspectionZoom")
    fit = _function("fitPageInspectionToWidth", "syncPageInspectionCropOverlay")
    opener = _function("openPageInspection", "closePageInspection")
    css = re.search(r"\.page-inspection-viewport\s*\{([^}]+)\}", CSS)
    assert css and "overflow:auto" in css.group(1)
    assert "PAGE_INSPECTION_MIN_ZOOM = 0.2" in APP_JS
    assert "PAGE_INSPECTION_MAX_ZOOM = 2.4" in APP_JS
    assert "PAGE_INSPECTION_ZOOM_STEP = 0.2" in APP_JS
    assert "PAGE_INSPECTION_MIN_ZOOM" in zoom and "PAGE_INSPECTION_MAX_ZOOM" in zoom
    assert "image.naturalWidth * state.pageInspection.zoom" in render
    assert "image.naturalHeight * state.pageInspection.zoom" in render
    assert 'image.style.width = `${width}px`' in render
    assert 'image.style.height = `${height}px`' in render
    assert 'canvas.style.width = `${width}px`' in render
    assert 'canvas.style.height = `${height}px`' in render
    assert "image.naturalWidth" in fit and "availableWidth / image.naturalWidth" in fit
    assert "setPageInspectionZoom(fitZoom, {fitWidth:true})" in fit
    assert "transform" not in zoom + render

    viewer_code = opener + zoom + render + fit + _function("syncPageInspectionCropOverlay", "openPageInspection")
    assert not re.search(r"setTimeout|setInterval|while\s*\(", viewer_code)
    events = _function("setupEvents", "setZoom")
    viewer_events = events.split('$("#page-inspection-open")', 1)[1].split("setupCropEvents();", 1)[0]
    assert not re.search(r"setTimeout|setInterval|while\s*\(", viewer_events)
    assert 'image.src = documentPageImageUrl(documentId, page, 200);' in opener
    assert "prefetch" not in viewer_code.lower()
