from __future__ import annotations

from copy import deepcopy
from typing import Any

CURRENT_RESULT_SCHEMA_VERSION = 1


class UnsupportedResultSchemaError(ValueError):
    pass


def migrate_result(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Upgrade a persisted result to the current schema.

    rc7 result files did not contain ``schema_version``. They are structurally
    equivalent to schema v1, so missing versions are treated as v1. Future
    migrations can be added here without scattering compatibility checks across
    API routes and services.
    """
    if data is None:
        return None
    if not data:
        return data

    result = deepcopy(data)
    raw_version = result.get("schema_version", 1)
    try:
        version = int(raw_version)
    except (TypeError, ValueError) as exc:
        raise UnsupportedResultSchemaError(
            f"Некорректная версия формата результата: {raw_version!r}"
        ) from exc

    if version > CURRENT_RESULT_SCHEMA_VERSION:
        raise UnsupportedResultSchemaError(
            "Результат создан более новой версией Averon Import "
            f"(schema {version}, поддерживается {CURRENT_RESULT_SCHEMA_VERSION})"
        )
    if version < 1:
        raise UnsupportedResultSchemaError(f"Неизвестная версия формата результата: {version}")

    result["schema_version"] = CURRENT_RESULT_SCHEMA_VERSION
    return result
