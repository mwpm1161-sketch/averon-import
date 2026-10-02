from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
from pathlib import Path
import sqlite3
import threading

import pytest

from averon_import.core.unit_normalization import normalize_sourcing_unit_family, normalize_unit_family
from averon_import.services.one_c_history.read_model import OneCHistoryReadError
from averon_import.services.one_c_history.repository import OneCHistoryRepository
from averon_import.services.one_c_history.xlsx_import import ParsedEvent, ParsedWorkbook
from averon_import.services.sourcing.models import (
    HistoryRetrievalClassification,
    MatchDecision,
    Offer,
    ProductIntent,
    SourcingSourceMode,
)
from averon_import.services.sourcing.providers.one_c_history import (
    HistoryMatchOutcome,
    HistorySafeMatchBasis,
    OneCHistoryProvider,
    normalize_exact_source_name,
    normalize_product_search_text,
)
from averon_import.services.sourcing.providers.base import SourcingProviderCachePolicy
from averon_import.services.sourcing import service as sourcing_service_module
from averon_import.services.sourcing.service import SourcingService


def _event(
    item_id="code:001",
    *,
    item_code="001",
    name="Насос тестовый",
    unit="шт",
    article="ART-001",
    manufacturer="",
    characteristic="",
    row=2,
    when="2026-08-01",
    price="12.50",
    reported="12.50",
    amount="25.00",
    quantity="2",
    price_usable=True,
    counterparty="Контрагент синтетический",
    currency=None,
    group_number=1,
):
    return ParsedEvent(
        item_key=item_id,
        item_code=item_code,
        item_name=name,
        raw_unit=unit,
        unit_family=normalize_unit_family(unit),
        identity_quality="stable_code_present" if item_code else "degraded_missing_stable_code",
        group_number=group_number,
        source_row=row,
        document_date=when,
        document_type="Поступление товаров и услуг",
        document_reference=f"synthetic-{row}",
        counterparty=counterparty,
        contract="synthetic contract",
        quantity=quantity,
        reported_unit_price_gross=reported,
        effective_unit_price_gross=price,
        amount_gross=amount,
        price_usable=price_usable,
        optional_facts={
            "article": article,
            "manufacturer": manufacturer,
            "characteristic": characteristic,
            "currency": currency,
        },
        source_facts={},
    )


def _parsed(events, *, source_sha=None, fingerprint=None):
    identity = "|".join(
        f"{event.item_key}:{event.source_row}:{event.document_date}:{event.effective_unit_price_gross}"
        for event in events
    )
    sha = source_sha or hashlib.sha256(("source:" + identity).encode()).hexdigest()
    semantic = fingerprint or hashlib.sha256(("semantic:" + identity).encode()).hexdigest()
    item_ids = {event.item_key for event in events}
    document_types: dict[str, int] = {}
    for event in events:
        document_types[event.document_type] = document_types.get(event.document_type, 0) + 1
    dates = sorted(event.document_date for event in events if event.document_date)
    return ParsedWorkbook(
        filename="synthetic-history.xlsx",
        file_sha256=sha,
        sheet_name="Synthetic",
        header_row=1,
        headers=[],
        header_signature="a" * 64,
        layout_type="flat",
        field_mapping={},
        group_header_row=None,
        event_header_row=1,
        group_headers=[],
        group_field_mapping={},
        event_field_mapping={},
        group_header_signature=None,
        event_header_signature="a" * 64,
        item_name_parse_strategy="none",
        events=list(events),
        item_count=len(item_ids),
        group_count=len(item_ids),
        physical_row_count=len(events),
        distinct_counterparty_count=len({event.counterparty for event in events if event.counterparty}),
        unit_vocabulary_count=len({event.raw_unit for event in events}),
        period_start=dates[0] if dates else None,
        period_end=dates[-1] if dates else None,
        document_type_counts=document_types,
        document_type_other_event_count=0,
        supplier_missing_count=sum(not event.counterparty for event in events),
        unusable_price_count=sum(not event.price_usable for event in events),
        missing_code_count=sum(not event.item_code for event in events),
        repeated_display_label_count=0,
        normalized_display_collision_count=0,
        code_conflict_count=0,
        skipped_row_count=0,
        warnings=[],
    ), semantic


def _activate(repository: OneCHistoryRepository, events, *, source_sha=None, fingerprint=None):
    parsed, semantic = _parsed(events, source_sha=source_sha, fingerprint=fingerprint)
    staging = repository.build_staging_snapshot(
        parsed,
        profile_id=None,
        semantic_import_fingerprint=semantic,
    )
    repository.activate(staging)
    return parsed, semantic


def _intent(*, name="Насос тестовый", article="ART-001", unit="шт", model="", required=None, queries=None):
    return ProductIntent(
        source_row_id="synthetic-row-1",
        source_text=name,
        normalized_name=name,
        article=article,
        unit=unit,
        model=model,
        required_attributes=required or {},
        search_queries=queries or [],
    )


def _provider(tmp_path: Path, events=None):
    repository = OneCHistoryRepository(tmp_path / "data")
    if events is not None:
        _activate(repository, events)
    return repository, OneCHistoryProvider(repository)


def test_absent_snapshot_returns_safe_unavailable_state_without_paths(tmp_path):
    repository, provider = _provider(tmp_path)

    assert provider.search(_intent()) == []
    result = provider.lookup(_intent())
    stats = provider.stats()

    assert result.outcome == HistoryMatchOutcome.NO_MATCH
    assert result.available is False
    assert result.reason_code == "history_unavailable"
    assert stats.reachable is False
    assert stats.catalog_version == "unavailable"
    assert str(repository.root) not in stats.error
    assert "sqlite" not in stats.error.casefold()


def test_one_item_one_usable_event_projects_decimal_historical_offer(tmp_path):
    _, provider = _provider(tmp_path, [_event()])

    offers = provider.search(_intent())

    assert len(offers) == 1
    assert offers[0].price == Decimal("12.50")
    assert isinstance(offers[0].price, Decimal)
    assert offers[0].data_provenance["source_kind"] == "historical_purchase"


def test_multiple_events_collapse_to_one_item_and_latest_usable_price(tmp_path):
    events = [
        _event(row=2, when="2026-06-01", price="10", reported="10", amount="20"),
        _event(row=3, when="2026-08-01", price="18.75", reported="18.75", amount="37.5"),
        _event(row=4, when="2026-08-01", price="19.25", reported="19.25", amount="38.5"),
    ]
    _, provider = _provider(tmp_path, events)

    offers = provider.search(_intent())

    assert len(offers) == 1
    assert offers[0].price == Decimal("19.25")
    assert offers[0].data_provenance["purchase_date"] == "2026-08-01"
    assert offers[0].data_provenance["selected_event_id"].endswith(":4")


def test_unusable_newest_event_does_not_replace_latest_usable_price(tmp_path):
    events = [
        _event(row=2, when="2026-07-01", price="10.5", reported="10.5", amount="21"),
        _event(row=3, when="2026-09-01", price=None, reported=None, amount=None, price_usable=False),
    ]
    _, provider = _provider(tmp_path, events)

    offer = provider.search(_intent())[0]

    assert offer.price == Decimal("10.5")
    assert offer.data_provenance["purchase_date"] == "2026-07-01"
    assert offer.data_provenance["selected_event_id"].endswith(":2")


def test_item_without_usable_price_remains_unpriced_and_cannot_be_safe(tmp_path):
    _, provider = _provider(tmp_path, [
        _event(price=None, reported=None, amount=None, price_usable=False),
    ])

    result = provider.lookup(_intent())

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert len(result.candidates) == 1
    assert result.candidates[0].price is None
    assert result.selected_offer is None


def test_stable_code_variants_are_searchable_but_emit_one_canonical_candidate(tmp_path):
    events = [
        _event(item_id="code:001", item_code="001", name="Насос версия А", article="OLD-1", row=2),
        _event(item_id="code:001", item_code="001", name="Насос версия Б", article="NEW-1", row=3, when="2026-09-01", price="14"),
    ]
    _, provider = _provider(tmp_path, events)

    offers = provider.search(_intent(name="Насос версия Б", article="NEW-1"))
    previous_variant = provider.search(_intent(name="Насос версия А", article="OLD-1"))

    assert len(offers) == 1
    assert offers[0].title == "Насос версия Б"
    assert offers[0].source_item_id == "code:001"
    assert len(previous_variant) == 1
    assert previous_variant[0].title == "Насос версия А"
    assert previous_variant[0].source_item_id == "code:001"


def test_different_stable_codes_with_same_name_are_never_merged(tmp_path):
    events = [
        _event(item_id="code:A", item_code="A", name="Одинаковое имя", article="A-1", row=2),
        _event(item_id="code:B", item_code="B", name="Одинаковое имя", article="B-1", row=3),
    ]
    _, provider = _provider(tmp_path, events)

    offers = provider.search(_intent(name="Одинаковое имя", article=""))

    assert len(offers) == 2
    assert {offer.source_item_id for offer in offers} == {"code:A", "code:B"}


def test_duplicate_weak_groups_remain_separate_and_name_only_is_review(tmp_path):
    events = [
        _event(item_id="weak:1", item_code=None, name="Одинаковое имя", article="", row=2, group_number=1),
        _event(item_id="weak:2", item_code=None, name="Одинаковое имя", article="", row=3, group_number=2),
    ]
    _, provider = _provider(tmp_path, events)

    result = provider.lookup(_intent(name="Одинаковое имя", article=""))

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert len(result.candidates) == 2
    assert {offer.data_provenance["group_number"] for offer in result.candidates} == {1, 2}


def test_exact_article_match_with_compatible_unit_and_price_is_safe(tmp_path):
    _, provider = _provider(tmp_path, [_event()])

    result = provider.lookup(_intent())

    assert result.outcome == HistoryMatchOutcome.SAFE_MATCH
    assert result.safe_basis == HistorySafeMatchBasis.EXACT_ARTICLE
    assert result.selected_offer is not None
    assert result.match_results[0].decision == MatchDecision.MATCH
    assert result.selected_offer.data_provenance["source"] == "one_c_history"


def test_conflicting_variant_and_price_event_units_cannot_be_safe(tmp_path):
    events = [
        _event(unit="шт", row=2, when="2026-06-01", price="10", reported="10", amount="20"),
        _event(unit="кг", row=3, when="2026-08-01", price="18", reported="18", amount="36"),
    ]
    repository, provider = _provider(tmp_path, events)

    result = provider.lookup(_intent(unit="кг"))
    snapshot_item = repository.read_catalog_snapshot().items[0]

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert result.selected_offer is None
    assert result.candidates[0].price_unit == "кг"
    retrieved = provider._retrieve(provider._projection, _intent(unit="кг"), 20)[1][0]
    assert retrieved.variant.raw_unit == "шт"
    assert retrieved.indexed.selected_price_event.raw_unit == "кг"
    assert "unit_family_conflict" in snapshot_item.integrity_conflicts


def test_whole_item_unit_conflict_blocks_when_matched_variant_and_latest_event_agree(tmp_path):
    events = [
        _event(name="Насос тестовый", article="", unit="шт", row=2, when="2026-06-01", price="10", reported="10", amount="20"),
        _event(name="Насос тестовый", article="ART-001", unit="кг", row=3, when="2026-08-01", price="18", reported="18", amount="36"),
    ]
    repository, provider = _provider(tmp_path, events)

    result = provider.lookup(_intent(unit="кг"))
    snapshot_item = repository.read_catalog_snapshot().items[0]

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert result.selected_offer is None
    assert result.candidates[0].article == "ART-001"
    assert result.candidates[0].price_unit == "кг"
    assert result.match_results[0].decision == MatchDecision.MATCH
    retrieved = provider._retrieve(provider._projection, _intent(unit="кг"), 20)[1][0]
    assert retrieved.variant.raw_unit == "кг"
    assert retrieved.indexed.selected_price_event.raw_unit == "кг"
    assert "unit_family_conflict" in snapshot_item.integrity_conflicts


def test_event_code_mismatch_with_canonical_stable_code_cannot_be_safe(tmp_path):
    events = [
        _event(item_code="001", row=2, when="2026-06-01", price="10", reported="10", amount="20"),
        _event(item_code="002", row=3, when="2026-08-01", price="18", reported="18", amount="36"),
    ]
    repository, provider = _provider(tmp_path, events)

    result = provider.lookup(_intent())
    snapshot_item = repository.read_catalog_snapshot().items[0]

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert result.selected_offer is None
    assert "item_code_conflict" in snapshot_item.integrity_conflicts
    assert all(offer.data_provenance.get("source_item_code") != "002" for offer in result.candidates)


def test_missing_descriptive_variants_remain_review_only(tmp_path):
    repository, provider = _provider(tmp_path, [
        _event(),
        _event(
            item_id="code:002", item_code="002", name="Клапан контрольный",
            article="VALVE-002", row=3,
        ),
    ])
    connection = repository._connect()
    try:
        connection.execute(
            "DELETE FROM nomenclature_descriptive_variants WHERE item_id = ?",
            ("code:001",),
        )
        connection.commit()
    finally:
        connection.close()

    result = provider.lookup(_intent())

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert result.selected_offer is None
    assert len(result.candidates) == 1
    damaged_item = next(item for item in provider._projection.items if item.item.item_id == "code:001")
    assert "missing_variant_provenance" in damaged_item.item.integrity_conflicts
    assert damaged_item.searchable_variants[0].provenance_valid is False
    valid_result = provider.lookup(_intent(name="Клапан контрольный", article="VALVE-002"))
    assert valid_result.outcome == HistoryMatchOutcome.SAFE_MATCH


def test_same_family_unit_aliases_across_intent_variant_and_event_can_be_safe(tmp_path):
    events = [
        _event(unit="штука", row=2, when="2026-06-01", price="10", reported="10", amount="20"),
        _event(unit="шт", row=3, when="2026-08-01", price="18", reported="18", amount="36"),
    ]
    _, provider = _provider(tmp_path, events)

    result = provider.lookup(_intent(unit="штуки"))

    assert result.outcome == HistoryMatchOutcome.SAFE_MATCH
    assert result.selected_offer is not None


@pytest.mark.parametrize(
    ("field", "first_value", "second_value", "conflict_code"),
    [
        ("article", "ART-001", "ART-002", "article_conflict"),
        ("manufacturer", "Maker A", "Maker B", "manufacturer_conflict"),
        ("characteristic", "сталь", "латунь", "characteristic_conflict"),
    ],
)
def test_phase1_stable_code_description_conflicts_block_safe_match(
    tmp_path, field, first_value, second_value, conflict_code,
):
    first = _event(row=2, when="2026-06-01", **{field: first_value})
    second = _event(row=3, when="2026-08-01", **{field: second_value})
    repository, provider = _provider(tmp_path, [first, second])

    intent_article = second_value if field == "article" else "ART-001"
    result = provider.lookup(_intent(article=intent_article))
    snapshot_item = repository.read_catalog_snapshot().items[0]

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert result.selected_offer is None
    assert conflict_code in snapshot_item.integrity_conflicts


def test_exact_article_and_resolved_required_attribute_can_be_safe(tmp_path):
    _, provider = _provider(tmp_path, [_event(characteristic="сталь", article="ART-001")])

    result = provider.lookup(_intent(required={"characteristic": "сталь"}))

    assert result.outcome == HistoryMatchOutcome.SAFE_MATCH
    assert result.match_results[0].decision == MatchDecision.MATCH


def test_conflicting_required_attribute_cannot_be_safe(tmp_path):
    _, provider = _provider(tmp_path, [_event(characteristic="сталь", article="ART-001")])

    result = provider.lookup(_intent(required={"characteristic": "латунь"}))

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert result.selected_offer is None
    assert any(match.conflicting_attributes for match in result.match_results)


def test_likely_match_is_never_safe(tmp_path):
    _, provider = _provider(tmp_path, [_event(article="")])

    result = provider.lookup(_intent(article=""))

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert result.selected_offer is None
    assert any(match.decision == MatchDecision.LIKELY_MATCH for match in result.match_results)


def test_name_only_exact_support_is_never_safe(tmp_path):
    _, provider = _provider(tmp_path, [_event(article="")])

    result = provider.lookup(_intent(article=""))

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert result.selected_offer is None


def test_conservative_exact_source_name_normalizer_preserves_punctuation_and_order():
    assert normalize_exact_source_name("\u00a0Клапан   DN50  ") == "клапан dn50"
    assert normalize_exact_source_name("Ёлка × 2") == "елка × 2"
    assert normalize_exact_source_name("Клапан DN50") != normalize_exact_source_name("Клапан DN 50")
    assert normalize_exact_source_name("Клапан A/B") != normalize_exact_source_name("Клапан A-B")
    assert normalize_exact_source_name("Клапан DN50 M-500") != normalize_exact_source_name("Клапан M-500 DN50")


def test_exact_source_name_unit_with_specification_token_is_safe_and_uses_company_currency_default(tmp_path):
    repository, provider = _provider(tmp_path, [
        _event(name="Клапан M-500", article="ART-001", currency=None),
    ])
    source = _intent(name="Клапан M-500", article="")

    result = provider.lookup(source, source_intent=source)

    assert result.outcome == HistoryMatchOutcome.SAFE_MATCH
    assert result.safe_basis == HistorySafeMatchBasis.EXACT_SOURCE_NAME_UNIT
    assert result.selected_offer is not None
    assert result.selected_offer.currency == "RUB"
    assert result.selected_offer.data_provenance["currency_basis"] == "company_default"
    assert repository.read_catalog_snapshot().items[0].events[0].currency == ""
    assert result.selected_offer.availability is None
    assert result.selected_offer.url == ""


@pytest.mark.parametrize("name", [
    "Кабель-1",
    "Насос-2",
    "Труба-20",
    "Болт-10",
    "Позиция-1",
    "Материал-5",
    "Кабель/1",
    "Насос/2",
    "Позиция/1",
])
def test_generic_word_separator_number_name_is_not_specific_enough_for_safe_history(name, tmp_path):
    _, provider = _provider(tmp_path, [_event(name=name, article="")])
    source = _intent(name=name, article="")

    result = provider.lookup(source, source_intent=source)

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert result.selected_offer is None


@pytest.mark.parametrize("name", [
    "Клапан DN50",
    "Клапан DN 50",
    "Клапан PN16",
    "Светильник IP65",
    "Цемент M-500",
    "Цемент М-500",
    "Шпунт AZ-13-770",
    "Насос 32-80",
    "Кабель 5x6",
    "Кабель 5×6",
    "Кабель 5х6",
])
def test_strong_engineering_specification_tokens_can_establish_safe_name_basis(name, tmp_path):
    _, provider = _provider(tmp_path, [_event(name=name, article="")])
    source = _intent(name=name, article="")

    result = provider.lookup(source, source_intent=source)

    assert result.outcome == HistoryMatchOutcome.SAFE_MATCH
    assert result.safe_basis == HistorySafeMatchBasis.EXACT_SOURCE_NAME_UNIT


def test_rdf_number_needs_separately_source_owned_model_for_name_specificity(tmp_path):
    _, provider = _provider(tmp_path, [
        _event(name="Регулятор RDF 310", article="", characteristic="RDF 310"),
    ])
    source_without_model = _intent(name="Регулятор RDF 310", article="")
    source_with_model = _intent(name="Регулятор RDF 310", article="", model="RDF 310")

    without_model = provider.lookup(source_without_model, source_intent=source_without_model)
    with_model = provider.lookup(source_with_model, source_intent=source_with_model)

    assert without_model.outcome == HistoryMatchOutcome.REVIEW
    assert without_model.selected_offer is None
    assert with_model.outcome == HistoryMatchOutcome.SAFE_MATCH
    assert with_model.safe_basis == HistorySafeMatchBasis.EXACT_SOURCE_NAME_UNIT


def test_exact_source_name_unit_allows_only_case_and_whitespace_variation(tmp_path):
    _, provider = _provider(tmp_path, [_event(name="  КЛАПАН   M-500  ", article="")])
    source = _intent(name="Клапан M-500", article="")

    result = provider.lookup(source, source_intent=source)

    assert result.outcome == HistoryMatchOutcome.SAFE_MATCH
    assert result.safe_basis == HistorySafeMatchBasis.EXACT_SOURCE_NAME_UNIT


def test_exact_source_name_punctuation_difference_stays_review(tmp_path):
    _, provider = _provider(tmp_path, [_event(name="Клапан DN-50", article="")])
    source = _intent(name="Клапан DN50", article="")

    result = provider.lookup(source, source_intent=source)

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert result.safe_basis is None


@pytest.mark.parametrize(("first_code", "second_code"), [("001", "002"), (None, None)])
def test_duplicate_exact_name_unit_across_canonical_items_stays_review(tmp_path, first_code, second_code):
    _, provider = _provider(tmp_path, [
        _event(item_id="item:1", item_code=first_code, name="Клапан DN50", article="", row=2, group_number=1),
        _event(item_id="item:2", item_code=second_code, name="Клапан DN50", article="", row=3, group_number=2),
    ])
    source = _intent(name="Клапан DN50", article="")

    result = provider.lookup(source, source_intent=source)

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert result.safe_basis is None


def test_loose_punctuation_collision_blocks_otherwise_unique_exact_name(tmp_path):
    _, provider = _provider(tmp_path, [
        _event(item_id="item:1", item_code="001", name="Клапан DN50", article="", row=2, group_number=1),
        _event(item_id="item:2", item_code="002", name="Клапан-DN50", article="ART-2", row=3, group_number=2),
    ])
    source = _intent(name="Клапан DN50", article="")

    result = provider.lookup(source, source_intent=source)

    assert result.outcome == HistoryMatchOutcome.REVIEW


def test_ai_only_article_cannot_create_article_safe_basis_or_rescue_generic_name(tmp_path):
    _, provider = _provider(tmp_path, [_event(name="Кабель", article="ART-001")])
    source = _intent(name="Кабель", article="")
    resolved = _intent(name="Кабель", article="ART-001")

    result = provider.lookup(resolved, source_intent=source)

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert result.safe_basis is None


def test_ai_only_article_does_not_change_independent_exact_name_basis(tmp_path):
    _, provider = _provider(tmp_path, [_event(name="Клапан M-500", article="ART-001")])
    source = _intent(name="Клапан M-500", article="")
    resolved = _intent(name="Клапан M-500", article="ART-001")

    result = provider.lookup(resolved, source_intent=source)

    assert result.outcome == HistoryMatchOutcome.SAFE_MATCH
    assert result.safe_basis == HistorySafeMatchBasis.EXACT_SOURCE_NAME_UNIT


def test_ai_only_preferred_attribute_cannot_satisfy_name_specificity_guard(tmp_path):
    _, provider = _provider(tmp_path, [_event(name="Кабель", article="")])
    source = _intent(name="Кабель", article="")
    resolved = source.model_copy(update={
        "preferred_attributes": {"material": "медь"},
        "evidence": {"attribute_origins": {"material": "ai_inferred"}},
    })

    result = provider.lookup(resolved, source_intent=source)

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert result.safe_basis is None


def test_source_article_mismatch_cannot_be_rescued_by_exact_name(tmp_path):
    _, provider = _provider(tmp_path, [_event(name="Клапан DN50", article="ART-001")])
    source = _intent(name="Клапан DN50", article="ART-999")

    result = provider.lookup(source, source_intent=source)

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert result.safe_basis is None


@pytest.mark.parametrize(
    ("field", "value", "when"),
    [("price", "0", "2026-08-01"), ("price", "12.50", ""), ("price", "12.50", "2099-01-01")],
)
def test_exact_article_zero_price_missing_or_future_date_cannot_be_safe(tmp_path, field, value, when):
    kwargs = {field: value, "when": when}
    _, provider = _provider(tmp_path, [_event(**kwargs)])

    result = provider.lookup(_intent())

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert result.safe_basis is None


def test_exact_name_model_conflict_and_required_attribute_conflict_stay_review(tmp_path):
    _, provider = _provider(tmp_path, [_event(name="Клапан M-500", article="", characteristic="Сталь")])
    model_source = _intent(name="Клапан M-500", article="", model="M-999")
    required_source = _intent(name="Клапан M-500", article="", required={"characteristic": "Латунь"})

    assert provider.lookup(model_source, source_intent=model_source).outcome == HistoryMatchOutcome.REVIEW
    assert provider.lookup(required_source, source_intent=required_source).outcome == HistoryMatchOutcome.REVIEW


def test_match_with_incompatible_history_unit_requires_review(tmp_path):
    _, provider = _provider(tmp_path, [_event(unit="г")])

    result = provider.lookup(_intent(unit="кг"))

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert result.selected_offer is None


def test_match_with_unknown_unit_requires_review(tmp_path):
    _, provider = _provider(tmp_path, [_event(unit="ящик")])

    result = provider.lookup(_intent(unit="ящик"))

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert result.selected_offer is None


def test_canonical_history_units_use_broader_unit_normalizer_without_changing_live_aliases(tmp_path):
    expected = {
        "шт": "piece", "кг": "kilogram", "г": "gram", "м": "meter",
        "м2": "square_meter", "л": "litre", "м3": "cubic_meter",
        "компл": "set", "пог. м": "meter", "упак": "pack", "Пар": "pair",
        "боб": "bobbin", "т": "tonne", "мл": "millilitre",
    }
    assert {unit: normalize_unit_family(unit) for unit in expected} == expected
    assert normalize_sourcing_unit_family("г") is None
    assert normalize_sourcing_unit_family("мл") is None
    assert normalize_sourcing_unit_family("Пар") is None


def test_fuzzy_retrieval_does_not_promote_similarity_to_safe_match(tmp_path):
    _, provider = _provider(tmp_path, [_event(name="Насос тестовый", article="OTHER-9")])

    result = provider.lookup(_intent(name="Насос тестов", article="MISSING-9"))

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert result.selected_offer is None
    assert result.candidates[0].history_retrieval_classification == HistoryRetrievalClassification.FUZZY


def test_exact_name_unit_precedes_noisy_fuzzy_neighbours_and_is_not_displaced_by_limit(tmp_path):
    _, provider = _provider(tmp_path, [
        _event(item_id="exact:1", item_code=None, name="Плющ искусственный", article="", row=2, group_number=1),
        _event(item_id="noise:1", item_code=None, name="Камень искусственный 3680х760х12 мм (белый)", article="", row=3, group_number=2),
        _event(item_id="noise:2", item_code=None, name="Камень искусственный 3680х760х12 мм (черный)", article="", row=4, group_number=3),
        _event(item_id="noise:3", item_code=None, name="Петрушка искусственная", article="", row=5, group_number=4),
    ])
    source = _intent(name="Плющ искусственный", article="", unit="шт")
    qwen_resolved = source.model_copy(update={
        "normalized_name": "Камень искусственный",
        "search_queries": ["Камень искусственный 3680х760х12 мм"],
    })

    fallback = provider.lookup(source, source_intent=source, limit=1)
    qwen = provider.lookup(qwen_resolved, source_intent=source, limit=1)

    assert fallback.outcome == HistoryMatchOutcome.REVIEW
    assert fallback.selected_offer is None
    assert [offer.title for offer in fallback.candidates] == ["Плющ искусственный"]
    assert [offer.source_item_id for offer in qwen.candidates] == [offer.source_item_id for offer in fallback.candidates]
    assert qwen.candidates[0].history_retrieval_classification == HistoryRetrievalClassification.EXACT_NAME_UNIT
    assert "Камень искусственный" not in qwen.candidates[0].title


def test_qwen_exact_looking_fuzzy_candidate_cannot_become_source_owned_safe_match(tmp_path):
    _, provider = _provider(tmp_path, [
        _event(item_id="history:1", name="Клапан M-500", article="ART-001"),
    ])
    source = _intent(name="Иная исходная позиция", article="", unit="шт")
    qwen_resolved = _intent(name="Клапан M-500", article="ART-001", unit="шт")

    result = provider.lookup(qwen_resolved, source_intent=source)

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert result.selected_offer is None
    assert len(result.candidates) == 1
    assert result.candidates[0].history_retrieval_classification == HistoryRetrievalClassification.FUZZY


def test_exact_article_index_returns_bounded_exact_identity_with_classification(tmp_path):
    _, provider = _provider(tmp_path, [
        _event(item_id="article:exact", name="Источник точный", article="ART-EXACT", row=2),
        *[
            _event(item_id=f"noise:{index}", item_code=None, name=f"Источник похожий {index}", article=f"OTHER-{index}", row=index + 3, group_number=index)
            for index in range(30)
        ],
    ])

    result = provider.lookup(_intent(name="Источник точный", article="ART-EXACT"), limit=1)

    assert [offer.source_item_id for offer in result.candidates] == ["article:exact"]
    assert result.candidates[0].history_retrieval_classification == HistoryRetrievalClassification.EXACT_ARTICLE


def test_exact_name_unit_ambiguity_returns_only_exact_candidates(tmp_path):
    _, provider = _provider(tmp_path, [
        _event(item_id="exact:1", item_code=None, name="Одинаковое имя", article="", row=2, group_number=1),
        _event(item_id="exact:2", item_code=None, name="Одинаковое имя", article="", row=3, group_number=2),
        _event(item_id="noise:1", item_code=None, name="Одинаковое очень похожее имя", article="", row=4, group_number=3),
    ])
    source = _intent(name="Одинаковое имя", article="")

    result = provider.lookup(source, source_intent=source, limit=20)

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert {offer.source_item_id for offer in result.candidates} == {"exact:1", "exact:2"}
    assert result.reason_code == "ambiguous_exact_name_identity"
    assert result.candidates[0].history_retrieval_classification == HistoryRetrievalClassification.EXACT_NAME_UNIT


def test_exact_name_unit_without_usable_price_remains_review_and_is_still_retrieved(tmp_path):
    _, provider = _provider(tmp_path, [
        _event(item_id="unpriced:1", item_code=None, name="Клапан M-500", article="", price_usable=False),
    ])
    source = _intent(name="Клапан M-500", article="")

    result = provider.lookup(source, source_intent=source)

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert result.selected_offer is None
    assert len(result.candidates) == 1
    assert result.candidates[0].price is None
    assert result.candidates[0].history_retrieval_classification == HistoryRetrievalClassification.EXACT_NAME_UNIT


def test_projection_builds_snapshot_local_retrieval_and_negative_collision_indexes(tmp_path):
    _, provider = _provider(tmp_path, [
        _event(item_id="exact:1", item_code=None, name="Клапан DN50", article="DN-50", row=2),
        _event(item_id="collision:1", item_code=None, name="Клапан-DN50", article="OTHER", row=3, group_number=2),
    ])
    projection = provider._load_projection()

    assert projection.exact_article_index["dn-50"]
    assert projection.exact_name_unit_index[("клапан dn50", "piece")]
    assert projection.loose_name_unit_item_ids[("клапанdn50", "piece")] == frozenset({"exact:1", "collision:1"})
    assert projection.matcher_article_item_ids
    with pytest.raises(TypeError):
        projection.exact_article_index["new"] = ()


def test_broader_existing_matcher_article_normalization_retrieves_but_does_not_make_safe(tmp_path):
    _, provider = _provider(tmp_path, [_event(article="KEV-9P2012E")])

    result = provider.lookup(_intent(article="КЭВ-9П2012Е"))

    assert len(result.candidates) == 1
    assert result.match_results[0].decision == MatchDecision.MATCH
    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert result.selected_offer is None


def test_multiple_equally_strong_article_matches_are_ambiguous(tmp_path):
    events = [
        _event(item_id="weak:1", item_code=None, article="DUP-1", row=2, group_number=1),
        _event(item_id="weak:2", item_code=None, article="DUP-1", row=3, group_number=2),
    ]
    _, provider = _provider(tmp_path, events)

    result = provider.lookup(_intent(article="DUP-1"))

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert len([match for match in result.match_results if match.decision == MatchDecision.MATCH]) == 2


def test_historical_offer_never_claims_live_availability_or_url_and_uses_company_currency_default(tmp_path):
    _, provider = _provider(tmp_path, [_event(currency=None)])

    offer = provider.search(_intent())[0]

    assert offer.availability is None
    assert "неизвестна" in offer.availability_text.casefold()
    assert offer.url == ""
    assert offer.currency == "RUB"
    assert offer.data_provenance["currency_basis"] == "company_default"


@pytest.mark.parametrize(
    ("source_currency", "effective_currency", "currency_basis"),
    [
        (None, "RUB", "company_default"),
        ("", "RUB", "company_default"),
        ("RUB", "RUB", "source"),
        ("rub", "RUB", "source"),
        ("USD", "USD", "source"),
    ],
)
def test_currency_policy_is_applied_to_offer_without_reimport_or_source_mutation(
    tmp_path, monkeypatch, source_currency, effective_currency, currency_basis
):
    repository, provider = _provider(tmp_path, [_event(currency=source_currency)])
    active_version = repository.catalog_version()
    source_snapshot = repository.read_catalog_snapshot()
    source_event_currency = source_snapshot.items[0].events[0].currency

    def reject_reactivation(*_args, **_kwargs):
        pytest.fail("currency policy must use the active snapshot without rebuilding it")

    monkeypatch.setattr(repository, "activate", reject_reactivation)

    offer = provider.search(_intent())[0]

    assert source_event_currency == (source_currency or "")
    assert repository.read_catalog_snapshot().items[0].events[0].currency == source_event_currency
    assert repository.catalog_version() == active_version
    assert offer.currency == effective_currency
    assert offer.data_provenance["currency_basis"] == currency_basis


def test_source_currency_is_normalized_and_price_fields_remain_decimal(tmp_path):
    _, provider = _provider(tmp_path, [_event(currency="usd")])

    offer = provider.search(_intent())[0]

    assert offer.currency == "USD"
    assert offer.data_provenance["currency_basis"] == "source"
    assert offer.price == Decimal("12.50")
    assert offer.data_provenance["reported_unit_price_gross"] == Decimal("12.50")
    assert offer.data_provenance["effective_unit_price_gross"] == Decimal("12.50")


def test_snapshot_version_uses_source_and_semantic_identity_not_import_time(tmp_path):
    repository = OneCHistoryRepository(tmp_path / "data")
    events = [_event()]
    source_sha = "a" * 64
    fingerprint = "b" * 64
    _activate(repository, events, source_sha=source_sha, fingerprint=fingerprint)
    first = repository.catalog_version()
    _activate(repository, events, source_sha=source_sha, fingerprint=fingerprint)

    assert first is not None
    assert repository.catalog_version() == first
    _activate(repository, events, source_sha="c" * 64, fingerprint=fingerprint)
    second = repository.catalog_version()
    assert second != first
    _activate(repository, events, source_sha="c" * 64, fingerprint="d" * 64)
    assert repository.catalog_version() != second


def test_projection_is_reused_for_same_snapshot_and_invalidated_on_activation(tmp_path, monkeypatch):
    repository, provider = _provider(tmp_path, [_event()])
    real_read = repository.read_catalog_snapshot
    read_count = 0

    def counted_read():
        nonlocal read_count
        read_count += 1
        return real_read()

    monkeypatch.setattr(repository, "read_catalog_snapshot", counted_read)
    provider.search(_intent())
    first_projection = provider._projection
    provider.search(_intent(name="Насос тестовый"))

    assert provider._projection is first_projection
    assert provider._projection_build_count == 1
    assert read_count == 1
    _activate(repository, [_event(row=2, name="Насос новый", article="ART-002")], source_sha="e" * 64, fingerprint="f" * 64)
    provider.search(_intent(name="Насос новый", article="ART-002"))
    assert provider._projection is not first_projection
    assert provider._projection_build_count == 2
    assert read_count == 2


def test_provider_search_and_stats_do_not_modify_active_snapshot(tmp_path):
    repository, provider = _provider(tmp_path, [_event()])
    before = hashlib.sha256(repository.database_path.read_bytes()).hexdigest()

    provider.stats()
    provider.search(_intent())
    provider.lookup(_intent())

    after = hashlib.sha256(repository.database_path.read_bytes()).hexdigest()
    assert after == before


def test_same_repository_lock_keeps_read_and_activation_from_mixing_snapshots(tmp_path, monkeypatch):
    repository = OneCHistoryRepository(tmp_path / "data")
    _activate(repository, [_event(name="Старый снимок")])
    next_snapshot, next_fingerprint = _parsed([_event(name="Новый снимок")], source_sha="9" * 64)
    staging = repository.build_staging_snapshot(
        next_snapshot,
        profile_id=None,
        semantic_import_fingerprint=next_fingerprint,
    )
    real_connect = repository._connect
    reader_inside_connect = threading.Event()
    release_reader = threading.Event()
    activation_started = threading.Event()
    activation_done = threading.Event()
    result = {}

    def blocked_connect(path=None):
        if path is None and threading.current_thread().name == "history-reader":
            reader_inside_connect.set()
            assert release_reader.wait(3)
        return real_connect(path)

    monkeypatch.setattr(repository, "_connect", blocked_connect)
    reader = threading.Thread(
        target=lambda: result.setdefault("snapshot", repository.read_catalog_snapshot()),
        name="history-reader",
    )
    reader.start()
    assert reader_inside_connect.wait(2)

    def activate_next():
        activation_started.set()
        repository.activate(staging)
        activation_done.set()

    activator = threading.Thread(target=activate_next, name="history-activator")
    activator.start()
    assert activation_started.wait(2)
    assert not activation_done.wait(0.05)
    release_reader.set()
    reader.join(3)
    activator.join(3)

    assert not reader.is_alive() and not activator.is_alive()
    assert result["snapshot"].items[0].display_name == "Старый снимок"
    assert repository.read_catalog_snapshot().items[0].display_name == "Новый снимок"


def test_provider_respects_zero_requested_limit_and_global_candidate_cap(tmp_path):
    events = [
        _event(item_id=f"weak:{index}", item_code=None, name=f"Насос серия {index}", article=f"A-{index}", row=index + 2, group_number=index)
        for index in range(70)
    ]
    _, provider = _provider(tmp_path, events)

    assert provider.search(_intent(name="Насос серия", article=""), limit=0) == []
    assert len(provider.search(_intent(name="Насос серия", article=""), limit=1)) == 1
    assert len(provider.search(_intent(name="Насос серия", article=""), limit=100)) <= 50


def test_malformed_decimal_fails_closed_for_price_candidate(tmp_path):
    _, provider = _provider(tmp_path, [_event()])
    with sqlite3.connect(provider.repository.database_path) as connection:
        connection.execute("UPDATE purchase_events SET effective_unit_price_gross_decimal='not-a-decimal'")

    result = provider.lookup(_intent())

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert result.candidates[0].price is None
    assert result.selected_offer is None


def test_malformed_optional_provenance_is_not_used_as_price_evidence(tmp_path):
    _, provider = _provider(tmp_path, [_event()])
    with sqlite3.connect(provider.repository.database_path) as connection:
        connection.execute("UPDATE purchase_events SET optional_facts_json='[]'")

    result = provider.lookup(_intent())

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert result.candidates[0].price is None
    assert result.selected_offer is None


def test_inconsistent_candidate_provenance_cannot_be_safe(tmp_path, monkeypatch):
    _, provider = _provider(tmp_path, [_event()])
    original = OneCHistoryProvider._to_offer

    def inconsistent(snapshot, candidate):
        offer = original(snapshot, candidate)
        provenance = {**offer.data_provenance, "history_item_id": "different-item"}
        return offer.model_copy(update={"data_provenance": provenance})

    monkeypatch.setattr(OneCHistoryProvider, "_to_offer", staticmethod(inconsistent))

    result = provider.lookup(_intent())

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert result.selected_offer is None


def test_offer_provenance_is_explicit_and_excludes_unrelated_import_fields(tmp_path):
    _, provider = _provider(tmp_path, [_event(article="Art- 001", name="  Source Name  ")])

    offer = provider.search(_intent(name="Source Name", article=""))[0]

    assert offer.title == "  Source Name  "
    assert offer.article == "Art- 001"
    assert offer.brand == ""
    assert "contract" not in offer.data_provenance
    assert "source_facts_json" not in offer.data_provenance
    assert "path" not in offer.data_provenance


def test_corrupt_snapshot_returns_typed_path_free_unavailable_result(tmp_path):
    repository, provider = _provider(tmp_path, [_event()])
    repository.database_path.write_bytes(b"not a sqlite snapshot")

    result = provider.lookup(_intent())
    stats = provider.stats()

    assert result.available is False
    assert result.reason_code == "history_unavailable"
    assert stats.reachable is False
    assert str(repository.database_path) not in stats.error
    assert "sqlite" not in stats.error.casefold()
    with pytest.raises(OneCHistoryReadError):
        repository.read_catalog_snapshot()


def test_provider_stats_expose_bounded_snapshot_version_and_item_count(tmp_path):
    _, provider = _provider(tmp_path, [_event()])

    stats = provider.stats()

    assert stats.reachable is True
    assert stats.item_count == 1
    assert len(stats.catalog_version) == 27
    assert stats.catalog_version.startswith("1c-")


def test_conservative_search_normalization_preserves_identifier_punctuation_and_meaning():
    assert normalize_product_search_text("  ёлка × 2  ") == "елка x 2"
    assert normalize_product_search_text("M-20/5") != normalize_product_search_text("M205")


def test_history_provider_is_not_registered_in_current_sourcing_runtime():
    source = (Path(__file__).resolve().parents[1] / "averon_import" / "services" / "sourcing" / "runtime.py").read_text(encoding="utf-8")
    provider_map = source.split("providers: dict[str, SourcingProvider] = {", 1)[1].split("\n    }", 1)[0]
    assert "OneCHistoryProvider" not in provider_map
    assert "one_c_history_provider=(" in source


class _CountingLiveProvider:
    key = "stub_live"
    label = "Поставщик для теста"
    cache_policy = SourcingProviderCachePolicy(cache_search_results=True)

    def __init__(self, *, fail=False, stats_fail=False, empty=False):
        self.stats_calls = 0
        self.search_calls = 0
        self.fail = fail
        self.stats_fail = stats_fail
        self.empty = empty

    def stats(self):
        self.stats_calls += 1
        if self.stats_fail:
            raise ValueError("synthetic provider health failure")
        return {"reachable": True, "catalog_version": "live-v1", "item_count": 1}

    def search(self, intent, *, limit=20):
        self.search_calls += 1
        if self.fail:
            raise ValueError("synthetic provider failure")
        if self.empty:
            return []
        title = intent.normalized_name or "Тестовое предложение"
        return [Offer(
            offer_id=f"live:{intent.source_row_id}",
            provider=self.key,
            title=title,
            article=intent.article,
            manufacturer=intent.manufacturer,
            price=Decimal("10"),
            currency="RUB",
            price_unit=intent.unit or "шт",
        )][:limit]


def _routed_service(tmp_path, events=None, *, live=None):
    repository = OneCHistoryRepository(tmp_path / "data")
    if events is not None:
        _activate(repository, events)
    history_provider = OneCHistoryProvider(repository)
    live_provider = live or _CountingLiveProvider()
    return (
        SourcingService(
            {live_provider.key: live_provider},
            default_provider=live_provider.key,
            one_c_history_provider=history_provider,
        ),
        repository,
        history_provider,
        live_provider,
    )


def _row(name, *, source_id="row-1", code="", unit="шт", quantity="1"):
    return {
        "id": source_id,
        "source_row_id": source_id,
        "row_type": "item",
        "name": name,
        "code": code,
        "unit": unit,
        "quantity": quantity,
    }


def test_provider_only_default_is_identical_and_never_touches_history(tmp_path, monkeypatch):
    service, repository, history_provider, live = _routed_service(tmp_path, [_event()])
    monkeypatch.setattr(history_provider, "lookup", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("history lookup")))
    monkeypatch.setattr(repository, "catalog_version", lambda: (_ for _ in ()).throw(AssertionError("history read")))
    row = _row("Насос тестовый", code="ART-001")

    old_result = service.search_row(row, ai_rerank=False)
    default_result = service.search_row_routed(row, ai_rerank=False)
    explicit_result = service.search_row_routed(row, source_mode=SourcingSourceMode.PROVIDER_ONLY, ai_rerank=False)

    assert default_result.route is None
    assert explicit_result.route is None
    assert default_result.recommended_offer.offer_id == old_result.recommended_offer.offer_id
    assert explicit_result.recommended_offer.offer_id == old_result.recommended_offer.offer_id
    assert live.search_calls == 3
    assert live.stats_calls == 3


def test_one_c_then_provider_safe_history_row_skips_provider_network(tmp_path):
    service, _, _, live = _routed_service(
        tmp_path,
        [_event(name="Клапан M-500", article="", currency=None)],
    )

    result = service.search_row_routed(
        _row("Клапан M-500"),
        source_mode=SourcingSourceMode.ONE_C_THEN_PROVIDER,
        ai_rerank=False,
    )

    assert result.route.final_source_kind == "historical_purchase"
    assert result.route.fallback_called is False
    assert result.route.fallback_status == "not_called"
    assert result.route.history_safe_basis == HistorySafeMatchBasis.EXACT_SOURCE_NAME_UNIT
    assert result.route.history_outcome == "SAFE_MATCH"
    assert result.recommended_offer.provider == "one_c_history"
    assert live.stats_calls == 0
    assert live.search_calls == 0


@pytest.mark.parametrize(
    "requested_period_start",
    ["2026-09-22", "2026-08-30", "2026-06-29", "2026-03-29"],
    ids=["7-days", "1-month", "3-months", "6-months"],
)
def test_historical_routing_has_no_age_cutoff_for_user_selected_periods(
    tmp_path, requested_period_start, monkeypatch
):
    # Vary the snapshot's actual range while keeping the same current purchase
    # event. The selected display period is metadata only and must not change
    # SAFE_MATCH routing or impose a fixed freshness cutoff.
    frozen_today = datetime(2026, 9, 30, tzinfo=timezone.utc)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_today if tz is not None else frozen_today.replace(tzinfo=None)

    monkeypatch.setattr(sourcing_service_module, "datetime", FrozenDateTime)
    purchase_date = "2026-09-29"
    service, repository, _, live = _routed_service(
        tmp_path,
        [
            _event(row=2, when=requested_period_start, price_usable=False, price=None, reported=None, amount=None),
            _event(row=3, when=purchase_date),
        ],
    )
    snapshot = repository.read_catalog_snapshot()
    snapshot_events = [event for item in snapshot.items for event in item.events] if snapshot else []
    actual_period_start = min(event.document_date for event in snapshot_events) if snapshot_events else None

    result = service.search_row_routed(
        _row("Насос тестовый", code="ART-001"),
        source_mode="one_c_then_provider",
        ai_rerank=False,
    )

    assert snapshot is not None
    assert actual_period_start == requested_period_start
    assert result.route.final_source_kind == "historical_purchase"
    expected_age = (frozen_today.date() - datetime.fromisoformat(purchase_date).date()).days
    assert result.route.history_purchase_date == purchase_date
    assert result.route.history_age_days == expected_age
    assert result.route.fallback_called is False
    assert live.stats_calls == 0 and live.search_calls == 0


def test_provider_only_cached_result_cannot_satisfy_one_c_only(tmp_path):
    service, _, _, live = _routed_service(
        tmp_path,
        [_event(name="Клапан M-500", article="", currency=None)],
    )
    row = _row("Клапан M-500")
    live_result = service.search_row(row, ai_rerank=False)

    history_result = service.search_row_routed(row, source_mode="one_c_only", ai_rerank=False)

    assert live_result.recommended_offer.provider == live.key
    assert history_result.recommended_offer.provider == "one_c_history"
    assert history_result.route.final_source_kind == "historical_purchase"
    assert live.search_calls == 1
    assert live.stats_calls == 1


def test_one_c_then_provider_review_falls_back_once_without_mixing_history_candidates(tmp_path):
    service, _, _, live = _routed_service(tmp_path, [_event(name="Кабель", article="")])

    result = service.search_row_routed(
        _row("Кабель"),
        source_mode="one_c_then_provider",
        ai_rerank=False,
    )

    assert result.route.history_outcome == "REVIEW"
    assert result.route.fallback_called is True
    assert result.route.fallback_status == "completed"
    assert result.route.final_source_kind == "provider"
    assert not any(notice.code == "ONE_C_FALLBACK_SUPPRESSED" for notice in result.notices)
    assert any(notice.code == "ONE_C_HISTORY_REVIEW" for notice in result.notices)
    assert all(offer.provider == live.key for offer in result.offers)
    assert live.stats_calls == 1
    assert live.search_calls == 1


def test_one_c_then_provider_completed_empty_search_is_not_a_provider_error(tmp_path):
    live = _CountingLiveProvider(empty=True)
    service, _, _, live = _routed_service(
        tmp_path,
        [_event(name="Кабель", article="")],
        live=live,
    )

    result = service.search_row_routed(
        _row("Кабель"),
        source_mode=SourcingSourceMode.ONE_C_THEN_PROVIDER,
        ai_rerank=False,
    )

    assert result.offers == []
    assert result.recommended_offer is None
    assert result.route.fallback_called is True
    assert result.route.fallback_status == "completed"
    assert result.route.final_source_kind == "provider"
    assert live.stats_calls == 1
    assert live.search_calls == 1


def test_one_c_then_provider_stats_failure_suppresses_live_call_and_history_candidate(tmp_path):
    live = _CountingLiveProvider(stats_fail=True)
    service, _, _, live = _routed_service(
        tmp_path,
        [_event(name="Кабель", article="")],
        live=live,
    )

    result = service.search_row_routed(
        _row("Кабель"),
        source_mode=SourcingSourceMode.ONE_C_THEN_PROVIDER,
        ai_rerank=False,
    )

    assert result.route.history_outcome == "REVIEW"
    assert result.route.fallback_called is False
    assert result.route.fallback_status == "suppressed"
    assert result.route.final_source_kind == "none"
    assert result.provider_call_suppressed is True
    assert result.recommended_offer is None
    assert result.offers == []
    assert result.review_candidate is None
    assert live.stats_calls == 1
    assert any(notice.code == "ONE_C_FALLBACK_SUPPRESSED" for notice in result.notices)
    assert not any(notice.code == "ONE_C_FALLBACK_USED" for notice in result.notices)
    assert live.search_calls == 0


@pytest.mark.parametrize("failure", ["no_match", "unavailable"])
def test_one_c_then_provider_no_match_or_unavailable_falls_back_once(tmp_path, failure):
    service, repository, _, live = _routed_service(tmp_path, [_event()])
    row = _row("Принципиально иной материал")
    if failure == "unavailable":
        repository.database_path.write_bytes(b"not a sqlite snapshot")

    result = service.search_row_routed(
        row,
        source_mode=SourcingSourceMode.ONE_C_THEN_PROVIDER,
        ai_rerank=False,
    )

    assert result.route.fallback_called is True
    assert result.route.history_outcome == ("UNAVAILABLE" if failure == "unavailable" else "NO_MATCH")
    assert live.stats_calls == 1
    assert live.search_calls == 1


def test_one_c_then_provider_failure_does_not_resurrect_review_candidate(tmp_path):
    live = _CountingLiveProvider(fail=True)
    service, _, _, live = _routed_service(tmp_path, [_event(name="Кабель", article="")], live=live)

    result = service.search_row_routed(
        _row("Кабель"),
        source_mode=SourcingSourceMode.ONE_C_THEN_PROVIDER,
        ai_rerank=False,
    )

    assert result.route.history_outcome == "REVIEW"
    assert result.recommended_offer is None
    assert result.offers == []
    assert result.review_candidate is None
    assert result.route.fallback_called is True
    assert result.route.fallback_status == "error"
    assert result.route.final_source_kind == "none"
    assert live.stats_calls == 1
    assert live.search_calls == 1


def test_one_c_only_review_and_unavailable_never_call_live_provider(tmp_path):
    service, repository, _, live = _routed_service(tmp_path, [_event(name="Кабель", article="")])
    review = service.search_row_routed(_row("Кабель"), source_mode="one_c_only", ai_rerank=False)

    assert review.route.final_source_kind == "history_review"
    assert review.route.fallback_called is False
    assert review.route.fallback_status == "not_called"
    assert review.recommended_offer is None
    assert review.offers
    assert any(notice.code == "ONE_C_HISTORY_REVIEW" for notice in review.notices)
    assert live.stats_calls == 0 and live.search_calls == 0

    no_match = service.search_row_routed(
        _row("Совсем другая позиция"),
        source_mode="one_c_only",
        ai_rerank=False,
    )
    assert no_match.route.final_source_kind == "none"
    assert no_match.route.fallback_called is False
    assert no_match.route.fallback_status == "not_called"
    assert any(notice.code == "ONE_C_HISTORY_NOT_FOUND" for notice in no_match.notices)
    assert live.stats_calls == 0 and live.search_calls == 0

    repository.database_path.write_bytes(b"not a sqlite snapshot")
    unavailable = service.search_row_routed(_row("Кабель"), source_mode="one_c_only", ai_rerank=False)
    assert unavailable.route.final_source_kind == "none"
    assert unavailable.route.fallback_called is False
    assert unavailable.route.fallback_status == "not_called"
    assert unavailable.offers == []
    assert any(notice.code == "ONE_C_HISTORY_UNAVAILABLE" for notice in unavailable.notices)
    assert live.stats_calls == 0 and live.search_calls == 0


def test_one_c_only_safe_never_calls_live_provider(tmp_path):
    service, _, _, live = _routed_service(
        tmp_path,
        [_event(name="Клапан M-500", article="", currency=None)],
    )

    result = service.search_row_routed(_row("Клапан M-500"), source_mode="one_c_only", ai_rerank=False)

    assert result.route.final_source_kind == "historical_purchase"
    assert result.route.fallback_status == "not_called"
    assert result.recommended_offer.provider == "one_c_history"
    assert live.stats_calls == 0 and live.search_calls == 0


def test_mixed_project_falls_back_per_row_and_keeps_historical_amount_out_of_live_totals(tmp_path, monkeypatch):
    service, repository, _, live = _routed_service(
        tmp_path,
        [_event(name="Клапан M-500", article="", currency=None)],
    )
    snapshot_reads = 0
    read_snapshot = repository.read_catalog_snapshot

    def counted_snapshot_read():
        nonlocal snapshot_reads
        snapshot_reads += 1
        return read_snapshot()

    monkeypatch.setattr(repository, "read_catalog_snapshot", counted_snapshot_read)
    understand_calls = 0
    real_understand = service._understand_row_result_with_cache

    def counted_understand(row):
        nonlocal understand_calls
        understand_calls += 1
        return real_understand(row)

    monkeypatch.setattr(service, "_understand_row_result_with_cache", counted_understand)
    rows = [
        _row("Клапан M-500", source_id="history", quantity="99"),
        _row("Лампа", source_id="fallback", quantity="2"),
    ]

    result = service.search_project_routed(rows, source_mode="one_c_then_provider")

    assert understand_calls == 2
    assert live.stats_calls == 1
    assert live.search_calls == 1
    assert snapshot_reads == 1
    assert result.positions_history_matched == 1
    assert result.positions_provider_matched == 1
    assert result.positions_fallback_called == 1
    assert result.positions_matched == 2
    assert result.confirmed_totals == {"RUB": Decimal("20")}
    assert result.results[0].recommended_offer.currency == "RUB"
    assert result.results[0].route.final_source_kind == "historical_purchase"
    assert result.results[0].route.routing_policy_revision == "one-c-routing-v2"
    assert result.results[1].route.final_source_kind == "provider"


def test_search_intent_routing_requires_explicit_source_intent(tmp_path):
    service, _, _, _ = _routed_service(tmp_path)

    with pytest.raises(ValueError, match="исходная строка"):
        service.search_intent_routed(_intent(), source_mode="one_c_only")


def test_routed_lookup_observes_active_snapshot_version_after_activation(tmp_path):
    service, repository, _, live = _routed_service(
        tmp_path,
        [_event(name="Клапан M-500", article="", currency=None)],
    )
    first = service.search_row_routed(_row("Клапан M-500"), source_mode="one_c_only", ai_rerank=False)
    first_version = first.route.history_catalog_version
    _activate(
        repository,
        [_event(name="Клапан M-501", article="", currency=None)],
        source_sha="7" * 64,
        fingerprint="8" * 64,
    )

    second = service.search_row_routed(_row("Клапан M-501"), source_mode="one_c_only", ai_rerank=False)

    assert second.route.final_source_kind == "historical_purchase"
    assert second.route.history_catalog_version != first_version
    assert live.stats_calls == 0 and live.search_calls == 0
