"""Explicit B2 adapter; never selected by the production composition factory."""
import asyncio
import hashlib
import threading

from .coordination_contracts import Attempt, Binding, CoordinationError, fingerprint, rub_units
from .durable_dispatch import (
    CALLER, DispatchBinding, HistoricalSettlementContext, drain_thread,
    reserve_units, units_rub, usage_units,
)
from .orchestration import AdapterPageResult, AdapterRequestError, PageAttempt, PageRunRecord, _fingerprint, _utc_now
from .qwen_vision_adapter import QwenVisionAdapter
from averon_import.services.ocr.qwen_vision_client import QwenVisionError


def configuration_digest(config, binding):
    return fingerprint({"orchestration": config.model_dump(mode="json"), "binding_digest": binding.fingerprint})


class DurableQwenVisionAdapter(QwenVisionAdapter):
    def __init__(self, client, *, dispatcher, dispatch_binding: DispatchBinding, **kwargs):
        if client._dispatch_boundary is not dispatcher:
            raise CoordinationError("durable_composition_mismatch")
        super().__init__(client, **kwargs)
        self.dispatcher, self.dispatch_binding = dispatcher, dispatch_binding
        self.durable_dispatch = self
        self._caller = object()
        self._descriptor = self._descriptor.model_copy(update={"parameters": {
            **self._descriptor.parameters, "coordination_protocol": "durable-b2-v1",
        }})
        dispatcher.attach(client, self._caller, dispatch_binding)

    def restore_quota(self, identity, records):
        # B1 is the sole account/quota authority. Never import checkpoint holds
        # into an independent process-local limiter on this path.
        return None

    def reserve_cost(self, estimate):
        return units_rub(reserve_units(self.dispatch_binding.pricing,
                                      estimate.reserved_input_tokens, estimate.reserved_output_tokens))

    def actual_cost(self, usage):
        return units_rub(usage_units(self.dispatch_binding.pricing, usage))

    def sum_costs(self, values):
        return units_rub(sum(rub_units(value) for value in values))

    @staticmethod
    def _has_dispatch_marker(attempt):
        return attempt.request_started or attempt.dispatched_at is not None or attempt.dispatch_intent_at is not None

    @classmethod
    def _uncommitted_preparation(cls, attempt):
        return (not cls._has_dispatch_marker(attempt) and attempt.state == "failed"
                and attempt.safe_error_code in {"durable_pre_intent_interrupted", "process_restarted_before_dispatch"})

    async def _reconcile_checkpoint(self, page, row, checkpoint, records):
        old = records.get(page)
        if row is None:
            if old is None:
                return
            # Missing history never clears an old dispatch marker/ambiguity.
            if (any(self._has_dispatch_marker(attempt) for attempt in old.attempts)
                    or old.state in {"unknown_outcome", "completed", "incomplete"}):
                raise CoordinationError("checkpoint_ledger_history_mismatch")
            if old.state == "pending":
                if any(not self._uncommitted_preparation(attempt) for attempt in old.attempts):
                    raise CoordinationError("checkpoint_ledger_history_mismatch")
                return
            if old.state != "running":
                return
            if any(not self._uncommitted_preparation(attempt) for attempt in old.attempts[:-1]):
                raise CoordinationError("checkpoint_ledger_history_mismatch")
            # Verified absence of an intent plus no checkpoint send evidence:
            # only preparation was interrupted. Preserve that history entry.
            now = _utc_now()
            latest = old.attempts[-1].model_copy(update={
                "state": "failed", "finished_at": now, "safe_error_code": "durable_pre_intent_interrupted",
            })
            records[page] = old.model_copy(update={
                "state": "pending", "attempts": [*old.attempts[:-1], latest], "updated_at": now,
            })
            await checkpoint.write(records[page])
            return
        if old is not None and old.state in {"completed", "incomplete"}:
            if row["state"] != "terminal":
                raise CoordinationError("checkpoint_ledger_history_mismatch")
            return
        now = _utc_now()
        attempts = old.attempts if old is not None else []
        if any(not self._uncommitted_preparation(attempt) for attempt in attempts[:-1]):
            raise CoordinationError("durable_checkpoint_history_mismatch")
        latest = attempts[-1] if attempts else PageAttempt(state="running", started_at=now)
        not_sent = row["state"] == "not_sent"
        state = "failed" if not_sent else "unknown_outcome"
        latest = latest.model_copy(update={
            "state": state, "finished_at": now,
            # Observation time of the SQLite intent; never evidence of a POST.
            "dispatch_intent_at": latest.dispatch_intent_at if not_sent else latest.dispatch_intent_at or now,
            "reserved_input_tokens": row["input_tokens"], "reserved_output_tokens": row["output_tokens"],
            "reserved_cost_rub": units_rub(row["reserve_units"]), "actual_cost_rub": units_rub(row["actual_units"]),
            "safe_error_code": "durable_not_sent_not_replayable" if not_sent else "durable_intent_recovered",
        })
        base = old or PageRunRecord(page_number=page, state="pending", updated_at=now)
        records[page] = base.model_copy(update={
            "state": state, "attempts": [*attempts[:-1], latest], "updated_at": now,
        })
        await checkpoint.write(records[page])

    async def start(self, identity, config, checkpoint, records):
        self._region_scoping = bool(config.page_regions)
        grant = self.dispatch_binding
        if (identity.run_id != grant.run.run_id or identity.source_sha256 != grant.run.source_digest
                or grant.run.config_digest != configuration_digest(config, grant.binding)
                or grant.run.budget_units != rub_units(config.max_budget_rub)
                or grant.run.request_cap != min(config.max_requests, self.max_requests)
                or config.pricing != grant.pricing):
            raise CoordinationError("durable_run_mismatch")
        _, cancelled = await drain_thread(self.dispatcher.register, self.client)
        if cancelled:
            raise asyncio.CancelledError
        history, cancelled = await drain_thread(self.dispatcher.ledger.checkpoint_attempts,
                                               self.dispatcher._owner, grant.run)
        if cancelled:
            raise asyncio.CancelledError
        by_page = {}
        for row in history:
            page = row["page_number"]
            if (page in by_page or not 1 <= page <= identity.page_count
                    or row["attempt_id"] != self.dispatcher.attempt_id(grant.run.run_id, page)
                    or row["attempt_no"] != 1 or row["claim_seq"] != 1):
                raise CoordinationError("durable_checkpoint_history_mismatch")
            by_page[page] = row
        for page in range(1, identity.page_count + 1):
            payload, cancelled = await drain_thread(self.dispatcher.evidence.read,
                self.dispatcher.attempt_id(grant.run.run_id, page))
            if cancelled:
                raise asyncio.CancelledError
            if payload is None:
                await self._reconcile_checkpoint(page, by_page.get(page), checkpoint, records)
                continue
            result, cancelled = await drain_thread(self._recover_result, payload, page, identity, config)
            if cancelled:
                raise asyncio.CancelledError
            old = records.get(page)
            if old is not None and old.state in ("completed", "incomplete"):
                if old.extraction != result.extraction:
                    raise CoordinationError("checkpoint_evidence_mismatch")
                continue
            if old is not None and any(not self._uncommitted_preparation(attempt) for attempt in old.attempts[:-1]):
                raise CoordinationError("durable_checkpoint_history_mismatch")
            now = _utc_now()
            state = "incomplete" if result.extraction.status == "incomplete" else "completed"
            attempt = old.attempts[-1] if old is not None and old.attempts else PageAttempt(state="running", started_at=now)
            attempt = attempt.model_copy(update={
                "state": state, "request_started": True, "dispatched_at": attempt.dispatched_at or attempt.started_at,
                "finished_at": now, "usage": result.usage,
                "reserved_input_tokens": payload["attempt"]["input_tokens"],
                "reserved_output_tokens": payload["attempt"]["output_tokens"],
                "reserved_cost_rub": units_rub(payload["attempt"]["reserve_units"]),
                "actual_cost_rub": self.actual_cost(result.usage),
                "http_status": result.http_status, "finish_reason": result.finish_reason,
                "request_latency_ms": result.request_latency_ms,
                "safe_error_code": "transcription_incomplete" if state == "incomplete" else None,
                "elapsed_s": attempt.elapsed_s or 0.0,
            })
            descriptor = self.descriptor
            records[page] = PageRunRecord(
                page_number=page, state=state, attempts=[*(old.attempts[:-1] if old is not None else []), attempt], extraction=result.extraction,
                provider_id=descriptor.provider_id, model_id=descriptor.model_id,
                model_version=descriptor.model_version, prompt_version=descriptor.prompt_version,
                schema_version=descriptor.schema_version, configuration_fingerprint=identity.adapter_fingerprint,
                updated_at=now,
            )
            await checkpoint.write(records[page])

    async def settle_saved_evidence(self, attempt_id, historical_context):
        """Explicit finance-only recovery; no current checkpoint or client gate."""
        receipt, cancelled = await drain_thread(
            self.dispatcher.settle_saved_evidence, attempt_id, historical_context,
        )
        if cancelled:
            raise asyncio.CancelledError
        return receipt

    def _recover_result(self, payload, page, identity, config):
        try:
            item = dict(payload["attempt"])
            item["binding"] = Binding(**{**item["binding"], "quota_scope_ids": tuple(item["binding"]["quota_scope_ids"])})
            item["quota_revisions"] = tuple(tuple(pair) for pair in item["quota_revisions"])
            item = Attempt(**item)
            result = AdapterPageResult.model_validate(payload["result"])
        except (ValueError, TypeError, KeyError):
            raise CoordinationError("result_evidence_invalid") from None
        grant = self.dispatch_binding
        if (item.run_id != grant.run.run_id or item.binding != grant.binding or item.page_number != page
                or item.attempt_id != self.dispatcher.attempt_id(grant.run.run_id, page)):
            raise CoordinationError("result_evidence_identity_mismatch")
        self.dispatcher.settle_saved_evidence(
            item.attempt_id, HistoricalSettlementContext.capture(grant.run, config),
        )
        # Finance is already durable. This check is only for optional projection
        # into the current checkpoint; a stale identity cannot undo settlement.
        current = self.dispatcher.evidence.read(item.attempt_id)
        if current is None or fingerprint(current) != fingerprint(payload):
            raise CoordinationError("result_evidence_changed")
        if (result.extraction.source_document_id != identity.source_document_id
                or result.extraction.page_number != page):
            raise CoordinationError("result_evidence_identity_mismatch")
        if identity.run_id.startswith("run-") or self._region_scoping:
            result = result.model_copy(update={"extraction": result.extraction.model_copy(update={
                "extraction_fingerprint": _fingerprint({"payload": result.extraction.extraction_fingerprint,
                    "run": identity.run_id, "adapter": identity.adapter_fingerprint,
                    "processing": identity.processing_fingerprint}),
            })})
        return result

    async def extract(self, request):
        if (request.run_id != self.dispatch_binding.run.run_id
                or request.source_document_id != "pdf-sha256:" + self.dispatch_binding.run.source_digest
                or request.image.page_number != request.page_number):
            raise AdapterRequestError("durable_run_mismatch", outcome_unknown=False, request_started=False)
        if request.cancel_event is not None and request.cancel_event.is_set():
            raise AdapterRequestError("cancelled_before_dispatch", outcome_unknown=False, request_started=False)
        estimate = self.estimate(request.image)
        token = CALLER.set(self._caller)
        call = None
        result = error = None
        thread_cancel = threading.Event()
        try:
            digest, cancelled = await drain_thread(lambda: hashlib.sha256(self.client._request_body(
                request.image.data, request.page_number, self._user_context)).hexdigest())
            if cancelled:
                raise AdapterRequestError("cancelled_before_dispatch", outcome_unknown=False, request_started=False)
            call = self.dispatcher.call(self.client, request.page_number, digest, estimate,
                                        job_claim=request.job_claim)
            loop = asyncio.get_running_loop()

            def before_dispatch():
                if request.before_dispatch is not None:
                    future = asyncio.run_coroutine_threadsafe(request.before_dispatch(), loop)
                    try:
                        future.result()
                    except AdapterRequestError as exc:
                        raise QwenVisionError(exc.safe_code) from None

            def on_dispatch():
                request.dispatch_started_at = _utc_now()

            def invoke():
                return self.client.extract(
                    request.image.data, request.page_number, cancel=thread_cancel,
                    user_context=self._user_context, before_dispatch=before_dispatch,
                    on_dispatch=on_dispatch, dispatch_call=call,
                )

            task = asyncio.create_task(self._call_client(invoke, thread_cancel))
            try:
                try:
                    completion = await asyncio.shield(task)
                except asyncio.CancelledError:
                    thread_cancel.set()
                    completion = await self._wait_for_thread(task, thread_cancel)
                result = self._convert_completion(completion, request)
            except QwenVisionError as exc:
                if call.deferred is not None:
                    raise call.deferred
                try:
                    result = self._raise_client_error(exc, request)
                except AdapterRequestError as failure:
                    error = failure
            except Exception:
                error = AdapterRequestError("durable_adapter_error", outcome_unknown=call.delegated,
                                            request_started=call.delegated)
            if call.uncertain or (error is not None and error.safe_code == "dispatch_already_reserved"):
                error = AdapterRequestError("durable_dispatch_uncertain", outcome_unknown=True, request_started=True)
            if error is not None:
                raise error
            return result
        finally:
            try:
                if call is not None:
                    try:
                        await drain_thread(self.dispatcher.finish, call, result, error)
                    except Exception:
                        raise AdapterRequestError("durable_settlement_failed", outcome_unknown=True,
                                                  request_started=call.delegated) from None
            finally:
                CALLER.reset(token)
