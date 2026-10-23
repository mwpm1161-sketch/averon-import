"""Offline comparison of OCR runs against an independent row-level reference.

This module is deliberately not imported by production OCR routes. It keeps
matching, field accuracy, classification, order, location, columns, runtime and
cost as separate measurements and returns aggregate-only reports.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from difflib import SequenceMatcher
import math
import re
import unicodedata
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from averon_import.services.ai_first.contracts import AIDocumentDraft

if TYPE_CHECKING:
    from averon_import.services.ocr.base import OcrResult

_FIELDS = {
    "position", "code", "name", "quantity", "unit", "mass", "note",
    "type_mark", "manufacturer",
}
_ITEM_FIELDS = ("position", "code", "name", "quantity", "unit")
_FIELD_ALIASES = {"article": "code", "qty": "quantity"}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_ROLE_ALIASES = {
    "item_root": "item", "item": "item", "позиция": "item",
    "component": "component", "section": "section", "header": "header",
    "column_numbers": "column_numbers", "note": "note", "service": "note",
    "system": "system", "continuation": "continuation", "unknown": "unknown",
    "context": "context", "skip": "header",
}
_SCORE_WEIGHTS = {
    "position": 3.0, "code": 6.0, "name": 5.0, "quantity": 2.0,
    "unit": 1.5, "mass": 1.0, "note": 1.0, "type_mark": 2.0,
    "manufacturer": 1.5,
}
_MIN_MATCH_SCORE = 2.0


def _safe_mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError("invalid_comparison_input")
    return value


def _positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _field_name(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid_comparison_input")
    normalized = _FIELD_ALIASES.get(value, value)
    if normalized not in _FIELDS:
        raise ValueError("invalid_comparison_input")
    return normalized


def _role(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "unknown"
    normalized = value.strip().casefold()
    return _ROLE_ALIASES.get(normalized, normalized)


def _text_values(value: Any) -> dict[str, str | None]:
    source = _safe_mapping(value)
    result: dict[str, str | None] = {}
    for key, item in source.items():
        field_name = _field_name(key)
        if field_name is None:
            raise ValueError("invalid_comparison_input")
        if item is not None and not isinstance(item, str):
            raise ValueError("invalid_comparison_input")
        if field_name in result:
            raise ValueError("invalid_comparison_input")
        result[field_name] = item
    return result


def _location(value: Any, *, default_basis: str | None = None) -> dict[str, Any] | None:
    if value is None:
        return None
    location = _safe_mapping(value)
    bbox = _safe_mapping(location.get("bbox"))
    if set(bbox) != {"x", "y", "width", "height"}:
        raise ValueError("invalid_comparison_input")
    box: dict[str, float] = {}
    for key, number in bbox.items():
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
            raise ValueError("invalid_comparison_input")
        box[key] = float(number)
    if (box["x"] < 0 or box["y"] < 0 or box["width"] <= 0 or box["height"] <= 0
            or box["x"] + box["width"] > 1.000001
            or box["y"] + box["height"] > 1.000001):
        raise ValueError("invalid_comparison_input")
    basis = location.get("basis", default_basis)
    if basis not in {"independent_reference", "provider_geometry", "model_candidate_unverified"}:
        raise ValueError("invalid_comparison_input")
    return {"bbox": box, "basis": basis, "coordinate_space": "page_normalized"}


def _reference(value: Mapping[str, Any]) -> dict[str, Any]:
    value = _safe_mapping(value)
    if value.get("format_version") != 1:
        raise ValueError("invalid_comparison_reference")
    case_id = value.get("case_id")
    source_sha256 = value.get("source_sha256")
    pages = value.get("expected_pages")
    tables = value.get("expected_columns")
    rows = value.get("expected_rows")
    if (not isinstance(case_id, str) or not _ID_RE.fullmatch(case_id)
            or not isinstance(source_sha256, str) or not _SHA256_RE.fullmatch(source_sha256)):
        raise ValueError("invalid_comparison_reference")
    if (not isinstance(pages, list) or any(not _positive_int(page) for page in pages)
            or pages != sorted(set(pages))):
        raise ValueError("invalid_comparison_reference")
    if not isinstance(tables, list) or not isinstance(rows, list):
        raise ValueError("invalid_comparison_reference")
    expected_columns: dict[tuple[int, int], tuple[str | None, ...]] = {}
    for table in tables:
        table = _safe_mapping(table)
        page, index, fields = table.get("page"), table.get("table_index"), table.get("fields")
        if (type(page) is not int or page not in pages or type(index) is not int or index < 0
                or not isinstance(fields, list)):
            raise ValueError("invalid_comparison_reference")
        key = (page, index)
        if key in expected_columns:
            raise ValueError("invalid_comparison_reference")
        normalized_fields = tuple(_field_name(field) for field in fields)
        present_fields = [field for field in normalized_fields if field is not None]
        if len(present_fields) != len(set(present_fields)):
            raise ValueError("invalid_comparison_reference")
        expected_columns[key] = normalized_fields
    expected_rows: list[dict[str, Any]] = []
    seen: set[tuple[int, int, int]] = set()
    seen_page_orders: set[tuple[int, int]] = set()
    for raw in rows:
        raw = _safe_mapping(raw)
        page, table_index, row_index = raw.get("page"), raw.get("table_index"), raw.get("row_index")
        role, fields = raw.get("role"), _text_values(raw.get("values", {}))
        page_order = raw.get("order_index")
        identity = (page, table_index, row_index)
        if (type(page) is not int or page not in pages or type(table_index) is not int or table_index < 0
                or type(row_index) is not int or row_index < 0
                or type(page_order) is not int or page_order < 0
                or not isinstance(role, str) or not role
                or identity in seen or (page, table_index) not in expected_columns
                or (page, page_order) in seen_page_orders):
            raise ValueError("invalid_comparison_reference")
        section = raw.get("section")
        if section is not None and not isinstance(section, str):
            raise ValueError("invalid_comparison_reference")
        location = _location(raw.get("location"), default_basis="independent_reference")
        if location is not None and location["basis"] != "independent_reference":
            raise ValueError("invalid_comparison_reference")
        expected_rows.append({
            "page": page, "table_index": table_index, "order": row_index,
            "page_order": page_order,
            "role": _role(role), "section": section, "values": fields,
            "location": location,
        })
        seen.add(identity)
        seen_page_orders.add((page, page_order))
    expected_rows.sort(key=lambda row: (row["page"], row["table_index"], row["order"]))
    return {
        "case_id": case_id, "expected_pages": tuple(pages),
        "source_sha256": source_sha256,
        "expected_columns": expected_columns, "expected_rows": expected_rows,
    }


def _run(value: Mapping[str, Any]) -> dict[str, Any]:
    value = _safe_mapping(value)
    engine = value.get("engine")
    if not isinstance(engine, str) or not _ID_RE.fullmatch(engine):
        raise ValueError("invalid_comparison_run")
    source_sha256 = value.get("source_sha256")
    if not isinstance(source_sha256, str) or not _SHA256_RE.fullmatch(source_sha256):
        raise ValueError("invalid_comparison_run")
    duration = value.get("duration_ms")
    if duration is not None and (
        isinstance(duration, bool) or not isinstance(duration, (int, float))
        or not math.isfinite(duration) or duration < 0
    ):
        raise ValueError("invalid_comparison_run")
    cost = _safe_mapping(value.get("cost", {"amount_rub": None, "basis": "unknown"}))
    amount, cost_basis = cost.get("amount_rub"), cost.get("basis")
    if cost_basis not in {"actual_billing", "estimated", "unknown"}:
        raise ValueError("invalid_comparison_run")
    if amount is not None and (
        isinstance(amount, bool) or not isinstance(amount, (int, float))
        or not math.isfinite(amount) or amount < 0 or cost_basis == "unknown"
    ):
        raise ValueError("invalid_comparison_run")
    if cost_basis == "unknown" and amount is not None:
        raise ValueError("invalid_comparison_run")
    usage_raw = _safe_mapping(value.get("usage", {}))
    usage: dict[str, int | None] = {}
    for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens"):
        token_count = usage_raw.get(key)
        if token_count is not None and (type(token_count) is not int or token_count < 0):
            raise ValueError("invalid_comparison_run")
        usage[key] = token_count
    raw_pages = value.get("pages")
    if not isinstance(raw_pages, list):
        raise ValueError("invalid_comparison_run")
    pages: dict[int, dict[str, Any]] = {}
    seen_page_orders: dict[int, set[int]] = defaultdict(set)
    for raw_page in raw_pages:
        raw_page = _safe_mapping(raw_page)
        number, status, raw_tables = raw_page.get("page"), raw_page.get("status"), raw_page.get("tables", [])
        if (not _positive_int(number) or number in pages
                or status not in {"completed", "no_specification", "incomplete", "failed", "unknown"}
                or not isinstance(raw_tables, list)):
            raise ValueError("invalid_comparison_run")
        tables: dict[int, dict[str, Any]] = {}
        next_page_order = 0
        for raw_table in raw_tables:
            raw_table = _safe_mapping(raw_table)
            index = raw_table.get("table_index")
            raw_columns = raw_table.get("columns", [])
            raw_rows = raw_table.get("rows")
            if (type(index) is not int or index < 0 or index in tables
                    or not isinstance(raw_columns, list) or not isinstance(raw_rows, list)):
                raise ValueError("invalid_comparison_run")
            columns = tuple(_field_name(field) for field in raw_columns)
            rows: list[dict[str, Any]] = []
            for order, raw_row in enumerate(raw_rows):
                raw_row = _safe_mapping(raw_row)
                role = raw_row.get("kind", "unknown")
                section = raw_row.get("section")
                page_order = raw_row.get("page_order", next_page_order)
                if (not isinstance(role, str) or not role
                        or (section is not None and not isinstance(section, str))
                        or type(page_order) is not int or page_order < 0
                        or page_order in seen_page_orders[number]):
                    raise ValueError("invalid_comparison_run")
                rows.append({
                    "page": number, "table_index": index, "order": order,
                    "page_order": page_order,
                    "role": _role(role), "section": section,
                    "values": _text_values(raw_row.get("values", {})),
                    "location": _location(raw_row.get("location")),
                })
                seen_page_orders[number].add(page_order)
                next_page_order = max(next_page_order + 1, page_order + 1)
            tables[index] = {"columns": columns, "rows": rows}
        pages[number] = {"status": status, "tables": tables}
    return {
        "engine": engine, "source_sha256": source_sha256,
        "duration_ms": None if duration is None else float(duration),
        "cost": {"amount_rub": None if amount is None else float(amount), "basis": cost_basis},
        "usage": usage, "pages": pages,
    }


def _match_text(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


def _exact_text(value: str | None) -> str | None:
    if value is None:
        return None
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def _bbox_iou(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    a, b = left["bbox"], right["bbox"]
    ax1, ay1, ax2, ay2 = a["x"], a["y"], a["x"] + a["width"], a["y"] + a["height"]
    bx1, by1, bx2, by2 = b["x"], b["y"], b["x"] + b["width"], b["y"] + b["height"]
    intersection = max(0.0, min(ax2, bx2) - max(ax1, bx1)) * max(0.0, min(ay2, by2) - max(ay1, by1))
    union = a["width"] * a["height"] + b["width"] * b["height"] - intersection
    return intersection / union if union > 0 else 0.0


def _score_pair(expected: Mapping[str, Any], candidate: Mapping[str, Any], *, unique_position: bool) -> float:
    score = 0.0
    for field, weight in _SCORE_WEIGHTS.items():
        if field == "position" and not unique_position:
            continue
        left = _match_text(expected["values"].get(field))
        right = _match_text(candidate["values"].get(field))
        if left and right:
            ratio = SequenceMatcher(None, left, right, autojunk=False).ratio()
            score += weight * (2 * ratio - 1)
    left_section, right_section = expected.get("section"), candidate.get("section")
    if left_section and right_section:
        score += 2.5 if _match_text(left_section) == _match_text(right_section) else -1.5
    gold_location, candidate_location = expected.get("location"), candidate.get("location")
    if (gold_location and candidate_location
            and candidate_location["basis"] == "provider_geometry"):
        score += 2.0 * _bbox_iou(gold_location, candidate_location) - 1.0
    return score


def _has_identity_anchor(expected: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    """Require content or trusted geometry; a row number alone is never identity."""
    for field in ("code", "name"):
        left = _match_text(expected["values"].get(field))
        right = _match_text(candidate["values"].get(field))
        if left and right and SequenceMatcher(None, left, right, autojunk=False).ratio() >= 0.5:
            return True
    gold_location, candidate_location = expected.get("location"), candidate.get("location")
    return bool(
        gold_location and candidate_location
        and candidate_location["basis"] == "provider_geometry"
        and _bbox_iou(gold_location, candidate_location) >= 0.5
    )


def _maximum_weight_assignment(
    weights: list[list[float]],
) -> tuple[list[int], list[float], list[float]]:
    """Return an optimal assignment and its Hungarian dual potentials."""
    size = len(weights)
    if not size:
        return [], [], []
    # Hungarian potentials for a minimum-cost assignment; negated weights turn
    # it into a maximum assignment. Zero-weight cells are unmatched fillers.
    u = [0.0] * (size + 1)
    v = [0.0] * (size + 1)
    p = [0] * (size + 1)
    way = [0] * (size + 1)
    for i in range(1, size + 1):
        p[0] = i
        j0 = 0
        minv = [float("inf")] * (size + 1)
        used = [False] * (size + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = float("inf")
            j1 = 0
            for j in range(1, size + 1):
                if used[j]:
                    continue
                current = -weights[i0 - 1][j - 1] - u[i0] - v[j]
                if current < minv[j]:
                    minv[j] = current
                    way[j] = j0
                if minv[j] < delta:
                    delta, j1 = minv[j], j
            for j in range(size + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    assigned = [-1] * size
    for j in range(1, size + 1):
        if p[j]:
            assigned[p[j] - 1] = j - 1
    return assigned, u, v


def _strong_components(graph: list[set[int]]) -> list[int]:
    """Find strongly connected components without recursion depth limits."""
    size = len(graph)
    reverse: list[set[int]] = [set() for _ in range(size)]
    for node, neighbors in enumerate(graph):
        for neighbor in neighbors:
            reverse[neighbor].add(node)

    visited: set[int] = set()
    order: list[int] = []
    for start in range(size):
        if start in visited:
            continue
        visited.add(start)
        stack: list[tuple[int, Any]] = [(start, iter(graph[start]))]
        while stack:
            node, neighbors = stack[-1]
            try:
                neighbor = next(neighbors)
            except StopIteration:
                order.append(node)
                stack.pop()
                continue
            if neighbor not in visited:
                visited.add(neighbor)
                stack.append((neighbor, iter(graph[neighbor])))

    components = [-1] * size
    component_index = 0
    for start in reversed(order):
        if components[start] >= 0:
            continue
        components[start] = component_index
        stack = [start]
        while stack:
            node = stack.pop()
            for neighbor in reverse[node]:
                if components[neighbor] < 0:
                    components[neighbor] = component_index
                    stack.append(neighbor)
        component_index += 1
    return components


def _align_rows(
    reference_rows: list[dict[str, Any]], candidate_rows: list[dict[str, Any]],
) -> tuple[list[tuple[int, int]], set[int], set[int]]:
    matches: list[tuple[int, int]] = []
    ambiguous_expected: set[int] = set()
    ambiguous_candidate: set[int] = set()
    by_page_ref: dict[int, list[int]] = defaultdict(list)
    by_page_cand: dict[int, list[int]] = defaultdict(list)
    for index, row in enumerate(reference_rows):
        by_page_ref[row["page"]].append(index)
    for index, row in enumerate(candidate_rows):
        by_page_cand[row["page"]].append(index)
    for page in sorted(set(by_page_ref) | set(by_page_cand)):
        expected_indexes, candidate_indexes = by_page_ref[page], by_page_cand[page]
        if not expected_indexes or not candidate_indexes:
            continue
        expected_positions = Counter(
            _match_text(reference_rows[index]["values"].get("position"))
            for index in expected_indexes
            if reference_rows[index]["role"] == "item"
            and reference_rows[index]["values"].get("position")
        )
        candidate_positions = Counter(
            _match_text(candidate_rows[index]["values"].get("position"))
            for index in candidate_indexes
            if candidate_rows[index]["values"].get("position")
        )
        size = max(len(expected_indexes), len(candidate_indexes))
        weights = [[0.0] * size for _ in range(size)]
        raw_scores: dict[tuple[int, int], float] = {}
        for row_i, expected_index in enumerate(expected_indexes):
            expected = reference_rows[expected_index]
            for col_i, candidate_index in enumerate(candidate_indexes):
                candidate = candidate_rows[candidate_index]
                position = _match_text(expected["values"].get("position"))
                unique_position = bool(
                    expected["role"] == "item" and position and expected_positions[position] == 1
                    and candidate_positions[position] == 1
                )
                score = _score_pair(expected, candidate, unique_position=unique_position)
                raw_scores[(row_i, col_i)] = score
                if score >= _MIN_MATCH_SCORE and _has_identity_anchor(expected, candidate):
                    weights[row_i][col_i] = score
        assignment, row_potentials, column_potentials = _maximum_weight_assignment(weights)
        inverse_assignment = [-1] * size
        for row_i, col_i in enumerate(assignment):
            inverse_assignment[col_i] = row_i

        # A tight edge can participate in an optimal assignment when it is
        # already selected or belongs to an alternating cycle in the equality
        # graph. SCC membership finds all such alternatives, including rows
        # currently assigned to a zero-weight (unmatched) edge.
        equality_graph: list[set[int]] = [set() for _ in range(size)]
        tight_edges: set[tuple[int, int]] = set()
        for row_i in range(size):
            for col_i in range(size):
                reduced_cost = -weights[row_i][col_i] - row_potentials[row_i + 1] - column_potentials[col_i + 1]
                if math.isclose(reduced_cost, 0.0, rel_tol=0.0, abs_tol=1e-9):
                    tight_edges.add((row_i, col_i))
                    equality_graph[row_i].add(inverse_assignment[col_i])
        components = _strong_components(equality_graph)

        def can_use_edge(row_i: int, col_i: int) -> bool:
            matched_row = inverse_assignment[col_i]
            return (assignment[row_i] == col_i
                    or components[row_i] == components[matched_row])

        ambiguous_rows: set[int] = set()
        ambiguous_columns: set[int] = set()
        expected_targets: dict[int, set[int]] = defaultdict(set)
        candidate_sources: dict[int, set[int]] = defaultdict(set)
        expected_can_be_unmatched: set[int] = set()
        candidate_can_be_unmatched: set[int] = set()
        for row_i in range(size):
            for col_i in range(size):
                if (row_i, col_i) not in tight_edges or not can_use_edge(row_i, col_i):
                    continue
                is_real_expected = row_i < len(expected_indexes)
                is_real_candidate = col_i < len(candidate_indexes)
                if is_real_expected and is_real_candidate and weights[row_i][col_i] > 0:
                    expected_targets[row_i].add(col_i)
                    candidate_sources[col_i].add(row_i)
                else:
                    if is_real_expected:
                        expected_can_be_unmatched.add(row_i)
                    if is_real_candidate:
                        candidate_can_be_unmatched.add(col_i)

        for row_i, targets in expected_targets.items():
            if len(targets) > 1 or row_i in expected_can_be_unmatched:
                ambiguous_rows.add(row_i)
                ambiguous_columns.update(targets)
        for col_i, sources in candidate_sources.items():
            if len(sources) > 1 or col_i in candidate_can_be_unmatched:
                ambiguous_columns.add(col_i)
                ambiguous_rows.update(sources)
        ambiguous_expected.update(expected_indexes[index] for index in ambiguous_rows)
        ambiguous_candidate.update(candidate_indexes[index] for index in ambiguous_columns)
        for row_i, col_i in enumerate(assignment[:len(expected_indexes)]):
            if col_i < 0 or col_i >= len(candidate_indexes):
                continue
            if row_i in ambiguous_rows or col_i in ambiguous_columns:
                continue
            expected_index = expected_indexes[row_i]
            candidate_index = candidate_indexes[col_i]
            if (raw_scores.get((row_i, col_i), 0) >= _MIN_MATCH_SCORE
                    and _has_identity_anchor(reference_rows[expected_index], candidate_rows[candidate_index])):
                matches.append((expected_index, candidate_index))
    return matches, ambiguous_expected, ambiguous_candidate


def _column_metrics(reference: dict[str, Any], run: dict[str, Any], matches: list[tuple[int, int]],
                    reference_rows: list[dict[str, Any]], candidate_rows: list[dict[str, Any]]) -> dict[str, Any]:
    votes: dict[tuple[int, int], Counter[int]] = defaultdict(Counter)
    for expected_index, candidate_index in matches:
        expected, candidate = reference_rows[expected_index], candidate_rows[candidate_index]
        votes[(expected["page"], expected["table_index"])][candidate["table_index"]] += 1
    compared = correct = mapped_tables = ambiguous_tables = 0
    for expected_key, expected_fields in reference["expected_columns"].items():
        page, expected_table = expected_key
        candidate_tables = run["pages"].get(page, {}).get("tables", {})
        table_votes = votes.get(expected_key, Counter())
        if table_votes:
            best = max(table_votes.values())
            winners = [index for index, count in table_votes.items() if count == best]
            if len(winners) != 1:
                ambiguous_tables += 1
                continue
            candidate_table = winners[0]
        else:
            continue
        actual_fields = candidate_tables.get(candidate_table, {}).get("columns", ())
        if not actual_fields:
            continue
        mapped_tables += 1
        for index in range(max(len(expected_fields), len(actual_fields))):
            expected_field = expected_fields[index] if index < len(expected_fields) else None
            actual_field = actual_fields[index] if index < len(actual_fields) else None
            compared += 1
            correct += expected_field == actual_field
    return {
        "status": "measured" if compared else "unavailable",
        "mapped_tables": mapped_tables,
        "ambiguous_tables": ambiguous_tables,
        "compared_assignments": compared,
        "correct_assignments": correct,
        "errors": compared - correct,
        "accuracy_rate": correct / compared if compared else None,
    }


def _report_one(reference: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    reference_rows = reference["expected_rows"]
    candidate_rows = [
        row
        for page in sorted(run["pages"])
        for table in sorted(run["pages"][page]["tables"])
        for row in run["pages"][page]["tables"][table]["rows"]
    ]
    matches, ambiguous_expected, ambiguous_candidate = _align_rows(reference_rows, candidate_rows)
    expected_to_candidate = {expected: candidate for expected, candidate in matches}
    candidate_to_expected = {candidate: expected for expected, candidate in matches}
    expected_items = [index for index, row in enumerate(reference_rows) if row["role"] == "item"]
    detected_items = sum(index in expected_to_candidate for index in expected_items)
    candidate_items = sum(row["role"] == "item" for row in candidate_rows)
    extra_rows = len(candidate_rows) - len(matches)
    unmatched_item_candidates = sum(
        row["role"] == "item" and index not in candidate_to_expected
        for index, row in enumerate(candidate_rows)
    )

    classified = sum(
        _role(reference_rows[expected]["role"]) == _role(candidate_rows[candidate]["role"])
        for expected, candidate in matches
    )
    class_compared = len(matches)
    field_metrics: dict[str, dict[str, Any]] = {}
    for field in _ITEM_FIELDS:
        compared = correct = unresolved = 0
        for expected_index in expected_items:
            expected_value = reference_rows[expected_index]["values"].get(field)
            if expected_value is None:
                continue
            if expected_index in ambiguous_expected:
                unresolved += 1
                continue
            compared += 1
            candidate_index = expected_to_candidate.get(expected_index)
            actual_value = None if candidate_index is None else candidate_rows[candidate_index]["values"].get(field)
            correct += _exact_text(expected_value) == _exact_text(actual_value)
        field_metrics[field] = {
            "compared": compared, "correct": correct, "errors": compared - correct,
            "unresolved_ambiguous_rows": unresolved,
            "accuracy_rate": correct / compared if compared else None,
        }

    item_matches = [
        (expected, candidate)
        for expected, candidate in matches
        if reference_rows[expected]["role"] == "item"
    ]
    order_candidates_by_page: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for expected, candidate in item_matches:
        page = reference_rows[expected]["page"]
        order_candidates_by_page[page].append((
            reference_rows[expected]["page_order"], candidate_rows[candidate]["page_order"],
        ))
    inversions = 0
    order_pairs = 0
    for page_orders in order_candidates_by_page.values():
        page_orders.sort()
        order_pairs += len(page_orders) * (len(page_orders) - 1) // 2
        for left in range(len(page_orders)):
            for right in range(left + 1, len(page_orders)):
                inversions += page_orders[left][1] > page_orders[right][1]

    pages = run["pages"]
    processing_complete_pages = sum(
        number in pages and pages[number]["status"] in {"completed", "no_specification"}
        for number in reference["expected_pages"]
    )
    present_pages = sum(number in pages for number in reference["expected_pages"])
    failed_pages = sum(
        number in pages and pages[number]["status"] in {"failed", "incomplete", "unknown"}
        for number in reference["expected_pages"]
    )
    complete_pages = sum(
        number in pages
        and pages[number]["status"] in {"completed", "no_specification"}
        and all(
            index in expected_to_candidate
            for index, row in enumerate(reference_rows)
            if row["page"] == number and row["role"] == "item"
        )
        for number in reference["expected_pages"]
    )

    provider_ious: list[float] = []
    model_ious: list[float] = []
    for expected, candidate in matches:
        gold_location = reference_rows[expected].get("location")
        candidate_location = candidate_rows[candidate].get("location")
        if not gold_location or not candidate_location:
            continue
        if candidate_location["basis"] == "provider_geometry":
            provider_ious.append(_bbox_iou(gold_location, candidate_location))
        elif candidate_location["basis"] == "model_candidate_unverified":
            model_ious.append(_bbox_iou(gold_location, candidate_location))

    return {
        "engine": run["engine"],
        "pages": {
            "expected": len(reference["expected_pages"]), "present": present_pages,
            "processing_complete": processing_complete_pages,
            "complete": complete_pages, "missing": len(reference["expected_pages"]) - present_pages,
            "failed_or_incomplete": failed_pages,
            "completeness_rate": complete_pages / len(reference["expected_pages"])
            if reference["expected_pages"] else None,
        },
        "position_detection": {
            "expected_items": len(expected_items), "candidate_item_rows": candidate_items,
            "detected_item_rows": detected_items, "missing_item_rows": len(expected_items) - detected_items,
            "unmatched_candidate_rows": extra_rows,
            "unmatched_item_candidates": unmatched_item_candidates,
            "coverage_rate": detected_items / len(expected_items) if expected_items else None,
        },
        "matching": {
            "ambiguous_expected_rows": len(ambiguous_expected),
            "ambiguous_candidate_rows": len(ambiguous_candidate),
        },
        "classification": {
            "compared_rows": class_compared, "correct_rows": classified,
            "errors": class_compared - classified,
            "unmatched_item_candidates": unmatched_item_candidates,
            "accuracy_rate": classified / class_compared if class_compared else None,
        },
        "order": {
            "matched_items": sum(len(page_orders) for page_orders in order_candidates_by_page.values()),
            "compared_pairs": order_pairs,
            "inversion_pairs": inversions,
            "preserved": inversions == 0 if order_pairs else None,
        },
        "field_accuracy": field_metrics,
        "column_assignment": _column_metrics(reference, run, matches, reference_rows, candidate_rows),
        "physical_location": {
            "status": "measured" if provider_ious else "candidate_only" if model_ious else "unavailable",
            "provider_geometry_comparisons": len(provider_ious),
            "mean_provider_bbox_iou": sum(provider_ious) / len(provider_ious) if provider_ious else None,
            "unverified_model_candidate_comparisons": len(model_ious),
            "mean_unverified_candidate_bbox_iou": sum(model_ious) / len(model_ious) if model_ious else None,
        },
        "runtime": {
            "duration_ms": run["duration_ms"],
            "basis": "observed" if run["duration_ms"] is not None else "unknown",
        },
        "cost": {"amount_rub": run["cost"]["amount_rub"], "basis": run["cost"]["basis"]},
        "usage": run["usage"],
    }


def build_comparison_report(reference: Mapping[str, Any], runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Score multiple normalized OCR runs against an independent reference.

    The report contains aggregate counts only. A run's cost must be marked
    ``estimated`` unless a billing record supplied an actual amount.
    """
    parsed_reference = _reference(reference)
    if not isinstance(runs, Sequence) or isinstance(runs, (str, bytes)) or not runs:
        raise ValueError("invalid_comparison_runs")
    parsed_runs = [_run(run) for run in runs]
    engines = [run["engine"] for run in parsed_runs]
    if len(set(engines)) != len(engines):
        raise ValueError("duplicate_comparison_engine")
    if any(run["source_sha256"] != parsed_reference["source_sha256"] for run in parsed_runs):
        raise ValueError("comparison_source_document_mismatch")
    return {
        "format_version": 1,
        "case_id": parsed_reference["case_id"],
        "source_identity": "declared_sha256_match",
        "expected_pages": list(parsed_reference["expected_pages"]),
        "runs": [_report_one(parsed_reference, run) for run in parsed_runs],
    }


def _canonical_header(header: str | None) -> str | None:
    if not header:
        return None
    key = " ".join(unicodedata.normalize("NFKC", header).casefold().split())
    aliases = {
        "позиция": "position", "поз.": "position", "поз": "position", "№": "position",
        "№ п/п": "position", "номер позиции": "position", "артикул": "code", "код": "code",
        "код оборудования": "code",
        "наименование": "name", "наименование и техническая характеристика": "name", "описание": "name",
        "количество": "quantity", "кол-во": "quantity", "кол.": "quantity",
        "единица измерения": "unit", "ед. изм.": "unit", "ед.изм.": "unit",
        "масса": "mass", "масса единицы, кг": "mass", "примечание": "note", "примечания": "note",
        "тип": "type_mark", "марка": "type_mark", "обозначение": "type_mark",
        "тип / марка / обозначение": "type_mark",
        "производитель": "manufacturer", "изготовитель": "manufacturer",
        "завод-изготовитель": "manufacturer",
    }
    return aliases.get(key)


def _cost_payload(cost: Mapping[str, Any] | None) -> dict[str, Any]:
    if cost is None:
        return {"amount_rub": None, "basis": "unknown"}
    source = _safe_mapping(cost)
    return {"amount_rub": source.get("amount_rub"), "basis": source.get("basis", "unknown")}


def _usage_payload(value: Mapping[str, Any] | None) -> dict[str, int | None]:
    source = _safe_mapping(value or {})
    aliases = {"prompt_tokens": "input_tokens", "completion_tokens": "output_tokens"}
    result: dict[str, int | None] = {}
    for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens"):
        raw = source.get(key)
        if raw is None:
            raw = source.get(next((old for old, new in aliases.items() if new == key), ""))
        result[key] = raw if type(raw) is int and raw >= 0 else None
    return result


def comparison_run_from_ai_draft(
    draft: AIDocumentDraft, *, source_sha256: str, engine: str = "qwen_vision", duration_ms: float | None = None,
    cost: Mapping[str, Any] | None = None, usage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert an AI-first draft to the shared comparison input shape."""
    pages = []
    for page in draft.pages:
        status = {
            "extracted": "completed", "no_specification": "no_specification",
            "incomplete": "incomplete", "failed": "failed",
        }[page.status]
        tables = []
        page_order = 0
        for table in sorted(page.tables, key=lambda item: item.index):
            columns = [None] * (max((column.index for column in table.columns), default=-1) + 1)
            for column in table.columns:
                if column.index < len(columns):
                    columns[column.index] = _field_name(column.canonical_field_candidate)
            rows = []
            for row in sorted(table.rows, key=lambda item: item.index):
                values: dict[str, str | None] = {}
                column_values: dict[str, list[str]] = defaultdict(list)
                for cell in row.raw_cells:
                    field = columns[cell.column_index] if cell.column_index < len(columns) else None
                    if field:
                        column_values[field].append(cell.raw_value)
                for field, values_for_field in column_values.items():
                    values[field] = values_for_field[0] if len(values_for_field) == 1 else None
                bbox = row.bbox_candidate
                location = None if bbox is None else {
                    "bbox": bbox, "basis": "model_candidate_unverified",
                }
                rows.append({
                    "kind": row.model_kind or "unknown", "values": values,
                    "page_order": page_order,
                    "section": row.raw_model_payload.get("section"), "location": location,
                })
                page_order += 1
            tables.append({"table_index": table.index, "columns": columns, "rows": rows})
        pages.append({"page": page.page_number, "status": status, "tables": tables})
    return {
        "engine": engine, "source_sha256": source_sha256,
        "duration_ms": duration_ms, "cost": _cost_payload(cost),
        "usage": _usage_payload(usage), "pages": pages,
    }


def _provider_location(bbox: Mapping[str, Any], geometry: Mapping[str, Any]) -> dict[str, Any] | None:
    width, height = geometry.get("width"), geometry.get("height")
    if not isinstance(width, (int, float)) or not isinstance(height, (int, float)) or width <= 0 or height <= 0:
        return None
    vertices = bbox.get("vertices") if isinstance(bbox, Mapping) else None
    if not isinstance(vertices, list) or not vertices:
        return None
    try:
        xs = [float(point["x"]) for point in vertices]
        ys = [float(point["y"]) for point in vertices]
    except (KeyError, TypeError, ValueError):
        return None
    x1, y1, x2, y2 = min(xs) / width, min(ys) / height, max(xs) / width, max(ys) / height
    if x2 <= x1 or y2 <= y1:
        return None
    return {"bbox": {"x": x1, "y": y1, "width": x2 - x1, "height": y2 - y1},
            "basis": "provider_geometry"}


def comparison_run_from_ocr_result(
    result: OcrResult, *, source_sha256: str, engine: str | None = None, duration_ms: float | None = None,
    cost: Mapping[str, Any] | None = None, usage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert an existing provider OcrResult without invoking the provider."""
    stats = result.stats if isinstance(result.stats, dict) else {}
    if duration_ms is None:
        candidate_duration = stats.get("elapsed_ms")
        duration_ms = candidate_duration if isinstance(candidate_duration, (int, float)) else None
    if usage is None:
        usage = stats.get("reported_usage") or stats.get("usage")
    output_pages = []
    for page in result.pages:
        page_status = "failed" if page.errors else "completed" if page.rows else "unknown"
        raw_page = next((
            row.metadata.get("vision_raw_page")
            for row in page.rows
            if (isinstance(row.metadata, dict)
                and isinstance(row.metadata.get("vision_raw_page"), dict))
        ), None)
        tables: list[dict[str, Any]] = []
        page_order = 0
        if raw_page is not None:
            # Qwen Vision retains the original page transcription in OcrRow
            # metadata; use it instead of its lossy legacy projection.
            for table_index, raw_table in enumerate(raw_page.get("tables", [])):
                headers = raw_table.get("headers", [])
                columns = [_canonical_header(header) if isinstance(header, str) else None for header in headers]
                raw_rows = []
                for raw_row in raw_table.get("rows", []):
                    values: dict[str, str | None] = {}
                    for cell in raw_row.get("cells", []):
                        index = cell.get("column_index")
                        if type(index) is int and 0 <= index < len(columns) and columns[index]:
                            values[columns[index]] = cell.get("raw_text")
                    raw_rows.append({
                        "kind": raw_row.get("kind", "unknown"), "values": values,
                        "page_order": page_order,
                        "location": None if raw_row.get("bbox_candidate") is None else {
                            "bbox": raw_row["bbox_candidate"], "basis": "model_candidate_unverified",
                        },
                    })
                    page_order += 1
                tables.append({"table_index": table_index, "columns": columns, "rows": raw_rows})
            page_status = "completed" if not page.errors else "failed"
        else:
            tables_by_index: dict[int, dict[str, Any]] = {}
            for page_order, row in enumerate(page.rows):
                metadata = row.metadata if isinstance(row.metadata, dict) else {}
                table_index = metadata.get("source_table_index", metadata.get("vision_candidate_table_index", 0))
                if type(table_index) is not int or table_index < 0:
                    table_index = 0
                table = tables_by_index.setdefault(table_index, {
                    "table_index": table_index, "columns": [], "rows": [],
                })
                for column_text, fields in (metadata.get("column_mapping") or {}).items():
                    try:
                        column_index = int(column_text)
                    except (TypeError, ValueError):
                        continue
                    if column_index < 0:
                        continue
                    while len(table["columns"]) <= column_index:
                        table["columns"].append(None)
                    field_values = [field for field in fields if field in _FIELDS] if isinstance(fields, list) else []
                    table["columns"][column_index] = field_values[0] if len(field_values) == 1 else None
                role = metadata.get("semantic_role", metadata.get("semantic_row_type", "unknown"))
                location = _provider_location(row.bbox, page.geometry or {}) if isinstance(row.bbox, dict) else None
                table["rows"].append({
                    "kind": role, "values": _text_values(row.values),
                    "page_order": page_order,
                    "section": metadata.get("section"), "location": location,
                })
            tables = [tables_by_index[index] for index in sorted(tables_by_index)]
            if not page.rows and not page.errors:
                page_status = "unknown"
        output_pages.append({"page": page.page, "status": page_status, "tables": tables})
    normalized_usage = _usage_payload(usage)
    return {
        "engine": engine or result.provider, "source_sha256": source_sha256,
        "duration_ms": duration_ms,
        "cost": _cost_payload(cost), "usage": normalized_usage, "pages": output_pages,
    }
