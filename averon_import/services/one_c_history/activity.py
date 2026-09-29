"""Process-local shared/exclusive coordination for the active 1C snapshot.

This registry is app-owned and survives sourcing-runtime rebuilds. A future
multi-process deployment must replace it with, or back it by, a cross-process
lease; this phase targets Averon's current single-process runtime.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


class OneCHistoryActivityConflict(RuntimeError):
    def __init__(self, code: str, message: str, *, active_sourcing_count: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.active_sourcing_count = active_sourcing_count


@dataclass(slots=True)
class _Lease:
    registry: "OneCHistoryActivityRegistry"
    kind: str
    released: bool = False
    _release_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def release(self) -> None:
        with self._release_lock:
            if self.released:
                return
            self.released = True
        if self.kind == "sourcing":
            self.registry._release_sourcing()
        else:
            self.registry._finish_update()

    def __enter__(self) -> "_Lease":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


class OneCHistoryActivityRegistry:
    """Fail-fast shared readers and one exclusive active-history updater."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_sourcing_count = 0
        self._update_in_progress = False

    def acquire_sourcing(self) -> _Lease:
        with self._lock:
            if self._update_in_progress:
                raise OneCHistoryActivityConflict(
                    "ONE_C_HISTORY_UPDATING",
                    "История 1С обновляется. Повторите подбор через несколько секунд.",
                    active_sourcing_count=self._active_sourcing_count,
                )
            self._active_sourcing_count += 1
        return _Lease(self, "sourcing")

    def try_begin_update(self) -> _Lease:
        with self._lock:
            if self._update_in_progress:
                raise OneCHistoryActivityConflict(
                    "ONE_C_HISTORY_UPDATE_IN_PROGRESS",
                    "Другой пользователь уже обновляет историю 1С. Повторите позже.",
                    active_sourcing_count=self._active_sourcing_count,
                )
            if self._active_sourcing_count:
                raise OneCHistoryActivityConflict(
                    "ONE_C_HISTORY_IN_USE",
                    "История 1С сейчас используется в подборе. Повторите замену после завершения подбора.",
                    active_sourcing_count=self._active_sourcing_count,
                )
            self._update_in_progress = True
        return _Lease(self, "update")

    def _release_sourcing(self) -> None:
        with self._lock:
            if self._active_sourcing_count > 0:
                self._active_sourcing_count -= 1

    def _finish_update(self) -> None:
        with self._lock:
            self._update_in_progress = False

    def status(self) -> dict[str, int | bool]:
        with self._lock:
            count = self._active_sourcing_count
            updating = self._update_in_progress
        return {
            "active_sourcing_count": count,
            "update_in_progress": updating,
            "replacement_allowed": count == 0 and not updating,
        }


__all__ = ["OneCHistoryActivityConflict", "OneCHistoryActivityRegistry"]
