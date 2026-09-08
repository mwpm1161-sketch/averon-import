"""Evidence-based global assignment of physical columns to Averon fields."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

from averon_import.services.ocr.semantics.header_evidence import (
    HeaderCellEvidence,
    HeaderMappingResult,
    HeaderRegionEvidence,
    HeaderSourceCell,
    SemanticCandidate,
)
from averon_import.services.ocr.semantics.header_normalizer import HeaderCellNormalizer


TRUSTED = "trusted"
AMBIGUOUS = "ambiguous"
UNAVAILABLE = "unavailable"

CANONICAL_FIELDS = (
    "position",
    "name",
    "type_mark",
    "code",
    "manufacturer",
    "unit",
    "quantity",
    "mass",
    "note",
)
CORE_FIELDS = frozenset({"name", "unit", "quantity"})

MAX_HEADER_SCAN_ROWS = 5
MAX_HEADER_REGION_ROWS = 3
MIN_ASSIGNABLE_SCORE = 0.62
MIN_TRUSTED_FIELD_SCORE = 0.68
MIN_TRUSTED_CORE_SCORE = 0.72
MIN_ASSIGNMENT_MARGIN = 0.12
MIN_HEADER_FIELDS = 4


@dataclass(frozen=True, slots=True)
class _Vocabulary:
    phrases: tuple[str, ...]
    abbreviations: tuple[str, ...] = ()
    tokens: tuple[str, ...] = ()
    weak_stems: tuple[str, ...] = ()


VOCABULARY: dict[str, _Vocabulary] = {
    "position": _Vocabulary(
        phrases=("позиция", "номер позиции"),
        abbreviations=("поз", "поз ция", "no"),
        tokens=("позиция", "позиции"),
        weak_stems=("позиц",),
    ),
    "name": _Vocabulary(
        phrases=(
            "наименование и техническая характеристика",
            "наименование техническая характеристика",
            "наименование",
            "техническая характеристика",
        ),
        tokens=("наименование", "характеристика"),
        weak_stems=("наименован", "характерист",),
    ),
    "type_mark": _Vocabulary(
        phrases=(
            "тип марка обозначение документа опросного листа",
            "тип марка обозначение документа",
            "тип марка",
            "обозначение документа",
            "опросного листа",
        ),
        tokens=("тип", "марка", "обозначение", "документа", "опросного", "листа"),
        weak_stems=("обознач", "документ", "опросн"),
    ),
    "code": _Vocabulary(
        phrases=(
            "код оборудования материала изделия",
            "код оборудования изделия материала",
            "код оборудования",
            "код материала",
            "код изделия",
            "код",
        ),
        tokens=("код", "оборудования", "материала", "изделия"),
        weak_stems=("оборуд", "материал", "издел"),
    ),
    "manufacturer": _Vocabulary(
        phrases=(
            "завод изготовитель поставщик",
            "заводизготовитель поставщик",
            "завод изготовитель",
            "заводизготовитель",
            "изготовитель",
            "производитель",
            "поставщик",
        ),
        tokens=("завод", "заводизготовитель", "изготовитель", "производитель", "поставщик"),
        weak_stems=("изготовит", "производит", "поставщик"),
    ),
    "unit": _Vocabulary(
        phrases=(
            "единица измерения",
            "единицы измерения",
            "единица измер",
            "eduница езмерения",
            "единица езмерения",
        ),
        abbreviations=("ед изм", "ед измер", "ед"),
        tokens=("единица", "единицы", "измерения", "езмерения", "eduница"),
        weak_stems=("измер", "езмер", "edu"),
    ),
    "quantity": _Vocabulary(
        phrases=("количество",),
        abbreviations=("кол во", "кол", "кол во единиц"),
        tokens=("количество",),
        weak_stems=("колич",),
    ),
    "mass": _Vocabulary(
        phrases=("масса единицы кг", "масса кг", "масса единицы", "масса", "вес"),
        tokens=("масса", "вес", "кг"),
        weak_stems=("масс",),
    ),
    "note": _Vocabulary(
        phrases=("примечание", "примечания"),
        tokens=("примечание", "примечания"),
        weak_stems=("примеч",),
    ),
}

_NUMERIC_VALUE_RE = re.compile(r"^[+-]?\d+(?:[.,/]\d+)*$", re.IGNORECASE)
_ENGINEERING_VALUE_RE = re.compile(
    r"(?:\d.*[a-zа-я]|[a-zа-я].*\d|\b(?:гост|ту|сп)\b)", re.IGNORECASE
)
_BODY_EXCLUSIVE_FIRST_TOKENS = frozenset({"изделие", "оборудование", "материал"})


def _bounded_edit_distance(left: str, right: str, limit: int) -> int:
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous = list(range(len(right) + 1))
    for index, char in enumerate(left, start=1):
        current = [index]
        row_min = index
        for other_index, other in enumerate(right, start=1):
            value = min(
                current[-1] + 1,
                previous[other_index] + 1,
                previous[other_index - 1] + (char != other),
            )
            current.append(value)
            row_min = min(row_min, value)
        if row_min > limit:
            return limit + 1
        previous = current
    return previous[-1]


def _fuzzy_token_match(token: str, expected: str) -> bool:
    if min(len(token), len(expected)) < 5:
        return False
    limit = 1 if max(len(token), len(expected)) <= 8 else 2
    return _bounded_edit_distance(token, expected, limit) <= limit


def _score_field(normalized_text: str, tokens: tuple[str, ...], field: str) -> SemanticCandidate | None:
    vocabulary = VOCABULARY[field]
    token_set = set(tokens)
    components: dict[str, float] = {}
    matched: set[str] = set()
    evidence: list[str] = []

    for phrase in vocabulary.phrases:
        phrase_tokens = tuple(phrase.split())
        if normalized_text == phrase:
            components["exact_phrase"] = max(components.get("exact_phrase", 0.0), 1.0)
            matched.update(phrase_tokens)
            evidence.append(f"exact_phrase:{phrase}")
        elif len(phrase_tokens) >= 2 and phrase in normalized_text:
            components["contained_phrase"] = max(
                components.get("contained_phrase", 0.0), 0.91
            )
            matched.update(phrase_tokens)
            evidence.append(f"contained_phrase:{phrase}")
        elif len(phrase_tokens) >= 2 and set(phrase_tokens).issubset(token_set):
            components["token_set_phrase"] = max(
                components.get("token_set_phrase", 0.0), 0.84
            )
            matched.update(phrase_tokens)
            evidence.append(f"token_set_phrase:{phrase}")

    for abbreviation in vocabulary.abbreviations:
        abbreviation_tokens = tuple(abbreviation.split())
        if normalized_text == abbreviation or (
            len(abbreviation_tokens) >= 2 and abbreviation in normalized_text
        ):
            components["known_abbreviation"] = max(
                components.get("known_abbreviation", 0.0), 0.93
            )
            matched.update(abbreviation_tokens)
            evidence.append(f"known_abbreviation:{abbreviation}")

    exact_tokens = token_set.intersection(vocabulary.tokens)
    if exact_tokens:
        components["exact_token"] = 0.76
        matched.update(exact_tokens)
        evidence.append("exact_token")

    fuzzy_pairs: list[tuple[str, str]] = []
    if not components or max(components.values()) < MIN_ASSIGNABLE_SCORE:
        for token in tokens:
            for expected in vocabulary.tokens:
                if _fuzzy_token_match(token, expected):
                    fuzzy_pairs.append((token, expected))
                    break
    if fuzzy_pairs:
        components["bounded_ocr_variation"] = 0.72
        matched.update(token for token, _expected in fuzzy_pairs)
        evidence.extend(
            f"bounded_ocr_variation:{token}->{expected}"
            for token, expected in fuzzy_pairs
        )

    weak_matches = {
        token
        for token in tokens
        for stem in vocabulary.weak_stems
        if len(stem) >= 3 and token.startswith(stem)
    }
    if weak_matches:
        components["weak_substring"] = 0.46
        matched.update(weak_matches)
        evidence.append("weak_substring")

    if not components:
        return None
    score = max(components.values())
    strong_components = sum(value >= MIN_ASSIGNABLE_SCORE for value in components.values())
    if strong_components > 1:
        score = min(1.0, score + min(0.04, 0.01 * (strong_components - 1)))
    return SemanticCandidate(
        field=field,
        score=round(score, 4),
        score_components=tuple(sorted(components.items())),
        matched_tokens=tuple(sorted(matched)),
        evidence=tuple(dict.fromkeys(evidence)),
    )


def _semantic_candidates(normalized_text: str, tokens: tuple[str, ...]) -> tuple[SemanticCandidate, ...]:
    candidates = [
        candidate
        for field in CANONICAL_FIELDS
        if (candidate := _score_field(normalized_text, tokens, field)) is not None
    ]
    return tuple(sorted(candidates, key=lambda item: (-item.score, item.field)))


class HeaderSemanticMapper:
    def __init__(self, normalizer: HeaderCellNormalizer | None = None) -> None:
        self.normalizer = normalizer or HeaderCellNormalizer()

    def _cell_evidence(self, cell: HeaderSourceCell) -> HeaderCellEvidence:
        normalized = self.normalizer.normalize(cell.raw_text)
        candidates = _semantic_candidates(
            normalized.normalized_text, normalized.normalized_tokens
        )
        if any(line.rstrip().endswith("-") for line in normalized.visual_lines):
            candidates = tuple(
                SemanticCandidate(
                    field=candidate.field,
                    score=candidate.score,
                    score_components=tuple(sorted((
                        *candidate.score_components,
                        ("dehyphenated_phrase", 0.95),
                    ))),
                    matched_tokens=candidate.matched_tokens,
                    evidence=tuple((*candidate.evidence, "line_ending_dehyphenation")),
                )
                for candidate in candidates
            )
        body_evidence = self._body_evidence(normalized.visual_lines, candidates)
        return HeaderCellEvidence(
            physical_row=cell.physical_row,
            physical_column=cell.physical_column,
            bbox=dict(cell.bbox),
            raw_text=normalized.raw_text,
            visual_lines=normalized.visual_lines,
            joined_text=normalized.joined_text,
            dehyphenated_text=normalized.dehyphenated_text,
            normalized_text=normalized.normalized_text,
            normalized_tokens=normalized.normalized_tokens,
            semantic_candidates=candidates,
            body_evidence=body_evidence,
            provenance=tuple(dict(item) for item in cell.provenance),
            row_span=max(1, int(cell.row_span)),
            column_span=max(1, int(cell.column_span)),
        )

    def _body_evidence(
        self,
        lines: tuple[str, ...],
        whole_candidates: tuple[SemanticCandidate, ...],
    ) -> tuple[str, ...]:
        if not lines:
            return ()
        strong_whole = {
            candidate.field
            for candidate in whole_candidates
            if candidate.score >= MIN_ASSIGNABLE_SCORE
        }
        line_fields: set[str] = set()
        reasons: list[str] = []
        for index, line in enumerate(lines):
            normalized = self.normalizer.normalize(line)
            if not normalized.normalized_text:
                continue
            line_candidates = _semantic_candidates(
                normalized.normalized_text, normalized.normalized_tokens
            )
            strong_line = {
                candidate.field
                for candidate in line_candidates
                if candidate.score >= MIN_ASSIGNABLE_SCORE
            }
            line_fields.update(strong_line)
            first_token = normalized.normalized_tokens[0] if normalized.normalized_tokens else ""
            if first_token in _BODY_EXCLUSIVE_FIRST_TOKENS:
                reasons.append(f"product_value_line:{index}")
            elif _NUMERIC_VALUE_RE.fullmatch(normalized.normalized_text):
                reasons.append(f"numeric_value_line:{index}")
            elif _ENGINEERING_VALUE_RE.search(normalized.normalized_text) and not strong_line:
                reasons.append(f"engineering_value_line:{index}")
            elif not strong_line and any(char.isalpha() for char in normalized.normalized_text):
                previous_is_hyphenated = index > 0 and lines[index - 1].rstrip().endswith("-")
                current_is_hyphenated = line.rstrip().endswith("-")
                if not (previous_is_hyphenated or current_is_hyphenated):
                    reasons.append(f"unmatched_text_line:{index}")
        if len(line_fields) > 1 and not any(
            candidate.score >= 0.90 for candidate in whole_candidates
        ) and not {"mass", "note"}.issubset(strong_whole):
            reasons.append("mixed_semantic_lines")
        if line_fields and strong_whole and not line_fields.issubset(strong_whole):
            reasons.append("semantic_line_conflict")
        return tuple(dict.fromkeys(reasons))

    @staticmethod
    def _is_numbering_row(cells: Iterable[HeaderSourceCell], column_count: int) -> bool:
        values = [
            re.sub(r"\D", "", str(cell.raw_text or ""))
            for cell in cells
            if str(cell.raw_text or "").strip()
        ]
        if len(values) < min(3, max(1, column_count)) or any(not value for value in values):
            return False
        numbers = [int(value) for value in values]
        return len(set(numbers)) == len(numbers) and all(
            1 <= value <= max(column_count + 1, len(values) + 1) for value in numbers
        )

    def _aggregate_column_candidates(
        self,
        cells: tuple[HeaderSourceCell, ...],
        rows: tuple[int, ...],
        column_count: int,
        numbering_rows: tuple[int, ...] = (),
    ) -> tuple[dict[int, tuple[SemanticCandidate, ...]], tuple[HeaderCellEvidence, ...]]:
        row_set = set(rows)
        numbering_row_set = set(numbering_rows)
        selected = tuple(cell for cell in cells if cell.physical_row in row_set)
        evidence_cells = tuple(self._cell_evidence(cell) for cell in selected)
        by_column: dict[int, list[HeaderCellEvidence]] = {
            column: [] for column in range(column_count)
        }
        for evidence in evidence_cells:
            if evidence.physical_row in numbering_row_set:
                continue
            for column in range(
                evidence.physical_column,
                min(column_count, evidence.physical_column + evidence.column_span),
            ):
                by_column[column].append(evidence)
        result: dict[int, tuple[SemanticCandidate, ...]] = {}
        for column, column_cells in by_column.items():
            if not column_cells:
                result[column] = ()
                continue
            combined = "\n".join(cell.raw_text for cell in column_cells if cell.raw_text)
            normalized = self.normalizer.normalize(combined)
            candidates = list(
                _semantic_candidates(normalized.normalized_text, normalized.normalized_tokens)
            )
            supporting: dict[str, int] = {}
            for cell in column_cells:
                for candidate in cell.semantic_candidates:
                    if candidate.score >= MIN_ASSIGNABLE_SCORE:
                        supporting[candidate.field] = supporting.get(candidate.field, 0) + 1
            adjusted = []
            for candidate in candidates:
                support = supporting.get(candidate.field, 0)
                score = min(1.0, candidate.score + min(0.04, max(0, support - 1) * 0.02))
                adjusted.append(
                    SemanticCandidate(
                        field=candidate.field,
                        score=round(score, 4),
                        score_components=candidate.score_components,
                        matched_tokens=candidate.matched_tokens,
                        evidence=candidate.evidence + ((f"supporting_cells:{support}",) if support else ()),
                    )
                )
            result[column] = tuple(
                sorted(adjusted, key=lambda item: (-item.score, item.field))
            )
        return result, evidence_cells

    @staticmethod
    def _global_assignment(
        candidates_by_column: dict[int, tuple[SemanticCandidate, ...]],
        column_count: int,
    ) -> tuple[dict[int, tuple[str, ...]], float, float, dict[int, float]]:
        field_bits = {field: 1 << index for index, field in enumerate(CANONICAL_FIELDS)}
        states: dict[int, list[tuple[float, tuple[tuple[str, ...], ...]]]] = {
            0: [(0.0, tuple())]
        }
        candidate_scores = {
            column: {candidate.field: candidate.score for candidate in candidates}
            for column, candidates in candidates_by_column.items()
        }
        for column in range(column_count):
            options: list[tuple[tuple[str, ...], int, float]] = [((), 0, 0.0)]
            scores = candidate_scores.get(column, {})
            for field, score in scores.items():
                if score < MIN_ASSIGNABLE_SCORE:
                    continue
                expected = CANONICAL_FIELDS.index(field) / max(1, len(CANONICAL_FIELDS) - 1)
                actual = column / max(1, column_count - 1)
                order_prior = max(0.0, 0.03 * (1.0 - abs(expected - actual)))
                options.append(((field,), field_bits[field], score + order_prior))
            if scores.get("mass", 0.0) >= MIN_TRUSTED_FIELD_SCORE and scores.get(
                "note", 0.0
            ) >= MIN_TRUSTED_FIELD_SCORE:
                options.append(
                    (
                        ("mass", "note"),
                        field_bits["mass"] | field_bits["note"],
                        scores["mass"] + scores["note"] - 0.05,
                    )
                )
            next_states: dict[int, list[tuple[float, tuple[tuple[str, ...], ...]]]] = {}
            for used_mask, entries in states.items():
                for current_score, assignment in entries:
                    for fields, option_mask, option_score in options:
                        if used_mask & option_mask:
                            continue
                        new_mask = used_mask | option_mask
                        entry = (current_score + option_score, assignment + (fields,))
                        bucket = next_states.setdefault(new_mask, [])
                        if entry[1] not in {existing[1] for existing in bucket}:
                            bucket.append(entry)
                        bucket.sort(key=lambda item: item[0], reverse=True)
                        del bucket[2:]
            states = next_states
        ranked: list[tuple[float, tuple[tuple[str, ...], ...]]] = []
        seen = set()
        for entries in states.values():
            for entry in entries:
                if entry[1] in seen:
                    continue
                seen.add(entry[1])
                ranked.append(entry)
        ranked.sort(key=lambda item: item[0], reverse=True)
        if not ranked:
            return {}, 0.0, 0.0, {}
        best_score, best_assignment = ranked[0]
        second_score = ranked[1][0] if len(ranked) > 1 else 0.0
        mapping = {
            column: fields
            for column, fields in enumerate(best_assignment)
            if fields
        }
        selected_scores = {
            column: min(candidate_scores[column].get(field, 0.0) for field in fields)
            for column, fields in mapping.items()
        }
        return mapping, best_score, second_score, selected_scores

    def _evaluate_region(
        self,
        cells: tuple[HeaderSourceCell, ...],
        rows: tuple[int, ...],
        numbering_rows: tuple[int, ...],
        column_count: int,
    ) -> HeaderMappingResult:
        candidates_by_column, evidence_cells = self._aggregate_column_candidates(
            cells, rows, column_count, numbering_rows
        )
        mapping, best, second, selected_scores = self._global_assignment(
            candidates_by_column, column_count
        )
        mapped_fields = {field for fields in mapping.values() for field in fields}
        missing_core = tuple(sorted(CORE_FIELDS - mapped_fields))
        body_evidence = tuple(
            f"r{cell.physical_row}c{cell.physical_column}:{reason}"
            for cell in evidence_cells
            if cell.physical_row not in set(numbering_rows)
            for reason in cell.body_evidence
        )
        reasons: list[str] = []
        if len(mapped_fields) < MIN_HEADER_FIELDS or "name" not in mapped_fields:
            reasons.append("insufficient_header_evidence")
        if missing_core:
            reasons.append("missing_core_fields:" + ",".join(missing_core))
        weak_fields = sorted(
            field
            for column, fields in mapping.items()
            for field in fields
            if selected_scores.get(column, 0.0)
            < (MIN_TRUSTED_CORE_SCORE if field in CORE_FIELDS else MIN_TRUSTED_FIELD_SCORE)
        )
        if weak_fields:
            reasons.append("weak_semantic_evidence:" + ",".join(dict.fromkeys(weak_fields)))
        margin = max(0.0, best - second)
        if mapping and margin < MIN_ASSIGNMENT_MARGIN:
            reasons.append("assignment_margin_too_low")
        if body_evidence:
            reasons.append("header_body_conflict")
        if not mapping:
            status = UNAVAILABLE
        elif reasons:
            status = AMBIGUOUS
        else:
            status = TRUSTED
        region = HeaderRegionEvidence(
            header_rows=rows,
            cells=evidence_cells,
            body_evidence=body_evidence,
            numbering_rows=numbering_rows,
            reasons=tuple(reasons),
        )
        return HeaderMappingResult(
            status=status,
            header_rows=rows,
            mapping=mapping,
            candidates_by_column=candidates_by_column,
            best_score=best,
            second_best_score=second,
            assignment_margin=margin,
            unmapped_columns=tuple(
                column for column in range(column_count) if column not in mapping
            ),
            missing_core_fields=missing_core,
            reasons=tuple(reasons),
            header_cells=evidence_cells,
            candidate_regions=(region,),
        )

    def map(self, cells: Iterable[HeaderSourceCell], column_count: int) -> HeaderMappingResult:
        source_cells = tuple(cells)
        if column_count <= 0 or not source_cells:
            return HeaderMappingResult(
                status=UNAVAILABLE,
                header_rows=(),
                mapping={},
                candidates_by_column={},
                best_score=0.0,
                second_best_score=0.0,
                assignment_margin=0.0,
                unmapped_columns=tuple(range(max(0, column_count))),
                missing_core_fields=tuple(sorted(CORE_FIELDS)),
                reasons=("header_region_missing",),
            )
        rows_by_index: dict[int, tuple[HeaderSourceCell, ...]] = {}
        for row in sorted({cell.physical_row for cell in source_cells}):
            rows_by_index[row] = tuple(
                cell for cell in source_cells if cell.physical_row == row
            )
        candidate_starts = sorted(rows_by_index)[:MAX_HEADER_SCAN_ROWS]
        results: list[HeaderMappingResult] = []
        for start in candidate_starts:
            region_rows: list[int] = []
            numbering_rows: list[int] = []
            for row in range(start, start + MAX_HEADER_REGION_ROWS):
                row_cells = rows_by_index.get(row)
                if not row_cells:
                    break
                numbering = self._is_numbering_row(row_cells, column_count)
                row_evidence = tuple(self._cell_evidence(cell) for cell in row_cells)
                strong_fields = {
                    candidate.field
                    for cell in row_evidence
                    for candidate in cell.semantic_candidates
                    if candidate.score >= MIN_ASSIGNABLE_SCORE
                }
                has_body = any(cell.body_evidence for cell in row_evidence)
                if row > start and not numbering and not strong_fields:
                    break
                region_rows.append(row)
                if numbering:
                    numbering_rows.append(row)
                results.append(
                    self._evaluate_region(
                        source_cells,
                        tuple(region_rows),
                        tuple(numbering_rows),
                        column_count,
                    )
                )
                if has_body and not numbering:
                    break
        viable = [
            result
            for result in results
            if result.mapping and "name" in {field for values in result.mapping.values() for field in values}
        ]
        if not viable:
            reasons = ("semantic_mapping_unavailable",)
            return HeaderMappingResult(
                status=UNAVAILABLE,
                header_rows=(),
                mapping={},
                candidates_by_column={},
                best_score=max((result.best_score for result in results), default=0.0),
                second_best_score=0.0,
                assignment_margin=0.0,
                unmapped_columns=tuple(range(column_count)),
                missing_core_fields=tuple(sorted(CORE_FIELDS)),
                reasons=reasons,
                candidate_regions=tuple(
                    region
                    for result in results
                    for region in result.candidate_regions
                ),
            )
        viable.sort(
            key=lambda result: (
                result.status == TRUSTED,
                result.best_score,
                len(result.candidate_regions[0].numbering_rows),
                len(result.header_rows),
            ),
            reverse=True,
        )
        selected = viable[0]
        return HeaderMappingResult(
            status=selected.status,
            header_rows=selected.header_rows,
            mapping=selected.mapping,
            candidates_by_column=selected.candidates_by_column,
            best_score=selected.best_score,
            second_best_score=selected.second_best_score,
            assignment_margin=selected.assignment_margin,
            unmapped_columns=selected.unmapped_columns,
            missing_core_fields=selected.missing_core_fields,
            reasons=selected.reasons,
            header_cells=selected.header_cells,
            candidate_regions=tuple(
                region for result in results for region in result.candidate_regions
            ),
        )


def map_semantic_header(
    cells: Iterable[HeaderSourceCell], column_count: int
) -> HeaderMappingResult:
    return HeaderSemanticMapper().map(cells, column_count)
