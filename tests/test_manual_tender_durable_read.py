from __future__ import annotations

import builtins
import json
import socket
from copy import deepcopy
from dataclasses import FrozenInstanceError
from decimal import Decimal
from itertools import combinations
from pathlib import Path

import pytest
from pydantic import ValidationError

from averon_import.services.manual_tenders.durable_read import (
    MAX_DURABLE_V2_BYTES, DurableTenderReadCode, DurableTenderReadError,
    DurableTenderSourcingRunV1, DurableTenderSourcingRunV2, read_tender_sourcing_run,
)
from averon_import.services.manual_tenders.parser import parse_unit_basis
from averon_import.services.manual_tenders.price_export import TenderPriceResolver
from averon_import.services.manual_tenders.repository import MAX_TENDER_RUN_BYTES, TenderWorkspaceRepository
from averon_import.services.manual_tenders.sourcing import TenderSourcingRunStore, canonical_tender_projection
from averon_import.services.sourcing.models import (
    HistorySafeMatchBasis, MatchDecision, MatchResult, Offer, ProductIntent,
    ProjectSourcingResult, SourcingResult, SourcingRouteMetadata, SourcingSourceMode,
)


ETM = "etm_ipro"
LEMANA = "lemana_b2b"
ROW_ID = "c" * 32
NOW = "2026-10-08T10:00:00+00:00"


def _ref(provider=ETM, offer_id="same-id"):
    return {"provider_key": provider, "offer_id": offer_id}


def _offer(provider=ETM, offer_id="same-id", amount="1234.5600"):
    source_id = f"item-{offer_id}"
    proof = {"source": "unproven"}
    if provider == ETM:
        proof = {"source": ETM, "source_item_id": source_id, "price_field": "pricewnds",
                 "catalog_version": "catalog-v1", "price_status": ""}
    elif provider == LEMANA:
        proof = {"source": LEMANA, "product_item": source_id, "mirror_revision": "d" * 64, "region_id": 1}
    return {"offer_reference": _ref(provider, offer_id), "source_item_id": source_id,
            "title": "Synthetic assembly", "article": "SKU-1", "manufacturer": "Maker", "brand": "Brand",
            "price": amount, "currency": "RUB", "price_unit": "шт", "availability": True,
            "availability_text": "Available", "url": "https://example.test/product/1", "provenance": proof}


def _match(offer):
    return {"offer_reference": deepcopy(offer["offer_reference"]), "decision": "MATCH", "rank": 1,
            "matched_attributes": ["article"], "supporting_attributes": ["manufacturer"],
            "conflicting_attributes": [], "missing_attributes": [],
            "deterministic_evidence": {"hard_contradiction": False, "preferred_differences": [],
                                       "model_evidence_source": "article"}, "explanation": "Exact identity"}


def _evidence(offer):
    provider = offer["offer_reference"]["provider_key"]
    complete = provider == ETM
    revision = {ETM: "m2a-etm-pricewnds-v1", LEMANA: "m2a-lemana-unproven-v1",
                "local_catalog": "m2a-local-unproven-v1"}.get(provider, "m2a-provider-unproven-v1")
    issues = [] if complete else ["PROVIDER_PRICE_BASIS_UNPROVEN", "VAT_BASIS_UNKNOWN"]
    if provider not in {ETM, LEMANA}:
        issues.append("PRICE_UNIT_UNTRUSTED")
    return {"offer_reference": deepcopy(offer["offer_reference"]), "amount": offer["price"],
            "currency": offer["currency"], "vat_basis": "GROSS_INCLUDING_VAT" if complete else "UNKNOWN",
            "price_unit": offer["price_unit"], "unit_family": "piece",
            "evidence_state": "COMPLETE" if complete else "INCOMPLETE", "issue_codes": issues,
            "basis_revision": revision}


def _outcome(provider, offers, *, state=None, failure=None):
    return {"provider_key": provider, "state": state or ("success" if offers else "empty"),
            "request_count": 1, "failure_category": failure,
            "affinity": {"environment": "", "region_id": "1" if provider == LEMANA else "",
                         "config_revision": "", "adapter_revision": ""},
            "catalog_version": "d" * 64 if provider == LEMANA else "catalog-v1",
            "offers_returned_count": len(offers),
            "retained_offer_references": [deepcopy(item["offer_reference"]) for item in offers]}


def _fixture(kind="multi"):
    offers = [_offer(), _offer(LEMANA)]
    keys = [ETM, LEMANA]
    if kind == "winner":
        offers = [_offer(offer_id="a", amount="100.000000000000000000000001"),
                  _offer(offer_id="b", amount="100.000000000000000000000002")]
        keys = [ETM]
    elif kind == "partial":
        offers = [_offer()]
    outcomes = [_outcome(provider, [item for item in offers if item["offer_reference"]["provider_key"] == provider])
                for provider in keys]
    if kind == "partial":
        outcomes[1] = _outcome(LEMANA, [], state="failure", failure="timeout")
    selection = {"state": "NO_SAFE_WINNER", "selected_reference": None,
                 "candidate_references": [deepcopy(item["offer_reference"]) for item in offers],
                 "reason_codes": ["COMMERCIAL_EVIDENCE_INCOMPLETE"], "selection_basis": None}
    if kind != "multi":
        selection.update(state="SELECTED", selected_reference=deepcopy(offers[0]["offer_reference"]),
                         reason_codes=[], selection_basis="LOWEST_COMPARABLE_PRICE" if kind == "winner"
                         else "SOLE_STRONGEST_IDENTITY")
    row = {"source_row_id": ROW_ID, "physical_excel_row": 2, "result_limit": 100,
           "outcomes": outcomes, "reused_provider_keys": [], "offers": offers,
           "recommended_offer_reference": deepcopy(offers[0]["offer_reference"]) if kind == "partial" else None,
           "review_candidate_reference": None,
           "matches": [_match(item) for item in offers], "commercial_evidence": [_evidence(item) for item in offers],
           "commercial_selection": selection}
    return {"schema_version": 2, "projection_policy": "decision-closure-v1", "run_id": "a" * 32, "tender_id": "b" * 32,
            "source_sha256": "f" * 64, "workspace_revision": 1, "status": "completed",
            "created_at": NOW, "started_at": NOW, "completed_at": NOW,
            "selection": {"provider_keys": keys}, "selected_source_row_ids": [ROW_ID], "rows": [row]}


def _encoded(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@pytest.mark.parametrize("transport", ["dict", "text", "bytes"])
def test_valid_multi_provider_same_offer_id_and_unknown_lemana_vat(transport):
    payload = _fixture()
    value = payload if transport == "dict" else _encoded(payload)
    if transport == "text":
        value = value.decode("utf-8")
    run = read_tender_sourcing_run(value)
    assert isinstance(run, DurableTenderSourcingRunV2)
    assert run.selection.provider_keys == (ETM, LEMANA)
    row = run.rows[0]
    assert [(item.offer_reference.provider_key, item.offer_reference.offer_id) for item in row.offers] == [
        (ETM, "same-id"), (LEMANA, "same-id")]
    assert len(row.matches) == len(row.commercial_evidence) == 2
    assert all(item.state.value == "success" for item in row.outcomes)
    assert row.commercial_evidence[0].evidence_state.value == "COMPLETE"
    assert row.commercial_evidence[1].evidence_state.value == "INCOMPLETE"
    assert row.commercial_evidence[1].vat_basis.value == "UNKNOWN"
    assert row.offers[1].provenance.region_id == 1
    assert row.commercial_selection.state.value == "NO_SAFE_WINNER"
    assert tuple(item.value for item in row.commercial_selection.reason_codes) == ("COMMERCIAL_EVIDENCE_INCOMPLETE",)
    assert not run.partial_failure
    # Test-only DTO -> JSON fixture -> reader; there is no production serializer.
    assert read_tender_sourcing_run(run.model_dump_json()) == run


def test_valid_winner_preserves_exact_decimal_and_stored_composite_selection():
    run = read_tender_sourcing_run(_fixture("winner"))
    row = run.rows[0]
    assert row.matches[0].decision == row.matches[1].decision == MatchDecision.MATCH
    assert all(item.evidence_state.value == "COMPLETE" for item in row.commercial_evidence)
    assert row.commercial_selection.state.value == "SELECTED"
    assert row.commercial_selection.selection_basis.value == "LOWEST_COMPARABLE_PRICE"
    assert row.commercial_selection.selected_reference == row.offers[0].offer_reference
    assert row.recommended_offer_reference is None  # Equal identity remains ambiguous despite the commercial winner.
    assert row.recommended_match is None
    assert type(row.offers[0].price) is Decimal
    assert row.offers[0].price < row.offers[1].price
    assert row.offers[1].price - row.offers[0].price == Decimal("0.000000000000000000000001")


def test_partial_failure_is_distinct_from_empty_or_unselected():
    payload = _fixture("partial")
    run = read_tender_sourcing_run(payload)
    assert run.partial_failure and run.rows[0].partial_failure
    assert run.rows[0].outcomes[1].state.value == "failure"
    assert run.rows[0].outcomes[1].failure_category.value == "timeout"
    assert run.rows[0].outcomes[1].retained_offer_references == ()
    assert len(run.rows[0].offers) == len(run.rows[0].commercial_evidence) == 1
    assert run.rows[0].commercial_selection.selected_reference.provider_key == ETM
    assert run.rows[0].recommended_match.offer_reference.provider_key == ETM
    assert run.rows[0].commercial_selection.selection_basis.value == "SOLE_STRONGEST_IDENTITY"
    empty = deepcopy(payload)
    empty["rows"][0]["outcomes"][1].update(state="empty", failure_category=None)
    assert not read_tender_sourcing_run(empty).partial_failure
    only = deepcopy(payload)
    only["selection"]["provider_keys"] = [ETM]
    only["rows"][0]["outcomes"].pop()
    assert not read_tender_sourcing_run(only).partial_failure


def test_canonical_ordering_immutability_and_defensive_ownership():
    payload = _fixture()
    expected = read_tender_sourcing_run(payload)
    payload["selection"]["provider_keys"].reverse()
    row = payload["rows"][0]
    for key in ("outcomes", "offers", "matches", "commercial_evidence"):
        row[key].reverse()
    row["commercial_selection"]["candidate_references"].reverse()
    row["reused_provider_keys"] = [LEMANA, ETM]
    run = read_tender_sourcing_run(payload)
    assert run.rows[0].offers == expected.rows[0].offers
    assert run.rows[0].commercial_selection == expected.rows[0].commercial_selection
    assert run.rows[0].reused_provider_keys == (ETM, LEMANA)
    payload["rows"][0]["offers"][0]["title"] = "mutated"
    assert run.rows[0].offers == expected.rows[0].offers
    for model, name, value in [(run, "status", "failed"), (run.selection, "provider_keys", ("other",)),
                               (run.rows[0].offers[0], "title", "mutated"),
                               (run.rows[0].commercial_evidence[0], "amount", Decimal(1))]:
        with pytest.raises(ValidationError):
            setattr(model, name, value)
    assert isinstance(run.rows[0].matches[0].matched_attributes, tuple)


def _corrupt(payload, case):
    row = payload["rows"][0]
    selected = row["commercial_selection"]
    if case == "duplicate-outcome": row["outcomes"].append(deepcopy(row["outcomes"][0]))
    elif case == "missing-outcome": row["outcomes"].pop()
    elif case == "foreign-outcome": row["outcomes"][1]["provider_key"] = "other"
    elif case == "duplicate-provider": payload["selection"]["provider_keys"].append(ETM)
    elif case == "duplicate-offer": row["offers"].append(deepcopy(row["offers"][0]))
    elif case == "absent-outcome-ref": row["outcomes"][0]["retained_offer_references"] = []
    elif case == "duplicate-outcome-ref": row["outcomes"][0]["retained_offer_references"] *= 2
    elif case == "foreign-outcome-ref": row["outcomes"][0]["retained_offer_references"][0] = _ref(LEMANA)
    elif case == "unknown-outcome-ref": row["outcomes"][0]["retained_offer_references"][0] = _ref(ETM, "absent")
    elif case in {"missing-matches", "missing-commercial_evidence"}: row[case.removeprefix("missing-")].pop()
    elif case in {"duplicate-matches", "duplicate-commercial_evidence"}: row[case.removeprefix("duplicate-")].append(deepcopy(row[case.removeprefix("duplicate-")][0]))
    elif case in {"foreign-matches", "foreign-commercial_evidence"}: row[case.removeprefix("foreign-")][0]["offer_reference"] = _ref("other")
    elif case == "wrong-provider-match": row["matches"][0]["offer_reference"] = _ref(LEMANA)
    elif case == "foreign-recommendation": row["recommended_offer_reference"] = _ref("other")
    elif case == "foreign-review": row["review_candidate_reference"] = _ref("other")
    elif case == "recommend-reject":
        row["recommended_offer_reference"] = _ref()
        row["matches"][0]["decision"] = "REJECT"
    elif case == "review-match": row["review_candidate_reference"] = _ref()
    elif case == "forged-amount": row["commercial_evidence"][0]["amount"] = "999"
    elif case == "forged-reference-amount":
        row["offers"][1]["price"] = row["commercial_evidence"][1]["amount"] = "2000"
        row["commercial_evidence"][0]["offer_reference"], row["commercial_evidence"][1]["offer_reference"] = (
            row["commercial_evidence"][1]["offer_reference"], row["commercial_evidence"][0]["offer_reference"])
    elif case == "forged-currency": row["commercial_evidence"][0]["currency"] = "USD"
    elif case == "forged-unit": row["commercial_evidence"][0].update(price_unit="кг", unit_family="kilogram")
    elif case == "forged-basis": row["commercial_evidence"][0]["basis_revision"] = "m2a-local-unproven-v1"
    elif case == "etm-wrong-source": row["offers"][0]["provenance"]["source_item_id"] = "other"
    elif case == "etm-net-field": row["offers"][0]["provenance"]["price_field"] = "price"
    elif case == "etm-price-status": row["offers"][0]["provenance"]["price_status"] = "PRICE_MISSING"
    elif case == "etm-catalog-conflict": row["offers"][0]["provenance"]["catalog_version"] = "different"
    elif case == "lemana-known-vat": row["commercial_evidence"][1].update(vat_basis="GROSS_INCLUDING_VAT")
    elif case == "lemana-wrong-item": row["offers"][1]["provenance"]["product_item"] = "other"
    elif case == "lemana-wrong-region": row["offers"][1]["provenance"]["region_id"] = 2
    elif case == "lemana-wrong-revision": row["offers"][1]["provenance"]["mirror_revision"] = "e" * 64
    elif case == "lemana-missing-unproven-issue": row["commercial_evidence"][1]["issue_codes"] = ["VAT_BASIS_UNKNOWN"]
    elif case == "conflict-contradiction": row["matches"][0]["deterministic_evidence"]["hard_contradiction"] = True
    elif case == "absent-candidate": selected["candidate_references"].append(_ref(ETM, "absent"))
    elif case == "duplicate-candidate": selected["candidate_references"] *= 2
    elif case == "selected-null": selected.update(state="SELECTED", reason_codes=[], selection_basis="LOWEST_COMPARABLE_PRICE")
    elif case == "selected-outside-candidates": selected.update(state="SELECTED", reason_codes=[], selection_basis="LOWEST_COMPARABLE_PRICE", selected_reference=_ref(ETM, "absent"))
    elif case == "selected-absent-offer":
        selected.update(state="SELECTED", reason_codes=[], selection_basis="LOWEST_COMPARABLE_PRICE", selected_reference=_ref(ETM, "absent"))
        selected["candidate_references"].append(_ref(ETM, "absent"))
    elif case == "sole-two-candidates": selected.update(state="SELECTED", reason_codes=[], selected_reference=_ref(), selection_basis="SOLE_STRONGEST_IDENTITY")
    elif case == "lowest-one-candidate": selected.update(state="SELECTED", reason_codes=[], selected_reference=_ref(), selection_basis="LOWEST_COMPARABLE_PRICE", candidate_references=[_ref()])
    elif case == "no-safe-selected": selected["selected_reference"] = _ref()
    elif case == "no-safe-basis": selected["selection_basis"] = "SOLE_STRONGEST_IDENTITY"
    elif case == "no-safe-no-reason": selected["reason_codes"] = []
    elif case == "selected-with-reason": selected.update(state="SELECTED", selected_reference=_ref(), selection_basis="LOWEST_COMPARABLE_PRICE")
    elif case == "duplicate-reason": selected["reason_codes"] *= 2
    elif case == "duplicate-issue": row["commercial_evidence"][1]["issue_codes"] *= 2
    elif case == "invalid-provider": payload["selection"]["provider_keys"][0] = "ETM / bad"
    elif case == "blank-id": row["offers"][0]["offer_reference"]["offer_id"] = "   "
    elif case == "boolean-count": row["outcomes"][0]["request_count"] = True
    elif case == "string-count": row["outcomes"][0]["request_count"] = "1"
    elif case == "negative-count": row["outcomes"][0]["request_count"] = -1
    elif case == "excess-count": row["outcomes"][0]["request_count"] = 1_000_001
    elif case == "success-failure-category": row["outcomes"][0]["failure_category"] = "timeout"
    elif case == "failed-with-offer": row["outcomes"][0].update(state="failure", failure_category="timeout")
    elif case == "failure-no-category": row["outcomes"][1].update(state="failure", offers_returned_count=0, retained_offer_references=[])
    elif case == "suppressed-requests": row["outcomes"][1].update(state="suppressed", offers_returned_count=0, retained_offer_references=[])
    elif case == "reused-unknown": row["reused_provider_keys"] = ["other"]
    elif case == "reused-duplicate": row["reused_provider_keys"] = [ETM, ETM]
    elif case == "outcome-result-limit": row["result_limit"] = 0
    elif case == "unselected-row": row["source_row_id"] = "e" * 32
    elif case == "duplicate-row": payload["rows"].append(deepcopy(row))
    elif case == "duplicate-selected-row": payload["selected_source_row_ids"] *= 2
    elif case == "missing-completed-row": payload["rows"] = []
    elif case == "invalid-date": payload["created_at"] = "yesterday"
    elif case == "naive-date": payload["created_at"] = "2026-10-08T00:00:00"
    elif case == "terminal-without-time": payload["completed_at"] = None
    elif case == "invalid-url": row["offers"][0]["url"] = "javascript:alert(1)"
    elif case == "url-credentials": row["offers"][0]["url"] = "https://user:password@example.test/item"
    elif case == "url-token": row["offers"][0]["url"] = "https://example.test/item?token=secret"
    elif case == "credential-text": row["offers"][0]["availability_text"] = "Authorization: Bearer private"
    else: raise AssertionError(case)


@pytest.mark.parametrize("case", [
    "duplicate-outcome", "missing-outcome", "foreign-outcome", "duplicate-provider", "duplicate-offer",
    "absent-outcome-ref", "duplicate-outcome-ref", "foreign-outcome-ref", "unknown-outcome-ref",
    "missing-matches", "duplicate-matches", "foreign-matches", "wrong-provider-match",
    "foreign-recommendation", "foreign-review", "recommend-reject", "review-match",
    "missing-commercial_evidence", "duplicate-commercial_evidence", "foreign-commercial_evidence",
    "forged-amount", "forged-reference-amount", "forged-currency", "forged-unit", "forged-basis",
    "etm-wrong-source", "etm-net-field", "etm-price-status", "etm-catalog-conflict", "lemana-known-vat",
    "lemana-wrong-item", "lemana-wrong-region", "lemana-wrong-revision", "lemana-missing-unproven-issue", "conflict-contradiction",
    "absent-candidate", "duplicate-candidate", "selected-null", "selected-outside-candidates",
    "selected-absent-offer", "sole-two-candidates", "lowest-one-candidate", "no-safe-selected", "no-safe-basis",
    "no-safe-no-reason", "selected-with-reason", "duplicate-reason", "duplicate-issue", "invalid-provider", "blank-id",
    "boolean-count", "string-count", "negative-count", "excess-count", "success-failure-category", "failed-with-offer",
    "failure-no-category", "suppressed-requests", "reused-unknown", "reused-duplicate", "outcome-result-limit",
    "unselected-row", "duplicate-row", "duplicate-selected-row", "missing-completed-row", "invalid-date",
    "naive-date", "terminal-without-time", "invalid-url", "url-credentials", "url-token", "credential-text",
])
def test_corruption_fails_closed_with_bounded_read_error(case):
    payload = _fixture()
    _corrupt(payload, case)
    before = deepcopy(payload)
    with pytest.raises(DurableTenderReadError) as caught:
        read_tender_sourcing_run(payload)
    assert len(str(caught.value)) <= 80
    assert len(caught.value.code.value) <= 40
    assert "private" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert payload == before


@pytest.mark.parametrize("path", [
    ("rows", 0, "outcomes", 0, "state"), ("rows", 0, "outcomes", 0, "failure_category"),
    ("rows", 0, "matches", 0, "decision"), ("rows", 0, "commercial_evidence", 0, "vat_basis"),
    ("rows", 0, "commercial_evidence", 0, "evidence_state"),
    ("rows", 0, "commercial_evidence", 1, "issue_codes", 0),
    ("rows", 0, "commercial_evidence", 0, "basis_revision"),
    ("rows", 0, "commercial_evidence", 0, "unit_family"),
    ("rows", 0, "commercial_selection", "state"), ("rows", 0, "commercial_selection", "reason_codes", 0),
    ("rows", 0, "commercial_selection", "selection_basis"), ("status",),
])
def test_every_closed_enum_rejects_unknown_values(path):
    payload = _fixture("winner" if path[-1] == "selection_basis" else "multi")
    if path[-1] == "failure_category":
        payload["rows"][0]["outcomes"][0].update(state="partial_success", failure_category="timeout")
    # Every starting fixture is valid; only the enum value below is corrupted.
    assert isinstance(read_tender_sourcing_run(payload), DurableTenderSourcingRunV2)
    obj = payload
    for part in path[:-1]: obj = obj[part]
    obj[path[-1]] = "UNKNOWN_NEW_ENUM"
    with pytest.raises(DurableTenderReadError):
        read_tender_sourcing_run(payload)


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "-1", "bad", "", "1_000", " 1 ",
                                     "1e101", "9" * 81, "0" * 121, float("nan"), float("inf"), 1.5, True])
@pytest.mark.parametrize("target", ["price", "amount"])
def test_invalid_and_float_prices_never_become_canonical_decimal(bad, target):
    payload = _fixture()
    key = "offers" if target == "price" else "commercial_evidence"
    payload["rows"][0][key][0][target] = bad
    with pytest.raises(DurableTenderReadError):
        read_tender_sourcing_run(payload)


def test_json_numeric_tokens_are_exact_decimal_not_float():
    payload = _fixture("winner")
    encoded = _encoded(payload).decode("utf-8").replace('"100.000000000000000000000001"', "100.000000000000000000000001")
    run = read_tender_sourcing_run(encoded)
    assert run.rows[0].offers[0].price == Decimal("100.000000000000000000000001")
    assert type(run.rows[0].offers[0].price) is Decimal
    assert type(run.rows[0].commercial_evidence[0].amount) is Decimal


@pytest.mark.parametrize("version", [3, 999, 2.0, "2", None, False])
def test_unsupported_version_is_not_interpreted_as_known_schema(version):
    payload = _fixture()
    payload["schema_version"] = version
    with pytest.raises(DurableTenderReadError) as caught:
        read_tender_sourcing_run(payload)
    assert caught.value.code == DurableTenderReadCode.UNSUPPORTED_VERSION


@pytest.mark.parametrize("payload", ["{", "[]", "null", b"\xff", {}, {"rows": []}, Path("run.json"),
                                    '{"schema_version":2,"schema_version":2}'])
def test_missing_version_and_malformed_or_non_payload_input_fail_closed(payload):
    with pytest.raises(DurableTenderReadError):
        read_tender_sourcing_run(payload)


@pytest.mark.parametrize("duplicate", ["top", "offer"])
def test_v2_json_duplicate_members_fail_closed(duplicate):
    encoded = _encoded(_fixture()).decode("utf-8")
    if duplicate == "top": encoded = encoded.replace('"schema_version":2', '"schema_version":2,"schema_version":2')
    else: encoded = encoded.replace('"title":"Synthetic assembly"', '"title":"hidden","title":"Synthetic assembly"', 1)
    with pytest.raises(DurableTenderReadError):
        read_tender_sourcing_run(encoded)


@pytest.mark.parametrize("location", ["top", "selection", "row", "outcome", "affinity", "offer", "reference",
                                         "provenance", "match", "deterministic", "evidence", "commercial_selection"])
@pytest.mark.parametrize("field", ["password", "token", "Authorization", "client_secret", "settings",
                                     "request_headers", "response_headers", "attributes", "data_provenance", "ai_evidence"])
def test_secret_shaped_or_arbitrary_extra_fields_are_rejected_everywhere(location, field):
    payload = _fixture()
    row = payload["rows"][0]
    objects = {"top": payload, "selection": payload["selection"], "row": row, "outcome": row["outcomes"][0],
               "affinity": row["outcomes"][0]["affinity"], "offer": row["offers"][0],
               "reference": row["offers"][0]["offer_reference"], "provenance": row["offers"][0]["provenance"],
               "match": row["matches"][0], "deterministic": row["matches"][0]["deterministic_evidence"],
               "evidence": row["commercial_evidence"][0], "commercial_selection": row["commercial_selection"]}
    objects[location][field] = {"secret": "NEVER_EXPOSE_THIS"}
    with pytest.raises(DurableTenderReadError) as caught:
        read_tender_sourcing_run(payload)
    assert "NEVER_EXPOSE_THIS" not in str(caught.value)


@pytest.mark.parametrize("path,limit", [
    (("selection", "provider_keys", 0), 100), (("rows", 0, "offers", 0, "offer_reference", "offer_id"), 180),
    (("rows", 0, "offers", 0, "source_item_id"), 180), (("rows", 0, "offers", 0, "title"), 320),
    (("rows", 0, "offers", 0, "article"), 100), (("rows", 0, "offers", 0, "manufacturer"), 100),
    (("rows", 0, "offers", 0, "brand"), 100), (("rows", 0, "offers", 0, "currency"), 3),
    (("rows", 0, "offers", 0, "price_unit"), 80), (("rows", 0, "offers", 0, "availability_text"), 120),
    (("rows", 0, "offers", 0, "url"), 500), (("rows", 0, "outcomes", 0, "catalog_version"), 120),
    (("rows", 0, "outcomes", 0, "affinity", "environment"), 24),
    (("rows", 0, "outcomes", 0, "affinity", "region_id"), 80),
    (("rows", 0, "outcomes", 0, "affinity", "config_revision"), 120),
    (("rows", 0, "outcomes", 0, "affinity", "adapter_revision"), 120),
    (("rows", 0, "matches", 0, "explanation"), 240),
    (("rows", 0, "matches", 0, "matched_attributes", 0), 40),
    (("rows", 0, "offers", 0, "provenance", "price_field"), 40),
    (("rows", 0, "offers", 0, "provenance", "price_status"), 80), (("created_at",), 40),
])
def test_explicit_string_bounds(path, limit):
    payload = _fixture()
    obj = payload
    for part in path[:-1]: obj = obj[part]
    obj[path[-1]] = "x" * (limit + 1)
    with pytest.raises(DurableTenderReadError):
        read_tender_sourcing_run(payload)


@pytest.mark.parametrize("key,limit", [("outcomes", 8), ("offers", 400), ("matches", 400),
                                        ("commercial_evidence", 400), ("reused_provider_keys", 8)])
def test_collection_limits(key, limit):
    payload = _fixture()
    row = payload["rows"][0]
    item = ETM if key == "reused_provider_keys" else row[key][0]
    row[key] = [deepcopy(item) for _ in range(limit + 1)]
    with pytest.raises(DurableTenderReadError):
        read_tender_sourcing_run(payload)


@pytest.mark.parametrize("location", ["providers", "rows", "selected_rows", "outcome_refs", "candidates", "issues", "attributes", "reasons"])
def test_remaining_collection_limits(location):
    payload = _fixture()
    row = payload["rows"][0]
    if location == "providers": payload["selection"]["provider_keys"] = [f"p{i}" for i in range(9)]
    elif location == "rows": payload["rows"] = [deepcopy(row) for _ in range(501)]
    elif location == "selected_rows": payload["selected_source_row_ids"] = [f"{i:032x}" for i in range(501)]
    elif location == "outcome_refs": row["outcomes"][0]["retained_offer_references"] = [_ref(ETM, str(i)) for i in range(101)]
    elif location == "candidates": row["commercial_selection"]["candidate_references"] = [_ref(ETM, str(i)) for i in range(401)]
    elif location == "issues": row["commercial_evidence"][0]["issue_codes"] = ["PRICE_MISSING"] * 11
    elif location == "attributes": row["matches"][0]["matched_attributes"] = [f"a{i}" for i in range(17)]
    else: row["commercial_selection"]["reason_codes"] = ["LOWEST_PRICE_TIED"] * 6
    with pytest.raises(DurableTenderReadError):
        read_tender_sourcing_run(payload)


def _large_fixture(count=400):
    payload = _fixture()
    row = payload["rows"][0]
    keys = [ETM, "fixture_b", "fixture_c", "fixture_d"]
    offers = [_offer(keys[i // 100], f"offer-{i:03d}", "123.4500") for i in range(count)]
    payload["selection"]["provider_keys"] = keys
    row.update(offers=offers, matches=[_match(item) for item in offers],
               commercial_evidence=[_evidence(item) for item in offers],
               outcomes=[_outcome(key, [item for item in offers if item["offer_reference"]["provider_key"] == key]) for key in keys])
    row["commercial_selection"]["candidate_references"] = [deepcopy(item["offer_reference"]) for item in offers]
    return payload


def _near_budget_fixture():
    payload = _large_fixture()
    for offer in payload["rows"][0]["offers"]:
        offer["title"] = "😀" * 195
    remaining = MAX_DURABLE_V2_BYTES - len(_encoded(payload))
    assert 0 < remaining < 4000
    for offer in payload["rows"][0]["offers"]:
        extra = min(remaining, 320 - len(offer["title"]))
        offer["title"] += "x" * extra
        remaining -= extra
    assert remaining == 0
    return payload


def test_400_offer_snapshot_and_multibyte_near_budget_have_storage_headroom():
    payload = _large_fixture()
    baseline_size = len(_encoded(payload))
    assert len(read_tender_sourcing_run(payload).rows[0].offers) == 400
    assert baseline_size < MAX_DURABLE_V2_BYTES
    # Real compact facts fill the byte budget; no whitespace padding or raw
    # payload fields are used to simulate the near-limit snapshot.
    payload = _near_budget_fixture()
    near = _encoded(payload)
    assert len(near) == MAX_DURABLE_V2_BYTES
    run = read_tender_sourcing_run(near)
    canonical_size = len(run.model_dump_json().encode("utf-8"))
    assert canonical_size == MAX_DURABLE_V2_BYTES == 786432
    assert MAX_TENDER_RUN_BYTES == 1048576
    assert MAX_TENDER_RUN_BYTES - len(near) == 262144
    with pytest.raises(DurableTenderReadError) as caught:
        read_tender_sourcing_run(near + b" ")
    assert caught.value.code == DurableTenderReadCode.TOO_LARGE
    # The compact dictionary must fit too, independently of source whitespace.
    for offer in payload["rows"][0]["offers"]: offer["title"] = "😀" * 320
    assert len(_encoded(payload)) > MAX_DURABLE_V2_BYTES
    with pytest.raises(DurableTenderReadError):
        read_tender_sourcing_run(payload)


def test_default_affinity_fields_are_restored_under_the_compact_wire_budget():
    payload = _near_budget_fixture()
    for outcome in payload["rows"][0]["outcomes"]:
        outcome["affinity"] = {}
    raw_size = len(_encoded(payload))
    assert raw_size < MAX_DURABLE_V2_BYTES
    # Expanded friendly field names are no longer the canonical wire format.
    # Restored defaults fit in the lossless positional representation.
    next(offer for offer in payload["rows"][0]["offers"] if len(offer["title"]) < 320)["title"] += "x"
    assert len(_encoded(payload)) < MAX_DURABLE_V2_BYTES
    run = read_tender_sourcing_run(payload)
    assert all(outcome.affinity.environment == "" for outcome in run.rows[0].outcomes)


def test_per_record_compact_budget_and_obsolete_aggregate_400_bound_removed():
    payload = _fixture()
    offer = payload["rows"][0]["offers"][0]
    offer.update(title="😀" * 320, article="😀" * 100, manufacturer="😀" * 100, brand="😀" * 100)
    assert len(_encoded(payload)) < MAX_DURABLE_V2_BYTES
    with pytest.raises(DurableTenderReadError): read_tender_sourcing_run(payload)
    payload = _large_fixture()
    row = deepcopy(_fixture("partial")["rows"][0])
    row["source_row_id"] = "e" * 32
    row["outcomes"] = [_outcome(key, row["offers"] if key == ETM else []) for key in payload["selection"]["provider_keys"]]
    payload["rows"].append(row)
    payload["selected_source_row_ids"].append(row["source_row_id"])
    assert sum(len(item.offers) for item in read_tender_sourcing_run(payload).rows) == 401


def _current_v1_run(tmp_path, *, history=False):
    repository = TenderWorkspaceRepository(tmp_path / "data")
    workspace_path = repository.workspace_root / ("b" * 32)
    workspace_path.mkdir()
    store = TenderSourcingRunStore(repository)
    mode = "one_c_only" if history else "provider_only"
    source = {"source_row_id": ROW_ID, "excel_row": 2, "row_type": "item", "name": "Assembly",
              "quantity": "2", "quantity_trusted": True, "raw_unit": "шт", "unit_basis": parse_unit_basis("шт")}
    provenance = {"source": ETM, "source_item_id": "item-1", "price_field": "pricewnds", "catalog_version": "v1"}
    route = None  # Current provider_only results really have no history route.
    provider = ETM
    if history:
        provider = "one_c_history"
        provenance = {"source": provider, "source_kind": "historical_purchase", "history_item_id": "item-1",
                      "selected_event_id": "event-1", "purchase_date": "2025-01-02", "snapshot_version": "history-v1",
                      "price_basis": "gross_including_vat", "effective_unit_price_gross": "1234.5600"}
        route = SourcingRouteMetadata(source_mode=SourcingSourceMode.ONE_C_ONLY,
                                      final_source_kind="historical_purchase", history_outcome="SAFE_MATCH",
                                      history_safe_basis=HistorySafeMatchBasis.EXACT_ARTICLE,
                                      history_catalog_version="history-v1", history_selected_event_id="event-1",
                                      history_purchase_date="2025-01-02")
    offer = Offer(offer_id="legacy-offer", provider=provider, source_item_id="item-1", title="Assembly",
                  article="SKU-1", price=Decimal("1234.5600"), currency="RUB", price_unit="шт", data_provenance=provenance)
    match = MatchResult(offer=offer, decision=MatchDecision.MATCH, rank=1, matched_attributes=["article"])
    result = ProjectSourcingResult(positions_total=1, positions_processed=1, positions_matched=1,
                                  source_mode=SourcingSourceMode(mode), results=[SourcingResult(
                                      intent=ProductIntent(source_row_id=ROW_ID, source_text="Assembly", article="SKU-1"),
                                      offers=[offer], recommended_offer=offer, match_results=[match], route=route)])
    rows = canonical_tender_projection(result.model_dump(mode="json"), [source], [ROW_ID])
    running = store.create_running(workspace_path, {"tender_id": "b" * 32, "source_sha256": "f" * 64, "revision": 1},
                                   source_mode=mode, provider=None, selected_ids=[ROW_ID],
                                   history_catalog_version="history-v1" if history else None)
    run_path = workspace_path / "runs" / f'{running["run_id"]}.json'
    assert json.loads(run_path.read_bytes())["schema_version"] == 1
    store.complete(workspace_path, running["run_id"], summary={"positions_total": 1, "positions_processed": 1,
                   "positions_matched": 1, "positions_review": 0, "positions_without_offers": 0},
                   catalog_version="v1", history_catalog_version="history-v1" if history else None, rows=rows)
    return store, workspace_path, run_path, source


@pytest.mark.parametrize("history", [False, True])
def test_real_current_v1_writer_reader_recommendation_route_and_export_are_unchanged(tmp_path, history):
    store, workspace, path, source = _current_v1_run(tmp_path, history=history)
    before = path.read_bytes()
    stat = path.stat()
    legacy = store.get_public(workspace, "b" * 32, path.stem)
    decoded = read_tender_sourcing_run(before)
    assert isinstance(decoded, DurableTenderSourcingRunV1)
    assert decoded.payload == legacy == json.loads(before)
    assert decoded.payload["schema_version"] == 1
    row = decoded.payload["rows"][0]
    assert row["recommended_offer"] == legacy["rows"][0]["recommended_offer"]
    assert row["recommended_match"] == legacy["rows"][0]["recommended_match"]
    assert row["recommended_match"]["offer_id"] == "legacy-offer"
    assert row["route"] == legacy["rows"][0]["route"]
    if history:
        assert row["route"]["history_safe_basis"] == "EXACT_ARTICLE"
        assert row["provider_source"]["provider"] == "one_c_history"
    else:
        assert row["route"] == {}
        assert row["provider_source"]["provider"] == ETM
    resolver = TenderPriceResolver()
    original = resolver.resolve(source, legacy["rows"][0], legacy, include_historical_prices=True)
    reread = resolver.resolve(source, row, decoded.payload, include_historical_prices=True)
    assert original == reread
    assert original.eligible
    assert original.source_unit_price == Decimal("1234.560000")
    assert original.total_price == Decimal("2469.12")
    assert original.historical == history
    if history:
        assert resolver.resolve(source, row, decoded.payload, include_historical_prices=False).reason_code == "HISTORICAL_PRICE_NOT_INCLUDED"
    exported = decoded.payload
    exported["rows"][0]["recommended_offer"]["price"] = "1"
    assert decoded.payload == legacy
    with pytest.raises(FrozenInstanceError): decoded._payload = {}
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == stat.st_mtime_ns
    assert set(item.name for item in path.parent.iterdir()) == {path.name}


@pytest.mark.parametrize("version", [1, 1.0, True])
def test_v1_discriminator_preserves_exact_legacy_equality_check(tmp_path, version):
    path = tmp_path / "legacy.json"
    payload = {"schema_version": version, "recommended_offer": {"old_float": 1.5}, "legacy_extra": "kept"}
    path.write_bytes(_encoded(payload))
    assert read_tender_sourcing_run(path.read_bytes()).payload == TenderSourcingRunStore._read_path(path)
    payload.pop("schema_version")
    path.write_bytes(_encoded(payload))
    with pytest.raises(ValueError): TenderSourcingRunStore._read_path(path)
    with pytest.raises(DurableTenderReadError): read_tender_sourcing_run(path.read_bytes())


def test_v1_original_byte_limit_is_preserved_even_when_float_reencoding_is_larger(tmp_path):
    template = '{"schema_version":1,"legacy_price":1e100,"padding":""}'
    encoded = template.replace('"padding":""', '"padding":"' + "x" * (MAX_TENDER_RUN_BYTES - len(template)) + '"').encode()
    assert len(encoded) == MAX_TENDER_RUN_BYTES
    path = tmp_path / "legacy-limit.json"
    path.write_bytes(encoded)
    legacy = TenderSourcingRunStore._read_path(path)
    assert len(_encoded(legacy)) > MAX_TENDER_RUN_BYTES
    before = path.stat()
    assert read_tender_sourcing_run(encoded).payload == legacy
    assert read_tender_sourcing_run(encoded.decode()).payload == legacy
    assert path.read_bytes() == encoded
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    with pytest.raises(DurableTenderReadError): read_tender_sourcing_run(encoded + b" ")


def test_v1_dictionary_decimal_size_accounting_keeps_legacy_string_representation():
    payload = {"schema_version": 1, "legacy_price": Decimal("1234.5600"), "padding": ""}
    baseline = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode()
    payload["padding"] = "x" * (MAX_TENDER_RUN_BYTES - len(baseline))
    assert read_tender_sourcing_run(payload).payload == payload
    payload["padding"] += "x"
    with pytest.raises(DurableTenderReadError): read_tender_sourcing_run(payload)


def test_reader_has_no_filesystem_network_provider_or_decision_engine_calls(monkeypatch):
    import averon_import.services.sourcing.provider_commercial as commercial
    import averon_import.services.sourcing.provider_commercial_selection as selection
    import averon_import.services.sourcing.provider_matching as matching
    from averon_import.services.sourcing.matching import OfferMatcher
    from averon_import.services.sourcing.provider_matching import ProviderMatchEvaluator
    from averon_import.services.sourcing.providers.execution import ProviderRunner

    def forbidden(*args, **kwargs):
        raise AssertionError("reader crossed an I/O or decision-engine boundary")

    encoded = [_encoded(_fixture(kind)) for kind in ("partial", "multi", "winner")]
    for reason in ("LOWEST_PRICE_TIED", "COMMERCIAL_BASIS_NOT_COMPARABLE"):
        payload = _fixture("winner")
        _stored_no_safe_winner(payload["rows"][0], [reason])
        encoded.append(_encoded(payload))
    with monkeypatch.context() as guard:
        guard.setattr(builtins, "open", forbidden)
        guard.setattr(Path, "open", forbidden)
        guard.setattr(socket, "socket", forbidden)
        guard.setattr(socket, "getaddrinfo", forbidden)
        guard.setattr(OfferMatcher, "match", forbidden)
        guard.setattr(ProviderMatchEvaluator, "evaluate", forbidden)
        guard.setattr(matching, "_recommendation_quality", forbidden)
        guard.setattr(matching, "_unique_identity_recommendation", forbidden)
        guard.setattr(ProviderRunner, "run", forbidden)
        guard.setattr(commercial, "resolve_commercial_evidence", forbidden)
        for resolver in commercial.COMMERCIAL_EVIDENCE_RESOLVERS.values():
            guard.setattr(type(resolver), "resolve", forbidden)
        guard.setattr(commercial, "compare_commercial_evidence", forbidden)
        guard.setattr(selection, "select_provider_commercial_winner", forbidden)
        runs = [read_tender_sourcing_run(payload) for payload in encoded]
        assert runs[0].partial_failure
        assert [run.rows[0].commercial_selection.reason_codes[0].value for run in runs[3:]] == [
            "LOWEST_PRICE_TIED", "COMMERCIAL_BASIS_NOT_COMPARABLE"]
        assert read_tender_sourcing_run({"schema_version": 1, "rows": []}).payload == {"schema_version": 1, "rows": []}


def test_reader_does_not_recompute_stored_identity_cohort_or_lowest_price():
    # Structural facts are validated; market/identity decisions are not silently
    # recalculated on read. Future reconciliation is a separate phase.
    payload = _fixture("winner")
    payload["rows"][0]["commercial_selection"]["selected_reference"] = _ref(ETM, "b")
    assert read_tender_sourcing_run(payload).rows[0].commercial_selection.selected_reference.offer_id == "b"


def test_stored_review_candidate_is_read_without_reconstructing_identity_policy():
    payload = _fixture()
    row = payload["rows"][0]
    row["matches"][0].update(decision="REVIEW", missing_attributes=["power"])
    row["matches"][1].update(decision="REJECT", conflicting_attributes=["article"])
    row["matches"][1]["deterministic_evidence"]["hard_contradiction"] = True
    row["review_candidate_reference"] = _ref()
    row["commercial_selection"].update(candidate_references=[], reason_codes=["NO_IDENTITY_CANDIDATE"])
    for name in ("offers", "matches", "commercial_evidence"):
        row[name] = row[name][:1]
    row["outcomes"][1]["retained_offer_references"] = []
    read = read_tender_sourcing_run(payload).rows[0]
    assert read.recommended_match is None
    assert read.review_candidate.decision == MatchDecision.REVIEW
    assert read.review_candidate.offer_reference.provider_key == ETM


def _stored_no_safe_winner(row, reasons):
    row["commercial_selection"].update(state="NO_SAFE_WINNER", selected_reference=None,
                                        selection_basis=None, reason_codes=list(reasons))


def _stored_unusable_evidence(row, index, state):
    """Test-only coherent scalar facts, without invoking a provider resolver."""
    offer = row["offers"][index]
    evidence = row["commercial_evidence"][index]
    evidence.update(evidence_state=state, vat_basis="UNKNOWN",
                    issue_codes=["PROVIDER_PRICE_BASIS_UNPROVEN", "VAT_BASIS_UNKNOWN"])
    if state == "INCOMPLETE":
        offer["provenance"]["price_field"] = "price"
    else:
        offer["provenance"]["source_item_id"] = "unmatched-item"
        evidence["currency"] = ""
        evidence["issue_codes"] += ["PROVENANCE_MISMATCH", "CURRENCY_UNKNOWN"]


@pytest.mark.parametrize("decision", ["MATCH", "LIKELY_MATCH", "ALTERNATIVE"])
def test_selected_complete_candidates_accept_all_identity_eligible_decisions(decision):
    payload = _fixture("winner")
    for match in payload["rows"][0]["matches"]:
        match["decision"] = decision
    run = read_tender_sourcing_run(payload)
    row = run.rows[0]
    assert row.commercial_selection.state.value == "SELECTED"
    assert all(match.decision.value == decision for match in row.matches)
    assert all(item.evidence_state.value == "COMPLETE" for item in row.commercial_evidence)


@pytest.mark.parametrize("state", ["INCOMPLETE", "INVALID"])
@pytest.mark.parametrize("index", [0, 1], ids=["selected", "other-candidate"])
@pytest.mark.parametrize("transport", ["dict", "bytes"])
def test_selected_rejects_unusable_evidence_for_selected_or_other_candidate(state, index, transport):
    payload = _fixture("winner")
    row = payload["rows"][0]
    _stored_unusable_evidence(row, index, state)
    selected = deepcopy(row["commercial_selection"])
    reason = f"COMMERCIAL_EVIDENCE_{state}"
    _stored_no_safe_winner(row, [reason])
    # The same offer/evidence/match facts pass every existing scalar/provenance
    # invariant. Only declaring SELECTED makes this snapshot contradictory.
    valid = read_tender_sourcing_run(payload)
    assert valid.rows[0].commercial_evidence[index].evidence_state.value == state
    row["commercial_selection"] = selected
    before = deepcopy(payload)
    with pytest.raises(DurableTenderReadError):
        read_tender_sourcing_run(payload if transport == "dict" else _encoded(payload))
    assert payload == before
    assert len(row["commercial_selection"]["candidate_references"]) == 2


@pytest.mark.parametrize("state", ["SELECTED", "NO_SAFE_WINNER"])
@pytest.mark.parametrize("index", [0, 1], ids=["selected-or-first", "other-candidate"])
@pytest.mark.parametrize("decision", ["REVIEW", "REJECT"])
def test_commercial_candidates_and_selected_reference_reject_review_or_reject(state, index, decision):
    payload = _fixture("winner" if state == "SELECTED" else "multi")
    assert isinstance(read_tender_sourcing_run(payload), DurableTenderSourcingRunV2)
    row = payload["rows"][0]
    match = row["matches"][index]
    match["decision"] = decision
    if decision == "REVIEW":
        match["missing_attributes"] = ["power"]
    else:
        match["conflicting_attributes"] = ["article"]
        match["deterministic_evidence"]["hard_contradiction"] = True
    before = deepcopy(payload)
    with pytest.raises(DurableTenderReadError):
        read_tender_sourcing_run(payload)
    assert payload == before


@pytest.mark.parametrize("reason", ["COMMERCIAL_EVIDENCE_INCOMPLETE", "COMMERCIAL_EVIDENCE_INVALID",
                                     "NO_IDENTITY_CANDIDATE"])
def test_no_safe_winner_rejects_reasons_without_corresponding_candidate_facts(reason):
    payload = _fixture("winner")
    _stored_no_safe_winner(payload["rows"][0], [reason])
    with pytest.raises(DurableTenderReadError):
        read_tender_sourcing_run(payload)


@pytest.mark.parametrize("state", ["INCOMPLETE", "INVALID"])
def test_reason_requires_candidate_evidence_not_merely_other_returned_evidence(state):
    payload = _fixture("winner")
    row = payload["rows"][0]
    _stored_unusable_evidence(row, 1, state)
    _stored_no_safe_winner(row, [f"COMMERCIAL_EVIDENCE_{state}"])
    assert isinstance(read_tender_sourcing_run(payload), DurableTenderSourcingRunV2)
    row["commercial_selection"]["candidate_references"] = [_ref(ETM, "a")]
    with pytest.raises(DurableTenderReadError):
        read_tender_sourcing_run(payload)


@pytest.mark.parametrize("reasons", [
    ["COMMERCIAL_EVIDENCE_INCOMPLETE"], ["COMMERCIAL_EVIDENCE_INVALID"],
    ["COMMERCIAL_EVIDENCE_INCOMPLETE", "COMMERCIAL_EVIDENCE_INVALID"],
])
def test_reason_correlation_accepts_present_states_without_requiring_reverse_implication(reasons):
    payload = _fixture("winner")
    row = payload["rows"][0]
    _stored_unusable_evidence(row, 0, "INCOMPLETE")
    _stored_unusable_evidence(row, 1, "INVALID")
    _stored_no_safe_winner(row, reasons)
    read = read_tender_sourcing_run(payload).rows[0]
    assert tuple(code.value for code in read.commercial_selection.reason_codes) == tuple(sorted(reasons))


@pytest.mark.parametrize("reason", ["COMMERCIAL_EVIDENCE_INCOMPLETE", "COMMERCIAL_EVIDENCE_INVALID"])
def test_evidence_failure_reason_rejects_empty_candidates(reason):
    payload = _fixture("winner")
    row = payload["rows"][0]
    row["commercial_selection"]["candidate_references"] = []
    _stored_no_safe_winner(row, [reason])
    with pytest.raises(DurableTenderReadError):
        read_tender_sourcing_run(payload)


def test_empty_candidates_preserve_stored_no_identity_reason_without_inferring_identity_policy():
    payload = _fixture("winner")
    row = payload["rows"][0]
    row["commercial_selection"]["candidate_references"] = []
    _stored_no_safe_winner(row, ["NO_IDENTITY_CANDIDATE"])
    for name in ("offers", "matches", "commercial_evidence"):
        row[name] = []
    row["outcomes"][0]["retained_offer_references"] = []
    read = read_tender_sourcing_run(payload).rows[0]
    assert read.commercial_selection.candidate_references == ()
    assert tuple(code.value for code in read.commercial_selection.reason_codes) == ("NO_IDENTITY_CANDIDATE",)


@pytest.mark.parametrize("reason", ["LOWEST_PRICE_TIED", "COMMERCIAL_BASIS_NOT_COMPARABLE"])
@pytest.mark.parametrize("count", [0, 1])
@pytest.mark.parametrize("transport", ["dict", "bytes"])
def test_comparison_reason_rejects_fewer_than_two_candidates(reason, count, transport):
    payload = _fixture("winner")
    row = payload["rows"][0]
    row["commercial_selection"]["candidate_references"] = row["commercial_selection"]["candidate_references"][:count]
    _stored_no_safe_winner(row, [reason])
    before = deepcopy(payload)
    with pytest.raises(DurableTenderReadError):
        read_tender_sourcing_run(payload if transport == "dict" else _encoded(payload))
    assert payload == before


@pytest.mark.parametrize("reason", ["LOWEST_PRICE_TIED", "COMMERCIAL_BASIS_NOT_COMPARABLE"])
@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("transport", ["dict", "bytes"])
def test_comparison_reason_accepts_multiple_complete_candidates_without_proving_conclusion(reason, count, transport):
    payload = _fixture("winner")
    row = payload["rows"][0]
    if count == 3:
        offer = _offer(offer_id="c", amount="200")
        row["offers"].append(offer)
        row["matches"].append(_match(offer))
        row["commercial_evidence"].append(_evidence(offer))
        row["outcomes"][0]["retained_offer_references"].append(_ref(ETM, "c"))
        row["outcomes"][0]["offers_returned_count"] = 3
        row["commercial_selection"]["candidate_references"].append(_ref(ETM, "c"))
    _stored_no_safe_winner(row, [reason])
    before = deepcopy(payload)
    read = read_tender_sourcing_run(payload if transport == "dict" else _encoded(payload)).rows[0]
    # The stored offers have unequal prices and the same proved commercial
    # basis. Acceptance must not depend on either comparison conclusion.
    assert len(read.commercial_selection.candidate_references) == count
    assert all(item.evidence_state.value == "COMPLETE" for item in read.commercial_evidence)
    assert tuple(code.value for code in read.commercial_selection.reason_codes) == (reason,)
    assert read.commercial_selection.selected_reference is None
    assert payload == before


@pytest.mark.parametrize("reason", ["LOWEST_PRICE_TIED", "COMMERCIAL_BASIS_NOT_COMPARABLE"])
@pytest.mark.parametrize("state", ["INCOMPLETE", "INVALID"])
@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("transport", ["dict", "bytes"])
def test_comparison_reason_rejects_any_unusable_candidate_evidence(reason, state, index, transport):
    payload = _fixture("winner")
    row = payload["rows"][0]
    _stored_unusable_evidence(row, index, state)
    _stored_no_safe_winner(row, [f"COMMERCIAL_EVIDENCE_{state}"])
    assert isinstance(read_tender_sourcing_run(payload), DurableTenderSourcingRunV2)
    _stored_no_safe_winner(row, [reason])
    with pytest.raises(DurableTenderReadError):
        read_tender_sourcing_run(payload if transport == "dict" else _encoded(payload))


@pytest.mark.parametrize("reasons", [
    reasons for count in range(2, 6) for reasons in combinations((
        "NO_IDENTITY_CANDIDATE", "COMMERCIAL_EVIDENCE_INCOMPLETE", "COMMERCIAL_EVIDENCE_INVALID",
        "COMMERCIAL_BASIS_NOT_COMPARABLE", "LOWEST_PRICE_TIED",
    ), count) if set(reasons) != {"COMMERCIAL_EVIDENCE_INCOMPLETE", "COMMERCIAL_EVIDENCE_INVALID"}
])
@pytest.mark.parametrize("transport", ["dict", "bytes"])
def test_no_safe_winner_rejects_every_mixed_cross_phase_reason_family(reasons, transport):
    payload = _fixture("winner")
    row = payload["rows"][0]
    if "NO_IDENTITY_CANDIDATE" in reasons:
        row["commercial_selection"]["candidate_references"] = []
    else:
        if "COMMERCIAL_EVIDENCE_INCOMPLETE" in reasons:
            _stored_unusable_evidence(row, 0, "INCOMPLETE")
        if "COMMERCIAL_EVIDENCE_INVALID" in reasons:
            _stored_unusable_evidence(row, 1, "INVALID")
    _stored_no_safe_winner(row, reasons)
    with pytest.raises(DurableTenderReadError):
        read_tender_sourcing_run(payload if transport == "dict" else _encoded(payload))


def test_correlation_does_not_replace_a_stored_weaker_identity_cohort():
    payload = _fixture("winner")
    row = payload["rows"][0]
    row["matches"][1]["decision"] = "ALTERNATIVE"
    row["matches"][1]["deterministic_evidence"]["preferred_differences"] = ["brand"]
    row["recommended_offer_reference"] = _ref(ETM, "a")
    row["commercial_selection"].update(selected_reference=_ref(ETM, "b"), candidate_references=[_ref(ETM, "b")],
                                        selection_basis="SOLE_STRONGEST_IDENTITY")
    read = read_tender_sourcing_run(payload).rows[0]
    assert read.matches[0].decision == MatchDecision.MATCH
    assert read.commercial_selection.selected_reference.offer_id == "b"
    assert read.commercial_selection.candidate_references == (read.matches[1].offer_reference,)
