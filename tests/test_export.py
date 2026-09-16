from openpyxl import load_workbook

from averon_import.services.export_service import ExcelExportService


def test_export_selected_columns(tmp_path):
    rows = [
        {
            "name": "Воздуховод 500×500",
            "type_mark": "ГОСТ 14918-2020",
            "unit": "м",
            "quantity": "12,5",
            "row_type": "item",
            "status": "verified",
            "selected": True,
        },
        {
            "name": "Раздел",
            "row_type": "section",
            "status": "recognized",
            "selected": True,
        },
    ]
    target = tmp_path / "result.xlsx"
    ExcelExportService().export(rows, ["name", "unit", "quantity"], target)
    workbook = load_workbook(target)
    sheet = workbook["Спецификация"]
    assert sheet.max_row == 2
    assert sheet["A2"].value == "Воздуховод 500×500"
    assert sheet["C2"].value == 12.5
    assert workbook["Сведения"]["B5"].value == "Андриянов Степан Владимирович - НВСС"


def test_export_keeps_structured_text_rows(tmp_path):
    target = tmp_path / "structured-note.xlsx"
    ExcelExportService().export(
        [{
            "name": "с электромеханическим приводом 220В",
            "row_type": "note",
            "structured_table": True,
            "status": "review",
            "selected": True,
        }],
        ["name", "quantity"],
        target,
    )

    workbook = load_workbook(target)
    sheet = workbook["Спецификация"]
    assert sheet.max_row == 2
    assert sheet["A2"].value == "с электромеханическим приводом 220В"


def _status(page: int, output: str, blockers=None) -> dict:
    return {
        "page": page,
        "output_status": output,
        "page_disposition": "REVIEW_REQUIRED" if output != "USABLE" else "SPEC",
        "blockers": list(blockers or []),
    }


def test_review_export_creates_inspection_sheets_with_diagnostics(tmp_path):
    row = {
        "page": 58,
        "name": "Насос",
        "row_type": "item",
        "status": "review",
        "confidence": 61.0,
        "selected": False,
        "review_reasons": ["critical_value_missing"],
        "unit": "",
        "quantity": "",
        "mass": "",
    }
    target = tmp_path / "averon_import_review.xlsx"

    ExcelExportService().export(
        [row],
        ["name"],
        target,
        page_statuses={"58": _status(58, "REVIEW_REQUIRED", ["critical_value_missing"])},
        review_export=True,
    )

    workbook = load_workbook(target)
    assert {"Проверка", "Страницы"}.issubset(workbook.sheetnames)
    sheet = workbook["Спецификация"]
    assert [cell.value for cell in sheet[1]] == [
        "Наименование и техническая характеристика",
        "Страница PDF",
        "Тип строки",
        "Статус",
        "Уверенность, %",
    ]
    assert sheet["A2"].value == "Насос"
    assert sheet["B2"].value == 58

    review_sheet = workbook["Проверка"]
    assert [cell.value for cell in review_sheet[1]] == [
        "Страница",
        "Тип строки",
        "Наименование",
        "Статус",
        "Причины проверки",
        "Критичные блокеры",
    ]
    assert review_sheet["C2"].value == "Насос"
    assert review_sheet["E2"].value == "critical_value_missing"
    assert review_sheet["F2"].value == "critical_value_missing"

    page_sheet = workbook["Страницы"]
    assert [cell.value for cell in page_sheet[1]] == [
        "Страница",
        "Статус результата",
        "Решение страницы",
        "Блокеры",
    ]
    assert [cell.value for cell in page_sheet[2]] == [
        58,
        "REVIEW_REQUIRED",
        "REVIEW_REQUIRED",
        "critical_value_missing",
    ]


def test_review_export_keeps_unselected_rows_but_production_skips_them(tmp_path):
    row = {
        "page": 1,
        "name": "Неподтверждённая строка",
        "row_type": "item",
        "status": "recognized",
        "selected": False,
    }
    status = {"1": _status(1, "USABLE")}
    production = tmp_path / "production.xlsx"
    review = tmp_path / "review.xlsx"

    ExcelExportService().export([row], ["name"], production, page_statuses=status)
    ExcelExportService().export_for_review([row], ["name"], review, page_statuses=status)

    assert load_workbook(production)["Спецификация"].max_row == 1
    review_sheet = load_workbook(review)["Спецификация"]
    assert review_sheet.max_row == 2
    assert review_sheet["A2"].value == "Неподтверждённая строка"


def test_semantic_review_row_stays_out_of_production_but_enters_review_export(tmp_path):
    row = {
        "page": 2,
        "name": "Семантический кандидат",
        "row_type": "semantic_review",
        "status": "review",
        "semantic_state": "REVIEW",
        "selected": True,
    }
    status = {"2": _status(2, "USABLE")}
    production = tmp_path / "production.xlsx"
    review = tmp_path / "review.xlsx"

    ExcelExportService().export(
        [row], ["name"], production, page_statuses=status, only_exportable=True
    )
    ExcelExportService().export_for_review([row], ["name"], review, page_statuses=status)

    assert load_workbook(production)["Спецификация"].max_row == 1
    review_sheet = load_workbook(review)["Спецификация"]
    assert review_sheet.max_row == 2
    assert review_sheet["A2"].value == "Семантический кандидат"


def test_review_export_includes_critical_blocker_on_non_review_row(tmp_path):
    row = {
        "page": 3,
        "name": "Число для проверки",
        "row_type": "item",
        "status": "recognized",
        "selected": True,
        "critical_blockers": ["numeric_suspect"],
    }
    target = tmp_path / "review.xlsx"

    ExcelExportService().export_for_review(
        [row], ["name"], target, page_statuses={"3": _status(3, "USABLE")}
    )

    review_sheet = load_workbook(target)["Проверка"]
    assert review_sheet.max_row == 2
    assert review_sheet["D2"].value == "Распознано"
    assert review_sheet["F2"].value == "numeric_suspect"
