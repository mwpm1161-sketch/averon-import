from __future__ import annotations

import fitz

from averon_import.services.ai_first.contracts import (
    AICell, AIColumn, AIDocumentDraft, AIPageExtraction, AIRow, AITable,
)
from averon_import.services.ai_first.pdf_locator import locate_ai_first_pdf_bbox


def _draft():
    table_id = "synthetic-table"
    values = ["1", "Synthetic pump", "MP-1", "Example Works", "pc", "2"]
    row_id = "synthetic-row"
    columns = [AIColumn(
        id=f"column-{index}", source_document_id="synthetic-doc", page_number=1,
        table_id=table_id, index=index, raw_header=f"Field {index + 1}",
    ) for index in range(len(values))]
    row = AIRow(
        id=row_id, source_document_id="synthetic-doc", page_number=1,
        table_id=table_id, index=0, model_kind="item",
        raw_cells=[AICell(
            id=f"cell-{index}", source_document_id="synthetic-doc", page_number=1,
            table_id=table_id, row_id=row_id, column_index=index, raw_value=value,
        ) for index, value in enumerate(values)],
        # Candidate coordinates are intentionally wrong. A locator must not trust them.
        bbox_candidate={"x":0.9,"y":0.9,"width":0.05,"height":0.05},
    )
    table = AITable(
        id=table_id, source_document_id="synthetic-doc", page_number=1,
        index=0, columns=columns, rows=[row],
    )
    page = AIPageExtraction(
        source_document_id="synthetic-doc", page_number=1,
        extraction_fingerprint="synthetic-fingerprint", status="extracted", tables=[table],
    )
    return AIDocumentDraft(
        source_document_id="synthetic-doc", revision_id="synthetic-revision",
        pages=[page], expected_page_numbers=[1],
    ), row_id


def _pdf(path, lines):
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    for y, text in lines:
        page.insert_text((72, y), text, fontsize=11)
    doc.save(path)
    doc.close()


def test_locator_uses_unique_exact_text_not_model_bbox(tmp_path):
    source = tmp_path / "synthetic.pdf"
    _pdf(source, [(100, "1 Synthetic pump MP-1 Example Works pc 2")])
    draft, row_id = _draft()

    located = locate_ai_first_pdf_bbox(source, draft, row_id)
    assert located["matched"] is True
    assert located["basis"] == "unique_exact_pdf_text_layer"
    assert located["bbox"]["x"] < 0.5 and located["bbox"]["y"] < 0.5
    assert located["bbox"]["x"] + located["bbox"]["width"] <= 1.000001

    cell = locate_ai_first_pdf_bbox(source, draft, row_id, cell_index=1)
    assert cell["matched"] is True
    assert cell["bbox"]["width"] < located["bbox"]["width"]


def test_locator_returns_no_box_for_repeated_text_or_short_ambiguous_cell(tmp_path):
    source = tmp_path / "duplicate.pdf"
    text = "1 Synthetic pump MP-1 Example Works pc 2"
    _pdf(source, [(100, text), (160, text)])
    draft, row_id = _draft()

    assert locate_ai_first_pdf_bbox(source, draft, row_id)["matched"] is False
    number = locate_ai_first_pdf_bbox(source, draft, row_id, cell_index=0)
    assert number == {"matched":False,"reason":"text_match_missing_or_ambiguous"} or number["matched"] is False


def test_image_only_pdf_fails_closed_without_highlight(tmp_path):
    source = tmp_path / "image-only.pdf"
    doc = fitz.open()
    doc.new_page(width=612, height=792)
    doc.save(source)
    doc.close()
    draft, row_id = _draft()

    result = locate_ai_first_pdf_bbox(source, draft, row_id)
    assert result == {"matched":False,"reason":"text_layer_unavailable"}
