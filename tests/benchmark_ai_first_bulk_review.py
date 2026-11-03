"""Synthetic, offline-only ledger benchmark; not collected as a pytest test.

Run with the same Python and --repeat count in each checkout. Setup, input
hashing and correctness assertions are outside the timed preview/commit calls.
Only aggregate timings and synthetic input/code hashes enter the output file.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
from statistics import median
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from averon_import.services.ai_first.bulk_review import BulkReviewRequest, execute_bulk_review
from averon_import.services.ai_first.contracts import AIReviewEvent, AIReviewState
from averon_import.services.ai_first.editor_draft import review_token
from averon_import.services.ai_first.local_workflow import AIWorkspaceStore
from averon_import.services.ai_first.review import new_review_state
from test_ai_first_foundation import _cell, _draft, _payload, _row


def fixture(row_count: int, history_count: int):
    draft = _draft(_payload(headers=[f"Synthetic column {n}" for n in range(9)], rows=[
        _row(index, str(index + 1), f"Synthetic item {index + 1}", cells=[
            _cell(column, f"R{index + 1} C{column + 1}") for column in range(9)
        ]) for index in range(row_count)
    ]))
    state = new_review_state(draft)
    state.resolved_issue_ids = {issue.id for issue in [*draft.issues, *(i for p in draft.pages for i in p.issues)]}
    page, table = draft.pages[0], draft.pages[0].tables[0]
    state.history = [AIReviewEvent(
        event_id=f"review-event:synthetic-{index}", occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        action="confirm_row", source_document_id=draft.source_document_id,
        reviewer_id="synthetic-reviewer", page_number=1, source_record_ids=[table.rows[0].id],
        payload={"disposition": "confirmed", "values": {"cell:1": "Historical synthetic value"}},
    ) for index in range(history_count)]
    request = BulkReviewRequest(
        mode="commit", operation_id="0" * 32, review_token=review_token(draft, state, None),
        draft_revision_id=draft.revision_id, editor_revision=0,
        record_ids=[row.id for row in table.rows], role="standalone_item", intent="confirm_values",
        provenance={row.id: {"page_number": 1, "extraction_fingerprint": page.extraction_fingerprint,
                             "table_id": table.id} for row in table.rows},
        values={row.id: {f"cell:{cell.column_index}": cell.raw_value or "" for cell in row.raw_cells}
                for row in table.rows},
    )
    return draft, state, request


def digest_input(draft, state):
    ledger = state.model_dump(mode="json")
    ledger["resolved_issue_ids"] = sorted(ledger["resolved_issue_ids"])
    ledger["confirmed_complete_pages"] = sorted(ledger["confirmed_complete_pages"])
    return hashlib.sha256(json.dumps([draft.model_dump(mode="json"), ledger],
                                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def timed_call(callback):
    original = AIReviewState.model_copy
    counts = {"deep_copies": 0, "copy_ms": 0.0}

    def measured_copy(self, *args, **kwargs):
        start = perf_counter()
        try:
            return original(self, *args, **kwargs)
        finally:
            if kwargs.get("deep"):
                counts["deep_copies"] += 1
                counts["copy_ms"] += (perf_counter() - start) * 1000

    with patch.object(AIReviewState, "model_copy", measured_copy):
        start = perf_counter()
        result = callback()
        elapsed = (perf_counter() - start) * 1000
    return result, {"elapsed_ms": elapsed, **counts}


def sample(row_count, history_count):
    draft, state, request = fixture(row_count, history_count)
    before = state.model_dump(mode="json")
    raw = draft.model_dump(mode="json")
    input_hash = digest_input(draft, state)
    with TemporaryDirectory(prefix="avbench-") as directory:
        store = AIWorkspaceStore(Path(directory) / "document")
        store.write_many({"transcription.json": raw, "review.json": before})
        preview, preview_time = timed_call(lambda: execute_bulk_review(
            store, draft, state, None, request.model_copy(update={"mode": "preview"}), "synthetic-reviewer"))
        assert preview["can_commit"] and store.read("review.json") == before
        committed, commit_time = timed_call(lambda: execute_bulk_review(
            store, draft, state, None, request, "synthetic-reviewer"))
        checked = AIReviewState.model_validate_json(json.dumps(store.read("review.json")))
        assert committed["saved"] and len(checked.decisions) == row_count
        assert len(checked.row_roles) == row_count and len(checked.history) == history_count + row_count * 2
        assert checked.model_dump(mode="json")["history"][:history_count] == before["history"]
        assert state.model_dump(mode="json") == before and draft.model_dump(mode="json") == raw
        replay = execute_bulk_review(store, draft, checked, None, request, "synthetic-reviewer")
        assert replay["replayed"] and store.read("review.json") == checked.model_dump(mode="json")
    return {"input_sha256": input_hash, "preview": preview_time, "commit": commit_time}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repeat", type=int, default=3, choices=range(1, 11))
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; preserve previous measurements.")
    code = hashlib.sha256(b"".join((ROOT / name).read_bytes() for name in [
        "averon_import/services/ai_first/review.py", "averon_import/services/ai_first/bulk_review.py",
    ])).hexdigest()
    report = {"python": platform.python_version(), "platform": platform.system(),
              "repeat": args.repeat, "code_sha256": code, "cases": []}
    for history_count in (0, 1000):
        for row_count in (25, 50, 100):
            runs = [sample(row_count, history_count) for _ in range(args.repeat)]
            assert len({run["input_sha256"] for run in runs}) == 1
            case = {"rows": row_count, "columns": 9, "history": history_count,
                    "runs": runs, "median": {phase: {
                        key: median(run[phase][key] for run in runs)
                        for key in ("elapsed_ms", "deep_copies", "copy_ms")
                    } for phase in ("preview", "commit")}}
            report["cases"].append(case)
            print(json.dumps({k: v for k, v in case.items() if k != "runs"}), flush=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
