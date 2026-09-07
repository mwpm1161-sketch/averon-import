from __future__ import annotations

import copy
from pathlib import Path

import pytest

from averon_import.services.ocr import OcrResult, OcrRow, PageOcrResult
from averon_import.services.pdf_service import PdfService
from averon_import.services.recognition import RecognitionService
from averon_import.services.row_assembler import SpecificationRowAssembler


def mkrow(source_row, values, confidences=None, sources=None, bbox=None, metadata=None):
    return OcrRow(
        source_row=source_row,
        values={k: str(v) for k, v in values.items()},
        confidences=confidences or {k: 90.0 for k in values},
        sources=sources or {},
        bbox=bbox or {"x": 0.1, "y": 0.25, "width": 0.8, "height": 0.25},
        metadata=metadata or {},
    )


def strip_ids(rows):
    cleaned = []
    for row in rows:
        row = copy.deepcopy(row)
        row.pop("id")
        cleaned.append(row)
    return cleaned


def test_item_row_basic_fields_and_confidence():
    assembler = SpecificationRowAssembler()
    row = assembler.build_page(7, [mkrow(
        4,
        {"position": "12", "name": "Вентиль", "quantity": "5"},
        confidences={"position": 90.0, "name": 80.0, "quantity": 70.0},
        sources={"position": "page-mixed"},
        bbox={"x": 0.05, "y": 0.3, "width": 0.9, "height": 0.2},
    )])[0]
    assert row["row_type"] == "item"
    assert row["status"] == "recognized"
    assert row["confidence"] == 80.0
    assert row["section"] == "" and row["system"] == ""
    assert row["page"] == 7 and row["edited"] is False
    assert row["source_row"] == 4
    assert row["bbox"] == {"x": 0.05, "y": 0.3, "width": 0.9, "height": 0.2}
    assert row["ocr_sources"] == {"position": "page-mixed"}
    assert row["position"] == "12" and row["name"] == "Вентиль"


def test_product_evidence_wins_over_section_keyword_when_quantity_missing():
    assembler = SpecificationRowAssembler()
    rows = assembler.build_page(47, [mkrow(
        6,
        {
            "name": "Решетка вентиляционная внутренняя",
            "type_mark": "PP300×300",
            "manufacturer": "ЭРА",
            "unit": "шт.",
        },
        metadata={"provider": "yandex_vision"},
    )])
    assert rows[0]["row_type"] == "item"
    assert rows[0]["status"] == "review"
    assert "quantity" in rows[0]["critical_fields"]


def test_continuation_row_merge():
    assembler = SpecificationRowAssembler()
    rows = assembler.build_page(1, [
        mkrow(2, {"name": "Воздуховод круглого сечения,"}, confidences={"name": 88.0},
              bbox={"x": 0.1, "y": 0.25, "width": 0.8, "height": 0.25}),
        mkrow(3, {"name": "из оцинкованной стали"}, confidences={"name": 70.0},
              bbox={"x": 0.1, "y": 0.4, "width": 0.8, "height": 0.2}),
    ])
    assert len(rows) == 1
    merged = rows[0]
    assert merged["name"] == "Воздуховод круглого сечения, из оцинкованной стали"
    assert merged["confidence"] == 79.0
    assert merged["bbox"]["height"] == pytest.approx(0.35)
    assert merged["source_row"] == "2+3"
    assert merged["row_type"] == "note"


def test_section_header_then_system_code():
    assembler = SpecificationRowAssembler()
    rows = assembler.build_page(1, [
        mkrow(0, {"name": "Отопление:"}),
        mkrow(1, {"name": "В2"}),
        mkrow(2, {"name": "Радиатор", "quantity": "10"}),
    ])
    assert rows[0]["row_type"] == "section"
    assert rows[0]["section"] == "Отопление"
    assert rows[1]["row_type"] == "system"
    assert rows[1]["system"] == "В2"
    assert rows[1]["status"] == "recognized"
    assert rows[2]["section"] == "Отопление" and rows[2]["system"] == "В2"


def test_system_like_text_without_valid_code_forced_review():
    assembler = SpecificationRowAssembler()
    rows = assembler.build_page(1, [
        mkrow(0, {"name": "Отопление:"}),
        mkrow(1, {"name": "АБС"}),
    ])
    assert rows[1]["row_type"] == "system"
    assert rows[1]["status"] == "review"


def test_fully_empty_row_still_emitted_as_skip():
    assembler = SpecificationRowAssembler()
    rows = assembler.build_page(1, [mkrow(5, {}, confidences={})])
    assert len(rows) == 1
    assert rows[0]["row_type"] == "skip"
    assert rows[0]["confidence"] == 0.0
    assert rows[0]["status"] == "unrecognized"


def test_component_block_after_kompleks():
    assembler = SpecificationRowAssembler()
    rows = assembler.build_page(1, [
        mkrow(1, {"name": "Комплект вентиляции", "unit": "компл.", "quantity": "1"}),
        mkrow(2, {"name": "Клапан обратный"}),
        mkrow(3, {"name": "Следующий прибор", "quantity": "3"}),
    ])
    assert rows[0]["row_type"] == "item"
    assert rows[1]["row_type"] == "component"
    assert rows[2]["row_type"] == "item"


def test_section_carries_across_pages_and_ids_unique():
    assembler = SpecificationRowAssembler()
    page1 = assembler.build_page(1, [mkrow(0, {"name": "Вентиляция:"})])
    page2 = assembler.build_page(2, [mkrow(0, {"name": "Клапан", "quantity": "2"})])
    assert page2[0]["section"] == "Вентиляция"
    assert page1[0]["id"] != page2[0]["id"]
    assert [r["page"] for r in (*page1, *page2)] == [1, 2]


@pytest.mark.parametrize("page_gap", [30, 50])
def test_non_adjacent_page_gap_resets_section_system_and_component_context(page_gap):
    assembler = SpecificationRowAssembler()
    assembler.build_page(18, [mkrow(0, {"name": "Вентиляция:"})])
    assembler.build_page(19, [mkrow(1, {"name": "П1"})])

    rows = assembler.build_page(page_gap, [mkrow(
        2, {"name": "Клапан обратный", "quantity": "2"}
    )])

    assert rows[0]["section"] == ""
    assert rows[0]["system"] == ""
    assert "context_missing" in rows[0]["review_reasons"]


def test_adjacent_page_context_carry_is_allowed():
    assembler = SpecificationRowAssembler()
    assembler.build_page(18, [mkrow(0, {"name": "Вентиляция:"})])
    assembler.build_page(19, [mkrow(1, {"name": "П1"})])

    row = assembler.build_page(20, [mkrow(
        2, {"name": "Клапан обратный", "quantity": "2"}
    )])[0]

    assert row["section"] == "Вентиляция"
    assert row["system"] == "П1"
    assert "context_missing" not in row["review_reasons"]


def test_new_section_resets_previous_system_and_component_block():
    assembler = SpecificationRowAssembler()
    assembler.build_page(1, [
        mkrow(0, {"name": "Вентиляция:"}),
        mkrow(1, {"name": "П1"}),
        mkrow(2, {"name": "Комплект вентиляции", "unit": "компл.", "quantity": "1"}),
    ])

    rows = assembler.build_page(2, [
        mkrow(0, {"name": "Отопление:"}),
        mkrow(1, {"name": "Радиатор", "quantity": "2"}),
    ])

    assert rows[0]["section"] == "Отопление"
    assert rows[0]["system"] == ""
    assert rows[1]["section"] == "Отопление"
    assert rows[1]["system"] == ""
    assert rows[1]["row_type"] == "item"


def test_malformed_raw_row_raises_and_service_keeps_prior_rows():
    class BadRow(OcrRow):
        def as_dict(self):
            raise KeyError("values")

    assembler = SpecificationRowAssembler()
    with pytest.raises(KeyError):
        assembler.build_page(2, [BadRow(0, {}, {}, {}, {})])

    class PartiallyBrokenProvider:
        key = "broken"

        def recognize(self, pdf_path, pages, **kwargs):
            return OcrResult(provider=self.key, pages=[
                PageOcrResult(page=1, rows=[mkrow(0, {"name": "Насос", "quantity": "1"})]),
                PageOcrResult(page=2, rows=[BadRow(0, {}, {}, {}, {})]),
            ])

    service = RecognitionService(PdfService(), ocr=PartiallyBrokenProvider())
    result = service.recognize(
        Path("d.pdf"), Path("p"), [1, 2], None, 300, lambda c, t, m: None,
    )
    assert len(result["rows"]) == 1 and result["rows"][0]["page"] == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0]["page"] == 2
    assert result["summary"]["page_errors"] == 1


def test_unknown_value_keys_are_preserved_verbatim():
    assembler = SpecificationRowAssembler()
    row = assembler.build_page(1, [mkrow(0, {"name": "Труба", "custom_extra": "X"})])[0]
    assert row["custom_extra"] == "X"


def test_assembler_does_not_normalize_text_again():
    assembler = SpecificationRowAssembler()
    row = assembler.build_page(1, [mkrow(0, {
        "name": "Воздуховод 100 x 200",
        "unit": "м2",
    })])[0]
    assert row["name"] == "Воздуховод 100 x 200"
    assert row["unit"] == "м2"


class FixedProvider:
    key = "fixed"

    def __init__(self, rows_by_page):
        self.rows_by_page = rows_by_page

    def recognize(self, pdf_path, pages, **kwargs):
        return OcrResult(provider=self.key, pages=[
            PageOcrResult(page=p, rows=self.rows_by_page.get(p, []))
            for p in pages
        ])


RAW_PAGES = {
    1: [
        mkrow(0, {"name": "Отопление:"}),
        mkrow(1, {"name": "П1"}),
        mkrow(2, {"name": "Радиатор отопительный,", "quantity": "12"}, confidences={"name": 84.0, "quantity": 76.0}),
        mkrow(3, {"name": "с терморегулятором"}, confidences={"name": 66.0}),
    ],
    2: [
        mkrow(0, {"position": "1", "name": "Кран шаровый", "quantity": "4", "mass": "2,5"}),
    ],
}


def test_service_path_and_direct_assembler_produce_identical_rows():
    service = RecognitionService(PdfService(), ocr=FixedProvider(RAW_PAGES))
    via_service = service.recognize(
        Path("doc.pdf"), Path("pages"), [1, 2], None, 300, lambda c, t, m: None,
    )
    direct_assembler = SpecificationRowAssembler()
    via_direct = []
    for page in (1, 2):
        via_direct.extend(direct_assembler.build_page(page, RAW_PAGES[page]))
    assert strip_ids(via_service["rows"]) == strip_ids(via_direct)
    assert via_service["summary"]["total_rows"] == len(via_direct)
    assert via_service["rows"][2]["confidence"] == 75.5
    assert via_service["rows"][2]["row_type"] == "item"


def test_recognition_normalizes_selected_pages_before_provider_call():
    class RecordingProvider(FixedProvider):
        def recognize(self, pdf_path, pages, **kwargs):
            self.received_pages = list(pages)
            return super().recognize(pdf_path, pages, **kwargs)

    provider = RecordingProvider({1: [], 2: []})
    result = RecognitionService(PdfService(), ocr=provider).recognize(
        Path("doc.pdf"), Path("pages"), [2, 1, 2], None, 300,
        lambda c, t, m: None,
    )

    assert provider.received_pages == [1, 2]
    assert result["pages"] == [1, 2]
