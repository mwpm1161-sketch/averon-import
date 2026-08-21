from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from averon_import.api.dependencies import AppServices, get_services
from averon_import.core.schemas import ExportRequest

router = APIRouter(prefix="/api/documents", tags=["export"])


def safe_filename(value: str) -> str:
    value = Path(value or "averon_import.xlsx").name
    value = re.sub(r"[^\w\-. ()А-Яа-яЁё]", "_", value)
    if not value.lower().endswith(".xlsx"):
        value += ".xlsx"
    return value[:160]


@router.post("/{document_id}/export")
def export_document(
    document_id: str,
    request: ExportRequest,
    services: AppServices = Depends(get_services),
):
    try:
        workspace = services.workspace.get(document_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc

    filename = safe_filename(request.filename)
    output = workspace.exports_dir / filename
    rows = [row.model_dump(mode="json") for row in request.rows]
    try:
        services.export.export(
            rows=rows,
            columns=request.columns,
            output_path=output,
            sheet_name=request.sheet_name,
            include_headers=request.include_headers,
            only_exportable=request.only_exportable,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return FileResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )
