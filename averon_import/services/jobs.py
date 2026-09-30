from __future__ import annotations

import hashlib
import json
import threading
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable


DOCUMENT_PROCESSING = "document_processing"
SOURCING = "sourcing"
LANE_CAPACITIES = {DOCUMENT_PROCESSING: (1, 2), SOURCING: (1, 2)}
QUEUE_WAIT_SECONDS = 180
TERMINAL_TTL_SECONDS = 15 * 60
MAX_TERMINAL_JOBS = 64


class JobAdmissionError(RuntimeError):
    """Safe, typed admission failure suitable for an HTTP 409 response."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Job:
    id: str
    lane: str = DOCUMENT_PROCESSING
    kind: str = "operation"
    owner_id: str = "__internal__"
    dedupe_key: str | None = None
    document_id: str | None = None
    status: str = "queued"
    current: int = 0
    total: int = 0
    message: str = "В очереди"
    result: Any = None
    error: str | None = None
    error_code: str | None = None
    traceback: str | None = field(default=None, repr=False)
    created_at: str = field(default_factory=lambda: _now_iso())
    started_at: str | None = None
    finished_at: str | None = None
    _created_monotonic: float = field(default_factory=lambda: __import__("time").monotonic(), repr=False)
    _finished_monotonic: float | None = field(default=None, repr=False)
    _future: Future | None = field(default=None, repr=False)
    _release_callbacks: list[Callable[[], None]] = field(default_factory=list, repr=False)
    _released: bool = field(default=False, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "lane": self.lane,
            "kind": self.kind,
            "status": self.status,
            "current": self.current,
            "total": self.total,
            "message": self.message,
            "result": self.result if self.status == "completed" else None,
            "error": self.error,
            "error_code": self.error_code,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @property
    def active(self) -> bool:
        return self.status in {"queued", "running"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_fingerprint(payload: Any) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class _ExecutorShutdown:
    """Compatibility handle: old callers shut down JobService.executor."""

    def __init__(self, executors: dict[str, ThreadPoolExecutor]):
        self._executors = executors

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        for executor in self._executors.values():
            executor.shutdown(wait=wait, cancel_futures=cancel_futures)


class JobCoordinator:
    """Bounded process-local coordinator with one serialized worker per lane."""

    def __init__(
        self,
        *,
        capacities: dict[str, tuple[int, int]] | None = None,
        queue_wait_seconds: float = QUEUE_WAIT_SECONDS,
        terminal_ttl_seconds: float = TERMINAL_TTL_SECONDS,
        max_terminal_jobs: int = MAX_TERMINAL_JOBS,
    ) -> None:
        self.capacities = dict(capacities or LANE_CAPACITIES)
        self.queue_wait_seconds = max(0.0, float(queue_wait_seconds))
        self.terminal_ttl_seconds = max(0.0, float(terminal_ttl_seconds))
        self.max_terminal_jobs = max(1, int(max_terminal_jobs))
        self.jobs: dict[str, Job] = {}
        self.lock = threading.RLock()
        self._executors = {
            lane: ThreadPoolExecutor(max_workers=max(1, limits[0]), thread_name_prefix=f"averon-{lane}")
            for lane, limits in self.capacities.items()
        }
        self.executor = _ExecutorShutdown(self._executors)

    def submit(
        self,
        function: Callable[[Callable], Any],
        *,
        lane: str = DOCUMENT_PROCESSING,
        kind: str = "operation",
        owner_id: str = "__internal__",
        document_id: str | None = None,
        dedupe_key: str | None = None,
        release_callbacks: Iterable[Callable[[], None]] = (),
        fail_if_lane_occupied: bool = False,
    ) -> Job:
        callbacks = list(release_callbacks)
        with self.lock:
            self._cleanup_locked()
            if lane not in self._executors:
                self._release_callbacks(callbacks)
                raise ValueError(f"Unknown job lane: {lane}")

            existing = self._find_duplicate_locked(owner_id, dedupe_key)
            if existing is not None:
                self._release_callbacks(callbacks)
                return existing

            if document_id is not None:
                conflict = next(
                    (
                        item for item in self.jobs.values()
                        if item.active and item.document_id == document_id
                    ),
                    None,
                )
                if conflict is not None:
                    self._release_callbacks(callbacks)
                    raise JobAdmissionError(
                        "DOCUMENT_JOB_BUSY",
                        "Для этого документа уже выполняется или ожидает другое задание.",
                    )

            running_capacity, queued_capacity = self.capacities[lane]
            active = [item for item in self.jobs.values() if item.active and item.lane == lane]
            if fail_if_lane_occupied and active:
                self._release_callbacks(callbacks)
                raise JobAdmissionError(
                    "JOB_LANE_BUSY",
                    "Выполняется другое задание обслуживания. Повторите позже.",
                )
            running = sum(item.status == "running" for item in active)
            queued = sum(item.status == "queued" for item in active)
            if running >= running_capacity and queued >= queued_capacity:
                self._release_callbacks(callbacks)
                raise JobAdmissionError(
                    "JOB_LANE_BUSY",
                    "Очередь заданий занята. Повторите попытку позже.",
                )

            job = Job(
                uuid.uuid4().hex,
                lane=lane,
                kind=kind,
                owner_id=owner_id,
                dedupe_key=dedupe_key,
                document_id=document_id,
                _release_callbacks=callbacks,
            )
            self.jobs[job.id] = job

            def progress(current: int, total: int, message: str) -> None:
                with self.lock:
                    if job.status == "running":
                        job.current = current
                        job.total = total
                        job.message = message

            def runner() -> None:
                with self.lock:
                    if job.status != "queued":
                        return
                    if self._queue_expired(job):
                        self._finish_locked(job, "expired", "Задание истекло в очереди. Запустите его повторно.", "JOB_QUEUE_EXPIRED")
                        return
                    job.status = "running"
                    job.message = "Выполняется"
                    job.started_at = _now_iso()
                try:
                    result = function(progress)
                    with self.lock:
                        job.result = result
                        self._finish_locked(job, "completed", "Готово", None)
                        self._cleanup_locked()
                except Exception as exc:
                    with self.lock:
                        job.error = _safe_job_error(exc)
                        job.error_code = _safe_job_error_code(getattr(exc, "code", None))
                        job.traceback = traceback.format_exc()
                        self._finish_locked(job, "failed", "Ошибка", job.error_code)
                        self._cleanup_locked()

            try:
                job._future = self._executors[lane].submit(runner)
            except Exception:
                self.jobs.pop(job.id, None)
                self._release_job_locked(job)
                raise
            return job

    def get(self, job_id: str, *, owner_id: str | None = None) -> Job:
        with self.lock:
            self._cleanup_locked()
            job = self.jobs.get(job_id)
            if job is None or (owner_id is not None and job.owner_id != owner_id):
                raise KeyError(job_id)
            return job

    def _find_duplicate_locked(self, owner_id: str, dedupe_key: str | None) -> Job | None:
        if not dedupe_key:
            return None
        return next(
            (
                job for job in self.jobs.values()
                if job.active and job.owner_id == owner_id and job.dedupe_key == dedupe_key
            ),
            None,
        )

    def _queue_expired(self, job: Job) -> bool:
        import time

        return time.monotonic() - job._created_monotonic >= self.queue_wait_seconds

    def _cleanup_locked(self) -> None:
        import time

        now = time.monotonic()
        for job in list(self.jobs.values()):
            if job.status == "queued" and now - job._created_monotonic >= self.queue_wait_seconds:
                future = job._future
                if future is None or future.cancel():
                    self._finish_locked(
                        job,
                        "expired",
                        "Задание истекло в очереди. Запустите его повторно.",
                        "JOB_QUEUE_EXPIRED",
                    )
        terminal = [job for job in self.jobs.values() if not job.active]
        expired_ids = {
            job.id for job in terminal
            if job._finished_monotonic is not None
            and job._finished_monotonic <= now - self.terminal_ttl_seconds
        }
        for job_id in expired_ids:
            self.jobs.pop(job_id, None)
        terminal = sorted(
            (job for job in self.jobs.values() if not job.active),
            key=lambda item: item.finished_at or item.created_at,
        )
        for job in terminal[: max(0, len(terminal) - self.max_terminal_jobs)]:
            self.jobs.pop(job.id, None)

    def _finish_locked(self, job: Job, status: str, message: str, error_code: str | None) -> None:
        if not job.active:
            return
        job.status = status
        job.message = message
        job.error_code = error_code
        if status == "expired":
            job.error = message
        job.finished_at = _now_iso()
        import time

        job._finished_monotonic = time.monotonic()
        self._release_job_locked(job)

    def _release_job_locked(self, job: Job) -> None:
        if job._released:
            return
        job._released = True
        callbacks, job._release_callbacks = job._release_callbacks, []
        self._release_callbacks(callbacks)

    @staticmethod
    def _release_callbacks(callbacks: Iterable[Callable[[], None]]) -> None:
        for callback in callbacks:
            try:
                callback()
            except Exception:
                # Resource release is best-effort but attempted exactly once.
                pass


class JobService(JobCoordinator):
    """Backward-compatible name for the process-local coordinator."""

    def __init__(self, max_workers: int = 1, **kwargs: Any):
        # max_workers remains accepted for downstream test/integration callers;
        # Phase 1 always enforces the two explicit single-worker lanes.
        super().__init__(**kwargs)


def _safe_job_error(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    lowered = message.casefold()
    if not message or len(message) > 240 or any(
        marker in lowered for marker in ("api-key", "authorization", "secret", "token", "password")
    ):
        return "Задание не выполнено. Подробности доступны в журнале приложения."
    return message


def _safe_job_error_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    lowered = code.casefold()
    valid_format = bool(code) and len(code) <= 80 and all(
        char.isascii() and (char.isalnum() or char in "_-") for char in code
    )
    sensitive = any(
        marker in lowered
        for marker in ("api-key", "authorization", "secret", "token", "password")
    )
    if not valid_format or sensitive:
        return "JOB_FAILED"
    return code
