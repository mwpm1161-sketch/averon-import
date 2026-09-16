from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from averon_import.services.app_settings import LemanaB2BSettings
from averon_import.services.secrets import (
    LEMANA_B2B_CLIENT_SECRET,
    SecretStore,
    resolve_secret,
)
from averon_import.services.sourcing.models import (
    Offer,
    ProductIntent,
    SourcingProviderCapabilities,
    SourcingProviderRuntimeState,
)
from averon_import.services.sourcing.providers.base import SourcingProviderError

from .client import LemanaB2BClient
from .mirror import LemanaCatalogMirror, LemanaMirrorSyncResult


class LemanaB2BProvider:
    key = "lemana_b2b"
    label = "Лемана ПРО B2B"
    capabilities = SourcingProviderCapabilities(
        supports_price=True,
        supports_availability=True,
        supports_product_url=True,
        supports_article_search=True,
        supports_model_search=True,
        supports_batch_search=False,
        supports_catalog_version=True,
        supports_stock_quantity=False,
    )

    def __init__(
        self,
        settings: LemanaB2BSettings,
        secret_store: SecretStore,
        data_dir,
        *,
        client: LemanaB2BClient | None = None,
        mirror: LemanaCatalogMirror | None = None,
    ) -> None:
        self.settings = settings
        data_dir = Path(data_dir)
        self.mirror = mirror or LemanaCatalogMirror(
            data_dir / "sourcing" / "providers" / "lemana" / "catalog.sqlite3"
        )
        secret = resolve_secret(
            os.environ.get("AVERON_LEMANA_B2B_CLIENT_SECRET"),
            secret_store,
            LEMANA_B2B_CLIENT_SECRET,
        )
        self.client = client or LemanaB2BClient(settings, secret)

    @property
    def configured(self) -> bool:
        return self.client.configured

    def stats(self) -> SourcingProviderRuntimeState:
        item_count = self.mirror.count()
        revision = self.mirror.revision
        if not self.configured:
            return SourcingProviderRuntimeState(
                configured=False,
                reachable=False,
                item_count=item_count,
                catalog_version=revision,
                error="Лемана ПРО B2B не настроен",
            )
        if not self.mirror.has_content():
            return SourcingProviderRuntimeState(
                configured=True,
                reachable=False,
                item_count=0,
                catalog_version=revision,
                error="Локальное зеркало Лемана ПРО B2B ещё не синхронизировано",
            )
        try:
            self.client.check_access()
        except SourcingProviderError as exc:
            return SourcingProviderRuntimeState(
                configured=True,
                reachable=False,
                item_count=item_count,
                catalog_version=revision,
                error=exc.public_message,
            )
        except Exception:
            return SourcingProviderRuntimeState(
                configured=True,
                reachable=False,
                item_count=item_count,
                catalog_version=revision,
                error="Проверка каталога Lemana PRO B2B не выполнена",
            )
        return SourcingProviderRuntimeState(
            configured=True,
            reachable=True,
            item_count=item_count,
            catalog_version=revision,
        )

    def sync(self) -> LemanaMirrorSyncResult:
        self._ensure_configured()
        assert self.settings.region_id is not None
        return self.mirror.sync(
            self.client,
            region_id=self.settings.region_id,
            per_page=100,
        )

    def search(self, intent: ProductIntent, *, limit: int = 20) -> list[Offer]:
        self._ensure_configured()
        if not self.mirror.has_content():
            raise SourcingProviderError(
                "Локальное зеркало Лемана ПРО B2B ещё не синхронизировано",
                code="CATALOG_NOT_SYNCED",
                category="not_configured",
            )
        assert self.settings.region_id is not None
        products = self.mirror.search(intent, limit=max(1, min(int(limit), 100)))
        if not products:
            return []
        prices = self.client.get_prices(
            [product.product_item for product in products],
            region_id=self.settings.region_id,
        )
        price_by_item = {price.product_item: price for price in prices}
        return [self._offer(product, price_by_item.get(product.product_item)) for product in products]

    def _ensure_configured(self) -> None:
        if not self.configured:
            raise SourcingProviderError(
                "Лемана ПРО B2B не настроен",
                code="NOT_CONFIGURED",
                category="not_configured",
            )
        if self.settings.region_id is None:
            raise SourcingProviderError(
                "Для Лемана ПРО B2B не указан регион",
                code="NOT_CONFIGURED",
                category="not_configured",
            )

    def _offer(self, product, price) -> Offer:
        price_value = price.price if price is not None else None
        currency = _currency(price.currency) if price is not None else ""
        availability = product.product_available
        if availability is None and price is not None and price_value is not None:
            availability = True
        availability_text = ""
        if availability is True:
            availability_text = "В наличии"
        elif availability is False:
            availability_text = "Нет в наличии"
        return Offer(
            offer_id=f"{self.key}:{product.product_item}",
            provider=self.key,
            source_item_id=product.product_item,
            title=product.product_name or product.product_model or product.product_item,
            article=product.product_item,
            brand=product.product_brand,
            price=price_value,
            currency=currency,
            price_unit=_unit(product.product_unit_sale),
            availability=availability,
            availability_text=availability_text,
            url=_safe_url(product.product_url),
            attributes={
                "model": product.product_model,
                "description": product.product_description,
                "barcode": product.product_barcode,
                "params": product.product_params,
                "categories": product.categories,
                "photo": product.product_photo,
            },
            data_provenance={
                "source": self.key,
                "product_item": product.product_item,
                "mirror_revision": self.mirror.revision,
                "region_id": self.settings.region_id,
            },
        )


def _currency(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    return "RUB" if normalized in {"RUB", "RUR", "РУБ", "РУБ."} else normalized


def _unit(value: Any) -> str:
    if value is None:
        return "шт."
    if isinstance(value, dict):
        value = value.get("name") or value.get("unit") or value.get("code")
    if isinstance(value, (dict, list)):
        return "шт."
    return str(value).strip() or "шт."


def _safe_url(value: Any) -> str:
    normalized = str(value or "").strip()
    parsed = urlsplit(normalized)
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    return normalized
