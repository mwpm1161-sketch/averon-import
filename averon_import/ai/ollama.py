from __future__ import annotations

import base64
import json
from typing import Any
from urllib import error, request

from averon_import.ai.models import AiReviewSuggestion


SYSTEM_PROMPT = """Ты проверяешь одну строку инженерной спецификации по OCR и изображению.
Не выдумывай отсутствующие данные. Особенно не меняй количество, артикул, марку,
DN/PN, мощность и размеры без достаточных оснований. Если поле не читается — верни
null и добавь имя поля в uncertain_fields. Ответ только JSON с ключами fields,
uncertain_fields, notes. fields может содержать только: position, name, type_mark,
code, manufacturer, unit, quantity, mass, note."""


class OllamaProvider:
    def __init__(
        self,
        model: str,
        *,
        base_url: str = "http://127.0.0.1:11434",
        timeout_seconds: float = 120.0,
    ):
        self.model = model.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        if not self.model:
            raise ValueError("Не указана локальная AI-модель")

    def _json_request(self, path: str, payload: dict | None = None) -> dict:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="GET" if payload is None else "POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.URLError as exc:
            raise RuntimeError(f"Ollama недоступен: {exc.reason}") from exc

    def health(self) -> dict[str, Any]:
        data = self._json_request("/api/tags")
        installed = [item.get("name", "") for item in data.get("models", [])]
        available = self.model in installed or any(
            name.split(":", 1)[0] == self.model.split(":", 1)[0] for name in installed
        )
        return {
            "available": available,
            "status": "ready" if available else "model_missing",
            "provider": "ollama",
            "model": self.model,
            "installed_models": installed,
        }

    def review_row(
        self,
        row: dict[str, Any],
        *,
        image_bytes: bytes | None = None,
    ) -> AiReviewSuggestion:
        evidence = row.get("ocr_evidence", {})
        user_payload = {
            "current": {key: row.get(key, "") for key in (
                "position", "name", "type_mark", "code", "manufacturer",
                "unit", "quantity", "mass", "note",
            )},
            "ocr_evidence": evidence,
            "row_confidence": row.get("confidence", 0),
            "critical_confidence": row.get("critical_confidence", 0),
        }
        message: dict[str, Any] = {
            "role": "user",
            "content": "Проверь строку и предложи только обоснованные исправления:\n"
            + json.dumps(user_payload, ensure_ascii=False),
        }
        if image_bytes:
            message["images"] = [base64.b64encode(image_bytes).decode("ascii")]

        data = self._json_request(
            "/api/chat",
            {
                "model": self.model,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.0},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    message,
                ],
            },
        )
        content = str(data.get("message", {}).get("content", "")).strip()
        if content.startswith("```"):
            content = content.strip("`")
            if content.startswith("json"):
                content = content[4:].lstrip()
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Локальная модель вернула некорректный JSON") from exc

        allowed = {
            "position", "name", "type_mark", "code", "manufacturer",
            "unit", "quantity", "mass", "note",
        }
        fields = {
            key: value
            for key, value in dict(parsed.get("fields", {})).items()
            if key in allowed and (value is None or isinstance(value, str))
        }
        uncertain = [
            key for key in parsed.get("uncertain_fields", [])
            if isinstance(key, str) and key in allowed
        ]
        return AiReviewSuggestion(
            fields=fields,
            uncertain_fields=uncertain,
            notes=str(parsed.get("notes", "")),
            provider="ollama",
            model=self.model,
        )
