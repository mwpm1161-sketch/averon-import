"""Read-only inventory of AI-first workspaces for an authorized operator.

Only presence flags and aggregate review counts are emitted. No document text,
absolute paths, reviewer IDs, ownership IDs, or secrets are printed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from averon_import.services.ai_first.private_acl import PrivateAclError, verify_private_path, verify_private_tree


_DOCUMENT_ID = re.compile(r"^[0-9a-f]{32}$")
_CHECKPOINT = re.compile(r"^page-([1-9][0-9]*)\.json$")
_MAX_JSON_BYTES = 64 * 1024 * 1024


def _read_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_JSON_BYTES:
        raise ValueError("unavailable")
    return json.loads(path.read_text(encoding="utf-8"))


def _file_present(root: Path, name: str) -> bool:
    path = root / name
    return path.is_file() and not path.is_symlink()


def inventory(data_dir: Path) -> list[dict[str, Any]]:
    root = data_dir / "documents"
    if root.is_symlink() or not root.is_dir():
        raise ValueError("documents_root_unavailable")
    root_resolved = root.resolve()
    result: list[dict[str, Any]] = []
    for document_root in sorted(root.iterdir(), key=lambda item: item.name):
        document_id = document_root.name
        if not _DOCUMENT_ID.fullmatch(document_id) or document_root.is_symlink() or not document_root.is_dir():
            continue
        if document_root.resolve().parent != root_resolved:
            continue
        ai_root = document_root / "ai_first"
        if ai_root.is_symlink() or not ai_root.is_dir():
            continue
        record: dict[str, Any] = {
            "document_id": document_id,
            "security_state": "unverified",
            "transcription_present": _file_present(ai_root, "transcription.json"),
            "report_present": _file_present(ai_root, "report.json"),
            "checkpoint_pages": None,
            "review_history_events": None,
            "active_value_decisions": None,
            "human_confirmed_value_decisions": None,
            "active_role_decisions": None,
            "confirmed_complete_pages": None,
            "uploader_identity_present": None,
            "access_file_present": _file_present(ai_root, "access.json"),
            "ordinary_access_basis": "unknown",
        }
        try:
            verify_private_tree(ai_root)
            record["security_state"] = "verified_for_current_process"
        except (OSError, PrivateAclError):
            result.append(record)
            continue

        # A private metadata file is required before reading the uploader field.
        metadata_path = document_root / "metadata.json"
        try:
            verify_private_path(metadata_path, directory=False)
            metadata = _read_json(metadata_path)
            record["uploader_identity_present"] = bool(
                isinstance(metadata, dict) and metadata.get("uploaded_by_user_id")
            )
        except (OSError, ValueError, PrivateAclError):
            record["uploader_identity_present"] = False

        review: Any = None
        access: Any = None
        try:
            review = _read_json(ai_root / "review.json") if _file_present(ai_root, "review.json") else {}
            access = _read_json(ai_root / "access.json") if record["access_file_present"] else None
        except (OSError, ValueError):
            record["security_state"] = "verified_but_state_unreadable"
            result.append(record)
            continue

        try:
            checkpoint_dirs = [ai_root / "checkpoint"]
            runs = ai_root / "runs"
            if runs.is_symlink():
                raise ValueError("unavailable")
            if runs.is_dir():
                verify_private_tree(runs)
                checkpoint_dirs.extend(
                    run_root / "checkpoint" for run_root in runs.iterdir()
                    if re.fullmatch(r"run-[0-9a-f]{32}", run_root.name)
                    and run_root.is_dir() and not run_root.is_symlink()
                )
            count = 0
            for checkpoint in checkpoint_dirs:
                if checkpoint.is_symlink():
                    raise ValueError("unavailable")
                if checkpoint.is_dir():
                    verify_private_tree(checkpoint)
                    count += sum(
                        bool(_CHECKPOINT.fullmatch(path.name)) and path.is_file() and not path.is_symlink()
                        for path in checkpoint.iterdir()
                    )
            record["checkpoint_pages"] = count
        except (OSError, ValueError, PrivateAclError):
            record["checkpoint_pages"] = None

        if not isinstance(review, dict):
            review = {}
        decisions = review.get("decisions") if isinstance(review.get("decisions"), dict) else {}
        roles = review.get("row_roles") if isinstance(review.get("row_roles"), dict) else {}
        history = review.get("history") if isinstance(review.get("history"), list) else []
        record["review_history_events"] = len(history)
        record["active_value_decisions"] = sum(
            isinstance(item, dict) and bool(item.get("active")) for item in decisions.values()
        )
        record["human_confirmed_value_decisions"] = sum(
            isinstance(item, dict) and bool(item.get("active")) and bool(item.get("human_confirmed"))
            for item in decisions.values()
        )
        record["active_role_decisions"] = sum(
            isinstance(item, dict) and bool(item.get("active")) for item in roles.values()
        )
        pages = review.get("confirmed_complete_pages")
        record["confirmed_complete_pages"] = len(pages) if isinstance(pages, (list, set)) else 0

        reviewers = {
            item.get("reviewer_id") for item in history
            if isinstance(item, dict) and isinstance(item.get("reviewer_id"), str) and item.get("reviewer_id")
        }
        if isinstance(access, dict) and access.get("owner_id") and isinstance(access.get("legacy_reviewer_ids"), list):
            record["ordinary_access_basis"] = "access_file_owner_and_legacy_reviewers"
        elif reviewers:
            record["ordinary_access_basis"] = "server_authored_review_history"
        else:
            record["ordinary_access_basis"] = "administrator_recovery_required"
        result.append(record)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path,
                        help="Explicit authorized Averon data root; inventory never writes to it.")
    args = parser.parse_args(argv)
    try:
        records = inventory(args.data_dir)
    except (OSError, ValueError, PrivateAclError):
        print(json.dumps({"status": "inventory_unavailable"}, ensure_ascii=False))
        return 2
    print(json.dumps({"status": "ok", "workspace_count": len(records), "workspaces": records}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
