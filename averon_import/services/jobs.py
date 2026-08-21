from __future__ import annotations

import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable


@dataclass
class Job:
    id: str
    status: str = "queued"
    current: int = 0
    total: int = 0
    message: str = "Ожидание"
    result: Any = None
    error: str | None = None
    traceback: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    finished_at: str | None = None
    _finished_monotonic: float | None = field(default=None, repr=False)

    def public(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "current": self.current,
            "total": self.total,
            "message": self.message,
            "result": self.result if self.status == "completed" else None,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


class JobService:
    def __init__(
        self,
        max_workers: int = 1,
        *,
        ttl_seconds: float = 30 * 60,
        max_finished_jobs: int = 100,
    ):
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.ttl_seconds = ttl_seconds
        self.max_finished_jobs = max_finished_jobs

    def submit(self, function: Callable[[Callable], Any]) -> Job:
        job = Job(uuid.uuid4().hex)
        with self.lock:
            self._cleanup_locked()
            self.jobs[job.id] = job

        def progress(current: int, total: int, message: str) -> None:
            with self.lock:
                job.current = current
                job.total = total
                job.message = message

        def runner() -> None:
            with self.lock:
                job.status = "running"
            try:
                result = function(progress)
                with self.lock:
                    job.result = result
                    job.status = "completed"
                    job.message = "Готово"
                    self._mark_finished(job)
            except Exception as exc:
                with self.lock:
                    job.status = "failed"
                    job.error = str(exc)
                    job.traceback = traceback.format_exc()
                    job.message = "Ошибка"
                    self._mark_finished(job)

        self.executor.submit(runner)
        return job

    def get(self, job_id: str) -> Job:
        with self.lock:
            self._cleanup_locked()
            if job_id not in self.jobs:
                raise KeyError(job_id)
            return self.jobs[job_id]

    def cleanup(self) -> int:
        """Drop expired/excess finished jobs and return the number removed."""
        with self.lock:
            before = len(self.jobs)
            self._cleanup_locked()
            return before - len(self.jobs)

    @staticmethod
    def _mark_finished(job: Job) -> None:
        job.finished_at = datetime.now().isoformat()
        job._finished_monotonic = time.monotonic()

    def _cleanup_locked(self) -> None:
        now = time.monotonic()
        finished = [
            job
            for job in self.jobs.values()
            if job.status in {"completed", "failed"} and job._finished_monotonic is not None
        ]
        expired_ids = {
            job.id
            for job in finished
            if self.ttl_seconds >= 0
            and now - (job._finished_monotonic or now) > self.ttl_seconds
        }
        for job_id in expired_ids:
            self.jobs.pop(job_id, None)

        if self.max_finished_jobs < 0:
            return
        remaining_finished = sorted(
            (
                job
                for job in self.jobs.values()
                if job.status in {"completed", "failed"}
                and job._finished_monotonic is not None
            ),
            key=lambda item: item._finished_monotonic or 0,
        )
        excess = max(0, len(remaining_finished) - self.max_finished_jobs)
        for job in remaining_finished[:excess]:
            self.jobs.pop(job.id, None)
