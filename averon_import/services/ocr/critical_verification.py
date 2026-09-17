"""BBox-only secondary verification for critical Yandex OCR fields.

Primary reconstructed values remain authoritative. Secondary table and exact
cell results are manual-review candidates unless an exact-cell candidate has
independent structural proof and passes the explicit safe-promotion contract.
"""

from __future__ import annotations

import re

from averon_import.core.normalizers import normalize_cell, numeric_cell_metadata
from averon_import.services.ocr.base import OcrRow
from averon_import.services.review_policy import (
    CRITICAL_FIELDS,
    is_critical_values,
    mark_semantic_field_required,
)

_KNOWN_UNITS = {"шт.", "м", "м²", "м³", "кг", "компл.", "п.м.", "л", "к-т"}


def _vertices_box(box: dict | None) -> tuple[float, float, float, float] | None:
    vertices = (box or {}).get("vertices") if isinstance(box, dict) else None
    if not isinstance(vertices, list):
        return None
    points: list[tuple[float, float]] = []
    for vertex in vertices:
        if not isinstance(vertex, dict):
            continue
        try:
            points.append((float(vertex.get("x", 0)), float(vertex.get("y", 0))))
        except (TypeError, ValueError):
            continue
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _normal_box(box: tuple[float, float, float, float], width: float, height: float) -> dict:
    return {
        "x": round(box[0] / width, 6),
        "y": round(box[1] / height, 6),
        "width": round((box[2] - box[0]) / width, 6),
        "height": round((box[3] - box[1]) / height, 6),
    }


def _box_from_normal(value: dict) -> tuple[float, float, float, float] | None:
    try:
        x = float(value["x"])
        y = float(value["y"])
        return x, y, x + float(value["width"]), y + float(value["height"])
    except (KeyError, TypeError, ValueError):
        return None


def _intersection(a, b) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0.0, min(a[3], b[3]) - max(a[1], b[1])
    )


def _table_cells(payload: dict, crop: dict | None = None) -> list[dict]:
    annotation = payload.get("textAnnotation") if isinstance(payload, dict) else None
    if not isinstance(annotation, dict):
        return []
    try:
        width = float(annotation.get("width") or payload.get("page", {}).get("width"))
        height = float(annotation.get("height") or payload.get("page", {}).get("height"))
    except (AttributeError, TypeError, ValueError):
        return []
    if width <= 0 or height <= 0:
        return []
    crop = crop or {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}
    cells: list[dict] = []
    for table in annotation.get("tables") or []:
        if not isinstance(table, dict):
            continue
        for cell in table.get("cells") or []:
            if not isinstance(cell, dict):
                continue
            box = _vertices_box(cell.get("boundingBox"))
            if box is None:
                continue
            local = _normal_box(box, width, height)
            cells.append(
                {
                    "text": str(cell.get("text") or ""),
                    "bbox": {
                        "x": crop["x"] + local["x"] * crop["width"],
                        "y": crop["y"] + local["y"] * crop["height"],
                        "width": local["width"] * crop["width"],
                        "height": local["height"] * crop["height"],
                    },
                }
            )
    return cells


def _candidate_value(field: str, raw: str) -> str | None:
    lines = [line.strip() for line in str(raw).replace("\r", "").splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    value = normalize_cell(field, lines[0])
    if not value:
        return None
    if field in {"quantity", "mass"}:
        details = numeric_cell_metadata(lines[0])
        if details.get("numeric_suspect") or not re.fullmatch(r"-?\d+(?:\.\d+)?", value):
            return None
    elif field == "unit" and value not in _KNOWN_UNITS:
        return None
    return value


def _match_field(primary_box: dict, field: str, secondary_cells: list[dict]) -> dict | None:
    target = _box_from_normal(primary_box)
    if target is None:
        return None
    target_area = max(1e-9, (target[2] - target[0]) * (target[3] - target[1]))
    ranked: list[tuple[float, dict]] = []
    for cell in secondary_cells:
        value = _candidate_value(field, cell["text"])
        if value is None:
            continue
        box = _box_from_normal(cell["bbox"])
        if box is None:
            continue
        overlap = _intersection(target, box) / target_area
        center_x = (box[0] + box[2]) / 2
        center_y = (box[1] + box[3]) / 2
        if overlap >= 0.15 or (
            target[0] <= center_x <= target[2] and target[1] <= center_y <= target[3]
        ):
            ranked.append((overlap, {"value_candidate": value, "raw_value": cell["text"], "bbox": cell["bbox"]}))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if not ranked:
        return None
    # More than one geometrically plausible token is not safe to resolve by
    # score alone: a neighboring row/column may have been captured as well.
    if len(ranked) > 1:
        return None
    best_score, best = ranked[0]
    if best_score < 0.15:
        return None
    return best


def attach_secondary_candidates(
    primary_rows: list[OcrRow],
    secondary_payload: dict,
    *,
    crop: dict | None = None,
) -> dict[str, int]:
    """Attach unique secondary candidates to primary rows in place."""
    secondary_cells = _table_cells(secondary_payload, crop=crop)
    stats = {"secondary_candidates": 0, "secondary_recovered": 0}
    for row in primary_rows:
        if not is_critical_values(row.values):
            continue
        metadata = row.metadata
        field_bboxes = metadata.get("cell_bboxes") or {}
        if not field_bboxes:
            continue
        candidates = dict(metadata.get("value_candidates") or {})
        reasons = list(metadata.get("review_reasons") or [])
        conflict_fields = set(metadata.get("secondary_conflict_fields") or [])
        for field in CRITICAL_FIELDS:
            primary_box = field_bboxes.get(field)
            if not isinstance(primary_box, dict):
                continue
            candidate = _match_field(primary_box, field, secondary_cells)
            if candidate is None:
                continue
            primary_value = normalize_cell(field, row.values.get(field, ""))
            if primary_value and primary_value == candidate["value_candidate"]:
                # Agreement is useful for diagnostics but does not need a
                # review action or a duplicated value in the result.
                continue
            conflict = bool(primary_value and primary_value != candidate["value_candidate"])
            mark_semantic_field_required(row, field)
            candidate.update({
                "candidate_source": "yandex_secondary",
                "review_reason": (
                    "secondary_conflict" if conflict
                    else "recovered_by_secondary_ocr"
                ),
            })
            candidates[field] = candidate
            stats["secondary_candidates"] += 1
            if conflict:
                conflict_fields.add(field)
                if "secondary_conflict" not in reasons:
                    reasons.append("secondary_conflict")
            else:
                stats["secondary_recovered"] += 1
        if candidates:
            metadata["value_candidates"] = candidates
            if conflict_fields:
                metadata["secondary_conflict_fields"] = sorted(conflict_fields)
            if "recovered_by_secondary_ocr" not in reasons:
                if any(
                    candidate.get("review_reason") == "recovered_by_secondary_ocr"
                    for candidate in candidates.values()
                    if isinstance(candidate, dict)
                ):
                    reasons.append("recovered_by_secondary_ocr")
            metadata["review_reasons"] = reasons
    return stats


def attach_exact_cell_candidate(
    row: OcrRow,
    field: str,
    raw_value: str,
    *,
    bbox: dict,
    source: str = "yandex_exact_cell_2x",
) -> bool:
    """Attach an isolated-cell result as review evidence, never as a value."""
    primary_raw = str(row.values.get(field, "") or "")
    primary_value = normalize_cell(field, primary_raw)
    primary_shape_trigger = bool(
        field == "quantity"
        and primary_value
        and numeric_cell_metadata(primary_raw).get("integer_like_decimal")
    )
    if field not in CRITICAL_FIELDS or (primary_value and not primary_shape_trigger):
        return False
    value = _candidate_value(field, raw_value)
    if value is None:
        return False
    metadata = row.metadata if isinstance(row.metadata, dict) else {}
    # A source-blank package component has no applicable quantity field unless
    # the raster layer independently proved that the target cell contains a
    # glyph, or an earlier stage explicitly declared the field applicable.
    # Structural safety alone is not proof of a printed value and cached
    # evidence must not manufacture that proof.
    if (
        not primary_value
        and metadata.get("semantic_role") == "COMPONENT"
    ):
        field_evidence_map = metadata.get("semantic_field_evidence")
        field_evidence = (
            field_evidence_map.get(field)
            if isinstance(field_evidence_map, dict)
            else None
        )
        required_fields = metadata.get("semantic_required_critical_fields") or ()
        if not isinstance(required_fields, (list, tuple, set)):
            required_fields = ()
        raster_glyph_proven = (
            isinstance(field_evidence, dict)
            and field_evidence.get("raster_glyph") is True
        )
        if not raster_glyph_proven and field not in required_fields:
            return False
    mark_semantic_field_required(row, field)
    candidates = dict(metadata.get("value_candidates") or {})
    existing = candidates.get(field)
    evidence = {
        "value_candidate": value,
        "raw_value": str(raw_value),
        "bbox": dict(bbox),
        "candidate_source": source,
        "review_reason": "recovered_by_exact_cell_ocr",
        "auto_trusted": False,
    }
    if primary_value:
        agrees_with_primary = primary_value == value
        evidence["agreement_with_primary"] = agrees_with_primary
        if not agrees_with_primary:
            evidence["review_reason"] = "numeric_shape_conflict"
    else:
        agrees_with_primary = False
    if isinstance(existing, dict):
        if existing.get("value_candidate") == value:
            sources = list(existing.get("evidence_sources") or [])
            for item in (existing.get("candidate_source"), source):
                if item and item not in sources:
                    sources.append(item)
            existing = dict(existing)
            existing["evidence_sources"] = sources
            existing["agreement_with_primary"] = bool(
                existing.get("agreement_with_primary") or agrees_with_primary
            )
            candidates[field] = existing
        else:
            alternatives = list(metadata.get("alternative_value_candidates") or [])
            alternatives.append({"field": field, **evidence})
            metadata["alternative_value_candidates"] = alternatives
            reasons = list(metadata.get("review_reasons") or [])
            if "secondary_conflict" not in reasons:
                reasons.append("secondary_conflict")
            if "numeric_shape_conflict" not in reasons:
                reasons.append("numeric_shape_conflict")
            metadata["review_reasons"] = reasons
            metadata["value_candidates"] = candidates
            return True
    else:
        candidates[field] = evidence
    if primary_value and not agrees_with_primary:
        alternatives = list(metadata.get("alternative_value_candidates") or [])
        if not any(
            isinstance(item, dict)
            and item.get("field") == field
            and item.get("value_candidate") == value
            for item in alternatives
        ):
            alternatives.append({"field": field, **evidence})
        metadata["alternative_value_candidates"] = alternatives
        conflict_fields = set(metadata.get("secondary_conflict_fields") or ())
        conflict_fields.add(field)
        metadata["secondary_conflict_fields"] = sorted(conflict_fields)
        reasons = list(metadata.get("review_reasons") or [])
        for reason in ("secondary_conflict", "numeric_shape_conflict"):
            if reason not in reasons:
                reasons.append(reason)
        metadata["review_reasons"] = reasons
        metadata["value_candidates"] = candidates
        return True
    metadata["value_candidates"] = candidates
    reasons = list(metadata.get("review_reasons") or [])
    if primary_value and agrees_with_primary and resolve_numeric_shape_with_exact_cell_evidence(row, field):
        return True
    if "recovered_by_exact_cell_ocr" not in reasons:
        reasons.append("recovered_by_exact_cell_ocr")
    metadata["review_reasons"] = reasons
    return True


def resolve_numeric_shape_with_exact_cell_evidence(row: OcrRow, field: str) -> bool:
    """Resolve a quantity shape warning only after safe exact-cell agreement."""
    if field != "quantity":
        return False
    metadata = row.metadata if isinstance(row.metadata, dict) else {}
    primary = normalize_cell(field, row.values.get(field, ""))
    if not primary or not numeric_cell_metadata(str(row.values.get(field) or "")).get(
        "integer_like_decimal"
    ):
        return False
    candidate = (metadata.get("value_candidates") or {}).get(field)
    if not isinstance(candidate, dict):
        return False
    candidate_value = _candidate_value(field, str(candidate.get("raw_value") or ""))
    if candidate_value != primary or not str(candidate.get("candidate_source") or "").startswith(
        "yandex_exact_cell"
    ):
        return False
    safety = (metadata.get("target_cell_structural_safety") or {}).get(field)
    if not isinstance(safety, dict) or safety.get("safe") is not True:
        return False
    if field in set(metadata.get("secondary_conflict_fields") or ()):
        return False
    if any(
        isinstance(item, dict) and str(item.get("field") or "") == field
        for item in metadata.get("alternative_value_candidates") or ()
    ):
        return False
    candidate = dict(candidate)
    candidate.update({
        "agreement_with_primary": True,
        "auto_trusted": True,
        "verified_by_exact_cell_ocr": True,
        "review_reason": None,
    })
    metadata.setdefault("value_candidates", {})[field] = candidate
    verified = set(metadata.get("numeric_shape_verified_fields") or ())
    verified.add(field)
    metadata["numeric_shape_verified_fields"] = sorted(verified)
    metadata["review_reasons"] = [
        reason
        for reason in metadata.get("review_reasons") or ()
        if reason != "numeric_shape_suspect"
    ]
    required = metadata.get("semantic_required_critical_fields") or ()
    if required and all(
        str(row.values.get(required_field, "") or "").strip()
        for required_field in required
    ) and not metadata["review_reasons"]:
        metadata["semantic_review"] = False
        metadata["semantic_state"] = "VERIFIED"
        metadata["semantic_review_impact"] = "NONE"
    return True


def promote_exact_cell_candidate(row: OcrRow, field: str) -> bool:
    """Promote one locally safe exact-cell quantity to canonical output.

    Exact-cell OCR is independent evidence only when the raster layer has
    already proved the target cell safe and there is exactly one candidate.
    Package-unit rows intentionally remain review-only: an absent package
    quantity is not evidence that the quantity is one.
    """

    if field not in CRITICAL_FIELDS:
        return False
    metadata = row.metadata if isinstance(row.metadata, dict) else {}
    if metadata.get("semantic_role") != "ITEM_ROOT":
        return False
    if str(row.values.get(field, "") or "").strip():
        return False
    candidate = (metadata.get("value_candidates") or {}).get(field)
    if not isinstance(candidate, dict):
        return False
    source = str(candidate.get("candidate_source") or "")
    if not source.startswith("yandex_exact_cell"):
        return False
    if field in set(metadata.get("secondary_conflict_fields") or ()):
        return False
    if any(
        isinstance(item, dict) and str(item.get("field") or "") == field
        for item in metadata.get("alternative_value_candidates") or ()
    ):
        return False
    safety = (metadata.get("target_cell_structural_safety") or {}).get(field)
    if not isinstance(safety, dict) or safety.get("safe") is not True:
        return False
    normalized = _candidate_value(field, str(candidate.get("raw_value") or ""))
    if normalized is None:
        return False
    if field == "quantity":
        details = numeric_cell_metadata(str(candidate.get("raw_value") or ""))
        if details.get("integer_like_decimal"):
            return False
    unit = normalize_cell("unit", row.values.get("unit", ""))
    if field == "quantity" and unit == "компл.":
        return False

    row.values[field] = normalized
    row.sources[field] = source
    metadata.setdefault("raw_values", {})[field] = str(
        candidate.get("raw_value") or normalized
    )
    metadata.setdefault("normalization", {})[field] = numeric_cell_metadata(
        str(candidate.get("raw_value") or normalized)
    )
    metadata.setdefault("semantic_field_evidence", {})[field] = {
        **dict(metadata.get("semantic_field_evidence", {}).get(field) or {}),
        "exact_cell_ocr": True,
        "verified": True,
        "bbox": dict(candidate.get("bbox") or {}),
    }
    promoted = dict(candidate)
    promoted["auto_trusted"] = True
    promoted["verified_by_exact_cell_ocr"] = True
    promoted["review_reason"] = None
    metadata.setdefault("value_candidates", {})[field] = promoted
    metadata["review_reasons"] = [
        reason
        for reason in metadata.get("review_reasons") or ()
        if reason not in {"recovered_by_exact_cell_ocr", "critical_value_missing"}
    ]
    required = metadata.get("semantic_required_critical_fields") or ()
    if required and all(
        str(row.values.get(required_field, "") or "").strip()
        for required_field in required
    ) and not metadata["review_reasons"]:
        metadata["semantic_review"] = False
        metadata["semantic_state"] = "VERIFIED"
        metadata["semantic_review_impact"] = "NONE"
    return True


def record_secondary_conflict(row: OcrRow, field: str, candidate: dict) -> None:
    """Record a disagreement without replacing the primary value."""
    primary = normalize_cell(field, row.values.get(field, ""))
    secondary = str(candidate.get("value_candidate") or "")
    if primary and secondary and primary != secondary:
        reasons = list(row.metadata.get("review_reasons") or [])
        if "secondary_conflict" not in reasons:
            reasons.append("secondary_conflict")
        row.metadata["review_reasons"] = reasons
        fields = set(row.metadata.get("secondary_conflict_fields") or [])
        fields.add(field)
        row.metadata["secondary_conflict_fields"] = sorted(fields)
