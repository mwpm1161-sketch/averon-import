from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from averon_import.services.sourcing.models import ProductIntent, SourcingResult


class SourcingCache:
    """Small JSON cache keyed by intent fingerprint and catalog version."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None

    def _read(self) -> dict[str, Any]:
        if not self.path or not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, key: str) -> SourcingResult | None:
        payload = self._read().get(key)
        if not isinstance(payload, dict):
            return None

    def get_intent(self, key: str) -> tuple[ProductIntent, list[str]] | None:
        payload = self._read().get("__intents__", {}).get(key)
        if not isinstance(payload, dict):
            return None
        try:
            return (
                ProductIntent.model_validate(payload["intent"]),
                [str(item) for item in payload.get("warnings", [])],
            )
        except Exception:
            return None
        try:
            return SourcingResult.model_validate(payload)
        except Exception:
            return None

    def set(self, key: str, value: SourcingResult) -> None:
        if not self.path:
            return
        data = self._read()
        data[key] = value.model_dump(mode="json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def set_intent(self, key: str, intent: ProductIntent, warnings: list[str]) -> None:
        if not self.path:
            return
        data = self._read()
        intents = data.setdefault("__intents__", {})
        if not isinstance(intents, dict):
            intents = {}
            data["__intents__"] = intents
        intents[key] = {
            "intent": intent.model_dump(mode="json"),
            "warnings": list(warnings),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
