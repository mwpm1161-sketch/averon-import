from __future__ import annotations

from decimal import Decimal
import io
import json
import socket
import traceback
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

import pytest

from averon_import.services.sourcing.providers.vseinstrumenti.client import (
    VSEINSTRUMENTI_API_BASE_URLS,
    VSEINSTRUMENTI_MAX_RESPONSE_BYTES,
    VseinstrumentiApiError,
    VseinstrumentiClient,
    VseinstrumentiErrorCategory,
    VseinstrumentiRateLimiter,
    _NoRedirectHandler,
    _validate_search_endpoint,
)
from averon_import.services.sourcing.providers.vseinstrumenti.models import (
    VseinstrumentiResponseError,
    parse_product_search_result,
)
from averon_import.services.sourcing.providers.execution import ProviderRequestCounter


REGION = "0c5b2444-70a0-4932-980c-b4dc0d3f02b5"
TOKEN = "VI_SECRET_SENTINEL_123"
SEARCH = "Аккумуляторная дрель Durofix 60V RK60132-PM"


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self.body = body
        self.status = status
        self.read_limit = None
        self.closed = False

    def read(self, limit: int) -> bytes:
        self.read_limit = limit
        return self.body[:limit]

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def __call__(self, request: urllib.request.Request, timeout: float):
        self.requests.append(request)
        self.timeouts.append(timeout)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _product(**updates):
    product = {
        "sku": "0015555760",
        "name": "РемоКолор Ножницы по металлу",
        "productCode": "19-6-401",
        "brandName": "РемоКолор",
        "technicalSpecifications": {"name": "Длина", "value": "19", "unit": "см", "description": ""},
        "prices": {"price": "3800.1250", "basePrice": 4000},
        "unit": "шт",
        "stock": {"atWarehouse": "11000"},
        "deliveryDates": {"pickup": "19.12.2024", "courier": "21.12.2024"},
        "siteUrl": "https://www.vseinstrumenti.ru/view_goods_by_id_redirect.php?id=1401524",
    }
    product.update(updates)
    return product


def _payload(*products, search=SEARCH, region_id=REGION):
    return {"result": {"search": search, "regionId": region_id, "products": list(products)}}


def _response(payload, status=200):
    if isinstance(payload, bytes):
        body = payload
    else:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return FakeResponse(body, status=status)


def _client(transport, **kwargs):
    return VseinstrumentiClient(TOKEN, transport=transport, **kwargs)


def test_search_builds_documented_one_page_request_with_bearer_header_only():
    response = _response(_payload(_product()))
    transport = FakeTransport(response)
    client = _client(transport, environment="test")

    result = client.search_products(SEARCH, region_id=REGION, limit=20, sort="desc", order_by="popularity")

    request = transport.requests[0]
    parts = urlsplit(request.full_url)
    query = parse_qs(parts.query)
    assert parts.scheme == "https"
    assert parts.netloc == "api.vseinstrumenti.ru"
    assert parts.path == "/open-api/dev/v1/products"
    assert query == {
        "search": [SEARCH], "regionId": [REGION], "limit": ["20"], "offset": ["0"],
        "sort": ["desc"], "orderBy": ["popularity"],
    }
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert TOKEN not in request.full_url
    assert transport.timeouts == [10.0]
    assert response.read_limit == VSEINSTRUMENTI_MAX_RESPONSE_BYTES + 1
    assert response.closed
    assert result.region_id == REGION
    assert len(result.products) == 1


def test_outbound_observer_runs_once_immediately_before_transport():
    events = []
    response = _response(_payload(_product()))

    class OrderedObserver:
        def record_outbound_attempt(self):
            events.append("attempt")

    class OrderedTransport:
        def __call__(self, request, timeout):
            events.append("transport")
            return response

    client = VseinstrumentiClient(TOKEN, transport=OrderedTransport())
    result = client.search_products(
        SEARCH,
        region_id=REGION,
        outbound_attempt_observer=OrderedObserver(),
    )

    assert result.products
    assert events == ["attempt", "transport"]


def test_outbound_observer_is_not_called_for_local_request_refusal():
    counter = ProviderRequestCounter()
    transport = FakeTransport(_response(_payload(_product())))

    with pytest.raises(VseinstrumentiApiError) as caught:
        _client(transport).search_products(
            SEARCH,
            region_id=REGION,
            limit=41,
            outbound_attempt_observer=counter,
        )

    assert caught.value.category == VseinstrumentiErrorCategory.MISCONFIGURED
    assert counter.request_count == 0
    assert transport.requests == []


@pytest.mark.parametrize("environment,base", [
    ("prod", "https://api.vseinstrumenti.ru/open-api"),
    ("test", "https://api.vseinstrumenti.ru/open-api/dev"),
])
def test_environment_selects_only_documented_endpoint(environment, base):
    assert VSEINSTRUMENTI_API_BASE_URLS[environment] == base
    _validate_search_endpoint(base + "/v1/products", environment)


@pytest.mark.parametrize("url", [
    "http://api.vseinstrumenti.ru/open-api/v1/products",
    "https://evil.example/open-api/v1/products",
    "https://api.vseinstrumenti.ru/open-api/other/v1/products",
    "https://api.vseinstrumenti.ru/open-api/v1/products?token=secret",
    "https://user:password@api.vseinstrumenti.ru/open-api/v1/products",
    "https://api.vseinstrumenti.ru:444/open-api/v1/products",
])
def test_search_endpoint_rejects_noncanonical_host_scheme_or_path(url):
    with pytest.raises(VseinstrumentiApiError) as caught:
        _validate_search_endpoint(url, "prod")
    assert caught.value.category == VseinstrumentiErrorCategory.MISCONFIGURED
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("updates", [
    {"search": ""}, {"search": "   "}, {"region_id": "Moscow"},
    {"limit": 0}, {"limit": 41}, {"limit": True}, {"sort": "random"},
    {"order_by": "relevance"},
])
def test_invalid_request_parameters_fail_before_transport(updates):
    transport = FakeTransport(_response(_payload()))
    kwargs = {"search": SEARCH, "region_id": REGION, "limit": 20}
    kwargs.update(updates)
    with pytest.raises(VseinstrumentiApiError) as caught:
        _client(transport).search_products(**kwargs)
    assert caught.value.category == VseinstrumentiErrorCategory.MISCONFIGURED
    assert transport.requests == []


def test_client_creation_is_offline_and_shared_limiter_survives_client_rebuilds():
    first = VseinstrumentiClient(TOKEN)
    second = VseinstrumentiClient(TOKEN)
    assert first.rate_limiter is second.rate_limiter
    assert "VI_SECRET_SENTINEL_123" not in repr(first)


def test_timeout_is_bounded_and_configuration_errors_do_not_call_transport():
    transport = FakeTransport(_response(_payload()))
    with pytest.raises(VseinstrumentiApiError):
        _client(transport, timeout_seconds=30.1)
    with pytest.raises(VseinstrumentiApiError):
        _client(transport, timeout_seconds=float("inf"))
    assert transport.requests == []


def test_fake_clock_and_sleeper_enforce_sliding_window_and_single_slot():
    now = [0.0]
    sleeps = []

    def sleeper(delay):
        sleeps.append(delay)
        now[0] += delay

    limiter = VseinstrumentiRateLimiter(max_requests=2, clock=lambda: now[0], sleeper=sleeper)
    starts = []
    for _ in range(4):
        with limiter.request_slot():
            starts.append(now[0])
    assert starts[:2] == [0.0, 0.0]
    assert starts[2:] == pytest.approx([60.0, 60.0])
    assert sleeps[0] > 60.0


def test_limiter_rejects_configuration_above_documented_rpm():
    with pytest.raises(ValueError):
        VseinstrumentiRateLimiter(max_requests=101)
    with pytest.raises(ValueError):
        VseinstrumentiRateLimiter(max_requests=1, window_seconds=0.1)
    with pytest.raises(ValueError):
        VseinstrumentiRateLimiter(max_requests=66, window_seconds=40)


def test_limiter_rejects_66_requests_per_40_seconds_counterexample():
    now = [0.0]
    sleeps = []

    def sleeper(delay):
        sleeps.append(delay)
        now[0] += delay

    with pytest.raises(ValueError, match="60-second window"):
        VseinstrumentiRateLimiter(
            max_requests=66,
            window_seconds=40,
            clock=lambda: now[0],
            sleeper=sleeper,
        )
    assert now == [0.0]
    assert sleeps == []

    limiter = VseinstrumentiRateLimiter(
        max_requests=66,
        clock=lambda: now[0],
        sleeper=sleeper,
    )
    starts = []
    for _ in range(66):
        with limiter.request_slot():
            starts.append(now[0])
    now[0] = 40.0
    with limiter.request_slot():
        starts.append(now[0])

    assert starts[:66] == [0.0] * 66
    assert starts[-1] > 60.0
    assert sleeps[0] > 20.0


def test_response_parser_accepts_exact_requested_and_global_page_limits():
    limited_products = [dict(_product(), sku=f"limit-{index}") for index in range(5)]
    requested_boundary = parse_product_search_result(
        _payload(*limited_products),
        expected_search=SEARCH,
        expected_region_id=REGION,
        requested_limit=5,
    )
    global_boundary = parse_product_search_result(
        _payload(*(dict(_product(), sku=f"global-{index}") for index in range(40))),
        expected_search=SEARCH,
        expected_region_id=REGION,
        requested_limit=40,
    )

    assert len(requested_boundary.products) == 5
    assert len(global_boundary.products) == 40


def test_response_parser_rejects_raw_entries_over_requested_limit_before_normalization():
    transport = FakeTransport(_response(_payload(*[_product() for _ in range(6)])))

    with pytest.raises(VseinstrumentiApiError) as caught:
        _client(transport).search_products(SEARCH, region_id=REGION, limit=5)

    assert caught.value.category == VseinstrumentiErrorCategory.INVALID_RESPONSE
    assert transport.requests


def test_response_parser_rejects_more_than_global_maximum_raw_product_entries():
    payload = _payload(*(dict(_product(), sku=f"overflow-{index}") for index in range(41)))

    with pytest.raises(VseinstrumentiResponseError, match="requested page limit"):
        parse_product_search_result(
            payload,
            expected_search=SEARCH,
            expected_region_id=REGION,
            requested_limit=40,
        )


@pytest.mark.parametrize("status,category", [
    (400, VseinstrumentiErrorCategory.INVALID_REQUEST),
    (401, VseinstrumentiErrorCategory.AUTHENTICATION),
    (403, VseinstrumentiErrorCategory.AUTHENTICATION),
    (404, VseinstrumentiErrorCategory.INVALID_REQUEST),
    (429, VseinstrumentiErrorCategory.RATE_LIMITED),
    (500, VseinstrumentiErrorCategory.UNAVAILABLE),
    (503, VseinstrumentiErrorCategory.UNAVAILABLE),
    (302, VseinstrumentiErrorCategory.INVALID_RESPONSE),
])
def test_http_statuses_are_normalized_without_returning_upstream_body(status, category):
    response = _response({"message": f"private response includes {TOKEN}"}, status=status)
    transport = FakeTransport(response)
    with pytest.raises(VseinstrumentiApiError) as caught:
        _client(transport).search_products(SEARCH, region_id=REGION)
    error = caught.value
    assert error.category == category
    assert error.status_code == status
    assert TOKEN not in str(error)
    assert TOKEN not in repr(error)
    assert TOKEN not in "".join(traceback.format_exception(error))
    assert len(transport.requests) == 1
    assert response.closed


def test_vi_documented_code_500_token_error_is_auth_but_other_500_is_unavailable():
    auth_response = _response({"code": 500, "message": "failed to parse token: private"})
    with pytest.raises(VseinstrumentiApiError) as auth:
        _client(FakeTransport(auth_response)).search_products(SEARCH, region_id=REGION)
    assert auth.value.category == VseinstrumentiErrorCategory.AUTHENTICATION
    assert auth.value.status_code == 500

    unavailable_response = _response({"code": 500, "message": "internal failure"})
    with pytest.raises(VseinstrumentiApiError) as unavailable:
        _client(FakeTransport(unavailable_response)).search_products(SEARCH, region_id=REGION)
    assert unavailable.value.category == VseinstrumentiErrorCategory.UNAVAILABLE

    error_envelope = _response({
        "error": 500,
        "details": {"message": "Нет доступа к ресурсам компании"},
    })
    with pytest.raises(VseinstrumentiApiError) as access:
        _client(FakeTransport(error_envelope)).search_products(SEARCH, region_id=REGION)
    assert access.value.category == VseinstrumentiErrorCategory.AUTHENTICATION


def test_http_error_without_redirect_followup_is_normalized_once():
    body = io.BytesIO(b"redirect response")
    error = urllib.error.HTTPError(
        "https://api.vseinstrumenti.ru/open-api/v1/products", 302, "private", {}, body,
    )
    transport = FakeTransport(error)
    with pytest.raises(VseinstrumentiApiError) as caught:
        _client(transport).search_products(SEARCH, region_id=REGION)
    assert caught.value.category == VseinstrumentiErrorCategory.INVALID_RESPONSE
    assert len(transport.requests) == 1


def test_nonstandard_json_nan_is_rejected():
    body = (
        '{"result":{"search":"' + SEARCH + '","regionId":"' + REGION + '",'
        '"products":[{"sku":"1","name":"Tool","prices":{"price":NaN}}]}}'
    ).encode("utf-8")
    with pytest.raises(VseinstrumentiApiError) as caught:
        _client(FakeTransport(FakeResponse(body))).search_products(SEARCH, region_id=REGION)
    assert caught.value.category == VseinstrumentiErrorCategory.INVALID_RESPONSE


def test_redirect_handler_refuses_target_and_never_replays_authorization():
    request = urllib.request.Request(
        "https://api.vseinstrumenti.ru/open-api/v1/products",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    handler = _NoRedirectHandler()
    assert handler.redirect_request(
        request, None, 302, "Found", {}, "https://evil.example/collect",
    ) is None
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"


@pytest.mark.parametrize("error", [
    socket.timeout(f"timeout includes {TOKEN}"),
    urllib.error.URLError(f"transport includes {TOKEN}"),
    RuntimeError(f"failure includes Bearer {TOKEN}"),
])
def test_transport_failures_hide_exception_text_and_never_retry(error):
    transport = FakeTransport(error)
    with pytest.raises(VseinstrumentiApiError) as caught:
        _client(transport).search_products(SEARCH, region_id=REGION)
    expected = VseinstrumentiErrorCategory.TIMEOUT if isinstance(error, socket.timeout) else VseinstrumentiErrorCategory.TRANSPORT
    assert caught.value.category == expected
    assert caught.value.__cause__ is None
    assert TOKEN not in str(caught.value)
    assert TOKEN not in repr(caught.value)
    assert TOKEN not in "".join(traceback.format_exception(caught.value))
    assert len(transport.requests) == 1


def test_oversized_response_is_bounded_and_rejected():
    response = FakeResponse(b" " * (VSEINSTRUMENTI_MAX_RESPONSE_BYTES + 1))
    transport = FakeTransport(response)
    with pytest.raises(VseinstrumentiApiError) as caught:
        _client(transport).search_products(SEARCH, region_id=REGION)
    assert caught.value.category == VseinstrumentiErrorCategory.INVALID_RESPONSE
    assert response.read_limit == VSEINSTRUMENTI_MAX_RESPONSE_BYTES + 1
    assert response.closed


def test_prices_preserve_exact_decimal_for_json_numbers_and_documented_strings():
    raw = (
        '{"result":{"search":"' + SEARCH + '","regionId":"' + REGION + '","products":[{'
        '"sku":"1","name":"Tool","prices":{"price":123456789012345.67890123456789,'
        '"basePrice":"4000.0050"}}]}}'
    ).encode("utf-8")
    result = _client(FakeTransport(FakeResponse(raw))).search_products(SEARCH, region_id=REGION)
    product = result.products[0]
    assert product.price == Decimal("123456789012345.67890123456789")
    assert type(product.price) is Decimal
    assert product.base_price == Decimal("4000.0050")
    assert product.unit == ""
    assert product.site_url == ""


def test_documented_product_fields_are_normalized_without_inventing_commercial_facts():
    product = _client(FakeTransport(_response(_payload(_product())))).search_products(
        SEARCH, region_id=REGION,
    ).products[0]
    assert product.sku == "0015555760"
    assert product.product_code == "19-6-401"
    assert product.brand_name == "РемоКолор"
    assert product.technical_specifications[0].value == "19"
    assert product.price == Decimal("3800.1250")
    assert product.base_price == Decimal("4000")
    assert product.unit == "шт"
    assert product.stock_at_warehouse == Decimal("11000")
    assert product.pickup_date == "19.12.2024"
    assert product.courier_date == "21.12.2024"
    assert product.site_url.endswith("?id=1401524")
    assert not hasattr(product, "currency")
    assert not hasattr(product, "vat_basis")
    assert not hasattr(product, "availability")


def test_absent_optional_commercial_stock_delivery_and_unit_stay_unknown():
    product = _client(FakeTransport(_response(_payload({"sku": "42", "name": "Tool"})))).search_products(
        SEARCH, region_id=REGION,
    ).products[0]
    assert product.price is None
    assert product.base_price is None
    assert product.unit == ""
    assert product.stock_at_warehouse is None
    assert product.pickup_date == ""
    assert product.courier_date == ""
    assert product.technical_specifications == ()
    assert product.site_url == ""


def test_null_optional_fields_are_preserved_as_unknown():
    product = _product(
        productCode=None, brandName=None, technicalSpecifications=None,
        prices={"price": None, "basePrice": None}, unit=None,
        stock={"atWarehouse": None}, deliveryDates={"pickup": None, "courier": None},
        siteUrl=None,
    )
    parsed = _client(FakeTransport(_response(_payload(product)))).search_products(
        SEARCH, region_id=REGION,
    ).products[0]
    assert parsed.product_code == parsed.brand_name == parsed.unit == ""
    assert parsed.price is parsed.base_price is parsed.stock_at_warehouse is None
    assert parsed.pickup_date == parsed.courier_date == parsed.site_url == ""


@pytest.mark.parametrize("products", [[], {}])
def test_empty_documented_product_representations_are_empty(products):
    result = _client(FakeTransport(_response({"result": {
        "search": SEARCH, "regionId": REGION, "products": products,
    }}))).search_products(SEARCH, region_id=REGION)
    assert result.products == ()


def test_identical_duplicate_sku_is_collapsed_but_conflicting_product_is_rejected():
    repeated = _product()
    result = _client(FakeTransport(_response(_payload(repeated, dict(repeated))))).search_products(
        SEARCH, region_id=REGION,
    )
    assert len(result.products) == 1

    conflicting = dict(repeated, name="A different product")
    transport = FakeTransport(_response(_payload(repeated, conflicting)))
    with pytest.raises(VseinstrumentiApiError) as caught:
        _client(transport).search_products(SEARCH, region_id=REGION)
    assert caught.value.category == VseinstrumentiErrorCategory.INVALID_RESPONSE


@pytest.mark.parametrize("payload", [
    [], {"result": []}, {"result": {"search": SEARCH, "regionId": REGION}},
    {"result": {"search": SEARCH, "regionId": REGION, "products": {"x": {"sku": "x"}}}},
    {"result": {"search": SEARCH, "regionId": REGION, "products": "empty"}},
    {"result": {"search": SEARCH, "regionId": "different", "products": []}},
    {"result": {"search": "different", "regionId": REGION, "products": []}},
    _payload({"sku": "42", "name": 123}),
    _payload({"sku": 42, "name": "Tool"}),
    _payload({"sku": "42", "name": "Tool", "prices": {"price": True}}),
    _payload({"sku": "42", "name": "Tool", "technicalSpecifications": ["unknown shape"]}),
    _payload({"sku": "42", "name": "Tool", "stock": {"atWarehouse": -1}}),
    _payload({"sku": "42", "name": "Tool", "siteUrl": "javascript:alert(1)"}),
])
def test_malformed_or_contradictory_response_fails_closed(payload):
    transport = FakeTransport(_response(payload))
    with pytest.raises(VseinstrumentiApiError) as caught:
        _client(transport).search_products(SEARCH, region_id=REGION)
    assert caught.value.category == VseinstrumentiErrorCategory.INVALID_RESPONSE
    assert len(transport.requests) == 1


def test_duplicate_json_object_keys_are_rejected():
    body = (
        '{"result":{"search":"' + SEARCH + '","regionId":"' + REGION + '",'
        '"products":[],"products":[]}}'
    ).encode("utf-8")
    with pytest.raises(VseinstrumentiApiError) as caught:
        _client(FakeTransport(FakeResponse(body))).search_products(SEARCH, region_id=REGION)
    assert caught.value.category == VseinstrumentiErrorCategory.INVALID_RESPONSE


def test_response_parser_rejects_conflicting_duplicate_skus_even_when_one_field_is_unmapped():
    first = _product()
    second = dict(first, barcode="conflicting-unmapped-field")
    with pytest.raises(VseinstrumentiResponseError, match="conflicting duplicate SKUs"):
        parse_product_search_result(_payload(first, second), expected_search=SEARCH, expected_region_id=REGION)


def test_supplied_clock_and_limiter_are_used_without_startup_requests():
    now = [12.0]
    sleeps = []

    def sleeper(delay):
        sleeps.append(delay)
        now[0] += delay

    limiter = VseinstrumentiRateLimiter(max_requests=1, clock=lambda: now[0], sleeper=sleeper)
    transport = FakeTransport(_response(_payload()), _response(_payload()))
    client = VseinstrumentiClient(TOKEN, transport=transport, rate_limiter=limiter)
    assert transport.requests == []
    client.search_products(SEARCH, region_id=REGION)
    client.search_products(SEARCH, region_id=REGION)
    assert len(sleeps) == 1
    assert sleeps[0] > 60.0
    assert len(transport.requests) == 2


def test_clock_and_sleeper_can_be_injected_directly_on_client():
    now = [0.0]
    client = VseinstrumentiClient(
        TOKEN,
        transport=FakeTransport(_response(_payload())),
        clock=lambda: now[0],
        sleeper=lambda delay: now.__setitem__(0, now[0] + delay),
    )
    assert client.rate_limiter is not VseinstrumentiClient(TOKEN).rate_limiter
