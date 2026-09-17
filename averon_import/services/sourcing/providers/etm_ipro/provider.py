from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from averon_import.services.app_settings import EtmIproSettings
from averon_import.services.secrets import ETM_IPRO_LOGIN, ETM_IPRO_PASSWORD, SecretStore, resolve_secret
from averon_import.services.sourcing.models import (
    Offer,
    ProductIntent,
    SourcingProviderCapabilities,
    SourcingProviderRuntimeState,
)
from averon_import.services.sourcing.providers.base import SourcingProviderCachePolicy, SourcingProviderError

from .client import EtmIproClient
from .mirror import EtmCatalogMirror, EtmCatalogSyncResult, EtmJobStatus
from .models import EtmCatalogRecord


class EtmIproProvider:
    key = "etm_ipro"
    label = "ЭТМ iPRO"
    cache_policy = SourcingProviderCachePolicy(cache_search_results=False)
    capabilities = SourcingProviderCapabilities(
        supports_price=True,
        supports_availability=True,
        supports_product_url=False,
        supports_article_search=True,
        supports_model_search=True,
        supports_batch_search=False,
        supports_catalog_version=True,
        supports_stock_quantity=True,
    )

    def __init__(
        self,
        settings: EtmIproSettings,
        secret_store: SecretStore,
        data_dir: Path,
        *,
        client: EtmIproClient | None = None,
        mirror: EtmCatalogMirror | None = None,
    ) -> None:
        self.settings = settings
        data_root = Path(data_dir)
        self.mirror = mirror or EtmCatalogMirror(
            data_root / "sourcing" / "providers" / "etm_ipro" / "catalog.sqlite3"
        )
        login = resolve_secret(os.environ.get("AVERON_ETM_IPRO_LOGIN"), secret_store, ETM_IPRO_LOGIN)
        password = resolve_secret(os.environ.get("AVERON_ETM_IPRO_PASSWORD"), secret_store, ETM_IPRO_PASSWORD)
        self.client = client or EtmIproClient(settings, login, password)

    @property
    def configured(self) -> bool:
        return bool(self.settings.enabled and self.client.configured)

    def stats(self) -> SourcingProviderRuntimeState:
        item_count = self.mirror.count()
        if not self.settings.enabled or not self.client.configured:
            return SourcingProviderRuntimeState(
                configured=False,
                reachable=False,
                item_count=item_count,
                catalog_version=self.mirror.revision,
                error="ЭТМ iPRO не настроен: укажите логин и пароль",
            )
        if not self.mirror.has_content() and self.mirror.manufacturer_count() == 0:
            return SourcingProviderRuntimeState(
                configured=True,
                reachable=False,
                item_count=0,
                catalog_version=self.mirror.revision,
                error="ЭТМ iPRO не готов: нет локального каталога или справочника производителей",
            )
        return SourcingProviderRuntimeState(
            configured=True,
            reachable=True,
            item_count=item_count,
            catalog_version=self.mirror.revision,
        )

    def health(self) -> dict[str, Any]:
        if not self.configured:
            return {"configured": False, "reachable": False, "catalog_version": self.mirror.revision}
        try:
            self.client.check_access()
        except SourcingProviderError as exc:
            return {
                "configured": True,
                "reachable": False,
                "catalog_version": self.mirror.revision,
                "error": exc.public_message,
            }
        return {
            "configured": True,
            "reachable": True,
            "catalog_version": self.mirror.revision,
            "item_count": self.mirror.count(),
        }

    def sync_manufacturers(self) -> int:
        self._ensure_configured()
        return self.mirror.sync_manufacturers(self.client.get_manufacturers())

    def manufacturer_status(self) -> dict[str, Any]:
        return {"count": self.mirror.manufacturer_count()}

    def start_catalog_sync(self) -> EtmJobStatus:
        self._ensure_configured()
        return self.mirror.create_job(self.client)

    def catalog_sync_status(self) -> EtmJobStatus:
        self._ensure_configured()
        return self.mirror.update_job(self.client)

    def search(self, intent: ProductIntent, *, limit: int = 20) -> list[Offer]:
        self._ensure_configured()
        requested_limit = max(1, min(int(limit), 100))
        live_limit = min(requested_limit, int(self.settings.max_live_candidates))
        mirror_available = self.mirror.has_content()
        direct_goods: list[dict[str, Any]] = []
        manufacturer_code = None
        if intent.article and intent.manufacturer:
            manufacturer_code = self.mirror.resolve_manufacturer(intent.manufacturer)
            if manufacturer_code:
                payload = self.client.get_goods(
                    intent.article,
                    lookup_type="mnf",
                    manufacturer_code=manufacturer_code,
                )
                direct_goods = _goods_rows(payload)[:live_limit]
        records = (
            self.mirror.search(
                intent,
                limit=requested_limit,
                manufacturer_code=manufacturer_code,
            )
            if mirror_available
            else []
        )
        goods_by_id: dict[str, dict[str, Any]] = {}
        for raw in direct_goods:
            if len(goods_by_id) >= live_limit:
                break
            parsed = _goods_record(raw)
            if parsed is not None:
                goods_by_id[parsed.source_item_id] = raw
        for record in records:
            if len(goods_by_id) >= live_limit:
                break
            goods_by_id.setdefault(record.source_item_id, {})
        if not goods_by_id:
            if not mirror_available:
                raise SourcingProviderError(
                    "ЭТМ iPRO не может выполнить поиск: локальный каталог не синхронизирован",
                    code="CATALOG_NOT_SYNCED",
                    category="not_configured",
                )
            return []
        details: list[tuple[str, dict[str, Any], EtmCatalogRecord | None]] = []
        record_by_id = {record.source_item_id: record for record in records}
        for source_item_id, raw in goods_by_id.items():
            details.append((source_item_id, raw, record_by_id.get(source_item_id)))
        source_ids = [item[0] for item in details]
        prices = self.client.get_prices(source_ids)
        offers: list[Offer] = []
        for source_item_id, raw, catalog_record in details:
            if not raw:
                raw = self.client.get_goods(source_item_id)
            goods = _goods_record(raw)
            if goods is None:
                goods = catalog_record
            if goods is None:
                continue
            remains = self.client.get_remains(source_item_id)
            offers.append(self._offer(goods, _price_row(prices, source_item_id), remains, raw))
        return offers

    def _ensure_configured(self) -> None:
        if not self.configured:
            raise SourcingProviderError(
                "ЭТМ iPRO не настроен: укажите логин и пароль",
                code="NOT_CONFIGURED",
                category="not_configured",
            )

    def _offer(
        self,
        goods: EtmCatalogRecord,
        price: dict[str, Any] | None,
        remains: Any,
        raw_detail: dict[str, Any],
    ) -> Offer:
        price_value = _commercial_price(price)
        stock_records, availability, availability_text = _stock_summary(
            remains, self.settings.warehouse_codes
        )
        price_status = _price_status(price, price_value)
        if price_status == "requires_individual_request":
            price_text = "Цена уточняется индивидуально"
        elif price_status == "unknown":
            price_text = "Цена не получена"
        else:
            price_text = ""
        if price_text and availability_text:
            availability_text = f"{availability_text}; {price_text}"
        elif price_text:
            availability_text = price_text
        return Offer(
            offer_id=f"{self.key}:{goods.source_item_id}",
            provider=self.key,
            source_item_id=goods.source_item_id,
            title=goods.name or goods.article or goods.source_item_id,
            article=goods.article,
            manufacturer=goods.brand,
            brand=goods.brand,
            price=price_value,
            currency="RUB" if price_value is not None else "",
            price_unit=_text(goods_source(raw_detail, "edizm")),
            availability=availability,
            availability_text=availability_text,
            url="",
            attributes={
                "unit": _text(goods_source(raw_detail, "edizm")),
                "min_cnt": goods_source(raw_detail, "min_cnt"),
                "params": goods_source(raw_detail, "gdsChars") or {},
                "class_tree": goods_source(raw_detail, "gdsClassTree") or [],
                "packs": goods_source(raw_detail, "gdsPacks") or [],
                "images": _images(goods_source(raw_detail, "gdsImages")),
                "remains": stock_records,
                "supplier_stores": _detail_value(remains, "InfoSuppStores", "supplier_stores"),
                "forecast": _detail_value(remains, "InfoForecast", "forecast"),
                "delivery": _detail_value(remains, "InforDeliveryTime", "delivery", "delivery_time"),
            },
            data_provenance={
                "source": self.key,
                "catalog_version": self.mirror.revision,
                "source_item_id": goods.source_item_id,
                "price_field": "pricewnds" if price_value is not None else "",
                "price_status": price_status,
            },
        )


def _goods_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("rows", "goods", "items", "products"):
            if isinstance(data.get(key), list):
                return [item for item in data[key] if isinstance(item, dict)]
        if any(key in data for key in ("gdscode", "id", "code", "source_item_id")):
            return [data]
    return []


def _goods_record(raw: Any) -> EtmCatalogRecord | None:
    if isinstance(raw, EtmCatalogRecord):
        return raw
    if not isinstance(raw, dict):
        return None
    try:
        return EtmCatalogRecord.model_validate(raw)
    except Exception:
        return None


def goods_source(goods: Any, key: str) -> Any:
    if not isinstance(goods, dict):
        return getattr(goods, key, None)
    return goods.get(key)


def _detail_value(payload: Any, *keys: str) -> Any:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return None
    for key in keys:
        if key in data:
            return data[key]
    return None


def _price_row(payload: Any, source_item_id: str) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data", payload)
    rows: list[Any]
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("prices", data.get("goods", data.get("items", [data])))
    else:
        rows = []
    if not isinstance(rows, list):
        rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        identifier = str(row.get("gdscode", row.get("id", row.get("code", source_item_id)))).strip()
        if identifier == source_item_id or len(rows) == 1:
            return row
    return None


def _commercial_price(row: dict[str, Any] | None) -> Decimal | None:
    if not row:
        return None
    values = [row.get(name) for name in ("pricewnds", "price", "price_tarif", "price_retail")]
    parsed: list[Decimal] = []
    for value in values:
        try:
            parsed.append(Decimal(str(value or "0").replace(",", ".")))
        except (InvalidOperation, TypeError, ValueError):
            parsed.append(Decimal("0"))
    if all(value == 0 for value in parsed):
        return None
    value = parsed[0]
    return value if value.is_finite() and value > 0 else None


def _price_status(row: dict[str, Any] | None, price: Decimal | None) -> str:
    if not row:
        return ""
    raw = row.get("pricewnds")
    try:
        commercial = Decimal(str(raw).replace(",", "."))
    except (InvalidOperation, TypeError, ValueError):
        return "unknown"
    if not commercial.is_finite():
        return "unknown"
    if price is not None:
        return ""
    if commercial == 0:
        other_values = (row.get("price"), row.get("price_tarif"), row.get("price_retail"))
        try:
            parsed = [Decimal(str(value or "0").replace(",", ".")) for value in other_values]
        except (InvalidOperation, TypeError, ValueError):
            return "unknown"
        if all(value == 0 for value in parsed):
            return "requires_individual_request"
    return "unknown"


def _stock_summary(payload: Any, configured_codes: list[str]) -> tuple[list[dict[str, Any]], bool | None, str]:
    if not isinstance(payload, dict):
        return [], None, "Наличие уточняется"
    data = payload.get("data", payload)
    if isinstance(data, dict):
        if "InfoStores" in data:
            rows = data.get("InfoStores")
        else:
            rows = data.get("stores", data.get("remains", data.get("items", [data])))
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    wanted = {str(code).strip() for code in configured_codes if str(code).strip()}
    selected: list[dict[str, Any]] = []
    known_values: list[Decimal] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("StoreCode", raw.get("store_code", raw.get("storeCode", raw.get("code", raw.get("store", "")))))).strip()
        is_open = raw.get("open", raw.get("is_open", raw.get("isOpen", True)))
        if is_open is False or (wanted and code not in wanted):
            continue
        selected_row = dict(raw)
        value = raw.get("StoreQuantRem", raw.get("stock", raw.get("quantity", raw.get("amount", raw.get("remain")))))
        if code and "store_code" not in selected_row:
            selected_row["store_code"] = code
        if value is not None and "stock" not in selected_row:
            selected_row["stock"] = value
        selected.append(selected_row)
        try:
            number = Decimal(str(value).replace(",", "."))
            if number.is_finite():
                known_values.append(number)
        except (InvalidOperation, TypeError, ValueError):
            pass
    if not selected or not wanted:
        return selected, None, "Наличие уточняется"
    selected_codes = {
        str(row.get("StoreCode", row.get("store_code", ""))).strip()
        for row in selected
    }
    if not wanted.issubset(selected_codes):
        return selected, None, "Наличие уточняется"
    if any(value > 0 for value in known_values):
        return selected, True, "В наличии на выбранном складе"
    if known_values and len(known_values) == len(selected):
        return selected, False, "Нет в наличии на выбранном складе"
    return selected, None, "Наличие уточняется"


def _text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def _images(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    result: list[Any] = []
    for item in value:
        if isinstance(item, str):
            value = item.strip()
            if not value:
                continue
            result.append(value if value.startswith(("http://", "https://")) else "https://cdn.etm.ru/" + value.lstrip("/"))
        elif isinstance(item, dict):
            copied = dict(item)
            for field_name in ("gdsImgSrc", "gdsImgRef"):
                original = copied.get(field_name)
                if isinstance(original, str) and original.strip():
                    copied[f"source_{field_name}"] = original
                    copied[field_name] = _cdn_url(original)
            raw_url = copied.get("url", copied.get("path", copied.get("src")))
            if isinstance(raw_url, str) and raw_url.strip() and not raw_url.startswith(("http://", "https://")):
                copied["url"] = _cdn_url(raw_url)
            result.append(copied)
    return result


def _cdn_url(value: str) -> str:
    value = value.strip()
    return value if value.startswith(("http://", "https://")) else "https://cdn.etm.ru/" + value.lstrip("/")
