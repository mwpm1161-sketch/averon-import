"""Allowlisted diagnostics for the VI provider; disabled unless explicitly enabled.

Set ``AVERON_VSEINSTRUMENTI_SAFE_DIAGNOSTICS=1`` in the server process to emit
one bounded diagnostic line per observed VI failure. No request or response
values are accepted by the logging interface.
"""

from __future__ import annotations

from enum import Enum
import logging
import os


class VseinstrumentiDiagnosticErrorType(str, Enum):
    HTTP = "HTTP"
    TRANSPORT = "TRANSPORT"
    JSON_DECODING = "JSON_DECODING"
    ENVELOPE_VALIDATION = "ENVELOPE_VALIDATION"
    RESPONSE_VALIDATION = "RESPONSE_VALIDATION"
    OFFER_CONVERSION = "OFFER_CONVERSION"
    OUTCOME_CONTRACT = "OUTCOME_CONTRACT"


class VseinstrumentiDiagnosticReasonCode(str, Enum):
    ENVELOPE_SHAPE = "ENVELOPE_SHAPE"
    CONTEXT_MISMATCH = "CONTEXT_MISMATCH"
    PRODUCTS_SHAPE = "PRODUCTS_SHAPE"
    SKU_TYPE = "SKU_TYPE"
    SPECS_SHAPE = "SPECS_SHAPE"
    PRICE_SHAPE = "PRICE_SHAPE"
    OTHER_FIELD_SHAPE = "OTHER_FIELD_SHAPE"
    DUPLICATE_CONFLICT = "DUPLICATE_CONFLICT"
    OFFER_CONVERSION = "OFFER_CONVERSION"
    OUTCOME_CONTRACT = "OUTCOME_CONTRACT"
    UNKNOWN = "UNKNOWN"


class VseinstrumentiContentTypeCategory(str, Enum):
    JSON = "JSON"
    HTML = "HTML"
    OTHER = "OTHER"
    MISSING = "MISSING"


_LOGGER = logging.getLogger("averon.sourcing.vseinstrumenti.diagnostics")


def content_type_category(response: object) -> VseinstrumentiContentTypeCategory:
    """Reduce a response Content-Type to a fixed category without retaining it."""

    headers = getattr(response, "headers", None)
    if headers is None:
        return VseinstrumentiContentTypeCategory.MISSING
    try:
        value = headers.get("Content-Type")
        if value is None:
            value = headers.get("content-type")
    except Exception:
        return VseinstrumentiContentTypeCategory.OTHER
    if not isinstance(value, str) or not value.strip():
        return VseinstrumentiContentTypeCategory.MISSING
    media_type = value.split(";", 1)[0].strip().casefold()
    if media_type in {"application/json", "text/json"} or media_type.endswith("+json"):
        return VseinstrumentiContentTypeCategory.JSON
    if media_type in {"text/html", "application/xhtml+xml"}:
        return VseinstrumentiContentTypeCategory.HTML
    return VseinstrumentiContentTypeCategory.OTHER


def log_vseinstrumenti_diagnostic(
    *,
    error_type: VseinstrumentiDiagnosticErrorType,
    reason_code: VseinstrumentiDiagnosticReasonCode = VseinstrumentiDiagnosticReasonCode.UNKNOWN,
    status_code: int | None = None,
    content_type: VseinstrumentiContentTypeCategory = VseinstrumentiContentTypeCategory.MISSING,
    response_bytes: int | None = None,
) -> None:
    """Write only fixed categories and bounded numeric metadata when enabled."""

    if os.environ.get("AVERON_VSEINSTRUMENTI_SAFE_DIAGNOSTICS") != "1":
        return
    if not isinstance(error_type, VseinstrumentiDiagnosticErrorType):
        error_type = VseinstrumentiDiagnosticErrorType.TRANSPORT
    if not isinstance(reason_code, VseinstrumentiDiagnosticReasonCode):
        reason_code = VseinstrumentiDiagnosticReasonCode.UNKNOWN
    if not isinstance(content_type, VseinstrumentiContentTypeCategory):
        content_type = VseinstrumentiContentTypeCategory.OTHER
    safe_status = status_code if type(status_code) is int and 100 <= status_code <= 599 else None
    safe_size = response_bytes if type(response_bytes) is int and 0 <= response_bytes <= 2_097_153 else None
    _LOGGER.warning(
        "vseinstrumenti_safe_diagnostic error_type=%s status_code=%s content_type=%s "
        "response_bytes=%s reason_code=%s",
        error_type.value,
        safe_status if safe_status is not None else "MISSING",
        content_type.value,
        safe_size if safe_size is not None else "MISSING",
        reason_code.value,
    )
