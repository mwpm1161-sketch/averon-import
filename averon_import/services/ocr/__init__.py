"""OCR provider package: neutral contract plus registered adapters."""

from averon_import.services.ocr.base import (
    OcrProvider,
    OcrProviderError,
    OcrResult,
    OcrRow,
    PageOcrResult,
    ProgressCallback,
)
from averon_import.services.ocr.reconstruction import (
    reconstruct_page_rows,
    reconstruct_page_rows_result,
)
from averon_import.services.ocr.semantics.semantic_projection import (
    StructuredReconstructionResult,
    project_semantic_table,
)
from averon_import.services.ocr.physical_grid import (
    AmbiguousWord,
    PhysicalGrid,
    PhysicalGridCell,
    PhysicalGridDetection,
    PhysicalGridDetector,
    validate_physical_grid,
)
from averon_import.services.ocr.raster_grid import RasterRuledTableGridDetector
from averon_import.services.ocr.page_scene import (
    LocalGridHypothesis,
    PageSceneDetector,
    PageSceneIR,
    RasterRegionProposalSource,
    TableRegionCandidate,
    VectorTableRegionDetector,
    build_page_scene,
)
from averon_import.services.ocr.tesseract_adapter import TesseractOcrAdapter
from averon_import.services.ocr.yandex_vision import YandexVisionProvider
from averon_import.services.ocr.page_contract import PageExtractionStatus
from averon_import.services.ocr.page_disposition import (
    CONFIRMED_NON_SPEC,
    POSSIBLE_SPEC_UNRESOLVED,
    SPEC_OUTPUT,
    PageDispositionDecision,
    page_disposition_from_scene,
)
from averon_import.services.ocr.page_scene_arbiter import (
    PageSceneRegionArbiter,
    RegionArbitrationResult,
)
from averon_import.services.ocr.table_ir import (
    PhysicalCellIR,
    PhysicalCellRef,
    PhysicalRowFragmentIR,
    PhysicalRowIR,
    PhysicalRowRef,
    PhysicalTableIR,
    PhysicalTableRef,
    PhysicalWordIR,
    PhysicalWordRef,
)

_PROVIDER_FACTORIES: dict[str, type] = {"tesseract": TesseractOcrAdapter}


def register_provider(key: str, factory: type) -> None:
    _PROVIDER_FACTORIES[key] = factory


def create_ocr_provider(key: str, *args, **kwargs):
    try:
        factory = _PROVIDER_FACTORIES[key]
    except KeyError:
        raise ValueError(f"Неизвестный OCR-провайдер: {key}") from None
    return factory(*args, **kwargs)


__all__ = [
    "OcrProvider",
    "OcrProviderError",
    "OcrResult",
    "OcrRow",
    "PageOcrResult",
    "ProgressCallback",
    "AmbiguousWord",
    "PhysicalGrid",
    "PhysicalGridCell",
    "PhysicalGridDetection",
    "PhysicalGridDetector",
    "RasterRuledTableGridDetector",
    "LocalGridHypothesis",
    "PageSceneDetector",
    "PageSceneIR",
    "RasterRegionProposalSource",
    "TableRegionCandidate",
    "VectorTableRegionDetector",
    "build_page_scene",
    "TesseractOcrAdapter",
    "YandexVisionProvider",
    "reconstruct_page_rows",
    "reconstruct_page_rows_result",
    "StructuredReconstructionResult",
    "project_semantic_table",
    "validate_physical_grid",
    "register_provider",
    "create_ocr_provider",
    "PageExtractionStatus",
    "PageDispositionDecision",
    "PageSceneRegionArbiter",
    "RegionArbitrationResult",
    "SPEC_OUTPUT",
    "CONFIRMED_NON_SPEC",
    "POSSIBLE_SPEC_UNRESOLVED",
    "page_disposition_from_scene",
    "PhysicalCellIR",
    "PhysicalCellRef",
    "PhysicalRowFragmentIR",
    "PhysicalRowIR",
    "PhysicalRowRef",
    "PhysicalTableIR",
    "PhysicalTableRef",
    "PhysicalWordIR",
    "PhysicalWordRef",
]
