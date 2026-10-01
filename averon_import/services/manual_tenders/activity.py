from __future__ import annotations

import threading
from dataclasses import dataclass, field


class TenderActivityConflict(RuntimeError):
    pass


@dataclass
class _Lease:
    registry: "TenderActivityRegistry"
    tender_id: str
    released: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def release(self) -> None:
        with self._lock:
            if self.released:
                return
            self.released = True
        self.registry._release(self.tender_id)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.release()


class TenderActivityRegistry:
    """Single-process leases protect a workspace from lazy cleanup/delete."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: dict[str, int] = {}
        self._deleting: set[str] = set()

    def acquire(self, tender_id: str) -> _Lease:
        with self._lock:
            if tender_id in self._deleting:
                raise TenderActivityConflict("Тендер сейчас используется.")
            self._active[tender_id] = self._active.get(tender_id, 0) + 1
        return _Lease(self, tender_id)

    def _release(self, tender_id: str) -> None:
        with self._lock:
            count = self._active.get(tender_id, 0)
            if count <= 1:
                self._active.pop(tender_id, None)
            else:
                self._active[tender_id] = count - 1

    def begin_delete(self, tender_id: str) -> bool:
        with self._lock:
            if tender_id in self._deleting or self._active.get(tender_id, 0):
                return False
            self._deleting.add(tender_id)
            return True

    def finish_delete(self, tender_id: str) -> None:
        with self._lock:
            self._deleting.discard(tender_id)

    def active(self, tender_id: str) -> bool:
        with self._lock:
            return bool(self._active.get(tender_id, 0))


__all__ = ["TenderActivityConflict", "TenderActivityRegistry"]
