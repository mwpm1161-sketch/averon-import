from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, AliasChoices, field_validator


class EtmCatalogRecord(BaseModel):
    """The deliberately small, documented SgGds discovery record."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    source_item_id: str = Field(
        validation_alias=AliasChoices("gdscode", "id", "source_item_id", "code")
    )
    name: str = Field(default="", validation_alias=AliasChoices("name", "title"))
    brand: str = Field(default="", validation_alias=AliasChoices("brand", "mnf_name", "manufacturer"))
    article: str = Field(default="", validation_alias=AliasChoices("article", "art"))
    brand_code: str = Field(default="", validation_alias=AliasChoices("brand_code", "mnf", "mnf_code"))
    cli_code: str = Field(default="", validation_alias=AliasChoices("cli_code", "cli"))
    class_code: str = Field(default="", validation_alias=AliasChoices("class_code", "classCode"))
    product_class: str = Field(default="", validation_alias=AliasChoices("class", "product_class"))

    @field_validator(
        "source_item_id", "name", "brand", "article", "brand_code", "cli_code",
        "class_code", "product_class", mode="before"
    )
    @classmethod
    def _text(cls, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (dict, list, tuple, set)):
            raise ValueError("ETM catalog text must be scalar")
        return str(value).strip()

    @field_validator("source_item_id")
    @classmethod
    def _required_id(cls, value: str) -> str:
        if not value:
            raise ValueError("ETM catalog id is required")
        return value


@dataclass(frozen=True)
class EtmManufacturer:
    code: str
    label: str


def _rows_from_payload(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise ValueError("ETM catalog response must be an object or list")
    for key in ("data", "goods", "items", "products", "catalog"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for nested in ("items", "goods", "products", "rows"):
                if isinstance(value.get(nested), list):
                    return value[nested]
    raise ValueError("ETM catalog response contains no rows")


def parse_catalog_snapshot(payload: Any) -> tuple[EtmCatalogRecord, ...]:
    """Validate every row before a mirror transaction can replace old data."""

    rows = _rows_from_payload(payload)
    parsed: list[EtmCatalogRecord] = []
    for raw in rows:
        if isinstance(raw, EtmCatalogRecord):
            parsed.append(raw)
            continue
        if not isinstance(raw, dict):
            raise ValueError("ETM catalog row must be an object")
        parsed.append(EtmCatalogRecord.model_validate(raw))
    if not parsed:
        raise ValueError("ETM catalog snapshot is empty")
    unique: dict[str, EtmCatalogRecord] = {}
    for row in parsed:
        unique[row.source_item_id] = row
    return tuple(unique[key] for key in sorted(unique))


def parse_manufacturers(payload: Any) -> tuple[EtmManufacturer, ...]:
    rows = _rows_from_payload(payload)
    result: list[EtmManufacturer] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        code = raw.get("value", raw.get("code", raw.get("mnf_code", raw.get("manufacturer_code", raw.get("id")))))
        label = raw.get("label", raw.get("name", raw.get("mnf_name", raw.get("manufacturer"))))
        if code is None or label is None:
            continue
        code_text, label_text = str(code).strip(), str(label).strip()
        if code_text and label_text:
            result.append(EtmManufacturer(code_text, label_text))
    if rows and not result:
        raise ValueError("ETM manufacturer response contains no valid rows")
    return tuple(sorted({item.code: item for item in result}.values(), key=lambda item: item.code))
