from __future__ import annotations

from decimal import Decimal
import json
import socket
import traceback
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

import pytest

from averon_import.services.sourcing.providers.tstn.client import (
    TSTN_API_BASE_URL,
    TSTN_MAX_HTTP_ATTEMPTS,
    TSTN_MAX_RESPONSE_BYTES,
    TstnApiClient,
    TstnApiError,
    TstnErrorCategory,
    TstnHttpResponse,
    _NoRedirectHandler,
    _default_transport,
)
from averon_import.services.sourcing.providers.tstn.models import (
    TstnAuthorizationRequest,
    TstnItemPrice,
)


SYNTHETIC_ACCESS_TOKEN = "synthetic-token/with?reserved&characters"
SYNTHETIC_KEY = "synthetic-key/with?reserved&characters"


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


def _response(body: bytes | object = b"{}", *, status: int = 200) -> TstnHttpResponse:
    if not isinstance(body, bytes):
        body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return TstnHttpResponse(status_code=status, body=body)


def _query(request: urllib.request.Request) -> dict[str, list[str]]:
    return parse_qs(urlsplit(request.full_url).query, keep_blank_values=True)


def test_client_construction_is_offline_and_repr_contains_no_credentials():
    transport = FakeTransport(_response({"access_token": "unused"}))
    client = TstnApiClient(transport=transport)

    assert transport.requests == []
    assert "token" not in repr(client).casefold()
    assert client.request_attempt_count == 0


def test_all_eight_documented_operations_use_only_their_swagger_paths_and_parameters():
    token = SYNTHETIC_ACCESS_TOKEN
    transport = FakeTransport(
        _response({"access_token": token, "access_date": "uninterpreted supplier value"}),
        _response({"count": 1, "results": [{"id": 12, "name": "Section"}]}),
        _response({"id": 12, "name": "Section", "categories": []}),
        _response({"count": 1, "count_all": 1, "page_num": 1, "results": [
            {"id": 41, "article": "001755", "name": "Item", "units_list": [{"unknown": "kept"}],
             "base_unit": "м"},
        ]}),
        _response({"id": 41, "article": "001755", "base_unit": "м"}),
        _response({"city": "077", "count": 1, "results": [
            {"article": "001755", "status": "Новый статус"},
        ]}),
        _response({"city": "077", "count": 1, "results": [
            {"article": "001755", "price": "6655.47"},
        ]}),
        _response([{"name": "Город", "code": "077"}]),
    )
    client = TstnApiClient(transport=transport)

    authorization = client.authorize(service="service identifier /+?", key=SYNTHETIC_KEY)
    sections = client.list_sections(access_token=authorization.access_token)
    section = client.get_section(12, access_token=authorization.access_token)
    items = client.list_items(access_token=authorization.access_token, category=5, limit=20, offset=40)
    item = client.get_item("001755", access_token=authorization.access_token)
    available = client.get_available(
        access_token=authorization.access_token,
        category=5,
        id="41",
        article="001755",
        city="077",
    )
    prices = client.get_prices(access_token=authorization.access_token, article="001755", city="077")
    cities = client.list_cities(access_token=authorization.access_token)

    assert authorization.access_token.get_secret_value() == token
    assert token not in repr(authorization)
    assert token not in authorization.model_dump_json()
    assert authorization.access_date == "uninterpreted supplier value"
    assert sections.results[0].id == 12
    assert section.id == 12
    assert items.results[0].id == 41
    assert items.results[0].article == "001755"
    assert items.results[0].units_list == ({"unknown": "kept"},)
    assert item.article == "001755"
    assert available.results[0].status == "Новый статус"
    assert prices.results[0].price == Decimal("6655.47")
    assert cities[0].code == "077"

    expected = [
        ("POST", "/api/v1/user/", {"service": ["service identifier /+?"], "key": [SYNTHETIC_KEY]}),
        ("GET", "/api/v1/catalog/sections/", {"access_token": [token]}),
        ("GET", "/api/v1/catalog/sections/12/", {"access_token": [token]}),
        ("GET", "/api/v1/catalog/items/", {
            "access_token": [token], "category": ["5"], "limit": ["20"], "offset": ["40"],
        }),
        ("GET", "/api/v1/catalog/items/001755/", {"access_token": [token]}),
        ("GET", "/api/v1/catalog/available/", {
            "access_token": [token], "category": ["5"], "id": ["41"],
            "article": ["001755"], "city": ["077"],
        }),
        ("GET", "/api/v1/catalog/prices/", {
            "access_token": [token], "article": ["001755"], "city": ["077"],
        }),
        ("GET", "/api/v1/city/", {"access_token": [token]}),
    ]
    assert len(transport.requests) == 8
    assert client.request_attempt_count == 8
    assert transport.timeouts == [10.0] * 8
    for request, (method, path, query) in zip(transport.requests, expected, strict=True):
        parts = urlsplit(request.full_url)
        assert parts.scheme == "https"
        assert parts.netloc == "tstn.ru"
        assert parts.path == path
        assert request.get_method() == method
        assert _query(request) == query
        assert request.data is None
        assert "X-sp-secret" not in request.headers


def test_item_path_identifier_is_percent_encoded_as_one_segment():
    transport = FakeTransport(_response({"id": 1, "article": "A/B? C"}))
    client = TstnApiClient(transport=transport)

    client.get_item("A/B? C", access_token=SYNTHETIC_ACCESS_TOKEN)

    parts = urlsplit(transport.requests[0].full_url)
    assert parts.path == "/api/v1/catalog/items/A%2FB%3F%20C/"
    assert _query(transport.requests[0]) == {"access_token": [SYNTHETIC_ACCESS_TOKEN]}


def test_optional_parameters_are_omitted_and_no_name_search_operation_exists():
    transport = FakeTransport(
        _response({"results": []}),
        _response({"results": []}),
    )
    client = TstnApiClient(transport=transport)

    client.list_items(access_token=SYNTHETIC_ACCESS_TOKEN)
    client.get_prices(access_token=SYNTHETIC_ACCESS_TOKEN)

    assert _query(transport.requests[0]) == {"access_token": [SYNTHETIC_ACCESS_TOKEN]}
    assert _query(transport.requests[1]) == {"access_token": [SYNTHETIC_ACCESS_TOKEN]}
    assert not hasattr(client, "search_by_name")
    assert not hasattr(client, "search_products")


@pytest.mark.parametrize("value, expected", [
    (6655, Decimal("6655")),
    (Decimal("6655.47"), Decimal("6655.47")),
    ("6655.47", Decimal("6655.47")),
])
def test_item_price_accepts_exact_finite_integer_and_fractional_values(value, expected):
    assert TstnItemPrice(article="001755", price=value).price == expected


def test_json_fractional_price_is_parsed_without_float_rounding():
    transport = FakeTransport(_response(b'{"results":[{"article":"001755","price":6655.47}]}'))
    prices = TstnApiClient(transport=transport).get_prices(
        access_token=SYNTHETIC_ACCESS_TOKEN,
        article="001755",
    )

    assert prices.results[0].price == Decimal("6655.47")


@pytest.mark.parametrize("price", [-1, "-1.25", "NaN", "Infinity", "1e101", True, [], {}])
def test_invalid_or_ambiguous_prices_fail_closed_without_echoing_input(price):
    raw_price = json.dumps(price, separators=(",", ":"), allow_nan=True)
    body = f'{{"results":[{{"article":"001755","price":{raw_price}}}]}}'.encode()
    transport = FakeTransport(_response(body))

    with pytest.raises(TstnApiError) as caught:
        TstnApiClient(transport=transport).get_prices(
            access_token=SYNTHETIC_ACCESS_TOKEN,
            article="001755",
        )

    assert caught.value.category == TstnErrorCategory.INVALID_RESPONSE
    assert raw_price not in str(caught.value)


def test_empty_and_partial_documented_responses_remain_explicit():
    transport = FakeTransport(
        _response({"results": []}),
        _response({"results": [{"article": "001755"}]}),
        _response({"results": [{"article": "001755"}]}),
    )
    client = TstnApiClient(transport=transport)

    empty = client.list_items(access_token=SYNTHETIC_ACCESS_TOKEN)
    partial_item = client.list_items(access_token=SYNTHETIC_ACCESS_TOKEN)
    partial_price = client.get_prices(access_token=SYNTHETIC_ACCESS_TOKEN)

    assert empty.results == ()
    assert partial_item.results[0].id is None
    assert partial_item.results[0].article == "001755"
    assert partial_price.results[0].price is None


@pytest.mark.parametrize("status, category", [
    (401, TstnErrorCategory.AUTHENTICATION),
    (403, TstnErrorCategory.FORBIDDEN),
    (404, TstnErrorCategory.NOT_FOUND),
    (429, TstnErrorCategory.RATE_LIMITED),
    (500, TstnErrorCategory.UNAVAILABLE),
    (503, TstnErrorCategory.UNAVAILABLE),
    (400, TstnErrorCategory.INVALID_REQUEST),
    (302, TstnErrorCategory.REDIRECT_BLOCKED),
])
def test_http_errors_are_classified_without_reflected_query_or_body(status, category):
    body = f'{{"message":"reflected {SYNTHETIC_ACCESS_TOKEN} {SYNTHETIC_KEY}"}}'.encode()
    transport = FakeTransport(_response(body, status=status))
    client = TstnApiClient(transport=transport)

    with pytest.raises(TstnApiError) as caught:
        client.list_sections(access_token=SYNTHETIC_ACCESS_TOKEN)

    error = caught.value
    rendered = " ".join((str(error), repr(error), " ".join(error.args)))
    assert error.category == category
    assert error.status_code == status
    assert SYNTHETIC_ACCESS_TOKEN not in rendered
    assert SYNTHETIC_KEY not in rendered
    assert "access_token=" not in rendered
    assert error.__context__ is None
    assert len(transport.requests) == 1


@pytest.mark.parametrize("exception, category", [
    (TimeoutError(f"timed out for {SYNTHETIC_ACCESS_TOKEN}"), TstnErrorCategory.TIMEOUT),
    (socket.timeout(f"timed out for {SYNTHETIC_ACCESS_TOKEN}"), TstnErrorCategory.TIMEOUT),
    (RuntimeError(f"failed URL access_token={SYNTHETIC_ACCESS_TOKEN}&key={SYNTHETIC_KEY}"),
     TstnErrorCategory.TRANSPORT),
    (urllib.error.URLError(f"failed URL access_token={SYNTHETIC_ACCESS_TOKEN}"),
     TstnErrorCategory.TRANSPORT),
])
def test_transport_errors_are_sanitized_unchained_and_not_retried(exception, category):
    transport = FakeTransport(exception)
    client = TstnApiClient(transport=transport)

    with pytest.raises(TstnApiError) as caught:
        client.list_sections(access_token=SYNTHETIC_ACCESS_TOKEN)

    error = caught.value
    rendered_traceback = "".join(traceback.format_exception(error))
    rendered = f"{error!s} {error!r} {rendered_traceback}"
    assert error.category == category
    assert SYNTHETIC_ACCESS_TOKEN not in rendered
    assert SYNTHETIC_KEY not in rendered
    assert TSTN_API_BASE_URL not in rendered
    assert error.__context__ is None
    assert len(transport.requests) == 1
    assert client.request_attempt_count == 1


def test_http_error_exception_url_is_not_retained():
    url = f"{TSTN_API_BASE_URL}/catalog/sections/?access_token={SYNTHETIC_ACCESS_TOKEN}"
    transport = FakeTransport(urllib.error.HTTPError(url, 401, "bad", {}, None))
    client = TstnApiClient(transport=transport)

    with pytest.raises(TstnApiError) as caught:
        client.list_sections(access_token=SYNTHETIC_ACCESS_TOKEN)

    error = caught.value
    rendered = f"{error!s} {error!r} {''.join(traceback.format_exception(error))}"
    assert error.category == TstnErrorCategory.AUTHENTICATION
    assert SYNTHETIC_ACCESS_TOKEN not in rendered
    assert url not in rendered
    assert error.__context__ is None
    assert len(transport.requests) == 1


def test_authorization_transport_error_does_not_expose_service_or_key():
    service = "synthetic-service-id"
    key = SYNTHETIC_KEY
    fake_url = f"{TSTN_API_BASE_URL}/user/?service={service}&key={key}"
    transport = FakeTransport(RuntimeError(f"failed request {fake_url}"))
    client = TstnApiClient(transport=transport)

    with pytest.raises(TstnApiError) as caught:
        client.authorize(service=service, key=key)

    error = caught.value
    rendered = f"{error!s} {error!r} {''.join(traceback.format_exception(error))}"
    assert error.category == TstnErrorCategory.TRANSPORT
    assert service not in rendered
    assert key not in rendered
    assert fake_url not in rendered
    assert error.__context__ is None


def test_client_enforces_response_size_limit():
    transport = FakeTransport(_response(b" " * 11))
    client = TstnApiClient(transport=transport, max_response_bytes=10)

    with pytest.raises(TstnApiError) as caught:
        client.list_sections(access_token=SYNTHETIC_ACCESS_TOKEN)

    assert caught.value.category == TstnErrorCategory.RESPONSE_TOO_LARGE
    assert caught.value.status_code == 200


def test_client_caps_total_http_attempts_and_does_not_retry():
    transport = FakeTransport(_response({"results": []}), _response({"results": []}))
    client = TstnApiClient(transport=transport, max_attempts=2)

    client.list_sections(access_token=SYNTHETIC_ACCESS_TOKEN)
    client.list_sections(access_token=SYNTHETIC_ACCESS_TOKEN)
    with pytest.raises(TstnApiError) as caught:
        client.list_sections(access_token=SYNTHETIC_ACCESS_TOKEN)

    assert caught.value.category == TstnErrorCategory.REQUEST_LIMIT
    assert client.request_attempt_count == 2
    assert len(transport.requests) == 2
    assert TSTN_MAX_HTTP_ATTEMPTS == 64


@pytest.mark.parametrize("timeout", [0, -1, 30.1, float("nan"), float("inf"), True])
def test_invalid_timeout_fails_during_offline_client_construction(timeout):
    with pytest.raises(TstnApiError) as caught:
        TstnApiClient(timeout_seconds=timeout)
    assert caught.value.category == TstnErrorCategory.MISCONFIGURED


def test_invalid_request_credentials_and_pagination_fail_before_transport():
    transport = FakeTransport()
    client = TstnApiClient(transport=transport)

    invalid_calls = [
        lambda: client.list_sections(access_token=f"{SYNTHETIC_ACCESS_TOKEN}\n"),
        lambda: client.authorize(service=" ", key=SYNTHETIC_KEY),
        lambda: client.list_items(access_token=SYNTHETIC_ACCESS_TOKEN, limit=101),
        lambda: client.list_items(access_token=SYNTHETIC_ACCESS_TOKEN, offset=-1),
        lambda: client.get_section(True, access_token=SYNTHETIC_ACCESS_TOKEN),
    ]
    for call in invalid_calls:
        with pytest.raises(TstnApiError) as caught:
            call()
        assert caught.value.category == TstnErrorCategory.MISCONFIGURED
        assert SYNTHETIC_ACCESS_TOKEN not in str(caught.value)

    assert transport.requests == []
    assert client.request_attempt_count == 0


def test_authorization_models_mask_credentials_in_repr_and_json_dump():
    request = TstnAuthorizationRequest(service="synthetic-service", key=SYNTHETIC_KEY)
    rendered = f"{request!r} {request.model_dump_json()}"

    assert SYNTHETIC_KEY not in rendered
    assert "**********" in rendered


def test_invalid_json_and_duplicate_json_keys_fail_without_body_echo():
    for body in (
        b"not-json",
        b'{"results":[],"results":[]} ',
        b'{"results":[{"price":NaN}]}',
    ):
        client = TstnApiClient(transport=FakeTransport(_response(body)))
        with pytest.raises(TstnApiError) as caught:
            client.get_prices(access_token=SYNTHETIC_ACCESS_TOKEN)
        assert caught.value.category == TstnErrorCategory.INVALID_RESPONSE
        assert SYNTHETIC_ACCESS_TOKEN not in str(caught.value)


def test_no_redirect_handler_refuses_redirects_without_following_target():
    request = urllib.request.Request(
        f"{TSTN_API_BASE_URL}/catalog/sections/?access_token={SYNTHETIC_ACCESS_TOKEN}",
        method="GET",
    )
    handler = _NoRedirectHandler()

    target = handler.redirect_request(
        request, None, 302, "Found", {"Location": "https://evil.example/"},
        "https://evil.example/",
    )

    assert target is None


def test_default_transport_reads_at_most_response_limit_plus_one(monkeypatch):
    class FakeStream:
        def __init__(self):
            self.status = 200
            self.read_limit = None
            self.closed = False

        def getcode(self):
            return self.status

        def read(self, limit):
            self.read_limit = limit
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.closed = True

    class FakeOpener:
        def __init__(self, response):
            self.response = response
            self.requests = []

        def open(self, request, timeout):
            self.requests.append((request, timeout))
            return self.response

    response = FakeStream()
    opener = FakeOpener(response)
    monkeypatch.setattr(
        "averon_import.services.sourcing.providers.tstn.client._NO_REDIRECT_OPENER",
        opener,
    )
    request = urllib.request.Request(
        f"{TSTN_API_BASE_URL}/catalog/sections/?access_token={SYNTHETIC_ACCESS_TOKEN}",
    )

    result = _default_transport(request, 3.0)

    assert result == TstnHttpResponse(status_code=200, body=b"{}")
    assert response.read_limit == TSTN_MAX_RESPONSE_BYTES + 1
    assert response.closed
    assert len(opener.requests) == 1
    assert opener.requests[0][1] == 3.0
