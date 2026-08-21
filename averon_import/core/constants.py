from __future__ import annotations

from averon_import.version import DISPLAY_VERSION

APP_NAME = "Averon Import"
APP_VERSION = DISPLAY_VERSION
DEVELOPER = "Андриянов Степан Владимирович - НВСС"

BASE_COLUMNS = [
    {"key": "position", "title": "Позиция", "width": 14},
    {"key": "name", "title": "Наименование и техническая характеристика", "width": 56},
    {"key": "type_mark", "title": "Тип / марка / обозначение", "width": 34},
    {"key": "code", "title": "Код оборудования", "width": 24},
    {"key": "manufacturer", "title": "Производитель", "width": 28},
    {"key": "unit", "title": "Единица измерения", "width": 18},
    {"key": "quantity", "title": "Количество", "width": 14},
    {"key": "mass", "title": "Масса единицы, кг", "width": 17},
    {"key": "note", "title": "Примечание", "width": 38},
]

SYSTEM_COLUMNS = [
    {"key": "section", "title": "Раздел", "width": 20},
    {"key": "system", "title": "Система", "width": 16},
    {"key": "row_type", "title": "Тип строки", "width": 18},
    {"key": "page", "title": "Страница PDF", "width": 14},
    {"key": "confidence", "title": "Уверенность, %", "width": 17},
    {"key": "status", "title": "Статус", "width": 18},
]

ALL_COLUMNS = BASE_COLUMNS + SYSTEM_COLUMNS
COLUMN_BY_KEY = {column["key"]: column for column in ALL_COLUMNS}

DEFAULT_EXPORT_COLUMNS = [
    "name",
    "type_mark",
    "manufacturer",
    "unit",
    "quantity",
    "note",
]

ROW_TYPES = {
    "item": "Позиция",
    "component": "Компонент комплекта",
    "section": "Заголовок раздела",
    "system": "Система",
    "note": "Примечание",
    "skip": "Не экспортировать",
}

STATUSES = {
    "recognized": "Распознано",
    "review": "Требует проверки",
    "verified": "Проверено",
    "edited": "Изменено сотрудником",
    "unrecognized": "Не распознано",
}
