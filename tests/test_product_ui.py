import hashlib
from pathlib import Path
import re


ROOT = Path(__file__).parents[1]


def test_main_ui_is_yandex_vision_only():
    html = (ROOT / "averon_import" / "templates" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")

    assert "Yandex Vision OCR" in html
    assert 'id="yandex-ocr-status"' in html
    assert 'id="settings-modal"' in html
    assert 'name="ocr-mode"' not in html
    assert 'name="ai-provider"' not in html
    assert "Tesseract" not in html
    assert "Ollama" not in html
    assert 'processing_mode:"cloud"' in app_js
    assert 'ai_provider:"off"' in app_js
    assert 'ocr_mode:"standard"' in app_js
    assert 'row.row_type === "note" && row.structured_table' in app_js


def test_yandex_only_dom_covers_app_selectors_and_needs_no_legacy_controls():
    html = (ROOT / "averon_import" / "templates" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")

    html_ids = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
    static_selectors = set(re.findall(r'\$\("(#[A-Za-z0-9_-]+)"\)', app_js))
    generated_selectors = {"#select-all-rows"}
    missing = {
        selector for selector in static_selectors - generated_selectors
        if selector[1:] not in html_ids
    }

    assert not missing
    assert "initializeOcrMode" not in app_js
    assert "initializeAiProvider" not in app_js
    assert "updateAiProviderStatus" not in app_js
    assert 'input[name="ocr-mode"]' not in app_js
    assert 'input[name="ai-provider"]' not in app_js
    assert "local-ai-card" not in app_js
    assert "yandex-ai-card" not in app_js
    assert "accurate-mode-card" not in app_js


def test_critical_review_ui_is_present_and_export_safety_is_explicit():
    html = (ROOT / "averon_import" / "templates" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "averon_import" / "static" / "styles.css").read_text(encoding="utf-8")

    assert 'id="review-filter"' in html
    for label in (
        "Требуют проверки",
        "Не распознано количество",
        "Не распознана единица",
        "Подозрительные числа",
        "Проверено",
    ):
        assert label in html
    assert 'id="summary-critical"' in html
    assert 'id="export-safety"' in html
    assert "critical_value_missing" in app_js
    assert "numeric_suspect" in app_js
    assert "Yandex повторно распознал" in app_js
    assert "candidate-accept" in app_js
    assert "critical-review" in css


def test_review_export_is_explicit_and_not_gated_by_production_blockers():
    html = (ROOT / "averon_import" / "templates" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="download-review-excel"' in html
    assert "Экспорт для проверки" in html
    assert "Проверочный файл может содержать строки, требующие проверки." in html
    assert "function downloadReviewExcel()" in app_js
    review_export = app_js.split("async function downloadReviewExcel()", 1)[1].split("\nfunction resetApp()", 1)[0]
    production_export = app_js.split("async function downloadExcel()", 1)[1].split("\nfunction reviewExportFilename()", 1)[0]
    assert "review_export:true" in review_export
    assert "only_exportable:false" in review_export
    assert "rows:state.rows" in review_export
    assert "saveRows(false)" not in review_export
    assert "loadResult(" not in review_export
    assert "state.dirty = false" not in review_export
    assert "saveRows(false)" in production_export
    assert "backendExportBlockers()" in production_export
    assert "review_export:false" in production_export
    assert "function reviewExportFilename()" in app_js


def test_canonical_review_counts_and_export_safety_remain_consistent():
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "averon_import" / "static" / "styles.css").read_text(encoding="utf-8")

    summary = app_js.split("function updateSummary()", 1)[1].split("function backendExportBlockers()", 1)[0]
    blockers = app_js.split("function backendExportBlockers()", 1)[1].split("function updateExportSafety()", 1)[0]
    safety = app_js.split("function updateExportSafety()", 1)[1].split("function markDirty()", 1)[0]
    load_result = app_js.split("function loadResult(", 1)[1].split("const displayColumns", 1)[0]

    assert "serverSummary.review_rows" in summary
    assert "serverSummary.ready_rows" in summary
    assert "criticalBlockers(row).length > 0" in summary
    assert "status.blockers" in blockers
    assert "criticalBlockers(row).length > 0" in blockers
    assert "state.dirty" in safety
    assert "Backend проверит безопасность после сохранения" in safety
    assert "row.critical_blockers" in app_js
    assert "state.rows.forEach(refreshClientReview)" not in load_result
    assert ".data-table tr.active td { background:#eef3fa; }" in css


def test_clean_export_skips_save_round_trip_and_dirty_save_uses_authoritative_response():
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
    save = app_js.split("async function saveRows(", 1)[1].split("function copyRows(", 1)[0]

    assert "if (!state.dirty) return state.result;" in save
    assert save.count("api(") == 1
    assert "await api(`/api/documents/${state.document.document_id}/results`)" not in save
    assert "response?.result" in save
    assert "preserveView:true" in save
    assert "await waitForReviewMutations(documentId)" in save
    assert "state.reviewMutationQueues.get(documentId)" in app_js
    assert "expected_revision:Number(authoritative.revision || 0)" in app_js


def test_local_review_keeps_canonical_structural_blockers_and_serializes_no_preview_state():
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
    load_result = app_js.split("function loadResult(", 1)[1].split("const displayColumns", 1)[0]
    critical = app_js.split("function criticalBlockers(", 1)[1].split("function criticalFieldCount(", 1)[0]
    refresh = app_js.split("function refreshClientReview(", 1)[1].split("function resultTableScrollPosition", 1)[0]
    save = app_js.split("async function saveRows(", 1)[1].split("function copyRows(", 1)[0]

    assert "canonical_critical_blockers: Array.isArray(row.critical_blockers)" in load_result
    assert "canonicalBlockers" in critical
    assert "canonicalBlockers, ...blockers" in critical
    assert "row.provisional_critical_blockers = criticalBlockers(preview)" in refresh
    assert "row.critical_blockers =" not in refresh
    assert "row.review_reasons =" not in refresh
    assert "provisional_critical_blockers" in save
    assert "provisional_review_reasons" in save
    assert "expected_revision:Number(state.result?.revision || 0)" in save


def test_result_table_uses_delegated_events_row_indexes_and_debounced_search():
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
    renderer = app_js.split("function renderRows()", 1)[1].split("function ensureResultTableEvents()", 1)[0]
    events = app_js.split("function ensureResultTableEvents()", 1)[1].split("function renderPatchedReviewRows(", 1)[0]
    row_lookup = app_js.split("function rowById(", 1)[1].split("async function selectRow(", 1)[0]
    continuation_lookup = app_js.split("function continuationParent(", 1)[1].split("function continuationFragment(", 1)[0]
    search_setup = app_js.split('$("#table-search").addEventListener("input",', 1)[1].split('$("#type-filter")', 1)[0]

    assert "ensureResultTableEvents();" in renderer
    assert "body.querySelectorAll(\"tr\").forEach" not in renderer
    assert 'body.addEventListener("click"' in events
    assert 'body.addEventListener("change"' in events
    assert 'body.addEventListener("input"' in events
    assert 'body.addEventListener("focusin"' in events
    assert "renderPatchedReviewRows([row])" in events
    assert "renderRows()" not in events
    assert "state.rowIndexes.byId.get(String(id))" in row_lookup
    assert "state.rowIndexes.byPageRefs" in continuation_lookup
    assert "}, 120);" in search_setup


def test_review_export_button_has_busy_and_finally_recovery_semantics():
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
    review_export = app_js.split("async function downloadReviewExcel()", 1)[1].split("\nfunction resetApp()", 1)[0]

    assert "if (button.disabled) return;" in review_export
    assert "button.disabled = true;" in review_export
    assert 'button.textContent = "Формируем Excel…";' in review_export
    assert 'toast("Сначала откройте документ", "error")' in review_export
    assert 'toast("Нет строк для экспорта", "error")' in review_export
    assert "finally" in review_export
    assert "button.disabled = false;" in review_export
    assert 'button.textContent = "Экспорт для проверки";' in review_export


def test_static_assets_use_deterministic_content_revisions_and_modal_has_no_blur():
    app_path = ROOT / "averon_import" / "static" / "app.js"
    styles_path = ROOT / "averon_import" / "static" / "styles.css"
    html = (ROOT / "averon_import" / "templates" / "index.html").read_text(encoding="utf-8")
    css = styles_path.read_text(encoding="utf-8")

    app_revision = hashlib.sha256(app_path.read_bytes()).hexdigest()[:12]
    styles_revision = hashlib.sha256(styles_path.read_bytes()).hexdigest()[:12]
    assert f"/static/app.js?v={{{{ asset_revisions.app }}}}" in html
    assert f"/static/styles.css?v={{{{ asset_revisions.styles }}}}" in html
    assert len(app_revision) == 12
    assert len(styles_revision) == 12
    modal_backdrop = re.search(r"\.modal::backdrop\s*\{([^}]*)\}", css)
    assert modal_backdrop
    assert "backdrop-filter" not in modal_backdrop.group(1)


def test_export_safety_uses_backend_page_status_and_saved_result_preserves_pdf_viewer():
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "averon_import" / "static" / "styles.css").read_text(encoding="utf-8")

    assert "function backendExportBlockers()" in app_js
    assert "page_statuses" in app_js
    assert "output_status" in app_js
    assert "Backend подтвердил: экспорт разрешён." in app_js
    assert "loadResult(authoritative, {announce:false, preserveView:true})" in app_js
    assert "const preservedView = options.preserveView ?" in app_js
    assert "state.previewPage = null" in app_js
    assert "if (pageChanged) setZoom(1)" in app_js
    assert "justify-content:flex-start" in css
    assert "flex:0 0 auto" in css
    assert "transform-origin:top left" in css


def test_review_layout_contains_overflow_inside_workspace_and_pdf_pane():
    css = (ROOT / "averon_import" / "static" / "styles.css").read_text(encoding="utf-8")

    assert "html, body { width:100%; min-width:0" in css
    assert ".topbar { min-width:0" in css
    assert ".review-toolbar { display:flex; min-width:0; flex-wrap:wrap" in css
    assert ".pdf-stage { flex:1; min-width:0; overflow:auto; padding:18px 18px 18px 0" in css
    assert "overflow-x:hidden" not in css


def test_semantic_review_preview_is_visible_without_canonical_values():
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")

    assert 'row.row_type === "semantic_review"' in app_js
    assert "semantic_review_preview" in app_js
    assert "Проверить:" in app_js


def test_sourcing_modal_shows_audited_product_understanding_without_raw_transport_data():
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "averon_import" / "static" / "styles.css").read_text(encoding="utf-8")

    assert "function renderProductUnderstanding(understanding)" in app_js
    assert "Исходные данные" in app_js
    assert "Разбор позиции" in app_js
    assert "Предположение AI" in app_js
    assert "AI Studio · Qwen" in app_js
    assert "Локальный разбор · Qwen недоступен" in app_js
    assert "result.understanding" in app_js
    assert ".understanding-panel" in css
    assert "understanding.provenance.base_url" not in app_js
    assert "JSON.stringify(understanding)" not in app_js


def test_settings_ui_separates_vision_and_qwen_credentials():
    html = (ROOT / "averon_import" / "templates" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="settings-api-key"' in html
    assert 'id="settings-ai-api-key"' in html
    assert "Yandex Vision API key" in html
    assert "API-ключ Qwen / AI Studio" in html
    assert "vision_api_key_configured" in app_js
    assert "ai_api_key_configured" in app_js
    assert "payload.ai_api_key = aiApiKey" in app_js


def test_settings_ui_exposes_separate_sourcing_provider_and_demo_store_url():
    html = (ROOT / "averon_import" / "templates" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'id="settings-sourcing-provider"' in html
    assert 'value="local_catalog"' in html
    assert 'value="demo_store_http"' in html
    assert 'id="settings-demo-store-url"' in html
    assert "demo_store_base_url" in app_js
    assert "updateSourcingProviderFields" in app_js
    assert "sourcing: {" in app_js


def test_settings_ui_exposes_etm_ipro_without_exposing_credentials():
    html = (ROOT / "averon_import" / "templates" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'value="etm_ipro"' in html
    assert 'id="settings-etm-environment"' in html
    assert 'id="settings-etm-warehouses"' in html
    assert 'id="settings-etm-login"' in html
    assert 'id="settings-etm-password"' in html
    assert "payload.etm_login = etmLogin" in app_js
    assert "payload.etm_password = etmPassword" in app_js
    assert "login_configured" in app_js
    assert "password_configured" in app_js


def test_sourcing_offer_cards_show_provider_evidence_and_safe_external_link():
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")

    assert "function sourcingProviderLabel(offer)" in app_js
    assert 'averon_demo_store: "Averon Demo Store"' in app_js
    assert 'demo_store_http: "Averon Demo Store"' in app_js
    assert "Совпало:" in app_js
    assert "Конфликт:" in app_js
    assert 'target="_blank" rel="noopener noreferrer"' in app_js
    assert "Открыть у поставщика ↗" in app_js
    assert "safeOfferUrl(offer?.url)" in app_js
    assert "target=\"_blank\" rel=\"noopener noreferrer\"" in app_js
    assert 'provider === "etm_ipro"' not in app_js
    assert "pollSourcingJob" in app_js
    assert "job.current" in app_js
    assert "job.message" in app_js
    assert "Product Understanding → поиск → deterministic matching" in app_js
    assert "Позиции обрабатываются последовательно" in app_js


def test_project_sourcing_ui_explains_deterministic_match_state():
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")

    assert "function projectMatch(item)" in app_js
    assert "function projectDecision(item)" in app_js
    assert "function projectReason(item)" in app_js
    assert "Точное предложение не найдено" in app_js
    assert "Не подтверждено:" in app_js
    assert 'function renderSourcingDecision(decision, reason = "")' in app_js
    assert 'renderSourcingDecision(decision, reason)' in app_js
    assert 'MATCH: "Совпадение"' in app_js


def test_sourcing_notices_are_typed_and_do_not_render_raw_diagnostics():
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")

    assert "function renderSourcingNotices(notices)" in app_js
    assert "notice.user_visible === false" in app_js
    assert "sourcing-notice-${severity}" in app_js
    assert "escapeHtml(message)" in app_js
    assert app_js.count("renderSourcingNotices(result.notices)") == 2
    assert "(result.warnings || []).length" not in app_js
    assert "Qwen returned unsupported attribute keys; they were ignored" not in app_js


def test_project_sourcing_ui_marks_partial_totals_and_hides_provider_keys():
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")

    assert "Подтверждённая стоимость по позициям с ценой" in app_js
    assert "Стоимость альтернатив" in app_js
    assert "matched_unpriced_count" in app_js
    assert "alternative_unpriced_count" in app_js
    assert "offer?.provider || \"—\"" not in app_js
    assert "sourcingProviderLabel(offer)" in app_js


def test_project_sourcing_ui_guards_price_units_in_row_and_summary_totals():
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")

    assert "const PRICE_UNIT_FAMILIES" in app_js
    assert "function priceUnitsCompatible(sourceUnit, priceUnit)" in app_js
    assert "!priceUnitsCompatible(intent?.unit, offer?.price_unit)" in app_js
    assert 'total === null ? "Требует проверки"' in app_js
    assert 'offer.price_unit ? `/ ${escapeHtml(offer.price_unit)}` : "Единица цены не указана"' in app_js
    assert "unit_confirmation_count" in app_js
    assert "Проверить единицу цены" in app_js
    assert "matchedUnpriced > 0 || unitConfirmation > 0 || unresolved > 0" in app_js


def test_project_sourcing_ui_surfaces_review_candidate_before_weak_alternative():
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")

    assert "function projectRecommendedMatch(item)" in app_js
    assert "if (item.review_candidate) return item.review_candidate;" in app_js
    assert "result.review_candidate || recommended" in app_js
    assert "Альтернатива:" in app_js


def test_project_review_rows_expose_existing_candidates_without_network_actions():
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")

    assert "function projectReviewCandidates(item)" in app_js
    assert 'projectDecision(item) === "REVIEW"' in app_js
    assert 'candidate.decision === "REJECT"' in app_js
    assert "Посмотреть варианты" in app_js
    assert "data-project-item-index" in app_js
    assert "item.review_candidate" in app_js
    assert "item.match_results" in app_js
    assert "slice(0, 5)" in app_js
    assert "renderOfferCard(candidate, true, intent)" in app_js
    assert "renderProductUnderstanding(item.understanding)" in app_js
    assert "function renderProjectItemDetails(projectResult, item)" in app_js


def test_project_review_candidate_cards_reuse_generic_offer_renderer():
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
    detail = app_js.split("function renderProjectItemDetails", 1)[1].split("function bindSourcingFilters", 1)[0]

    assert "candidates.map((candidate) => renderOfferCard(candidate, true, intent))" in detail
    assert "Открыть у поставщика ↗" not in detail
    assert "provider === \"etm_ipro\"" not in detail


def test_project_review_detail_is_local_and_preserves_project_context():
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
    detail = app_js.split("function renderProjectItemDetails", 1)[1].split("function bindSourcingFilters", 1)[0]

    assert "project-results-back" in detail
    assert "← К результатам подбора" in detail
    assert "renderSourcingResult(projectResult)" in detail
    assert "api(" not in detail
    assert "/api/sourcing/search" not in detail
    assert "/api/sourcing/understand" not in detail
    assert "recommended_offer" not in detail
    assert "Рекомендуемое предложение" not in detail


def test_project_review_without_candidates_has_no_inspection_action():
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
    row = app_js.split("function renderProjectResultRow", 1)[1].split("function renderProjectSourcingList", 1)[0]

    assert "projectHasReviewCandidates(item)" in row
    assert 'class="button text project-review-candidates"' in row
    assert "projectReviewCandidates(item).length > 0" in app_js


def test_project_result_filters_and_totals_remain_on_project_render_path():
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")

    project_render = app_js.split("function renderSourcingResult", 1)[1].split("async function openSourcingForRow", 1)[0]
    project_list = app_js.split("function renderProjectSourcingList", 1)[1].split("function renderProjectItemDetails", 1)[0]
    assert "state.sourcing.result = result" in project_render
    assert "state.sourcing.projectFilter" in project_list
    assert "renderSourcingFilters(activeFilter)" in project_list
    assert "renderSourcingNotices(result.notices)" in project_render
    assert "confirmedTotals" in project_render
    assert "unitConfirmation" in project_render
    assert "bindSourcingFilters(result)" in project_render
    assert "bindProjectCandidateActions(result)" in project_render
