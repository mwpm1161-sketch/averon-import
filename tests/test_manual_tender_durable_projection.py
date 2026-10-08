from __future__ import annotations

import builtins
import json
import socket
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from functools import lru_cache
from pathlib import Path

import pytest

import averon_import.services.sourcing.provider_commercial as commercial_module
import averon_import.services.sourcing.provider_commercial_selection as selection_module
import averon_import.services.sourcing.provider_matching as matching_module
from averon_import.services.manual_tenders.durable_projection import (
    DurableProjectionCode, DurableProjectionError, DurableRowIdentity, DurableRunMetadata,
    build_durable_tender_sourcing_run_v2, encode_tender_sourcing_run_v2, project_durable_provider_row_v2,
)
from averon_import.services.manual_tenders.durable_read import (
    MAX_DURABLE_V2_BYTES, DurableTenderReadError, DurableTenderSourcingRowV2, DurableTenderSourcingRunV2,
    _canonical_decimal, _size_default, read_tender_sourcing_run,
)
from averon_import.services.manual_tenders.durable_wire import MAX_RETAINED_OFFERS, _wire_payload
from averon_import.services.sourcing.models import MatchDecision, MatchResult, Offer, ProductIntent
from averon_import.services.sourcing.provider_matching import ProviderMatchEvaluation
from averon_import.services.sourcing.providers.contracts import (
    ProviderAffinity, ProviderFailureCategory, ProviderOfferReference, ProviderSearchOutcome,
    ProviderSearchState, ProviderSelection, index_offers_by_provider_identity,
)
from averon_import.services.sourcing.providers.execution import ProviderExecutionResult, ProviderRunner

ETM, LEMANA = "etm_ipro", "lemana_b2b"
NOW = datetime(2026, 10, 8, 10, tzinfo=timezone.utc)


def _offer(provider, index=0, *, amount="1234.5600", row_number=1):
    source = f"catalog-product-{row_number:06d}-{index:02d}"
    proof = {"source": provider, "Authorization": "Bearer must-never-persist"}
    if provider == ETM:
        proof.update(source_item_id=source, price_field="pricewnds", catalog_version="catalog-2026-10",
                     price_status="")
    elif provider == LEMANA:
        proof.update(product_item=source, mirror_revision="d" * 64, region_id=1)
    return Offer(provider=provider, offer_id=f"offer-{row_number:06d}-{index:02d}", source_item_id=source,
                 title="Клапан регулирующий двухходовой DN25 PN16 с электроприводом 230 В",
                 article=f"CTRL-DN25-PN16-{row_number:06d}", manufacturer="Industrial Controls", brand="Control Systems",
                 price=Decimal(amount), currency="RUB", price_unit="шт", availability=True,
                 availability_text="В наличии на региональном складе",
                 url=f"https://example.test/catalog/control-valves/product-{row_number:06d}-{index:02d}",
                 data_provenance=proof, retrieved_at=NOW,
                 attributes={"password": "must-never-persist", "raw_payload": {"token": "must-never-persist"}})


def _chain(kind="partial", *, row_number=1, extra=0, reverse=False, amount="1234.5600", reused=False,
           lemana_state=None, etm_partial=False):
    source_id = f"{row_number:032x}"
    providers = [ETM, LEMANA]
    offers = [_offer(ETM, amount=amount, row_number=row_number)]
    if kind in {"multi", "review", "no_identity", "lemana_candidate"}:
        offers.append(_offer(LEMANA, amount=amount, row_number=row_number))
    elif kind in {"winner", "tie"}:
        offers.append(_offer(ETM, 1, amount=amount if kind == "tie" else "1250.00", row_number=row_number))
    if kind == "closure":
        offers.append(_offer(ETM, 1, row_number=row_number))
    offers.extend(_offer(ETM, i + 2, row_number=row_number) for i in range(extra))
    if reverse:
        offers.reverse()
        providers.reverse()
    provider_selection = ProviderSelection(provider_keys=tuple(providers))
    outcomes = []
    for key in provider_selection.provider_keys:
        items = tuple(offer for offer in offers if offer.provider == key)
        state = ProviderSearchState.SUCCESS if items else ProviderSearchState.FAILURE
        if key == LEMANA and lemana_state is not None:
            state = lemana_state
        if key == ETM and etm_partial:
            state = ProviderSearchState.PARTIAL_SUCCESS
        no_requests = state in {ProviderSearchState.NOT_ATTEMPTED, ProviderSearchState.SUPPRESSED}
        failed = state in {ProviderSearchState.FAILURE, ProviderSearchState.PARTIAL_SUCCESS}
        outcomes.append(ProviderSearchOutcome(
            provider_key=key, state=state, offers=items, request_count=0 if no_requests or reused and key == ETM else 1,
            failure_category=ProviderFailureCategory.TIMEOUT if failed else None,
            affinity=ProviderAffinity(environment="production-ru", region_id="1" if key == LEMANA else "",
                                      config_revision="provider-config-2026-10", adapter_revision="outcome-adapter-m1b-v1"),
            catalog_version="d" * 64 if key == LEMANA else "catalog-2026-10",
        ))
    execution = ProviderExecutionResult(selection=provider_selection, result_limit=100, outcomes=tuple(outcomes),
        offers=tuple(index_offers_by_provider_identity(offers).values()), reused_provider_keys=(ETM,) if reused else ())
    matches = []
    for offer in offers:
        decision = MatchDecision.ALTERNATIVE if int(offer.offer_id[-2:]) >= 2 else MatchDecision.MATCH
        if kind == "review":
            decision = MatchDecision.REVIEW if offer.provider == ETM else MatchDecision.REJECT
        elif kind == "no_identity":
            decision = MatchDecision.REJECT
        elif kind == "closure" and offer.offer_id.endswith("-01"):
            decision = MatchDecision.REVIEW
        elif kind == "lemana_candidate" and offer.provider == ETM:
            decision = MatchDecision.ALTERNATIVE
        matches.append(MatchResult(
            offer=offer, decision=decision, rank=1, matched_attributes=["article"],
            supporting_attributes=["manufacturer", "brand"], missing_attributes=["power"] if decision == MatchDecision.REVIEW else [],
            conflicting_attributes=["article"] if decision == MatchDecision.REJECT else [],
            deterministic_evidence={"hard_contradiction": decision == MatchDecision.REJECT,
                "preferred_differences": [], "model_evidence_source": "article", "token": "must-never-persist"},
            explanation="Article and manufacturer agree with the supplied product identity.",
        ))
    matches = tuple(matches)
    matching = ProviderMatchEvaluation(intent=ProductIntent(source_row_id=source_id, source_text="Control valve"),
        execution=execution, matches=matches,
        recommended_offer_reference=matching_module._unique_reference(matching_module._unique_identity_recommendation(matches)),
        review_candidate_reference=matching_module._unique_reference(matching_module._unique_review_candidate(matches)))
    commercial = commercial_module.evaluate_provider_commercial_evidence(matching)
    selection = selection_module.select_provider_commercial_winner(commercial)
    return execution, matching, commercial, selection


def _project(chain, row_number=1):
    execution, matching, commercial, selection = chain
    return project_durable_provider_row_v2(DurableRowIdentity(source_row_id=f"{row_number:032x}", physical_excel_row=row_number + 1),
        execution=execution, matching=matching, commercial=commercial, selection=selection)


def _metadata(rows, status="completed"):
    return DurableRunMetadata(run_id="a" * 32, tender_id="b" * 32, source_sha256="f" * 64,
        workspace_revision=1, status=status, created_at=NOW.isoformat(), started_at=NOW.isoformat(),
        completed_at=None if status == "running" else NOW.isoformat(),
        selected_source_row_ids=tuple(row.source_row_id for row in rows))


def _run(rows, status="completed"):
    return build_durable_tender_sourcing_run_v2(_metadata(rows, status),
        ProviderSelection(provider_keys=(LEMANA, ETM)), tuple(rows))


@pytest.mark.parametrize("kind", ["winner", "tie", "multi", "partial", "review", "no_identity", "lemana_candidate"])
def test_runtime_projection_encoder_reader_round_trip_and_closed_policy(kind):
    chain = _chain(kind)
    row = _project(chain)
    run = _run((row,))
    encoded = encode_tender_sourcing_run_v2(run)
    assert read_tender_sourcing_run(encoded) == run
    assert encode_tender_sourcing_run_v2(read_tender_sourcing_run(encoded)) == encoded
    assert b"must-never-persist" not in encoded
    assert b"Authorization" not in encoded and b"password" not in encoded
    assert run.projection_policy == "decision-closure-v1"
    if kind == "multi":
        assert row.offers[0].offer_reference.offer_id == row.offers[1].offer_reference.offer_id
        assert len(row.commercial_selection.candidate_references) == 2
    if kind == "partial":
        assert row.partial_failure and run.partial_failure
        assert row.outcomes[1].offers_returned_count == 0 and not row.outcomes[1].retained_offer_references
    if kind == "review":
        assert row.review_candidate.offer_reference == row.offers[0].offer_reference
        assert not row.commercial_selection.candidate_references
    if kind == "no_identity":
        assert not row.offers
        assert sum(item.offers_returned_count for item in row.outcomes) == 2
    if kind == "lemana_candidate":
        assert tuple(item.offer_reference.provider_key for item in row.offers) == (LEMANA,)


def test_exact_union_retains_recommendation_review_and_entire_cohort_not_first_n():
    chain = _chain("closure", extra=35, reverse=True)
    row = _project(chain)
    expected = set(chain[3].candidate_references) | {chain[1].recommended_offer_reference, chain[1].review_candidate_reference}
    assert {(ref.provider_key, ref.offer_id) for ref in expected} == {offer.offer_reference.ordering_key for offer in row.offers}
    assert row.outcomes[0].offers_returned_count == 37
    assert len(row.outcomes[0].retained_offer_references) == 2
    assert len(row.matches) == len(row.commercial_evidence) == len(row.offers) == 2


def test_equivalent_runtime_orders_produce_identical_wire_bytes():
    forward = _project(_chain("multi", extra=3))
    backward = _project(_chain("multi", extra=3, reverse=True))
    assert forward == backward
    assert encode_tender_sourcing_run_v2(_run((forward,))) == encode_tender_sourcing_run_v2(_run((backward,)))


@pytest.mark.parametrize("values", [("1234.5600", "123456E-2"), ("0.000", "-0"), ("1E100", "10E99"),
                                   ("10E100", "100E99"), ("1E-100", "10E-101")])
def test_canonical_decimal_is_exact_context_independent_and_value_deterministic(values):
    if values[-1] == "10E-101":
        with pytest.raises(ValueError): _canonical_decimal(Decimal(values[-1]))
        values = values[:1]
    with localcontext() as context:
        context.prec = 2
        outputs = [_canonical_decimal(Decimal(value)) for value in values]
    assert len(set(outputs)) == 1
    assert Decimal(outputs[0]) == Decimal(values[0])
    if values[0] not in {"10E100", "1E100", "1E-100"}:
        runs = [_run((_project(_chain(amount=value)),)) for value in values]
        assert len({encode_tender_sourcing_run_v2(run) for run in runs}) == 1


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "1E101", "1E-101", "1" * 81])
def test_invalid_decimal_bounds_remain_closed(value):
    with pytest.raises(ValueError): _canonical_decimal(Decimal(value))


@pytest.mark.parametrize("phase", [0, 1, 2, 3])
def test_mixed_chains_reject_even_when_composite_references_overlap(phase):
    chain = list(_chain("winner"))
    foreign = _chain("winner", amount="1235.56")
    chain[phase] = foreign[phase]
    with pytest.raises(DurableProjectionError) as caught: _project(chain)
    assert caught.value.code == DurableProjectionCode.CORRUPT_INPUT
    assert str(caught.value) == "Durable v2 projection could not be produced."


@pytest.mark.parametrize("mutation", ["selection-state", "selection-reference", "commercial", "matching", "execution", "presence", "float", "unwitnessed"])
def test_bypassed_freeze_or_missing_validation_witness_rejects(mutation):
    chain = list(_chain("winner"))
    if mutation == "selection-state": object.__setattr__(chain[3], "state", selection_module.CommercialSelectionState.NO_SAFE_WINNER)
    elif mutation == "selection-reference": object.__setattr__(chain[3].selected_reference, "offer_id", "foreign")
    elif mutation == "commercial": object.__setattr__(chain[2], "evidence", ())
    elif mutation == "matching": object.__setattr__(chain[1], "recommended_offer_reference", ProviderOfferReference.from_offer(chain[0].offers[0]))
    elif mutation == "execution": chain[0] = chain[0].model_copy(update={"result_limit": 1})
    elif mutation == "presence": object.__setattr__(chain[0].offers[0], "__pydantic_fields_set__", set())
    elif mutation == "float": object.__setattr__(chain[0].offers[0], "price", 1234.56)
    else: object.__delattr__(chain[3], "_validated_decision")
    with pytest.raises(DurableProjectionError): _project(chain)


@pytest.mark.parametrize("phase", [0, 1, 2, 3])
def test_arbitrary_runtime_dictionaries_are_not_accepted(phase):
    chain = list(_chain())
    chain[phase] = {}
    with pytest.raises(DurableProjectionError): _project(chain)


@pytest.mark.parametrize("error_type", [RuntimeError, OSError, AssertionError])
def test_unexpected_snapshot_errors_are_bounded_and_hide_raw_messages(monkeypatch, error_type):
    chain = _chain()
    def failed_snapshot(_self): raise error_type("Authorization: Bearer upstream-secret C:/private/path")
    monkeypatch.setattr(selection_module.ProviderCommercialSelection, "_validated_snapshot", failed_snapshot)
    with pytest.raises(DurableProjectionError) as caught: _project(chain)
    assert caught.value.code == DurableProjectionCode.CORRUPT_INPUT
    assert caught.value.args == ("Durable v2 projection could not be produced.",)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


@pytest.mark.parametrize("status", ["running", "completed", "failed", "interrupted"])
def test_run_metadata_preserves_lifecycle_semantics(status):
    run = _run((_project(_chain()),), status)
    assert read_tender_sourcing_run(encode_tender_sourcing_run_v2(run)) == run


def test_encoder_and_builder_require_typed_validated_boundaries():
    row = _project(_chain())
    run = _run((row,))
    for bad in ({}, run.model_dump(), run.model_copy(update={"schema_version": 1}), run.model_copy(update={"status": "unknown"})):
        with pytest.raises(DurableProjectionError): encode_tender_sourcing_run_v2(bad)
    for metadata, selection, rows in [({}, run.selection, (row,)), (_metadata((row,)), {}, (row,)),
                                      (_metadata((row,)), run.selection, [row])]:
        with pytest.raises(DurableProjectionError): build_durable_tender_sourcing_run_v2(metadata, selection, rows)


@pytest.mark.parametrize("case", ["returned-zero", "returned-bool", "returned-string", "returned-negative", "retained-over-returned",
    "returned-over-limit", "empty-nonzero", "failure-nonzero", "foreign-ref", "missing-offer", "extra-offer", "unknown-policy"])
def test_reader_fail_closed_returned_retained_and_closure_correlations(case):
    run = _run((_project(_chain("multi")),))
    payload = run.model_dump(mode="json")
    row = payload["rows"][0]
    outcome = row["outcomes"][0]
    if case == "returned-zero": outcome["offers_returned_count"] = 0
    elif case == "returned-bool": outcome["offers_returned_count"] = True
    elif case == "returned-string": outcome["offers_returned_count"] = "1"
    elif case == "returned-negative": outcome["offers_returned_count"] = -1
    elif case == "retained-over-returned": outcome["retained_offer_references"] *= 2
    elif case == "returned-over-limit": row["result_limit"] = 1; outcome["offers_returned_count"] = 2
    elif case == "empty-nonzero": outcome["state"] = "empty"
    elif case == "failure-nonzero": outcome.update(state="failure", failure_category="timeout")
    elif case == "foreign-ref": outcome["retained_offer_references"][0]["provider_key"] = LEMANA
    elif case == "missing-offer": row["offers"].pop()
    elif case == "extra-offer": row["commercial_selection"]["candidate_references"] = [row["offers"][0]["offer_reference"]]
    else: payload["projection_policy"] = "arbitrary"
    with pytest.raises(DurableTenderReadError): read_tender_sourcing_run(payload)


@pytest.mark.parametrize("case", ["root-extra", "row-extra", "offer-extra", "proof-extra", "match-extra", "evidence-extra",
    "negative-ref", "bool-ref", "foreign-ref", "null-ref", "duplicate-ref", "unknown-wire", "secret", "duplicate-key"])
def test_closed_wire_layout_and_reference_indices_reject_corruption(case):
    payload = json.loads(encode_tender_sourcing_run_v2(_run((_project(_chain("multi")),))))
    row = payload["rows"][0]
    if case == "root-extra": payload["extra"] = 1
    elif case == "row-extra": row.append(1)
    elif case == "offer-extra": row[5][0].append(1)
    elif case == "proof-extra": row[5][0][12].append(1)
    elif case == "match-extra": row[6][0].append(1)
    elif case == "evidence-extra": row[9][0].append(1)
    elif case == "negative-ref": row[6][0][0] = -1
    elif case == "bool-ref": row[6][0][0] = True
    elif case == "foreign-ref": row[6][0][0] = 100
    elif case == "null-ref": row[6][0][0] = None
    elif case == "duplicate-ref": row[10][2].append(row[10][2][0])
    elif case == "unknown-wire": payload["wire_format"] = "unknown"
    elif case == "secret": row[5][0][2] = "Authorization: Bearer secret"
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    if case == "duplicate-key": raw = raw.replace(b'"schema_version":2', b'"schema_version":2,"schema_version":2')
    with pytest.raises(DurableTenderReadError): read_tender_sourcing_run(raw)


@lru_cache(maxsize=2)
def _capacity_rows(retained):
    return tuple(_project(_chain("partial" if retained == 1 else "multi", row_number=i, extra=2), i) for i in range(1, 501))


@pytest.mark.parametrize("count,retained,expected_bytes", [(370, 1, 432217), (370, 2, 734507), (500, 1, 583927)])
def test_realistic_capacity_envelopes_and_more_than_400_retained_records(count, retained, expected_bytes):
    rows = _capacity_rows(retained)[:count]
    run = _run(rows)
    encoded = encode_tender_sourcing_run_v2(run)
    assert len(encoded) == expected_bytes <= MAX_DURABLE_V2_BYTES
    assert sum(len(row.offers) for row in run.rows) == count * retained
    assert read_tender_sourcing_run(encoded) == run
    print(f"capacity {count}x{retained}: {len(encoded)} bytes, {count*retained} retained")


def _raw_wire_size(rows):
    payload = dict(schema_version=2, projection_policy="decision-closure-v1", **_metadata(rows).model_dump(mode="python"),
        selection=ProviderSelection(provider_keys=(ETM, LEMANA)).model_dump(mode="python"),
        rows=[row.model_dump(mode="python") for row in rows])
    return len(json.dumps(_wire_payload(payload), ensure_ascii=False, separators=(",", ":"), allow_nan=False,
                          default=_size_default, sort_keys=True).encode())


@lru_cache(maxsize=1)
def _near_budget_rows():
    rows = [row.model_dump(mode="python") for row in _capacity_rows(2)[:370]]
    remaining = MAX_DURABLE_V2_BYTES - _raw_wire_size(_capacity_rows(2)[:370])
    assert remaining > 0
    for row in rows:
        for name, field, limit in (("offers", "title", 320), ("matches", "explanation", 240)):
            for item in row[name]:
                count = min(remaining, limit - len(item[field]))
                item[field] += "x" * count
                remaining -= count
    assert remaining == 0
    return tuple(DurableTenderSourcingRowV2.model_validate(row) for row in rows)


def test_exact_byte_budget_and_first_over_budget_are_all_or_nothing():
    rows = _near_budget_rows()
    run = _run(rows)
    encoded = encode_tender_sourcing_run_v2(run)
    assert len(encoded) == MAX_DURABLE_V2_BYTES == 786432
    assert read_tender_sourcing_run(encoded) == run
    data = [row.model_dump(mode="python") for row in rows]
    item = next(item for row in data for item in row["offers"] if len(item["title"]) < 320)
    item["title"] += "x"
    over = tuple(DurableTenderSourcingRowV2.model_validate(row) for row in data)
    assert _raw_wire_size(over) == 786433
    with pytest.raises(DurableProjectionError) as caught: _run(over)
    assert caught.value.code == DurableProjectionCode.TOO_LARGE
    bypass = run.model_copy(update={"rows": over})
    with pytest.raises(DurableProjectionError) as caught: encode_tender_sourcing_run_v2(bypass)
    assert caught.value.code == DurableProjectionCode.TOO_LARGE
    with pytest.raises(DurableTenderReadError): read_tender_sourcing_run(encoded + b" ")
    print("near-budget: 786432 bytes, 740 retained; first over: 786433 bytes, 740 retained")


def test_near_worst_legal_strings_fail_cleanly_on_global_bytes():
    data = [row.model_dump(mode="python") for row in _capacity_rows(1)]
    for row in data:
        row["offers"][0].update(title="Ж" * 320, article="A" * 100, manufacturer="M" * 100, brand="B" * 100,
                               availability_text="S" * 120, url="https://example.test/" + "u" * 479)
    rows = tuple(DurableTenderSourcingRowV2.model_validate(row) for row in data)
    size = _raw_wire_size(rows)
    assert size > MAX_DURABLE_V2_BYTES
    with pytest.raises(DurableProjectionError) as caught: _run(rows)
    assert caught.value.code == DurableProjectionCode.TOO_LARGE
    print(f"near-worst 500x1: {size} bytes, 500 retained, explicit TOO_LARGE")


def test_500_two_candidate_rows_reject_on_bytes_without_dropping_decision_facts():
    rows = _capacity_rows(2)
    size = _raw_wire_size(rows)
    assert size > MAX_DURABLE_V2_BYTES
    with pytest.raises(DurableProjectionError) as caught: _run(rows)
    assert caught.value.code == DurableProjectionCode.TOO_LARGE
    assert sum(len(row.commercial_selection.candidate_references) for row in rows) == 1000
    print(f"capacity 500x2: {size} bytes, 1000 retained, explicit TOO_LARGE")


def test_global_record_allocation_guard_is_separate_from_runtime_cap():
    row = _project(_chain("multi"))
    # Each row remains legal; deliberately repeated rows fail the preflight
    # count before attempting any serialization or run correlation.
    large = row.model_copy(update={"offers": row.offers * 100})
    rows = (large,) * 21
    assert sum(len(item.offers) for item in rows) > MAX_RETAINED_OFFERS == 4096
    with pytest.raises(DurableProjectionError) as caught: _run(rows)
    assert caught.value.code == DurableProjectionCode.TOO_LARGE


def test_projection_encoding_and_read_do_not_call_decision_engines_or_io(monkeypatch):
    from averon_import.services.sourcing.matching import OfferMatcher
    chains = [_chain(kind) for kind in ("winner", "multi", "partial", "review", "no_identity")]
    def forbidden(*args, **kwargs): raise AssertionError("unexpected recomputation or I/O")
    with monkeypatch.context() as guard:
        for obj, name in [(builtins, "open"), (Path, "open"), (socket, "socket"), (socket, "getaddrinfo"),
            (ProviderRunner, "run"), (OfferMatcher, "match"), (Offer, "model_dump"),
            (matching_module.ProviderMatchEvaluator, "evaluate"), (matching_module, "_recommendation_quality"),
            (matching_module, "_unique_identity_recommendation"), (matching_module, "_unique_review_candidate"),
            (commercial_module, "resolve_commercial_evidence"), (commercial_module, "evaluate_provider_commercial_evidence"),
            (commercial_module, "compare_commercial_evidence"), (selection_module, "select_provider_commercial_winner"),
            (selection_module, "_decision"), (selection_module, "_strongest_identity_cohort")]:
            guard.setattr(obj, name, forbidden)
        for resolver in commercial_module.COMMERCIAL_EVIDENCE_RESOLVERS.values():
            guard.setattr(type(resolver), "resolve", forbidden)
        for chain in chains:
            run = _run((_project(chain),))
            assert read_tender_sourcing_run(encode_tender_sourcing_run_v2(run)) == run


def test_reused_result_preserves_zero_outbound_requests_and_reused_keys():
    row = _project(_chain(reused=True))
    assert row.reused_provider_keys == (ETM,)
    assert row.outcomes[0].request_count == 0 and row.outcomes[0].offers_returned_count == 1
    assert read_tender_sourcing_run(encode_tender_sourcing_run_v2(_run((row,)))).rows[0] == row


@pytest.mark.parametrize("state", [ProviderSearchState.EMPTY, ProviderSearchState.NOT_ATTEMPTED,
                                  ProviderSearchState.SUPPRESSED, ProviderSearchState.FAILURE])
def test_zero_returned_outcomes_preserve_approved_state_and_request_semantics(state):
    row = _project(_chain(lemana_state=state))
    outcome = row.outcomes[1]
    assert outcome.state == state and outcome.offers_returned_count == 0
    assert not outcome.retained_offer_references
    assert outcome.request_count == (0 if state in {ProviderSearchState.NOT_ATTEMPTED, ProviderSearchState.SUPPRESSED} else 1)
    assert read_tender_sourcing_run(encode_tender_sourcing_run_v2(_run((row,)))).rows[0] == row


def test_partial_success_preserves_returned_count_and_failure_category():
    row = _project(_chain(etm_partial=True))
    outcome = row.outcomes[0]
    assert outcome.state == ProviderSearchState.PARTIAL_SUCCESS and outcome.offers_returned_count == 1
    assert outcome.failure_category == ProviderFailureCategory.TIMEOUT
    assert read_tender_sourcing_run(encode_tender_sourcing_run_v2(_run((row,)))).rows[0] == row


@pytest.mark.parametrize("status", ["running", "failed", "interrupted"])
def test_partial_evaluation_run_can_retain_selected_but_unevaluated_rows(status):
    row = _project(_chain())
    metadata = _metadata((row,), status).model_copy(update={"selected_source_row_ids": (row.source_row_id, f'{2:032x}')})
    run = build_durable_tender_sourcing_run_v2(metadata, ProviderSelection(provider_keys=(ETM, LEMANA)), (row,))
    assert len(run.selected_source_row_ids) == 2 and len(run.rows) == 1
    assert read_tender_sourcing_run(encode_tender_sourcing_run_v2(run)) == run


def _large_chain():
    keys = (ETM, LEMANA, "fixture_c", "fixture_d")
    offers = [_offer(key, index) for key in keys for index in range(100)]
    selection = ProviderSelection(provider_keys=keys)
    outcomes = tuple(ProviderSearchOutcome(provider_key=key, state=ProviderSearchState.SUCCESS,
        offers=tuple(offer for offer in offers if offer.provider == key), request_count=1,
        affinity=ProviderAffinity(region_id="1" if key == LEMANA else ""),
        catalog_version="d" * 64 if key == LEMANA else "catalog-2026-10") for key in selection.provider_keys)
    execution = ProviderExecutionResult(selection=selection, result_limit=100, outcomes=outcomes,
        offers=tuple(index_offers_by_provider_identity(offers).values()), reused_provider_keys=())
    matches = tuple(MatchResult(offer=offer, decision=MatchDecision.MATCH, rank=1,
        matched_attributes=["article"], supporting_attributes=["manufacturer", "brand"],
        deterministic_evidence={"hard_contradiction": False, "preferred_differences": [], "model_evidence_source": "article"},
        explanation="Article and manufacturer agree with the supplied product identity.") for offer in offers)
    matching = ProviderMatchEvaluation(intent=ProductIntent(source_row_id=f'{1:032x}', source_text="Control valve"),
        execution=execution, matches=matches, recommended_offer_reference=None, review_candidate_reference=None)
    commercial = commercial_module.evaluate_provider_commercial_evidence(matching)
    return execution, matching, commercial, selection_module.select_provider_commercial_winner(commercial)


def test_full_legal_400_candidate_cohort_survives_and_oversize_never_shrinks_it():
    chain = _large_chain()
    row = _project(chain)
    assert len(row.offers) == len(row.commercial_selection.candidate_references) == 400
    assert tuple(ref.ordering_key for ref in row.commercial_selection.candidate_references) == tuple(
        ref.ordering_key for ref in chain[3].candidate_references)
    run = build_durable_tender_sourcing_run_v2(_metadata((row,)), chain[0].selection, (row,))
    encoded = encode_tender_sourcing_run_v2(run)
    assert read_tender_sourcing_run(encoded) == run
    print(f"large cohort: {len(encoded)} bytes, 400 retained/candidates")
    rows = tuple(row.model_copy(update={"source_row_id": f'{i:032x}', "physical_excel_row": i + 1}) for i in range(1, 5))
    with pytest.raises(DurableProjectionError) as caught:
        build_durable_tender_sourcing_run_v2(_metadata(rows), chain[0].selection, rows)
    assert caught.value.code == DurableProjectionCode.TOO_LARGE
    assert all(len(item.commercial_selection.candidate_references) == 400 for item in rows)


def test_current_v1_store_all_lifecycle_paths_never_call_v2(monkeypatch, tmp_path):
    import averon_import.services.manual_tenders.durable_projection as projection
    from averon_import.services.manual_tenders.repository import TenderWorkspaceRepository
    from averon_import.services.manual_tenders.sourcing import TenderSourcingRunStore
    def forbidden(*args, **kwargs): raise AssertionError("v2 production activation")
    for name in ("project_durable_provider_row_v2", "build_durable_tender_sourcing_run_v2", "encode_tender_sourcing_run_v2"):
        monkeypatch.setattr(projection, name, forbidden)
    repository = TenderWorkspaceRepository(tmp_path / "data")
    workspace = repository.workspace_root / ("b" * 32)
    workspace.mkdir()
    store = TenderSourcingRunStore(repository)
    def running():
        return store.create_running(workspace, {"tender_id": "b" * 32, "source_sha256": "f" * 64, "revision": 1},
            source_mode="provider_only", provider=ETM, selected_ids=[f'{1:032x}'], history_catalog_version=None)
    complete, failed, recovered = running(), running(), running()
    store.complete(workspace, complete["run_id"], summary={}, catalog_version=None, history_catalog_version=None, rows=[])
    store.fail(workspace, failed["run_id"], code="SOURCING_FAILED", progress_current=0, progress_total=1)
    TenderSourcingRunStore(repository)
    files = list((workspace / "runs").iterdir())
    assert len(files) == 3 and all(path.suffix == ".json" for path in files)
    values = [json.loads(path.read_bytes()) for path in files]
    assert {value["status"] for value in values} == {"completed", "failed", "interrupted"}
    assert all(value["schema_version"] == 1 for value in values)
    assert all(read_tender_sourcing_run(path.read_bytes()).schema_version == 1 for path in files)
