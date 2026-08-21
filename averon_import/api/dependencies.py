from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from averon_import.ai.service import AiService
from averon_import.services.export_service import ExcelExportService
from averon_import.services.jobs import JobService
from averon_import.services.pdf_service import PdfService
from averon_import.services.recognition import RecognitionService
from averon_import.services.workspace import WorkspaceService


@dataclass(slots=True)
class AppServices:
    pdf: PdfService
    workspace: WorkspaceService
    recognition: RecognitionService
    export: ExcelExportService
    jobs: JobService
    ai: AiService


def get_services(request: Request) -> AppServices:
    return request.app.state.services
