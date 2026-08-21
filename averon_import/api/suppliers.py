from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from averon_import.api.dependencies import AppServices, get_services

router = APIRouter(prefix="/api", tags=["suppliers"])


class SupplierSearchRequest(BaseModel):
    supplier_ids: list[str] = Field(default_factory=list)


@router.get("/suppliers")
def list_suppliers(services: AppServices = Depends(get_services)):
    return {"suppliers": services.suppliers.registry.ids()}


@router.post("/documents/{document_id}/rows/{row_id}/supplier-search")
def search_suppliers_for_row(
    document_id: str,
    row_id: str,
    request: SupplierSearchRequest,
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

    try:
        matches = services.suppliers.search_row(row, request.supplier_ids or None)
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc

    return {
        "row_id": row_id,
        "matches": [match.model_dump(mode="json") for match in matches],
    }
