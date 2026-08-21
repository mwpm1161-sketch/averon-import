import pytest

from averon_import.core.page_ranges import PageRangeError, parse_page_ranges


def test_parse_multiple_ranges():
    assert parse_page_ranges("18-20, 22, 25-26", 58) == [18, 19, 20, 22, 25, 26]


def test_reverse_range():
    assert parse_page_ranges("5-3", 10) == [3, 4, 5]


def test_invalid_page():
    with pytest.raises(PageRangeError):
        parse_page_ranges("1, 11", 10)
