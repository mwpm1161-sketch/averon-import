from __future__ import annotations

from decimal import Decimal
import json
import logging
import sqlite3
import socket
from urllib.parse import parse_qs, urlsplit

import pytest

from averon_import.services.secrets import (
    TSTN_CLIENT_KEY,
    TSTN_CREDENTIALS_CONFIGURED,
    TSTN_SERVICE_IDENTIFIER,
    TSTN_X_SP_SECRET,
    MemorySecretStore,
)
from averon_import.services.sourcing.models import ProductIntent
from averon_import.services.sourcing.provider_commercial import (
    CommercialEvidenceState,
    evaluate_provider_commercial_evidence,
)
from averon_import.services.sourcing.provider_commercial_selection import (
    CommercialSelectionReason,
    CommercialSelectionState,
    select_provider_commercial_winner,
)
from averon_import.services.sourcing.provider_matching import ProviderMatchEvaluator
from averon_import.services.sourcing.providers.contracts import (
    ProviderFailureCategory,
    ProviderSearchState,
    ProviderSelection,
)
from averon_import.services.sourcing.providers.execution import (
    ProviderExecutionScope,
    ProviderRequestCounter,
    ProviderRunner,
)
from averon_import.services.sourcing.providers.tstn import (
    TstnApiClient,
    TstnCatalogMirror,
    TstnCatalogRecord,
    TstnCommercialExecutionAdapter,
    TstnHttpResponse,
    TstnItemListResponse,
)


GENERATION = "a" * 32
SERVICE = "tn4a-offline-service-synthetic"
CLIENT_KEY = "tn4a-offline-key-synthetic"
ACCESS_TOKEN = "tn4a-offline-access-token-synthetic"
X_SP_SECRET = "tn4a-offline-xsp-synthetic"
CATALOG_REVISION = "b" * 64
_UNSET = object()


class TrackingMemorySecretStore(MemorySecretStore):
    def __init__(self):
        super().__init__()
        self.read_keys: list[str] = []

    def get(self, key: str) -> str | None:
        self.read_keys.append(key)
        return super().get(key)


class FakeMirror:
    def __init__(
        self,
        records=(),
        *,
        revision: str = CATALOG_REVISION,
        error=None,
        article_records=None,
    ):
        self.records = tuple(records)
        self.article_records = tuple(records if article_records is None else article_records)
        self.revision = revision
        self.error = error
        self.search_calls = 0
        self.last_limit = None
        self.article_queries: list[str] = []
        self.search_index_ready = True

    def search(self, intent=None, *, article=None, limit):
        self.search_calls += 1
        if self.error is not None:
            raise self.error
        if article is not None:
            self.article_queries.append(article)
            return [
                record for record in self.article_records
                if record.article == article
            ][:limit]
        self.last_limit = limit
        return list(self.records[:limit])


CATALOG_SYNC_TOKEN = "tn4a-r2-offline-catalog-synthetic"


class FakeCatalogPageClient:
    def __init__(self, rows):
        self.rows = tuple(rows)
        self.calls = 0

    def list_items(self, *, access_token, limit, offset):
        assert access_token == CATALOG_SYNC_TOKEN
        assert limit is not None and offset == 0
        self.calls += 1
        selected = self.rows[offset : offset + limit]
        return TstnItemListResponse.model_validate({
            "count": len(selected),
            "count_all": len(self.rows),
            "page_num": 1,
            "results": selected,
        })


def _ready_real_mirror(path, rows):
    mirror = TstnCatalogMirror(path)
    client = FakeCatalogPageClient(rows)
    result = mirror.sync(
        client,
        access_token=CATALOG_SYNC_TOKEN,
        page_size=10,
        max_pages=2,
    )
    assert client.calls == 1
    assert result.item_count == len(rows)
    assert mirror.search_index_ready is True
    return mirror


def _catalog_row(*, item_id=101, article="000101", name="Крепёжный элемент"):
    return {
        "id": item_id,
        "article": article,
        "name": name,
        "brand": "ТЕХНОНИКОЛЬ",
        "categories": [{"id": 10, "name": "Крепёж", "parent": 1}],
    }


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = response(request)
        return response


def _response(payload, status=200):
    if isinstance(payload, bytes):
        body = payload
    else:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return TstnHttpResponse(status_code=status, body=body)


def _auth_response():
    return _response({"access_token": ACCESS_TOKEN, "access_date": "2026-10-15"})


def _price_response(
    article: str | None = None,
    price: str | None = None,
    *,
    rows=None,
    city: str | None = None,
):
    if rows is None:
        rows = [] if article is None else [{"article": article, "price": price}]
    payload = {"count": len(rows), "results": rows}
    if city is not None:
        payload["city"] = city
    return _response(payload)


def _availability_response(
    article: str | None = None,
    status: str | None = None,
    *,
    rows=None,
    city: str | None = None,
):
    if rows is None:
        rows = [] if article is None else [{"article": article, "status": status}]
    payload = {"count": len(rows), "results": rows}
    if city is not None:
        payload["city"] = city
    return _response(payload)


def _record(
    *,
    identity_kind="id",
    identity_value="101",
    item_id="101",
    article="000101",
    name="Крепёжный элемент",
    brand="ТЕХНОНИКОЛЬ",
    subbrand="",
    base_unit="шт.",
):
    return TstnCatalogRecord(
        identity_kind=identity_kind,
        identity_value=identity_value,
        id=item_id,
        article=article,
        name=name,
        brand=brand,
        subbrand=subbrand or None,
        base_unit=base_unit,
    )


def _intent(**updates):
    values = {
        "source_row_id": "row-tn4a",
        "source_text": "Крепёжный элемент 000101",
        "normalized_name": "Крепёжный элемент",
        "article": "000101",
        "required_attributes": {},
        "search_queries": ["Крепёжный элемент 000101"],
    }
    values.update(updates)
    return ProductIntent(**values)


def _setup(
    records=(),
    responses=(),
    *,
    mirror=_UNSET,
    enabled=True,
    generation=GENERATION,
    timeout=2.0,
    city_code=None,
    max_candidates=4,
    max_http_attempts=None,
    current=None,
    store=None,
):
    mirror = FakeMirror(records) if mirror is _UNSET else mirror
    store = store if store is not None else TrackingMemorySecretStore()
    store.set(TSTN_SERVICE_IDENTIFIER, SERVICE)
    store.set(TSTN_CLIENT_KEY, CLIENT_KEY)
    store.set(TSTN_X_SP_SECRET, X_SP_SECRET)
    store.set(TSTN_CREDENTIALS_CONFIGURED, generation)
    store.read_keys.clear()
    config = {"enabled": enabled, "generation": generation}
    transport = FakeTransport(*responses)

    def factory(timeout_seconds, *, max_attempts, outbound_attempt_observer):
        return TstnApiClient(
            transport=transport,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            outbound_attempt_observer=outbound_attempt_observer,
        )

    adapter = TstnCommercialExecutionAdapter(
        mirror,
        store,
        enabled=enabled,
        credential_generation=generation,
        request_timeout_s=timeout,
        current_configuration=current or (lambda: (config["enabled"], config["generation"])),
        verified_city_code=city_code,
        max_candidates=max_candidates,
        max_http_attempts=max_http_attempts,
        client_factory=factory,
    )
    return adapter, mirror, store, transport, config


def _execution(adapter, intent=None, *, limit=4, scope_id="tn4a-scope"):
    return ProviderRunner({"tstn": adapter}).run(
        selection=ProviderSelection(provider_keys=("tstn",)),
        intent=intent or _intent(),
        limit=limit,
        scope=ProviderExecutionScope(execution_scope_id=scope_id),
    )


def _query(request):
    return parse_qs(urlsplit(request.full_url).query)


def test_construction_identity_and_local_search_are_passive_and_deterministic():
    adapter, mirror, store, transport, _config = _setup([_record()])
    intent = _intent()

    first = adapter.request_identity(intent, limit=2, execution_scope_id="passive")
    second = adapter.request_identity(intent, limit=2, execution_scope_id="passive")

    assert first == second
    assert first.provider_key == "tstn"
    assert first.affinity.config_revision != GENERATION
    assert GENERATION not in first.affinity.config_revision
    assert mirror.search_calls == 0
    assert store.read_keys == []
    assert transport.requests == []


def test_empty_local_search_returns_empty_without_http_or_credential_reads():
    adapter, mirror, store, transport, _config = _setup([])
    outcome = adapter.execute_search(_intent(), limit=3, request_counter=ProviderRequestCounter())

    assert outcome.state == ProviderSearchState.EMPTY
    assert outcome.request_count == 0
    assert mirror.search_calls == 1
    assert store.read_keys == []
    assert transport.requests == []


def test_unsynced_real_catalog_fails_closed_without_credentials_or_http(tmp_path, monkeypatch):
    mirror = TstnCatalogMirror(tmp_path / "unsynced.sqlite3")
    adapter, _mirror, store, transport, _config = _setup(mirror=mirror)

    def unexpected_search(*args, **kwargs):
        raise AssertionError("unknown catalog revision must be rejected before search")

    monkeypatch.setattr(mirror, "search", unexpected_search)

    outcome = _execution(adapter, limit=3).outcomes[0]

    assert mirror.revision == "unknown"
    assert outcome.state == ProviderSearchState.FAILURE
    assert outcome.failure_category == ProviderFailureCategory.UNAVAILABLE
    assert outcome.request_count == 0
    assert outcome.offers == ()
    assert store.read_keys == []
    assert transport.requests == []


def test_synced_empty_real_catalog_returns_empty_without_credentials_or_http(tmp_path):
    mirror = _ready_real_mirror(tmp_path / "empty.sqlite3", [])
    adapter, _mirror, store, transport, _config = _setup(mirror=mirror)

    outcome = _execution(adapter, limit=3).outcomes[0]

    assert mirror.revision != "unknown"
    assert outcome.state == ProviderSearchState.EMPTY
    assert outcome.request_count == 0
    assert outcome.offers == ()
    assert store.read_keys == []
    assert transport.requests == []


def test_ready_real_catalog_without_matching_rows_returns_empty(tmp_path):
    mirror = _ready_real_mirror(
        tmp_path / "no-match.sqlite3",
        [_catalog_row(item_id=999, article="009999", name="Дренажная мембрана")],
    )
    adapter, _mirror, store, transport, _config = _setup(mirror=mirror)

    outcome = _execution(adapter, limit=3).outcomes[0]

    assert outcome.state == ProviderSearchState.EMPTY
    assert outcome.request_count == 0
    assert outcome.offers == ()
    assert store.read_keys == []
    assert transport.requests == []


def test_corrupt_real_catalog_fails_closed_without_credentials_or_http(tmp_path):
    path = tmp_path / "corrupt.sqlite3"
    _ready_real_mirror(path, [_catalog_row()])
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE tstn_catalog_products SET canonical_payload='{}'",
        )
    mirror = TstnCatalogMirror(path)
    adapter, _mirror, store, transport, _config = _setup(mirror=mirror)

    outcome = _execution(adapter, limit=3).outcomes[0]

    assert outcome.state == ProviderSearchState.FAILURE
    assert outcome.request_count == 0
    assert outcome.offers == ()
    assert store.read_keys == []
    assert transport.requests == []


@pytest.mark.parametrize(
    "mirror",
    [None, FakeMirror(error=RuntimeError("synthetic local mirror corruption"))],
)
def test_missing_or_corrupt_mirror_fails_closed_without_credentials_or_http(mirror):
    adapter, _mirror, store, transport, _config = _setup([_record()], mirror=mirror)
    outcome = adapter.execute_search(_intent(), limit=3, request_counter=ProviderRequestCounter())

    assert outcome.state == ProviderSearchState.FAILURE
    assert outcome.failure_category in {
        ProviderFailureCategory.MISCONFIGURED,
        ProviderFailureCategory.UNAVAILABLE,
    }
    assert outcome.request_count == 0
    assert store.read_keys == []
    assert transport.requests == []


def test_filtered_id_queries_normalize_one_offer_and_count_real_attempts():
    record = _record(article="0000123", item_id="456", identity_value="456")
    adapter, mirror, store, transport, _config = _setup(
        [record],
        [
            _auth_response(),
            _price_response("0000123", "6655.47"),
            _availability_response("0000123", "В наличии"),
        ],
        city_code="city-code-77",
    )
    counter = ProviderRequestCounter()

    outcome = adapter.execute_search(_intent(), limit=5, request_counter=counter)
    offer = outcome.offers[0]

    assert outcome.state == ProviderSearchState.SUCCESS
    assert outcome.request_count == counter.request_count == len(transport.requests) == 3
    assert mirror.last_limit == 4
    assert offer.provider == "tstn"
    assert offer.offer_id == "tstn:id:456"
    assert offer.source_item_id == "456"
    assert offer.article == "0000123"
    assert offer.title == "Крепёжный элемент"
    assert offer.brand == "ТЕХНОНИКОЛЬ"
    assert offer.price == Decimal("6655.47")
    assert type(offer.price) is Decimal
    assert offer.currency == ""
    assert offer.price_unit == ""
    assert offer.availability is None
    assert offer.availability_text == "В наличии"
    assert offer.url == ""

    auth_query = _query(transport.requests[0][0])
    assert auth_query == {"service": [SERVICE], "key": [CLIENT_KEY]}
    price_query = _query(transport.requests[1][0])
    availability_query = _query(transport.requests[2][0])
    for query in (price_query, availability_query):
        assert query["id"] == ["456"]
        assert query["city"] == ["city-code-77"]
        assert query["access_token"] == [ACCESS_TOKEN]
        assert "article" not in query
    assert X_SP_SECRET not in store.read_keys
    assert TSTN_SERVICE_IDENTIFIER in store.read_keys
    assert TSTN_CLIENT_KEY in store.read_keys
    assert store.read_keys.count(TSTN_CREDENTIALS_CONFIGURED) >= 2


@pytest.mark.parametrize("response_city", ["city-code-78", "Region name, unmapped"])
def test_unconfirmed_response_city_withholds_commercial_values(response_city):
    adapter, _mirror, _store, _transport, _config = _setup(
        [_record()],
        [
            _auth_response(),
            _price_response("000101", "99.50", city=response_city),
            _availability_response("000101", "В наличии", city=response_city),
        ],
        city_code="city-code-77",
    )

    outcome = adapter.execute_search(_intent(), limit=1, request_counter=ProviderRequestCounter())
    offer = outcome.offers[0]

    assert outcome.state == ProviderSearchState.PARTIAL_SUCCESS
    assert outcome.failure_category == ProviderFailureCategory.INVALID_RESPONSE
    assert offer.price is None
    assert offer.availability_text == ""
    assert offer.data_provenance["city_filter"] == "city-code-77"
    assert offer.data_provenance["price_region_match"] == "unconfirmed"
    assert offer.data_provenance["availability_region_match"] == "unconfirmed"


def test_missing_response_city_keeps_region_attribution_unconfirmed():
    adapter, _mirror, _store, _transport, _config = _setup(
        [_record()],
        [
            _auth_response(),
            _price_response("000101", "99.50"),
            _availability_response("000101", "В наличии"),
        ],
        city_code="city-code-77",
    )

    outcome = adapter.execute_search(_intent(), limit=1, request_counter=ProviderRequestCounter())
    offer = outcome.offers[0]

    assert outcome.state == ProviderSearchState.SUCCESS
    assert offer.price == Decimal("99.50")
    assert offer.availability_text == "В наличии"
    assert offer.data_provenance["price_region_match"] == "unreported"
    assert offer.data_provenance["availability_region_match"] == "unreported"


def test_exact_response_city_value_is_kept_without_name_aliasing():
    adapter, _mirror, _store, _transport, _config = _setup(
        [_record()],
        [
            _auth_response(),
            _price_response("000101", "99.50", city="city-code-77"),
            _availability_response("000101", "В наличии", city="city-code-77"),
        ],
        city_code="city-code-77",
    )

    outcome = adapter.execute_search(_intent(), limit=1, request_counter=ProviderRequestCounter())
    offer = outcome.offers[0]

    assert outcome.state == ProviderSearchState.SUCCESS
    assert offer.price == Decimal("99.50")
    assert offer.availability_text == "В наличии"
    assert offer.data_provenance["price_region_match"] == "exact_value_match"
    assert offer.data_provenance["availability_region_match"] == "exact_value_match"


def test_article_fallback_preserves_leading_zeroes_and_distinguishes_same_text_as_id():
    records = [
        _record(identity_kind="article", identity_value="00017", item_id=None, article="00017"),
        _record(identity_kind="id", identity_value="00017", item_id="00017", article="00018"),
    ]
    adapter, _mirror, _store, transport, _config = _setup(
        records,
        [
            _auth_response(),
            _price_response("00017", "2.50"),
            _availability_response("00017", "Под заказ"),
            _price_response("00018", "3.75"),
            _availability_response("00018", "Неизвестный статус"),
        ],
    )
    outcome = adapter.execute_search(_intent(), limit=4, request_counter=ProviderRequestCounter())

    assert outcome.state == ProviderSearchState.SUCCESS
    assert [item.offer_id for item in outcome.offers] == ["tstn:article:00017", "tstn:id:00017"]
    assert outcome.offers[0].article == "00017"
    assert _query(transport.requests[1][0])["article"] == ["00017"]
    assert "id" not in _query(transport.requests[1][0])
    assert _query(transport.requests[3][0])["id"] == ["00017"]
    assert outcome.offers[0].availability is None
    assert outcome.offers[0].availability_text == "Под заказ"
    assert outcome.offers[1].availability is None
    assert outcome.offers[1].availability_text == "Неизвестный статус"


def test_same_article_on_distinct_ids_never_shares_an_ambiguous_price():
    records = [
        _record(identity_kind="id", identity_value="11", item_id="11", article="00077"),
        _record(identity_kind="id", identity_value="12", item_id="12", article="00077"),
    ]
    adapter, _mirror, _store, transport, _config = _setup(
        records,
        [
            _auth_response(),
            _price_response("00077", "10.00"),
            _availability_response("00077", "В наличии"),
            _price_response("00077", "10.00"),
            _availability_response("00077", "В наличии"),
        ],
    )
    outcome = adapter.execute_search(_intent(), limit=4, request_counter=ProviderRequestCounter())

    assert outcome.state == ProviderSearchState.PARTIAL_SUCCESS
    assert outcome.failure_category == ProviderFailureCategory.INVALID_RESPONSE
    assert len(outcome.offers) == 2
    assert len({item.offer_id for item in outcome.offers}) == 2
    assert all(item.price is None for item in outcome.offers)
    assert all(item.availability_text == "" for item in outcome.offers)
    assert outcome.offers[0].data_provenance["availability_field"] == ""
    assert len(transport.requests) == outcome.request_count == 5


def test_duplicate_article_outside_bounded_candidate_list_remains_unpriced_and_unavailable():
    candidate = _record(identity_kind="id", identity_value="11", item_id="11", article="00077")
    outside_candidate = _record(
        identity_kind="id", identity_value="12", item_id="12", article="00077",
    )
    mirror = FakeMirror([candidate], article_records=[candidate, outside_candidate])
    adapter, _mirror, _store, transport, _config = _setup(
        [candidate],
        [
            _auth_response(),
            _price_response("00077", "10.00"),
            _availability_response("00077", "В наличии"),
        ],
        mirror=mirror,
    )

    outcome = adapter.execute_search(_intent(), limit=1, request_counter=ProviderRequestCounter())

    assert outcome.state == ProviderSearchState.PARTIAL_SUCCESS
    assert outcome.failure_category == ProviderFailureCategory.INVALID_RESPONSE
    assert len(outcome.offers) == 1
    assert outcome.offers[0].price is None
    assert outcome.offers[0].availability_text == ""
    assert outcome.offers[0].data_provenance["price_field"] == ""
    assert outcome.offers[0].data_provenance["availability_field"] == ""
    assert mirror.article_queries == ["00077"]
    assert len(transport.requests) == 3


@pytest.mark.parametrize(
    "price_rows",
    [
        [{"article": "000101", "price": "1.25"}, {"article": "000101", "price": "2.50"}],
        [{"article": "000101", "price": "1.25"}, {"article": "000101", "price": None}],
    ],
)
def test_conflicting_price_records_are_never_arbitrarily_selected(price_rows):
    adapter, _mirror, _store, _transport, _config = _setup(
        [_record()],
        [
            _auth_response(),
            _price_response(rows=price_rows),
            _availability_response("000101", "В наличии"),
        ],
    )
    outcome = adapter.execute_search(_intent(), limit=1, request_counter=ProviderRequestCounter())

    assert outcome.state == ProviderSearchState.PARTIAL_SUCCESS
    assert outcome.failure_category == ProviderFailureCategory.INVALID_RESPONSE
    assert len(outcome.offers) == 1
    assert outcome.offers[0].price is None


def test_mismatched_extra_rows_are_not_assigned_to_candidate():
    adapter, _mirror, _store, _transport, _config = _setup(
        [_record()],
        [
            _auth_response(),
            _price_response("000999", "1.00"),
            _availability_response("000999", "В наличии"),
        ],
    )
    outcome = adapter.execute_search(_intent(), limit=1, request_counter=ProviderRequestCounter())

    assert outcome.state == ProviderSearchState.PARTIAL_SUCCESS
    assert outcome.failure_category == ProviderFailureCategory.INVALID_RESPONSE
    assert outcome.offers[0].price is None
    assert outcome.offers[0].availability_text == ""
    assert outcome.offers[0].data_provenance["price_field"] == ""


def test_mixed_price_rows_discard_matching_price_and_keep_other_clean_fields():
    adapter, _mirror, _store, _transport, _config = _setup(
        [_record()],
        [
            _auth_response(),
            _price_response(rows=[
                {"article": "000101", "price": "10.00"},
                {"article": "000999", "price": "90.00"},
            ]),
            _availability_response("000101", "В наличии"),
        ],
    )

    outcome = adapter.execute_search(_intent(), limit=1, request_counter=ProviderRequestCounter())

    assert outcome.state == ProviderSearchState.PARTIAL_SUCCESS
    assert outcome.failure_category == ProviderFailureCategory.INVALID_RESPONSE
    assert outcome.offers[0].price is None
    assert outcome.offers[0].availability_text == "В наличии"


def test_mixed_availability_rows_discard_matching_status_and_keep_clean_price():
    adapter, _mirror, _store, _transport, _config = _setup(
        [_record()],
        [
            _auth_response(),
            _price_response("000101", "10.00"),
            _availability_response(rows=[
                {"article": "000101", "status": "В наличии"},
                {"article": "000999", "status": "Под заказ"},
            ]),
        ],
    )

    outcome = adapter.execute_search(_intent(), limit=1, request_counter=ProviderRequestCounter())

    assert outcome.state == ProviderSearchState.PARTIAL_SUCCESS
    assert outcome.failure_category == ProviderFailureCategory.INVALID_RESPONSE
    assert outcome.offers[0].price == Decimal("10.00")
    assert outcome.offers[0].availability_text == ""


def test_consistent_duplicate_rows_remain_usable_and_empty_responses_are_successful():
    adapter, _mirror, _store, _transport, _config = _setup(
        [_record()],
        [
            _auth_response(),
            _price_response(rows=[
                {"article": "000101", "price": "10.00"},
                {"article": "000101", "price": "10.00"},
            ]),
            _availability_response(rows=[
                {"article": "000101", "status": "В наличии"},
                {"article": "000101", "status": "В наличии"},
            ]),
        ],
    )

    duplicate_outcome = adapter.execute_search(
        _intent(), limit=1, request_counter=ProviderRequestCounter(),
    )
    assert duplicate_outcome.state == ProviderSearchState.SUCCESS
    assert duplicate_outcome.offers[0].price == Decimal("10.00")
    assert duplicate_outcome.offers[0].availability_text == "В наличии"

    empty_adapter, *_ = _setup(
        [_record()],
        [_auth_response(), _price_response(), _availability_response()],
    )
    empty_outcome = empty_adapter.execute_search(
        _intent(), limit=1, request_counter=ProviderRequestCounter(),
    )
    assert empty_outcome.state == ProviderSearchState.SUCCESS
    assert empty_outcome.offers[0].price is None
    assert empty_outcome.offers[0].availability_text == ""


def test_invalid_decimal_is_sanitized_and_yields_partial_unpriced_offer():
    adapter, _mirror, _store, _transport, _config = _setup(
        [_record()],
        [
            _auth_response(),
            _price_response(rows=[{"article": "000101", "price": "NaN"}]),
            _availability_response("000101", "Под заказ"),
        ],
    )
    outcome = adapter.execute_search(_intent(), limit=1, request_counter=ProviderRequestCounter())

    assert outcome.state == ProviderSearchState.PARTIAL_SUCCESS
    assert outcome.failure_category == ProviderFailureCategory.INVALID_RESPONSE
    assert outcome.offers[0].price is None
    assert "NaN" not in repr(outcome)


def test_transport_error_details_never_enter_outcome_or_logs(caplog):
    adapter, _mirror, _store, transport, _config = _setup(
        [_record()],
        [
            _auth_response(),
            RuntimeError(
                f"synthetic failed URL access_token={ACCESS_TOKEN}&service={SERVICE}&key={CLIENT_KEY}"
            ),
            _availability_response("000101", "Под заказ"),
        ],
    )
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        outcome = adapter.execute_search(
            _intent(), limit=1, request_counter=ProviderRequestCounter(),
        )

    rendered = repr(outcome) + " " + caplog.text
    assert outcome.state == ProviderSearchState.PARTIAL_SUCCESS
    assert outcome.failure_category == ProviderFailureCategory.TRANSPORT
    for secret in (ACCESS_TOKEN, SERVICE, CLIENT_KEY):
        assert secret not in rendered
    assert len(transport.requests) == 3


@pytest.mark.parametrize("status", ["В наличии", "Под заказ", "Нестандартный статус"])
def test_status_text_is_preserved_without_inventing_boolean_availability(status):
    adapter, _mirror, _store, _transport, _config = _setup(
        [_record()],
        [_auth_response(), _price_response(), _availability_response("000101", status)],
    )
    outcome = adapter.execute_search(_intent(), limit=1, request_counter=ProviderRequestCounter())

    assert outcome.state == ProviderSearchState.SUCCESS
    assert outcome.offers[0].availability is None
    assert outcome.offers[0].availability_text == status


def test_response_city_is_unbound_when_no_city_filter_was_requested():
    adapter, _mirror, _store, _transport, _config = _setup(
        [_record()],
        [
            _auth_response(),
            _price_response("000101", "5.00", city="Supplier default city"),
            _availability_response("000101", "В наличии", city="Supplier default city"),
        ],
    )

    outcome = adapter.execute_search(_intent(), limit=1, request_counter=ProviderRequestCounter())

    assert outcome.state == ProviderSearchState.SUCCESS
    assert outcome.offers[0].price == Decimal("5.00")
    assert outcome.offers[0].availability_text == "В наличии"
    assert outcome.offers[0].data_provenance["city_filter"] == ""
    assert outcome.offers[0].data_provenance["price_region_match"] == "not_requested"
    assert outcome.offers[0].data_provenance["availability_region_match"] == "not_requested"


def test_unknown_price_basis_stays_unproven_and_cannot_select_a_commercial_winner():
    adapter, _mirror, _store, _transport, _config = _setup(
        [_record()],
        [_auth_response(), _price_response("000101", "125.50"), _availability_response("000101", "В наличии")],
    )
    execution = _execution(adapter, limit=1)
    offer = execution.offers[0]
    match = ProviderMatchEvaluator().evaluate(_intent(), execution)
    commercial = evaluate_provider_commercial_evidence(match)
    selection = select_provider_commercial_winner(commercial)

    assert offer.price == Decimal("125.50")
    assert offer.currency == ""
    assert offer.price_unit == ""
    assert commercial.evidence[0].evidence_state == CommercialEvidenceState.INCOMPLETE
    assert selection.state == CommercialSelectionState.NO_SAFE_WINNER
    assert CommercialSelectionReason.COMMERCIAL_EVIDENCE_INCOMPLETE in selection.reason_codes


def test_authentication_failure_returns_no_offers_and_counts_attempt():
    adapter, _mirror, store, transport, _config = _setup(
        [_record()],
        [_response({"message": "credentials refused; " + SERVICE}, status=401)],
    )
    counter = ProviderRequestCounter()
    outcome = adapter.execute_search(_intent(), limit=1, request_counter=counter)

    assert outcome.state == ProviderSearchState.FAILURE
    assert outcome.failure_category == ProviderFailureCategory.AUTHENTICATION
    assert outcome.offers == ()
    assert outcome.request_count == counter.request_count == len(transport.requests) == 1
    assert SERVICE not in repr(outcome) and CLIENT_KEY not in repr(outcome)
    assert ACCESS_TOKEN not in repr(outcome)
    assert TSTN_X_SP_SECRET not in store.read_keys


def test_commercial_error_is_partial_and_not_misreported_as_empty():
    adapter, _mirror, _store, transport, _config = _setup(
        [_record()],
        [_auth_response(), socket.timeout("synthetic timeout detail"), _availability_response("000101", "Под заказ")],
    )
    counter = ProviderRequestCounter()
    outcome = adapter.execute_search(_intent(), limit=1, request_counter=counter)

    assert outcome.state == ProviderSearchState.PARTIAL_SUCCESS
    assert outcome.failure_category == ProviderFailureCategory.TIMEOUT
    assert len(outcome.offers) == 1
    assert outcome.offers[0].price is None
    assert outcome.offers[0].availability_text == "Под заказ"
    assert outcome.request_count == counter.request_count == len(transport.requests) == 3


def test_generation_rotation_after_authorization_stops_before_commercial_http():
    adapter, _mirror, store, transport, config = _setup(
        [_record()],
        [_auth_response(), _price_response("000101", "1.00"), _availability_response("000101", "В наличии")],
    )
    calls = [0]

    def current():
        calls[0] += 1
        if calls[0] >= 3:
            return True, "c" * 32
        return config["enabled"], config["generation"]

    adapter.current_configuration = current
    outcome = adapter.execute_search(_intent(), limit=1, request_counter=ProviderRequestCounter())

    assert calls[0] == 3
    assert outcome.state == ProviderSearchState.FAILURE
    assert outcome.failure_category == ProviderFailureCategory.MISCONFIGURED
    assert outcome.offers == ()
    assert outcome.request_count == len(transport.requests) == 1
    assert len(transport.requests) == 1
    assert store.get(TSTN_CREDENTIALS_CONFIGURED) == GENERATION


def test_generation_rotation_between_price_and_availability_stops_token_use():
    adapter, _mirror, store, transport, config = _setup(
        [_record()],
        [
            _auth_response(),
            lambda _request: (
                store.set(TSTN_CREDENTIALS_CONFIGURED, "d" * 32)
                or _price_response("000101", "1.00")
            ),
            _availability_response("000101", "В наличии"),
        ],
    )
    counter = ProviderRequestCounter()
    outcome = adapter.execute_search(_intent(), limit=1, request_counter=counter)

    assert outcome.state == ProviderSearchState.FAILURE
    assert outcome.failure_category == ProviderFailureCategory.MISCONFIGURED
    assert outcome.offers == ()
    assert counter.request_count == outcome.request_count == len(transport.requests) == 2
    assert store.get(TSTN_CREDENTIALS_CONFIGURED) == "d" * 32


def test_request_budget_is_shared_with_authorization_and_stops_new_transport_calls():
    records = [
        _record(identity_kind="id", identity_value="11", item_id="11", article="00011"),
        _record(identity_kind="id", identity_value="12", item_id="12", article="00012"),
    ]
    adapter, _mirror, _store, transport, _config = _setup(
        records,
        [
            _auth_response(),
            _price_response("00011", "1.00"),
            _availability_response("00011", "В наличии"),
            _price_response("00012", "2.00"),
        ],
        max_http_attempts=3,
    )
    counter = ProviderRequestCounter()
    outcome = adapter.execute_search(_intent(), limit=2, request_counter=counter)

    assert outcome.state == ProviderSearchState.PARTIAL_SUCCESS
    assert outcome.failure_category == ProviderFailureCategory.RATE_LIMITED
    assert outcome.request_count == counter.request_count == len(transport.requests) == 3
    assert len(outcome.offers) == 2
    assert outcome.offers[1].price is None


def test_runner_dedupe_binds_intent_limit_catalog_generation_city_and_policy():
    record = _record()
    adapter, mirror, store, transport, config = _setup(
        [record],
        [_auth_response(), _price_response("000101", "1.00"), _availability_response("000101", "В наличии")],
    )
    intent = _intent()
    first_identity = adapter.request_identity(intent, limit=1, execution_scope_id="dedupe")
    same_identity = adapter.request_identity(intent, limit=1, execution_scope_id="dedupe")
    changed_intent = adapter.request_identity(
        _intent(article="000102"), limit=1, execution_scope_id="dedupe",
    )
    changed_limit = adapter.request_identity(intent, limit=2, execution_scope_id="dedupe")
    assert first_identity == same_identity
    assert first_identity.request_fingerprint != changed_intent.request_fingerprint
    assert first_identity.request_fingerprint != changed_limit.request_fingerprint
    assert store.read_keys == []

    runner = ProviderRunner({"tstn": adapter})
    selection = ProviderSelection(provider_keys=("tstn",))
    scope = ProviderExecutionScope(execution_scope_id="dedupe")
    first = runner.run(selection=selection, intent=intent, limit=1, scope=scope)
    repeated = runner.run(selection=selection, intent=intent, limit=1, scope=scope)
    assert first.outcomes[0].request_count == 3
    assert repeated.outcomes[0].request_count == 0
    assert repeated.reused_provider_keys == ("tstn",)
    assert len(transport.requests) == 3

    mirror.revision = "e" * 64
    changed_catalog = adapter.request_identity(intent, limit=1, execution_scope_id="dedupe")
    assert changed_catalog.request_fingerprint != first_identity.request_fingerprint
    new_config = {"enabled": True, "generation": "f" * 32}
    changed_generation, _m, _s, _t, _c = _setup(
        [record], current=lambda: (True, new_config["generation"]), generation=new_config["generation"],
    )
    assert changed_generation.request_identity(
        intent, limit=1, execution_scope_id="dedupe",
    ).request_fingerprint != first_identity.request_fingerprint
    changed_city, *_ = _setup([record], city_code="verified-region-2")
    assert changed_city.request_identity(
        intent, limit=1, execution_scope_id="dedupe",
    ).request_fingerprint != first_identity.request_fingerprint
    assert transport.requests and all(ACCESS_TOKEN not in repr(item.data_provenance) for item in first.offers)


def test_runner_returns_current_tstn_cache_hit_without_search_or_supplier_http(tmp_path, monkeypatch):
    mirror = _ready_real_mirror(tmp_path / "cached.sqlite3", [_catalog_row()])
    adapter, _mirror, store, transport, _config = _setup(
        mirror=mirror,
        responses=[
            _auth_response(),
            _price_response("000101", "12.50"),
            _availability_response("000101", "В наличии"),
        ],
    )
    runner = ProviderRunner({"tstn": adapter})
    selection = ProviderSelection(provider_keys=("tstn",))
    scope = ProviderExecutionScope(execution_scope_id="cache-valid")
    intent = _intent()
    first = runner.run(selection=selection, intent=intent, limit=1, scope=scope)
    assert first.outcomes[0].request_count == 3
    assert len(transport.requests) == 3

    def unexpected_search(*args, **kwargs):
        raise AssertionError("cache validation must not search SQLite")

    def unexpected_integrity_scan(*args, **kwargs):
        raise AssertionError("cache validation must not run an integrity scan")

    monkeypatch.setattr(mirror, "search", unexpected_search)
    monkeypatch.setattr(mirror, "check_integrity", unexpected_integrity_scan)
    store.read_keys.clear()
    repeated = runner.run(selection=selection, intent=intent, limit=1, scope=scope)

    assert repeated.outcomes[0].state == ProviderSearchState.SUCCESS
    assert repeated.outcomes[0].request_count == 0
    assert repeated.outcomes[0].offers == first.outcomes[0].offers
    assert repeated.reused_provider_keys == ("tstn",)
    assert len(transport.requests) == 3
    assert set(store.read_keys) <= {TSTN_CREDENTIALS_CONFIGURED}


def test_runner_rejects_tstn_cache_when_catalog_revision_changes_after_identity(tmp_path):
    mirror = _ready_real_mirror(tmp_path / "cache-revision.sqlite3", [_catalog_row()])
    adapter, _mirror, store, transport, _config = _setup(
        mirror=mirror,
        responses=[
            _auth_response(),
            _price_response("000101", "12.50"),
            _availability_response("000101", "В наличии"),
        ],
    )
    runner = ProviderRunner({"tstn": adapter})
    selection = ProviderSelection(provider_keys=("tstn",))
    scope = ProviderExecutionScope(execution_scope_id="cache-revision")
    intent = _intent()
    first = runner.run(selection=selection, intent=intent, limit=1, scope=scope)
    assert first.outcomes[0].state == ProviderSearchState.SUCCESS
    original_identity = adapter.request_identity

    def identity_then_replace_catalog(*args, **kwargs):
        identity = original_identity(*args, **kwargs)
        mirror.sync(
            FakeCatalogPageClient([_catalog_row(item_id=102, article="000102", name="Другая позиция")]),
            access_token=CATALOG_SYNC_TOKEN,
            page_size=10,
            max_pages=2,
        )
        return identity

    adapter.request_identity = identity_then_replace_catalog
    store.read_keys.clear()
    repeated = runner.run(selection=selection, intent=intent, limit=1, scope=scope)

    assert repeated.outcomes[0].state == ProviderSearchState.FAILURE
    assert repeated.outcomes[0].failure_category == ProviderFailureCategory.UNAVAILABLE
    assert repeated.outcomes[0].request_count == 0
    assert repeated.outcomes[0].offers == ()
    assert repeated.reused_provider_keys == ()
    assert len(transport.requests) == 3
    assert store.read_keys == []


@pytest.mark.parametrize("changed_value", ["generation", "configured_marker"])
def test_runner_rejects_tstn_cache_when_credential_state_changes(
    tmp_path,
    changed_value,
):
    mirror = _ready_real_mirror(
        tmp_path / f"cache-credentials-{changed_value}.sqlite3",
        [_catalog_row()],
    )
    adapter, _mirror, store, transport, config = _setup(
        mirror=mirror,
        responses=[
            _auth_response(),
            _price_response("000101", "12.50"),
            _availability_response("000101", "В наличии"),
        ],
    )
    runner = ProviderRunner({"tstn": adapter})
    selection = ProviderSelection(provider_keys=("tstn",))
    scope = ProviderExecutionScope(execution_scope_id=f"cache-{changed_value}")
    intent = _intent()
    first = runner.run(selection=selection, intent=intent, limit=1, scope=scope)
    assert first.outcomes[0].state == ProviderSearchState.SUCCESS
    if changed_value == "generation":
        config["generation"] = "c" * 32
    else:
        store.set(TSTN_CREDENTIALS_CONFIGURED, "c" * 32)
    store.read_keys.clear()

    repeated = runner.run(selection=selection, intent=intent, limit=1, scope=scope)

    assert repeated.outcomes[0].state == ProviderSearchState.FAILURE
    assert repeated.outcomes[0].failure_category == ProviderFailureCategory.UNAVAILABLE
    assert repeated.outcomes[0].request_count == 0
    assert repeated.outcomes[0].offers == ()
    assert repeated.reused_provider_keys == ()
    assert len(transport.requests) == 3
    assert set(store.read_keys) <= {TSTN_CREDENTIALS_CONFIGURED}


def test_cached_identity_validation_errors_do_not_expose_secret_material(tmp_path, caplog):
    mirror = _ready_real_mirror(tmp_path / "cache-safe-error.sqlite3", [_catalog_row()])
    adapter, _mirror, _store, transport, _config = _setup(
        mirror=mirror,
        responses=[
            _auth_response(),
            _price_response("000101", "12.50"),
            _availability_response("000101", "В наличии"),
        ],
    )
    runner = ProviderRunner({"tstn": adapter})
    selection = ProviderSelection(provider_keys=("tstn",))
    scope = ProviderExecutionScope(execution_scope_id="cache-safe-error")
    intent = _intent()
    runner.run(selection=selection, intent=intent, limit=1, scope=scope)

    def invalid_cache_state(_identity):
        raise RuntimeError(f"synthetic failure contains {ACCESS_TOKEN}")

    adapter.is_cached_identity_current = invalid_cache_state
    repeated = runner.run(selection=selection, intent=intent, limit=1, scope=scope)

    assert repeated.outcomes[0].state == ProviderSearchState.FAILURE
    assert repeated.outcomes[0].request_count == 0
    assert repeated.outcomes[0].offers == ()
    assert repeated.reused_provider_keys == ()
    assert ACCESS_TOKEN not in repr(repeated)
    assert ACCESS_TOKEN not in caplog.text
    assert len(transport.requests) == 3


def test_provider_runner_rejects_catalog_revision_changed_after_request_identity():
    adapter, mirror, store, transport, _config = _setup(
        [_record()],
        [_auth_response(), _price_response("000101", "1.00"), _availability_response("000101", "В наличии")],
    )
    original_identity = adapter.request_identity

    def identity_then_switch(intent, *, limit, execution_scope_id):
        identity = original_identity(
            intent,
            limit=limit,
            execution_scope_id=execution_scope_id,
        )
        mirror.revision = "e" * 64
        return identity

    adapter.request_identity = identity_then_switch
    outcome = _execution(adapter, limit=1).outcomes[0]

    assert outcome.state == ProviderSearchState.FAILURE
    assert outcome.failure_category == ProviderFailureCategory.UNAVAILABLE
    assert outcome.offers == ()
    assert outcome.request_count == 0
    assert outcome.catalog_version is None
    assert mirror.search_calls == 0
    assert store.read_keys == []
    assert transport.requests == []


def test_catalog_revision_change_during_commercial_request_discards_partial_offer():
    holder = {}

    def change_revision_after_price(_request):
        holder["mirror"].revision = "e" * 64
        return _price_response("000101", "1.00")

    adapter, mirror, store, transport, _config = _setup(
        [_record()],
        [
            _auth_response(),
            change_revision_after_price,
            _availability_response("000101", "В наличии"),
        ],
    )
    holder["mirror"] = mirror

    outcome = _execution(adapter, limit=1).outcomes[0]

    assert outcome.state == ProviderSearchState.FAILURE
    assert outcome.failure_category == ProviderFailureCategory.UNAVAILABLE
    assert outcome.offers == ()
    assert outcome.request_count == 2
    assert outcome.catalog_version is None
    assert len(transport.requests) == 2
    assert store.read_keys.count(TSTN_CREDENTIALS_CONFIGURED) >= 2


def test_catalog_revision_change_during_duplicate_article_check_stops_before_auth():
    record = _record()
    mirror = FakeMirror([record])
    original_search = mirror.search

    def changing_article_search(intent=None, *, article=None, limit):
        matches = original_search(intent, article=article, limit=limit)
        if article is not None:
            mirror.revision = "e" * 64
        return matches

    mirror.search = changing_article_search
    adapter, _mirror, store, transport, _config = _setup(
        [record],
        [_auth_response(), _price_response("000101", "1.00"), _availability_response("000101", "В наличии")],
        mirror=mirror,
    )

    outcome = _execution(adapter, limit=1).outcomes[0]

    assert outcome.state == ProviderSearchState.FAILURE
    assert outcome.failure_category == ProviderFailureCategory.UNAVAILABLE
    assert outcome.offers == ()
    assert outcome.request_count == 0
    assert outcome.catalog_version is None
    assert store.read_keys == []
    assert transport.requests == []


def test_secret_material_never_enters_offer_provenance_or_outcome_errors():
    adapter, _mirror, _store, transport, _config = _setup(
        [_record()],
        [
            _auth_response(),
            _price_response("000101", "1.00"),
            _availability_response("000101", "В наличии"),
        ],
    )
    outcome = _execution(adapter, limit=1).outcomes[0]
    rendered = json.dumps(outcome.model_dump(mode="json"), ensure_ascii=False)

    for secret in (SERVICE, CLIENT_KEY, X_SP_SECRET, ACCESS_TOKEN):
        assert secret not in rendered
    assert "access_token" not in rendered.casefold()
    assert "service" not in outcome.offers[0].data_provenance
    assert "key" not in outcome.offers[0].data_provenance
    assert "x_sp_secret" not in outcome.offers[0].data_provenance
    assert len(transport.requests) == 3
