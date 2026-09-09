from __future__ import annotations

from averon_import.services.ocr.base import OcrRow
from averon_import.services.ocr.yandex_vision import YandexVisionProvider


def _provider() -> YandexVisionProvider:
    return YandexVisionProvider.__new__(YandexVisionProvider)


def _item(
    index: int,
    *,
    state: str = "VERIFIED",
    reasons: list[str] | None = None,
    values: dict[str, str] | None = None,
    metadata: dict | None = None,
) -> OcrRow:
    item_values = {
        "name": f"Насос {index}",
        "unit": "шт.",
        "quantity": "1",
        "mass": "2",
    }
    item_values.update(values or {})
    item_metadata = {
        "semantic_authoritative": True,
        "semantic_role": "ITEM_ROOT",
        "logical_item_id": f"item:{index}",
        "semantic_state": state,
        "semantic_review": state == "REVIEW",
        "review_reasons": list(reasons or []),
        "semantic_required_critical_fields": ["quantity", "unit"],
    }
    item_metadata.update(metadata or {})
    return OcrRow(index, item_values, {}, {}, {}, item_metadata)


def _finalize(rows: list[OcrRow]) -> dict:
    diagnostics: dict = {}
    _provider()._finalize_semantic_item_state_and_telemetry(rows, diagnostics)
    return diagnostics


def test_t1_final_partition_counts_three_logical_items():
    rows = [
        _item(1),
        _item(2),
        _item(
            3,
            state="REVIEW",
            reasons=["critical_value_missing"],
            values={"quantity": ""},
        ),
    ]

    diagnostics = _finalize(rows)

    assert diagnostics["logical_item_count"] == 3
    assert diagnostics["semantic_verified_item_count"] == 2
    assert diagnostics["semantic_review_item_count"] == 1
    assert diagnostics["semantic_auto_accept_rate"] == 2 / 3


def test_t2_post_projection_identity_loss_moves_item_to_review():
    row = _item(
        1,
        metadata={"identity_cell_missing": True},
        reasons=["identity_cell_missing"],
    )

    diagnostics = _finalize([row])

    assert row.metadata["semantic_state"] == "REVIEW"
    assert row.metadata["semantic_review"] is True
    assert diagnostics["semantic_verified_item_count"] == 0
    assert diagnostics["semantic_review_item_count"] == 1


def test_t3_post_projection_numeric_suspect_moves_item_to_review():
    row = _item(1, reasons=["numeric_suspect"])

    diagnostics = _finalize([row])

    assert row.metadata["semantic_state"] == "REVIEW"
    assert diagnostics["semantic_numeric_suspect_count"] == 1
    assert diagnostics["semantic_verified_item_count"] == 0
    assert diagnostics["semantic_review_item_count"] == 1


def test_t4_exact_cell_candidate_stays_candidate_only_and_item_is_review():
    row = _item(
        1,
        values={"quantity": ""},
        metadata={
            "value_candidates": {
                "quantity": {
                    "value_candidate": "1",
                    "auto_trusted": False,
                    "candidate_source": "yandex_exact_cell_2x",
                }
            }
        },
    )

    diagnostics = _finalize([row])

    assert row.values["quantity"] == ""
    assert row.metadata["value_candidates"]["quantity"]["auto_trusted"] is False
    assert row.metadata["semantic_state"] == "REVIEW"
    assert diagnostics["semantic_review_item_count"] == 1


def test_t5_informational_structural_reason_does_not_review_item():
    row = _item(
        1,
        state="REVIEW",
        reasons=["structural_ambiguity", "structural_disagreement"],
        metadata={"semantic_structural_impact": "INFORMATIONAL"},
    )

    diagnostics = _finalize([row])

    assert row.metadata["semantic_state"] == "VERIFIED"
    assert row.metadata["semantic_review"] is False
    assert diagnostics["semantic_verified_item_count"] == 1
    assert diagnostics["semantic_review_item_count"] == 0


def test_t6_review_evidence_is_separate_from_logical_item_partition():
    item = _item(1)
    evidence = OcrRow(
        2,
        {},
        {},
        {},
        {},
        {
            "semantic_authoritative": True,
            "semantic_review": True,
            "semantic_role": "REVIEW_EVIDENCE",
            "semantic_review_impact": "OUTPUT_CRITICAL",
            "review_reasons": ["physical_row_semantics_unresolved"],
        },
    )

    diagnostics = _finalize([item, evidence])

    assert diagnostics["logical_item_count"] == 1
    assert diagnostics["semantic_verified_item_count"] == 1
    assert diagnostics["semantic_review_item_count"] == 0
    assert diagnostics["semantic_review_evidence_row_count"] == 1
    assert diagnostics["semantic_non_output_review_row_count"] == 0


def test_t7_non_output_evidence_does_not_change_logical_partition():
    item = _item(1)
    evidence = OcrRow(
        2,
        {},
        {},
        {},
        {},
        {
            "semantic_authoritative": True,
            "semantic_review": True,
            "semantic_role": "REVIEW_EVIDENCE",
            "semantic_review_impact": "NON_OUTPUT",
            "review_reasons": ["physical_row_unresolved"],
        },
    )

    diagnostics = _finalize([item, evidence])

    assert diagnostics["logical_item_count"] == 1
    assert diagnostics["semantic_verified_item_count"] == 1
    assert diagnostics["semantic_review_item_count"] == 0
    assert diagnostics["semantic_review_evidence_row_count"] == 1
    assert diagnostics["semantic_non_output_review_row_count"] == 1


def test_t8_final_telemetry_partition_holds_for_each_authoritative_page():
    page_rows = [
        [_item(1), _item(2, reasons=["critical_value_missing"], values={"quantity": ""})],
        [_item(3), _item(4)],
        [_item(5, reasons=["numeric_suspect"]), _item(6)],
    ]

    for rows in page_rows:
        diagnostics = _finalize(rows)
        assert diagnostics["logical_item_count"] == (
            diagnostics["semantic_verified_item_count"]
            + diagnostics["semantic_review_item_count"]
        )
