from __future__ import annotations

from pathlib import Path

import fitz


class PdfService:
    def inspect(self, pdf_path: Path) -> dict:
        with fitz.open(pdf_path) as document:
            return {
                "page_count": document.page_count,
                "title": document.metadata.get("title") or pdf_path.stem,
            }

    def render_page(self, pdf_path: Path, page_number: int, dpi: int = 110) -> bytes:
        with fitz.open(pdf_path) as document:
            if page_number < 1 or page_number > document.page_count:
                raise IndexError(page_number)
            page = document[page_number - 1]
            pixmap = page.get_pixmap(dpi=dpi, alpha=False)
            return pixmap.tobytes("png")

    def render_page_to_path(
        self, pdf_path: Path, page_number: int, output_path: Path, dpi: int = 220
    ) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with fitz.open(pdf_path) as document:
            page = document[page_number - 1]
            pixmap = page.get_pixmap(dpi=dpi, alpha=False)
            pixmap.save(output_path)
        return output_path
