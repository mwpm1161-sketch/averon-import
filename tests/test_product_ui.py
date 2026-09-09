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


def test_semantic_review_preview_is_visible_without_canonical_values():
    app_js = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")

    assert 'row.row_type === "semantic_review"' in app_js
    assert "semantic_review_preview" in app_js
    assert "Проверить:" in app_js
