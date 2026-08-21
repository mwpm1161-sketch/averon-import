from averon_import.core.review import critical_confidence


def test_critical_confidence_uses_weakest_populated_critical_field():
    values = {"name": "Клапан", "unit": "шт.", "quantity": "2", "type_mark": "ABC"}
    confidences = {"name": 98, "unit": 92, "quantity": 37, "type_mark": 89}
    assert critical_confidence(values, confidences) == 37.0


def test_critical_confidence_ignores_empty_fields():
    values = {"unit": "шт.", "quantity": "", "type_mark": ""}
    confidences = {"unit": 91, "quantity": 12, "type_mark": 4}
    assert critical_confidence(values, confidences) == 91.0
