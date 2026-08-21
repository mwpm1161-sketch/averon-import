from __future__ import annotations

import re
from functools import lru_cache

from rapidfuzz import fuzz, process

# Conservative vocabulary used only to repair text which is already very close
# to a known engineering term. Product models, article numbers and all-uppercase
# abbreviations are intentionally excluded from fuzzy correction.
ENGINEERING_WORDS = [
    "антивибрационный", "вентилятор", "вентиляционный", "воздушный",
    "воздуховод", "врезка", "вытяжной", "гибкий", "датчик", "диффузор",
    "дренажный", "дроссель", "заглушка", "измерения", "клапан", "комплект",
    "кондиционер", "круглый", "нагреватель", "нагревательный", "насос",
    "наружный", "обратный", "оборудование", "отвод", "оцинкованный",
    "оцинкованной", "переход", "подобрать", "привод", "приточный",
    "приточная", "прямоугольный", "решетка", "решётка", "реле",
    "стали", "теплоизоляция", "тройник", "трубка", "управления", "фильтр",
    "цвет", "шумоглушитель", "щит",
]

ENGINEERING_PHRASES = [
    "антивибрационный комплект",
    "гибкие вставки",
    "дроссель-клапан",
    "канальный датчик",
    "привод воздушного клапана",
    "реле давления",
    "теплоизоляция",
    "щит управления",
]

# Frequent, highly characteristic OCR substitutions observed in Russian GOST
# specification sheets. Replacements are word-boundary based and deliberately
# limited to terms that are unambiguous in this document class.
EXACT_OCR_REPLACEMENTS = {
    "воздухобвод": "воздуховод",
    "воздухопод": "воздуховод",
    "троцник": "тройник",
    "отбод": "отвод",
    "фультр": "фильтр",
    "приуточная": "приточная",
    "приуточноя": "приточная",
    "прубод": "привод",
    "круглыц": "круглый",
    "оцинкованноц": "оцинкованной",
    "оцинкобанноц": "оцинкованной",
    "компл.ект": "комплект",
    "дренажныц": "дренажный",
    "цбет": "цвет",
    "дамчик": "датчик",
    "устанобка": "установка",
    "моновлочная": "моноблочная",
    "моноблочноая": "моноблочная",
    "боздушныц": "воздушный",
    "боздушнычц": "воздушный",
    "упрабления": "управления",
    "дабления": "давления",
    "wymoenywummenb": "шумоглушитель",
    "quuabmp": "фильтр",
    "e2udkue": "гибкие",
    "bcmabku": "вставки",
    "wum": "щит",
    "npubod": "привод",
    "bo3d": "возд.",
    "knanaha": "клапана",
    "dud": "диф.",
    "pene": "реле",
    "pewemka": "решетка",
    "apkmuka": "Арктика",
    "kqhoabhoy": "канальной",
    "kqhoabhou": "канальной",
    "damyuk": "датчик",
    "эстабкиу": "вставки",
    "2udkue": "гибкие",
    "wwymmoenywumenb": "шумоглушитель",
    "mohoonoyhaa": "моноблочная",
    "кональной": "канальной",
    "канальноцу": "канальной",
    "щелебая": "щелевая",
    "нахимобскии": "Нахимовский",
    "kaanaha": "клапана",
    "nod": "под",
    "ubem": "цвет",
    "цбем": "цвет",
    "облицовку": "облицовки",
}

# Tesseract sometimes emits Latin look-alikes inside Russian words.
LATIN_TO_CYRILLIC = str.maketrans(
    {
        "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К",
        "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
        "a": "а", "b": "в", "c": "с", "d": "д", "e": "е", "k": "к",
        "m": "м", "o": "о", "p": "р", "t": "т", "x": "х", "y": "у",
        "3": "з",
    }
)


def clean_text(value: str | None) -> str:
    value = value or ""
    value = value.replace("\u00ad", "").replace("\u00a0", " ")
    value = value.replace("—", "-").replace("–", "-")
    value = value.replace("ﬁ", "fi").replace("ﬂ", "fl")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\.{2,}", ".", value)
    value = re.sub(r'[”"»]+(?=:)', '', value)
    value = re.sub(r"\s*\n\s*", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    value = re.sub(r"(?<=\d)\s*[xхXХ]\s*(?=\d)", "×", value)
    value = re.sub(r"(?<!\w)[$§](?=\s*\d)", "Ø", value)
    value = re.sub(r"\bм\s*2\b", "м²", value, flags=re.IGNORECASE)
    value = re.sub(r"\bм\s*3\b", "м³", value, flags=re.IGNORECASE)
    value = re.sub(r"\bшт\s*\.*", "шт.", value, flags=re.IGNORECASE)
    value = re.sub(r"\bкомпл\s*\.*", "компл.", value, flags=re.IGNORECASE)
    # Mixed Cyrillic/Latin readings of ГОСТ are common in narrow model columns.
    value = re.sub(r"(?i)(?<!\w)[гgr][оo0][сcs][тt](?!\w)", "ГОСТ", value)
    return value.strip(" \n|_")


def _match_case(source: str, replacement: str) -> str:
    if source.isupper():
        return replacement.upper()
    if source[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


@lru_cache(maxsize=8192)
def _correct_token(token: str) -> str:
    low = token.lower()
    if low in EXACT_OCR_REPLACEMENTS:
        return _match_case(token, EXACT_OCR_REPLACEMENTS[low])
    if len(token) < 4 or any(char.isdigit() for char in token):
        return token
    if token.isupper() and len(token) <= 8:
        return token

    candidate = token.translate(LATIN_TO_CYRILLIC)
    if not re.fullmatch(r"[А-Яа-яЁё-]+", candidate):
        return token

    matches = process.extract(candidate.lower(), ENGINEERING_WORDS, scorer=fuzz.ratio, limit=2)
    if not matches:
        return token
    best_word, best_score, _ = matches[0]
    second_score = matches[1][1] if len(matches) > 1 else 0
    threshold = 93 if len(candidate) <= 4 else 88 if len(candidate) <= 7 else 84
    # Require both a strong match and separation from the next candidate to
    # avoid silently changing brand names or model codes.
    if best_score >= threshold and (best_score - second_score >= 5 or best_score >= 96):
        return _match_case(token, best_word)
    return token


@lru_cache(maxsize=4096)
def correct_engineering_text(value: str) -> str:
    if not value:
        return value
    tokens = re.split(r"([\s,;:()\[\]{}/.]+)", value)
    corrected = [_correct_token(token) if re.search(r"[A-Za-zА-Яа-яЁё]", token) else token for token in tokens]
    text = "".join(corrected)

    # Phrase-level correction is deliberately strict: both sides must already
    # contain multiple words and have similar length. This prevents a token
    # such as "датчик" from being expanded into an invented phrase.
    low = text.lower().strip()
    if len(low.split()) >= 2:
        for phrase in ENGINEERING_PHRASES:
            if phrase in low:
                continue
            if abs(len(low) - len(phrase)) > 5:
                continue
            if fuzz.ratio(low, phrase) >= 91:
                text = _match_case(text, phrase)
                break
    text = re.sub(r"\.{2,}", ".", text)
    return text


def normalize_cell(key: str, value: str | None) -> str:
    text = clean_text(value)
    if key in {"name", "note"}:
        text = re.sub(r"^[-–—]\s*[_~]+\s*", "- ", text)
        text = re.sub(r"(?i)\b6\s+(?=компл\.)", "в ", text)
        text = correct_engineering_text(text)
    if key == "manufacturer":
        text = correct_engineering_text(text)
        text = re.sub(r"(?i)нахимобскии", "Нахимовский", text)
        text = re.sub(r"^0{2,3}(?=\s|$)", "ООО", text)
    if key in {"type_mark", "code", "manufacturer"}:
        text = re.sub(r"(?i)(?<!\w)[гgr][оo0][сcs][тt](?=\s*\d)", "ГОСТ", text)
    if key == "type_mark":
        text = re.sub(r"(?i)\bAPH(?=\s*\d)", "АРН", text)
        text = re.sub(r"(?i)\bKBK(?=\s*\d)", "КВК", text)
        text = re.sub(r"(?i)\b6APC(?=\d)", "6АРС", text)
        text = re.sub(r"SOLARIS Lite [XХ]P(?=\s)", "SOLARIS Lite XP", text, flags=re.IGNORECASE)
        text = re.sub(r"(SOLARIS Lite XP \d+-\d+/)\\\.(\d+)", r"\1V.\2", text, flags=re.IGNORECASE)
    if key in {"quantity", "mass"}:
        text = text.replace(" ", "").replace(",", ".")
        text = re.sub(r"[^0-9.\-]", "", text)
        if text.count(".") > 1:
            first, *rest = text.split(".")
            text = first + "." + "".join(rest)
    if key == "unit":
        low = text.lower().strip(" .")
        compact = re.sub(r"[^а-яёa-z0-9²³]+", "", low)
        mapping = {
            "шт": "шт.", "шг": "шт.", "шм": "шт.", "wm": "шт.", "штт": "шт.",
            "м": "м", "мп": "м", "м2": "м²", "м²": "м²",
            "компл": "компл.", "комплект": "компл.", "комп": "компл.",
            "компд": "компл.", "комплд": "компл.", "компп": "компл.",
            "кг": "кг", "м3": "м³", "м³": "м³",
            "пм": "п.м.", "л": "л", "кт": "к-т", "к-т": "к-т",
        }
        text = mapping.get(compact, text)
    text = re.sub(r"(?i)\bвозд\.\s*Клапана\b", "возд.клапана", text)
    return text


def engineering_plausibility(key: str, value: str) -> float:
    """Return a small quality bonus/penalty used to compare OCR candidates."""
    text = normalize_cell(key, value)
    if not text:
        return -12.0
    if key in {"quantity", "mass"}:
        return 22.0 if re.fullmatch(r"-?\d+(?:[.,]\d+)?", text) else -35.0
    if key == "unit":
        return 22.0 if text in {"шт.", "м", "м²", "м³", "кг", "компл.", "п.м.", "л", "к-т"} else -18.0
    if key == "position":
        return 8.0 if re.search(r"[0-9IVXLСА-ЯA-Z]", text) else -10.0
    if key in {"name", "note"}:
        cyr = len(re.findall(r"[А-Яа-яЁё]", text))
        latin = len(re.findall(r"[A-Za-z]", text))
        weird = len(re.findall(r"[^\w\s.,;:()\-+/×Ø№²³\"']", text))
        return min(16.0, cyr * 0.35) - latin * 0.08 - weird * 3.0
    if key in {"type_mark", "code", "manufacturer"}:
        weird = len(re.findall(r"[^\w\s.,;:()\-+/×Ø№²³\"']", text))
        return 8.0 - weird * 3.0
    return 0.0


def as_excel_number(value: str | None):
    text = normalize_cell("quantity", value)
    if not text or text in {"-", "."}:
        return None
    try:
        number = float(text)
        return int(number) if number.is_integer() else number
    except ValueError:
        return value
