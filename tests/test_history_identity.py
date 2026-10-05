from averon_import.services.sourcing.history_identity import (
    HISTORY_IDENTITY_NORMALIZER_REVISION,
    history_model_characteristic_conflicts,
    history_name_signature,
    history_name_signature_digest,
)


def test_explicit_history_model_comparison_is_conservative_and_missing_aware():
    assert history_model_characteristic_conflicts("", "25-60") is False
    assert history_model_characteristic_conflicts("25-40", "") is False
    assert history_model_characteristic_conflicts(" 25-40 ", "25-40") is False
    for source, history in (("25-40", "25-60"), ("M-500", "M-501"), ("DN50", "DN51"), ("AB-001", "AB-1"), ("Е-50", "Ё-50"), ("25-40", "\n")):
        assert history_model_characteristic_conflicts(source, history) is True


def test_history_identity_revision_and_safe_descriptive_reordering():
    assert HISTORY_IDENTITY_NORMALIZER_REVISION == "normalized-name-unit-v1"
    pairs = (
        ("Искусственный плющ", "Плющ искусственный"),
        ("Коричневая краска по металлу", "Краска по металлу коричневая"),
        ("Пенополистирол 50 мм", "Пенополистирол 50мм"),
        ("Стеклоизделие Cristalvizion 4PLG", "Стеклоизделие 4PLG Cristalvizion"),
        ("Воздушный фильтр JSB", "Фильтр воздушный JSB"),
        ("Бетон В 15 W 2 F 100", "Бетон В15 F100 W2"),
        ("Труба ПНД SDR 17 32x2,0", "Труба ПНД 32×2.00 SDR17"),
        ("Краска порошковая RAL 7006", "RAL7006 Краска порошковая"),
        ("Кабель 5x6", "Кабель 5х6"),
        ("Кабель 5х6", "Кабель 5×6"),
        ("Работа диапазон 32-80", "32 - 80 Работа диапазон"),
        ("Кабель VVГнг(А)-LS 4x2,5", "Кабель VVГнг (А) - LS 4х2.50"),
        ("Трос 2,0 мм", "Трос 2.00мм"),
        ("Насос\u00a0тестовый", "Тестовый насос"),
    )
    for left, right in pairs:
        assert history_name_signature(left) == history_name_signature(right), (left, right)
        assert history_name_signature_digest(left) == history_name_signature_digest(right)


def test_history_identity_keeps_product_semantics_and_codes_distinct():
    unequal = (
        ("Плющ искусственный", "Камень искусственный"),
        ("RAL 7006 краска", "RAL7007 краска"),
        ("Труба SDR 17", "Труба SDR18"),
        ("Деталь DN50", "Деталь DN51"),
        ("Модель M-500", "Модель M-501"),
        ("32×2 труба", "2×32 труба"),
        ("Насос для клапана", "Клапан для насоса"),
        ("Насос не красный", "Насос красный"),
        ("Насос красный", "Насос без красного"),
        ("Болт болт M10", "Болт M10"),
        ("Кабель ВВГнг(А)-LS", "Кабель ВВГнг(A)-LS"),
        ("Артикул AB-001", "Артикул AB-1"),
        ("Артикул A/B-1", "Артикул AB/1"),
        ("Модель ЯП-0,5", "Модель ЯП-0.5"),
        ("Цемент M-500", "Цемент М-500"),
        ("Плющ искусственный", "Плющ искусственный!"),
    )
    for left, right in unequal:
        assert history_name_signature(left) != history_name_signature(right), (left, right)


def test_history_identity_opaque_identifiers_and_ambiguous_text_fail_closed():
    assert history_name_signature("Насос 80W-90") != history_name_signature("Насос 80W90")
    assert history_name_signature("Насос AB-050") != history_name_signature("Насос AB-50")
    assert history_name_signature("Деталь 5*6") is None
    assert history_name_signature("Деталь 5*6*7") is None
    assert history_name_signature("Насос, клапан") is None
    assert history_name_signature("Клапан, насос") is None
    assert history_name_signature("") is None
    assert history_name_signature("x" * 4001) is None
    assert history_name_signature("Насос\nтестовый") is None
