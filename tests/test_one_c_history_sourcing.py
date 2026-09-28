from __future__ import annotations

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
from averon_import.services.sourcing.models import MatchDecision, ProductIntent
from averon_import.services.sourcing.providers.one_c_history import (
    HistoryMatchOutcome,
    OneCHistoryProvider,
    normalize_product_search_text,
)


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

    result = provider.lookup(_intent(article="", model="Насос тестовый"))

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert result.selected_offer is None
    assert any(match.decision == MatchDecision.LIKELY_MATCH for match in result.match_results)


def test_name_only_exact_support_is_never_safe(tmp_path):
    _, provider = _provider(tmp_path, [_event(article="")])

    result = provider.lookup(_intent(article=""))

    assert result.outcome == HistoryMatchOutcome.REVIEW
    assert result.selected_offer is None


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


def test_historical_offer_never_claims_live_availability_or_url_and_does_not_invent_currency(tmp_path):
    _, provider = _provider(tmp_path, [_event(currency=None)])

    offer = provider.search(_intent())[0]

    assert offer.availability is None
    assert "неизвестна" in offer.availability_text.casefold()
    assert offer.url == ""
    assert offer.currency == ""


def test_source_currency_is_preserved_and_price_fields_remain_decimal(tmp_path):
    _, provider = _provider(tmp_path, [_event(currency="usd")])

    offer = provider.search(_intent())[0]

    assert offer.currency == "USD"
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
    assert "one_c_history" not in source
