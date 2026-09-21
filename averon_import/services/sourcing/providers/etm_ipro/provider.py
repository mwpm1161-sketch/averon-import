from __future__ import annotations

import os
import re
import tempfile
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

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
from .mirror import (
    EtmCatalogMirror,
    EtmCatalogSyncResult,
    EtmJobStatus,
    EtmSearchIndexResult,
)
from .models import EtmCatalogRecord


def _etm_product_url(source_item_id: Any) -> str:
    value = "" if source_item_id is None else str(source_item_id).strip()
    if not value or re.fullmatch(r"[0-9]+", value) is None:
        return ""
    return f"https://www.etm.ru/cat/nn/{value}"


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
            return {
                "configured": False,
                "reachable": False,
                "catalog_version": self.mirror.revision,
                "search_index_ready": self.mirror.search_index_ready,
            }
        try:
            self.client.check_access()
        except SourcingProviderError as exc:
            return {
                "configured": True,
                "reachable": False,
                "catalog_version": self.mirror.revision,
                "search_index_ready": self.mirror.search_index_ready,
                "error": exc.public_message,
            }
        return {
            "configured": True,
            "reachable": True,
            "catalog_version": self.mirror.revision,
            "item_count": self.mirror.count(),
            "search_index_ready": self.mirror.search_index_ready,
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

    def import_completed_catalog(
        self,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> EtmCatalogSyncResult:
        """Import the already-completed catalog without polling ETM again."""

        self._ensure_configured()
        if self.mirror.job_state != 1:
            raise SourcingProviderError(
                "Каталог ЭТМ iPRO ещё не готов к импорту",
                code="CATALOG_NOT_READY",
                category="invalid_request",
            )
        snapshot_url = self.mirror.job_url
        if not snapshot_url:
            raise SourcingProviderError(
                "ЭТМ iPRO не вернул файл каталога",
                code="INVALID_CATALOG",
                category="invalid_response",
            )
        handle, temporary_path = tempfile.mkstemp(
            prefix="etm-snapshot-",
            suffix=".json",
            dir=self.mirror.path.parent,
        )
        os.close(handle)
        path = Path(temporary_path)
        try:
            download = self.client.download_snapshot_to_file(
                snapshot_url,
                path,
                progress=progress,
            )
            return self.mirror.import_snapshot_file(
                download.path,
                snapshot_sha256=download.sha256,
                progress=progress,
            )
        except SourcingProviderError:
            raise
        except Exception as exc:
            raise SourcingProviderError(
                "Локальный каталог ЭТМ iPRO не обновлён",
                code="MIRROR_IMPORT_FAILED",
                category="storage_error",
            ) from exc
        finally:
            path.unlink(missing_ok=True)

    def rebuild_search_index(
        self,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> EtmSearchIndexResult:
        """Rebuild only the local ETM search index; no upstream calls."""

        return self.mirror.rebuild_search_index(progress=progress)

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
                raw = _goods_row_for_source_item(
                    self.client.get_goods(source_item_id),
                    source_item_id,
                ) or {}
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
        detail = _goods_detail(raw_detail)
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
        attributes = {
            "unit": _text(goods_source(raw_detail, "edizm")),
            "min_cnt": goods_source(raw_detail, "min_cnt"),
            "params_raw": detail["params_raw"],
            "params": detail["params"],
            "class_tree": detail["class_tree"],
            "packs": detail["packs"],
            "certificates": detail["certificates"],
            "videos": detail["videos"],
            "country": detail["country"],
            "min_pack": detail["min_pack"],
            "info_packs": detail["info_packs"],
            "images_raw": detail["images_raw"],
            "images": detail["images"],
            "remains": stock_records,
            "supplier_stores": _detail_value(remains, "InfoSuppStores", "supplier_stores"),
            "forecast": _detail_value(remains, "InfoForecast", "forecast"),
            "delivery": _detail_value(remains, "InforDeliveryTime", "delivery", "delivery_time"),
        }
        attributes.update(detail["canonical"])
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
            url=_etm_product_url(goods.source_item_id),
            attributes=attributes,
            data_provenance={
                "source": self.key,
                "catalog_version": self.mirror.revision,
                "source_item_id": goods.source_item_id,
                "price_field": "pricewnds" if price_value is not None else "",
                "price_status": price_status,
            },
        )


def _goods_detail(raw_row: Any) -> dict[str, Any]:
    row = raw_row if isinstance(raw_row, dict) else {}
    card = row.get("add_info_card")
    card = card if isinstance(card, dict) else {}

    def field(name: str, default: Any) -> Any:
        if name in card:
            return deepcopy(card[name])
        return deepcopy(row.get(name, default))

    params_raw = field("gdsChars", {})
    images_raw: list[Any] = []
    if row.get("image") not in (None, ""):
        images_raw.append(deepcopy(row["image"]))
    nested_images = field("gdsImages", [])
    if isinstance(nested_images, list):
        images_raw.extend(nested_images)
    return {
        "params_raw": params_raw,
        "params": _characteristic_map(params_raw),
        "canonical": _canonical_characteristics(params_raw),
        "class_tree": field("gdsClassTree", []),
        "packs": field("gdsPacks", []),
        "certificates": field("certificates", []),
        "videos": field("gdsVideos", []),
        "country": field("gdsNameCountry", ""),
        "min_pack": field("minPack", ""),
        "info_packs": field("gdsInfoPacks", ""),
        "images_raw": images_raw,
        "images": _images(images_raw),
    }


def _characteristic_entries(raw: Any) -> list[tuple[str, Any, dict[str, Any] | None]]:
    if isinstance(raw, dict):
        return [(str(name).strip(), value, None) for name, value in raw.items() if str(name).strip()]
    if not isinstance(raw, list):
        return []
    result: list[tuple[str, Any, dict[str, Any] | None]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("gdsCharName", item.get("name", item.get("label", "")))
        value = item.get("gdsCharVal", item.get("value", item.get("val")))
        name_text = str(name or "").strip()
        if name_text:
            result.append((name_text, value, item))
    return result


def _characteristic_map(raw: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value, _item in _characteristic_entries(raw):
        if name not in result:
            result[name] = deepcopy(value)
        elif isinstance(result[name], list):
            result[name].append(deepcopy(value))
        else:
            result[name] = [result[name], deepcopy(value)]
    return result


def _canonical_characteristics(raw: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    conflicted: set[str] = set()

    def put(key: str, value: Any) -> None:
        if key in conflicted:
            return
        if key not in result:
            result[key] = value
        elif result[key] != value:
            result.pop(key, None)
            conflicted.add(key)

    for name, value, item in _characteristic_entries(raw):
        base_label, label_unit = _split_characteristic_label(name)
        explicit_unit = ""
        if item:
            explicit_unit = _text(
                item.get("gdsCharUnit", item.get("unit", item.get("measure", "")))
            )
        unit = _normalize_unit(explicit_unit or label_unit)
        label = _normalize_label(base_label)
        number = _numeric_value(value)
        if label in {"напряжение", "voltage"} and unit in {"в", "v"} and number is not None:
            put("voltage", number)
        elif label in {"номинальныйток", "ток", "current"} and unit in {"а", "a"} and number is not None:
            put("current", number)
        elif label in {"степеньзащиты", "protectionclass"} and re.fullmatch(r"IP\s*\d+[A-ZА-ЯЁ0-9-]*", str(value or "").strip(), re.IGNORECASE):
            put("protection_class", str(value).strip())
        elif label in {"способмонтажа", "mountingmethod"} and _text(value):
            put("mounting_type", _text(value))
        elif label in {"мощность", "power"} and unit in {"вт", "w", "квт", "kw"} and number is not None:
            put("power", number / 1000 if unit in {"вт", "w"} else number)
        elif label in {"диаметр", "diameter"} and unit in {"мм", "mm"} and number is not None:
            put("diameter", number)
        elif label in {"материал", "material"} and _text(value):
            put("material", _text(value))
    return result


def _split_characteristic_label(value: Any) -> tuple[str, str]:
    text = _text(value)
    if "," in text:
        base, unit = text.split(",", 1)
        return base.strip(), unit.strip()
    match = re.match(r"^(.*?)[(]\s*([^()]+?)\s*[)]$", text)
    return (match.group(1).strip(), match.group(2).strip()) if match else (text, "")


def _normalize_label(value: Any) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", "", _text(value).casefold())


def _normalize_unit(value: Any) -> str:
    return _normalize_label(value).replace("ё", "е")


def _numeric_value(value: Any) -> int | float | None:
    match = re.search(r"[-+]?\d+(?:[.,]\d+)?", _text(value))
    if not match:
        return None
    number = float(match.group(0).replace(",", "."))
    return int(number) if number.is_integer() else number


def _goods_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return _normalize_goods_rows(payload)
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, list):
        return _normalize_goods_rows(data)
    if isinstance(data, dict):
        for key in ("rows", "goods", "items", "products"):
            if isinstance(data.get(key), list):
                return _normalize_goods_rows(data[key])
        if _has_goods_identity(data):
            normalized = _normalize_goods_row(data)
            return [normalized] if normalized is not None else []
    return []


def _goods_row_for_source_item(payload: Any, source_item_id: str) -> dict[str, Any] | None:
    requested = str(source_item_id or "").strip()
    if not requested:
        return None
    for row in _goods_rows(payload):
        if str(row.get("gdscode") or "").strip() == requested:
            return row
    return None


def _normalize_goods_rows(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    normalized: list[dict[str, Any]] = []
    for raw in rows:
        row = _normalize_goods_row(raw)
        if row is not None:
            normalized.append(row)
    return normalized


def _has_goods_identity(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    return any(raw.get(key) not in (None, "") for key in (
        "gdsCode", "gdscode", "code", "source_item_id", "id"
    ))


def _normalize_goods_row(raw: Any) -> dict[str, Any] | None:
    if not _has_goods_identity(raw):
        return None
    row = deepcopy(raw)

    def first_value(*keys: str) -> Any:
        for key in keys:
            value = raw.get(key)
            if value not in (None, ""):
                return value
        return None

    aliases = {
        "gdscode": ("gdsCode", "gdscode", "code", "source_item_id", "id"),
        "name": ("gdsNameTitle", "name", "title", "gdsNameInMnf"),
        "mnf_name": ("gdsMnfName", "mnf_name", "brand", "manufacturer"),
        "mnf_code": ("gdsMnfCode", "mnf_code", "brand_code", "mnf"),
        "art": ("gdsArt", "art", "article"),
        "edizm": ("gdsUnitName", "edizm", "unit"),
    }
    for canonical, keys in aliases.items():
        value = first_value(*keys)
        if value is not None:
            row[canonical] = value

    if (
        raw.get("gdsNameTitle") in (None, "")
        and first_value("name", "title") is None
        and raw.get("gdsNameInMnf") not in (None, "")
    ):
        row["name"] = raw["gdsNameInMnf"]
    return row


def _goods_record(raw: Any) -> EtmCatalogRecord | None:
    if isinstance(raw, EtmCatalogRecord):
        return raw
    normalized = _normalize_goods_row(raw)
    if normalized is None:
        return None
    try:
        return EtmCatalogRecord.model_validate(normalized)
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
        rows = data.get("rows")
        if not isinstance(rows, list):
            rows = data.get("prices", data.get("goods", data.get("items", [data])))
    else:
        rows = []
    if not isinstance(rows, list):
        rows = []
    rows = [row for row in rows if isinstance(row, dict)]
    target_id = str(source_item_id).strip()
    unidentifiable_rows: list[dict[str, Any]] = []
    for row in rows:
        identifiers = {
            str(row[key]).strip()
            for key in ("gdscode", "id", "code")
            if row.get(key) not in (None, "")
        }
        if not identifiers:
            unidentifiable_rows.append(row)
            continue
        if identifiers == {target_id}:
            return row
    if len(rows) == 1 and len(unidentifiable_rows) == 1:
        return unidentifiable_rows[0]
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
    seen_urls: set[str] = set()
    for item in value:
        if isinstance(item, str):
            value = item.strip()
            if not value:
                continue
            normalized = _cdn_url(value)
            if normalized and normalized not in seen_urls:
                result.append(normalized)
                seen_urls.add(normalized)
        elif isinstance(item, dict):
            copied = dict(item)
            normalized_urls: list[str] = []
            for field_name in ("gdsImgSrc", "gdsImgRef"):
                original = copied.get(field_name)
                if isinstance(original, str) and original.strip():
                    copied[f"source_{field_name}"] = original
                    normalized = _cdn_url(original)
                    if normalized:
                        copied[field_name] = normalized
                        normalized_urls.append(normalized)
                    else:
                        copied.pop(field_name, None)
            raw_url = copied.get("url", copied.get("path", copied.get("src")))
            if isinstance(raw_url, str) and raw_url.strip():
                copied.setdefault("source_url", raw_url)
                normalized = _cdn_url(raw_url)
                if normalized:
                    copied["url"] = normalized
                    normalized_urls.append(normalized)
                elif "url" in copied:
                    copied.pop("url", None)
            if not normalized_urls or any(url in seen_urls for url in normalized_urls):
                continue
            result.append(copied)
            seen_urls.update(normalized_urls)
    return result


def _cdn_url(value: str) -> str:
    value = value.strip()
    if not value or value.startswith("//"):
        return ""
    if value.startswith(("/", "ipro/")) or "://" not in value:
        return "https://cdn.etm.ru/" + value.lstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme == "https" and parsed.hostname and parsed.hostname.casefold() == "cdn.etm.ru":
        return value
    return ""
