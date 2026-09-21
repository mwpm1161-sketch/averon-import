from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path

from averon_import.services.sourcing.cache import SourcingCache
from averon_import.services.sourcing.models import Offer
from averon_import.services.sourcing.product_understanding import (
    SourcingAIService,
    build_fallback_intent,
)
from averon_import.services.sourcing.service import SourcingService


ROOT = Path(__file__).parents[1]


class FakeAIProvider:
    configured = True
    model = "fixture-qwen"

    def __init__(self, response: str | Exception):
        self.response = response

    def complete(self, messages, **kwargs):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeAIService:
    def __init__(self, provider):
        self.provider = provider

    def ensure_provider(self, key):
        assert key == "yandex"
        return self.provider


class ManualProvider:
    key = "manual-fixture"
    label = "Ручной fixture provider"

    def __init__(self, offers):
        self.offers = list(offers)

    def search(self, intent, *, limit=20):
        return self.offers[:limit]

    def stats(self):
        return {"item_count": len(self.offers), "catalog_version": "manual-fixture-1"}


def make_offer(**updates) -> Offer:
    payload = {
        "offer_id": "manual-offer",
        "provider": "manual-fixture",
        "source_item_id": "catalog-1",
        "title": "Насос циркуляционный MAGNA3 32-80",
        "article": "",
        "manufacturer": "",
        "brand": "",
        "price": Decimal("100.00"),
        "currency": "RUB",
        "price_unit": "шт.",
        "availability": True,
        "availability_text": "В наличии",
        "url": "",
        "attributes": {},
        "data_provenance": {"source": "fixture"},
    }
    payload.update(updates)
    return Offer(**payload)


def manual_row(**updates):
    row = {
        "id": "manual-1",
        "row_type": "item",
        "selected": True,
        "name": "Насос циркуляционный",
        "type_mark": "MAGNA3 32-80",
        "manufacturer": "Grundfos",
        "code": "",
        "quantity": "2",
        "unit": "шт.",
        "quantity_trusted": True,
        "status": "recognized",
        "source_origin": "manual",
    }
    row.update(updates)
    return row


def qwen_payload(row, **updates):
    payload = build_fallback_intent(row).model_dump(mode="json")
    payload.update(updates)
    return json.dumps(payload, ensure_ascii=False)


def test_manual_rows_build_same_source_contract_without_pdf_fields():
    row = manual_row(
        name="Насос циркуляционный Grundfos MAGNA3 32-80",
        quantity="2",
        unit="шт.",
    )
    intent = build_fallback_intent(row)

    assert intent.source_row_id == "manual-1"
    assert intent.quantity == "2"
    assert intent.unit == "шт."
    assert "Насос циркуляционный Grundfos MAGNA3 32-80" in intent.source_text
    assert "Grundfos" in intent.source_text
    assert "шт." in intent.source_text


def test_manual_model_only_and_article_only_rows_are_usable():
    model_intent = build_fallback_intent({
        "id": "model-only",
        "row_type": "item",
        "type_mark": "S203 C16",
        "manufacturer": "ABB",
        "quantity": "10",
        "unit": "шт.",
        "source_origin": "manual",
    })
    article_intent = build_fallback_intent({
        "id": "article-only",
        "row_type": "item",
        "code": "2CDS253001R0164",
        "quantity": "10",
        "unit": "шт.",
        "source_origin": "manual",
    })

    assert model_intent.model == "S203 C16"
    assert model_intent.manufacturer == "ABB"
    assert model_intent.source_text.startswith("S203 C16")
    assert article_intent.article == "2CDS253001R0164"
    assert article_intent.source_text.startswith("2CDS253001R0164")


def test_manual_explicit_source_fields_are_locked_and_row_is_immutable():
    row = manual_row(
        name="Автоматический выключатель",
        type_mark="S203 C16",
        manufacturer="ABB",
        code="SOURCE-ARTICLE",
        quantity="10",
        unit="шт.",
    )
    before = copy.deepcopy(row)
    ai = SourcingAIService(FakeAIService(FakeAIProvider(qwen_payload(
        row,
        manufacturer="Other",
        model="S203 C20",
        article="OTHER-ARTICLE",
        quantity="99",
        unit="м",
    ))))

    result = ai.understand_with_audit(row, build_fallback_intent(row))

    assert row == before
    resolved = result.resolved_intent
    assert resolved.manufacturer == "ABB"
    assert resolved.model == "S203 C16"
    assert resolved.article == "SOURCE-ARTICLE"
    assert resolved.quantity == "10"
    assert resolved.unit == "шт."


def service_for_manual(tmp_path, offers, ai=None):
    provider = ManualProvider(offers)
    return SourcingService(
        {provider.key: provider},
        default_provider=provider.key,
        ai=ai or SourcingAIService(),
        cache=SourcingCache(tmp_path / "manual-cache.json"),
    )


def test_manual_row_uses_project_pipeline_and_trusted_total_without_document(tmp_path):
    service = service_for_manual(tmp_path, [make_offer(
        offer_id="exact",
        source_item_id="manual-catalog-1",
        article="SOURCE-ARTICLE",
        manufacturer="ABB",
        title="Автоматический выключатель S203 C16",
        price=Decimal("12.50"),
    )])
    row = manual_row(
        name="Автоматический выключатель",
        type_mark="S203 C16",
        manufacturer="ABB",
        code="SOURCE-ARTICLE",
        quantity="10",
        unit="шт.",
    )

    result = service.search_project([row], ai_rerank=False)

    assert result.positions_total == result.positions_processed == 1
    assert result.positions_matched == 1
    assert result.confirmed_total == Decimal("125.00")
    assert result.confirmed_currency == "RUB"
    assert result.results[0].recommended_offer.offer_id == "exact"


def test_manual_quantity_without_unit_is_sourced_but_not_trusted_for_total(tmp_path):
    service = service_for_manual(tmp_path, [make_offer(
        offer_id="no-unit",
        article="SOURCE-ARTICLE",
        manufacturer="Grundfos",
        title="Насос циркуляционный MAGNA3 32-80",
    )])
    row = manual_row(unit="", quantity_trusted=False, code="SOURCE-ARTICLE")

    result = service.search_project([row], ai_rerank=False)

    assert result.positions_processed == 1
    assert result.confirmed_totals == {}
    assert result.confirmed_total is None
    assert any("quantity requires confirmation" in warning for warning in result.warnings)


def test_manual_ambiguous_candidates_remain_review_and_unresolved(tmp_path):
    offers = [
        make_offer(offer_id="ambiguous-a", source_item_id="a", title="Насос MAGNA3 32-80"),
        make_offer(offer_id="ambiguous-b", source_item_id="b", title="Насос MAGNA3 32-80"),
    ]
    service = service_for_manual(tmp_path, offers)
    row = manual_row(
        id="ambiguous",
        name="Насос",
        type_mark="MAGNA3 32-80",
        manufacturer="",
        code="",
    )

    result = service.search_project([row], ai_rerank=False)

    assert result.results[0].recommended_offer is None
    assert result.positions_review == 1
    assert result.unresolved_count == 1
    assert result.confirmed_totals == {}


def test_manual_alternative_and_zero_evidence_never_enter_confirmed_total(tmp_path):
    alternative_service = service_for_manual(tmp_path, [make_offer(
        offer_id="alternative",
        manufacturer="Other",
        title="Насос циркуляционный MAGNA3 32-80",
    )])
    alternative = alternative_service.search_project([manual_row(manufacturer="Preferred")], ai_rerank=False)
    assert alternative.confirmed_totals == {}
    assert alternative.alternative_total == Decimal("200.00")

    zero_service = service_for_manual(tmp_path, [make_offer(
        offer_id="unrelated",
        title="Совершенно другой товар",
        article="OTHER",
        manufacturer="Other",
    )])
    zero = zero_service.search_project([manual_row(code="", type_mark="", manufacturer="")], ai_rerank=False)
    assert zero.results[0].recommended_offer is None
    assert zero.confirmed_totals == {}
    assert zero.unresolved_count == 1


def test_manual_qwen_failure_uses_existing_deterministic_fallback(tmp_path):
    ai = SourcingAIService(FakeAIService(FakeAIProvider(RuntimeError("safe fixture outage"))))
    service = service_for_manual(tmp_path, [make_offer()], ai=ai)
    row = manual_row()

    result = service.search_project([row], ai_rerank=False)

    assert result.results[0].ai_mode == "fallback"
    assert result.results[0].intent.model == "MAGNA3 32-80"


def test_manual_frontend_has_separate_draft_and_reuses_project_pipeline():
    html = (ROOT / "averon_import" / "templates" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "averon_import" / "static" / "styles.css").read_text(encoding="utf-8")

    for marker in (
        "Тендер без спецификации",
        "open-manual-entry",
        "manual-view",
        "manual-add-row",
        "manual-paste-list",
        "manual-project-sourcing-button",
        "manual-paste-input",
        "manual-unit-suggestions",
    ):
        assert marker in html
    for marker in (
        "sessionStorage",
        "MANUAL_DRAFT_KEY",
        "source_origin: \"manual\"",
        "function runProjectSourcing(rows, documentId = null)",
        'const url = documentId ? `/api/documents/${documentId}/sourcing/search-all` : "/api/sourcing/search-all"',
        'await runProjectSourcing(manualRowsForSourcing(), null)',
        'api("/api/sourcing/understand"',
        "quantityTrusted",
        "crypto.randomUUID",
        "function parseManualPaste(text)",
    ):
        assert marker in app
    assert "manual-mode .steps" in css
    assert "contenteditable" not in app

    mapping = app.split("function manualRowsForSourcing()", 1)[1].split("function renderManualUnderstandingWarnings", 1)[0]
    assert "source_text" not in mapping


def test_manual_frontend_replaces_only_blank_placeholder_on_paste():
    app = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
    paste = app.split("function applyManualPaste()", 1)[1].split("function clearManualDraft", 1)[0]

    assert "function isBlankManualPlaceholder(row)" in app
    assert "MANUAL_FIELDS.every((key) => !String(row[key] || \"\").trim())" in app
    assert "state.manual.rows.length === 1 && isBlankManualPlaceholder(state.manual.rows[0])" in paste
    assert "state.manual.rows = rows" in paste
    assert "state.manual.rows.push(...rows)" in paste


def test_manual_understanding_validates_before_api_and_allows_warning_only_rows():
    app = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
    understanding = app.split("async function openManualUnderstanding(row)", 1)[1].split("async function resumeLastDocument", 1)[0]

    assert "const validation = manualRowValidation(row)" in understanding
    assert "if (!validation.valid)" in understanding
    assert "updateManualRowFeedback(row)" in understanding
    assert 'toast(validation.errors[0], "error")' in understanding
    assert 'api("/api/sourcing/understand"' in understanding
    assert understanding.index("if (!validation.valid)") < understanding.index('api("/api/sourcing/understand"')
    assert "else if (!unit) warnings.push" in app
