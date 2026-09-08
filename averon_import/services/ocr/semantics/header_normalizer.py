"""Lossless normalization helpers for physical table header cells."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^0-9a-zа-яё²³]+", re.IGNORECASE)
_LINE_HYPHEN_RE = re.compile(r"([0-9A-Za-zА-Яа-яЁё])-\s*\n\s*([0-9A-Za-zА-Яа-яЁё])")


@dataclass(frozen=True, slots=True)
class NormalizedHeaderText:
    raw_text: str
    visual_lines: tuple[str, ...]
    joined_text: str
    dehyphenated_text: str
    normalized_text: str
    normalized_tokens: tuple[str, ...]


class HeaderCellNormalizer:
    """Generate derived header representations without changing raw OCR."""

    @staticmethod
    def normalize_text(value: str) -> str:
        text = unicodedata.normalize("NFKC", str(value or ""))
        text = text.lower().replace("ё", "е")
        text = _PUNCT_RE.sub(" ", text)
        return _SPACE_RE.sub(" ", text).strip()

    def normalize(self, raw_text: str) -> NormalizedHeaderText:
        raw = str(raw_text or "").replace("\r\n", "\n").replace("\r", "\n")
        lines = tuple(
            _SPACE_RE.sub(" ", line).strip()
            for line in raw.split("\n")
            if line.strip()
        )
        joined = " ".join(lines)
        dehyphenated = raw
        previous = None
        while previous != dehyphenated:
            previous = dehyphenated
            dehyphenated = _LINE_HYPHEN_RE.sub(r"\1\2", dehyphenated)
        dehyphenated = _SPACE_RE.sub(" ", dehyphenated).strip()
        normalized = self.normalize_text(dehyphenated or joined)
        return NormalizedHeaderText(
            raw_text=raw,
            visual_lines=lines,
            joined_text=joined,
            dehyphenated_text=dehyphenated,
            normalized_text=normalized,
            normalized_tokens=tuple(normalized.split()),
        )
