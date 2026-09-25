"""In-process serialization for canonical mutations scoped to one document.

The application currently runs as a single process. These locks prevent lost
updates between request threads in that process; they are not a distributed
or multi-process locking mechanism.
"""

from __future__ import annotations

from contextlib import contextmanager
import threading
from typing import Iterator


class DocumentMutationLocks:
    """Provide independent re-entrant locks without retaining idle documents."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._entries: dict[str, tuple[threading.RLock, int]] = {}

    @contextmanager
    def for_document(self, document_id: str) -> Iterator[None]:
        key = str(document_id)
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                lock, users = threading.RLock(), 0
            else:
                lock, users = entry
            self._entries[key] = (lock, users + 1)
        acquired = False
        try:
            lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                lock.release()
            with self._guard:
                current = self._entries.get(key)
                if current is not None and current[0] is lock:
                    if current[1] <= 1:
                        del self._entries[key]
                    else:
                        self._entries[key] = (lock, current[1] - 1)


__all__ = ["DocumentMutationLocks"]
