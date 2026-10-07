from __future__ import annotations

from averon_import.services.manual_tenders.sourcing import canonical_tender_projection


def _projection(result: dict):
    return canonical_tender_projection(
        {"results": [result]},
        [{"source_row_id": "row-1", "excel_row": 7}],
        ["row-1"],
    )[0]


def _match(provider: str, decision: str):
    return {
        "offer": {"provider": provider, "offer_id": "shared-id"},
        "decision": decision,
        "rank": 1,
        "matched_attributes": ["article"],
    }


def _result(*, offer_provider: str | None, matches: list[dict], review_candidate=None):
    offer = {"offer_id": "shared-id"}
    if offer_provider is not None:
        offer["provider"] = offer_provider
    return {
        "intent": {"source_row_id": "row-1"},
        "recommended_offer": offer,
        "match_results": matches,
        "review_candidate": review_candidate,
        "offers": [],
        "route": {},
    }


def test_tender_projection_correlates_same_offer_id_by_provider_when_available():
    result = _result(
        offer_provider="lemana_b2b",
        matches=[
            _match("etm_ipro", "REVIEW"),
            _match("lemana_b2b", "MATCH"),
        ],
    )

    projection = _projection(result)

    assert projection["recommended_match"]["decision"] == "MATCH"
    assert projection["recommended_match"]["offer_id"] == "shared-id"


def test_tender_projection_preserves_single_provider_recommended_match():
    result = _result(
        offer_provider="etm_ipro",
        matches=[_match("etm_ipro", "MATCH")],
    )

    projection = _projection(result)

    assert projection["recommended_match"]["decision"] == "MATCH"
    assert projection["recommended_match"]["offer_id"] == "shared-id"


def test_tender_projection_does_not_fallback_to_another_provider_review_match():
    result = _result(
        offer_provider="lemana_b2b",
        matches=[_match("etm_ipro", "REVIEW")],
        review_candidate=_match("etm_ipro", "REVIEW"),
    )

    projection = _projection(result)

    assert projection["recommended_match"] is None
    assert projection["decision"] == "OFFER"


def test_tender_projection_keeps_offer_id_fallback_when_provider_is_absent():
    result = _result(
        offer_provider=None,
        matches=[{"offer": {"offer_id": "shared-id"}, "decision": "MATCH", "rank": 1}],
    )

    projection = _projection(result)

    assert projection["recommended_match"]["decision"] == "MATCH"
