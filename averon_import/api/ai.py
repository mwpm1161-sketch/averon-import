from __future__ import annotations

import cv2
from fastapi import APIRouter, Depends, HTTPException

from averon_import.ai.service import AiUnavailableError
from averon_import.api.dependencies import AppServices, get_services

router = APIRouter(prefix="/api", tags=["ai"])


@router.post("/documents/{document_id}/rows/{row_id}/ai-review")
def ai_review_row(
    document_id: str,
    row_id: str,
    services: AppServices = Depends(get_services),
):
    try:
        workspace = services.workspace.get(document_id)
        result = services.workspace.read_result(workspace) or {}
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc

    row = next((item for item in result.get("rows", []) if item.get("id") == row_id), None)
    if row is None:
        raise HTTPException(404, "Строка не найдена")

    image_bytes = None
    bbox = row.get("bbox") or {}
    page = int(row.get("page", 0) or 0)
    if page > 0 and bbox:
        image_path = workspace.pages_dir / f"page-{page}-220.png"
        if not image_path.exists():
            services.pdf.render_page_to_path(workspace.pdf_path, page, image_path, dpi=220)
        image = cv2.imread(str(image_path))
        if image is not None:
            height, width = image.shape[:2]
            pad_x = max(8, int(width * 0.005))
            pad_y = max(6, int(height * 0.003))
            x1 = max(0, int(float(bbox.get("x", 0)) * width) - pad_x)
            y1 = max(0, int(float(bbox.get("y", 0)) * height) - pad_y)
            x2 = min(width, int((float(bbox.get("x", 0)) + float(bbox.get("width", 0))) * width) + pad_x)
            y2 = min(height, int((float(bbox.get("y", 0)) + float(bbox.get("height", 0))) * height) + pad_y)
            crop = image[y1:y2, x1:x2]
            if crop.size:
                ok, encoded = cv2.imencode(".png", crop)
                if ok:
                    image_bytes = encoded.tobytes()

    try:
        suggestion = services.ai.review_row(row, image_bytes=image_bytes)
    except AiUnavailableError as exc:
        raise HTTPException(503, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc

    return suggestion.model_dump(mode="json")
