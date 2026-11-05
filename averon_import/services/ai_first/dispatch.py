"""Process-local rolling quota leases; never infer dispatch from admission.

An admitted request stays pinned until its transport settles. Only then does
its rolling cooldown begin. This deliberately over-reserves slow requests so
preparation, checkpoint I/O and thread scheduling cannot expire a send permit.
This is not a cross-process provider coordinator.
"""
from __future__ import annotations

import asyncio
import threading
import time


class QuotaPermit:
    def __init__(self, owner, tokens: int, reservation_id: str | None = None):
        self.owner = owner
        self.tokens = tokens
        self.reservation_id = reservation_id
        self.finished_at: float | None = None

    def finish(self, *, request_started: bool) -> None:
        with self.owner._lock:
            if self not in self.owner.events:
                return
            if not request_started:
                self.owner.events.remove(self)
            elif self.finished_at is None:
                self.finished_at = time.monotonic()


class RollingQuota:
    def __init__(self, rpm: int, tpm: int, *, window_s: float):
        self.rpm, self.tpm, self.window_s = rpm, tpm, window_s
        self.events: list[QuotaPermit] = []
        self._lock = threading.Lock()

    def restore(self, tokens: int, age_s: float, reservation_id: str | None = None) -> None:
        with self._lock:
            now = time.monotonic()
            self.events[:] = [p for p in self.events if p.finished_at is None
                              or p.finished_at > now - self.window_s]
            if reservation_id is not None and any(p.reservation_id == reservation_id for p in self.events):
                return
            if age_s >= self.window_s:
                return
            permit = QuotaPermit(self, tokens, reservation_id)
            permit.finished_at = now - max(0.0, age_s)
            self.events.append(permit)

    async def acquire(self, token_count, cancel_event=None, stop_event=None, *, reservation_id=None):
        if token_count > self.tpm:
            return False
        while True:
            if ((cancel_event is not None and cancel_event.is_set())
                    or (stop_event is not None and stop_event.is_set())):
                return False
            with self._lock:
                now = time.monotonic()
                self.events[:] = [p for p in self.events if p.finished_at is None
                                  or p.finished_at > now - self.window_s]
                if (len(self.events) < self.rpm
                        and sum(p.tokens for p in self.events) + token_count <= self.tpm):
                    permit = QuotaPermit(self, token_count, reservation_id)
                    self.events.append(permit)
                    return permit
                # Polling also works across event loops; no loop-bound lock or
                # orphan cancellation waiter is left behind by task.cancel().
                deadlines = [p.finished_at + self.window_s - now for p in self.events
                             if p.finished_at is not None]
                delay = min(0.02, max(0.001, min(deadlines, default=0.02)))
            await asyncio.sleep(delay)


class ProcessCapacity:
    """Bound resources across all jobs and event loops in this process."""
    def __init__(self, limit: int):
        self.limit = limit
        self._slots = threading.BoundedSemaphore(limit)

    async def acquire(self, *events) -> bool:
        while not any(event is not None and event.is_set() for event in events):
            if self._slots.acquire(blocking=False):
                return True
            await asyncio.sleep(0.01)
        return False

    def release(self):
        self._slots.release()

    def acquire_sync(self):
        self._slots.acquire()


# A page holds its slot from before rendering through checkpoint settlement.
# The single renderer can never build an unbounded queue of prepared images.
PAGE_CAPACITY = ProcessCapacity(10)
RENDER_CAPACITY = ProcessCapacity(1)
SYNC_RENDER_CAPACITY = ProcessCapacity(1)
