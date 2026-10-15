from __future__ import annotations

from decimal import Decimal
import json
import logging
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
    TstnCatalogRecord,
    TstnCommercialExecutionAdapter,
    TstnHttpResponse,
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


def _price_response(article: str | None = None, price: str | None = None, *, rows=None):
    if rows is None:
        rows = [] if article is None else [{"article": article, "price": price}]
    return _response({"count": len(rows), "results": rows})


def _availability_response(article: str | None = None, status: str | None = None, *, rows=None):
    if rows is None:
        rows = [] if article is None else [{"article": article, "status": status}]
    return _response({"count": len(rows), "results": rows})


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
