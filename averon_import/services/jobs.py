from __future__ import annotations

import threading
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
        }


class JobService:
    def __init__(self, max_workers: int = 1):
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=max_workers)

    def submit(self, function: Callable[[Callable], Any]) -> Job:
        job = Job(uuid.uuid4().hex)
        with self.lock:
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
            except Exception as exc:
                with self.lock:
                    job.status = "failed"
                    job.error = str(exc)
                    job.traceback = traceback.format_exc()
                    job.message = "Ошибка"

        self.executor.submit(runner)
        return job

    def get(self, job_id: str) -> Job:
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            return self.jobs[job_id]
