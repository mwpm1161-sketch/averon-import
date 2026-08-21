from averon_import.ocr.tesseract_provider import EvidenceTesseractOcrProvider
from averon_import.services.table_detector import GostSpecificationDetector


def test_cell_result_keeps_raw_text_before_normalization():
    engine = EvidenceTesseractOcrProvider(GostSpecificationDetector())
    result = engine._cell_from_tokens(
        [(1, 1, 1, 1, "wm.", 92.0)], "unit", "test"
    )
    assert result.raw_text == "wm."
    assert result.text == "шт."
    assert result.confidence == 92.0
