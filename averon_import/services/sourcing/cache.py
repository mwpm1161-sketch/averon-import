from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from averon_import.services.sourcing.models import (
    ProductIntent,
    ProductUnderstandingResult,
    SourcingResult,
)


_CACHE_LOCKS_GUARD = threading.Lock()
_CACHE_LOCKS: dict[str, threading.RLock] = {}


def _lock_for(path: Path | None) -> threading.RLock:
    if path is None:
        return threading.RLock()
    key = str(path.expanduser().resolve())
    with _CACHE_LOCKS_GUARD:
        return _CACHE_LOCKS.setdefault(key, threading.RLock())


class SourcingCache:
    """Small JSON cache keyed by intent fingerprint and catalog version."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self._lock = _lock_for(self.path)

    def _read(self) -> dict[str, Any]:
        if not self.path or not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, key: str) -> SourcingResult | None:
        with self._lock:
            payload = self._read().get(key)
        if not isinstance(payload, dict):
            return None
        try:
            return SourcingResult.model_validate(payload)
        except Exception:
            return None

    def get_intent(self, key: str) -> tuple[ProductIntent, list[str]] | None:
        with self._lock:
            understanding = self.get_understanding(key)
            if understanding is not None:
                return understanding.resolved_intent, list(understanding.warnings)
            intents = self._read().get("__intents__", {})
            payload = intents.get(key) if isinstance(intents, dict) else None
        if not isinstance(payload, dict):
            return None
        try:
            return (
                ProductIntent.model_validate(payload["intent"]),
                [str(item) for item in payload.get("warnings", [])],
            )
        except Exception:
            return None

    def get_understanding(self, key: str) -> ProductUnderstandingResult | None:
        with self._lock:
            intents = self._read().get("__intents__", {})
            payload = intents.get(key) if isinstance(intents, dict) else None
        if not isinstance(payload, dict):
            return None
        value = payload.get("understanding")
        if not isinstance(value, dict):
            return None
        try:
            return ProductUnderstandingResult.model_validate(value)
        except Exception:
            return None

    def set(self, key: str, value: SourcingResult) -> None:
        if not self.path:
            return
        with self._lock:
            data = self._read()
            data[key] = value.model_dump(mode="json")
            self._write(data)

    def set_intent(self, key: str, intent: ProductIntent, warnings: list[str]) -> None:
        if not self.path:
            return
        with self._lock:
            data = self._read()
            intents = data.setdefault("__intents__", {})
            if not isinstance(intents, dict):
                intents = {}
                data["__intents__"] = intents
            intents[key] = {
                "intent": intent.model_dump(mode="json"),
                "warnings": list(warnings),
            }
            self._write(data)

    def set_understanding(self, key: str, value: ProductUnderstandingResult) -> None:
        if not self.path:
            return
        with self._lock:
            data = self._read()
            intents = data.setdefault("__intents__", {})
            if not isinstance(intents, dict):
                intents = {}
                data["__intents__"] = intents
            intents[key] = {
                "intent": value.resolved_intent.model_dump(mode="json"),
                "warnings": list(value.warnings),
                "understanding": value.model_dump(mode="json"),
            }
            self._write(data)

    def _write(self, data: dict[str, Any]) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                json.dump(data, temporary, ensure_ascii=False, indent=2)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, self.path)
            temporary_name = None
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
