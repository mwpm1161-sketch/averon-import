"""Allowlisted diagnostics for the VI provider; disabled unless explicitly enabled.

Set ``AVERON_VSEINSTRUMENTI_SAFE_DIAGNOSTICS=1`` in the server process to emit
one bounded diagnostic line per observed VI failure. No request or response
values are logged. The optional missing-envelope probe accepts only a fixed-key,
type-only response signature; it never retains arbitrary response content.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
import logging
import os
import sys
import threading


class VseinstrumentiDiagnosticErrorType(str, Enum):
    HTTP = "HTTP"
    ERROR_ENVELOPE = "ERROR_ENVELOPE"
    TRANSPORT = "TRANSPORT"
    JSON_DECODING = "JSON_DECODING"
    ENVELOPE_VALIDATION = "ENVELOPE_VALIDATION"
    RESPONSE_VALIDATION = "RESPONSE_VALIDATION"
    OFFER_CONVERSION = "OFFER_CONVERSION"
    OUTCOME_CONTRACT = "OUTCOME_CONTRACT"


class VseinstrumentiDiagnosticReasonCode(str, Enum):
    ENVELOPE_SHAPE = "ENVELOPE_SHAPE"
    ROOT_NOT_OBJECT = "ROOT_NOT_OBJECT"
    RESULT_MISSING = "RESULT_MISSING"
    RESULT_NOT_OBJECT = "RESULT_NOT_OBJECT"
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


class VseinstrumentiJsonValueType(str, Enum):
    """Only JSON types that may be reported by the bounded shape probe."""

    OBJECT = "OBJECT"
    ARRAY = "ARRAY"
    STRING = "STRING"
    NUMBER = "NUMBER"
    BOOLEAN = "BOOLEAN"
    NULL = "NULL"
    MISSING = "MISSING"


@dataclass(frozen=True, slots=True)
class VseinstrumentiResponseShape:
    """A type-only signature over fixed JSON field allowlists."""

    root_types: tuple[VseinstrumentiJsonValueType, ...]
    nested_types: tuple[tuple[VseinstrumentiJsonValueType, ...], ...]


_RESPONSE_SHAPE_ROOT_FIELDS = (
    "result", "data", "products", "items", "response", "payload", "success",
    "error", "errors", "code", "status", "statusCode", "message", "details",
)
_RESPONSE_SHAPE_NESTED_CONTAINERS = (
    "result", "data", "products", "items", "response", "payload", "error", "errors", "details",
)
_RESPONSE_SHAPE_NESTED_FIELDS = (
    "products", "items", "search", "regionId", "error", "errors", "message",
)
_RESPONSE_SHAPE_MAX_FEATURES = len(_RESPONSE_SHAPE_ROOT_FIELDS) + (
    len(_RESPONSE_SHAPE_NESTED_CONTAINERS) * len(_RESPONSE_SHAPE_NESTED_FIELDS)
)
_RESPONSE_SHAPE_MAX_CHARS = 1500


def _json_value_type(value: object) -> VseinstrumentiJsonValueType | None:
    if type(value) is dict:
        return VseinstrumentiJsonValueType.OBJECT
    if type(value) is list:
        return VseinstrumentiJsonValueType.ARRAY
    if type(value) is str:
        return VseinstrumentiJsonValueType.STRING
    if type(value) is bool:
        return VseinstrumentiJsonValueType.BOOLEAN
    if value is None:
        return VseinstrumentiJsonValueType.NULL
    if type(value) in {int, float, Decimal}:
        return VseinstrumentiJsonValueType.NUMBER
    return None


def classify_vseinstrumenti_response_shape(payload: object) -> VseinstrumentiResponseShape | None:
    """Inspect only known key presence/types; never enumerate keys or arrays."""
    if type(payload) is not dict:
        return None
    try:
        root_types: list[VseinstrumentiJsonValueType] = []
        nested_types: list[tuple[VseinstrumentiJsonValueType, ...]] = []
        for field in _RESPONSE_SHAPE_ROOT_FIELDS:
            value = payload.get(field, _RESPONSE_SHAPE_MISSING)
            value_type = (
                VseinstrumentiJsonValueType.MISSING
                if value is _RESPONSE_SHAPE_MISSING
                else _json_value_type(value)
            )
            if value_type is None:
                return None
            root_types.append(value_type)

        root_by_field = dict(zip(_RESPONSE_SHAPE_ROOT_FIELDS, root_types, strict=True))
        for container_field in _RESPONSE_SHAPE_NESTED_CONTAINERS:
            container_value = payload.get(container_field, _RESPONSE_SHAPE_MISSING)
            if root_by_field[container_field] is not VseinstrumentiJsonValueType.OBJECT:
                nested_types.append(tuple(VseinstrumentiJsonValueType.MISSING for _ in _RESPONSE_SHAPE_NESTED_FIELDS))
                continue
            if type(container_value) is not dict:
                return None
            field_types: list[VseinstrumentiJsonValueType] = []
            for field in _RESPONSE_SHAPE_NESTED_FIELDS:
                value = container_value.get(field, _RESPONSE_SHAPE_MISSING)
                value_type = (
                    VseinstrumentiJsonValueType.MISSING
                    if value is _RESPONSE_SHAPE_MISSING
                    else _json_value_type(value)
                )
                if value_type is None:
                    return None
                field_types.append(value_type)
            nested_types.append(tuple(field_types))

        shape = VseinstrumentiResponseShape(tuple(root_types), tuple(nested_types))
        if len(shape.root_types) + sum(map(len, shape.nested_types)) > _RESPONSE_SHAPE_MAX_FEATURES:
            return None
        return shape
    except Exception:
        # The probe is diagnostic-only and must never affect response handling.
        return None


_RESPONSE_SHAPE_MISSING = object()


def _format_vseinstrumenti_response_shape(shape: object) -> str | None:
    if type(shape) is not VseinstrumentiResponseShape:
        return None
    if (len(shape.root_types) != len(_RESPONSE_SHAPE_ROOT_FIELDS)
            or len(shape.nested_types) != len(_RESPONSE_SHAPE_NESTED_CONTAINERS)
            or any(len(values) != len(_RESPONSE_SHAPE_NESTED_FIELDS) for values in shape.nested_types)
            or any(type(value) is not VseinstrumentiJsonValueType for value in shape.root_types)
            or any(type(value) is not VseinstrumentiJsonValueType for row in shape.nested_types for value in row)):
        return None
    root_by_field = dict(zip(_RESPONSE_SHAPE_ROOT_FIELDS, shape.root_types, strict=True))
    root_signature = ",".join(
        f"{field}={value.value}" for field, value in zip(_RESPONSE_SHAPE_ROOT_FIELDS, shape.root_types, strict=True)
    )
    nested_parts = []
    for container, values in zip(_RESPONSE_SHAPE_NESTED_CONTAINERS, shape.nested_types, strict=True):
        if root_by_field[container] is not VseinstrumentiJsonValueType.OBJECT:
            continue
        nested_fields = ",".join(
            f"{field}={value.value}"
            for field, value in zip(_RESPONSE_SHAPE_NESTED_FIELDS, values, strict=True)
        )
        nested_parts.append(f"{container}[{nested_fields}]")
    signature = f"root[{root_signature}]" + (f";nested[{';'.join(nested_parts)}]" if nested_parts else "")
    return signature if len(signature) <= _RESPONSE_SHAPE_MAX_CHARS else None


_LOGGER = logging.getLogger("averon.sourcing.vseinstrumenti.diagnostics")
_SINK_LOCK = threading.RLock()


class _SafeDiagnosticStreamHandler(logging.StreamHandler):
    """A normal stderr sink whose internal logging failures stay silent."""

    def handleError(self, record: logging.LogRecord) -> None:
        # StreamHandler.emit() normally calls logging.Handler.handleError(),
        # which writes a traceback and the record to stderr when
        # logging.raiseExceptions is true. Diagnostic records contain only
        # allowlisted values, but even that fallback can leak formatter or
        # stream exception text. Diagnostics are best-effort, so drop them.
        return None


_SAFE_HANDLER = _SafeDiagnosticStreamHandler(stream=sys.stderr)
_SAFE_HANDLER.setLevel(logging.WARNING)
_SAFE_FORMATTER = logging.Formatter("%(message)s")
_SAFE_HANDLER.setFormatter(_SAFE_FORMATTER)


def _ensure_safe_diagnostic_sink() -> None:
    """Keep this dedicated logger isolated from root and custom handlers."""

    with _SINK_LOCK:
        _LOGGER.propagate = False
        _LOGGER.setLevel(logging.WARNING)
        _LOGGER.disabled = False
        # This logger carries only the fixed event below. Do not let an
        # application filter mutate it or route it through another sink.
        _LOGGER.filters.clear()
        _SAFE_HANDLER.filters.clear()
        _SAFE_HANDLER.setFormatter(_SAFE_FORMATTER)
        # Logging configuration may be applied after this module is imported.
        # Detach every other handler from this VI-only logger before emitting.
        for handler in tuple(_LOGGER.handlers):
            if handler is not _SAFE_HANDLER:
                _LOGGER.removeHandler(handler)
        if _SAFE_HANDLER not in _LOGGER.handlers:
            _LOGGER.addHandler(_SAFE_HANDLER)


def safe_diagnostics_enabled() -> bool:
    """Return the opt-in state without allowing configuration reads to escape."""

    try:
        return os.environ.get("AVERON_VSEINSTRUMENTI_SAFE_DIAGNOSTICS") == "1"
    except Exception:
        return False


def content_type_category(response: object) -> VseinstrumentiContentTypeCategory:
    """Reduce a response Content-Type to a fixed category without retaining it."""

    try:
        headers = getattr(response, "headers", None)
        if headers is None:
            return VseinstrumentiContentTypeCategory.MISSING
        value = headers.get("Content-Type")
        if value is None:
            value = headers.get("content-type")
    except Exception:
        return VseinstrumentiContentTypeCategory.MISSING
    if not isinstance(value, str) or not value.strip():
        return VseinstrumentiContentTypeCategory.MISSING
    media_type = value.split(";", 1)[0].strip().casefold()
    if media_type in {"application/json", "text/json"} or media_type.endswith("+json"):
        return VseinstrumentiContentTypeCategory.JSON
    if media_type in {"text/html", "application/xhtml+xml"}:
        return VseinstrumentiContentTypeCategory.HTML
    return VseinstrumentiContentTypeCategory.OTHER


def _emit_allowlisted_diagnostic(
    *,
    error_type: VseinstrumentiDiagnosticErrorType,
    reason_code: VseinstrumentiDiagnosticReasonCode = VseinstrumentiDiagnosticReasonCode.UNKNOWN,
    status_code: int | None = None,
    envelope_status_code: int | None = None,
    content_type: VseinstrumentiContentTypeCategory = VseinstrumentiContentTypeCategory.MISSING,
    response_bytes: int | None = None,
    response_shape: VseinstrumentiResponseShape | None = None,
) -> None:
    """Construct and emit one allowlisted event; callers must isolate failures."""
    if not isinstance(error_type, VseinstrumentiDiagnosticErrorType):
        error_type = VseinstrumentiDiagnosticErrorType.TRANSPORT
    if not isinstance(reason_code, VseinstrumentiDiagnosticReasonCode):
        reason_code = VseinstrumentiDiagnosticReasonCode.UNKNOWN
    if not isinstance(content_type, VseinstrumentiContentTypeCategory):
        content_type = VseinstrumentiContentTypeCategory.OTHER
    safe_status = status_code if type(status_code) is int and 100 <= status_code <= 599 else None
    safe_envelope_status = (
        envelope_status_code
        if type(envelope_status_code) is int and 400 <= envelope_status_code <= 599
        else None
    )
    safe_size = response_bytes if type(response_bytes) is int and 0 <= response_bytes <= 2_097_153 else None
    safe_shape = _format_vseinstrumenti_response_shape(response_shape)
    _ensure_safe_diagnostic_sink()
    message = (
        "vseinstrumenti_safe_diagnostic error_type=%s status_code=%s envelope_status_code=%s content_type=%s "
        "response_bytes=%s reason_code=%s"
    )
    if safe_shape is not None:
        message += " response_shape=%s"
    arguments: tuple[object, ...] = (
        error_type.value,
        safe_status if safe_status is not None else "MISSING",
        safe_envelope_status if safe_envelope_status is not None else "MISSING",
        content_type.value,
        safe_size if safe_size is not None else "MISSING",
        reason_code.value,
    )
    if safe_shape is not None:
        arguments += (safe_shape,)
    _LOGGER.warning(message, *arguments)


def log_vseinstrumenti_diagnostic(
    *,
    error_type: VseinstrumentiDiagnosticErrorType,
    reason_code: VseinstrumentiDiagnosticReasonCode = VseinstrumentiDiagnosticReasonCode.UNKNOWN,
    status_code: int | None = None,
    envelope_status_code: int | None = None,
    content_type: VseinstrumentiContentTypeCategory = VseinstrumentiContentTypeCategory.MISSING,
    response_bytes: int | None = None,
    response_shape: VseinstrumentiResponseShape | None = None,
) -> None:
    """Best-effort bounded diagnostic; its failures never affect sourcing."""

    try:
        if not safe_diagnostics_enabled():
            return
        _emit_allowlisted_diagnostic(
            error_type=error_type,
            reason_code=reason_code,
            status_code=status_code,
            envelope_status_code=envelope_status_code,
            content_type=content_type,
            response_bytes=response_bytes,
            response_shape=response_shape,
        )
    except Exception:
        # Diagnostics are deliberately lossy. Never retry or fallback-log here.
        return
