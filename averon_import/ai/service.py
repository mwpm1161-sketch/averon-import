from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from typing import Any

from averon_import.ai.config import AiSettings
from averon_import.ai.provider import AiProviderError, OpenAICompatibleProvider
from averon_import.ai.schemas import AiCorrectionResponse
from averon_import.core.constants import BASE_COLUMNS

ProgressCallback = Callable[[int, int, str], None]

EDITABLE_FIELDS = tuple(column["key"] for column in BASE_COLUMNS)
HUMAN_LOCKED_STATUSES = {"verified", "edited"}

SYSTEM_PROMPT = """Ты выполняешь финальную проверку OCR инженерной спецификации.
Работай как консервативный инженер-корректор, а не как генератор текста.

Правила:
1. Исправляй только очевидные ошибки OCR: смешение кириллицы/латиницы, пропущенные или лишние символы, дефисы, пробелы, десятичные разделители и опечатки.
2. Не придумывай отсутствующие характеристики, марки, коды, производителей, единицы, количества или массу.
3. Если исходное поле пустое — оставь его пустым.
4. Сохраняй инженерные обозначения, ГОСТ, ТУ, маркировку, регистр, знаки ×/x, дроби и единицы измерения максимально близко к источнику.
5. Для position, quantity и mass будь особенно консервативен: меняй только при почти однозначной OCR-ошибке.
6. Не меняй смысл позиции и не подбирай аналог оборудования.
7. Верни ТОЛЬКО JSON без пояснений вокруг него.

Формат ответа:
{"rows":[{"id":"<id>","values":{"name":"..."},"confidence":0.97,"reason":"кратко"}]}

В values возвращай только поля, которые действительно нужно изменить. Если строка корректна, ее можно не возвращать.
"""


class AiCorrectionService:
    def __init__(
        self,
        settings: AiSettings | None = None,
        providers: dict[str, Any] | None = None,
    ) -> None:
        self.settings = settings or AiSettings.from_env()
        self.providers = providers or {
            "local": OpenAICompatibleProvider(
                self.settings.local,
                timeout_seconds=self.settings.timeout_seconds,
                temperature=self.settings.temperature,
                max_tokens=self.settings.max_tokens,
            ),
            "yandex": OpenAICompatibleProvider(
                self.settings.yandex,
                timeout_seconds=self.settings.timeout_seconds,
                temperature=self.settings.temperature,
                max_tokens=self.settings.max_tokens,
            ),
        }

    @classmethod
    def from_env(cls) -> "AiCorrectionService":
        return cls(AiSettings.from_env())

    def public_config(self) -> dict:
        return self.settings.public()

    def health(self) -> dict:
        providers: dict[str, dict] = {}
        for key, provider in self.providers.items():
            providers[key] = {
                "label": provider.label,
                "configured": bool(provider.configured),
                "model": provider.model,
            }
        return {"providers": providers}

    def ensure_provider(self, key: str) -> Any:
        if key not in self.providers:
            raise ValueError(f"Неизвестный AI-провайдер: {key}")
        provider = self.providers[key]
        if not provider.configured:
            raise ValueError(f"AI-провайдер «{provider.label}» не настроен")
        return provider

    def correct_result(
        self,
        result: dict,
        provider_key: str,
        progress: ProgressCallback | None = None,
    ) -> dict:
        provider = self.ensure_provider(provider_key)
        rows = result.get("rows", [])
        candidates = [row for row in rows if self._candidate(row)]
        batches = list(_chunks(candidates, self.settings.batch_size))
        warnings: list[str] = []
        changed_rows = 0
        changed_cells = 0

        for index, batch in enumerate(batches, start=1):
            if progress:
                progress(index - 1, max(1, len(batches)), f"ИИ: проверка блока {index}/{len(batches)}")
            try:
                response = self._request_batch(provider, batch)
                row_count, cell_count = self._apply_response(batch, response, provider)
                changed_rows += row_count
                changed_cells += cell_count
            except (AiProviderError, ValueError) as exc:
                warnings.append(str(exc))
            if progress:
                progress(index, max(1, len(batches)), f"ИИ: обработан блок {index}/{len(batches)}")

        if not batches:
            status = "skipped"
        elif warnings and changed_rows:
            status = "partial"
        elif warnings:
            status = "failed"
        else:
            status = "completed"

        result["ai"] = {
            "enabled": True,
            "provider": provider.key,
            "provider_label": provider.label,
            "model": provider.model,
            "status": status,
            "batches": len(batches),
            "changed_rows": changed_rows,
            "changed_cells": changed_cells,
            "warnings": warnings[:10],
        }
        return result

    def _request_batch(self, provider: Any, rows: list[dict]) -> AiCorrectionResponse:
        payload = []
        for row in rows:
            values = {key: str(row.get(key, "")) for key in EDITABLE_FIELDS}
            confidences = {
                key: float(row.get("confidences", {}).get(key, 0) or 0)
                for key in EDITABLE_FIELDS
                if values[key]
            }
            payload.append(
                {
                    "id": row.get("id"),
                    "row_type": row.get("row_type"),
                    "section": row.get("section", ""),
                    "system": row.get("system", ""),
                    "values": values,
                    "ocr_confidence": confidences,
                }
            )

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "Проверь строки спецификации:\n" + json.dumps(payload, ensure_ascii=False),
            },
        ]
        raw = provider.complete(messages)
        parsed = _extract_json(raw)
        return AiCorrectionResponse.model_validate(parsed)

    @staticmethod
    def _candidate(row: dict) -> bool:
        if row.get("status") in HUMAN_LOCKED_STATUSES:
            return False
        if row.get("row_type") == "skip":
            return False
        return any(str(row.get(key, "")).strip() for key in EDITABLE_FIELDS)

    @staticmethod
    def _apply_response(
        batch: list[dict], response: AiCorrectionResponse, provider: Any
    ) -> tuple[int, int]:
        by_id = {str(row.get("id")): row for row in batch}
        changed_rows = 0
        changed_cells = 0
        for correction in response.rows:
            row = by_id.get(correction.id)
            if not row:
                continue
            original_changes: dict[str, str] = {}
            applied_changes: dict[str, str] = {}
            for key, proposed in correction.values.items():
                if key not in EDITABLE_FIELDS:
                    continue
                old = str(row.get(key, ""))
                new = str(proposed or "").strip()
                # Blank source fields must never be hallucinated by the model.
                if not old.strip() or not new or _same_text(old, new):
                    continue
                original_changes[key] = old
                applied_changes[key] = new
                row[key] = new
            if not applied_changes:
                continue

            row.setdefault("ai_original", {}).update(original_changes)
            row.setdefault("ai_changes", {}).update(applied_changes)
            row["ai_provider"] = provider.key
            row["ai_model"] = provider.model
            row["ai_confidence"] = round(float(correction.confidence) * 100, 1)
            row["ai_reason"] = correction.reason[:500]
            # Every AI-modified engineering value remains visible for human review.
            row["status"] = "review"
            changed_rows += 1
            changed_cells += len(applied_changes)
        return changed_rows, changed_cells


def _chunks(items: list[dict], size: int) -> Iterable[list[dict]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _extract_json(value: str) -> dict:
    text = value.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("ИИ вернул ответ без JSON")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError("ИИ вернул некорректный JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("ИИ вернул неожиданный формат данных")
    return data


def _same_text(left: str, right: str) -> bool:
    return left.strip() == right.strip()
