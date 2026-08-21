from averon_import.core.normalizers import as_excel_number, normalize_cell


def test_dimensions_are_normalized():
    assert "1000×700" in normalize_cell("name", "Воздуховод 1000 x 700")


def test_units_are_normalized():
    assert normalize_cell("unit", "шт") == "шт."
    assert normalize_cell("unit", "м2") == "м²"
    assert normalize_cell("unit", "- КОМПЛ..") == "компл."
    assert normalize_cell("unit", "wm.") == "шт."


def test_numbers_are_excel_ready():
    assert as_excel_number("12,5") == 12.5
    assert as_excel_number("18") == 18


def test_engineering_ocr_errors_are_corrected():
    text = normalize_cell("name", "Воздухобвод оцинкованноц стали, троцник круглыц")
    assert "Воздуховод" in text
    assert "оцинкованной" in text
    assert "тройник" in text
    assert "круглый" in text


def test_mixed_gost_and_diameter_are_normalized():
    assert "ГОСТ" in normalize_cell("type_mark", "roct 14918-2020")
    assert "Ø315" in normalize_cell("type_mark", "$315")


def test_common_cyrillic_model_codes_are_normalized():
    assert normalize_cell("type_mark", "APH 1100x800").startswith("АРН 1100×800")
    assert normalize_cell("type_mark", "KBK 355") == "КВК 355"
    assert normalize_cell("type_mark", "6APC1500") == "6АРС1500"
