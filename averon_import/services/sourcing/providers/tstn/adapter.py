"""Explicit, request-local outcome adapter for TSTN commercial data.

The adapter is intentionally not registered by the application runtime. Its
constructor and request identity are local-only; supplier HTTP is possible
only from ``execute_search`` after a ready local mirror returned candidates.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
import re
from typing import Any, Protocol

from pydantic import SecretStr

from averon_import.services.secrets import TSTN_CREDENTIALS_CONFIGURED, SecretStore
from averon_import.services.sourcing.models import Offer, ProductIntent
from averon_import.services.sourcing.providers.contracts import (
    ProviderAffinity,
    ProviderFailureCategory,
    ProviderSearchOutcome,
    ProviderSearchRequestIdentity,
    ProviderSearchState,
)
from averon_import.services.sourcing.providers.execution import (
    ProviderRequestCounter,
    ProviderRequestLimitExceeded,
)

from .authorization import (
    TSTN_CREDENTIAL_LOCK,
    SecretStoreTstnAuthorizer,
    create_tstn_authorization_client,
)
from .client import TstnApiClient, TstnApiError, TstnErrorCategory
from .mirror import TSTN_MAX_SEARCH_RESULTS, TstnCatalogMirror, TstnCatalogRecord
from .models import TstnAvailableItemsResponse, TstnPricesResponse


TSTN_ADAPTER_REVISION = "m6p-tn4a-outcome-adapter-v1"
TSTN_MAX_CANDIDATES = 4
TSTN_MAX_HTTP_ATTEMPTS = 1 + (TSTN_MAX_CANDIDATES * 2)
_GENERATION_RE = re.compile(r"^[a-f0-9]{32}$")
_REVISION_RE = re.compile(r"^[a-f0-9]{64}$")


class TstnCurrentConfiguration(Protocol):
    def __call__(self) -> tuple[bool, str]: ...


class TstnClientFactory(Protocol):
    def __call__(
        self,
        timeout_seconds: float,
        *,
        max_attempts: int,
        outbound_attempt_observer: Callable[[], None],
    ) -> TstnApiClient: ...


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_revision(value: object) -> str:
    if isinstance(value, str) and (
        value == "unknown" or _REVISION_RE.fullmatch(value)
    ):
        return value
    return "unknown"


def _offer_identity(record: TstnCatalogRecord) -> str:
    """Make ID and article identities disjoint and bounded for provider DTOs."""

    raw = record.identity_value
    prefix = f"tstn:{record.identity_kind}:"
    if len(prefix) + len(raw) <= 180:
        return prefix + raw
    return prefix + "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _failure_category(error: TstnApiError) -> ProviderFailureCategory:
    return {
        TstnErrorCategory.AUTHENTICATION: ProviderFailureCategory.AUTHENTICATION,
        TstnErrorCategory.FORBIDDEN: ProviderFailureCategory.MISCONFIGURED,
        TstnErrorCategory.INVALID_RESPONSE: ProviderFailureCategory.INVALID_RESPONSE,
        TstnErrorCategory.RESPONSE_TOO_LARGE: ProviderFailureCategory.INVALID_RESPONSE,
        TstnErrorCategory.MISCONFIGURED: ProviderFailureCategory.MISCONFIGURED,
        TstnErrorCategory.RATE_LIMITED: ProviderFailureCategory.RATE_LIMITED,
        TstnErrorCategory.REQUEST_LIMIT: ProviderFailureCategory.RATE_LIMITED,
        TstnErrorCategory.TIMEOUT: ProviderFailureCategory.TIMEOUT,
        TstnErrorCategory.TRANSPORT: ProviderFailureCategory.TRANSPORT,
        TstnErrorCategory.UNAVAILABLE: ProviderFailureCategory.UNAVAILABLE,
        TstnErrorCategory.NOT_FOUND: ProviderFailureCategory.UNAVAILABLE,
        TstnErrorCategory.REDIRECT_BLOCKED: ProviderFailureCategory.UNAVAILABLE,
        TstnErrorCategory.UNEXPECTED_STATUS: ProviderFailureCategory.UNAVAILABLE,
        TstnErrorCategory.INVALID_REQUEST: ProviderFailureCategory.MISCONFIGURED,
    }.get(error.category, ProviderFailureCategory.UNKNOWN)


class TstnCommercialExecutionAdapter:
    """Local-candidate plus filtered-price/status adapter for one explicit run.

    A fresh TN1 client is built only after local candidates exist. The same
    client and attempt observer are used for POST /user/ and every commercial
    GET, so the budget cannot be reset between operations.
    """

    key = "tstn"

    def __init__(
        self,
        mirror: TstnCatalogMirror | None,
        secret_store: SecretStore,
        *,
        enabled: bool,
        credential_generation: str,
        request_timeout_s: float,
        current_configuration: TstnCurrentConfiguration,
        verified_city_code: str | None = None,
        max_candidates: int = TSTN_MAX_CANDIDATES,
        max_http_attempts: int | None = None,
        client_factory: TstnClientFactory = create_tstn_authorization_client,
    ) -> None:
        if type(enabled) is not bool:
            raise ValueError("TSTN enabled flag must be boolean")
        if not isinstance(credential_generation, str) or not _GENERATION_RE.fullmatch(
            credential_generation,
        ):
            raise ValueError("TSTN credential generation is invalid")
        try:
            timeout = float(request_timeout_s)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("TSTN request timeout is invalid") from None
        if (
            isinstance(request_timeout_s, bool)
            or not math.isfinite(timeout)
            or not 0.1 <= timeout <= 30.0
        ):
            raise ValueError("TSTN request timeout is invalid")
        if type(max_candidates) is not int or not 1 <= max_candidates <= TSTN_MAX_CANDIDATES:
            raise ValueError("TSTN candidate limit is invalid")
        maximum_budget = 1 + (max_candidates * 2)
        if max_http_attempts is None:
            max_http_attempts = min(TSTN_MAX_HTTP_ATTEMPTS, maximum_budget)
        if type(max_http_attempts) is not int or not 1 <= max_http_attempts <= maximum_budget:
            raise ValueError("TSTN request budget is invalid")
        if not callable(current_configuration) or not callable(client_factory):
            raise ValueError("TSTN runtime callbacks are invalid")
        if verified_city_code is not None and (
            not isinstance(verified_city_code, str)
            or not verified_city_code
            or verified_city_code != verified_city_code.strip()
            or len(verified_city_code) > 80
            or any(ord(char) < 32 or ord(char) == 127 for char in verified_city_code)
        ):
            raise ValueError("TSTN verified city code is invalid")

        self.mirror = mirror
        self.secret_store = secret_store
        self.enabled = enabled
        self.credential_generation = credential_generation
        self.request_timeout_s = timeout
        self.current_configuration = current_configuration
        self.verified_city_code = verified_city_code or ""
        self.max_candidates = max_candidates
        self.max_http_attempts = max_http_attempts
        self.client_factory = client_factory

    def _catalog_revision(self) -> str:
        try:
            return _safe_revision(self.mirror.revision if self.mirror is not None else "unknown")
        except Exception:
            return "unknown"

    def _affinity(self, catalog_revision: str) -> ProviderAffinity:
        config_revision = _digest({
            "adapter_revision": TSTN_ADAPTER_REVISION,
            "enabled": self.enabled,
            "credential_generation": self.credential_generation,
            "request_timeout_s": self.request_timeout_s,
            "verified_city_code": self.verified_city_code,
            "max_candidates": self.max_candidates,
            "max_http_attempts": self.max_http_attempts,
            "catalog_revision": catalog_revision,
        })
        return ProviderAffinity(
            environment="prod",
            region_id=self.verified_city_code,
            config_revision=config_revision,
            adapter_revision=TSTN_ADAPTER_REVISION,
        )

    def request_identity(
        self,
        intent: ProductIntent,
        *,
        limit: int,
        execution_scope_id: str,
    ) -> ProviderSearchRequestIdentity:
        """Create a deterministic local-only dedupe identity."""

        revision = self._catalog_revision()
        affinity = self._affinity(revision)
        fingerprint = _digest({
            "provider_key": self.key,
            "intent": intent.model_dump(mode="json"),
            "limit": limit,
            "catalog_revision": revision,
            "credential_generation": self.credential_generation,
            "verified_city_code": self.verified_city_code,
            "adapter_revision": TSTN_ADAPTER_REVISION,
            "affinity": affinity.model_dump(mode="json"),
        })
        return ProviderSearchRequestIdentity(
            execution_scope_id=execution_scope_id,
            provider_key=self.key,
            request_fingerprint=fingerprint,
            affinity=affinity,
            limit=limit,
        )

    def _configuration_matches(self) -> bool:
        try:
            enabled, generation = self.current_configuration()
            return (
                self.enabled
                and type(enabled) is bool
                and enabled
                and generation == self.credential_generation
                and self.secret_store.get(TSTN_CREDENTIALS_CONFIGURED)
                == self.credential_generation
            )
        except Exception:
            return False

    def _failure(
        self,
        counter: ProviderRequestCounter,
        affinity: ProviderAffinity,
        category: ProviderFailureCategory,
        *,
        catalog_revision: str,
    ) -> ProviderSearchOutcome:
        return ProviderSearchOutcome(
            provider_key=self.key,
            state=ProviderSearchState.FAILURE,
            request_count=counter.request_count,
            failure_category=category,
            affinity=affinity,
            catalog_version=catalog_revision,
        )

    def _execute_call(
        self,
        method: Callable[..., Any],
        *,
        request_counter: ProviderRequestCounter,
        access_token: SecretStr,
        filter_value: str,
        filter_kind: str,
    ) -> tuple[Any | None, ProviderFailureCategory | None]:
        with TSTN_CREDENTIAL_LOCK:
            if not self._configuration_matches():
                return None, ProviderFailureCategory.MISCONFIGURED
            if request_counter.request_count >= self.max_http_attempts:
                return None, ProviderFailureCategory.RATE_LIMITED
            query: dict[str, Any] = {
                "access_token": access_token,
                "city": self.verified_city_code or None,
                filter_kind: filter_value,
            }
            try:
                response = method(**query)
            except TstnApiError as exc:
                return None, _failure_category(exc)
            except ProviderRequestLimitExceeded:
                return None, ProviderFailureCategory.RATE_LIMITED
            except Exception:
                return None, ProviderFailureCategory.UNKNOWN
            return response, None

    @staticmethod
    def _matching_price(
        response: TstnPricesResponse,
        *,
        article: str | None,
        duplicate_article: bool,
    ) -> tuple[Decimal | None, bool]:
        if not article:
            return None, False
        rows = [row for row in (response.results or ()) if row.article == article]
        if not rows:
            return None, False
        if duplicate_article:
            return None, True
        values = {row.price for row in rows}
        if len(values) != 1:
            return None, True
        value = next(iter(values))
        return (value if isinstance(value, Decimal) else None), False

    @staticmethod
    def _matching_status(
        response: TstnAvailableItemsResponse,
        *,
        article: str | None,
        duplicate_article: bool,
    ) -> tuple[str, bool]:
        if not article:
            return "", False
        rows = [row for row in (response.results or ()) if row.article == article]
        if not rows:
            return "", False
        if duplicate_article:
            return "", True
        statuses = {row.status for row in rows}
        if len(statuses) != 1:
            return "Статусы доступности неоднозначны", True
        status = next(iter(statuses))
        return (status if isinstance(status, str) else ""), False

    def _ambiguous_catalog_articles(
        self,
        candidates: tuple[TstnCatalogRecord, ...] | list[TstnCatalogRecord],
    ) -> set[str]:
        """Detect duplicate articles across the local catalog, beyond top hits.

        TSTN's commercial responses identify rows by article rather than by the
        catalog ID accepted as a request filter. Therefore a second catalog
        identity with the same article makes both price and status ambiguous,
        even when that identity was outside the bounded candidate list.
        """

        if self.mirror is None:
            raise ValueError("catalog mirror is unavailable")
        ambiguous: set[str] = set()
        for record in candidates:
            article = record.article
            if not article:
                continue
            matches = self.mirror.search(article=article, limit=2)
            if (
                not isinstance(matches, (list, tuple))
                or len(matches) > 2
                or any(not isinstance(item, TstnCatalogRecord) for item in matches)
            ):
                raise ValueError("catalog article lookup is invalid")
            identities = {
                (item.identity_kind, item.identity_value)
                for item in matches
            }
            identity = (record.identity_kind, record.identity_value)
            if identity not in identities:
                raise ValueError("catalog article lookup omitted its candidate")
            if len(identities) > 1:
                ambiguous.add(article)
        return ambiguous

    def _offer(
        self,
        record: TstnCatalogRecord,
        *,
        price: Decimal | None,
        availability_text: str,
        price_observed: bool,
        availability_observed: bool,
        catalog_revision: str,
        affinity: ProviderAffinity,
    ) -> Offer:
        source_item_id = record.id if record.id is not None else record.article or ""
        attributes: dict[str, Any] = {}
        if record.subbrand:
            attributes["subbrand"] = record.subbrand
        if record.base_unit:
            # Catalog sale unit is descriptive only; it never becomes price_unit.
            attributes["base_unit"] = record.base_unit
        if record.categories:
            attributes["categories"] = [
                {
                    "id": item.id,
                    "name": item.name,
                    "parent": item.parent,
                }
                for item in record.categories
            ]
        provenance = {
            "source": self.key,
            "identity_kind": record.identity_kind,
            "identity_value": record.identity_value,
            "source_item_id": source_item_id,
            "catalog_revision": catalog_revision,
            "price_field": "results[].price" if price_observed else "",
            "availability_field": "results[].status" if availability_observed else "",
            "city_code": self.verified_city_code,
            "config_revision": affinity.config_revision,
        }
        return Offer(
            offer_id=_offer_identity(record),
            provider=self.key,
            source_item_id=source_item_id,
            title=record.name or "",
            article=record.article or "",
            manufacturer="",
            brand=record.brand or "",
            price=price,
            currency="",
            price_unit="",
            availability=None,
            availability_text=availability_text,
            url="",
            attributes=attributes,
            retrieved_at=datetime.now(timezone.utc),
            data_provenance=provenance,
        )

    def execute_search(
        self,
        intent: ProductIntent,
        *,
        limit: int,
        request_counter: ProviderRequestCounter,
    ) -> ProviderSearchOutcome:
        """Execute one bounded search after retrieving candidates locally."""

        if (
            not isinstance(intent, ProductIntent)
            or type(limit) is not int
            or not 1 <= limit <= TSTN_MAX_SEARCH_RESULTS
            or not isinstance(request_counter, ProviderRequestCounter)
        ):
            empty_counter = request_counter if isinstance(request_counter, ProviderRequestCounter) else ProviderRequestCounter()
            revision = self._catalog_revision()
            return self._failure(
                empty_counter,
                self._affinity(revision),
                ProviderFailureCategory.MISCONFIGURED,
                catalog_revision=revision,
            )

        catalog_revision = self._catalog_revision()
        affinity = self._affinity(catalog_revision)
        if self.mirror is None:
            return self._failure(
                request_counter,
                affinity,
                ProviderFailureCategory.MISCONFIGURED,
                catalog_revision=catalog_revision,
            )
        try:
            candidates = self.mirror.search(
                intent,
                limit=min(limit, self.max_candidates),
            )
        except Exception:
            return self._failure(
                request_counter,
                affinity,
                ProviderFailureCategory.UNAVAILABLE,
                catalog_revision=catalog_revision,
            )
        if (
            not isinstance(candidates, (list, tuple))
            or len(candidates) > min(limit, self.max_candidates)
            or any(not isinstance(item, TstnCatalogRecord) for item in candidates)
        ):
            return self._failure(
                request_counter,
                affinity,
                ProviderFailureCategory.INVALID_RESPONSE,
                catalog_revision=catalog_revision,
            )
        if not candidates:
            return ProviderSearchOutcome(
                provider_key=self.key,
                state=ProviderSearchState.EMPTY,
                request_count=request_counter.request_count,
                affinity=affinity,
                catalog_version=catalog_revision,
            )

        try:
            ambiguous_catalog_articles = self._ambiguous_catalog_articles(candidates)
        except Exception:
            return self._failure(
                request_counter,
                affinity,
                ProviderFailureCategory.INVALID_RESPONSE,
                catalog_revision=catalog_revision,
            )

        try:
            client = self.client_factory(
                self.request_timeout_s,
                max_attempts=self.max_http_attempts,
                outbound_attempt_observer=request_counter.record_outbound_attempt,
            )
        except Exception:
            return self._failure(
                request_counter,
                affinity,
                ProviderFailureCategory.MISCONFIGURED,
                catalog_revision=catalog_revision,
            )
        if not isinstance(client, TstnApiClient):
            return self._failure(
                request_counter,
                affinity,
                ProviderFailureCategory.MISCONFIGURED,
                catalog_revision=catalog_revision,
            )

        authorizer = SecretStoreTstnAuthorizer(
            self.secret_store,
            client,
            enabled=self.enabled,
            credential_generation=self.credential_generation,
            current_configuration=self.current_configuration,
            credential_lock=TSTN_CREDENTIAL_LOCK,
        )
        try:
            access_token = authorizer.authorize()
        except TstnApiError as exc:
            return self._failure(
                request_counter,
                affinity,
                _failure_category(exc),
                catalog_revision=catalog_revision,
            )
        except Exception:
            return self._failure(
                request_counter,
                affinity,
                ProviderFailureCategory.UNKNOWN,
                catalog_revision=catalog_revision,
            )

        offers: list[Offer] = []
        failures: list[ProviderFailureCategory] = []
        request_limit_reached = False

        for record in candidates:
            filter_kind: str
            filter_value: str
            if record.id is not None:
                filter_kind, filter_value = "id", record.id
            elif record.article:
                filter_kind, filter_value = "article", record.article
            else:
                failures.append(ProviderFailureCategory.INVALID_RESPONSE)
                continue

            price: Decimal | None = None
            status_text = ""
            price_observed = False
            availability_observed = False
            duplicate_article = record.article in ambiguous_catalog_articles

            price_response, price_failure = self._execute_call(
                client.get_prices,
                request_counter=request_counter,
                access_token=access_token,
                filter_value=filter_value,
                filter_kind=filter_kind,
            )
            if price_failure is not None:
                if price_failure in {
                    ProviderFailureCategory.AUTHENTICATION,
                    ProviderFailureCategory.MISCONFIGURED,
                }:
                    return self._failure(
                        request_counter,
                        affinity,
                        price_failure,
                        catalog_revision=catalog_revision,
                    )
                failures.append(price_failure)
                if price_failure == ProviderFailureCategory.RATE_LIMITED:
                    request_limit_reached = True
            elif not isinstance(price_response, TstnPricesResponse):
                failures.append(ProviderFailureCategory.INVALID_RESPONSE)
            else:
                price_rows = price_response.results or ()
                if price_rows and any(
                    not record.article or row.article != record.article
                    for row in price_rows
                ):
                    failures.append(ProviderFailureCategory.INVALID_RESPONSE)
                price, ambiguous_price = self._matching_price(
                    price_response,
                    article=record.article,
                    duplicate_article=duplicate_article,
                )
                price_observed = price is not None
                if ambiguous_price:
                    failures.append(ProviderFailureCategory.INVALID_RESPONSE)

            if request_limit_reached:
                offers.append(self._offer(
                    record,
                    price=price,
                    availability_text=status_text,
                    price_observed=price_observed,
                    availability_observed=availability_observed,
                    catalog_revision=catalog_revision,
                    affinity=affinity,
                ))
                break

            available_response, available_failure = self._execute_call(
                client.get_available,
                request_counter=request_counter,
                access_token=access_token,
                filter_value=filter_value,
                filter_kind=filter_kind,
            )
            if available_failure is not None:
                if available_failure in {
                    ProviderFailureCategory.AUTHENTICATION,
                    ProviderFailureCategory.MISCONFIGURED,
                }:
                    return self._failure(
                        request_counter,
                        affinity,
                        available_failure,
                        catalog_revision=catalog_revision,
                    )
                failures.append(available_failure)
                if available_failure == ProviderFailureCategory.RATE_LIMITED:
                    request_limit_reached = True
            elif not isinstance(available_response, TstnAvailableItemsResponse):
                failures.append(ProviderFailureCategory.INVALID_RESPONSE)
            else:
                availability_rows = available_response.results or ()
                if availability_rows and any(
                    not record.article or row.article != record.article
                    for row in availability_rows
                ):
                    failures.append(ProviderFailureCategory.INVALID_RESPONSE)
                status_text, ambiguous_status = self._matching_status(
                    available_response,
                    article=record.article,
                    duplicate_article=duplicate_article,
                )
                availability_observed = bool(status_text) and not ambiguous_status
                if ambiguous_status:
                    failures.append(ProviderFailureCategory.INVALID_RESPONSE)

            offers.append(self._offer(
                record,
                price=price,
                availability_text=status_text,
                price_observed=price_observed,
                availability_observed=availability_observed,
                catalog_revision=catalog_revision,
                affinity=affinity,
            ))
            if request_limit_reached:
                break

        if not offers:
            category = failures[0] if failures else ProviderFailureCategory.INVALID_RESPONSE
            return self._failure(
                request_counter,
                affinity,
                category,
                catalog_revision=catalog_revision,
            )
        if failures:
            # Deterministically select one safe aggregate category.
            priority = (
                ProviderFailureCategory.MISCONFIGURED,
                ProviderFailureCategory.RATE_LIMITED,
                ProviderFailureCategory.TIMEOUT,
                ProviderFailureCategory.TRANSPORT,
                ProviderFailureCategory.AUTHENTICATION,
                ProviderFailureCategory.INVALID_RESPONSE,
                ProviderFailureCategory.UNAVAILABLE,
                ProviderFailureCategory.UNKNOWN,
            )
            category = next((item for item in priority if item in failures), failures[0])
            return ProviderSearchOutcome(
                provider_key=self.key,
                state=ProviderSearchState.PARTIAL_SUCCESS,
                offers=tuple(offers),
                request_count=request_counter.request_count,
                failure_category=category,
                affinity=affinity,
                catalog_version=catalog_revision,
            )
        return ProviderSearchOutcome(
            provider_key=self.key,
            state=ProviderSearchState.SUCCESS,
            offers=tuple(offers),
            request_count=request_counter.request_count,
            affinity=affinity,
            catalog_version=catalog_revision,
        )
