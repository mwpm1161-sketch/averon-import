import re


COMMON_REPLACEMENTS = {
    "BP": "ВР",
}


def apply_safe_rules(text: str) -> str:
    """Apply only conservative OCR fixes.

    Does not touch numbers or technical values.
    """
    result = text
    for source, target in COMMON_REPLACEMENTS.items():
        result = result.replace(source, target)

    # OCR often confuses Cyrillic З with digit 3 inside numeric tokens.
    result = re.sub(r"(?<=\d)З(?=\d|,|\.)", "3", result)
    return result
