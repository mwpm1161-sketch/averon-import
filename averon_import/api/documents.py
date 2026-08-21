from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from averon_import.api.dependencies import AppServices, get_services

router = APIRouter(prefix="/api/documents", tags=["documents"])
MAX_PDF_BYTES = 250 * 1024 * 1024


@router.post("")
async def upload_document(
    file: UploadFile = File(...),
    services: AppServices = Depends(get_services),
):
    filename = file.filename or "document.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Поддерживаются только PDF-файлы")

    total = 0
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temporary:
            temp_path = Path(temporary.name)
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_PDF_BYTES:
                    raise HTTPException(413, "Размер PDF превышает 250 МБ")
                temporary.write(chunk)

        inspection = services.pdf.inspect(temp_path)
        workspace = services.workspace.create(
            temp_path,
            {
                "filename": filename,
                "page_count": inspection["page_count"],
                "title": (
                    Path(filename).stem
                    if inspection["title"] == temp_path.stem
                    else inspection["title"]
                ),
                "size": total,
            },
        )
        return services.workspace.read_json(workspace.metadata_path)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, f"Не удалось открыть PDF: {exc}") from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


@router.get("/{document_id}")
def get_document(document_id: str, services: AppServices = Depends(get_services)):
    try:
        workspace = services.workspace.get(document_id)
        metadata = services.workspace.read_json(workspace.metadata_path)
        result = services.workspace.read_result(workspace)
        return {**metadata, "has_result": bool(result)}
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc


@router.get("/{document_id}/page/{page_number}")
def page_image(
    document_id: str,
    page_number: int,
    dpi: int = 110,
    services: AppServices = Depends(get_services),
):
    try:
        workspace = services.workspace.get(document_id)
        metadata = services.workspace.read_json(workspace.metadata_path)
        if page_number < 1 or page_number > metadata["page_count"]:
            raise HTTPException(404, "Страница не найдена")
        dpi = max(72, min(dpi, 300))
        cache = workspace.pages_dir / f"page-{page_number}-{dpi}.png"
        if not cache.exists():
            services.pdf.render_page_to_path(workspace.pdf_path, page_number, cache, dpi=dpi)
        return FileResponse(cache, media_type="image/png")
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc
