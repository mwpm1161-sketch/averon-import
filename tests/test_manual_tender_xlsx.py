from __future__ import annotations

import hashlib
import json
import math
import threading
import asyncio
import os
import re
import subprocess
import shutil
import time
import uuid
import zipfile
from copy import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.styles import PatternFill, Side

from averon_import.services.manual_tenders import TenderActivityRegistry, TenderTemplateService, TenderWorkbookParser, TenderWorkspaceRepository
from averon_import.services.manual_tenders.sourcing import TenderSourcingRowAdapter, TenderSourcingRunStore, canonical_tender_projection
from averon_import.services.jobs import JobService
from averon_import.services.manual_tenders.parser import (
    MAX_ACTUAL_ITEMS,
    MAX_HEADER_LENGTH,
    MAX_UNCOMPRESSED_BYTES,
    MAX_UPLOAD_BYTES,
    TenderParseError,
    parse_unit_basis,
    preflight_tender_xlsx,
)
from averon_import.services.manual_tenders.repository import (
    ABSOLUTE_TTL_SECONDS,
    IDLE_TTL_SECONDS,
    MAX_TENDER_RUN_BYTES,
    MAX_WORKSPACES_GLOBAL,
    MAX_WORKSPACES_PER_USER,
    PREVIEW_TTL_SECONDS,
    TenderWorkspaceError,
)
from averon_import.services.manual_tenders.template import PREPARED_ROWS, TEMPLATE_HEADERS, TEMPLATE_SCHEMA_NAME, TEMPLATE_SCHEMA_VERSION, TEMPLATE_SHEET, TEMPLATE_TABLE


def _official(path: Path, *, include_required=True) -> bytes:
    content = TenderTemplateService().bytes()
    path.write_bytes(content)
    if include_required:
        workbook = load_workbook(path)
        sheet = workbook[TEMPLATE_SHEET]
        sheet["A2"] = "R-001"
        sheet["B2"] = "Синтетический ресурс"
        sheet["C2"] = "шт"
        sheet["D2"] = 2
        sheet["E2"] = "SKU-1"
        sheet["F2"] = "Тестовый производитель"
        sheet["G2"] = "Модель X"
        workbook.save(path)
    return path.read_bytes()


def _fallback(path: Path, *, count=370) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Ресурсная ведомость1 - СУВР(О)_"
    for coordinate, value in {
        "A12": "№ смет/Код ресурса", "C12": "Наименование", "D12": "Ед. изм.", "E12": "Кол-во",
        "AI12": "Цена за единицу", "AJ12": "Общая стоимость", "CF12": "Итого",
    }.items():
        sheet[coordinate] = value
    sheet["F12"] = "Служебное скрытое поле F"
    sheet["G12"] = "Служебное скрытое поле G"
    sheet.column_dimensions["F"].hidden = True
    sheet.column_dimensions["G"].hidden = True
    sheet.column_dimensions["AI"].width = 15
    sheet["AI12"].number_format = "0.00"
    sheet["AJ12"].number_format = "0.00"
    sheet["B5"] = "Сводная часть"
    sheet.merge_cells("B5:C5")
    sheet.print_area = "A1:E391"
    row_number = 13
    sheet.cell(row_number, 3, "Синтетический раздел А")
    row_number += 1
    scaled_units = ["10 шт", "100 шт", "1000 шт", "10 м", "1000 м"] * 3 + ["т", "10 шт"]
    item_index = 0
    for _ in range(count // 2):
        sheet.cell(row_number, 1, "REP-1")
        sheet.cell(row_number, 3, f"Синтетическая позиция {item_index + 1}")
        sheet.cell(row_number, 4, scaled_units[item_index] if item_index < len(scaled_units) else "шт")
        sheet.cell(row_number, 5, 1 + (item_index % 3))
        item_index += 1
        row_number += 1
    sheet.cell(row_number, 3, "Синтетический раздел Б")
    row_number += 1
    while item_index < count:
        sheet.cell(row_number, 1, "REP-1")
        sheet.cell(row_number, 3, f"Синтетическая позиция {item_index + 1}")
        sheet.cell(row_number, 4, scaled_units[item_index] if item_index < len(scaled_units) else "кг")
        sheet.cell(row_number, 5, 1 + (item_index % 3))
        item_index += 1
        row_number += 1
    for label in ("ИТОГО по разделу", "Всего по ведомости"):
        sheet.cell(row_number, 3, label)
        sheet.cell(row_number, 5, 740)
        row_number += 1
    for index in range(1, 7):
        sheet.cell(row_number + index, 1, f"FORMAL-CF-{index}")
    workbook.save(path)
    return path.read_bytes()


def test_official_template_contract_and_parser_fast_path(tmp_path):
    path = tmp_path / "official.xlsx"
    _official(path)
    workbook = load_workbook(path, data_only=False, keep_links=False)
    assert workbook.sheetnames == [TEMPLATE_SHEET, "Инструкция"]
    sheet = workbook[TEMPLATE_SHEET]
    assert tuple(sheet.cell(1, col).value for col in range(1, 8)) == TEMPLATE_HEADERS
    assert list(sheet.tables) == [TEMPLATE_TABLE]
    assert sheet.tables[TEMPLATE_TABLE].ref == f"A1:G{PREPARED_ROWS + 1}"
    assert workbook.defined_names[TEMPLATE_SCHEMA_NAME].attr_text == TEMPLATE_SCHEMA_VERSION
    assert sheet.freeze_panes == "A2"
    assert sheet["A1"].fill.fgColor.rgb.endswith("E9ECEF")
    assert sheet.column_dimensions["B"].width >= 40
    assert not any(cell.data_type == "f" for ws in workbook.worksheets for row in ws.iter_rows() for cell in row)
    instructions = [str(workbook["Инструкция"].cell(row, 1).value) for row in range(1, workbook["Инструкция"].max_row + 1)]
    assert any("Одна строка таблицы" in line for line in instructions)
    assert any("не объединяйте ячейки" in line for line in instructions)
    assert any("будущем добавит" in line for line in instructions)
    workbook.close()

    parsed = TenderWorkbookParser().parse(path, tender_id="a" * 32)
    assert parsed["official_template"] is True
    assert parsed["mapping_required"] is False
    assert parsed["item_count"] == 1
    row = parsed["rows"][0]
    assert row["resource_code"] == "R-001"
    assert row["article"] == "SKU-1"
    assert row["resource_code"] != row["article"]
    assert parsed["logical_right_column"] == "G"
    assert [item["column"] for item in parsed["future_output_columns"]] == ["H", "I"]


def test_official_template_blank_prepared_rows_are_ignored(tmp_path):
    path = tmp_path / "blank.xlsx"
    _official(path, include_required=False)
    parsed = TenderWorkbookParser().parse(path)
    assert parsed["official_template"] is True
    assert parsed["mapping_required"] is False
    assert parsed["item_count"] == 0
    assert parsed["counts"]["ignored"] == 0


def test_official_template_rejects_changed_headers_and_duplicate_table(tmp_path):
    path = tmp_path / "bad-template.xlsx"
    _official(path, include_required=False)
    workbook = load_workbook(path)
    workbook[TEMPLATE_SHEET]["B1"] = "Имя"
    workbook.save(path)
    with pytest.raises(TenderParseError, match="Заголовки официальной таблицы"):
        TenderWorkbookParser().parse(path)

    stripped_path = tmp_path / "stripped-template.xlsx"
    _official(stripped_path, include_required=False)
    stripped = load_workbook(stripped_path)
    for column in range(1, len(TEMPLATE_HEADERS) + 1):
        stripped[TEMPLATE_SHEET].cell(1, column).value = None
    stripped[TEMPLATE_SHEET].tables.clear()
    stripped.save(stripped_path)
    with pytest.raises(TenderParseError, match="отсутствует корректная таблица AveronTenderInput"):
        TenderWorkbookParser().parse(stripped_path)


def test_official_template_accepts_resized_371_row_table_and_requires_schema_marker(tmp_path):
    path = tmp_path / "resized-official.xlsx"
    _official(path, include_required=False)
    workbook = load_workbook(path)
    sheet = workbook[TEMPLATE_SHEET]
    sheet.tables[TEMPLATE_TABLE].ref = "A1:G371"
    sheet["A371"] = "R-371"
    sheet["B371"] = "Синтетическая позиция 371"
    sheet["C371"] = "шт"
    sheet["D371"] = 1
    workbook.save(path)
    workbook.close()
    parsed = TenderWorkbookParser().parse(path)
    assert parsed["table_ref"] == "A1:G371"
    assert parsed["item_count"] == 1 and parsed["invalid_count"] == 0
    assert parsed["rows"][0]["excel_row"] == 371

    missing_marker = tmp_path / "missing-schema.xlsx"
    _official(missing_marker, include_required=False)
    workbook = load_workbook(missing_marker)
    del workbook.defined_names[TEMPLATE_SCHEMA_NAME]
    workbook.save(missing_marker)
    workbook.close()
    with pytest.raises(TenderParseError, match="отсутствует версия схемы"):
        TenderWorkbookParser().parse(missing_marker)

    wrong_marker = tmp_path / "wrong-schema.xlsx"
    _official(wrong_marker, include_required=False)
    workbook = load_workbook(wrong_marker)
    workbook.defined_names[TEMPLATE_SCHEMA_NAME].attr_text = "2"
    workbook.save(wrong_marker)
    workbook.close()
    with pytest.raises(TenderParseError, match="Версия схемы официального шаблона"):
        TenderWorkbookParser().parse(wrong_marker)


def test_manifest_format_fingerprint_is_stable_and_tracks_style_properties(tmp_path):
    original = tmp_path / "format-base.xlsx"
    _official(original)
    parser = TenderWorkbookParser()

    def fingerprint(path):
        parsed = parser.parse(path)
        cell = next(item for item in parsed["manifest"]["meaningful_cells"] if item["cell"] == "B2")
        return cell["style_fingerprint"]

    baseline = fingerprint(original)
    assert fingerprint(original) == baseline
    mutations = ("number_format", "font", "fill", "border", "alignment", "protection")
    for property_name in mutations:
        path = tmp_path / f"format-{property_name}.xlsx"
        path.write_bytes(original.read_bytes())
        workbook = load_workbook(path)
        cell = workbook[TEMPLATE_SHEET]["B2"]
        if property_name == "number_format":
            cell.number_format = "0.000"
        elif property_name == "font":
            style = copy(cell.font)
            style.bold = True
            cell.font = style
        elif property_name == "fill":
            cell.fill = PatternFill(fill_type="solid", fgColor="FFFF00")
        elif property_name == "border":
            style = copy(cell.border)
            style.left = Side(style="thin", color="FF000000")
            cell.border = style
        elif property_name == "alignment":
            style = copy(cell.alignment)
            style.wrap_text = True
            cell.alignment = style
        else:
            style = copy(cell.protection)
            style.hidden = True
            cell.protection = style
        workbook.save(path)
        workbook.close()
        assert fingerprint(path) != baseline, property_name


def test_invalid_item_candidates_are_exposed_safely_and_block_confirmation(tmp_path):
    source = tmp_path / "incomplete.xlsx"
    _official(source, include_required=False)
    workbook = load_workbook(source)
    sheet = workbook[TEMPLATE_SHEET]
    sheet["A2"] = "R-2"
    sheet["B2"] = "Синтетическая позиция"
    sheet["C2"] = "шт"
    sheet["D2"] = None
    workbook.save(source)
    workbook.close()
    payload = source.read_bytes()
    parser = TenderWorkbookParser()
    analysis = parser.parse(source)
    assert analysis["counts"]["invalid"] == analysis["invalid_count"] == 1
    assert analysis["item_count"] == 0
    assert analysis["invalid_examples"] == [{"excel_row": 2, "reasons": ["missing_quantity"]}]
    assert "Синтетическая позиция" not in json.dumps(analysis["invalid_examples"], ensure_ascii=False)

    repository = TenderWorkspaceRepository(tmp_path / "repo-data")
    digest = hashlib.sha256(payload).hexdigest()
    preview = repository.reserve_preview("owner", source.name, len(payload), digest)
    repository.write_preview_source(preview["preview_id"], payload)
    repository.update_preview(preview["preview_id"], "owner", status="ready", analysis=analysis, parser_version=analysis["parser_version"], mapping=analysis["mapping"])
    with pytest.raises(TenderWorkspaceError) as rejected:
        repository.confirm(preview["preview_id"], "owner")
    assert rejected.value.status_code == 409
    assert rejected.value.code == "TENDER_INVALID_ROWS"


def test_369_valid_items_plus_one_invalid_candidate_cannot_be_confirmed(tmp_path):
    source = tmp_path / "large-incomplete.xlsx"
    _official(source, include_required=False)
    workbook = load_workbook(source)
    sheet = workbook[TEMPLATE_SHEET]
    for row in range(2, 371):
        sheet.cell(row, 1, f"R-{row}")
        sheet.cell(row, 2, f"Синтетическая позиция {row}")
        sheet.cell(row, 3, "шт")
        sheet.cell(row, 4, 1)
    sheet["A371"] = "R-371"
    sheet["B371"] = "Синтетическая позиция 371"
    sheet["C371"] = "шт"
    sheet["D371"] = None
    workbook.save(source)
    workbook.close()
    payload = source.read_bytes()
    analysis = TenderWorkbookParser().parse(source)
    assert analysis["item_count"] == 369 and analysis["invalid_count"] == 1
    repository = TenderWorkspaceRepository(tmp_path / "large-repo-data")
    digest = hashlib.sha256(payload).hexdigest()
    preview = repository.reserve_preview("owner-large", source.name, len(payload), digest)
    repository.write_preview_source(preview["preview_id"], payload)
    repository.update_preview(preview["preview_id"], "owner-large", status="ready", analysis=analysis, parser_version=analysis["parser_version"], mapping=analysis["mapping"])
    with pytest.raises(TenderWorkspaceError) as rejected:
        repository.confirm(preview["preview_id"], "owner-large")
    assert rejected.value.code == "TENDER_INVALID_ROWS"


def test_fallback_name_only_may_be_section_but_item_signals_without_quantity_are_invalid(tmp_path):
    path = tmp_path / "row-classes.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Код ресурса", "Наименование", "Ед. изм.", "Количество", "Артикул"])
    sheet.append([None, "Синтетический раздел", None, None, None])
    sheet.append(["R-1", "Синтетическая позиция", "шт", None, None])
    sheet.append([None, "Ещё одна позиция", None, "не число", None])
    workbook.save(path)
    parsed = TenderWorkbookParser().parse(path)
    assert [row["row_type"] for row in parsed["rows"]] == ["section", "invalid", "invalid"]
    assert parsed["invalid_count"] == 2
    assert parsed["invalid_examples"] == [
        {"excel_row": 3, "reasons": ["missing_quantity"]},
        {"excel_row": 4, "reasons": ["invalid_quantity"]},
    ]


def test_fallback_ignores_column_scaffold_and_numbered_note_and_recognizes_merged_sections(tmp_path):
    path = tmp_path / "structural-row-classes.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = "Код ресурса"
    sheet["C1"] = "Наименование"
    sheet["D1"] = "Ед. изм."
    sheet["E1"] = "Кол-во"
    # A/C/D/E repeat their physical column indexes, a source-layout scaffold.
    sheet["A2"] = 1
    sheet["C2"] = 3
    sheet["D2"] = 4
    sheet["E2"] = "5"
    # The section label is anchored outside the mapped name column.
    sheet.merge_cells("A3:E3")
    sheet["A3"] = "Синтетический раздел"
    # A resource code plus item name and unit without quantity remains invalid.
    sheet["A4"] = "REF-4"
    sheet["C4"] = "Синтетическая позиция без количества"
    sheet["D4"] = "шт"
    # A numbered note in the resource/reference column is not an item.
    sheet["A5"] = "1. Синтетическое дополнительное примечание"
    # Unknown unit vocabulary alone does not invalidate a complete item.
    sheet["A6"] = "REF-6"
    sheet["C6"] = "Синтетическая позиция с неизвестной единицей"
    sheet["D6"] = "единица-неизвестна"
    sheet["E6"] = 2
    # Short numbered or ordinary resource codes alone remain incomplete rows.
    sheet["A7"] = "1. X"
    sheet["A8"] = "REF-8"
    workbook.save(path)
    workbook.close()

    parsed = TenderWorkbookParser().parse(path)
    rows_by_excel_row = {row["excel_row"]: row for row in parsed["rows"]}
    assert rows_by_excel_row[2]["row_type"] == "ignored"
    assert rows_by_excel_row[3]["row_type"] == "section"
    assert rows_by_excel_row[4]["row_type"] == "invalid"
    assert rows_by_excel_row[5]["row_type"] == "ignored"
    assert rows_by_excel_row[6]["row_type"] == "item"
    assert rows_by_excel_row[7]["row_type"] == "invalid"
    assert rows_by_excel_row[8]["row_type"] == "invalid"
    assert rows_by_excel_row[6]["unit_basis"]["trusted"] is False
    assert rows_by_excel_row[6]["warnings"]
    assert parsed["counts"] == {"item": 1, "section": 1, "total": 0, "ignored": 2, "invalid": 3}


def test_parser_deletes_normalized_temporary_and_preserves_uploaded_source(tmp_path, monkeypatch):
    import shutil
    import averon_import.services.manual_tenders.parser as parser_module
    path = tmp_path / "source.xlsx"
    _official(path)
    original = path.read_bytes()
    normalized = tmp_path / "normalized-temporary.xlsx"
    def normalize(source):
        shutil.copyfile(source, normalized)
        return normalized
    monkeypatch.setattr(parser_module, "shared_preflight_xlsx", normalize)
    assert TenderWorkbookParser().parse(path)["item_count"] == 1
    assert path.read_bytes() == original
    assert not normalized.exists()


def test_fallback_header_12_a_c_d_e_boundary_hidden_columns_and_row_identity(tmp_path):
    path = tmp_path / "synthetic-legacy.xlsx"
    _fallback(path)
    parsed = TenderWorkbookParser().parse(path, tender_id="tender-id")
    assert parsed["sheet_name"] == "Ресурсная ведомость1 - СУВР(О)_"
    assert parsed["header_row"] == 12
    assert parsed["mapping"] == {"resource_code": 1, "name": 3, "unit": 4, "quantity": 5, "article": None, "manufacturer": None, "model": None}
    assert parsed["counts"]["item"] == 370
    assert parsed["counts"]["section"] == 2
    assert parsed["counts"]["total"] == 2
    assert parsed["logical_right_column"] == "E"
    assert parsed["future_output_columns"][0]["column"] == "F"
    assert parsed["future_output_columns"][0]["hidden"] is True
    assert parsed["future_output_columns"][0]["value_count"] == 1
    assert parsed["future_output_columns"][1]["hidden"] is True
    assert parsed["future_output_columns"][1]["value_count"] == 1
    assert "DISTANT_PRICE_COLUMNS" in {warning["code"] for warning in parsed["warnings"]}
    items = [row for row in parsed["rows"] if row["row_type"] == "item"]
    assert len({row["source_row_id"] for row in items}) == 370
    assert all(row["resource_code"] == "REP-1" and row["article"] == "" for row in items)
    assert len([row for row in items if row["unit_basis"]["trusted"] and row["raw_unit"] not in {"шт", "кг"}]) == 17
    manifest = parsed["manifest"]
    assert manifest["source_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert manifest["sheets"][0]["print_area"]
    assert manifest["sheets"][0]["merged_ranges"]
    assert manifest["sheets"][0]["hidden_columns"] == ["F", "G"]


def test_fallback_skips_long_data_rows_as_header_candidates_without_truncating_source(tmp_path):
    path = tmp_path / "long-preamble-and-data.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Ресурсная ведомость"
    for coordinate, value in {
        "A12": "№ смет/Код ресурса", "C12": "Наименование", "D12": "Ед. изм.", "E12": "Кол-во",
    }.items():
        sheet[coordinate] = value
    long_449 = "Д" * 449
    long_393 = "Е" * 393
    for row, code, name in ((21, "R-21", long_449), (24, "R-24", long_393)):
        sheet.cell(row, 1, code)
        sheet.cell(row, 3, name)
        sheet.cell(row, 4, "шт")
        sheet.cell(row, 5, 1)
    workbook.save(path)
    workbook.close()

    parsed = TenderWorkbookParser().parse(path)
    assert parsed["header_row"] == 12
    assert parsed["mapping"] == {"resource_code": 1, "name": 3, "unit": 4, "quantity": 5, "article": None, "manufacturer": None, "model": None}
    assert parsed["item_count"] == 2 and parsed["invalid_count"] == 0
    parsed_names = {row["excel_row"]: row["name"] for row in parsed["rows"] if row["row_type"] == "item"}
    assert parsed_names == {21: long_449, 24: long_393}
    manifest_values = {cell["cell"]: cell["value"] for cell in parsed["manifest"]["meaningful_cells"]}
    assert manifest_values["C21"] == long_449 and manifest_values["C24"] == long_393


def test_overlong_header_candidate_and_explicitly_selected_header_are_rejected(tmp_path):
    path = tmp_path / "overlong-header.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Long header"
    sheet.cell(12, 1, "Наименование" + "X" * (MAX_HEADER_LENGTH + 1))
    sheet.cell(12, 2, "Количество")
    workbook.save(path)
    workbook.close()

    with pytest.raises(TenderParseError, match="Не удалось определить лист и заголовки"):
        TenderWorkbookParser().parse(path)
    with pytest.raises(TenderParseError, match="Заголовок XLSX превышает допустимую длину"):
        TenderWorkbookParser().parse(path, selected_sheet="Long header", selected_header_row=12)


def test_generic_code_header_is_not_guessed_as_article_and_mapping_can_be_explicit(tmp_path):
    path = tmp_path / "ambiguous-code.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Код", "Наименование", "Количество"])
    sheet.append(["001", "Синтетическая позиция", 1])
    workbook.save(path)
    parser = TenderWorkbookParser()
    preview = parser.parse(path)
    assert preview["mapping"]["article"] is None
    assert preview["mapping"]["resource_code"] is None
    assert preview["mapping_required"] is True
    assert preview["rows"][0]["article"] == ""
    mapped = parser.parse(path, mapping_override={"resource_code": 1, "name": 2, "quantity": 3}, selected_sheet=sheet.title, selected_header_row=1)
    assert mapped["rows"][0]["resource_code"] == "001"
    assert mapped["rows"][0]["article"] == ""
    with pytest.raises(TenderParseError, match="Код ресурса нельзя"):
        parser.parse(path, mapping_override={"resource_code": 1, "name": 2, "quantity": 3, "article": 1}, selected_sheet=sheet.title, selected_header_row=1)
    with pytest.raises(TenderParseError, match="явно указывает на артикул"):
        parser.parse(path, mapping_override={"name": 2, "quantity": 3, "article": 1}, selected_sheet=sheet.title, selected_header_row=1)

    explicit_article = tmp_path / "explicit-article.xlsx"
    explicit = Workbook()
    explicit_sheet = explicit.active
    explicit_sheet.append(["Артикул производителя", "Наименование", "Количество"])
    explicit_sheet.append(["SKU-001", "Синтетическая позиция", 1])
    explicit.save(explicit_article)
    explicit_result = parser.parse(explicit_article)
    assert explicit_result["rows"][0]["article"] == "SKU-001"


@pytest.mark.parametrize("value", [True, datetime(2026, 1, 1), float("nan"), float("inf"), 0, -1, "1 шт", "1,2.3"])
def test_ambiguous_quantity_values_are_not_trusted(tmp_path, value):
    path = tmp_path / "quantity.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Наименование", "Количество"])
    sheet.append(["Синтетическая позиция", value])
    workbook.save(path)
    parsed = TenderWorkbookParser().parse(path)
    assert parsed["rows"][0]["quantity_trusted"] is False


def test_formula_quantity_never_becomes_authoritative(tmp_path):
    path = tmp_path / "formula.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Наименование", "Количество"])
    sheet.append(["Синтетическая позиция", "=1+2"])
    workbook.save(path)
    parsed = TenderWorkbookParser().parse(path)
    assert parsed["rows"][0]["quantity_raw"] == "=1+2"
    assert parsed["rows"][0]["quantity_trusted"] is False


@pytest.mark.parametrize(("unit", "base", "scale", "dimension"), [
    ("шт", "шт", "1", "count"), ("10 шт", "шт", "10", "count"),
    ("100 шт", "шт", "100", "count"), ("1000 шт", "шт", "1000", "count"),
    ("м", "м", "1", "length"), ("10 м", "м", "10", "length"),
    ("1000 м", "м", "1000", "length"), ("кг", "кг", "1", "mass"),
    ("т", "кг", "1000", "mass"), ("м2", "м2", "1", "area"),
    ("м3", "м3", "1", "volume"), ("л", "л", "1", "volume"),
    ("компл", "компл", "1", "set"), ("уп", "уп", "1", "package"),
])
def test_tender_unit_basis_supported_units(unit, base, scale, dimension):
    result = parse_unit_basis(unit)
    assert result["trusted"] is True
    assert (result["base_unit"], result["scale"], result["dimension"]) == (base, scale, dimension)


def test_unknown_and_unapproved_pack_scales_remain_unknown():
    assert parse_unit_basis("коробка 12 шт")["trusted"] is False
    assert parse_unit_basis("12 уп")["trusted"] is False
    assert parse_unit_basis("10 кг")["trusted"] is False
    assert parse_unit_basis("10 компл")["trusted"] is False


def test_xlsx_preflight_rejects_non_xlsx_and_unsafe_archive_members(tmp_path):
    not_xlsx = tmp_path / "book.xlsm"
    not_xlsx.write_bytes(b"fake")
    with pytest.raises(TenderParseError, match=r"\.xlsx"):
        TenderWorkbookParser().parse(not_xlsx)
    malformed = tmp_path / "malformed.xlsx"
    malformed.write_bytes(b"not a zip archive")
    with pytest.raises(TenderParseError, match="корректной книгой XLSX"):
        preflight_tender_xlsx(malformed)

    malicious = tmp_path / "traversal.xlsx"
    with zipfile.ZipFile(malicious, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
        archive.writestr("../escape", "x")
    with pytest.raises(TenderParseError, match="небезопасные"):
        preflight_tender_xlsx(malicious)


def test_xlsx_preflight_rejects_case_collisions_external_relationships_and_entities(tmp_path):
    collision = tmp_path / "collision.xlsx"
    with zipfile.ZipFile(collision, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
        archive.writestr("XL/WORKBOOK.XML", "<workbook/>")
    with pytest.raises(TenderParseError, match="без учёта регистра"):
        preflight_tender_xlsx(collision)

    external = tmp_path / "external.xlsx"
    with zipfile.ZipFile(external, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships><Relationship Target="https://example.invalid" TargetMode="External"/></Relationships>')
    with pytest.raises(TenderParseError, match="Внешние связи"):
        preflight_tender_xlsx(external)

    entities = tmp_path / "entities.xlsx"
    with zipfile.ZipFile(entities, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", '<!DOCTYPE workbook [<!ENTITY x "unsafe">]><workbook/>')
    with pytest.raises(TenderParseError, match="DTD/ENTITY"):
        preflight_tender_xlsx(entities)


def test_xlsx_preflight_rejects_vba_and_compressed_size_bounds(tmp_path):
    macro = tmp_path / "macro.xlsx"
    with zipfile.ZipFile(macro, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
        archive.writestr("xl/vbaProject.bin", b"not a macro")
    with pytest.raises(TenderParseError, match="макросами"):
        preflight_tender_xlsx(macro)


def test_xlsx_preflight_rejects_duplicate_members_member_count_uncompressed_size_and_ratio(tmp_path, monkeypatch):
    import averon_import.services.manual_tenders.parser as parser_module
    duplicate = tmp_path / "duplicate.xlsx"
    with zipfile.ZipFile(duplicate, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
    with pytest.raises(TenderParseError, match="повторные компоненты"):
        preflight_tender_xlsx(duplicate)

    too_many = tmp_path / "too-many.xlsx"
    with zipfile.ZipFile(too_many, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
        archive.writestr("xl/third.xml", "x")
    monkeypatch.setattr(parser_module, "MAX_ZIP_MEMBERS", 2)
    with pytest.raises(TenderParseError, match="число компонентов"):
        preflight_tender_xlsx(too_many)
    monkeypatch.setattr(parser_module, "MAX_ZIP_MEMBERS", 1000)

    oversized = tmp_path / "oversized.xlsx"
    with zipfile.ZipFile(oversized, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
        archive.writestr("xl/data.bin", b"x" * 80)
    monkeypatch.setattr(parser_module, "MAX_UNCOMPRESSED_BYTES", 40)
    with pytest.raises(TenderParseError, match="Распакованный размер"):
        preflight_tender_xlsx(oversized)
    monkeypatch.setattr(parser_module, "MAX_UNCOMPRESSED_BYTES", 20 * 1024 * 1024)

    high_ratio = tmp_path / "high-ratio.xlsx"
    with zipfile.ZipFile(high_ratio, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
        archive.writestr("xl/large.xml", "A" * 2048)
    monkeypatch.setattr(parser_module, "MAX_COMPRESSION_RATIO", 1)
    with pytest.raises(TenderParseError, match="Коэффициент сжатия"):
        preflight_tender_xlsx(high_ratio)


def test_fallback_ambiguous_columns_require_manual_mapping_and_item_limit_is_enforced(tmp_path):
    ambiguous = tmp_path / "duplicate-header.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Наименование", "Наименование", "Количество"])
    sheet.append(["Первое имя", "Второе имя", 1])
    workbook.save(ambiguous)
    parsed = TenderWorkbookParser().parse(ambiguous)
    assert parsed["mapping_required"] is True
    mapped = TenderWorkbookParser().parse(
        ambiguous,
        mapping_override={"name": 2, "quantity": 3},
        selected_sheet=sheet.title,
        selected_header_row=1,
    )
    assert mapped["mapping_required"] is False
    assert mapped["rows"][0]["name"] == "Второе имя"

    excessive = tmp_path / "over-500.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Наименование", "Количество"])
    for index in range(MAX_ACTUAL_ITEMS + 1):
        sheet.append([f"Синтетическая позиция {index}", 1])
    workbook.save(excessive)
    with pytest.raises(TenderParseError, match="больше 500"):
        TenderWorkbookParser().parse(excessive)


def test_preview_and_confirmed_workspaces_are_owner_bound_persistent_and_path_free(tmp_path):
    activity = TenderActivityRegistry()
    repository = TenderWorkspaceRepository(tmp_path / "data", activity)
    source_path = tmp_path / "sample.xlsx"
    payload = _official(source_path)
    digest = hashlib.sha256(payload).hexdigest()
    preview = repository.reserve_preview("owner-a", "sample.xlsx", len(payload), digest)
    repository.write_preview_source(preview["preview_id"], payload)
    analysis = TenderWorkbookParser().parse(repository.preview_root / preview["preview_id"] / "source.xlsx", tender_id=preview["preview_id"])
    repository.update_preview(preview["preview_id"], "owner-a", status="ready", parser_version=analysis["parser_version"], mapping=analysis["mapping"], analysis=analysis)
    with pytest.raises(TenderWorkspaceError) as denied:
        repository.public_preview(preview["preview_id"], "owner-b")
    assert denied.value.status_code == 404
    public_preview = repository.public_preview(preview["preview_id"], "owner-a")
    assert "source.xlsx" not in json.dumps(public_preview)
    assert str(tmp_path) not in json.dumps(public_preview)
    workspace = repository.confirm(preview["preview_id"], "owner-a")
    assert workspace["counts"]["item"] == 1
    assert workspace["rows"][0]["resource_code"] == "R-001"
    assert workspace["rows"][0]["article"] == "SKU-1"
    assert "owner_id" not in workspace and str(tmp_path) not in json.dumps(workspace)
    original_source = repository.workspace_root / workspace["tender_id"] / "source.xlsx"
    assert original_source.read_bytes() == payload
    assert original_source.stat().st_mode & 0o200 == 0
    recovered = TenderWorkspaceRepository(tmp_path / "data", activity).public_workspace(workspace["tender_id"], "owner-a")
    assert recovered["source_sha256"] == digest


def test_preview_and_confirmed_ttl_absolute_lifetime_and_restart_job_semantics(tmp_path):
    repository = TenderWorkspaceRepository(tmp_path / "data")
    payload_path = tmp_path / "source.xlsx"
    payload = _official(payload_path)
    digest = hashlib.sha256(payload).hexdigest()
    preview = repository.reserve_preview("owner", "source.xlsx", len(payload), digest)
    repository.write_preview_source(preview["preview_id"], payload)
    metadata_path = repository.preview_root / preview["preview_id"] / "workspace.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert datetime.fromisoformat(metadata["expires_at"]) - datetime.fromisoformat(metadata["created_at"]) == timedelta(seconds=PREVIEW_TTL_SECONDS)
    metadata["status"] = "queued"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    restarted = TenderWorkspaceRepository(tmp_path / "data")
    assert restarted.public_preview(preview["preview_id"], "owner")["status"] == "interrupted"
    assert "перезапуском" in restarted.public_preview(preview["preview_id"], "owner")["error"]


def test_confirmed_ttl_does_not_extend_absolute_deadline(tmp_path):
    repository = TenderWorkspaceRepository(tmp_path / "data")
    payload_path = tmp_path / "source.xlsx"
    payload = _official(payload_path)
    digest = hashlib.sha256(payload).hexdigest()
    preview = repository.reserve_preview("owner", "source.xlsx", len(payload), digest)
    repository.write_preview_source(preview["preview_id"], payload)
    analysis = TenderWorkbookParser().parse(repository.preview_root / preview["preview_id"] / "source.xlsx")
    repository.update_preview(preview["preview_id"], "owner", status="ready", analysis=analysis, parser_version=analysis["parser_version"], mapping=analysis["mapping"])
    workspace = repository.confirm(preview["preview_id"], "owner")
    metadata_path = repository.workspace_root / workspace["tender_id"] / "workspace.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert datetime.fromisoformat(metadata["absolute_expires_at"]) - datetime.fromisoformat(metadata["created_at"]) == timedelta(seconds=ABSOLUTE_TTL_SECONDS)
    before = metadata["absolute_expires_at"]
    repository.public_workspace(workspace["tender_id"], "owner")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["absolute_expires_at"] == before
    assert datetime.fromisoformat(metadata["last_access_at"]) - datetime.fromisoformat(metadata["created_at"]) < timedelta(seconds=IDLE_TTL_SECONDS)


def test_concurrent_workspace_touches_keep_metadata_and_source_coherent(tmp_path):
    repository = TenderWorkspaceRepository(tmp_path / "concurrent-data")
    source_path = tmp_path / "concurrent-source.xlsx"
    payload = _official(source_path)
    digest = hashlib.sha256(payload).hexdigest()
    preview = repository.reserve_preview("owner-concurrent", source_path.name, len(payload), digest)
    repository.write_preview_source(preview["preview_id"], payload)
    analysis = TenderWorkbookParser().parse(repository.preview_root / preview["preview_id"] / "source.xlsx")
    repository.update_preview(preview["preview_id"], "owner-concurrent", status="ready", analysis=analysis, parser_version=analysis["parser_version"], mapping=analysis["mapping"])
    workspace = repository.confirm(preview["preview_id"], "owner-concurrent")
    workspace_dir = repository.workspace_root / workspace["tender_id"]
    metadata_path = workspace_dir / "workspace.json"
    original = json.loads(metadata_path.read_text(encoding="utf-8"))
    barrier = threading.Barrier(12)

    def touch_workspace(_):
        barrier.wait()
        return repository.public_workspace(workspace["tender_id"], "owner-concurrent", touch=True)

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(touch_workspace, range(12)))
    persisted = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert len(results) == 12
    assert persisted["owner_id"] == original["owner_id"]
    assert persisted["filename"] == original["filename"]
    assert persisted["source_sha256"] == original["source_sha256"] == digest
    assert persisted["absolute_expires_at"] == original["absolute_expires_at"]
    assert hashlib.sha256((workspace_dir / "source.xlsx").read_bytes()).hexdigest() == digest
    assert datetime.fromisoformat(persisted["last_access_at"])
    assert not list(workspace_dir.glob(".workspace.json.*.tmp"))
    orphan = workspace_dir / ".workspace.json.orphan.tmp"
    orphan.write_text("incomplete", encoding="utf-8")
    os.utime(orphan, (1, 1))
    repository.cleanup()
    assert not orphan.exists()


def test_workspace_quotas_are_atomic_and_enforced(tmp_path, monkeypatch):
    import averon_import.services.manual_tenders.repository as repository_module
    monkeypatch.setattr(repository_module, "MAX_WORKSPACES_GLOBAL", 20)
    repository = TenderWorkspaceRepository(tmp_path / "data")

    def prepare(target_repository, owner, marker):
        source = tmp_path / f"{marker}.xlsx"
        payload = _official(source)
        workbook = load_workbook(source)
        workbook[TEMPLATE_SHEET]["A2"] = marker
        workbook.save(source)
        payload = source.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        preview = target_repository.reserve_preview(owner, f"{marker}.xlsx", len(payload), digest)
        target_repository.write_preview_source(preview["preview_id"], payload)
        analysis = TenderWorkbookParser().parse(target_repository.preview_root / preview["preview_id"] / "source.xlsx", tender_id=preview["preview_id"])
        target_repository.update_preview(preview["preview_id"], owner, status="ready", analysis=analysis, parser_version=analysis["parser_version"], mapping=analysis["mapping"])
        return preview["preview_id"]

    for index in range(MAX_WORKSPACES_PER_USER):
        preview_id = prepare(repository, "same-owner", f"quota-{index}")
        assert repository.confirm(preview_id, "same-owner")
    over_user = prepare(repository, "same-owner", "quota-over-user")
    with pytest.raises(TenderWorkspaceError) as user_limit:
        repository.confirm(over_user, "same-owner")
    assert user_limit.value.code == "TENDER_WORKSPACE_QUOTA"
    assert len(repository._workspace_dirs()) == MAX_WORKSPACES_PER_USER

    global_repository = TenderWorkspaceRepository(tmp_path / "global-data")
    monkeypatch.setattr(repository_module, "MAX_WORKSPACES_GLOBAL", 2)
    previews = []
    for index in range(5):
        owner = f"global-owner-{index}"
        previews.append((prepare(global_repository, owner, f"global-{index}"), owner))
    barrier = threading.Barrier(len(previews))
    def confirm_global(pair):
        preview_id, owner = pair
        barrier.wait()
        try:
            return global_repository.confirm(preview_id, owner)["tender_id"]
        except TenderWorkspaceError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=len(previews)) as executor:
        results = list(executor.map(confirm_global, previews))
    assert sum(not result.startswith("TENDER_") for result in results) == 2
    assert results.count("TENDER_WORKSPACE_QUOTA") == 3
    assert len(global_repository._workspace_dirs()) == 2
    assert MAX_UNCOMPRESSED_BYTES == 20 * 1024 * 1024 and MAX_UPLOAD_BYTES == 5 * 1024 * 1024 and MAX_ACTUAL_ITEMS == 500


def test_activity_lease_prevents_delete_and_lazy_cleanup(tmp_path):
    activity = TenderActivityRegistry()
    repository = TenderWorkspaceRepository(tmp_path / "data", activity)
    payload_path = tmp_path / "source.xlsx"
    payload = _official(payload_path)
    digest = hashlib.sha256(payload).hexdigest()
    preview = repository.reserve_preview("owner", "source.xlsx", len(payload), digest)
    repository.write_preview_source(preview["preview_id"], payload)
    analysis = TenderWorkbookParser().parse(repository.preview_root / preview["preview_id"] / "source.xlsx")
    repository.update_preview(preview["preview_id"], "owner", status="ready", analysis=analysis, parser_version=analysis["parser_version"], mapping=analysis["mapping"])
    workspace = repository.confirm(preview["preview_id"], "owner")
    lease = activity.acquire(workspace["tender_id"])
    with pytest.raises(TenderWorkspaceError) as busy:
        repository.delete(workspace["tender_id"], "owner")
    assert busy.value.code == "TENDER_WORKSPACE_BUSY"
    metadata_path = repository.workspace_root / workspace["tender_id"] / "workspace.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    expired = datetime.now(timezone.utc) - timedelta(days=3)
    metadata["last_access_at"] = expired.isoformat()
    metadata["absolute_expires_at"] = expired.isoformat()
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    repository.cleanup()
    assert metadata_path.exists()
    lease.release()
    lease.release()
    repository.cleanup()
    assert not metadata_path.exists()


class _ApiResponse:
    def __init__(self, status, headers, body):
        self.status_code = status
        self.headers = {key.decode().lower(): value.decode() for key, value in headers}
        self.content = body
        self.text = body.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.content.decode("utf-8"))


def _api_request(app, method, path, *, headers=None, body=b""):
    sent = False
    messages = []
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8765),
    }
    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}
    async def send(message):
        messages.append(message)
    asyncio.run(app(scope, receive, send))
    start = next(item for item in messages if item["type"] == "http.response.start")
    response_body = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
    return _ApiResponse(start["status"], start.get("headers", []), response_body)


def _multipart_file(name: str, filename: str, payload: bytes) -> tuple[str, bytes]:
    boundary = f"----Tender-{uuid.uuid4().hex}"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
        "Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n"
    ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()
    return boundary, body


@pytest.fixture
def tender_api(monkeypatch, tmp_path):
    monkeypatch.setenv("AVERON_AUTH_MODE", "trusted_proxy")
    monkeypatch.setenv("AVERON_PROXY_SECRET", "tender-test-proxy")
    monkeypatch.setenv("AVERON_ADMIN_USERS", "tender-admin")
    monkeypatch.setenv("AVERON_DATA_DIR", str(tmp_path / "app-data"))
    from averon_import import main
    activity = TenderActivityRegistry()
    repository = TenderWorkspaceRepository(tmp_path / "api-data", activity)
    monkeypatch.setattr(main, "tender_activity", activity)
    monkeypatch.setattr(main, "tender_repository", repository)
    monkeypatch.setattr(main, "tender_sourcing_runs", TenderSourcingRunStore(repository))
    isolated_jobs = JobService(max_workers=1)
    monkeypatch.setattr(main, "job_service", isolated_jobs)
    return main, repository


def _auth_headers(username="tender-user"):
    return {"X-Averon-User": username, "X-Averon-Proxy": "tender-test-proxy"}


def test_api_auth_template_preview_confirm_owner_isolation_and_delete(tender_api, tmp_path):
    main, repository = tender_api
    unauthenticated = _api_request(main.app, "GET", "/api/manual-tenders/template")
    assert unauthenticated.status_code == 401
    template = _api_request(main.app, "GET", "/api/manual-tenders/template", headers=_auth_headers())
    assert template.status_code == 200
    assert "attachment" in template.headers.get("content-disposition", "")
    workbook_path = tmp_path / "downloaded-template.xlsx"
    workbook_path.write_bytes(template.content)
    workbook = load_workbook(workbook_path, read_only=False, data_only=False)
    assert TEMPLATE_SHEET in workbook.sheetnames and TEMPLATE_TABLE in workbook[TEMPLATE_SHEET].tables
    workbook.close()

    payload = _official(tmp_path / "api-upload.xlsx")
    boundary, body = _multipart_file("file", "test.xlsx", payload)
    headers = _auth_headers()
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    uploaded = _api_request(main.app, "POST", "/api/manual-tenders/previews", headers=headers, body=body)
    assert uploaded.status_code == 202, uploaded.text
    preview_id = uploaded.json()["preview_id"]
    job_id = uploaded.json().get("job_id")
    assert repository.preview_path(preview_id, "username:tender-user")[1]["owner_id"] == "username:tender-user"
    preview = None
    for _ in range(100):
        preview = _api_request(main.app, "GET", f"/api/manual-tenders/previews/{preview_id}", headers=_auth_headers())
        assert "status" in preview.json(), f"preview={preview_id} http={preview.status_code} body={preview.text}"
        if preview.json()["status"] in {"ready", "failed"}:
            break
        threading.Event().wait(0.02)
    job_traceback = main.job_service.get(job_id, owner_id="username:tender-user").traceback if job_id else None
    assert preview.json()["status"] == "ready", f"{preview.text}\n{job_traceback or ''}"
    same_boundary, same_body = _multipart_file("file", "renamed.xlsx", payload)
    same_headers = {**_auth_headers(), "Content-Type": f"multipart/form-data; boundary={same_boundary}"}
    duplicate = _api_request(main.app, "POST", "/api/manual-tenders/previews", headers=same_headers, body=same_body)
    assert duplicate.status_code == 202
    assert duplicate.json()["preview_id"] == preview_id and duplicate.json()["deduplicated"] is True
    confirmed = _api_request(main.app, "POST", f"/api/manual-tenders/previews/{preview_id}/confirm", headers=_auth_headers())
    assert confirmed.status_code == 201, confirmed.text
    workspace = confirmed.json()
    assert workspace["counts"]["item"] == 1
    encoded = json.dumps(workspace)
    assert str(repository.root) not in encoded
    assert "owner_id" not in workspace
    forbidden = _api_request(main.app, "GET", f"/api/manual-tenders/{workspace['tender_id']}", headers=_auth_headers("other-user"))
    assert forbidden.status_code == 404
    fetched = _api_request(main.app, "GET", f"/api/manual-tenders/{workspace['tender_id']}", headers=_auth_headers())
    assert fetched.status_code == 200 and fetched.json()["tender_id"] == workspace["tender_id"]
    deleted = _api_request(main.app, "DELETE", f"/api/manual-tenders/{workspace['tender_id']}", headers=_auth_headers())
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True
    main.job_service.executor.shutdown(wait=True)


def test_api_confirmation_returns_typed_invalid_rows_error(tender_api, tmp_path):
    main, _repository = tender_api
    path = tmp_path / "api-incomplete.xlsx"
    _official(path, include_required=False)
    workbook = load_workbook(path)
    workbook[TEMPLATE_SHEET]["A2"] = "R-2"
    workbook[TEMPLATE_SHEET]["B2"] = "Синтетическая позиция"
    workbook[TEMPLATE_SHEET]["C2"] = "шт"
    workbook.save(path)
    workbook.close()
    boundary, body = _multipart_file("file", path.name, path.read_bytes())
    uploaded = _api_request(main.app, "POST", "/api/manual-tenders/previews", headers={**_auth_headers(), "Content-Type": f"multipart/form-data; boundary={boundary}"}, body=body)
    assert uploaded.status_code == 202
    preview_id = uploaded.json()["preview_id"]
    for _ in range(100):
        preview = _api_request(main.app, "GET", f"/api/manual-tenders/previews/{preview_id}", headers=_auth_headers()).json()
        if preview["status"] in {"ready", "failed"}:
            break
        threading.Event().wait(0.02)
    assert preview["status"] == "ready"
    assert preview["analysis"]["invalid_count"] == 1
    assert preview["analysis"]["invalid_examples"] == [{"excel_row": 2, "reasons": ["missing_quantity"]}]
    confirmed = _api_request(main.app, "POST", f"/api/manual-tenders/previews/{preview_id}/confirm", headers=_auth_headers())
    assert confirmed.status_code == 409
    assert confirmed.json()["detail"]["code"] == "TENDER_INVALID_ROWS"
    assert "требующие исправления" in confirmed.json()["detail"]["message"]
    main.job_service.executor.shutdown(wait=True)


def test_api_mapping_resolves_generic_code_without_article_leak(tender_api, tmp_path):
    main, _repository = tender_api
    path = tmp_path / "manual-mapping.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Код", "Наименование", "Количество"])
    sheet.append(["R-09", "Синтетическая позиция", 2])
    workbook.save(path)
    boundary, body = _multipart_file("file", path.name, path.read_bytes())
    headers = {**_auth_headers(), "Content-Type": f"multipart/form-data; boundary={boundary}"}
    uploaded = _api_request(main.app, "POST", "/api/manual-tenders/previews", headers=headers, body=body)
    preview_id = uploaded.json()["preview_id"]
    for _ in range(100):
        preview = _api_request(main.app, "GET", f"/api/manual-tenders/previews/{preview_id}", headers=_auth_headers()).json()
        assert "status" in preview, preview
        if preview["status"] in {"ready", "failed"}:
            break
        threading.Event().wait(0.02)
    assert preview["analysis"]["mapping_required"] is True
    analysis = preview["analysis"]
    mapping = {"resource_code": 1, "name": 2, "unit": None, "quantity": 3, "article": None, "manufacturer": None, "model": None}
    unsafe_article_mapping = {**mapping, "resource_code": None, "article": 1}
    unsafe_request = json.dumps({"sheet_name": analysis["sheet_name"], "header_row": analysis["header_row"], "mapping": unsafe_article_mapping}, ensure_ascii=False).encode("utf-8")
    unsafe = _api_request(main.app, "POST", f"/api/manual-tenders/previews/{preview_id}/mapping", headers={**_auth_headers(), "Content-Type": "application/json"}, body=unsafe_request)
    assert unsafe.status_code == 400
    assert _api_request(main.app, "GET", f"/api/manual-tenders/previews/{preview_id}", headers=_auth_headers()).json()["status"] == "ready"
    request = json.dumps({"sheet_name": analysis["sheet_name"], "header_row": analysis["header_row"], "mapping": mapping}, ensure_ascii=False).encode("utf-8")
    mapped = _api_request(main.app, "POST", f"/api/manual-tenders/previews/{preview_id}/mapping", headers={**_auth_headers(), "Content-Type": "application/json"}, body=request)
    assert mapped.status_code == 202, mapped.text
    for _ in range(100):
        preview_response = _api_request(main.app, "GET", f"/api/manual-tenders/previews/{preview_id}", headers=_auth_headers())
        preview = preview_response.json()
        if preview["status"] in {"ready", "failed"}:
            break
        threading.Event().wait(0.02)
    assert preview["status"] == "ready" and preview["analysis"]["mapping_required"] is False
    confirmed = _api_request(main.app, "POST", f"/api/manual-tenders/previews/{preview_id}/confirm", headers=_auth_headers())
    assert confirmed.status_code == 201, confirmed.text
    row = confirmed.json()["rows"][0]
    assert row["resource_code"] == "R-09"
    assert row["article"] == ""
    main.job_service.executor.shutdown(wait=True)


def test_api_mapping_accepts_only_a_detected_alternate_header_candidate(tender_api, tmp_path):
    main, _repository = tender_api
    path = tmp_path / "multiple-header-candidates.xlsx"
    workbook = Workbook()
    first = workbook.active
    first.title = "Первый лист"
    first.append(["Наименование", "Количество"])
    first.append(["Не выбранная позиция", 1])
    second = workbook.create_sheet("Второй лист")
    second.append(["Наименование", "Количество"])
    second.append(["Выбранная позиция", 2])
    workbook.save(path)
    boundary, body = _multipart_file("file", path.name, path.read_bytes())
    headers = {**_auth_headers(), "Content-Type": f"multipart/form-data; boundary={boundary}"}
    uploaded = _api_request(main.app, "POST", "/api/manual-tenders/previews", headers=headers, body=body)
    assert uploaded.status_code == 202, uploaded.text
    preview_id = uploaded.json()["preview_id"]
    for _ in range(100):
        preview_response = _api_request(main.app, "GET", f"/api/manual-tenders/previews/{preview_id}", headers=_auth_headers())
        preview = preview_response.json()
        if preview["status"] in {"ready", "failed"}:
            break
        threading.Event().wait(0.02)
    assert preview["status"] == "ready", preview_response.text
    analysis = preview["analysis"]
    alternate = next(candidate for candidate in analysis["header_candidates"] if candidate["sheet_name"] == "Второй лист")
    mapping = {"resource_code": None, "name": 1, "unit": None, "quantity": 2, "article": None, "manufacturer": None, "model": None}

    invalid = json.dumps({"sheet_name": "Незаявленный лист", "header_row": 1, "mapping": mapping}, ensure_ascii=False).encode("utf-8")
    rejected = _api_request(main.app, "POST", f"/api/manual-tenders/previews/{preview_id}/mapping", headers={**_auth_headers(), "Content-Type": "application/json"}, body=invalid)
    assert rejected.status_code == 409
    invalid_mapping = {**mapping, "resource_code": 1, "article": 1}
    invalid = json.dumps({"sheet_name": alternate["sheet_name"], "header_row": alternate["header_row"], "mapping": invalid_mapping}, ensure_ascii=False).encode("utf-8")
    rejected = _api_request(main.app, "POST", f"/api/manual-tenders/previews/{preview_id}/mapping", headers={**_auth_headers(), "Content-Type": "application/json"}, body=invalid)
    assert rejected.status_code == 400
    still_ready = _api_request(main.app, "GET", f"/api/manual-tenders/previews/{preview_id}", headers=_auth_headers()).json()
    assert still_ready["status"] == "ready"

    request = json.dumps({"sheet_name": alternate["sheet_name"], "header_row": alternate["header_row"], "mapping": mapping}, ensure_ascii=False).encode("utf-8")
    mapped = _api_request(main.app, "POST", f"/api/manual-tenders/previews/{preview_id}/mapping", headers={**_auth_headers(), "Content-Type": "application/json"}, body=request)
    assert mapped.status_code == 202, mapped.text
    for _ in range(100):
        preview_response = _api_request(main.app, "GET", f"/api/manual-tenders/previews/{preview_id}", headers=_auth_headers())
        preview = preview_response.json()
        if preview["status"] in {"ready", "failed"}:
            break
        threading.Event().wait(0.02)
    assert preview["status"] == "ready", preview_response.text
    confirmed = _api_request(main.app, "POST", f"/api/manual-tenders/previews/{preview_id}/confirm", headers=_auth_headers())
    assert confirmed.status_code == 201, confirmed.text
    assert confirmed.json()["sheet_name"] == "Второй лист"
    assert confirmed.json()["rows"][0]["name"] == "Выбранная позиция"
    main.job_service.executor.shutdown(wait=True)


def test_upload_returns_normal_busy_response_when_document_processing_lane_is_saturated(tender_api, tmp_path, monkeypatch):
    main, repository = tender_api
    from averon_import.services.jobs import DOCUMENT_PROCESSING, SOURCING
    isolated = JobService(capacities={DOCUMENT_PROCESSING: (1, 0), SOURCING: (1, 2)})
    monkeypatch.setattr(main, "job_service", isolated)
    started = threading.Event()
    release = threading.Event()
    isolated.submit(lambda progress: (started.set(), release.wait(5))[1], lane=DOCUMENT_PROCESSING)
    assert started.wait(2)
    payload = _official(tmp_path / "busy.xlsx")
    boundary, body = _multipart_file("file", "busy.xlsx", payload)
    response = _api_request(main.app, "POST", "/api/manual-tenders/previews", headers={**_auth_headers(), "Content-Type": f"multipart/form-data; boundary={boundary}"}, body=body)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "JOB_LANE_BUSY"
    release.set()
    isolated.executor.shutdown(wait=True)


def test_ui_exposes_excel_path_without_changing_manual_sourcing_and_clears_on_logout():
    root = Path(__file__).resolve().parents[1]
    template = (root / "averon_import" / "templates" / "index.html").read_text(encoding="utf-8")
    script = (root / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'id="open-manual-entry">Ввести позиции вручную' in template
    assert 'id="open-tender-xlsx">Загрузить Excel' in template
    assert 'id="download-tender-template">Скачать шаблон Excel' in template
    assert "async function downloadExcelTenderTemplate()" in script
    assert "async function uploadExcelTender(file)" in script
    assert "async function waitForExcelTenderPreview(generation)" in script
    assert "invalidCount > 0" in script
    assert "Требуют исправления:" in script
    assert "TENDER_INVALID_ROWS" not in script
    assert "function renderExcelTenderRows()" in script
    assert "function clearExcelTenderState()" in script
    assert 'state.manual.rows.filter((row) => row.selected !== false)' in script
    assert "sessionStorage.setItem(EXCEL_TENDER_VIEW_KEY, JSON.stringify({tender_id:tenderId,user_id:userId}))" in script
    assert "sessionStorage.setItem(EXCEL_TENDER_VIEW_KEY, JSON.stringify({rows" not in script
    assert "localStorage.setItem(EXCEL_TENDER_VIEW_KEY" not in script
    assert "const generation = ++state.excelTender.pollGeneration" in script


def test_read_only_excel_workspace_renders_370_rows_with_filtering():
    node = shutil.which("node")
    assert node, "Node.js is required for tender UI lifecycle regression"
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, str(root / "tests" / "js" / "manual_tender_lifecycle.cjs"), str(root / "averon_import" / "static" / "app.js")],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS: 370-row workspace, default tender selection, and generation-safe adaptive sourcing polling" in result.stdout


def _confirm_synthetic_tender(main, repository, tmp_path, *, count=1, official=False):
    path = tmp_path / f"phase-b-{count}-{uuid.uuid4().hex}.xlsx"
    if official:
        payload = _official(path)
    else:
        _official(path, include_required=False)
        workbook = load_workbook(path)
        sheet = workbook[TEMPLATE_SHEET]
        units = ["100 шт", "1000 шт", "10 м", "затрата", "шт", "м", "кг"]
        for index in range(count):
            row_number = index + 2
            sheet.cell(row_number, 1, f"R-{index + 1:04d}")
            sheet.cell(row_number, 2, f"Синтетическая позиция {index + 1}")
            sheet.cell(row_number, 3, units[index % len(units)])
            sheet.cell(row_number, 4, index % 5 + 1)
            sheet.cell(row_number, 5, "")
        workbook.save(path)
        workbook.close()
        payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    preview = repository.reserve_preview("username:tender-user", path.name, len(payload), digest)
    source_path = repository.write_preview_source(preview["preview_id"], payload)
    analysis = main.tender_parser.parse(source_path, tender_id=preview["preview_id"])
    repository.update_preview(
        preview["preview_id"], "username:tender-user", status="ready",
        parser_version=analysis["parser_version"], mapping=analysis["mapping"], analysis=analysis,
    )
    return repository.confirm(preview["preview_id"], "username:tender-user")


def _fake_tender_sourcing_service(monkeypatch, main, *, catalog_version="catalog-v1"):
    from types import SimpleNamespace
    from averon_import.services.sourcing.models import (
        ProjectSourcingResult, SourcingResult, SourcingRouteMetadata, SourcingSourceMode,
    )
    from averon_import.services.sourcing.product_understanding import build_fallback_intent

    class FakeService:
        def __init__(self):
            self.received = []
            self.calls = 0
            self.modes = []

        def provider(self, provider_key=None):
            if provider_key == "invalid-provider":
                raise ValueError("unknown provider")
            return SimpleNamespace(key=provider_key or "fake-provider", label="Fake provider")

        def _project(self, rows, source_mode, progress):
            self.calls += 1
            self.received = [dict(row) for row in rows]
            self.modes.append(SourcingSourceMode(source_mode))
            if progress:
                progress(0, len(rows), "test")
            results = []
            for row in rows:
                route = None
                if source_mode != SourcingSourceMode.PROVIDER_ONLY:
                    route = SourcingRouteMetadata(
                        source_mode=source_mode, final_source_kind="none",
                        history_catalog_version=catalog_version,
                    )
                results.append(SourcingResult(intent=build_fallback_intent(row), route=route))
            if progress:
                progress(len(rows), len(rows), "test")
            return ProjectSourcingResult(
                positions_total=len(rows), positions_processed=len(rows),
                positions_without_offers=len(rows), source_mode=source_mode,
                catalog_version=catalog_version, results=results,
            )

        def search_project(self, rows, *, source_mode=SourcingSourceMode.PROVIDER_ONLY, progress=None, **_kwargs):
            return self._project(rows, SourcingSourceMode.PROVIDER_ONLY, progress)

        def search_project_routed(self, rows, *, source_mode, progress=None, **_kwargs):
            return self._project(rows, source_mode, progress)

    service = FakeService()
    monkeypatch.setattr(main, "sourcing_service", service)
    monkeypatch.setattr(main, "sourcing_runtime", SimpleNamespace(service=service))
    return service


def _post_tender_sourcing(main, tender_id, payload, *, username="tender-user"):
    body = json.dumps(payload).encode("utf-8")
    return _api_request(
        main.app, "POST", f"/api/manual-tenders/{tender_id}/sourcing",
        headers={**_auth_headers(username), "Content-Type": "application/json"}, body=body,
    )


def _wait_tender_job(main, job_id, *, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            job = main.job_service.get_public(job_id, owner_id="tender-user")
        except KeyError:
            job = None
        if job and job["status"] in {"completed", "failed", "expired"}:
            return job
        time.sleep(0.01)
    raise AssertionError(f"tender sourcing job did not finish: {job_id}")


def test_tender_sourcing_is_server_authoritative_durable_and_owner_bound(tender_api, tmp_path, monkeypatch):
    main, repository = tender_api
    workspace = _confirm_synthetic_tender(main, repository, tmp_path, count=370)
    service = _fake_tender_sourcing_service(monkeypatch, main)
    before_sha = hashlib.sha256((repository.workspace_root / workspace["tender_id"] / "source.xlsx").read_bytes()).hexdigest()
    items = [row for row in workspace["rows"] if row["row_type"] == "item"]
    selected_ids = [row["source_row_id"] for row in items]
    assert len(selected_ids) == len(set(selected_ids)) == 370

    response = _post_tender_sourcing(main, workspace["tender_id"], {
        "source_row_ids": selected_ids, "source_mode": "provider_only", "limit": 20,
    })
    assert response.status_code == 202, response.text
    job = _wait_tender_job(main, response.json()["id"])
    assert job["status"] == "completed", json.dumps(job, ensure_ascii=False, default=str)
    assert job["result"]["tender_id"] == workspace["tender_id"]
    assert job["result"]["run_id"]
    assert len(service.received) == 370
    assert all("resource_code" not in row and "code" not in row and "source_text" not in row for row in service.received)
    assert all(row["quantity_trusted"] is True for row in service.received)
    assert {row["unit"] for row in service.received} >= {"100 шт", "1000 шт", "10 м", "затрата"}
    for source, adapted in zip(items, service.received, strict=True):
        assert adapted["source_row_id"] == source["source_row_id"]
        assert adapted["article"] == source["article"]
        assert adapted["quantity"] == source["quantity"]
        assert adapted["unit"] == source["raw_unit"]
    no_article = next(row for row in items if not row["article"] and row["resource_code"])
    adapted = next(row for row in service.received if row["source_row_id"] == no_article["source_row_id"])
    from averon_import.services.sourcing.product_understanding import build_fallback_intent
    assert adapted["article"] == "" and build_fallback_intent(adapted).article == ""

    run_id = job["result"]["run_id"]
    owner_detail = _api_request(main.app, "GET", f"/api/manual-tenders/{workspace['tender_id']}/runs/{run_id}", headers=_auth_headers())
    assert owner_detail.status_code == 200
    run = owner_detail.json()
    assert run["status"] == "completed"
    assert run["source_sha256"] == workspace["source_sha256"]
    assert run["workspace_revision"] == workspace["revision"]
    assert len(run["rows"]) == 370
    assert {item["source_row_id"] for item in run["rows"]} == set(selected_ids)
    foreign_list = _api_request(main.app, "GET", f"/api/manual-tenders/{workspace['tender_id']}/runs", headers=_auth_headers("other-user"))
    foreign_detail = _api_request(main.app, "GET", f"/api/manual-tenders/{workspace['tender_id']}/runs/{run_id}", headers=_auth_headers("other-user"))
    assert foreign_list.status_code == foreign_detail.status_code == 404
    foreign_sourcing = _post_tender_sourcing(main, workspace["tender_id"], {
        "source_row_ids": selected_ids[:1], "source_mode": "provider_only",
    }, username="other-user")
    assert foreign_sourcing.status_code == 404
    listing = _api_request(main.app, "GET", f"/api/manual-tenders/{workspace['tender_id']}/runs", headers=_auth_headers())
    assert listing.status_code == 200 and listing.json()["runs"][0]["run_id"] == run_id
    after_sha = hashlib.sha256((repository.workspace_root / workspace["tender_id"] / "source.xlsx").read_bytes()).hexdigest()
    assert before_sha == after_sha == workspace["source_sha256"]


def test_tender_sourcing_rejects_browser_facts_unknown_duplicate_nonitems_and_oversize(tender_api, tmp_path, monkeypatch):
    main, repository = tender_api
    workspace = _confirm_synthetic_tender(main, repository, tmp_path, count=1)
    service = _fake_tender_sourcing_service(monkeypatch, main)
    item = next(row for row in workspace["rows"] if row["row_type"] == "item")
    section = {"source_row_id":"d"*32,"row_type":"section","excel_row":1}
    workspace_path = repository.workspace_root / workspace["tender_id"]
    metadata = repository._metadata(workspace_path)
    metadata["rows"].append(section)
    repository._atomic_json(workspace_path / "workspace.json", metadata)

    browser_facts = _post_tender_sourcing(main, workspace["tender_id"], {
        "source_row_ids": [item["source_row_id"]], "source_mode": "provider_only",
        "name": "browser override", "quantity": "999", "resource_code": "browser code", "rows": [{"article": "FAKE"}],
    })
    assert browser_facts.status_code == 422
    duplicate = _post_tender_sourcing(main, workspace["tender_id"], {
        "source_row_ids": [item["source_row_id"], item["source_row_id"]], "source_mode": "provider_only",
    })
    assert duplicate.status_code == 422
    unknown = _post_tender_sourcing(main, workspace["tender_id"], {
        "source_row_ids": ["f" * 32], "source_mode": "provider_only",
    })
    assert unknown.status_code == 400 and unknown.json()["detail"]["code"] == "TENDER_SOURCE_ROW_UNKNOWN"
    non_item = _post_tender_sourcing(main, workspace["tender_id"], {
        "source_row_ids": [section["source_row_id"]], "source_mode": "provider_only",
    })
    assert non_item.status_code == 400 and non_item.json()["detail"]["code"] == "TENDER_SOURCE_ROW_NOT_ITEM"
    oversized = _post_tender_sourcing(main, workspace["tender_id"], {
        "source_row_ids": [f"{index:032x}" for index in range(501)], "source_mode": "provider_only",
    })
    assert oversized.status_code == 422
    empty = _post_tender_sourcing(main, workspace["tender_id"], {"source_row_ids":[],"source_mode":"provider_only"})
    malformed_mode = _post_tender_sourcing(main, workspace["tender_id"], {"source_row_ids":[item["source_row_id"]],"source_mode":"history_first"})
    malformed_limit = _post_tender_sourcing(main, workspace["tender_id"], {"source_row_ids":[item["source_row_id"]],"source_mode":"provider_only","limit":0})
    malformed_limit_type = _post_tender_sourcing(main, workspace["tender_id"], {"source_row_ids":[item["source_row_id"]],"source_mode":"provider_only","limit":"20"})
    assert empty.status_code == malformed_mode.status_code == malformed_limit.status_code == malformed_limit_type.status_code == 422
    invalid_provider = _post_tender_sourcing(main, workspace["tender_id"], {"source_row_ids":[item["source_row_id"]],"source_mode":"provider_only","provider":"invalid-provider"})
    assert invalid_provider.status_code == 400
    assert service.calls == 0


def test_tender_sourcing_accepts_500_and_true_article_without_resource_code_leak(tender_api, tmp_path, monkeypatch):
    main, repository = tender_api
    official = _confirm_synthetic_tender(main, repository, tmp_path, official=True)
    service = _fake_tender_sourcing_service(monkeypatch, main)
    source = next(row for row in official["rows"] if row["row_type"] == "item")
    assert source["article"] == "SKU-1" and source["resource_code"] == "R-001"
    accepted = _post_tender_sourcing(main, official["tender_id"], {
        "source_row_ids": [source["source_row_id"]], "source_mode": "provider_only",
    })
    assert accepted.status_code == 202
    assert _wait_tender_job(main, accepted.json()["id"])["status"] == "completed"
    from averon_import.services.sourcing.product_understanding import build_fallback_intent
    intent = build_fallback_intent(service.received[0])
    assert intent.article == "SKU-1"
    assert "R-001" not in intent.source_text

    large = _confirm_synthetic_tender(main, repository, tmp_path, count=500)
    large_items = [row for row in large["rows"] if row["row_type"] == "item"]
    assert len(large_items) == 500
    accepted_large = _post_tender_sourcing(main, large["tender_id"], {
        "source_row_ids": [row["source_row_id"] for row in large_items], "source_mode": "provider_only",
    })
    assert accepted_large.status_code == 202, accepted_large.text
    assert _wait_tender_job(main, accepted_large.json()["id"], timeout=8)["status"] == "completed"
    assert len(service.received) == 500


def test_tender_activity_lease_spans_queue_coalesces_and_releases(tender_api, tmp_path, monkeypatch):
    main, repository = tender_api
    workspace = _confirm_synthetic_tender(main, repository, tmp_path, count=1)
    _fake_tender_sourcing_service(monkeypatch, main)
    started = threading.Event()
    release = threading.Event()
    blocker = main.job_service.submit(lambda progress: (started.set(), release.wait(5))[1], lane="sourcing", owner_id="blocker")
    assert started.wait(2)
    row = next(row for row in workspace["rows"] if row["row_type"] == "item")
    request = {"source_row_ids": [row["source_row_id"]], "source_mode": "provider_only"}
    first = _post_tender_sourcing(main, workspace["tender_id"], request)
    second = _post_tender_sourcing(main, workspace["tender_id"], request)
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert repository.activity.active(workspace["tender_id"])
    tender_sourcing_runs = main.tender_sourcing_runs.list_public(repository.workspace_root / workspace["tender_id"], workspace["tender_id"])
    assert tender_sourcing_runs["runs"] == []
    deleted = _api_request(main.app, "DELETE", f"/api/manual-tenders/{workspace['tender_id']}", headers=_auth_headers())
    assert deleted.status_code == 409
    release.set()
    _wait_tender_job(main, first.json()["id"])
    main.job_service.get_public(blocker.id)
    assert not repository.activity.active(workspace["tender_id"])
    final = _api_request(main.app, "DELETE", f"/api/manual-tenders/{workspace['tender_id']}", headers=_auth_headers())
    assert final.status_code == 200


def test_tender_admission_rejection_releases_lease_without_ghost_run(tender_api, tmp_path, monkeypatch):
    main, repository = tender_api
    workspace = _confirm_synthetic_tender(main, repository, tmp_path, count=1)
    _fake_tender_sourcing_service(monkeypatch, main)
    from averon_import.services.jobs import DOCUMENT_PROCESSING, SOURCING, JobService
    jobs = JobService(capacities={DOCUMENT_PROCESSING:(1,2), SOURCING:(1,0)})
    monkeypatch.setattr(main, "job_service", jobs)
    started = threading.Event()
    release = threading.Event()
    jobs.submit(lambda progress: (started.set(), release.wait(5))[1], lane=SOURCING, owner_id="blocker")
    assert started.wait(2)
    row = next(row for row in workspace["rows"] if row["row_type"] == "item")
    response = _post_tender_sourcing(main, workspace["tender_id"], {"source_row_ids":[row["source_row_id"]],"source_mode":"provider_only"})
    assert response.status_code == 409 and not repository.activity.active(workspace["tender_id"])
    release.set()
    jobs.executor.shutdown(wait=True)


def test_tender_history_modes_hold_captured_history_lease_and_use_existing_router(tender_api, tmp_path, monkeypatch):
    main, _repository = tender_api
    workspace = _confirm_synthetic_tender(main, _repository, tmp_path, count=1)
    service = _fake_tender_sourcing_service(monkeypatch, main, catalog_version="history-v1")
    monkeypatch.setattr(main.one_c_history_repository, "catalog_version", lambda: "history-v1")
    row = next(row for row in workspace["rows"] if row["row_type"] == "item")
    from averon_import.services.jobs import SOURCING
    started = threading.Event()
    release = threading.Event()
    blocker = main.job_service.submit(lambda progress: (started.set(), release.wait(5))[1], lane=SOURCING, owner_id="history-blocker")
    assert started.wait(2)
    for index, mode in enumerate(("one_c_only", "one_c_then_provider")):
        response = _post_tender_sourcing(main, workspace["tender_id"], {"source_row_ids":[row["source_row_id"]],"source_mode":mode})
        assert response.status_code == 202, response.text
        if index == 0:
            assert main.one_c_history_activity.status()["active_sourcing_count"] == 1
            release.set()
        assert _wait_tender_job(main, response.json()["id"])["status"] == "completed"
        assert service.modes[-1].value == mode
        detail = _api_request(main.app, "GET", f"/api/manual-tenders/{workspace['tender_id']}/runs/{_wait_tender_job(main, response.json()['id'])['result']['run_id']}", headers=_auth_headers()).json()
        assert detail["history_catalog_version"] == "history-v1"
        assert detail["rows"][0]["route"]["history_catalog_version"] == "history-v1"
        assert main.one_c_history_activity.status()["active_sourcing_count"] == 0
    response = _post_tender_sourcing(main, workspace["tender_id"], {"source_row_ids":[row["source_row_id"]],"source_mode":"provider_only"})
    assert response.status_code == 202
    _wait_tender_job(main, response.json()["id"])
    assert main.one_c_history_activity.status()["active_sourcing_count"] == 0
    main.job_service.get_public(blocker.id)


def test_tender_runs_recover_running_as_interrupted_are_immutable_and_bounded(tender_api, tmp_path):
    main, repository = tender_api
    workspace = _confirm_synthetic_tender(main, repository, tmp_path, count=1)
    path = repository.workspace_root / workspace["tender_id"]
    source = next(row for row in workspace["rows"] if row["row_type"] == "item")
    running = main.tender_sourcing_runs.create_running(
        path, {"tender_id":workspace["tender_id"],"source_sha256":workspace["source_sha256"],"revision":workspace["revision"]},
        source_mode="provider_only",provider=None,selected_ids=[source["source_row_id"]],history_catalog_version=None,
    )
    complete_before_restart = main.tender_sourcing_runs.create_running(
        path, {"tender_id":workspace["tender_id"],"source_sha256":workspace["source_sha256"],"revision":workspace["revision"]},
        source_mode="provider_only",provider=None,selected_ids=[source["source_row_id"]],history_catalog_version=None,
    )
    main.tender_sourcing_runs.complete(path, complete_before_restart["run_id"],summary={"positions_total":1,"positions_processed":1,"positions_matched":1,"positions_review":0,"positions_without_offers":0},catalog_version="v",history_catalog_version=None,rows=[])
    recovered = TenderSourcingRunStore(repository)
    interrupted = recovered.get_public(path, workspace["tender_id"], running["run_id"])
    assert interrupted["status"] == "interrupted"
    assert "Подбор был прерван перезапуском сервера" in interrupted["failure"]["message"]
    assert recovered.get_public(path, workspace["tender_id"], complete_before_restart["run_id"])["status"] == "completed"
    completed_ids = []
    for _ in range(6):
        run = recovered.create_running(
            path, {"tender_id":workspace["tender_id"],"source_sha256":workspace["source_sha256"],"revision":workspace["revision"]},
            source_mode="provider_only",provider=None,selected_ids=[source["source_row_id"]],history_catalog_version=None,
        )
        completed_ids.append(run["run_id"])
        recovered.complete(path, run["run_id"],summary={"positions_total":1,"positions_processed":1,"positions_matched":0,"positions_review":0,"positions_without_offers":1},catalog_version="v",history_catalog_version=None,rows=[])
    newest = recovered.get_public(path, workspace["tender_id"], completed_ids[-1])
    with pytest.raises(TenderWorkspaceError):
        recovered.complete(path, newest["run_id"],summary={},catalog_version=None,history_catalog_version=None,rows=[])
    files = list((path / "runs").glob("*.json"))
    assert len(files) == 5
    active = recovered.create_running(
        path, {"tender_id":workspace["tender_id"],"source_sha256":workspace["source_sha256"],"revision":workspace["revision"]},
        source_mode="provider_only",provider=None,selected_ids=[source["source_row_id"]],history_catalog_version=None,
    )
    assert recovered.get_public(path, workspace["tender_id"], active["run_id"])["status"] == "running"
    assert len(list((path / "runs").glob("*.json"))) == 6
    oversized = path / "runs" / f"{'e' * 32}.json"
    with pytest.raises(TenderWorkspaceError, match="превышает допустимый объём"):
        recovered._atomic_write(oversized, {"payload":"x" * (1024 * 1024)})


def test_tender_result_correlation_fence_rejects_missing_duplicate_and_unknown_ids():
    source_rows = [{"source_row_id":"a"*32,"excel_row":12},{"source_row_id":"b"*32,"excel_row":13}]
    def result(*ids):
        return {"results":[{"intent":{"source_row_id":row_id}} for row_id in ids]}
    with pytest.raises(TenderWorkspaceError, match="соответствия строк"):
        canonical_tender_projection(result("a"*32),source_rows,["a"*32,"b"*32])
    with pytest.raises(TenderWorkspaceError, match="соответствия строк"):
        canonical_tender_projection(result("a"*32,"a"*32),source_rows,["a"*32,"b"*32])
    with pytest.raises(TenderWorkspaceError, match="соответствия строк"):
        canonical_tender_projection(result("a"*32,"c"*32),source_rows,["a"*32,"b"*32])


def test_tender_runner_failure_releases_lease_and_records_safe_failure(tender_api, tmp_path, monkeypatch):
    main, repository = tender_api
    workspace = _confirm_synthetic_tender(main, repository, tmp_path, count=1)
    service = _fake_tender_sourcing_service(monkeypatch, main)
    def fail_search(*_args, **_kwargs):
        raise RuntimeError("provider secret/token must not be persisted")
    monkeypatch.setattr(service, "search_project", fail_search)
    row = next(row for row in workspace["rows"] if row["row_type"] == "item")
    response = _post_tender_sourcing(main, workspace["tender_id"], {"source_row_ids":[row["source_row_id"]],"source_mode":"provider_only"})
    assert response.status_code == 202
    job = _wait_tender_job(main, response.json()["id"])
    assert job["status"] == "failed"
    assert not repository.activity.active(workspace["tender_id"])
    run = main.tender_sourcing_runs.list_public(repository.workspace_root / workspace["tender_id"], workspace["tender_id"])["runs"][0]
    encoded = json.dumps(run, ensure_ascii=False).casefold()
    assert run["status"] == "failed"
    assert run["failure"]["code"] == "SOURCING_FAILED"
    assert "secret" not in encoded and "token" not in encoded


def test_tender_queue_expiry_has_no_durable_run_and_releases_lease(tender_api, tmp_path, monkeypatch):
    main, repository = tender_api
    workspace = _confirm_synthetic_tender(main, repository, tmp_path, count=1)
    _fake_tender_sourcing_service(monkeypatch, main)
    from averon_import.services.jobs import DOCUMENT_PROCESSING, SOURCING, JobService
    jobs = JobService(capacities={DOCUMENT_PROCESSING:(1,2), SOURCING:(1,2)}, queue_wait_seconds=5)
    monkeypatch.setattr(main, "job_service", jobs)
    started = threading.Event()
    release = threading.Event()
    blocker = jobs.submit(lambda progress: (started.set(), release.wait(5))[1], lane=SOURCING, owner_id="blocker")
    assert started.wait(2)
    row = next(row for row in workspace["rows"] if row["row_type"] == "item")
    response = _post_tender_sourcing(main, workspace["tender_id"], {"source_row_ids":[row["source_row_id"]],"source_mode":"provider_only"})
    assert response.status_code == 202
    queued = jobs.get(response.json()["id"], owner_id="tender-user")
    queued._created_monotonic = time.monotonic() - 6
    job = jobs.get_public(response.json()["id"], owner_id="tender-user")
    assert job["status"] == "expired"
    assert not repository.activity.active(workspace["tender_id"])
    assert main.tender_sourcing_runs.list_public(repository.workspace_root / workspace["tender_id"], workspace["tender_id"])["runs"] == []
    release.set()
    jobs.get_public(blocker.id)
    jobs.executor.shutdown(wait=True)


def test_tender_queue_revalidates_source_and_canonical_rows_before_provider_call(tender_api, tmp_path, monkeypatch):
    main, repository = tender_api
    workspace = _confirm_synthetic_tender(main, repository, tmp_path, count=1)
    service = _fake_tender_sourcing_service(monkeypatch, main)
    from averon_import.services.jobs import SOURCING
    started = threading.Event()
    release = threading.Event()
    blocker = main.job_service.submit(lambda progress: (started.set(), release.wait(5))[1], lane=SOURCING, owner_id="integrity-blocker")
    assert started.wait(2)
    item = next(row for row in workspace["rows"] if row["row_type"] == "item")
    response = _post_tender_sourcing(main, workspace["tender_id"], {"source_row_ids":[item["source_row_id"]],"source_mode":"provider_only"})
    assert response.status_code == 202
    path = repository.workspace_root / workspace["tender_id"]
    metadata = repository._metadata(path)
    metadata["rows"][0]["name"] = "synthetic tamper before execution"
    repository._atomic_json(path / "workspace.json", metadata)
    release.set()
    job = _wait_tender_job(main, response.json()["id"])
    assert job["status"] == "failed"
    assert service.calls == 0
    run = main.tender_sourcing_runs.list_public(path, workspace["tender_id"])["runs"][0]
    detail = main.tender_sourcing_runs.get_public(path, workspace["tender_id"], run["run_id"])
    assert run["status"] == "failed" and detail["source_sha256"] == workspace["source_sha256"]
    main.job_service.get_public(blocker.id)


def test_tender_runtime_is_captured_at_admission_and_job_owners_do_not_coalesce(tender_api, tmp_path, monkeypatch):
    main, _repository = tender_api
    workspace = _confirm_synthetic_tender(main, _repository, tmp_path, count=1)
    admitted_service = _fake_tender_sourcing_service(monkeypatch, main)
    from averon_import.services.jobs import SOURCING, JobService
    started = threading.Event()
    release = threading.Event()
    main.job_service.submit(lambda progress: (started.set(), release.wait(5))[1], lane=SOURCING, owner_id="runtime-blocker")
    assert started.wait(2)
    row = next(row for row in workspace["rows"] if row["row_type"] == "item")
    response = _post_tender_sourcing(main, workspace["tender_id"], {"source_row_ids":[row["source_row_id"]],"source_mode":"provider_only"})
    assert response.status_code == 202
    replacement = _fake_tender_sourcing_service(monkeypatch, main)
    release.set()
    job = _wait_tender_job(main, response.json()["id"])
    assert job["status"] == "completed"
    assert admitted_service.calls == 1 and replacement.calls == 0

    jobs = JobService()
    hold = threading.Event()
    entered = threading.Event()
    first = jobs.submit(lambda progress: (entered.set(), hold.wait(5))[1], lane=SOURCING, owner_id="owner-a", dedupe_key="same")
    assert entered.wait(2)
    second = jobs.submit(lambda progress: "second", lane=SOURCING, owner_id="owner-b", dedupe_key="same")
    assert first.id != second.id
    hold.set()
    jobs.executor.shutdown(wait=True)


def _project_result_with_offer(source_row, *, provider, provenance, source_mode="provider_only"):
    from averon_import.services.sourcing.models import (
        MatchDecision, MatchResult, Offer, ProjectSourcingResult,
        SourcingResult, SourcingRouteMetadata, SourcingSourceMode,
    )
    from averon_import.services.sourcing.product_understanding import build_fallback_intent

    adapted = TenderSourcingRowAdapter.convert(source_row)
    intent = build_fallback_intent(adapted)
    offer = Offer(
        offer_id=f"{provider}:synthetic-offer",
        provider=provider,
        source_item_id="synthetic-source-item",
        title="Synthetic replacement assembly with bounded provider title",
        article="SYN-100",
        manufacturer="Synthetic manufacturer",
        brand="Synthetic brand",
        price=Decimal("1234.5600"),
        currency="RUB",
        price_unit="шт",
        availability=True,
        availability_text="Synthetic stock status",
        data_provenance=provenance,
    )
    mode = SourcingSourceMode(source_mode)
    if mode == SourcingSourceMode.ONE_C_ONLY:
        route = SourcingRouteMetadata(
            source_mode=mode,
            final_source_kind="historical_purchase",
            history_outcome="SAFE_MATCH",
            history_catalog_version="history-snapshot-v9",
            history_selected_event_id=str(provenance.get("selected_event_id") or ""),
            history_purchase_date=str(provenance.get("purchase_date") or ""),
        )
    else:
        route = SourcingRouteMetadata(source_mode=mode, final_source_kind="provider")
    match = MatchResult(offer=offer, decision=MatchDecision.MATCH, rank=1, matched_attributes=["name"])
    item = SourcingResult(intent=intent, recommended_offer=offer, match_results=[match], route=route)
    return ProjectSourcingResult(
        positions_total=1,
        positions_processed=1,
        positions_matched=1,
        source_mode=mode,
        results=[item],
    )


def _persist_project_result(store, repository, workspace, result, *, source_row, source_mode):
    workspace_path = repository.workspace_root / workspace["tender_id"]
    run = store.create_running(
        workspace_path,
        workspace,
        source_mode=source_mode,
        provider=None,
        selected_ids=[source_row["source_row_id"]],
        history_catalog_version="history-snapshot-v9" if source_mode == "one_c_only" else None,
    )
    payload = result.model_dump(mode="json")
    rows = canonical_tender_projection(payload, [source_row], [source_row["source_row_id"]])
    store.complete(
        workspace_path,
        run["run_id"],
        summary={
            "positions_total": 1, "positions_processed": 1, "positions_matched": 1,
            "positions_review": 0, "positions_without_offers": 0,
        },
        catalog_version="synthetic-provider-catalog-v4",
        history_catalog_version="history-snapshot-v9" if source_mode == "one_c_only" else None,
        rows=rows,
    )
    return run["run_id"]


def test_excel_adapter_uses_one_model_field_without_resource_code_leak():
    from averon_import.services.sourcing.product_understanding import build_fallback_intent

    source = {
        "source_row_id": "a" * 32,
        "row_type": "item",
        "name": "Synthetic pump",
        "model": "ABC-100",
        "article": "TRUE-SKU-7",
        "resource_code": "RESOURCE-CODE-9",
        "manufacturer": "Synthetic manufacturer",
        "raw_unit": "шт",
        "quantity": "2",
        "quantity_trusted": True,
    }
    adapted = TenderSourcingRowAdapter.convert(source)
    intent = build_fallback_intent(adapted)

    assert "model" not in adapted
    assert adapted["type_mark"] == "ABC-100"
    assert intent.model == "ABC-100"
    assert intent.source_text.count("ABC-100") == 1
    assert adapted["article"] == intent.article == "TRUE-SKU-7"
    assert all("RESOURCE-CODE-9" not in str(value) for value in adapted.values())
    assert "RESOURCE-CODE-9" not in intent.source_text


def test_tender_price_provenance_is_allowlisted_durable_and_self_contained(tender_api, tmp_path, monkeypatch):
    main, repository = tender_api
    workspace = _confirm_synthetic_tender(main, repository, tmp_path, count=3)
    items = [row for row in workspace["rows"] if row["row_type"] == "item"]
    store = main.tender_sourcing_runs

    etm_result = _project_result_with_offer(
        items[0], provider="etm_ipro", source_mode="provider_only",
        provenance={
            "source": "etm_ipro", "catalog_version": "etm-mirror-2026-10",
            "source_item_id": "etm-item-42", "price_field": "pricewnds",
            "price_status": "available", "token": "private-token-value",
            "authorization": "Bearer private-auth-value", "secret": "private-secret-value",
            "raw_response": {"items": [{"secret": "nested-secret-value"}]},
            "nested": {"anything": "must not persist"},
        },
    )
    etm_run_id = _persist_project_result(
        store, repository, workspace, etm_result, source_row=items[0], source_mode="provider_only",
    )
    del etm_result

    history_result = _project_result_with_offer(
        items[1], provider="one_c_history", source_mode="one_c_only",
        provenance={
            "source": "one_c_history", "source_kind": "historical_purchase",
            "snapshot_version": "history-snapshot-v9", "history_item_id": "history-item-17",
            "selected_event_id": "history-item-17:row-51", "purchase_date": "2026-09-21",
            "price_basis": "gross_per_unit", "effective_unit_price_gross": Decimal("987.654321"),
            "currency_basis": "company_default", "unit_family": "piece",
            "counterparty": "must not persist", "document_type": "must not persist",
            "source_item_code": "must not persist",
        },
    )
    history_run_id = _persist_project_result(
        store, repository, workspace, history_result, source_row=items[1], source_mode="one_c_only",
    )
    del history_result

    lemana_result = _project_result_with_offer(
        items[2], provider="lemana_b2b", source_mode="provider_only",
        provenance={
            "source": "lemana_b2b", "product_item": "lemana-item-8",
            "mirror_revision": "lemana-mirror-v3", "region_id": "region-4",
            "price_basis": "gross_per_unit", "effective_unit_price_gross": "999.99",
            "currency_basis": "verified_gross",
        },
    )
    lemana_run_id = _persist_project_result(
        store, repository, workspace, lemana_result, source_row=items[2], source_mode="provider_only",
    )
    del lemana_result

    # A fresh store instance reads only the completed records; the in-memory
    # project result objects are gone and the short-lived Job record is unused.
    reloaded = TenderSourcingRunStore(repository)
    monkeypatch.setattr(main, "tender_sourcing_runs", reloaded)
    etm_detail = _api_request(
        main.app, "GET", f"/api/manual-tenders/{workspace['tender_id']}/runs/{etm_run_id}",
        headers=_auth_headers(),
    )
    history_detail = _api_request(
        main.app, "GET", f"/api/manual-tenders/{workspace['tender_id']}/runs/{history_run_id}",
        headers=_auth_headers(),
    )
    lemana_detail = _api_request(
        main.app, "GET", f"/api/manual-tenders/{workspace['tender_id']}/runs/{lemana_run_id}",
        headers=_auth_headers(),
    )
    assert etm_detail.status_code == history_detail.status_code == lemana_detail.status_code == 200
    etm_provenance = etm_detail.json()["rows"][0]["price_provenance"]
    assert etm_provenance == {
        "source": "etm_ipro", "catalog_version": "etm-mirror-2026-10",
        "source_item_id": "etm-item-42", "price_field": "pricewnds", "price_status": "available",
    }
    history_provenance = history_detail.json()["rows"][0]["price_provenance"]
    assert history_provenance == {
        "source": "one_c_history", "source_kind": "historical_purchase",
        "snapshot_version": "history-snapshot-v9", "history_item_id": "history-item-17",
        "selected_event_id": "history-item-17:row-51", "purchase_date": "2026-09-21",
        "price_basis": "gross_per_unit", "effective_unit_price_gross": "987.654321",
        "currency_basis": "company_default", "unit_family": "piece",
    }
    route = history_detail.json()["rows"][0]["route"]
    assert route["final_source_kind"] == "historical_purchase"
    assert route["history_outcome"] == "SAFE_MATCH"
    assert route["history_purchase_date"] == "2026-09-21"
    lemana_provenance = lemana_detail.json()["rows"][0]["price_provenance"]
    assert lemana_provenance == {
        "source": "lemana_b2b", "product_item": "lemana-item-8",
        "mirror_revision": "lemana-mirror-v3",
    }
    assert not any("gross" in key or "vat" in key for key in lemana_provenance)
    from averon_import.services.manual_tenders.sourcing import _safe_price_provenance
    bounded = _safe_price_provenance({
        "provider": "etm_ipro",
        "data_provenance": {
            "source": "etm_ipro", "catalog_version": "v" * 200,
            "source_item_id": {"nested": "not a scalar"},
        },
    })
    assert len(bounded["catalog_version"]) == 120
    assert "source_item_id" not in bounded

    run_dir = repository.workspace_root / workspace["tender_id"] / "runs"
    persisted_json = "\n".join(path.read_text(encoding="utf-8") for path in run_dir.glob("*.json"))
    for forbidden in (
        "private-token-value", "private-auth-value", "private-secret-value",
        "raw_response", "nested-secret-value", "must not persist", "region-4",
    ):
        assert forbidden not in persisted_json
    public_json = json.dumps([etm_detail.json(), history_detail.json(), lemana_detail.json()], ensure_ascii=False)
    for forbidden in ("token", "authorization", "raw_response", "counterparty", "document_type", "verified_gross"):
        assert forbidden not in public_json.casefold()
    assert all(
        "data_provenance" not in row
        for detail in (etm_detail, history_detail, lemana_detail)
        for row in detail.json()["rows"]
    )


def test_realistic_370_row_tender_run_fits_existing_one_mib_bound(tender_api, tmp_path):
    main, repository = tender_api
    workspace = _confirm_synthetic_tender(main, repository, tmp_path, count=370)
    source_rows = [row for row in workspace["rows"] if row["row_type"] == "item"]
    selected_ids = [row["source_row_id"] for row in source_rows]
    results = []
    for index, row in enumerate(source_rows, start=1):
        offer_id = f"etm_ipro:synthetic-{index}"
        results.append({
            "intent": {
                "source_row_id": row["source_row_id"],
                "normalized_name": f"Industrial centrifugal pump assembly {index:03d} for process-water service",
                "product_class": "pump equipment",
                "manufacturer": "Synthetic Industrial Equipment Works",
                "brand": "Synthetic Works",
                "model": f"SP-{index:03d}-A",
                "article": f"SYN-ARTICLE-{index:04d}",
                "quantity": str(row.get("quantity") or "2"),
                "unit": str(row.get("raw_unit") or "шт"),
            },
            "recommended_offer": {
                "offer_id": offer_id, "provider": "etm_ipro",
                "source_item_id": f"ETM-SYN-{index:06d}",
                "title": f"Synthetic industrial pump, model SP-{index:03d}-A, cast housing, standard motor",
                "article": f"SYN-ARTICLE-{index:04d}",
                "manufacturer": "Synthetic Industrial Equipment Works",
                "brand": "Synthetic Works", "price": "123456.78", "currency": "RUB",
                "price_unit": "шт", "retrieved_at": "2026-10-01T12:00:00+00:00",
                "availability": True, "availability_text": "In stock; synthetic test data",
                "data_provenance": {
                    "source": "etm_ipro", "catalog_version": "synthetic-etm-catalog-v2026-10",
                    "source_item_id": f"ETM-SYN-{index:06d}",
                    "price_field": "pricewnds", "price_status": "available",
                    "raw_response": {"excluded": "never persisted"},
                },
            },
            "match_results": [{
                "offer": {"offer_id": offer_id}, "decision": "MATCH", "rank": 1,
                "matched_attributes": ["product_class", "model", "manufacturer"],
                "supporting_attributes": ["article"],
                "conflicting_attributes": [], "missing_attributes": [],
            }],
            "route": {
                "source_mode": "provider_only", "final_source_kind": "provider",
                "fallback_status": "not_called", "history_outcome": "NO_MATCH",
                "history_catalog_version": "", "history_selected_event_id": "",
                "history_purchase_date": "", "history_age_days": None,
                "fallback_called": False, "fallback_provider_key": "",
                "fallback_catalog_version": "", "routing_policy_revision": "one-c-routing-v2",
            },
            "notices": [],
        })

    rows = canonical_tender_projection(
        {"results": results}, source_rows, selected_ids,
    )
    workspace_path = repository.workspace_root / workspace["tender_id"]
    store = main.tender_sourcing_runs
    running = store.create_running(
        workspace_path, workspace, source_mode="provider_only", provider=None,
        selected_ids=selected_ids, history_catalog_version=None,
    )
    store.complete(
        workspace_path, running["run_id"],
        summary={
            "positions_total": 370, "positions_processed": 370, "positions_matched": 370,
            "positions_review": 0, "positions_without_offers": 0,
        },
        catalog_version="synthetic-etm-catalog-v2026-10",
        history_catalog_version=None,
        rows=rows,
    )
    run_path = workspace_path / "runs" / f"{running['run_id']}.json"
    assert len(rows) == 370
    assert run_path.stat().st_size < MAX_TENDER_RUN_BYTES
    assert MAX_TENDER_RUN_BYTES == 1024 * 1024


def test_tender_ui_uses_id_only_request_default_selection_runs_and_shared_result_renderer():
    root = Path(__file__).resolve().parents[1]
    template = (root / "averon_import" / "templates" / "index.html").read_text(encoding="utf-8")
    script = (root / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'id="tender-sourcing-button">Подобрать предложения' in template
    assert "selectedIds = new Set((workspace.rows || []).filter((row) => row.row_type === \"item\").map((row) => row.source_row_id))" in script
    assert 'body:JSON.stringify({source_row_ids:selectedIds,source_mode:mode,limit:20})' in script
    assert 'body:JSON.stringify({source_row_ids:selectedIds,source_mode:mode,limit:20})' in script
    assert "renderSourcingResult(job.result)" in script
    assert 'api(`/api/manual-tenders/${encodeURIComponent(tenderId)}/runs`)' in script
    assert "state.excelTender.pollGeneration === generation" in script
    assert "Подбор был прерван перезапуском сервера. Запустите его повторно." in script
    assert "Структура шаблона распознана." in script
    assert "Ошибок структуры нет." not in script
    assert "state.excelTender.sourcingActive = true" in script
    assert "state.excelTender.pollGeneration === generation" in script
