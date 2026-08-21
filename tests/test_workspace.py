from averon_import.core.result_schema import CURRENT_RESULT_SCHEMA_VERSION
from averon_import.services.workspace import WorkspaceService


def test_workspace_reads_legacy_result_through_migration(tmp_path):
    service = WorkspaceService(tmp_path / "data")
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"not-a-real-pdf")
    workspace = service.create(source, {"filename": "sample.pdf"})
    service.write_json(workspace.result_path, {"pages": [1], "rows": []})

    result = service.read_result(workspace)
    assert result["schema_version"] == CURRENT_RESULT_SCHEMA_VERSION
