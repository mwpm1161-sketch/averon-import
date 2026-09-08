"""Provider-neutral semantic interpretation of physical table headers."""

from averon_import.services.ocr.semantics.column_mapper import (
    HeaderSemanticMapper,
    map_semantic_header,
)
from averon_import.services.ocr.semantics.header_evidence import (
    HeaderCellEvidence,
    HeaderMappingResult,
    HeaderRegionEvidence,
    HeaderSourceCell,
    SemanticCandidate,
)
from averon_import.services.ocr.semantics.header_normalizer import HeaderCellNormalizer
from averon_import.services.ocr.semantics.context_evidence import (
    BoundedFamilyContext,
    ContextRegionEvidence,
)
from averon_import.services.ocr.semantics.family_classifier import (
    AMBIGUOUS as AMBIGUOUS_FAMILY,
    OTHER_TABLE,
    SUPPORTED_SPECIFICATION,
    DEFAULT_TABLE_FAMILY_CLASSIFIER,
    FamilyEvidence,
    TableFamilyAssessment,
    TableFamilyClassifier,
)
from averon_import.services.ocr.semantics.schema_gate import (
    SchemaAssessment,
    SchemaGate,
    SchemaGateEvidence,
    DEFAULT_SCHEMA_GATE,
)

__all__ = [
    "HeaderCellEvidence",
    "HeaderCellNormalizer",
    "HeaderMappingResult",
    "HeaderRegionEvidence",
    "HeaderSemanticMapper",
    "HeaderSourceCell",
    "SemanticCandidate",
    "map_semantic_header",
    "BoundedFamilyContext",
    "ContextRegionEvidence",
    "FamilyEvidence",
    "TableFamilyAssessment",
    "TableFamilyClassifier",
    "DEFAULT_TABLE_FAMILY_CLASSIFIER",
    "SUPPORTED_SPECIFICATION",
    "OTHER_TABLE",
    "AMBIGUOUS_FAMILY",
    "SchemaAssessment",
    "SchemaGate",
    "SchemaGateEvidence",
    "DEFAULT_SCHEMA_GATE",
]
