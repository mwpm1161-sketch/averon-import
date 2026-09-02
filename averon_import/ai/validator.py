import re


_DIGIT_HOMOGLYPHS = str.maketrans({"З": "3", "О": "0"})


class AICorrectionValidator:
    """Reject unsafe AI edits."""

    def validate(self, original: str, corrected: str) -> bool:
        if not corrected:
            return False

        # First safety rule: AI cannot silently modify numeric parameters.
        # Cyrillic/digit homoglyphs (З->3, О->0) are OCR noise, not new numbers.
        original_numbers = re.findall(r"\d+[\d,.]*", original.translate(_DIGIT_HOMOGLYPHS))
        corrected_numbers = re.findall(r"\d+[\d,.]*", corrected.translate(_DIGIT_HOMOGLYPHS))

        return original_numbers == corrected_numbers
