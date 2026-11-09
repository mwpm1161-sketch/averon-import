"""Inert B1 contracts. No credentials, provider clients or application startup."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Literal
from uuid import UUID

MAX_INTEGER = (1 << 63) - 1
RUB_SCALE = 1_000_000_000  # nano-RUB; every persisted money field is INTEGER.


class CoordinationError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class DeferredWithoutIntent(CoordinationError):
    """A proven pre-insert capacity refusal; no financial intent was created."""


class DispatchGateRejected(CoordinationError):
    """A deterministic pre-commit rejection of a job-managed send gate."""


class CommitReceiptUncertain(CoordinationError):
    """The SQLite operation may have committed but its receipt was not observed."""


def integer(value: int, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= MAX_INTEGER:
        raise CoordinationError("invalid_integer")
    return value


def identifier(value: str) -> str:
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", value):
        raise CoordinationError("invalid_identifier")
    return value


def digest(value: str) -> str:
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise CoordinationError("invalid_digest")
    return value


def uuid_hex(value: str) -> str:
    if type(value) is not str:
        raise CoordinationError("invalid_uuid")
    try:
        if UUID(value).hex != value:
            raise ValueError
    except ValueError:
        raise CoordinationError("invalid_uuid") from None
    return value


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def rub_units(value: Decimal | str) -> int:
    """Conservatively round RUB up to nano-RUB; never accept float/bool."""
    if not isinstance(value, (Decimal, str)):
        raise CoordinationError("invalid_money")
    try:
        if isinstance(value, str) and len(value) > 4096:
            raise ValueError
        number = Decimal(value)
        if not number.is_finite() or number < 0:
            raise ValueError
        parts = number.as_tuple()
        if len(parts.digits) > 4096:
            raise ValueError
        coefficient = int("".join(map(str, parts.digits)))
        if coefficient == 0:
            return 0
        power = parts.exponent + 9
        if power >= 0:
            if len(parts.digits) + power > 19:
                raise ValueError
            return integer(coefficient * 10 ** power)
        if -power >= len(parts.digits):
            return 1
        quotient, remainder = divmod(coefficient, 10 ** -power)
        return integer(quotient + bool(remainder))
    except (InvalidOperation, ValueError, OverflowError):
        raise CoordinationError("invalid_money") from None


@dataclass(frozen=True)
class ClockStamp:
    boot_id: str
    milliseconds: int

    def __post_init__(self):
        uuid_hex(self.boot_id)
        integer(self.milliseconds)


@dataclass(frozen=True)
class BillingScope:
    scope_id: str
    revision: int
    ceiling_units: int
    opening_spent_units: int
    opening_held_units: int
    history_complete: bool
    evidence_digest: str

    def __post_init__(self):
        identifier(self.scope_id)
        integer(self.revision, minimum=1)
        integer(self.ceiling_units)
        integer(self.opening_spent_units)
        integer(self.opening_held_units)
        if type(self.history_complete) is not bool:
            raise CoordinationError("invalid_history_attestation")
        digest(self.evidence_digest)


@dataclass(frozen=True)
class QuotaScope:
    scope_id: str
    revision: int
    requests: int
    tokens: int
    concurrency: int
    window_ms: int
    opening_requests: int
    opening_tokens: int
    opening_inflight: int
    history_complete: bool
    evidence_digest: str

    def __post_init__(self):
        identifier(self.scope_id)
        for value in (self.revision, self.requests, self.tokens, self.concurrency, self.window_ms):
            integer(value, minimum=1)
        for value in (self.opening_requests, self.opening_tokens, self.opening_inflight):
            integer(value)
        if type(self.history_complete) is not bool:
            raise CoordinationError("invalid_history_attestation")
        digest(self.evidence_digest)


@dataclass(frozen=True)
class Binding:
    binding_id: str
    revision: int
    credential_generation: str  # opaque reference, NOT the key or its account identity
    credential_tag: str  # private persistent tag verified by the future composition layer
    billing_scope_id: str
    quota_scope_ids: tuple[str, ...]
    target_digest: str  # approved endpoint/project/model snapshot, not a URL supplied by a caller

    def __post_init__(self):
        for value in (self.binding_id, self.credential_generation, self.billing_scope_id):
            identifier(value)
        integer(self.revision, minimum=1)
        digest(self.credential_tag)
        digest(self.target_digest)
        if (type(self.quota_scope_ids) is not tuple or not self.quota_scope_ids
                or len(self.quota_scope_ids) > 16
                or tuple(sorted(set(self.quota_scope_ids))) != self.quota_scope_ids):
            raise CoordinationError("invalid_quota_binding")
        for value in self.quota_scope_ids:
            identifier(value)

    @property
    def fingerprint(self) -> str:
        return fingerprint(asdict(self))


@dataclass(frozen=True)
class Bootstrap:
    installation_id: str
    authorization_id: str
    history_manifest_digest: str
    billings: tuple[BillingScope, ...]
    quotas: tuple[QuotaScope, ...]
    bindings: tuple[Binding, ...]
    global_concurrency: int = 1

    def __post_init__(self):
        uuid_hex(self.installation_id)
        uuid_hex(self.authorization_id)
        digest(self.history_manifest_digest)
        integer(self.global_concurrency, minimum=1)
        if self.global_concurrency > 10:
            raise CoordinationError("invalid_global_concurrency")
        for values, cls, key in ((self.billings, BillingScope, "scope_id"),
                                 (self.quotas, QuotaScope, "scope_id"),
                                 (self.bindings, Binding, "binding_id")):
            if type(values) is not tuple or not values or any(type(v) is not cls for v in values):
                raise CoordinationError("invalid_bootstrap")
            if len({getattr(v, key) for v in values}) != len(values):
                raise CoordinationError("duplicate_scope")


@dataclass(frozen=True)
class Run:
    run_id: str
    tenant_id: str
    document_id: str
    generation: int
    source_digest: str
    config_digest: str
    budget_units: int
    request_cap: int

    def __post_init__(self):
        uuid_hex(self.run_id)
        for value in (self.tenant_id, self.document_id):
            identifier(value)
        for value in (self.generation, self.request_cap):
            integer(value, minimum=1)
        integer(self.budget_units)
        digest(self.source_digest)
        digest(self.config_digest)


@dataclass(frozen=True)
class Attempt:
    operation_id: str
    attempt_id: str
    run_id: str
    page_number: int
    attempt_no: int
    claim_seq: int
    binding: Binding
    billing_revision: int
    quota_revisions: tuple[tuple[str, int], ...]
    request_digest: str
    input_tokens: int
    output_tokens: int
    reserve_units: int

    def __post_init__(self):
        for value in (self.operation_id, self.attempt_id, self.run_id):
            uuid_hex(value)
        for value in (self.page_number, self.attempt_no, self.claim_seq):
            integer(value, minimum=1)
        for value in (self.input_tokens, self.output_tokens):
            integer(value)
        integer(self.reserve_units, minimum=1)
        integer(self.input_tokens + self.output_tokens, minimum=1)
        if type(self.binding) is not Binding:
            raise CoordinationError("invalid_binding")
        integer(self.billing_revision, minimum=1)
        if (type(self.quota_revisions) is not tuple
                or any(type(v) is not tuple or len(v) != 2 for v in self.quota_revisions)
                or tuple(v[0] for v in self.quota_revisions) != self.binding.quota_scope_ids):
            raise CoordinationError("invalid_quota_revisions")
        for item in self.quota_revisions:
            if type(item) is not tuple or len(item) != 2:
                raise CoordinationError("invalid_quota_revisions")
            identifier(item[0])
            integer(item[1], minimum=1)
        digest(self.request_digest)


@dataclass(frozen=True)
class Outcome:
    operation_id: str
    attempt_id: str
    request_digest: str
    evidence_digest: str
    state: Literal["terminal", "unknown", "not_sent"]
    actual_units: int | None

    def __post_init__(self):
        uuid_hex(self.operation_id)
        uuid_hex(self.attempt_id)
        digest(self.request_digest)
        digest(self.evidence_digest)
        if self.state not in {"terminal", "unknown", "not_sent"}:
            raise CoordinationError("invalid_outcome")
        if self.actual_units is not None:
            integer(self.actual_units)
        if self.state != "terminal" and self.actual_units is not None:
            raise CoordinationError("invalid_outcome")


@dataclass(frozen=True)
class Reservation:
    attempt_id: str
    owner_epoch: int
    newly_created: bool
    # A B1 ledger receipt. It is NOT an HTTP permission/capability.
