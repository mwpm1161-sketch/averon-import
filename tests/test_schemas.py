import pytest
from pydantic import ValidationError

from averon_import.core.schemas import SaveRowsRequest


def _row(**overrides):
    data = {
        "id": "row-1",
        "name": "Клапан",
        "page": 2,
        "confidence": 91.0,
        "status": "recognized",
        "row_type": "item",
    }
    data.update(overrides)
    return data


def test_specification_rows_are_typed_at_api_boundary():
    request = SaveRowsRequest(rows=[_row(quantity="12.5")])
    assert request.rows[0].name == "Клапан"
    assert request.rows[0].quantity == "12.5"


def test_invalid_page_is_rejected():
    with pytest.raises(ValidationError):
        SaveRowsRequest(rows=[_row(page=0)])
