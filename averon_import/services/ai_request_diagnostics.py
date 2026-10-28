"""Content-free evidence for clients and separate versioned diagnostic files.

Provider strings are accepted by exact membership, never by a permissive
identifier regex. In particular, error messages and arbitrary metadata have
no field here. Unknown provider codes stay unknown.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


ProviderErrorCode = Literal[
    "INVALID_ARGUMENT", "RESOURCE_EXHAUSTED", "PERMISSION_DENIED",
    "UNAUTHENTICATED", "DEADLINE_EXCEEDED", "INTERNAL", "UNAVAILABLE",
    "UNIMPLEMENTED", "invalid_request_error", "bad_request",
    "request_too_large", "context_length_exceeded", "unsupported_parameter",
    "unsupported_value", "invalid_parameter", "invalid_value",
    "invalid_image", "invalid_image_format", "invalid_base64_image",
    "invalid_image_url", "image_too_large", "image_file_too_large",
    "unsupported_image_media_type", "invalid_json_schema",
]
ProviderErrorType = Literal[
    "invalid_request_error", "invalid_argument", "validation_error",
    "request_too_large", "rate_limit_error", "server_error",
]
ProviderErrorParam = Literal[
    "messages", "messages[0].role", "messages[1].role", "messages[2].role",
    "messages[0].content", "messages[1].content", "messages[2].content",
    "image_url", "image_url.url", "response_format",
    "response_format.json_schema", "response_format.json_schema.strict",
    "response_format.json_schema.schema", "reasoning_effort",
    "max_completion_tokens", "model", "temperature", "stream", "store",
]


class AIRequestDiagnostics(BaseModel):
    """HTTP evidence that is never embedded in the canonical page checkpoint."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    request_body_bytes: int | None = Field(default=None, ge=0)
    image_bytes: int | None = Field(default=None, ge=0)
    image_width: int | None = Field(default=None, gt=0)
    image_height: int | None = Field(default=None, gt=0)
    system_message_count: int | None = Field(default=None, ge=0, le=100)
    user_context_present: bool | None = None
    user_context_utf8_bytes: int | None = Field(default=None, ge=0, le=4000)
    context_body_delta_bytes: int | None = Field(default=None, ge=0, le=10000)
    client_request_id: str | None = None
    provider_request_id: str | None = None
    provider_trace_id: str | None = None
    provider_error_code: ProviderErrorCode | None = None
    provider_error_type: ProviderErrorType | None = None
    provider_error_param: ProviderErrorParam | None = None
    error_body_state: Literal["empty", "oversized", "invalid_json", "non_object", "parsed"] | None = None

    @field_validator("client_request_id", "provider_request_id", "provider_trace_id")
    @classmethod
    def canonical_uuid_only(cls, value: str | None) -> str | None:
        return str(UUID(value)) if value is not None else None


class AIRequestDiagnosticRecord(BaseModel):
    """Versioned sidecar binding HTTP evidence to an existing page attempt."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    format_version: Literal[1]
    checkpoint_identity_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_number: int = Field(ge=1)
    attempt_started_at: str
    http_status: int | None = Field(default=None, ge=100, le=599)
    finish_reason: Literal["stop", "length", "content_filter", "tool_calls", "unknown"] | None = None
    request_latency_ms: float | None = Field(default=None, ge=0)
    evidence: AIRequestDiagnostics

    @field_validator("format_version", mode="before")
    @classmethod
    def exact_version(cls, value: int) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("unsupported diagnostic version")
        return value

    @field_validator("attempt_started_at")
    @classmethod
    def valid_timestamp(cls, value: str) -> str:
        datetime.fromisoformat(value)
        return value
