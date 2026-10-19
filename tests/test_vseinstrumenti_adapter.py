from __future__ import annotations

from decimal import Decimal
import hashlib
import io
import json
import logging
import socket
import sys
from threading import RLock
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
from averon_import.services.sourcing.providers.vseinstrumenti import client as vi_client_module
from averon_import.services.sourcing.providers import safe_diagnostics as diagnostics_module
from averon_import.services.sourcing.providers.vseinstrumenti.runtime_client import (
    SecretBackedVseinstrumentiClient,
)
from averon_import.services.secrets import (
    MemorySecretStore,
    VSEINSTRUMENTI_BEARER_TOKEN,
    VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED,
)


@pytest.fixture
def diagnostic_output(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(diagnostics_module._SAFE_HANDLER, "stream", output)
    return output


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


class HeadersRaisingResponse(FakeResponse):
    def __init__(self, body: bytes, status: int = 200):
        super().__init__(body, status)
        self.header_reads = 0

    @property
    def headers(self):
        self.header_reads += 1
        raise RuntimeError("SYNTHETIC_HEADER_ACCESS_SECRET")


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
    changed_credential_generation = VseinstrumentiExecutionAdapter(
        _client(FakeTransport()),
        region_id=REGION,
        credential_generation="a" * 32,
    ).request_identity(intent, limit=5, execution_scope_id="scope-a")

    assert first == same
    assert first.request_fingerprint != changed_query.request_fingerprint
    assert first.request_fingerprint != changed_limit.request_fingerprint
    assert first.request_fingerprint != changed_region.request_fingerprint
    assert first.request_fingerprint != changed_environment.request_fingerprint
    assert first.request_fingerprint != changed_configuration.request_fingerprint
    assert first.request_fingerprint != changed_credential_generation.request_fingerprint
    assert first.affinity.environment == "prod"
    assert first.affinity.region_id == REGION
    assert len(first.affinity.config_revision) == 64
    assert first.affinity.config_revision != changed_configuration.affinity.config_revision
    assert first.affinity.config_revision != changed_credential_generation.affinity.config_revision
    assert "a" * 32 not in json.dumps(changed_credential_generation.model_dump(mode="json"))
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


def test_offer_conversion_failure_emits_only_fixed_diagnostic(monkeypatch, diagnostic_output):
    monkeypatch.setenv("AVERON_VSEINSTRUMENTI_SAFE_DIAGNOSTICS", "1")
    adapter, transport = _adapter(_response(_payload(_product())))

    def fail_conversion(_product, _affinity):
        raise ValueError(f"{TOKEN} ARTICLE_SECRET_SENTINEL {REGION}")

    monkeypatch.setattr(adapter, "_offer", fail_conversion)
    result = adapter.execute_search(_intent(), limit=5, request_counter=ProviderRequestCounter())

    assert result.state == ProviderSearchState.FAILURE
    assert result.failure_category == ProviderFailureCategory.INVALID_RESPONSE
    assert result.offers == ()
    assert result.request_count == 1
    output = diagnostic_output.getvalue()
    assert "error_type=OFFER_CONVERSION" in output
    assert "reason_code=OFFER_CONVERSION" in output
    assert TOKEN not in output
    assert "ARTICLE_SECRET_SENTINEL" not in output
    assert REGION not in output
    assert len(transport.requests) == 1


class _RaisingDiagnosticFilter(logging.Filter):
    def __init__(self, calls):
        super().__init__()
        self.calls = calls

    def filter(self, _record):
        self.calls.append("filter")
        raise RuntimeError("SYNTHETIC_LOG_FILTER_SECRET")


class _RaisingDiagnosticFormatter(logging.Formatter):
    def __init__(self, calls):
        super().__init__()
        self.calls = calls

    def format(self, _record):
        self.calls.append("formatter")
        raise RuntimeError("SYNTHETIC_LOG_FORMATTER_SECRET")


def _inject_diagnostic_failure(monkeypatch, failure_kind):
    logger = diagnostics_module._LOGGER
    monkeypatch.setattr(logger, "filters", [])
    calls = []
    if failure_kind == "handler":
        def fail_handler(_record):
            calls.append("handler")
            raise RuntimeError("SYNTHETIC_LOG_HANDLER_SECRET")

        monkeypatch.setattr(diagnostics_module._SAFE_HANDLER, "emit", fail_handler)
    elif failure_kind == "filter":
        diagnostic_filter = _RaisingDiagnosticFilter(calls)
        monkeypatch.setattr(logger, "filter", diagnostic_filter.filter)
    elif failure_kind == "handler_filter":
        diagnostic_filter = _RaisingDiagnosticFilter(calls)
        monkeypatch.setattr(diagnostics_module._SAFE_HANDLER, "filter", diagnostic_filter.filter)
    elif failure_kind == "formatter":
        formatter = _RaisingDiagnosticFormatter(calls)
        monkeypatch.setattr(diagnostics_module._SAFE_HANDLER, "format", formatter.format)
    elif failure_kind == "stream":
        class RaisingStream:
            def write(self, _message):
                calls.append("stream")
                raise RuntimeError("SYNTHETIC_LOG_STREAM_SECRET")

            def flush(self):
                return None

        monkeypatch.setattr(diagnostics_module._SAFE_HANDLER, "stream", RaisingStream())
    elif failure_kind == "construction":
        def fail_construction(**_kwargs):
            calls.append("construction")
            raise RuntimeError("SYNTHETIC_EVENT_CONSTRUCTION_SECRET")

        monkeypatch.setattr(diagnostics_module, "_emit_allowlisted_diagnostic", fail_construction)
    else:
        raise AssertionError("unknown synthetic diagnostics fault")
    return calls


@pytest.mark.parametrize(
    "failure_kind", ["handler", "filter", "handler_filter", "formatter", "stream", "construction"],
)
def test_diagnostic_failures_do_not_change_provider_outcome_or_retry_request(
    monkeypatch, capsys, failure_kind,
):
    monkeypatch.setenv("AVERON_VSEINSTRUMENTI_SAFE_DIAGNOSTICS", "1")
    monkeypatch.setattr(logging, "raiseExceptions", True)
    monkeypatch.setattr(diagnostics_module._SAFE_HANDLER, "stream", sys.stderr)

    baseline_adapter, baseline_transport = _adapter(_response(b"{INVALID_RESPONSE_BODY_SECRET"))
    baseline = baseline_adapter.execute_search(
        _intent(), limit=5, request_counter=ProviderRequestCounter(),
    )
    capsys.readouterr()  # discard the expected baseline diagnostic record

    calls = _inject_diagnostic_failure(monkeypatch, failure_kind)
    faulted_adapter, faulted_transport = _adapter(_response(b"{INVALID_RESPONSE_BODY_SECRET"))
    faulted = faulted_adapter.execute_search(
        _intent(), limit=5, request_counter=ProviderRequestCounter(),
    )

    assert baseline.model_dump(mode="json") == faulted.model_dump(mode="json")
    assert baseline.state == faulted.state == ProviderSearchState.FAILURE
    assert baseline.failure_category == faulted.failure_category == ProviderFailureCategory.INVALID_RESPONSE
    assert baseline.request_count == faulted.request_count == 1
    assert len(baseline_transport.requests) == len(faulted_transport.requests) == 1
    assert calls.count("handler") == (1 if failure_kind == "handler" else 0)
    assert calls.count("filter") == (1 if failure_kind in {"filter", "handler_filter"} else 0)
    assert calls.count("formatter") == (1 if failure_kind == "formatter" else 0)
    assert calls.count("stream") == (1 if failure_kind == "stream" else 0)
    assert calls.count("construction") == (1 if failure_kind == "construction" else 0)
    stderr = capsys.readouterr().err
    assert "--- Logging error ---" not in stderr
    assert "Traceback" not in stderr
    for secret in (
        TOKEN,
        "INVALID_RESPONSE_BODY_SECRET",
        "SYNTHETIC_LOG_HANDLER_SECRET",
        "SYNTHETIC_LOG_FILTER_SECRET",
        "SYNTHETIC_LOG_FORMATTER_SECRET",
        "SYNTHETIC_LOG_STREAM_SECRET",
        "SYNTHETIC_EVENT_CONSTRUCTION_SECRET",
    ):
        assert secret not in stderr
    assert stderr == ""


@pytest.mark.parametrize("flag_value", [None, "0"])
def test_diagnostics_off_preserves_baseline_outcome_and_emits_nothing(monkeypatch, diagnostic_output, flag_value):
    if flag_value is None:
        monkeypatch.delenv("AVERON_VSEINSTRUMENTI_SAFE_DIAGNOSTICS", raising=False)
    else:
        monkeypatch.setenv("AVERON_VSEINSTRUMENTI_SAFE_DIAGNOSTICS", flag_value)

    baseline_adapter, baseline_transport = _adapter(_response(b"{INVALID_RESPONSE_BODY_SECRET"))
    baseline = baseline_adapter.execute_search(
        _intent(), limit=5, request_counter=ProviderRequestCounter(),
    )
    current_adapter, current_transport = _adapter(_response(b"{INVALID_RESPONSE_BODY_SECRET"))
    current = current_adapter.execute_search(
        _intent(), limit=5, request_counter=ProviderRequestCounter(),
    )

    assert baseline.model_dump(mode="json") == current.model_dump(mode="json")
    assert baseline.request_count == current.request_count == 1
    assert len(baseline_transport.requests) == len(current_transport.requests) == 1
    assert diagnostic_output.getvalue() == ""


def test_enabled_diagnostic_emits_one_allowlisted_record_without_extra_request(
    monkeypatch, diagnostic_output,
):
    monkeypatch.setenv("AVERON_VSEINSTRUMENTI_SAFE_DIAGNOSTICS", "1")
    adapter, transport = _adapter(_response(b"{INVALID_RESPONSE_BODY_SECRET"))

    outcome = adapter.execute_search(
        _intent(), limit=5, request_counter=ProviderRequestCounter(),
    )

    lines = diagnostic_output.getvalue().splitlines()
    assert outcome.state == ProviderSearchState.FAILURE
    assert outcome.failure_category == ProviderFailureCategory.INVALID_RESPONSE
    assert outcome.request_count == 1
    assert len(transport.requests) == 1
    assert len(lines) == 1
    assert lines[0].startswith("vseinstrumenti_safe_diagnostic ")
    assert set(lines[0].split()[1:]) == {
        "error_type=JSON_DECODING",
        "status_code=200",
        "envelope_status_code=MISSING",
        "content_type=MISSING",
        f"response_bytes={len(b'{INVALID_RESPONSE_BODY_SECRET')}",
        "reason_code=UNKNOWN",
    }


def test_safe_sink_does_not_propagate_to_root_handler(monkeypatch, capsys):
    monkeypatch.setenv("AVERON_VSEINSTRUMENTI_SAFE_DIAGNOSTICS", "1")
    monkeypatch.setattr(diagnostics_module._SAFE_HANDLER, "stream", sys.stderr)
    logger = diagnostics_module._LOGGER
    root = logging.getLogger()
    root_output = io.StringIO()
    root_handler = logging.StreamHandler(root_output)
    original_root_handlers = tuple(root.handlers)
    original_propagate = logger.propagate
    try:
        root.addHandler(root_handler)
        logger.propagate = True
        diagnostics_module.log_vseinstrumenti_diagnostic(
            error_type=diagnostics_module.VseinstrumentiDiagnosticErrorType.TRANSPORT,
        )
    finally:
        root.removeHandler(root_handler)
        logger.propagate = original_propagate

    assert tuple(root.handlers) == original_root_handlers
    assert root_output.getvalue() == ""
    stderr = capsys.readouterr().err
    assert stderr.count("vseinstrumenti_safe_diagnostic ") == 1


def test_header_metadata_failure_preserves_provider_outcome_off_and_on(monkeypatch, diagnostic_output):
    monkeypatch.delenv("AVERON_VSEINSTRUMENTI_SAFE_DIAGNOSTICS", raising=False)
    off_response = HeadersRaisingResponse(b"{INVALID_RESPONSE_BODY_SECRET")
    off_adapter, off_transport = _adapter(off_response)
    off_outcome = off_adapter.execute_search(
        _intent(), limit=5, request_counter=ProviderRequestCounter(),
    )
    assert off_response.header_reads == 0
    assert diagnostic_output.getvalue() == ""

    monkeypatch.setenv("AVERON_VSEINSTRUMENTI_SAFE_DIAGNOSTICS", "1")
    on_response = HeadersRaisingResponse(b"{INVALID_RESPONSE_BODY_SECRET")
    on_adapter, on_transport = _adapter(on_response)
    on_outcome = on_adapter.execute_search(
        _intent(), limit=5, request_counter=ProviderRequestCounter(),
    )

    assert off_outcome.model_dump(mode="json") == on_outcome.model_dump(mode="json")
    assert on_response.header_reads == 1
    assert "content_type=MISSING" in diagnostic_output.getvalue()
    assert "SYNTHETIC_HEADER_ACCESS_SECRET" not in diagnostic_output.getvalue()
    for outcome in (off_outcome, on_outcome):
        assert outcome.state == ProviderSearchState.FAILURE
        assert outcome.failure_category == ProviderFailureCategory.INVALID_RESPONSE
        assert outcome.offers == ()
        assert outcome.request_count == 1
    assert len(off_transport.requests) == len(on_transport.requests) == 1


def test_runner_outcome_contract_failure_is_safely_diagnosed(monkeypatch, diagnostic_output):
    monkeypatch.setenv("AVERON_VSEINSTRUMENTI_SAFE_DIAGNOSTICS", "1")
    adapter, transport = _adapter()
    identity = adapter.request_identity(_intent(), limit=5, execution_scope_id="diagnostic-scope")

    def invalid_outcome(_intent, *, limit, request_counter):
        return ProviderSearchOutcome(
            provider_key="other-provider",
            state=ProviderSearchState.EMPTY,
            request_count=request_counter.request_count,
            affinity=identity.affinity,
        )

    adapter.execute_search = invalid_outcome
    result = ProviderRunner({"vseinstrumenti": adapter}).run(
        selection=ProviderSelection(provider_keys=("vseinstrumenti",)),
        intent=_intent(),
        limit=5,
        scope=ProviderExecutionScope(execution_scope_id="diagnostic-scope"),
    )

    assert result.outcomes[0].state == ProviderSearchState.FAILURE
    assert result.outcomes[0].failure_category == ProviderFailureCategory.INVALID_RESPONSE
    assert result.outcomes[0].offers == ()
    assert result.outcomes[0].request_count == 0
    assert "error_type=OUTCOME_CONTRACT" in diagnostic_output.getvalue()
    assert "reason_code=OUTCOME_CONTRACT" in diagnostic_output.getvalue()
    assert len(transport.requests) == 0


@pytest.mark.parametrize(
    "failure_kind", ["handler", "filter", "handler_filter", "formatter", "stream", "construction"],
)
def test_runner_outcome_is_preserved_when_diagnostic_sink_fails(monkeypatch, capsys, failure_kind):
    monkeypatch.setenv("AVERON_VSEINSTRUMENTI_SAFE_DIAGNOSTICS", "1")
    monkeypatch.setattr(logging, "raiseExceptions", True)
    monkeypatch.setattr(diagnostics_module._SAFE_HANDLER, "stream", sys.stderr)
    adapter, transport = _adapter()
    identity = adapter.request_identity(_intent(), limit=5, execution_scope_id="sink-fault-runner-baseline")

    def invalid_outcome(_intent, *, limit, request_counter):
        return ProviderSearchOutcome(
            provider_key="other-provider",
            state=ProviderSearchState.EMPTY,
            request_count=request_counter.request_count,
            affinity=identity.affinity,
        )

    adapter.execute_search = invalid_outcome
    selection = ProviderSelection(provider_keys=("vseinstrumenti",))
    baseline = ProviderRunner({"vseinstrumenti": adapter}).run(
        selection=selection,
        intent=_intent(),
        limit=5,
        scope=ProviderExecutionScope(execution_scope_id="sink-fault-runner-baseline"),
    )
    capsys.readouterr()

    calls = _inject_diagnostic_failure(monkeypatch, failure_kind)
    faulted = ProviderRunner({"vseinstrumenti": adapter}).run(
        selection=selection,
        intent=_intent(),
        limit=5,
        scope=ProviderExecutionScope(execution_scope_id="sink-fault-runner-faulted"),
    )

    assert baseline.outcomes[0].model_dump(mode="json") == faulted.outcomes[0].model_dump(mode="json")
    assert faulted.outcomes[0].state == ProviderSearchState.FAILURE
    assert faulted.outcomes[0].failure_category == ProviderFailureCategory.INVALID_RESPONSE
    assert faulted.outcomes[0].request_count == 0
    assert faulted.outcomes[0].offers == ()
    assert transport.requests == []
    assert calls
    stderr = capsys.readouterr().err
    assert stderr == ""
    for secret in (
        "SYNTHETIC_LOG_HANDLER_SECRET",
        "SYNTHETIC_LOG_FILTER_SECRET",
        "SYNTHETIC_LOG_FORMATTER_SECRET",
        "SYNTHETIC_LOG_STREAM_SECRET",
        "SYNTHETIC_EVENT_CONSTRUCTION_SECRET",
    ):
        assert secret not in stderr


def test_runner_outcome_contract_is_preserved_when_diagnostic_emission_fails(monkeypatch):
    monkeypatch.setenv("AVERON_VSEINSTRUMENTI_SAFE_DIAGNOSTICS", "1")
    adapter, transport = _adapter()
    identity = adapter.request_identity(_intent(), limit=5, execution_scope_id="diagnostic-failure-scope")

    def invalid_outcome(_intent, *, limit, request_counter):
        return ProviderSearchOutcome(
            provider_key="other-provider",
            state=ProviderSearchState.EMPTY,
            request_count=request_counter.request_count,
            affinity=identity.affinity,
        )

    adapter.execute_search = invalid_outcome
    baseline = ProviderRunner({"vseinstrumenti": adapter}).run(
        selection=ProviderSelection(provider_keys=("vseinstrumenti",)),
        intent=_intent(),
        limit=5,
        scope=ProviderExecutionScope(execution_scope_id="diagnostic-failure-scope"),
    )
    monkeypatch.setattr(
        diagnostics_module,
        "_emit_allowlisted_diagnostic",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("SYNTHETIC_EVENT_CONSTRUCTION_SECRET")),
    )
    faulted = ProviderRunner({"vseinstrumenti": adapter}).run(
        selection=ProviderSelection(provider_keys=("vseinstrumenti",)),
        intent=_intent(),
        limit=5,
        scope=ProviderExecutionScope(execution_scope_id="diagnostic-failure-scope"),
    )

    assert baseline.outcomes[0].model_dump(mode="json") == faulted.outcomes[0].model_dump(mode="json")
    assert faulted.outcomes[0].state == ProviderSearchState.FAILURE
    assert faulted.outcomes[0].failure_category == ProviderFailureCategory.INVALID_RESPONSE
    assert faulted.outcomes[0].request_count == 0
    assert faulted.outcomes[0].offers == ()
    assert transport.requests == []


def _secret_backed_adapter(monkeypatch, *, generation_value, store, transport):
    monkeypatch.setattr(vi_client_module, "_default_transport", transport)
    limiter = VseinstrumentiRateLimiter(clock=lambda: 0.0, sleeper=lambda _delay: None)
    client = SecretBackedVseinstrumentiClient(
        store,
        environment="prod",
        timeout_seconds=10.0,
        credential_generation="a" * 32,
        current_generation=lambda: generation_value[0],
        credential_lock=RLock(),
        rate_limiter=limiter,
    )
    return VseinstrumentiExecutionAdapter(
        client,
        region_id=REGION,
        credential_generation="a" * 32,
    )


class _ReadTrackingMemorySecretStore(MemorySecretStore):
    def __init__(self):
        super().__init__()
        self.reads = []

    def get(self, key):
        self.reads.append(key)
        return super().get(key)


def test_runtime_vi_cache_hit_rechecks_marker_without_reading_token(monkeypatch):
    store = _ReadTrackingMemorySecretStore()
    generation = "a" * 32
    store.set(VSEINSTRUMENTI_BEARER_TOKEN, TOKEN)
    store.set(VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED, generation)
    transport = FakeTransport(_response(_payload(_product())))
    adapter = _secret_backed_adapter(
        monkeypatch,
        generation_value=[generation],
        store=store,
        transport=transport,
    )
    runner = ProviderRunner({"vseinstrumenti": adapter})
    scope = ProviderExecutionScope(execution_scope_id="vi-runtime-cache-current")
    selection = ProviderSelection(provider_keys=("vseinstrumenti",))

    first = runner.run(selection=selection, intent=_intent(), limit=5, scope=scope)
    store.reads.clear()
    cached = runner.run(selection=selection, intent=_intent(), limit=5, scope=scope)

    assert first.outcomes[0].state == ProviderSearchState.SUCCESS
    assert cached.outcomes[0].state == ProviderSearchState.SUCCESS
    assert cached.outcomes[0].request_count == 0
    assert cached.reused_provider_keys == ("vseinstrumenti",)
    assert store.reads == [VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED]
    assert len(transport.requests) == 1


@pytest.mark.parametrize("change", ["generation", "marker"])
def test_runtime_vi_stale_cache_fails_closed_without_token_read_or_http(monkeypatch, change):
    store = _ReadTrackingMemorySecretStore()
    initial_generation = "a" * 32
    current_generation = [initial_generation]
    store.set(VSEINSTRUMENTI_BEARER_TOKEN, TOKEN)
    store.set(VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED, initial_generation)
    transport = FakeTransport(_response(_payload(_product())))
    adapter = _secret_backed_adapter(
        monkeypatch,
        generation_value=current_generation,
        store=store,
        transport=transport,
    )
    runner = ProviderRunner({"vseinstrumenti": adapter})
    scope = ProviderExecutionScope(execution_scope_id=f"vi-runtime-cache-stale-{change}")
    selection = ProviderSelection(provider_keys=("vseinstrumenti",))
    first = runner.run(selection=selection, intent=_intent(), limit=5, scope=scope)
    store.reads.clear()

    if change == "generation":
        current_generation[0] = "b" * 32
    else:
        store.delete(VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED)
    stale = runner.run(selection=selection, intent=_intent(), limit=5, scope=scope)

    assert first.outcomes[0].state == ProviderSearchState.SUCCESS
    assert stale.outcomes[0].state == ProviderSearchState.FAILURE
    assert stale.outcomes[0].offers == ()
    assert stale.outcomes[0].request_count == 0
    assert stale.reused_provider_keys == ()
    assert store.reads == [VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED]
    assert len(transport.requests) == 1
    assert TOKEN not in stale.model_dump_json()


def test_credential_generation_prevents_reuse_across_rotated_vi_credentials():
    first_transport = FakeTransport(_response(_payload(_product(sku="old-generation"))))
    second_transport = FakeTransport(_response(_payload(_product(sku="new-generation"))))
    first_adapter = VseinstrumentiExecutionAdapter(
        _client(first_transport),
        region_id=REGION,
        credential_generation="a" * 32,
    )
    second_adapter = VseinstrumentiExecutionAdapter(
        _client(second_transport),
        region_id=REGION,
        credential_generation="b" * 32,
    )
    selection = ProviderSelection(provider_keys=("vseinstrumenti",))
    scope = ProviderExecutionScope(execution_scope_id="rotation-scope")

    first = ProviderRunner({"vseinstrumenti": first_adapter}).run(
        selection=selection,
        intent=_intent(),
        limit=5,
        scope=scope,
    )
    second = ProviderRunner({"vseinstrumenti": second_adapter}).run(
        selection=selection,
        intent=_intent(),
        limit=5,
        scope=scope,
    )

    assert first.outcomes[0].request_count == 1
    assert second.outcomes[0].request_count == 1
    assert second.reused_provider_keys == ()
    assert len(first_transport.requests) == len(second_transport.requests) == 1


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


def test_vi_durable_projection_preserves_safe_provider_provenance_without_generation():
    adapter, _transport = _adapter(_response(_payload(_product())))
    offer = adapter.execute_search(_intent(), limit=5, request_counter=ProviderRequestCounter()).offers[0]

    durable = DurableOffer.model_validate(durable_offer_payload(offer))

    assert durable.provenance.model_dump(mode="json") == {
        "source": "vseinstrumenti",
        "source_item_id": "VI-001",
        "price_field": "prices.price",
        "environment": "prod",
        "region_id": REGION,
    }
    assert "config_revision" not in durable.provenance.model_dump(mode="json")
    assert durable.offer_reference.provider_key == "vseinstrumenti"
    assert durable.offer_reference.offer_id == "VI-001"
