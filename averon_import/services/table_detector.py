from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import cv2
import numpy as np


class TableDetectionError(RuntimeError):
    pass


@dataclass(slots=True)
class DetectedTable:
    x_lines: list[int]
    y_lines: list[int]
    image_width: int
    image_height: int
    crop_offset_x: int = 0
    crop_offset_y: int = 0

    @property
    def column_count(self) -> int:
        return max(0, len(self.x_lines) - 1)

    @property
    def row_count(self) -> int:
        return max(0, len(self.y_lines) - 1)

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        return self.x_lines[0], self.y_lines[0], self.x_lines[-1], self.y_lines[-1]


class GostSpecificationDetector:
    """Detects the standard nine-column equipment specification grid.

    The detector relies primarily on geometry rather than OCR. This makes it
    stable for scans or rasterized CAD sheets that follow the same standard,
    while still allowing a manual crop supplied by the UI.
    """

    EXPECTED_WIDTH_RATIOS = np.array(
        [0.051, 0.329, 0.152, 0.089, 0.111, 0.051, 0.051, 0.061, 0.105],
        dtype=np.float64,
    )

    def detect(self, image: np.ndarray, crop: dict | None = None) -> DetectedTable:
        if image is None or image.size == 0:
            raise TableDetectionError("Пустое изображение страницы")

        original_h, original_w = image.shape[:2]
        offset_x = offset_y = 0
        working = image
        if crop:
            x = int(crop["x"] * original_w)
            y = int(crop["y"] * original_h)
            width = int(crop["width"] * original_w)
            height = int(crop["height"] * original_h)

            # Users usually draw the crop over the visible table area, and a
            # pointer can land a few pixels inside the outer border. Expand the
            # selection before line detection so all ten vertical borders remain
            # available. The crop still limits unrelated page content.
            padding_x = max(8, int(original_w * 0.03))
            padding_y = max(8, int(original_h * 0.015))
            x -= padding_x
            y -= padding_y
            width += padding_x * 2
            height += padding_y * 2

            x = max(0, min(x, original_w - 1))
            y = max(0, min(y, original_h - 1))
            width = max(20, min(width, original_w - x))
            height = max(20, min(height, original_h - y))
            working = image[y : y + height, x : x + width]
            offset_x, offset_y = x, y

        gray = cv2.cvtColor(working, cv2.COLOR_BGR2GRAY)
        binary = cv2.threshold(gray, 205, 255, cv2.THRESH_BINARY_INV)[1]

        h, w = gray.shape
        horizontal_kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT, (max(60, w // 28), 1)
        )
        vertical_kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT, (1, max(60, h // 24))
        )
        horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, horizontal_kernel)
        vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN, vertical_kernel)

        manual_crop = bool(crop)
        x_candidates = self._vertical_candidates(vertical, allow_edges=manual_crop)
        x_lines = self._select_column_sequence(
            x_candidates, w, prefer_page_margin=not manual_crop
        )
        if len(x_lines) != 10:
            raise TableDetectionError(
                f"Не удалось определить 9 столбцов таблицы: найдено линий {len(x_lines)}"
            )

        left, right = x_lines[0], x_lines[-1]
        y_lines = self._horizontal_candidates(horizontal, left, right)
        y_lines = self._trim_footer_rows(y_lines)
        if len(y_lines) < 4:
            raise TableDetectionError(
                f"Не удалось определить строки таблицы: найдено линий {len(y_lines)}"
            )

        return DetectedTable(
            x_lines=[x + offset_x for x in x_lines],
            y_lines=[y + offset_y for y in y_lines],
            image_width=original_w,
            image_height=original_h,
            crop_offset_x=offset_x,
            crop_offset_y=offset_y,
        )

    @staticmethod
    def _groups_from_mask(mask: np.ndarray) -> list[int]:
        indexes = np.flatnonzero(mask)
        groups: list[list[int]] = []
        for value in indexes:
            value = int(value)
            if not groups or value > groups[-1][-1] + 1:
                groups.append([value])
            else:
                groups[-1].append(value)
        return [round(sum(group) / len(group)) for group in groups]

    def _vertical_candidates(
        self, vertical: np.ndarray, allow_edges: bool = False
    ) -> list[int]:
        h, w = vertical.shape
        # Main column borders begin close to the top and continue through much
        # of the page. The projection excludes the lower title block.
        projection = (vertical[: int(h * 0.88), :] > 0).sum(axis=0)
        candidates = self._groups_from_mask(projection > h * 0.45)
        if allow_edges:
            # A user-selected crop can start almost exactly at the table border,
            # so rejecting lines near x=0 would lose the first column boundary.
            return [x for x in candidates if 0 <= x < w]
        return [x for x in candidates if w * 0.025 < x < w * 0.995]

    def _select_column_sequence(
        self,
        candidates: list[int],
        width: int,
        prefer_page_margin: bool = True,
    ) -> list[int]:
        if len(candidates) < 10:
            return candidates
        best: tuple[float, list[int]] | None = None
        # There are normally only a dozen long vertical lines. Searching all
        # ordered 10-line windows is sufficient and avoids fragile hard-coded x.
        for start in range(0, len(candidates) - 9):
            sequence = candidates[start : start + 10]
            span = sequence[-1] - sequence[0]
            if span < width * 0.72:
                continue
            widths = np.diff(sequence).astype(np.float64)
            ratios = widths / widths.sum()
            shape_error = float(np.mean(np.abs(ratios - self.EXPECTED_WIDTH_RATIOS)))
            edge_penalty = (
                abs(sequence[0] / width - 0.045) * 0.04
                if prefer_page_margin
                else 0.0
            )
            score = shape_error + edge_penalty
            if best is None or score < best[0]:
                best = (score, sequence)
        if best:
            return best[1]
        return candidates[:10]

    def _horizontal_candidates(
        self, horizontal: np.ndarray, left: int, right: int
    ) -> list[int]:
        span = right - left
        projection = (horizontal[:, left : right + 1] > 0).sum(axis=1)
        lines = self._groups_from_mask(projection > span * 0.80)
        return [line for line in lines if line > 5]

    @staticmethod
    def _trim_footer_rows(lines: list[int]) -> list[int]:
        if len(lines) < 5:
            return lines
        gaps = np.diff(lines)
        regular = gaps[gaps < np.percentile(gaps, 75)]
        median = float(np.median(regular)) if len(regular) else float(np.median(gaps))
        # A large gap marks the start of notes/title block after the table.
        for index, gap in enumerate(gaps):
            if index >= 2 and gap > median * 2.15:
                return lines[: index + 1]
        return lines

    def line_masks(self, image: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        binary = cv2.threshold(gray, 205, 255, cv2.THRESH_BINARY_INV)[1]
        h, w = gray.shape
        horizontal_kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT, (max(60, w // 28), 1)
        )
        vertical_kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT, (1, max(60, h // 24))
        )
        return (
            cv2.morphologyEx(binary, cv2.MORPH_OPEN, horizontal_kernel),
            cv2.morphologyEx(binary, cv2.MORPH_OPEN, vertical_kernel),
        )
