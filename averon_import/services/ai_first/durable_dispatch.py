"""B2 one-shot dispatch boundary. No bootstrap, scheduler or publication API.

Only a fresh B1 receipt can mint a process/caller/thread-bound capability.
The capability is burned before revalidation; ambiguous commits never mint one.
Neither a recovered intent nor an outcome artifact authorizes another POST.
"""
from __future__ import annotations

import asyncio
import contextvars
from dataclasses import asdict, dataclass, replace
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
    Attempt, Binding, CommitReceiptUncertain, CoordinationError, DeferredWithoutIntent,
    DispatchGateRejected, Outcome, RUB_SCALE, Run, canonical,
    digest, fingerprint, integer, rub_units, uuid_hex,
)
from .dispatch_policy import durable_required
from .private_acl import secure_private_path, verify_private_path

CALLER = contextvars.ContextVar("averon_durable_dispatch_caller", default=None)
_MINT = object()

_RETRYABLE = frozenset({"ledger_busy", "dispatcher_busy", "database_locked"})
_PAGE_LOCAL = frozenset({
    "job_dispatch_claim_stale", "job_not_cancellable", "document_generation_changed",
    "billing_budget_exhausted", "request_cap_exhausted", "durable_job_not_dispatchable",
    "durable_job_page_not_claimable", "durable_job_claim_sequence_mismatch",
})
_INTENT_MISMATCH = frozenset({"dispatch_intent_mismatch", "dispatch_reserve_receipt_mismatch"})
_INTEGRITY = frozenset({
    "result_evidence_conflict", "historical_outcome_conflict", "outcome_conflict",
    "evidence_mismatch", "schema_mismatch", "capability_mismatch", "installation_mismatch",
    "storage_unverified", "owner_fenced", "clock_regressed", "dispatch_request_mismatch",
    "dispatch_caller_mismatch", "durable_authorization_required", "binding_mismatch",
    "policy_revision_mismatch", "result_evidence_invalid", "result_evidence_identity_mismatch",
})


def classify_fault(error):
    """§10.7 classifier: retry | retry_after | page_terminal | job_blocked | fatal."""
    if isinstance(error, DeferredWithoutIntent):
        return "retry"
    if isinstance(error, CommitReceiptUncertain):
        return "fatal"
    if isinstance(error, DispatchGateRejected):
        return "page_terminal"
    if isinstance(error, CoordinationError):
        if error.code in _RETRYABLE:
            return "retry"
        if error.code in _PAGE_LOCAL:
            return "page_terminal"
        if error.code in _INTENT_MISMATCH:
            return "job_blocked"
        if error.code in _INTEGRITY:
            return "fatal"
    return "fatal"


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
    def __init__(self, grant, page, body_digest, estimate, pricing_digest, job_claim=None):
        self.grant, self.page, self.body_digest, self.estimate = grant, page, body_digest, estimate
        self.job_claim = job_claim
        self.attempt = self.permit = self.response = None
        self.fresh = self.delegated = self.uncertain = False
        self.gate_committed = False
        self.deferred = None
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

    def _payload_bytes(self, payload):
        envelope = canonical({"payload": payload, "sha256": fingerprint(payload)}).encode()
        if len(envelope) > self.max_bytes:
            raise CoordinationError("result_evidence_size_limit")
        return envelope

    def _read_raw(self, path):
        verify_private_path(path, directory=False)
        with path.open("rb") as stream:
            raw = stream.read(self.max_bytes + 1)
        if len(raw) > self.max_bytes:
            raise CoordinationError("result_evidence_size_limit")
        return raw

    def write(self, attempt, epoch, result, outcome, proof, settlement=None):
        """Idempotent no-overwrite publication (v1 legacy, v2 job-managed)."""
        self._verify()
        version = 2 if (settlement is not None or result is None) else 1
        payload = {"version": version, "attempt": asdict(attempt), "owner_epoch": epoch,
                   "result": result.model_dump(mode="json") if result is not None else None,
                   "outcome": asdict(outcome), "proof": proof}
        if version == 2:
            payload["settlement"] = settlement
        envelope = self._payload_bytes(payload)
        final = self.path(attempt.attempt_id)
        pending = final.with_suffix(".pending")
        if final.exists():
            if self._read_raw(final) != envelope:
                raise CoordinationError("result_evidence_conflict")
            if pending.exists():
                if self._read_raw(pending) != envelope:
                    raise CoordinationError("result_evidence_conflict")
                pending.unlink()
                self._sync_directory()
            return
        if pending.exists():
            if self._read_raw(pending) != envelope:
                raise CoordinationError("result_evidence_conflict")
        else:
            descriptor = os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                secure_private_path(pending, directory=False)
                stream.write(envelope)
                stream.flush()
                os.fsync(stream.fileno())
        self._verify()
        try:
            # link is atomic and refuses replacement. A crash can leave .pending;
            # it never becomes a reason to resend or to trust incomplete evidence.
            os.link(pending, final)
        except FileExistsError:
            if self._read_raw(final) != envelope:
                raise CoordinationError("result_evidence_conflict") from None
        self._sync_directory()
        pending.unlink()
        self._sync_directory()

    def read(self, attempt_id):
        self._verify()
        path = self.path(attempt_id)
        if not path.exists():
            return None
        return self._validate_payload(self._read_raw(path), attempt_id)

    def _validate_payload(self, raw, attempt_id):
        try:
            from averon_import.services.ocr.qwen_vision_client import _json
            envelope = _json(raw)
            payload = envelope["payload"]
            version = payload["version"]
            if (set(envelope) != {"payload", "sha256"}
                    or type(version) is not int or version not in (1, 2)
                    or fingerprint(payload) != envelope["sha256"]
                    or type(payload.get("attempt")) is not dict
                    or payload["attempt"]["attempt_id"] != attempt_id):
                raise ValueError
            base = {"version", "attempt", "owner_epoch", "result", "outcome", "proof"}
            if version == 1:
                if set(payload) != base or type(payload["result"]) is not dict:
                    raise ValueError
            else:
                settlement = payload.get("settlement")
                if (set(payload) != base | {"settlement"} or type(settlement) is not dict
                        or set(settlement) != {"page_state", "safe_code", "result_bytes"}
                        or settlement["page_state"] not in ("completed", "incomplete", "failed")
                        or (settlement["safe_code"] is not None
                            and type(settlement["safe_code"]) is not str)
                        or type(settlement["result_bytes"]) is not int
                        or not 0 <= settlement["result_bytes"] <= 16_000_000
                        or (payload["result"] is not None and type(payload["result"]) is not dict)):
                    raise ValueError
            if (any(type(payload[key]) is not dict for key in ("attempt", "outcome", "proof"))
                    or set(payload["proof"]) != {"attempt", "state", "usage", "status",
                                                 "response_sha256", "result_sha256", "pricing_sha256"}
                    or type(payload["owner_epoch"]) is not int or payload["owner_epoch"] < 1):
                raise ValueError
            return payload
        except (ValueError, TypeError, KeyError, RecursionError):
            raise CoordinationError("result_evidence_invalid") from None

    def recover_publication(self):
        """Promote only validated pending files; never settle or index pending."""
        self._verify()
        published, conflicts = [], []
        for pending in sorted(self.root.glob("*.pending")):
            if not re.fullmatch(r"[0-9a-f]{32}\.pending", pending.name):
                raise CoordinationError("evidence_root_not_dedicated")
            attempt_id = pending.name[:-len(".pending")]
            try:
                raw = self._read_raw(pending)
                self._validate_payload(raw, attempt_id)
                final = self.path(attempt_id)
                if final.exists():
                    if self._read_raw(final) != raw:
                        raise CoordinationError("result_evidence_conflict")
                else:
                    try:
                        os.link(pending, final)
                    except FileExistsError:
                        if self._read_raw(final) != raw:
                            raise CoordinationError("result_evidence_conflict") from None
                    self._sync_directory()
                # Independently re-read and validate the immutable final path.
                self._validate_payload(self._read_raw(final), attempt_id)
                pending.unlink()
                self._sync_directory()
                published.append(attempt_id)
            except CoordinationError as exc:
                conflicts.append((attempt_id, exc.code))
        return tuple(published), tuple(conflicts)


class DurableDispatcher:
    """Own the OS lock through all HTTP, settlement and cancellation draining.

    Construct/close off-loop. The evidence root must be provisioned separately;
    neither it nor a missing financial ledger is initialized here.
    """
    def __init__(self, root, *, installation_id, evidence_root, enabled=False,
                 job_managed=False, **ledger_options):
        if enabled is not True or not durable_required():
            raise CoordinationError("durable_mode_required")
        if type(job_managed) is not bool:
            raise CoordinationError("invalid_job_managed_mode")
        self.ledger = CoordinationLedger(root, installation_id=installation_id, **ledger_options)
        self.evidence = ResultEvidence(evidence_root)
        if self.evidence.root.is_relative_to(self.ledger.root) or self.ledger.root.is_relative_to(self.evidence.root):
            raise CoordinationError("evidence_ledger_roots_overlap")
        self.job_managed = job_managed
        # Legacy B2 keeps the blanket intent->unknown acquisition. Job-managed
        # mode must acquire without mutating any old attempt; recovery below
        # classifies those attempts from page identity and final evidence.
        self._owner = (self.ledger.dispatcher_for_jobs() if job_managed
                       else self.ledger.dispatcher())
        self._admission_open = not job_managed
        self.startup = None
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
            if job_managed:
                self.startup = self.startup_recovery()
        except BaseException:
            self._stop.set()
            self._heartbeat.join(timeout=5)
            self._owner.close()
            raise

    def startup_recovery(self):
        """Owner adoption, publication recovery and unsettled classification.

        Scheduler admission stays closed unless every prior-epoch job was
        adopted and every prior-epoch attempt classified without quarantine.
        """
        if not self.job_managed:
            raise CoordinationError("job_managed_required")
        adoption = self.ledger.adopt_job_owner_epochs(self._owner)
        published, conflicts = self.evidence.recover_publication()
        recovery = self.ledger.recover_unsettled(self._owner, evidence=self.evidence)
        barrier = self.ledger.startup_barrier(self._owner)
        clean = (adoption.clean and recovery.clean and not conflicts
                 and not barrier["unauthorized"] and not barrier["unsettled"]
                 and not barrier["stale_claims"])
        self._admission_open = clean
        return {"adoption": adoption, "published": published,
                "publication_conflicts": conflicts, "recovery": recovery,
                "barrier": barrier, "admission_open": clean}

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
        state = self.ledger.verify_registered_run(self._owner, grant.run)
        if state == "absent":
            try:
                self.ledger.register_run(
                    self._owner, uuid5(UUID(grant.run.run_id), "register").hex, grant.run)
            except CoordinationError as exc:
                # A concurrent identical registration is adopted, never failed.
                if (exc.code not in ("generation_conflict", "run_identity_conflict")
                        or self.ledger.verify_registered_run(self._owner, grant.run) != "exact"):
                    raise
        elif state != "exact":
            raise CoordinationError("generation_conflict")
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

    def call(self, client, page, body_digest, estimate, *, job_claim=None):
        with self._condition:
            self._check()
            if client not in self._registered:
                raise CoordinationError("durable_run_not_registered")
            caller, grant = self._clients[client]
            if CALLER.get() is not caller:
                raise CoordinationError("dispatch_caller_mismatch")
            if job_claim is not None:
                from .durable_job_contracts import JobPageDispatchClaim
                if (type(job_claim) is not JobPageDispatchClaim
                        or job_claim.run_id != grant.run.run_id
                        or job_claim.page_number != page):
                    raise CoordinationError("invalid_job_dispatch_claim")
            call = DispatchCall(grant, page, body_digest, estimate, self._pricing[client], job_claim)
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
        identity = (call.job_claim.candidate_attempt_id if call.job_claim is not None
                    else self.attempt_id(grant.run.run_id, call.page))
        claim_seq = call.job_claim.claim_seq if call.job_claim is not None else 1
        estimate = call.estimate
        return Attempt(uuid5(UUID(identity), "reserve").hex, identity, grant.run.run_id,
                       call.page, 1, claim_seq, grant.binding, grant.billing_revision, grant.quota_revisions,
                       digest, estimate.reserved_input_tokens, estimate.reserved_output_tokens,
                       reserve_units(grant.pricing, estimate.reserved_input_tokens,
                                     estimate.reserved_output_tokens))

    def prepare(self, client, call, url, body, headers):
        """Internal transport caller only. Replaying reserve never mints a permit."""
        self._check()
        item = self._request(client, call, url, body, headers)
        call.attempt = item
        try:
            if call.job_claim is None:
                receipt = self.ledger.reserve(self._owner, item)
            else:
                receipt = self.ledger.reserve_job_managed(self._owner, item, call.job_claim)
        except DeferredWithoutIntent as exc:
            call.deferred = exc
            raise
        except BaseException as exc:
            fault = classify_fault(exc)
            if self.job_managed and fault == "retry":
                # Busy/backpressure: no attempt was created and the dispatcher
                # stays alive for a bounded retry.
                call.uncertain = False
                raise
            call.uncertain = True
            # Legacy B2 preserves its fail-closed behavior for every
            # non-deferred preparation error.
            if not self.job_managed or fault == "fatal":
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
        if call.job_claim is not None:
            # Reserve committed the page as intent and advanced its revision.
            # The gate must use that exact post-reserve projection.
            if receipt.page_revision is None or receipt.job_revision is None:
                raise CoordinationError("dispatch_reserve_receipt_mismatch")
            call.job_claim = replace(
                call.job_claim,
                page_revision=receipt.page_revision,
                job_revision=receipt.job_revision,
                claim_seq=receipt.claim_seq,
                cancel_revision=receipt.claim_cancel_revision,
                owner_epoch=receipt.owner_epoch,
            )
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
            if call.job_claim is not None:
                try:
                    self.ledger.commit_job_send_gate(
                        self._owner, permit.attempt, expected_run=call.grant.run,
                        claim=call.job_claim,
                    )
                except DispatchGateRejected:
                    raise
                except CommitReceiptUncertain:
                    call.uncertain = True
                    self._failed.set()
                    raise
                except BaseException:
                    # A lost commit receipt must never become a retryable POST.
                    # Retain the intent and fail this dispatcher closed.
                    call.uncertain = True
                    self._failed.set()
                    raise CommitReceiptUncertain("job_send_gate_receipt_uncertain") from None
                call.gate_committed = True
                # From this point onward local cancellation cannot revoke SQL
                # authorization. A failure before transport is ambiguous, not
                # proof that no send was admitted.
                call.delegated = True
            else:
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
        if not call.gate_committed:
            self._check()
        on_dispatch()
        if not call.gate_committed:
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
        except BaseException as exc:
            if not self.job_managed or classify_fault(exc) == "fatal":
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
        claim = (self.ledger.claim_for_attempt(self._owner, call.attempt.attempt_id)
                 if call.job_claim is not None else None)
        evidence_digest = fingerprint(proof)
        if claim is not None and state == "not_sent":
            # Current worker and recovery must emit the identical canonical
            # pre-gate proof, derived only from persisted immutable identity.
            evidence_digest = CoordinationLedger._pre_gate_proof(
                call.attempt.attempt_id, call.attempt.run_id, call.attempt.page_number,
                claim.claim_seq, claim.owner_epoch, "intent")
        outcome = Outcome(uuid5(UUID(call.attempt.attempt_id), "settlement").hex,
                          call.attempt.attempt_id, call.attempt.request_digest,
                          evidence_digest, state, actual_units)
        projection = (self._page_projection(result, outcome)
                      if claim is not None and state == "terminal" else None)
        if result is not None or projection is not None:
            if state != "terminal":
                raise CoordinationError("result_without_terminal_evidence")
            settlement = (None if projection is None else {
                "page_state": projection.state, "safe_code": projection.safe_code,
                "result_bytes": projection.result_bytes})
            self.evidence.write(call.attempt, self._owner.epoch, result, outcome, proof,
                                settlement)
        if claim is None:
            self.ledger.record_outcome(self._owner, outcome)
        else:
            self.ledger.record_job_outcome(
                self._owner, outcome, job_claim=claim, page=projection,
                generation=call.grant.run.generation,
            )
        if result is not None:
            # Account for a late response even after supersession, but do not
            # return that page as a current success. Immutable evidence stays.
            self.ledger.validate_dispatch(self._owner, call.attempt, expected_run=call.grant.run, evidence_only=True)

    @staticmethod
    def _page_projection(result, outcome):
        """Deterministic page projection from the canonical final result."""
        from .durable_job_contracts import PageOutcomeProjection
        if result is None:
            return PageOutcomeProjection("failed", "result_invalid", 0,
                                         outcome.evidence_digest)
        extraction = result.extraction
        if extraction.status in ("extracted", "no_specification"):
            state, code = "completed", None
        elif extraction.status == "incomplete":
            state, code = "incomplete", None
        elif extraction.status == "failed":
            state, code = "failed", extraction.error_code or "result_invalid"
        else:
            raise CoordinationError("result_evidence_invalid")
        size = len(canonical(result.model_dump(mode="json")).encode("utf-8"))
        return PageOutcomeProjection(state, code, size, outcome.evidence_digest)
