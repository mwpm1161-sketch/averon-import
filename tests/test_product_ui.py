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


def test_export_safety_uses_backend_page_status_and_pdf_viewer_resets_cleanly():
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "averon_import" / "static" / "styles.css").read_text(encoding="utf-8")

    assert "function backendExportBlockers()" in app_js
    assert "page_statuses" in app_js
    assert "output_status" in app_js
    assert "Backend подтвердил: экспорт разрешён." in app_js
    assert "loadResult(authoritative, {announce:false})" in app_js
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


def test_sourcing_offer_cards_show_provider_evidence_and_safe_external_link():
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")

    assert "offer.data_provenance?.source || offer.provider" in app_js
    assert "Совпало:" in app_js
    assert "Конфликт:" in app_js
    assert 'target="_blank" rel="noopener noreferrer"' in app_js
    assert "Открыть предложение" in app_js
    assert "pollSourcingJob" in app_js
    assert "job.current" in app_js
    assert "job.message" in app_js
    assert "Product Understanding → поиск → deterministic matching" in app_js
    assert "Позиции обрабатываются последовательно" in app_js
