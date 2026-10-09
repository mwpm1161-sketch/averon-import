from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import socket
import urllib.request
from urllib.parse import parse_qs, urlsplit

import pytest

from averon_import.services.manual_tenders.durable_projection import _offer as durable_offer_payload
from averon_import.services.manual_tenders.durable_read import DurableOffer
from averon_import.services.sourcing.models import Offer, ProductIntent
from averon_import.services.sourcing.provider_commercial import (
    CommercialEvidenceState,
    VatBasis,
    evaluate_provider_commercial_evidence,
    resolve_commercial_evidence,
)
from averon_import.services.sourcing.provider_commercial_selection import (
    CommercialSelectionReason,
    CommercialSelectionState,
    select_provider_commercial_winner,
)
from averon_import.services.sourcing.provider_matching import ProviderMatchEvaluator
from averon_import.services.sourcing.providers.contracts import (
    ProviderAffinity,
    ProviderFailureCategory,
    ProviderOfferReference,
    ProviderSearchOutcome,
    ProviderSearchRequestIdentity,
    ProviderSearchState,
    ProviderSelection,
    index_offers_by_provider_identity,
)
from averon_import.services.sourcing.providers.execution import (
    ProviderExecutionScope,
    ProviderRequestCounter,
    ProviderRunner,
)
from averon_import.services.sourcing.providers.vseinstrumenti.adapter import (
    VseinstrumentiExecutionAdapter,
    _safe_product_url,
)
from averon_import.services.sourcing.providers.vseinstrumenti.client import (
    VseinstrumentiApiError,
    VseinstrumentiClient,
    VseinstrumentiErrorCategory,
    VseinstrumentiRateLimiter,
)


REGION = "0c5b2444-70a0-4932-980c-b4dc0d3f02b5"
TOKEN = "VI_ADAPTER_TEST_TOKEN_SECRET"


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self.body = body
        self.status = status
        self.closed = False

    def read(self, limit: int) -> bytes:
        return self.body[:limit]

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, timeout: float):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _product(**updates):
    value = {
        "sku": "VI-001",
        "name": "Инструмент модель M-100",
        "productCode": "ABC-100",
        "brandName": "Марка",
        "technicalSpecifications": {
            "name": "Напряжение",
            "value": "220",
            "unit": "В",
            "description": "",
        },
        "prices": {"price": "12.3400", "basePrice": "14.00"},
        "unit": "шт.",
        "stock": {"atWarehouse": "4.5"},
        "deliveryDates": {"pickup": "19.12.2026", "courier": "21.12.2026"},
        "siteUrl": "https://www.vseinstrumenti.ru/catalog/product/vi-001",
    }
    value.update(updates)
    return value


def _payload(*products, search="ABC-100", region_id=REGION):
    return {"result": {"search": search, "regionId": region_id, "products": list(products)}}


def _response(payload, status=200):
    if isinstance(payload, bytes):
        body = payload
    else:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return FakeResponse(body, status)


def _client(transport, *, environment="prod", timeout_seconds=10.0):
    now = [0.0]

    def sleeper(delay):
        now[0] += delay

    limiter = VseinstrumentiRateLimiter(clock=lambda: now[0], sleeper=sleeper)
    return VseinstrumentiClient(
        TOKEN,
        environment=environment,
        timeout_seconds=timeout_seconds,
        transport=transport,
        rate_limiter=limiter,
    )


def _adapter(*responses, environment="prod", region_id=REGION, timeout_seconds=10.0):
    transport = FakeTransport(*responses)
    adapter = VseinstrumentiExecutionAdapter(
        _client(transport, environment=environment, timeout_seconds=timeout_seconds),
        region_id=region_id,
    )
    return adapter, transport


def _intent(**updates):
    values = {
        "source_row_id": "row-1",
        "source_text": "Инструмент модель M-100",
        "normalized_name": "Инструмент",
        "manufacturer": "",
        "brand": "",
        "model": "M-100",
        "article": "ABC-100",
        "attributes": {},
        "required_attributes": {},
        "preferred_attributes": {},
        "quantity": "1",
        "unit": "шт.",
        "search_queries": ["Инструмент M-100", "второй запрос"],
    }
    values.update(updates)
    return ProductIntent(**values)


@pytest.mark.parametrize(
    "fields,expected_query",
    [
        ({"article": "A-1", "search_queries": ["prepared"], "model": "M-1", "normalized_name": "Name", "source_text": "Source"}, "A-1"),
        ({"article": "", "search_queries": ["prepared", "ignored"], "model": "M-1", "normalized_name": "Name", "source_text": "Source"}, "prepared"),
        ({"article": "", "search_queries": [], "model": "M-1", "normalized_name": "Name", "source_text": "Source"}, "M-1"),
        ({"article": "", "search_queries": [], "model": "", "normalized_name": "Name", "source_text": "Source"}, "Name"),
        ({"article": "", "search_queries": [], "model": "", "normalized_name": "", "source_text": "Source"}, "Source"),
    ],
)
def test_product_intent_uses_one_query_with_explicit_priority(fields, expected_query):
    adapter, transport = _adapter(_response(_payload(search=expected_query)))
    intent = _intent(**fields)

    outcome = adapter.execute_search(intent, limit=4, request_counter=ProviderRequestCounter())

    assert outcome.state == ProviderSearchState.EMPTY
    assert len(transport.requests) == 1
    query = parse_qs(urlsplit(transport.requests[0].full_url).query)
    assert query["search"] == [expected_query]


def test_request_identity_fingerprints_actual_plan_limit_and_safe_affinity_without_io():
    transport = FakeTransport()
    adapter = VseinstrumentiExecutionAdapter(_client(transport), region_id=REGION)
    intent = _intent(article="", search_queries=["query one"])

    first = adapter.request_identity(intent, limit=5, execution_scope_id="scope-a")
    same = adapter.request_identity(intent, limit=5, execution_scope_id="scope-a")
    changed_query = adapter.request_identity(
        intent.model_copy(update={"search_queries": ["query two"]}),
        limit=5,
        execution_scope_id="scope-a",
    )
    changed_limit = adapter.request_identity(intent, limit=6, execution_scope_id="scope-a")
    changed_region = VseinstrumentiExecutionAdapter(_client(FakeTransport()), region_id="11111111-1111-1111-1111-111111111111").request_identity(
        intent, limit=5, execution_scope_id="scope-a",
    )
    changed_environment = VseinstrumentiExecutionAdapter(
        _client(FakeTransport(), environment="test"), region_id=REGION,
    ).request_identity(intent, limit=5, execution_scope_id="scope-a")
    changed_configuration = VseinstrumentiExecutionAdapter(
        _client(FakeTransport(), timeout_seconds=5.0), region_id=REGION,
    ).request_identity(intent, limit=5, execution_scope_id="scope-a")

    assert first == same
    assert first.request_fingerprint != changed_query.request_fingerprint
    assert first.request_fingerprint != changed_limit.request_fingerprint
    assert first.request_fingerprint != changed_region.request_fingerprint
    assert first.request_fingerprint != changed_environment.request_fingerprint
    assert first.request_fingerprint != changed_configuration.request_fingerprint
    assert first.affinity.environment == "prod"
    assert first.affinity.region_id == REGION
    assert len(first.affinity.config_revision) == 64
    assert first.affinity.config_revision != changed_configuration.affinity.config_revision
    assert TOKEN not in json.dumps(first.model_dump(mode="json"))
    assert transport.requests == []


def test_runner_executes_vi_outcome_and_request_local_dedup_does_not_double_count():
    adapter, transport = _adapter(_response(_payload(_product())))
    runner = ProviderRunner({"vseinstrumenti": adapter})
    selection = ProviderSelection(provider_keys=("vseinstrumenti",))
    scope = ProviderExecutionScope(execution_scope_id="scope-dedup")

    first = runner.run(selection=selection, intent=_intent(), limit=5, scope=scope)
    second_intent = _intent(source_row_id="row-2", source_text="Other source wording")
    second = runner.run(selection=selection, intent=second_intent, limit=5, scope=scope)

    assert first.outcomes[0].state == ProviderSearchState.SUCCESS
    assert first.outcomes[0].request_count == 1
    assert second.outcomes[0].request_count == 0
    assert second.reused_provider_keys == ("vseinstrumenti",)
    assert len(transport.requests) == 1


def test_runner_limit_over_vi_page_cap_uses_only_one_maximum_page():
    adapter, transport = _adapter(_response(_payload(_product())))

    outcome = adapter.execute_search(_intent(), limit=100, request_counter=ProviderRequestCounter())

    query = parse_qs(urlsplit(transport.requests[0].full_url).query)
    assert query["limit"] == ["40"]
    assert query["offset"] == ["0"]
    assert outcome.request_count == 1
    assert len(transport.requests) == 1
    assert len(outcome.offers) == 1


def test_offer_mapping_preserves_decimal_unknown_commercial_facts_and_only_supported_stock_facts():
    adapter, _transport = _adapter(_response(_payload(_product())))
    outcome = adapter.execute_search(_intent(), limit=5, request_counter=ProviderRequestCounter())
    offer = outcome.offers[0]

    assert outcome.state == ProviderSearchState.SUCCESS
    assert outcome.request_count == 1
    assert offer.provider == "vseinstrumenti"
    assert offer.offer_id == offer.source_item_id == "VI-001"
    assert offer.title == "Инструмент модель M-100"
    assert offer.article == "ABC-100"
    assert offer.brand == "Марка"
    assert offer.manufacturer == ""
    assert offer.price == Decimal("12.3400")
    assert type(offer.price) is Decimal
    assert offer.currency == ""
    assert offer.price_unit == "шт."
    assert offer.availability is None
    assert offer.attributes["voltage"] == Decimal("220")
    assert offer.attributes["supplier_stock_at_warehouse"] == Decimal("4.5")
    assert offer.attributes["supplier_pickup_date"] == "19.12.2026"
    assert offer.attributes["supplier_courier_date"] == "21.12.2026"
    assert offer.url == "https://www.vseinstrumenti.ru/catalog/product/vi-001"
    assert offer.data_provenance["source"] == "vseinstrumenti"
    assert "raw" not in offer.data_provenance
    assert TOKEN not in repr(offer)


def test_unknown_price_unit_is_explicitly_empty_and_generic_commercial_evidence_stays_unproven():
    product = _product(unit="", prices={"price": "0.01"}, stock=None, deliveryDates=None)
    adapter, _transport = _adapter(_response(_payload(product)))
    outcome = adapter.execute_search(_intent(), limit=5, request_counter=ProviderRequestCounter())
    offer = outcome.offers[0]

    evidence = resolve_commercial_evidence(offer, outcome)

    assert offer.price_unit == ""
    assert "price_unit" in offer.model_fields_set
    assert evidence.amount == Decimal("0.01")
    assert evidence.currency == ""
    assert evidence.vat_basis == VatBasis.UNKNOWN
    assert evidence.evidence_state == CommercialEvidenceState.INCOMPLETE


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.vseinstrumenti.ru/catalog/product/1", "https://www.vseinstrumenti.ru/catalog/product/1"),
        ("https://www.vseinstrumenti.ru/catalog/product/1?id=1", ""),
        ("https://www.vseinstrumenti.ru/view_goods_by_id_redirect.php", ""),
        ("https://redirect.vseinstrumenti.ru/catalog/product/1", ""),
        ("https://www.vseinstrumenti.ru/path/token%3Dsecret", ""),
    ],
)
def test_site_url_is_retained_only_without_rewriting_and_when_durable_safe(url, expected):
    assert _safe_product_url(url) == expected
    if expected:
        adapter, _transport = _adapter(_response(_payload(_product(siteUrl=url))))
        offer = adapter.execute_search(_intent(), limit=5, request_counter=ProviderRequestCounter()).offers[0]
        durable = DurableOffer.model_validate(durable_offer_payload(offer))
        assert durable.url == url


def test_empty_product_list_is_empty_with_one_attempt_and_no_partial_success():
    adapter, transport = _adapter(_response(_payload()))

    outcome = adapter.execute_search(_intent(), limit=5, request_counter=ProviderRequestCounter())

    assert outcome.state == ProviderSearchState.EMPTY
    assert outcome.offers == ()
    assert outcome.request_count == 1
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "response,expected_category",
    [
        (_response({"message": TOKEN}, status=401), ProviderFailureCategory.AUTHENTICATION),
        (_response({"message": "throttled"}, status=429), ProviderFailureCategory.RATE_LIMITED),
        (socket.timeout("private timeout detail"), ProviderFailureCategory.TIMEOUT),
        (_response(b"not-json"), ProviderFailureCategory.INVALID_RESPONSE),
    ],
)
def test_errors_are_normalized_without_becoming_empty_or_leaking_secrets(response, expected_category):
    adapter, transport = _adapter(response)
    outcome = adapter.execute_search(_intent(), limit=5, request_counter=ProviderRequestCounter())

    assert outcome.state == ProviderSearchState.FAILURE
    assert outcome.failure_category == expected_category
    assert outcome.offers == ()
    assert outcome.request_count == 1
    assert len(transport.requests) == 1
    assert TOKEN not in repr(outcome)


def test_missing_query_is_local_misconfiguration_with_zero_outbound_attempts():
    adapter, transport = _adapter(_response(_payload()))
    intent = _intent(
        source_text="",
        normalized_name="",
        article="",
        model="",
        search_queries=[],
    )
    counter = ProviderRequestCounter()

    outcome = adapter.execute_search(intent, limit=5, request_counter=counter)

    assert outcome.state == ProviderSearchState.FAILURE
    assert outcome.failure_category == ProviderFailureCategory.MISCONFIGURED
    assert outcome.request_count == 0
    assert counter.request_count == 0
    assert transport.requests == []


def test_offer_identity_is_composite_provider_and_local_offer_id():
    adapter, _transport = _adapter(_response(_payload(_product())))
    vi_offer = adapter.execute_search(_intent(), limit=5, request_counter=ProviderRequestCounter()).offers[0]
    other_offer = Offer(
        offer_id=vi_offer.offer_id,
        provider="etm_ipro",
        source_item_id="same-source-id",
        title="Other source product",
        price=Decimal("2.00"),
        price_unit="шт.",
    )

    references = index_offers_by_provider_identity((vi_offer, other_offer))

    assert len(references) == 2
    assert ProviderOfferReference.from_offer(vi_offer) != ProviderOfferReference.from_offer(other_offer)


class StaticEtmAdapter:
    key = "etm_ipro"

    def __init__(self, offer):
        self.offer = offer
        self.affinity = ProviderAffinity(
            environment="test",
            config_revision="fixture-config",
            adapter_revision="fixture-adapter",
        )

    def request_identity(self, intent, *, limit, execution_scope_id):
        fingerprint = hashlib.sha256(f"fixture:{intent.article}:{limit}".encode()).hexdigest()
        return ProviderSearchRequestIdentity(
            execution_scope_id=execution_scope_id,
            provider_key=self.key,
            request_fingerprint=fingerprint,
            affinity=self.affinity,
            limit=limit,
        )

    def execute_search(self, intent, *, limit, request_counter):
        return ProviderSearchOutcome(
            provider_key=self.key,
            state=ProviderSearchState.SUCCESS,
            offers=(self.offer,),
            request_count=request_counter.request_count,
            affinity=self.affinity,
            catalog_version="fixture-catalog",
        )


def test_cheaper_vi_offer_cannot_win_against_proven_etm_through_m1d_m2a_m2b():
    intent = _intent(
        normalized_name="",
        model="",
        search_queries=["ABC-100"],
        article="ABC-100",
    )
    etm_offer = Offer(
        offer_id="ABC-100",
        provider="etm_ipro",
        source_item_id="ABC-100",
        title="Электроинструмент",
        article="ABC-100",
        price=Decimal("100.00"),
        currency="RUB",
        price_unit="шт.",
        data_provenance={
            "source": "etm_ipro",
            "source_item_id": "ABC-100",
            "price_field": "pricewnds",
            "price_status": "",
            "catalog_version": "fixture-catalog",
        },
    )
    vi_adapter, vi_transport = _adapter(_response(_payload(
        _product(prices={"price": "1.00"}),
    )))
    runner = ProviderRunner({
        "etm_ipro": StaticEtmAdapter(etm_offer),
        "vseinstrumenti": vi_adapter,
    })
    execution = runner.run(
        selection=ProviderSelection(provider_keys=("etm_ipro", "vseinstrumenti")),
        intent=intent,
        limit=5,
        scope=ProviderExecutionScope(execution_scope_id="scope-commercial"),
    )
    matching = ProviderMatchEvaluator().evaluate(intent, execution)
    commercial = evaluate_provider_commercial_evidence(matching)
    selection = select_provider_commercial_winner(commercial)

    vi_offer = next(offer for offer in execution.offers if offer.provider == "vseinstrumenti")
    vi_evidence = next(item for item in commercial.evidence if item.offer_reference == ProviderOfferReference.from_offer(vi_offer))
    assert vi_offer.price == Decimal("1.00")
    assert vi_evidence.evidence_state == CommercialEvidenceState.INCOMPLETE
    assert vi_evidence.vat_basis == VatBasis.UNKNOWN
    assert selection.state == CommercialSelectionState.NO_SAFE_WINNER
    assert CommercialSelectionReason.COMMERCIAL_EVIDENCE_INCOMPLETE in selection.reason_codes
    assert selection.selected_reference is None
    assert len(vi_transport.requests) == 1


def test_vi_durable_projection_uses_existing_generic_unproven_provenance():
    adapter, _transport = _adapter(_response(_payload(_product())))
    offer = adapter.execute_search(_intent(), limit=5, request_counter=ProviderRequestCounter()).offers[0]

    durable = DurableOffer.model_validate(durable_offer_payload(offer))

    assert durable.provenance.source == "unproven"
    assert durable.offer_reference.provider_key == "vseinstrumenti"
    assert durable.offer_reference.offer_id == "VI-001"
