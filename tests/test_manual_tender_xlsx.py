from __future__ import annotations

import hashlib
import json
import math
import threading
import asyncio
import re
import subprocess
import shutil
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from averon_import.services.manual_tenders import TenderActivityRegistry, TenderTemplateService, TenderWorkbookParser, TenderWorkspaceRepository
from averon_import.services.jobs import JobService
from averon_import.services.manual_tenders.parser import (
    MAX_ACTUAL_ITEMS,
    MAX_UNCOMPRESSED_BYTES,
    MAX_UPLOAD_BYTES,
    TenderParseError,
    parse_unit_basis,
    preflight_tender_xlsx,
)
from averon_import.services.manual_tenders.repository import (
    ABSOLUTE_TTL_SECONDS,
    IDLE_TTL_SECONDS,
    MAX_WORKSPACES_GLOBAL,
    MAX_WORKSPACES_PER_USER,
    PREVIEW_TTL_SECONDS,
    TenderWorkspaceError,
)
from averon_import.services.manual_tenders.template import PREPARED_ROWS, TEMPLATE_HEADERS, TEMPLATE_SHEET, TEMPLATE_TABLE


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
    assert sheet.freeze_panes == "A2"
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
    assert "PASS: 370-row read-only tender workspace" in result.stdout
