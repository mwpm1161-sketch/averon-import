from __future__ import annotations

import re


class PageRangeError(ValueError):
    """Raised when a page range cannot be parsed."""


def parse_page_ranges(value: str, page_count: int) -> list[int]:
    """Parse 1-based page expressions such as ``18-25, 31, 40-42``.

    The returned values are unique, sorted and still 1-based for clarity at the
    API/UI boundary.
    """
    value = (value or "").strip()
    if not value:
        return []

    pages: set[int] = set()
    for token in re.split(r"[,;\s]+", value):
        token = token.strip()
        if not token:
            continue
        if "-" in token or "–" in token or "—" in token:
            parts = re.split(r"[-–—]", token)
            if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
                raise PageRangeError(f"Некорректный диапазон страниц: {token}")
            start, end = (int(part.strip()) for part in parts)
            if start > end:
                start, end = end, start
            pages.update(range(start, end + 1))
        elif token.isdigit():
            pages.add(int(token))
        else:
            raise PageRangeError(f"Некорректный номер страницы: {token}")

    invalid = [page for page in pages if page < 1 or page > page_count]
    if invalid:
        raise PageRangeError(
            f"Страницы вне диапазона документа 1-{page_count}: "
            + ", ".join(map(str, sorted(invalid)))
        )
    return sorted(pages)
