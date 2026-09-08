from __future__ import annotations

from averon_import.services.ocr.semantics import (
    HeaderCellNormalizer,
    HeaderSourceCell,
    map_semantic_header,
)


CLASSIC_HEADERS = (
    "Позиция",
    "Наименование и техническая характеристика",
    "Тип, марка, обозначение\nдокумента, опросного\nлиста",
    "Код\nоборудования,\nматериала\nизделия",
    "Завод-\nизготовитель",
    "Единица\nизмерения",
    "Кол-во",
    "Масса\nединицы,\nкг.",
    "Примечание",
)

MULTILINE_HEADERS = (
    "Позиция",
    "Наименование и техническая характеристика",
    "Тип, марка,\nобозначение документа,\nопросного листа",
    "Код обору-\nдования,\nизделия,\nматериала",
    "Завод-\nизготовитель\n(поставщик)",
    "Еди-\nница\nизме-\nрения",
    "Коли-\nчество",
    "Масса единицы,\nкг.\nПримечания",
)


def _cells(*rows: tuple[str, ...]) -> tuple[HeaderSourceCell, ...]:
    return tuple(
        HeaderSourceCell(
            physical_row=row_index,
            physical_column=column_index,
            bbox={"left": column_index, "top": row_index},
            raw_text=text,
            provenance=({"source": "sanitized_fixture"},),
        )
        for row_index, values in enumerate(rows)
        for column_index, text in enumerate(values)
    )


def _mapped_fields(result) -> dict[int, tuple[str, ...]]:
    return dict(result.mapping)


def test_wrapped_classic_header_maps_all_columns_with_evidence():
    result = map_semantic_header(_cells(CLASSIC_HEADERS), len(CLASSIC_HEADERS))

    assert result.status == "trusted"
    assert _mapped_fields(result) == {
        0: ("position",),
        1: ("name",),
        2: ("type_mark",),
        3: ("code",),
        4: ("manufacturer",),
        5: ("unit",),
        6: ("quantity",),
        7: ("mass",),
        8: ("note",),
    }
    assert result.assignment_margin >= 0.12


def test_multiline_header_maps_conservative_mass_note_composite():
    result = map_semantic_header(_cells(MULTILINE_HEADERS), len(MULTILINE_HEADERS))

    assert result.status == "trusted"
    assert result.mapping[5] == ("unit",)
    assert result.mapping[6] == ("quantity",)
    assert result.mapping[7] == ("mass", "note")


def test_normalizer_preserves_raw_text_and_dehyphenates_visual_lines():
    raw = "Коли-\nчество"
    normalized = HeaderCellNormalizer().normalize(raw)

    assert normalized.raw_text == raw
    assert normalized.visual_lines == ("Коли-", "чество")
    assert normalized.dehyphenated_text == "Количество"
    assert normalized.normalized_text == "количество"

    result = map_semantic_header(_cells(MULTILINE_HEADERS), len(MULTILINE_HEADERS))
    quantity_cell = next(cell for cell in result.header_cells if cell.physical_column == 6)
    quantity = next(candidate for candidate in quantity_cell.semantic_candidates if candidate.field == "quantity")
    assert ("dehyphenated_phrase", 0.95) in quantity.score_components
    assert "line_ending_dehyphenation" in quantity.evidence


def test_header_fragments_map_quantity_unit_mass_type_and_code():
    result = map_semantic_header(_cells(MULTILINE_HEADERS), len(MULTILINE_HEADERS))

    assert result.mapping[6] == ("quantity",)
    assert result.mapping[5] == ("unit",)
    assert result.mapping[7] == ("mass", "note")
    assert result.mapping[2] == ("type_mark",)
    assert result.mapping[3] == ("code",)


def test_bounded_header_ocr_variation_is_scored_and_explained():
    headers = list(CLASSIC_HEADERS)
    headers[6] = "Колнчество"
    result = map_semantic_header(_cells(tuple(headers)), len(headers))

    assert result.status == "trusted"
    quantity = next(
        candidate
        for candidate in result.candidates_by_column[6]
        if candidate.field == "quantity"
    )
    assert quantity.score == 0.72
    assert any(item.startswith("bounded_ocr_variation:") for item in quantity.evidence)


def test_product_body_line_mixed_into_header_is_ambiguous():
    headers = list(CLASSIC_HEADERS)
    headers[1] = "Наименование,\nНасос"
    result = map_semantic_header(_cells(tuple(headers)), len(headers))

    assert result.status == "ambiguous"
    assert "header_body_conflict" in result.reasons
    assert any("unmatched_text_line" in item for item in result.candidate_regions[0].body_evidence)


def test_first_product_row_is_not_consumed_as_header():
    body = ("1", "Насос НЦ-50", "N2", "C2", "Завод", "шт.", "1", "42", "")
    result = map_semantic_header(
        _cells(CLASSIC_HEADERS, body), len(CLASSIC_HEADERS)
    )

    assert result.status == "trusted"
    assert result.header_rows == (0,)


def test_product_exclusive_first_body_row_is_not_consumed_as_header():
    body = ("1", "Оборудование вентиляции", "N2", "C2", "Завод", "шт.", "1", "", "")
    result = map_semantic_header(
        _cells(CLASSIC_HEADERS, body), len(CLASSIC_HEADERS)
    )

    assert result.status == "trusted"
    assert result.header_rows == (0,)
    assert any(
        "product_value_line" in reason
        for region in result.candidate_regions
        for reason in region.body_evidence
    )


def test_duplicate_semantic_columns_fail_on_global_assignment_margin():
    headers = list(CLASSIC_HEADERS)
    headers[7] = "Количество"
    result = map_semantic_header(_cells(tuple(headers)), len(headers))

    assert result.status == "ambiguous"
    assert "assignment_margin_too_low" in result.reasons


def test_missing_optional_columns_is_valid_with_core_and_family_evidence():
    headers = (
        "Позиция",
        "Наименование",
        "Код оборудования",
        "Единица измерения",
        "Количество",
    )
    result = map_semantic_header(_cells(headers), len(headers))

    assert result.status == "trusted"
    assert {field for fields in result.mapping.values() for field in fields} == {
        "position", "name", "code", "unit", "quantity"
    }


def test_unrelated_four_column_list_is_not_accepted_as_specification_header():
    headers = ("Описание", "Размер", "Цена", "Сумма")
    result = map_semantic_header(_cells(headers), len(headers))

    assert result.status in {"unavailable", "ambiguous"}
    assert result.trusted is False


def test_header_diagnostics_keep_provenance_and_explain_quantity_mapping():
    result = map_semantic_header(_cells(CLASSIC_HEADERS), len(CLASSIC_HEADERS))
    diagnostics = result.as_dict()

    assert diagnostics["mapping_status"] == "trusted"
    assert diagnostics["selected_mapping"]["6"] == ["quantity"]
    quantity_cell = next(cell for cell in diagnostics["header_cells"] if cell["column"] == 6)
    assert quantity_cell["raw_text"] == "Кол-во"
    assert quantity_cell["normalized_text"] == "кол во"
    assert quantity_cell["provenance"] == [{"source": "sanitized_fixture"}]
