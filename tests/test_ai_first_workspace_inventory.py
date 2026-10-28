import json

from averon_import.tools import ai_first_workspace_inventory as inventory_tool


def _workspace(root, document_id, *, history=None, access=None):
    document = root / "documents" / document_id
    ai = document / "ai_first"
    checkpoint = ai / "checkpoint"
    checkpoint.mkdir(parents=True)
    (document / "metadata.json").write_text(json.dumps({"uploaded_by_user_id": "private-uploader"}), encoding="utf-8")
    review = {
        "decisions": {
            "r1": {"active": True, "human_confirmed": True, "values": {"cell:1": "PRIVATE-DOCUMENT-TEXT"}},
            "r2": {"active": True, "human_confirmed": False},
        },
        "row_roles": {"r1": {"active": True, "role": "standalone_item"}},
        "history": history or [],
        "confirmed_complete_pages": [1],
    }
    (ai / "review.json").write_text(json.dumps(review), encoding="utf-8")
    (ai / "transcription.json").write_text("{}", encoding="utf-8")
    (ai / "report.json").write_text("{}", encoding="utf-8")
    (checkpoint / "page-1.json").write_text("{}", encoding="utf-8")
    if access is not None:
        (ai / "access.json").write_text(json.dumps(access), encoding="utf-8")


def test_inventory_is_read_only_redacted_and_uses_existing_auth_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(inventory_tool, "verify_private_tree", lambda _path: 1)
    monkeypatch.setattr(inventory_tool, "verify_private_path", lambda _path, directory=False: 1)
    _workspace(tmp_path, "a" * 32, history=[{"reviewer_id": "private-reviewer"}])
    _workspace(tmp_path, "b" * 32)
    _workspace(tmp_path, "c" * 32, access={"version": 1, "owner_id": "private-owner", "legacy_reviewer_ids": []})
    before = {str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")}

    records = inventory_tool.inventory(tmp_path)
    after = {str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")}
    output = json.dumps(records, ensure_ascii=False)

    assert before == after
    assert len(records) == 3
    assert records[0]["ordinary_access_basis"] == "server_authored_review_history"
    assert records[1]["ordinary_access_basis"] == "administrator_recovery_required"
    assert records[2]["ordinary_access_basis"] == "access_file_owner_and_legacy_reviewers"
    assert records[0]["human_confirmed_value_decisions"] == 1
    assert records[0]["active_role_decisions"] == 1
    assert records[0]["confirmed_complete_pages"] == 1
    assert records[0]["checkpoint_pages"] == 1
    assert "PRIVATE-DOCUMENT-TEXT" not in output
    assert "private-reviewer" not in output and "private-owner" not in output and "private-uploader" not in output


def test_inventory_fails_closed_for_unverified_acl_without_reading_state(tmp_path, monkeypatch):
    document_id = "d" * 32
    _workspace(tmp_path, document_id)
    monkeypatch.setattr(inventory_tool, "verify_private_tree", lambda _path: (_ for _ in ()).throw(inventory_tool.PrivateAclError("unsafe")))
    monkeypatch.setattr(inventory_tool, "verify_private_path", lambda _path, directory=False: 1)

    result = inventory_tool.inventory(tmp_path)

    assert result[0]["security_state"] == "unverified"
    assert result[0]["review_history_events"] is None
    assert result[0]["ordinary_access_basis"] == "unknown"
