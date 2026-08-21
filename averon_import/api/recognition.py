from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from averon_import.api.dependencies import AppServices, get_services
from averon_import.core.schemas import RecognitionRequest, SaveRowsRequest

router = APIRouter(prefix="/api", tags=["recognition"])


@router.post("/documents/{document_id}/suggest-pages")
def suggest_pages(document_id: str, services: AppServices = Depends(get_services)):
    try:
        workspace = services.workspace.get(document_id)
        metadata = services.workspace.read_json(workspace.metadata_path)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc

    def run(progress):
        return services.recognition.suggest_pages(
            workspace.pdf_path, workspace.pages_dir, metadata["page_count"], progress
        )

    return services.jobs.submit(run).public()


@router.post("/documents/{document_id}/recognize")
def recognize(
    document_id: str,
    request: RecognitionRequest,
    services: AppServices = Depends(get_services),
):
    try:
        workspace = services.workspace.get(document_id)
        metadata = services.workspace.read_json(workspace.metadata_path)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc

    pages = sorted(set(request.pages))
    if not pages:
        raise HTTPException(400, "Не выбраны страницы")
    invalid = [page for page in pages if page < 1 or page > metadata["page_count"]]
    if invalid:
        raise HTTPException(400, f"Некорректные страницы: {invalid}")
    crop = request.crop.model_dump() if request.crop else None

    def run(progress):
        result = services.recognition.recognize(
            workspace.pdf_path,
            workspace.pages_dir,
            pages,
            crop,
            request.dpi,
            progress,
            ocr_mode=request.ocr_mode,
        )
        services.workspace.write_result(workspace, result)
        return result

    return services.jobs.submit(run).public()


@router.get("/jobs/{job_id}")
def get_job(job_id: str, services: AppServices = Depends(get_services)):
    try:
        return services.jobs.get(job_id).public()
    except KeyError as exc:
        raise HTTPException(404, "Задание не найдено") from exc


@router.get("/documents/{document_id}/results")
def get_results(document_id: str, services: AppServices = Depends(get_services)):
    try:
        workspace = services.workspace.get(document_id)
        result = services.workspace.read_result(workspace)
        if not result:
            raise HTTPException(404, "Результат распознавания отсутствует")
        return result
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc


@router.put("/documents/{document_id}/results")
def save_results(
    document_id: str,
    request: SaveRowsRequest,
    services: AppServices = Depends(get_services),
):
    try:
        workspace = services.workspace.get(document_id)
        existing = services.workspace.read_result(workspace) or {}
        rows = [row.model_dump(mode="json") for row in request.rows]
        existing["rows"] = rows
        existing["summary"] = services.recognition._summary(
            rows, existing.get("errors", [])
        )
        services.workspace.write_result(workspace, existing)
        return {"saved": True, "summary": existing["summary"]}
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc
