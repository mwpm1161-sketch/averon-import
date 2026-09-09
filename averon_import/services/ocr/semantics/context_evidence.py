"""Bounded, geometry-scoped context used by table-family detection.

This module intentionally contains no semantic-column knowledge.  It only
keeps the small pieces of text that are physically adjacent to the selected
table.  In particular, a page's ``textAnnotation.fullText`` is never a valid
input to this contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any, Iterable


Bounds = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class ContextRegionEvidence:
    kind: str
    bounds: Bounds
    text: str
    word_refs: tuple[int, ...] = ()
    source: str = "ocr_words"
    provenance: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "bounds": [round(float(value), 6) for value in self.bounds],
            "text": self.text,
            "word_refs": list(self.word_refs),
            "source": self.source,
            "provenance": [dict(item) for item in self.provenance],
        }


@dataclass(frozen=True, slots=True)
class BoundedFamilyContext:
    """Raw text evidence bounded to one selected table."""

    regions: tuple[ContextRegionEvidence, ...] = ()
    provenance: tuple[dict[str, Any], ...] = ()

    def texts(self, kinds: Iterable[str] | None = None) -> tuple[str, ...]:
        allowed = set(kinds) if kinds is not None else None
        return tuple(
            region.text
            for region in self.regions
            if region.text and (allowed is None or region.kind in allowed)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "regions": [region.as_dict() for region in self.regions],
            "provenance": [dict(item) for item in self.provenance],
        }


def _word_bounds(word: dict[str, Any]) -> Bounds | None:
    # ``collect_words`` keeps provider-neutral words with the provider's
    # ``boundingBox.vertices`` shape.  Accept the direct ``vertices`` shape
    # as well so this helper remains usable by raster/vector adapters.
    vertices = word.get("vertices") or (
        (word.get("boundingBox") or {}).get("vertices")
        if isinstance(word.get("boundingBox"), dict)
        else []
    ) or []
    points = []
    for vertex in vertices:
        try:
            points.append((float(vertex["x"]), float(vertex["y"])))
        except (KeyError, TypeError, ValueError):
            continue
    if not points:
        return None
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def _union(bounds: Iterable[Bounds]) -> Bounds | None:
    values = list(bounds)
    if not values:
        return None
    return (
        min(value[0] for value in values),
        min(value[1] for value in values),
        max(value[2] for value in values),
        max(value[3] for value in values),
    )


def bounded_context_from_words(
    table_bounds: Bounds,
    words: Iterable[dict[str, Any]],
    *,
    page_height: float,
    compatibility_text: str = "",
) -> BoundedFamilyContext:
    """Collect only nearby text above ``table_bounds``.

    The distance is derived from nearby text geometry and hard-capped to a
    small page-relative region.  Table height is deliberately not allowed to
    expand the context: a tall table must not make distant page prose look
    like its caption.
    """
    left, top, right, _bottom = table_bounds
    raw_candidates: list[tuple[float, float, str, int, Bounds]] = []
    for index, word in enumerate(words):
        bounds = _word_bounds(word)
        text = str(word.get("text") or "").strip()
        if bounds is None or not text:
            continue
        word_left, word_top, word_right, word_bottom = bounds
        if word_bottom > top:
            continue
        overlap = max(0.0, min(word_right, right) - max(word_left, left))
        if overlap / max(1e-9, word_right - word_left) < 0.35:
            continue
        distance = top - word_bottom
        center = (word_top + word_bottom) / 2.0
        raw_candidates.append((center, distance, text, index, bounds))
    if not raw_candidates:
        candidates: list[tuple[float, float, str, int, Bounds]] = []
    else:
        heights = [max(1.0, item[4][3] - item[4][1]) for item in raw_candidates]
        nearby_height = max(1.0, float(median(heights)))
        page_cap = page_height * 0.12 if page_height > 0 else nearby_height * 8.0
        # Six nearby glyph heights permits a caption plus a compact title
        # cluster, while the page-relative cap is an absolute upper bound.
        distance_limit = min(page_cap, nearby_height * 6.0)
        candidates = [item for item in raw_candidates if item[1] <= distance_limit]
    candidates.sort(key=lambda item: (item[0], item[3]))
    groups: list[list[tuple[float, float, str, int, Bounds]]] = []
    line_height = max(
        1.0,
        float(median([max(1.0, item[4][3] - item[4][1]) for item in candidates]))
        if candidates else 1.0,
    )
    for item in candidates:
        if not groups or item[0] - groups[-1][-1][0] > max(1.5 * line_height, 1.0):
            groups.append([item])
        else:
            groups[-1].append(item)
    regions: list[ContextRegionEvidence] = []
    if groups:
        # Nearest group is the one immediately above the table.  Keep one
        # preceding group only when the gap is small enough to be a caption
        # plus title cluster, still bounded by table geometry.
        nearest = min(groups, key=lambda group: min(item[1] for item in group))
        selected = [nearest]
        nearest_top = min(item[0] for item in nearest)
        for group in groups:
            if group is nearest:
                continue
            if nearest_top - max(item[0] for item in group) <= max(3.0 * line_height, 1.0):
                selected.append(group)
        for group_index, group in enumerate(sorted(selected, key=lambda value: min(item[0] for item in value))):
            region_bounds = _union(item[4] for item in group) or (0.0, 0.0, 0.0, 0.0)
            regions.append(
                ContextRegionEvidence(
                    kind="table_caption" if group_index == len(selected) - 1 else "near_table_above",
                    bounds=region_bounds,
                    text=" ".join(item[2] for item in sorted(group, key=lambda value: value[4][0])),
                    word_refs=tuple(item[3] for item in group),
                    provenance=({"scope": "selected_table", "distance": round(min(item[1] for item in group), 4)},),
                )
            )
    if compatibility_text.strip():
        regions.append(
            ContextRegionEvidence(
                kind="compatibility_context",
                bounds=table_bounds,
                text=compatibility_text.strip()[:500],
                source="explicit_bounded_context",
                provenance=({"scope": "caller_supplied"},),
            )
        )
    return BoundedFamilyContext(tuple(regions))
