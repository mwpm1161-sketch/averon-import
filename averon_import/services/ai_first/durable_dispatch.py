"""B2 one-shot dispatch boundary. No bootstrap, scheduler or publication API.

Only a fresh B1 receipt can mint a process/caller/thread-bound capability.
The capability is burned before revalidation; ambiguous commits never mint one.
Neither a recovered intent nor an outcome artifact authorizes another POST.
"""
from __future__ import annotations

import asyncio
import contextvars
from dataclasses import asdict, dataclass
from decimal import Decimal
from fractions import Fraction
import hashlib
import hmac
import json
import os
import re
import threading
from uuid import UUID, uuid5

from .coordination import CoordinationLedger, _safe_root
from .coordination_contracts import (
    Attempt, Binding, CoordinationError, Outcome, RUB_SCALE, Run, canonical,
    digest, fingerprint, integer, rub_units, uuid_hex,
)
from .dispatch_policy import durable_required
from .private_acl import secure_private_path, verify_private_path

CALLER = contextvars.ContextVar("averon_durable_dispatch_caller", default=None)
_MINT = object()


def target_digest(config):
    return fingerprint({"endpoint": config.base_url + "/chat/completions", "model": config.model,
                        "project": config.folder_id, "auth_scheme": config.auth_scheme})


def credential_tag(config, generation):
    return hmac.new(config.api_key.encode(), ("averon-durable-credential-v1:" + generation).encode(),
                    hashlib.sha256).hexdigest()


def price_units(pricing, input_tokens, output_tokens, cached_input_tokens=0):
    """Exact, context-independent ceiling of one complete cost to nano-RUB.

    Limit representation work, not the operator's tariff or financial ceiling.
    Excess precision/exponent is an invalid durable pricing contract.
    """
    for value in (input_tokens, output_tokens, cached_input_tokens):
        integer(value)
    if cached_input_tokens > input_tokens:
        raise CoordinationError("invalid_token_usage")
    def rate(value):
        if (type(value) is not Decimal or not value.is_finite() or value <= 0
                or len(value.as_tuple().digits) > 4096 or abs(value.as_tuple().exponent) > 4096):
            raise CoordinationError("invalid_durable_pricing")
        return Fraction(value)
    uncached = rate(pricing.input_rub_per_1k)
    cached = rate(pricing.cached_input_rub_per_1k or pricing.input_rub_per_1k)
    output = rate(pricing.output_rub_per_1k)
    amount = ((input_tokens - cached_input_tokens) * uncached + cached_input_tokens * cached
              + output_tokens * output) * RUB_SCALE / 1000
    return integer((amount.numerator + amount.denominator - 1) // amount.denominator)


def reserve_units(pricing, input_tokens, output_tokens):
    # A configured cache tier is not assumed cheaper than uncached input.
    cached = input_tokens if (pricing.cached_input_rub_per_1k is not None
                             and pricing.cached_input_rub_per_1k > pricing.input_rub_per_1k) else 0
    return price_units(pricing, input_tokens, output_tokens, cached)


def usage_units(pricing, usage):
    if usage is None or usage.input_tokens is None or usage.output_tokens is None:
        return None
    if (usage.cached_input_tokens is None and pricing.cached_input_rub_per_1k is not None
            and pricing.cached_input_rub_per_1k != pricing.input_rub_per_1k):
        return None  # Price-sensitive usage is unknown; keep the whole hold.
    return price_units(pricing, usage.input_tokens, usage.output_tokens, usage.cached_input_tokens or 0)


def units_rub(value):
    if value is None:
        return None
    # Aggregate projections, like B1 liability sums, may exceed one int64.
    if type(value) is not int or value < 0:
        raise CoordinationError("invalid_integer")
    return Decimal(f"{value // RUB_SCALE}.{value % RUB_SCALE:09d}")


async def drain_thread(function, *args):
    """Once submitted, drain SQLite/fsync work even under repeated cancellation."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    value = task.result()
    return value, cancelled


class DispatchPermit:
    __slots__ = ("attempt", "epoch", "_client", "_caller", "_pid", "_thread", "_used", "_lock")

    def __init__(self, mint, attempt, epoch, client, caller):
        if mint is not _MINT:
            raise CoordinationError("permit_mint_forbidden")
        self.attempt, self.epoch, self._client, self._caller = attempt, epoch, client, caller
        self._pid, self._thread = os.getpid(), threading.get_ident()
        self._used, self._lock = False, threading.Lock()

    def __reduce__(self):
        raise TypeError("dispatch_permit_is_process_bound")


@dataclass(frozen=True)
class DispatchBinding:
    run: Run
    binding: Binding
    billing_revision: int
    quota_revisions: tuple[tuple[str, int], ...]
    pricing: object


@dataclass(frozen=True)
class HistoricalSettlementContext:
    """Frozen nonsecret preimages required to settle an immutable v1 artifact.

    v1 evidence stores pricing/config fingerprints, not their preimages. This
    context therefore carries a caller-supplied original snapshot as canonical
    JSON. It cannot be populated from current settings by this type.
    """
    run_json: str
    config_json: str

    @classmethod
    def capture(cls, run, config):
        from .orchestration import OrchestrationConfig
        if type(run) is not Run or type(config) is not OrchestrationConfig:
            raise CoordinationError("historical_context_required")
        return cls(canonical(asdict(run)), canonical(config.model_dump(mode="json")))

    def materialize(self):
        from .orchestration import OrchestrationConfig
        try:
            run_value = json.loads(self.run_json)
            run = Run(**run_value)
            config = OrchestrationConfig.model_validate_json(self.config_json)
            if canonical(asdict(run)) != self.run_json or canonical(config.model_dump(mode="json")) != self.config_json:
                raise ValueError
            return run, config
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            raise CoordinationError("historical_context_invalid") from None


class DispatchCall:
    def __init__(self, grant, page, body_digest, estimate, pricing_digest):
        self.grant, self.page, self.body_digest, self.estimate = grant, page, body_digest, estimate
        self.attempt = self.permit = self.response = None
        self.fresh = self.delegated = self.uncertain = False
        self.finished = False
        self.error = None
        self.pricing_digest = pricing_digest

    def __reduce__(self):
        raise TypeError("dispatch_call_is_process_bound")


class ResultEvidence:
    """Immutable validated page results; separate explicitly provisioned private root."""
    def __init__(self, root, *, max_bytes=16_000_000):
        self.root = _safe_root(root)
        verify_private_path(self.root, directory=True)
        for path in self.root.iterdir():
            if not re.fullmatch(r"[0-9a-f]{32}\.(json|pending)", path.name):
                raise CoordinationError("evidence_root_not_dedicated")
            verify_private_path(path, directory=False)
        self.identity = (self.root.stat().st_dev, self.root.stat().st_ino)
        self.max_bytes = max_bytes

    def _verify(self):
        verify_private_path(self.root, directory=True)
        if (self.root.stat().st_dev, self.root.stat().st_ino) != self.identity:
            raise CoordinationError("evidence_storage_replaced")

    def path(self, attempt_id):
        if UUID(attempt_id).hex != attempt_id:
            raise CoordinationError("invalid_attempt_identity")
        return self.root / (attempt_id + ".json")

    def _sync_directory(self):
        if os.name != "nt":
            descriptor = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def write(self, attempt, epoch, result, outcome, proof):
        self._verify()
        payload = {"version": 1, "attempt": asdict(attempt), "owner_epoch": epoch,
                   "result": result.model_dump(mode="json"), "outcome": asdict(outcome), "proof": proof}
        envelope = canonical({"payload": payload, "sha256": fingerprint(payload)}).encode()
        if len(envelope) > self.max_bytes:
            raise CoordinationError("result_evidence_size_limit")
        final = self.path(attempt.attempt_id)
        pending = final.with_suffix(".pending")
        descriptor = os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            secure_private_path(pending, directory=False)
            stream.write(envelope)
            stream.flush()
            os.fsync(stream.fileno())
        self._verify()
        # link is atomic and refuses replacement. A crash can leave .pending;
        # it never becomes a reason to resend or to trust incomplete evidence.
        os.link(pending, final)
        self._sync_directory()
        pending.unlink()
        self._sync_directory()

    def read(self, attempt_id):
        self._verify()
        path = self.path(attempt_id)
        if not path.exists():
            return None
        verify_private_path(path, directory=False)
        with path.open("rb") as stream:
            raw = stream.read(self.max_bytes + 1)
        if len(raw) > self.max_bytes:
            raise CoordinationError("result_evidence_size_limit")
        try:
            from averon_import.services.ocr.qwen_vision_client import _json
            envelope = _json(raw)
            payload = envelope["payload"]
            if (set(envelope) != {"payload", "sha256"} or type(payload["version"]) is not int or payload["version"] != 1
                    or set(payload) != {"version", "attempt", "owner_epoch", "result", "outcome", "proof"}
                    or any(type(payload[key]) is not dict for key in ("attempt", "result", "outcome", "proof"))
                    or set(payload["proof"]) != {"attempt", "state", "usage", "status", "response_sha256", "result_sha256", "pricing_sha256"}
                    or type(payload["owner_epoch"]) is not int or payload["owner_epoch"] < 1
                    or fingerprint(payload) != envelope["sha256"]
                    or payload["attempt"]["attempt_id"] != attempt_id):
                raise ValueError
            return payload
        except (ValueError, TypeError, KeyError, RecursionError):
            raise CoordinationError("result_evidence_invalid") from None


class DurableDispatcher:
    """Own the OS lock through all HTTP, settlement and cancellation draining.

    Construct/close off-loop. The evidence root must be provisioned separately;
    neither it nor a missing financial ledger is initialized here.
    """
    def __init__(self, root, *, installation_id, evidence_root, enabled=False, **ledger_options):
        if enabled is not True or not durable_required():
            raise CoordinationError("durable_mode_required")
        self.ledger = CoordinationLedger(root, installation_id=installation_id, **ledger_options)
        self.evidence = ResultEvidence(evidence_root)
        if self.evidence.root.is_relative_to(self.ledger.root) or self.ledger.root.is_relative_to(self.evidence.root):
            raise CoordinationError("evidence_ledger_roots_overlap")
        self._owner = self.ledger.dispatcher()
        self._pid = os.getpid()
        self._condition = threading.Condition()
        self._calls = 0
        self._closing = False
        self._failed = threading.Event()
        self._stop = threading.Event()
        self._clients = {}
        self._registered = set()
        self._pricing = {}
        self._heartbeat = threading.Thread(target=self._renew, name="durable-dispatch-lease", daemon=True)
        try:
            self._heartbeat.start()
        except BaseException:
            self._owner.close()
            raise

    def __reduce__(self):
        raise TypeError("durable_dispatcher_is_process_bound")

    @classmethod
    async def open(cls, root, **options):
        dispatcher, cancelled = await drain_thread(lambda: cls(root, **options))
        if cancelled:
            await drain_thread(dispatcher.close)
            raise asyncio.CancelledError
        return dispatcher

    async def aclose(self):
        _, cancelled = await drain_thread(self.close)
        if cancelled:
            raise asyncio.CancelledError

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        await self.aclose()

    def _renew(self):
        while not self._stop.wait(min(5.0, self.ledger.lease_ms / 3000)):
            try:
                self._owner.renew()
            except Exception:
                self._failed.set()
                return

    def close(self):
        if self._pid != os.getpid():
            raise CoordinationError("owner_fenced")
        with self._condition:
            self._closing = True
            while self._calls:
                self._condition.wait(0.05)
        self._stop.set()
        self._heartbeat.join()
        self._owner.close()

    def attach(self, client, caller, grant: DispatchBinding):
        with self._condition:
            self._check()
            if client in self._clients or type(grant) is not DispatchBinding:
                raise CoordinationError("invalid_dispatch_binding")
            if (target_digest(client.config) != grant.binding.target_digest
                    or credential_tag(client.config, grant.binding.credential_generation) != grant.binding.credential_tag):
                raise CoordinationError("binding_mismatch")
            price_units(grant.pricing, 0, 0)
            self._clients[client] = (caller, grant)
            self._pricing[client] = fingerprint(grant.pricing.model_dump(mode="json"))

    def register(self, client):
        self._check()
        _caller, grant = self._clients[client]
        self.ledger.register_run(self._owner, uuid5(UUID(grant.run.run_id), "register").hex, grant.run)
        with self._condition:
            self._registered.add(client)

    def settle_saved_evidence(self, attempt_id, historical_context):
        """Settle one already-authorized attempt without a client, permit or POST.

        The original snapshot is mandatory because schema v1 stores hashes but
        cannot reconstruct historical pricing. Current runs, credentials and
        policies are deliberately not consulted for this financial replay.
        """
        self._check()
        try:
            uuid_hex(attempt_id)
        except CoordinationError:
            raise CoordinationError("invalid_attempt_identity") from None
        if type(historical_context) is not HistoricalSettlementContext:
            raise CoordinationError("historical_context_required")
        run, config = historical_context.materialize()
        payload = self.evidence.read(attempt_id)
        if payload is None:
            raise CoordinationError("historical_evidence_missing")
        try:
            raw_attempt = dict(payload["attempt"])
            raw_attempt["binding"] = Binding(**{
                **raw_attempt["binding"],
                "quota_scope_ids": tuple(raw_attempt["binding"]["quota_scope_ids"]),
            })
            raw_attempt["quota_revisions"] = tuple(tuple(pair) for pair in raw_attempt["quota_revisions"])
            attempt = Attempt(**raw_attempt)
            outcome = Outcome(**payload["outcome"])
            from .orchestration import AdapterPageResult
            result = AdapterPageResult.model_validate(payload["result"])
        except (ValueError, TypeError, KeyError, RecursionError):
            raise CoordinationError("result_evidence_invalid") from None
        proof = payload["proof"]
        facts = self.ledger.historical_evidence_facts(self._owner, attempt, run)
        if (attempt.attempt_id != attempt_id or attempt.run_id != run.run_id
                or attempt.attempt_no != 1 or attempt.claim_seq != 1
                or attempt.attempt_id != self.attempt_id(run.run_id, attempt.page_number)
                or payload["owner_epoch"] != facts["owner_epoch"]
                or facts["state"] == "not_sent"):
            raise CoordinationError("historical_attempt_mismatch")

        config_digest = fingerprint({
            "orchestration": config.model_dump(mode="json"),
            "binding_digest": attempt.binding.fingerprint,
        })
        pricing_digest = fingerprint(config.pricing.model_dump(mode="json"))
        if (run.config_digest != config_digest
                or run.budget_units != rub_units(config.max_budget_rub)
                or run.request_cap > config.max_requests
                or pricing_digest != proof["pricing_sha256"]):
            raise CoordinationError("historical_context_required")
        if reserve_units(config.pricing, attempt.input_tokens, attempt.output_tokens) != attempt.reserve_units:
            raise CoordinationError("historical_attempt_mismatch")

        try:
            uuid_hex(attempt_id)
            digest(proof["response_sha256"])
        except CoordinationError:
            raise CoordinationError("result_evidence_invalid") from None
        usage_json = result.usage.model_dump(mode="json") if result.usage is not None else None
        status = proof["status"]
        if (set(proof) != {"attempt", "state", "usage", "status", "response_sha256", "result_sha256", "pricing_sha256"}
                or proof["attempt"] != attempt_id or proof["state"] != "terminal"
                or proof["usage"] != usage_json or proof["result_sha256"] != fingerprint(result.model_dump(mode="json"))
                or type(status) is not int or not 100 <= status < 500 or result.http_status != status
                or result.extraction.source_document_id != "pdf-sha256:" + run.source_digest
                or result.extraction.page_number != attempt.page_number):
            raise CoordinationError("result_evidence_identity_mismatch")
        expected_outcome = Outcome(
            uuid5(UUID(attempt_id), "settlement").hex, attempt_id, attempt.request_digest,
            fingerprint(proof), "terminal", usage_units(config.pricing, result.usage),
        )
        if outcome != expected_outcome:
            raise CoordinationError("result_evidence_cost_mismatch")
        if (facts["state"] == "terminal"
                and (facts["actual_units"] != outcome.actual_units
                     or facts["evidence_digest"] != outcome.evidence_digest)):
            raise CoordinationError("historical_outcome_conflict")
        if (facts["state"] == "unknown" and facts["evidence_digest"] not in (None, outcome.evidence_digest)):
            raise CoordinationError("historical_outcome_conflict")

        # B1 rechecks ownership, idempotency and terminal conflicts atomically.
        # This call never creates a reservation or a DispatchPermit.
        return self.ledger.record_outcome(self._owner, outcome)

    def _check(self):
        if (self._pid != os.getpid() or self._closing or self._failed.is_set()
                or not durable_required()):
            raise CoordinationError("dispatcher_unavailable")
        self._owner._check(self.ledger)

    def call(self, client, page, body_digest, estimate):
        with self._condition:
            self._check()
            if client not in self._registered:
                raise CoordinationError("durable_run_not_registered")
            caller, grant = self._clients[client]
            if CALLER.get() is not caller:
                raise CoordinationError("dispatch_caller_mismatch")
            call = DispatchCall(grant, page, body_digest, estimate, self._pricing[client])
            self._calls += 1
            return call

    @staticmethod
    def attempt_id(run_id, page):
        return uuid5(UUID(run_id), f"page:{page}:attempt:1").hex

    def _request(self, client, call, url, body, headers):
        caller, grant = self._clients.get(client, (None, None))
        if caller is None or CALLER.get() is not caller or type(call) is not DispatchCall or call.grant is not grant:
            raise CoordinationError("dispatch_caller_mismatch")
        if fingerprint(grant.pricing.model_dump(mode="json")) != call.pricing_digest:
            raise CoordinationError("pricing_snapshot_mismatch")
        config = client.config
        if (type(body) is not bytes or hashlib.sha256(body).hexdigest() != call.body_digest
                or target_digest(config) != grant.binding.target_digest
                or credential_tag(config, grant.binding.credential_generation) != grant.binding.credential_tag
                or url != config.base_url + "/chat/completions"
                or headers.get("OpenAI-Project", "") != config.folder_id
                or headers.get("Authorization") != f"{config.auth_scheme} {config.api_key}"):
            raise CoordinationError("dispatch_request_mismatch")
        payload = json.loads(body)
        if payload.get("model") != config.model:
            raise CoordinationError("dispatch_request_mismatch")
        digest = fingerprint({"body_sha256": call.body_digest, "target": grant.binding.target_digest,
                              "binding": grant.binding.fingerprint, "run": fingerprint(asdict(grant.run)), "method": "POST"})
        identity = self.attempt_id(grant.run.run_id, call.page)
        estimate = call.estimate
        return Attempt(uuid5(UUID(identity), "reserve").hex, identity, grant.run.run_id,
                       call.page, 1, 1, grant.binding, grant.billing_revision, grant.quota_revisions,
                       digest, estimate.reserved_input_tokens, estimate.reserved_output_tokens,
                       reserve_units(grant.pricing, estimate.reserved_input_tokens,
                                     estimate.reserved_output_tokens))

    def prepare(self, client, call, url, body, headers):
        """Internal transport caller only. Replaying reserve never mints a permit."""
        self._check()
        item = self._request(client, call, url, body, headers)
        call.attempt = item
        try:
            receipt = self.ledger.reserve(self._owner, item)
        except BaseException as exc:
            call.uncertain = True
            self._failed.set()
            if isinstance(exc, CoordinationError):
                try:
                    history = self.ledger.checkpoint_attempts(self._owner, call.grant.run)
                except CoordinationError:
                    pass  # Unavailable/fenced history is never proof of absence.
                else:
                    # A verified empty page history proves this preparation
                    # never acquired durable intent. No permit is minted here.
                    call.uncertain = any(row["page_number"] == call.page for row in history)
            raise
        if not receipt.newly_created:
            raise CoordinationError("dispatch_already_reserved")
        call.fresh = True
        call.permit = DispatchPermit(_MINT, item, receipt.owner_epoch, client, CALLER.get())
        return call.permit

    def consume(self, permit, client, call, url, body, headers, cancel):
        if type(permit) is not DispatchPermit:
            raise CoordinationError("dispatch_permit_required")
        with permit._lock:
            if (permit._client is not client or permit._caller is not CALLER.get()
                    or permit._pid != os.getpid() or permit._thread != threading.get_ident()):
                raise CoordinationError("dispatch_caller_mismatch")
            if permit._used:
                raise CoordinationError("dispatch_permit_consumed")
            permit._used = True
            self._check()
            if (call.permit is not permit or permit.epoch != self._owner.epoch
                    or self._request(client, call, url, body, headers) != permit.attempt):
                raise CoordinationError("dispatch_request_mismatch")
            self.ledger.validate_dispatch(self._owner, permit.attempt, expected_run=call.grant.run)
            if cancel is not None and cancel.is_set():
                raise CoordinationError("cancelled_before_dispatch")

    def post(self, client, call, *, url, body, headers, on_dispatch, before_dispatch=None, **kwargs):
        self._check()
        permit = self.prepare(client, call, url, body, headers)
        # Only a committed fresh SQLite intent may be projected to checkpoint.
        # Replays/ambiguous commit receipts never reach this callback.
        if before_dispatch is not None:
            before_dispatch()
        self.consume(permit, client, call, url, body, headers, kwargs.get("cancel"))
        self._check()
        on_dispatch()
        call.delegated = True
        # All transports, including injected ones, pass this boundary.
        call.response = client._transport.post(url, body=body, headers=headers, **kwargs)
        return call.response

    def finish(self, call, result, error):
        with self._condition:
            if call.finished:
                raise CoordinationError("dispatch_call_finished")
            call.finished = True
        try:
            return self._settle(call, result, error)
        except BaseException:
            self._failed.set()
            raise
        finally:
            with self._condition:
                self._calls -= 1
                self._condition.notify_all()

    def _settle(self, call, result, error):
        if not call.fresh or call.uncertain:
            return
        if fingerprint(call.grant.pricing.model_dump(mode="json")) != call.pricing_digest:
            raise CoordinationError("pricing_snapshot_mismatch")
        if not call.delegated:
            state = "not_sent"
        elif call.response is None or not 100 <= call.response.status < 500:
            state = "unknown"
        else:
            state = "terminal"
        usage = result.usage if result is not None else getattr(error, "usage", None)
        actual_units = usage_units(call.grant.pricing, usage) if state == "terminal" else None
        proof = {"attempt": call.attempt.attempt_id, "state": state,
                             "usage": usage.model_dump(mode="json") if usage is not None else None,
                             "status": call.response.status if call.response is not None else None,
                             "response_sha256": hashlib.sha256(call.response.body).hexdigest() if call.response is not None else None,
                             "result_sha256": fingerprint(result.model_dump(mode="json")) if result is not None else None,
                             "pricing_sha256": call.pricing_digest}
        outcome = Outcome(uuid5(UUID(call.attempt.attempt_id), "settlement").hex,
                          call.attempt.attempt_id, call.attempt.request_digest, fingerprint(proof), state, actual_units)
        if result is not None:
            if state != "terminal":
                raise CoordinationError("result_without_terminal_evidence")
            self.evidence.write(call.attempt, self._owner.epoch, result, outcome, proof)
        self.ledger.record_outcome(self._owner, outcome)
        if result is not None:
            # Account for a late response even after supersession, but do not
            # return that page as a current success. Immutable evidence stays.
            self.ledger.validate_dispatch(self._owner, call.attempt, expected_run=call.grant.run, evidence_only=True)
