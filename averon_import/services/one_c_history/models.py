from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator


LAYOUT_TYPES = ("hierarchical_grouped", "flat")
FIELD_NAMES = (
    "item_code", "item_name", "unit", "quantity", "reported_unit_price_gross",
    "amount_gross", "document_date", "document_type", "document_reference",
    "counterparty", "contract", "article", "manufacturer", "characteristic",
    "supplier_code", "supplier_inn", "vat_rate", "currency", "organization",
    "document_stable_reference", "document_line_number",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ImportProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(default_factory=lambda: uuid.uuid4().hex, pattern=r"^[0-9a-f]{32}$")
    name: str = Field(min_length=1, max_length=100)
    layout_type: Literal["hierarchical_grouped", "flat"]
    sheet_name: str = Field(min_length=1, max_length=128)
    header_row: int = Field(ge=1, le=50)
    field_mapping: dict[str, int] = Field(default_factory=dict)
    item_name_parse_strategy: Literal["none", "comma_suffix_unit", "comma_or_parentheses"] = "none"
    header_signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    parser_version: int = Field(ge=1)
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)

    @field_validator("field_mapping")
    @classmethod
    def validate_mapping(cls, value: dict[str, int]) -> dict[str, int]:
        if any(name not in FIELD_NAMES or type(index) is not int or index < 0 for name, index in value.items()):
            raise ValueError("Некорректное сопоставление полей профиля")
        return value


class ImportMappingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    sheet_name: str | None = Field(default=None, min_length=1, max_length=128)
    header_row: int | None = Field(default=None, ge=1, le=50)
    layout_type: Literal["hierarchical_grouped", "flat"]
    field_mapping: dict[str, int | None]
    item_name_parse_strategy: Literal["none", "comma_suffix_unit", "comma_or_parentheses"] = "none"
    save_profile: bool = False
    profile_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    profile_name: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("field_mapping")
    @classmethod
    def validate_mapping(cls, value: dict[str, int | None]) -> dict[str, int | None]:
        if any(name not in FIELD_NAMES or (index is not None and (type(index) is not int or index < 0)) for name, index in value.items()):
            raise ValueError("Некорректное сопоставление полей")
        return value


class PreviewMappingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sheet_name: str = Field(min_length=1, max_length=128)
    header_row: int = Field(ge=1, le=50)
    layout_type: Literal["hierarchical_grouped", "flat"]
    field_mapping: dict[str, int | None]
    item_name_parse_strategy: Literal["none", "comma_suffix_unit", "comma_or_parentheses"] = "none"

    @field_validator("field_mapping")
    @classmethod
    def validate_mapping(cls, value: dict[str, int | None]) -> dict[str, int | None]:
        if any(name not in FIELD_NAMES or (index is not None and (type(index) is not int or index < 0)) for name, index in value.items()):
            raise ValueError("Некорректное сопоставление полей")
        return value


class OneCHistoryStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active_import: dict | None = None
    last_attempt: dict | None = None
    profiles: list[ImportProfile]
