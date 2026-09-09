"""Provider-independent assembly of OCR rows into final Averon rows.

Consumes the internal OCR DTO only (``OcrRow`` / its ``as_dict`` wire shape)
and applies the historical RecognitionService semantics verbatim: continuation
repair, classification, section/system tracking, component blocks, statuses,
confidences and service fields. Heuristics and thresholds must stay identical
between providers; text normalization deliberately does NOT happen here —
providers are responsible for returning already normalized cell text.
"""

from __future__ import annotations

import re
import uuid

from averon_import.services.ocr.base import OcrRow
from averon_import.services.review_policy import (
    critical_blockers_for_row,
    critical_field_count,
    refresh_review_state,
)


class SpecificationRowAssembler:
    SECTION_WORDS = (
        "вентиляц",
        "кондиционир",
        "отоплен",
        "теплоснабжен",
        "холодоснабжен",
        "водоснабжен",
        "канализац",
        "оборудован",
    )
    SYSTEM_RE = re.compile(r"^(?:[ПВКЕВBPK]{1,4}\s*\d+(?:[.,]\d+)?|К\d+(?:\.\d+)*)$", re.I)

    def __init__(self):
        self.current_section = ""
        self.current_system = ""
        self.component_block_active = False
        self.previous_page: int | None = None
        self.context_unresolved = True

    def begin_page(self, page: int) -> None:
        """Start a page with context carry restricted to adjacent pages."""
        page = int(page)
        if self.previous_page is None or (
            page != self.previous_page and page != self.previous_page + 1
        ):
            self.current_section = ""
            self.current_system = ""
            self.component_block_active = False
            self.context_unresolved = True
        self.previous_page = page

    def build_page(self, page: int, raw_rows: list[OcrRow]) -> list[dict]:
        """Convenience composition of prepare + build_row for one page."""
        self.begin_page(page)
        prepared = self.prepare(raw_rows)
        return [self.build_row(page, raw) for raw in prepared]

    def prepare(self, raw_rows: list[OcrRow]) -> list[dict]:
        return self.repair_continuation_rows([row.as_dict() for row in raw_rows])

    def build_row(self, page: int, raw: dict) -> dict:
        self.begin_page(page)
        values = raw["values"]
        metadata = raw.get("metadata") or {}
        row_type = self.classify_row(values)
        name = values.get("name", "").strip()
        position = values.get("position", "").strip()
        # A short standalone row directly below a recognized section
        # is normally a system code (П1, В2, К1 and similar). Even
        # when the narrow GOST font is read as Ш/И/01, preserve the
        # source text but classify the row correctly for review.
        nonempty_fields = [
            key for key, value in values.items() if str(value).strip()
        ]
        structured_geometry = (
            metadata.get("structured_table")
            and metadata.get("provider_has_explicit_rows")
            and metadata.get("reconstruction_mode") == "geometry_first"
        )
        if (
            row_type == "note"
            and self.current_section
            and len(nonempty_fields) == 1
            and nonempty_fields[0] in {"name", "position"}
            and len(name or position) <= 5
            and (
                not structured_geometry
                or self.SYSTEM_RE.fullmatch((name or position).replace(" ", ""))
            )
        ):
            row_type = "system"

        has_independent_amount = bool(
            values.get("unit") or values.get("quantity") or values.get("manufacturer")
        )
        if self.component_block_active and not has_independent_amount and row_type not in {"section", "system"}:
            if values.get("name") or values.get("type_mark"):
                row_type = "component"
        if self.component_block_active and has_independent_amount:
            self.component_block_active = False
        if row_type == "item" and "компл" in name.lower() and values.get("quantity"):
            self.component_block_active = True

        # In a provider-structured specification, a physical body row that
        # contains only identity evidence is not a harmless free-text note.
        # Keep proven sections, systems and bullet components on their
        # existing paths; all other identity-only structured rows remain
        # reviewable candidates until critical values are resolved.
        if (
            row_type == "note"
            and metadata.get("structured_table")
            and metadata.get("provider_has_explicit_rows")
            and metadata.get("reconstruction_mode") == "geometry_first"
            and (name or position)
        ):
            row_type = "item_candidate"

        if row_type == "section":
            self.current_section = name.rstrip(":*") or position
            self.current_system = ""
            self.component_block_active = False
            self.context_unresolved = False
        elif row_type == "system":
            self.current_system = name or position

        confidence_values = [
            value
            for key, value in raw["confidences"].items()
            if values.get(key, "").strip()
        ]
        confidence = (
            round(sum(confidence_values) / len(confidence_values), 1)
            if confidence_values
            else 0.0
        )
        review_reasons = list(metadata.get("review_reasons") or [])
        if row_type == "item_candidate" and "physical_row_unresolved" not in review_reasons:
            review_reasons.append("physical_row_unresolved")
        if self.context_unresolved and row_type not in {"section", "system", "skip"}:
            if "context_missing" not in review_reasons:
                review_reasons.append("context_missing")
        if metadata.get("provider") == "yandex_vision" and not raw["confidences"]:
            if "no_confidence" not in review_reasons:
                review_reasons.append("no_confidence")
        if "numeric_suspect" in review_reasons:
            status = "review"
        else:
            status = self.status_for(values, confidence, row_type)
        if row_type == "system":
            system_text = (name or position).replace(" ", "")
            if not self.SYSTEM_RE.fullmatch(system_text):
                status = "review"
        result = {
            "id": uuid.uuid4().hex,
            **values,
            "section": self.current_section,
            "system": self.current_system,
            "row_type": row_type,
            "page": page,
            "confidence": confidence,
            "status": status,
            "bbox": raw["bbox"],
            "confidences": raw["confidences"],
            "ocr_sources": raw.get("ocr_sources", {}),
            "source_row": raw["source_row"],
            "edited": False,
            "ocr_metadata": metadata,
            "structured_table": bool(metadata.get("structured_table")),
            "provider_has_explicit_rows": bool(metadata.get("provider_has_explicit_rows")),
            "source_table_index": metadata.get("source_table_index"),
            "source_row_index": metadata.get("source_row_index"),
            "source_subrow_index": metadata.get("source_subrow_index"),
            "source_cell_refs": list(metadata.get("source_cell_refs") or []),
            "split_evidence": list(metadata.get("split_evidence") or []),
            "structural_ambiguity": bool(metadata.get("structural_ambiguity")),
            "secondary_conflict_fields": list(
                metadata.get("secondary_conflict_fields") or []
            ),
            "review_reasons": review_reasons,
            "review_reason": ", ".join(review_reasons),
        }
        result["value_candidates"] = dict(metadata.get("value_candidates") or {})
        refresh_review_state(result)
        return result

    def build_semantic_row(self, page: int, raw: OcrRow | dict) -> dict:
        """Present an already-resolved semantic row without legacy inference.

        The semantic resolver owns row meaning, continuation composition and
        canonical values.  The assembler only adds the stable presentation
        shape and applies the shared review policy; in particular it never
        calls ``classify_row`` or ``repair_continuation_rows`` on this path.
        """

        self.begin_page(page)
        payload = raw.as_dict() if isinstance(raw, OcrRow) else dict(raw)
        values = dict(payload.get("values") or {})
        metadata = dict(payload.get("metadata") or {})
        semantic_role = str(metadata.get("semantic_role") or "ITEM_ROOT")
        role_to_type = {
            "ITEM_ROOT": "item",
            "COMPONENT": "component",
            "NOTE": "note",
            "SERVICE": "note",
            "SECTION": "section",
            "SYSTEM": "system",
            "HEADER": "skip",
        }
        row_type = str(
            metadata.get("semantic_row_type")
            or role_to_type.get(semantic_role, "semantic_review")
        )
        if semantic_role == "CONTEXT":
            row_type = {
                "SECTION": "section",
                "SYSTEM": "system",
            }.get(str(metadata.get("semantic_qualifier") or ""), "note")
        if metadata.get("semantic_resolved") is False:
            row_type = "semantic_review"
        semantic_review = bool(
            metadata.get("semantic_review")
            or metadata.get("semantic_resolved") is False
            or metadata.get("semantic_state") == "REVIEW"
        )
        review_reasons = list(metadata.get("review_reasons") or [])
        if semantic_review and "physical_row_semantics_unresolved" in review_reasons:
            pass
        elif semantic_review and metadata.get("semantic_resolved") is False:
            review_reasons.append("physical_row_semantics_unresolved")
        confidence_values = [
            float(value)
            for key, value in (payload.get("confidences") or {}).items()
            if values.get(key, "").strip()
        ]
        confidence = round(sum(confidence_values) / len(confidence_values), 1) if confidence_values else 0.0
        section = self.current_section
        system = self.current_system
        if row_type == "section":
            section = str(values.get("name") or values.get("position") or "").rstrip(":*")
            self.current_section = section
            self.current_system = ""
            system = ""
        elif row_type == "system":
            system = str(values.get("name") or values.get("position") or "")
            self.current_system = system
        result = {
            "id": uuid.uuid4().hex,
            **values,
            "section": section,
            "system": system,
            "row_type": row_type,
            "page": page,
            "confidence": confidence,
            "status": "review" if semantic_review else "recognized",
            "bbox": dict(payload.get("bbox") or {}),
            "confidences": dict(payload.get("confidences") or {}),
            "ocr_sources": dict(payload.get("ocr_sources") or {}),
            "source_row": payload.get("source_row", 0),
            "edited": False,
            "ocr_metadata": metadata,
            "structured_table": bool(metadata.get("structured_table")),
            "provider_has_explicit_rows": bool(metadata.get("provider_has_explicit_rows")),
            "source_table_index": metadata.get("source_table_index"),
            "source_row_index": metadata.get("source_row_index"),
            "source_subrow_index": metadata.get("source_subrow_index"),
            "source_cell_refs": list(metadata.get("source_cell_refs") or []),
            "physical_row_refs": list(metadata.get("physical_row_refs") or []),
            "split_evidence": list(metadata.get("split_evidence") or []),
            "structural_ambiguity": bool(metadata.get("structural_ambiguity")),
            "secondary_conflict_fields": list(
                metadata.get("secondary_conflict_fields") or []
            ),
            "review_reasons": list(dict.fromkeys(review_reasons)),
            "semantic_authoritative": bool(metadata.get("semantic_authoritative")),
            "semantic_review": semantic_review,
            "semantic_state": metadata.get("semantic_state", "VERIFIED"),
            "semantic_review_impact": metadata.get("semantic_review_impact", "NONE"),
            "logical_item_id": metadata.get("logical_item_id"),
            "semantic_review_preview": metadata.get("semantic_review_preview", ""),
        }
        result["review_reason"] = ", ".join(result["review_reasons"])
        result["value_candidates"] = dict(metadata.get("value_candidates") or {})
        refresh_review_state(result)
        if semantic_review and result.get("status") == "recognized":
            result["status"] = "review"
        return result

    @staticmethod
    def repair_continuation_rows(raw_rows: list[dict]) -> list[dict]:
        """Conservatively merge OCR fragments split into a following table row.

        A row is merged only when it has no independent position/quantity/unit
        and clearly looks like a continuation: the name starts with lower-case
        text, the previous name ends with punctuation, or the current row only
        contains a secondary field. Bullet/component rows are preserved.
        """
        repaired: list[dict] = []
        for current in raw_rows:
            values = current.get("values", {})
            metadata = current.get("metadata") or {}
            name = str(values.get("name", "")).strip()
            independent = any(str(values.get(key, "")).strip() for key in (
                "position", "type_mark", "code", "manufacturer", "unit", "quantity", "mass"
            ))
            starts_component = bool(re.match(r"^[\-–—•]", name))
            secondary_only = (
                not name
                and any(str(values.get(key, "")).strip() for key in ("type_mark", "code", "note"))
            )
            starts_lower = bool(name and name[:1].islower())
            previous_punct = bool(
                repaired
                and str(repaired[-1].get("values", {}).get("name", "")).rstrip().endswith((",", ";", "-"))
            )
            should_merge = bool(
                repaired
                and not metadata.get("provider_has_explicit_rows")
                and not metadata.get("structured_table")
                and not independent and not starts_component
                and (secondary_only or previous_punct)
            )
            if not should_merge:
                repaired.append(current)
                continue

            previous = repaired[-1]
            for key, value in values.items():
                value = str(value).strip()
                if not value:
                    continue
                old = str(previous["values"].get(key, "")).strip()
                previous["values"][key] = f"{old} {value}".strip()
                old_conf = float(previous.get("confidences", {}).get(key, 0) or 0)
                new_conf = float(current.get("confidences", {}).get(key, 0) or 0)
                previous.setdefault("confidences", {})[key] = round(
                    (old_conf + new_conf) / (2 if old_conf and new_conf else 1), 1
                )
            first_box = previous.get("bbox", {})
            second_box = current.get("bbox", {})
            if first_box and second_box:
                bottom = max(
                    first_box.get("y", 0) + first_box.get("height", 0),
                    second_box.get("y", 0) + second_box.get("height", 0),
                )
                first_box["height"] = bottom - first_box.get("y", 0)
            previous["source_row"] = f"{previous.get('source_row')}+{current.get('source_row')}"
        return repaired

    def classify_row(self, values: dict[str, str]) -> str:
        name = values.get("name", "").strip()
        position = values.get("position", "").strip()
        quantity = values.get("quantity", "").strip()
        unit = values.get("unit", "").strip()
        type_mark = values.get("type_mark", "").strip()
        manufacturer = values.get("manufacturer", "").strip()
        identity_fields = any(
            values.get(key, "").strip()
            for key in ("name", "position", "type_mark", "code", "manufacturer")
        )
        product_evidence = any(
            values.get(key, "").strip()
            for key in ("type_mark", "code", "manufacturer", "unit", "mass")
        )

        low = name.lower()
        if (
            any(word in low for word in self.SECTION_WORDS)
            and not quantity
            and not product_evidence
        ):
            return "section"
        if (
            self.SYSTEM_RE.fullmatch(name.replace(" ", ""))
            or self.SYSTEM_RE.fullmatch(position.replace(" ", ""))
        ) and not quantity and not unit:
            return "system"
        if re.match(r"^[\-–—•]", name):
            return "component"
        if (quantity or unit or values.get("note") or values.get("mass")) and not identity_fields:
            # A physical body row with amount evidence is not a harmless
            # separator. Preserve it as a reviewable candidate so page/export
            # safety can account for the missing identity field.
            return "item_candidate"
        if quantity or unit or type_mark or values.get("code") or manufacturer or values.get("mass"):
            return "item"
        if name or position:
            return "note"
        return "skip"

    @staticmethod
    def status_for(values: dict[str, str], confidence: float, row_type: str) -> str:
        if row_type == "item_candidate":
            return "unrecognized"
        if row_type in {"section", "system", "note", "component"}:
            return "recognized" if confidence >= 55 else "review"
        critical_present = bool(values.get("name")) and bool(
            values.get("quantity") or values.get("unit")
        )
        if not critical_present:
            return "review" if values.get("name") else "unrecognized"
        return "recognized" if confidence >= 68 else "review"

    @staticmethod
    def summary(rows: list[dict], errors: list[dict]) -> dict:
        status_counts: dict[str, int] = {}
        type_counts: dict[str, int] = {}
        for row in rows:
            status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
            type_counts[row["row_type"]] = type_counts.get(row["row_type"], 0) + 1
        return {
            "total_rows": len(rows),
            "status_counts": status_counts,
            "type_counts": type_counts,
            "page_errors": len(errors),
            "unresolved_critical": sum(critical_field_count(row) for row in rows),
            "critical_rows": sum(bool(critical_blockers_for_row(row)) for row in rows),
        }
