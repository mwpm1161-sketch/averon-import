import re


class AICorrectionValidator:
    """Reject unsafe AI edits."""

    def validate(self, original: str, corrected: str) -> bool:
        if not corrected:
            return False

        original_numbers = re.findall(r"\d+[\d,.]*", original)
        corrected_numbers = re.findall(r"\d+[\d,.]*", corrected)

        # First safety rule: AI cannot silently modify numeric parameters.
        return original_numbers == corrected_numbers
