from __future__ import annotations

import re

from rapidfuzz import fuzz

from averon_import.suppliers.models import ProductMatch, ProductOffer, ProductQuery


def _norm(value: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", " ", (value or "").lower()).strip()


def _compact(value: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", "", (value or "").lower())


class ProductMatcher:
    """Conservative deterministic matcher used before any AI arbitration."""

    def match(self, query: ProductQuery, offer: ProductOffer) -> ProductMatch:
        conflicts: list[str] = []
        reasons: list[str] = []

        q_article = _compact(query.article)
        o_article = _compact(offer.article)
        exact_article = bool(q_article and o_article and q_article == o_article)
        if q_article and o_article and not exact_article:
            conflicts.append("article_mismatch")
        elif exact_article:
            reasons.append("exact_article")

        q_manufacturer = _norm(query.manufacturer)
        o_manufacturer = _norm(offer.manufacturer)
        if q_manufacturer and o_manufacturer:
            manufacturer_score = fuzz.ratio(q_manufacturer, o_manufacturer)
            if manufacturer_score < 65:
                conflicts.append("manufacturer_mismatch")
            elif manufacturer_score >= 90:
                reasons.append("manufacturer_match")
        else:
            manufacturer_score = 0.0

        model_score = 0.0
        if query.model:
            offer_model_text = " ".join(filter(None, [offer.model, offer.title]))
            model_score = fuzz.token_set_ratio(_norm(query.model), _norm(offer_model_text))
            if model_score >= 90:
                reasons.append("model_match")

        name_score = fuzz.token_set_ratio(_norm(query.name), _norm(offer.title)) if query.name else 0.0
        if name_score >= 85:
            reasons.append("name_match")

        if exact_article:
            score = 100.0
        else:
            weights: list[tuple[float, float]] = []
            if query.name:
                weights.append((name_score, 0.45))
            if query.model:
                weights.append((model_score, 0.40))
            if q_manufacturer and o_manufacturer:
                weights.append((manufacturer_score, 0.15))
            denominator = sum(weight for _, weight in weights) or 1.0
            score = sum(value * weight for value, weight in weights) / denominator
            if conflicts:
                score = min(score, 49.0)

        return ProductMatch(
            query=query,
            offer=offer,
            score=round(max(0.0, min(100.0, score)), 1),
            exact_article=exact_article,
            hard_conflicts=conflicts,
            reasons=reasons,
        )
