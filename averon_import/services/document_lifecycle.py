"""Process-local coordination between workspace operations and hard deletion.

The registry is intentionally process-local, matching DocumentMutationLocks and
the application's current single-process deployment contract.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import threading
from typing import Callable, Iterator

from averon_import.services.workspace import validate_document_id


class DocumentUnavailable(FileNotFoundError):
    """The workspace is being deleted or its id is invalid."""


@dataclass(slots=True)
class _DocumentActivity:
    active_operations: int = 0
    deleting: bool = False


class DocumentActivityRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._documents: dict[str, _DocumentActivity] = {}

    @contextmanager
    def lease(self, document_id: str) -> Iterator[None]:
        release = self.acquire(document_id)
        try:
            yield
        finally:
            release()

    def acquire(self, document_id: str) -> Callable[[], None]:
        """Reserve an operation before queuing background work; return release()."""
        try:
            document_id = validate_document_id(document_id)
        except ValueError as exc:
            raise DocumentUnavailable(document_id) from exc
        with self._lock:
            activity = self._documents.setdefault(document_id, _DocumentActivity())
            if activity.deleting:
                raise DocumentUnavailable(document_id)
            activity.active_operations += 1

        released = False
        release_lock = threading.Lock()

        def release() -> None:
            nonlocal released
            with release_lock:
                if released:
                    return
                released = True
            with self._lock:
                activity.active_operations = max(0, activity.active_operations - 1)
                if activity.active_operations == 0 and not activity.deleting:
                    self._documents.pop(document_id, None)

        return release

    def begin_delete(self, document_id: str) -> bool:
        document_id = validate_document_id(document_id)
        with self._lock:
            activity = self._documents.setdefault(document_id, _DocumentActivity())
            if activity.deleting:
                return False
            activity.deleting = True
            if activity.active_operations:
                activity.deleting = False
                return False
            return True

    def cancel_delete(self, document_id: str) -> None:
        with self._lock:
            activity = self._documents.get(document_id)
            if activity is None:
                return
            activity.deleting = False
            if activity.active_operations == 0:
                self._documents.pop(document_id, None)

    def finish_delete(self, document_id: str) -> None:
        with self._lock:
            activity = self._documents.get(document_id)
            if activity is not None and activity.active_operations == 0:
                self._documents.pop(document_id, None)

    def active_operations(self, document_id: str) -> int:
        with self._lock:
            activity = self._documents.get(document_id)
            return activity.active_operations if activity else 0
