"""Offline conversion of a saved Qwen Vision transcription to an AI-first draft."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import fitz

from averon_import.services.ai_first.assembly import (
    AIInputError, assemble_document_draft, page_from_payload,
)


def _expected_pages_from_pdf(source_pdf: Path) -> list[int]:
    try:
        with fitz.open(source_pdf.expanduser()) as document:
            page_count = document.page_count
    except Exception as exc:
        raise AIInputError("source_pdf_unavailable_or_invalid") from exc
    if type(page_count) is not int or page_count < 1:
        raise AIInputError("source_pdf_has_no_pages")
    return list(range(1, page_count + 1))


def _load_draft(source: Path, document_id: str, expected_pages: list[int]):
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AIInputError("transcription_unavailable_or_invalid") from exc
    if not isinstance(data, dict) or not isinstance(data.get("pages"), list):
        raise AIInputError("unsupported_transcription_envelope")
    pages = []
    try:
        for record in data["pages"]:
            if not isinstance(record, dict):
                raise AIInputError("invalid_transcription_structure")
            page_number = record.get("page")
            payload = record.get("transcription")
            if type(page_number) is not int or not isinstance(payload, dict):
                raise AIInputError("invalid_transcription_structure")
            if payload.get("page", page_number) != page_number:
                raise AIInputError("page_number_mismatch")
            pages.append(page_from_payload(
                payload, source_document_id=document_id, page_number=page_number,
            ))
    except (TypeError, ValueError, KeyError) as exc:
        if isinstance(exc, AIInputError):
            raise
        raise AIInputError("transcription_schema_invalid") from exc
    envelope = {key: value for key, value in data.items() if key != "pages"}
    return assemble_document_draft(
        document_id, pages, requested_pages=expected_pages, source_envelope=envelope,
    )


def _summary(draft: Any) -> dict[str, Any]:
    model_kinds: dict[str, int] = {}
    table_count = row_count = cell_count = 0
    for page in draft.pages:
        for table in page.tables:
            table_count += 1
            for row in table.rows:
                row_count += 1
                cell_count += len(row.raw_cells)
                kind = row.model_kind or "unknown"
                model_kinds[kind] = model_kinds.get(kind, 0) + 1
    return {
        "status": "offline_draft_created",
        "page_count": len(draft.pages),
        "expected_page_count": len(draft.expected_page_numbers),
        "page_status_counts": {
            status: sum(page.status == status for page in draft.pages)
            for status in ("extracted", "no_specification", "incomplete", "failed")
            if any(page.status == status for page in draft.pages)
        },
        "table_count": table_count,
        "row_observation_count": row_count,
        "cell_observation_count": cell_count,
        "model_kind_counts": model_kinds,
        "all_rows_unverified": all(
            row.role_verified is False
            for page in draft.pages for table in page.tables for row in table.rows
        ),
        "issue_count": len(draft.issues),
        "export_authorized": draft.export_authorized,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert a saved transcription to a private AI-first draft; performs no API calls."
    )
    parser.add_argument("--input", required=True, type=Path, help="Saved transcription JSON")
    parser.add_argument("--output", required=True, type=Path, help="New protected local JSON output path")
    parser.add_argument(
        "--source-pdf", required=True, type=Path,
        help="Original PDF used to derive the authoritative page manifest",
    )
    parser.add_argument("--document-id", default="offline-replay", help="Opaque local source identifier")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.output.exists():
            raise AIInputError("output_already_exists")
        expected_pages = _expected_pages_from_pdf(args.source_pdf)
        draft = _load_draft(args.input, args.document_id, expected_pages)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents an accidental overwrite; the caller should
        # choose a directory whose ACL is appropriate for the source document.
        with args.output.open("x", encoding="utf-8", newline="\n") as target:
            json.dump(draft.model_dump(mode="json"), target, ensure_ascii=False, indent=2)
            target.write("\n")
        print(json.dumps(_summary(draft), ensure_ascii=False, sort_keys=True))
        return 0
    except AIInputError as exc:
        print(json.dumps({"status": "failed", "error_code": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    except OSError:
        print(json.dumps({"status": "failed", "error_code": "local_io_failed"}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
