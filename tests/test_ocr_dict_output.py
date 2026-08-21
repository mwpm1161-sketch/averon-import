from __future__ import annotations

import os

import numpy as np
from pytesseract import Output

from averon_import.services.ocr_engine import TesseractOcrEngine
from averon_import.services.table_detector import GostSpecificationDetector


def _ocr_dict(text: str = "Тест") -> dict[str, list]:
    return {
        "level": [5], "page_num": [1], "block_num": [1], "par_num": [1],
        "line_num": [1], "word_num": [1], "left": [2], "top": [2],
        "width": [8], "height": [8], "conf": [92.5], "text": [text],
    }


def test_page_ocr_uses_dict_output(monkeypatch):
    def fake_image_to_data(*args, **kwargs):
        assert kwargs["output_type"] == Output.DICT
        return _ocr_dict()

    monkeypatch.setattr("pytesseract.image_to_data", fake_image_to_data)
    engine = TesseractOcrEngine(GostSpecificationDetector())
    result = engine._extract_words(
        np.full((20, 20), 255, dtype=np.uint8),
        "rus", [0, 20], [0, 20], 0, 0, 0,
        extra_config="", source="test",
    )
    assert result[(0, 0)][0][4] == "Тест"


def test_cell_ocr_uses_dict_output(monkeypatch):
    def fake_image_to_data(*args, **kwargs):
        assert kwargs["output_type"] == Output.DICT
        return _ocr_dict("12")

    monkeypatch.setattr("pytesseract.image_to_data", fake_image_to_data)
    engine = TesseractOcrEngine(GostSpecificationDetector())
    result = engine._recognize_cell(
        np.full((30, 30), 255, dtype=np.uint8),
        0, 0, 30, 30, "quantity", mode="standard",
    )
    assert result.text == "12"
    assert result.confidence == 92.5


def test_accurate_model_detection(monkeypatch, tmp_path):
    (tmp_path / "rus.traineddata").write_bytes(b"model")
    (tmp_path / "eng.traineddata").write_bytes(b"model")
    monkeypatch.setenv("AVERON_BEST_TESSDATA_DIR", str(tmp_path))
    assert TesseractOcrEngine.accurate_models_available()
    assert TesseractOcrEngine(GostSpecificationDetector())._base_config("accurate") == "--oem 1"


def test_accurate_environment_uses_unquoted_path(monkeypatch, tmp_path):
    model_dir = tmp_path / "Models with spaces" / "tessdata_best"
    model_dir.mkdir(parents=True)
    monkeypatch.setenv("AVERON_BEST_TESSDATA_DIR", str(model_dir))
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)
    engine = TesseractOcrEngine(GostSpecificationDetector())
    with engine._tessdata_environment("accurate"):
        assert os.environ["TESSDATA_PREFIX"] == str(model_dir.resolve())
        assert '"' not in os.environ["TESSDATA_PREFIX"]
    assert "TESSDATA_PREFIX" not in os.environ
