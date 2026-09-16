from __future__ import annotations

import json
import urllib.error
from urllib.parse import parse_qs

import pytest

from averon_import.services.app_settings import LemanaB2BSettings
from averon_import.services.sourcing.providers.base import SourcingProviderError
from averon_import.services.sourcing.providers.lemana_b2b import (
    LEMANA_API_URLS,
    LEMANA_AUTH_URL,
    LEMANA_PRICE_BATCH_PATH,
    LEMANA_PRODUCTS_PATH,
    LemanaB2BClient,
    parse_price_payload,
    parse_products_payload,
)


class FakeResponse:
    def __init__(self, status: int, payload: object = None, raw: bytes | None = None):
        self.status = status
        self._raw = raw if raw is not None else json.dumps(payload).encode("utf-8")
        self.closed = False

    def read(self, limit: int | None = None) -> bytes:
        if limit is not None:
            return self._raw[:limit]
        return self._raw

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def __call__(self, request, timeout):
        body = request.data
        self.calls.append((request.method, request.full_url, dict(request.headers), body))
        if not self.responses:
            raise AssertionError("unexpected transport call")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _settings(*, environment: str = "test", enabled: bool = True) -> LemanaB2BSettings:
    return LemanaB2BSettings(
        enabled=enabled,
        environment=environment,
        client_id="client-1",
        region_id=34,
        request_timeout_s=7,
    )


def _token(value: str = "access-token", expires_in: int = 600) -> FakeResponse:
    return FakeResponse(
        200,
        {"access_token": value, "expires_in": expires_in, "token_type": "Bearer"},
    )


def _products_response(item: str = "82331508") -> FakeResponse:
    return FakeResponse(
        200,
        {
            "products": [{"productItem": item, "productName": "Клапан"}],
            "paging": {"totalCount": 1},
        },
    )


def _prices_response(item: int = 82331508) -> FakeResponse:
    return FakeResponse(
        200,
        {
            "productPrice": [
                {"productItem": item, "salesPrices": [{"salesPrice": 796.14, "currencyName": "Rub"}]}
            ]
        },
    )


def test_constructor_does_not_call_network_and_selects_known_environment_url():
    transport = FakeTransport([])
    client = LemanaB2BClient(_settings(), "secret-value", transport=transport)

    assert transport.calls == []
    assert client.api_base_url == LEMANA_API_URLS["test"]
    assert client.auth_url == LEMANA_AUTH_URL
    assert client.configured is True

    prod = LemanaB2BClient(
        _settings(environment="prod"), "secret-value", transport=FakeTransport([])
    )
    assert prod.api_base_url == LEMANA_API_URLS["prod"]


def test_client_credentials_form_and_token_cache():
    transport = FakeTransport([_token(), _products_response(), _prices_response()])
    client = LemanaB2BClient(_settings(), "client-secret", transport=transport)

    page = client.get_products()
    prices = client.get_prices(["82331508"])

    assert page is not None and page.products[0].product_item == "82331508"
    assert str(prices[0].price) == "796.14"
    assert len(transport.calls) == 3
    auth_method, auth_url, _, auth_body = transport.calls[0]
    assert auth_method == "POST" and auth_url == LEMANA_AUTH_URL
    assert parse_qs((auth_body or b"").decode()) == {
        "grant_type": ["client_credentials"],
        "client_id": ["client-1"],
        "client_secret": ["client-secret"],
    }
    assert transport.calls[1][1].startswith(f"{LEMANA_API_URLS['test']}{LEMANA_PRODUCTS_PATH}?")
    assert transport.calls[2][1] == f"{LEMANA_API_URLS['test']}{LEMANA_PRICE_BATCH_PATH}"
    assert transport.calls[1][2]["Authorization"] == "Bearer access-token"


def test_token_expires_with_safety_margin_and_is_refreshed():
    now = [100.0]
    transport = FakeTransport([_token("first", 100), _products_response(), _token("second", 100), _products_response()])
    client = LemanaB2BClient(_settings(), "secret", transport=transport, clock=lambda: now[0])

    client.get_products()
    now[0] += 95
    client.get_products()

    assert [call[0] for call in transport.calls] == ["POST", "GET", "POST", "GET"]
    assert transport.calls[3][2]["Authorization"] == "Bearer second"


def test_401_causes_one_controlled_reauth_retry():
    transport = FakeTransport([_token("old"), FakeResponse(401, {"internal": "private"}), _token("new"), _products_response()])
    client = LemanaB2BClient(_settings(), "secret", transport=transport)

    page = client.get_products()

    assert page is not None
    assert [call[0] for call in transport.calls] == ["POST", "GET", "POST", "GET"]
    assert transport.calls[-1][2]["Authorization"] == "Bearer new"


@pytest.mark.parametrize(
    ("response", "category", "status"),
    [
        (FakeResponse(400, {"secret": "private"}), "invalid_request", 400),
        (FakeResponse(401, {"token": "private"}), "auth", 401),
        (FakeResponse(403, {"authorization": "private"}), "auth", 403),
        (FakeResponse(429, {"body": "private"}), "rate_limited", 429),
        (FakeResponse(500, {"body": "private"}), "upstream_error", 500),
        (FakeResponse(523, {"body": "private"}), "upstream_error", 523),
    ],
)
def test_http_statuses_are_safe_and_typed(response, category, status):
    responses = [_token(), response]
    if status == 401:
        responses.extend([_token("retry-token"), response])
    transport = FakeTransport(responses)
    client = LemanaB2BClient(_settings(), "secret", transport=transport)

    with pytest.raises(SourcingProviderError) as error:
        client.get_products()

    assert error.value.category == category
    assert error.value.status_code == status
    assert "private" not in str(error.value)
    assert "token" not in str(error.value).casefold()
    assert "authorization" not in str(error.value).casefold()


def test_invalid_token_json_is_rejected_without_exposing_payload():
    transport = FakeTransport([FakeResponse(200, {"access_token": "private-token"})])
    client = LemanaB2BClient(_settings(), "secret", transport=transport)

    with pytest.raises(SourcingProviderError) as error:
        client.check_access()

    assert error.value.category == "invalid_response"
    assert "private-token" not in str(error.value)


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError(TimeoutError("private timeout")),
        TimeoutError("private timeout"),
    ],
)
def test_timeout_is_converted_to_safe_provider_error(failure):
    transport = FakeTransport([failure])
    client = LemanaB2BClient(_settings(), "secret", transport=transport)

    with pytest.raises(SourcingProviderError) as error:
        client.check_access()

    assert error.value.category == "timeout"
    assert "private" not in str(error.value)


def test_invalid_json_and_oversized_response_are_bounded():
    invalid = FakeResponse(200, raw=b"not-json")
    client = LemanaB2BClient(_settings(), "secret", transport=FakeTransport([_token(), invalid]))
    with pytest.raises(SourcingProviderError, match="некорректный JSON"):
        client.get_products()

    oversized = FakeResponse(200, raw=b"x" * (5 * 1024 * 1024 + 1))
    client = LemanaB2BClient(_settings(), "secret", transport=FakeTransport([_token(), oversized]))
    with pytest.raises(SourcingProviderError, match="слишком большой"):
        client.get_products()


def test_products_request_uses_documented_query_and_not_modified_is_local_signal():
    transport = FakeTransport([_token(), FakeResponse(304, {})])
    client = LemanaB2BClient(_settings(), "secret", transport=transport)

    assert client.get_products(page=3, per_page=50, if_modified_since="Wed, 01 Jan 2025 00:00:00 GMT") is None
    query = parse_qs(transport.calls[-1][1].split("?", 1)[1])
    assert query == {"regionId": ["34"], "page": ["3"], "perPage": ["50"]}
    headers = {key.casefold(): value for key, value in transport.calls[-1][2].items()}
    assert headers["if-modified-since"] == "Wed, 01 Jan 2025 00:00:00 GMT"


def test_supplier_models_whitelist_official_product_fields_and_isolate_bad_rows():
    page = parse_products_payload(
        {
            "products": [
                {
                    "productItem": 82331508,
                    "productAvailible": True,
                    "productName": "Клапан",
                    "productDescription": "Описание",
                    "productUrl": "https://example.test/p/82331508",
                    "productModel": "K-100",
                    "productBrand": "Brand",
                    "productPhoto": ["https://example.test/p.jpg"],
                    "productBarcode": "4600000000000",
                    "productParams": {"diameter": "100"},
                    "productUnitSale": {"name": "шт."},
                    "categories": [{"id": 10}],
                    "client_secret": "must-not-escape",
                },
                {"productItem": {"private": "bad"}},
            ],
            "paging": {"totalCount": 2},
        },
        page=1,
        per_page=100,
    )

    assert page.malformed_count == 1
    product = page.products[0]
    assert product.product_item == "82331508"
    assert product.product_model == "K-100"
    assert product.product_params == {"diameter": "100"}
    assert "client_secret" not in product.model_dump()


def test_price_parser_maps_official_batch_response_and_ignores_extra_fields():
    prices = parse_price_payload(
        {
            "productPrice": [
                {
                    "productItem": 85087716,
                    "salesPrices": [
                        {"salesPrice": 796.14, "currencyName": "Rub", "secret": "private"}
                    ],
                    "raw": "ignored",
                },
                {"productItem": "bad", "salesPrices": []},
            ]
        }
    )

    assert len(prices) == 1
    assert prices[0].product_item == "85087716"
    assert prices[0].currency == "Rub"
    assert str(prices[0].price) == "796.14"
