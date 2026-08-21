import os
from pathlib import Path

import cv2
import pytest

from averon_import.services.pdf_service import PdfService
from averon_import.services.table_detector import GostSpecificationDetector


@pytest.mark.integration
def test_current_gost_specification(tmp_path):
    source = os.environ.get("AVERON_SAMPLE_PDF")
    if not source or not Path(source).exists():
        pytest.skip("AVERON_SAMPLE_PDF is not configured")
    image_path = tmp_path / "page-19.png"
    PdfService().render_page_to_path(Path(source), 19, image_path, dpi=120)
    table = GostSpecificationDetector().detect(cv2.imread(str(image_path)))
    assert table.column_count == 9
    assert table.row_count >= 20

    cropped = GostSpecificationDetector().detect(
        cv2.imread(str(image_path)),
        crop={"x": 0.02, "y": 0.01, "width": 0.96, "height": 0.88},
    )
    assert cropped.column_count == 9
    assert cropped.row_count >= 20
