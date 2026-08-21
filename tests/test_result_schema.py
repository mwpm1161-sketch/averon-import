import pytest

from averon_import.core.result_schema import (
    CURRENT_RESULT_SCHEMA_VERSION,
    UnsupportedResultSchemaError,
    migrate_result,
)


def test_legacy_result_gets_schema_version():
    legacy = {"pages": [1], "rows": []}
    migrated = migrate_result(legacy)
    assert migrated["schema_version"] == CURRENT_RESULT_SCHEMA_VERSION
    assert "schema_version" not in legacy


def test_newer_result_schema_is_rejected():
    with pytest.raises(UnsupportedResultSchemaError):
        migrate_result({"schema_version": CURRENT_RESULT_SCHEMA_VERSION + 1})
