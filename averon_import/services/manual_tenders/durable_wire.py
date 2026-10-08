"""Private, lossless JSON layouts for the inactive decision-closure v2 DTO.

Positions are closed by compact-records-v1; references index a row's complete
retained offer table. No compression, pooling, I/O or decision logic.
"""

from __future__ import annotations

from averon_import.services.sourcing.providers.contracts import MAX_PROVIDER_OUTCOME_OFFERS, MAX_PROVIDER_SELECTION_SIZE
from averon_import.services.sourcing.providers.execution import MAX_PROVIDER_EXECUTION_OFFERS
from .parser import MAX_ACTUAL_ITEMS

WIRE_FORMAT = "compact-records-v1"
PROJECTION_POLICY = "decision-closure-v1"
# 768 KiB / 192 record allocation units. This is an independent anti-DoS
# ceiling, not an estimate that a 4096-record run will fit the byte budget.
MAX_RETAINED_OFFERS = 4096

_RUN = ("schema_version", "projection_policy", "run_id", "tender_id", "source_sha256",
        "workspace_revision", "status", "created_at", "started_at", "completed_at",
        "selection", "selected_source_row_ids", "rows")
_ROW = ("source_row_id", "physical_excel_row", "result_limit", "outcomes", "reused_provider_keys",
        "offers", "matches", "recommended_offer_reference", "review_candidate_reference",
        "commercial_evidence", "commercial_selection")
_OUTCOME = ("provider_key", "state", "request_count", "failure_category", "affinity", "catalog_version",
            "offers_returned_count", "retained_offer_references")
_AFFINITY = ("environment", "region_id", "config_revision", "adapter_revision")
_REFERENCE = ("provider_key", "offer_id")
_OFFER = ("offer_reference", "source_item_id", "title", "article", "manufacturer", "brand", "price",
          "currency", "price_unit", "availability", "availability_text", "url", "provenance")
_PROOF = {
    "etm_ipro": ("source", "source_item_id", "price_field", "catalog_version", "price_status"),
    "lemana_b2b": ("source", "product_item", "mirror_revision", "region_id"),
    "unproven": ("source",),
}
_MATCH = ("offer_reference", "decision", "rank", "matched_attributes", "supporting_attributes",
          "conflicting_attributes", "missing_attributes", "deterministic_evidence", "explanation")
_DETERMINISTIC = ("hard_contradiction", "preferred_differences", "model_evidence_source")
_EVIDENCE = ("offer_reference", "amount", "currency", "vat_basis", "price_unit", "unit_family",
             "evidence_state", "issue_codes", "basis_revision")
_SELECTION = ("state", "selected_reference", "candidate_references", "reason_codes", "selection_basis")


def _pack(value: dict, fields: tuple[str, ...]) -> list:
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError("invalid closed record")
    return [value[name] for name in fields]


def _unpack(value: object, fields: tuple[str, ...]) -> dict:
    if type(value) is not list or len(value) != len(fields):
        raise ValueError("invalid closed record")
    return dict(zip(fields, value))


def _list(value: object, limit: int) -> list:
    if type(value) is not list or len(value) > limit:
        raise ValueError("invalid bounded records")
    return value


def _wire_payload(run: dict) -> dict:
    """Only used with a validated DTO's explicit model projection."""
    _pack(run, _RUN)
    output = dict(run, wire_format=WIRE_FORMAT)
    rows = []
    for source in run["rows"]:
        row = dict(source)
        references = [offer["offer_reference"] for offer in row["offers"]]
        indices = {(ref["provider_key"], ref["offer_id"]): i for i, ref in enumerate(references)}

        def ref(value):
            return None if value is None else indices[(value["provider_key"], value["offer_id"])]

        outcomes = []
        for value in row["outcomes"]:
            item = dict(value)
            item["affinity"] = _pack(item["affinity"], _AFFINITY)
            item["retained_offer_references"] = [ref(v) for v in item["retained_offer_references"]]
            outcomes.append(_pack(item, _OUTCOME))
        row["outcomes"] = outcomes
        offers = []
        for value in row["offers"]:
            item = dict(value)
            item["offer_reference"] = _pack(item["offer_reference"], _REFERENCE)
            item["provenance"] = _pack(item["provenance"], _PROOF[item["provenance"]["source"]])
            offers.append(_pack(item, _OFFER))
        row["offers"] = offers
        for name, fields in (("matches", _MATCH), ("commercial_evidence", _EVIDENCE)):
            records = []
            for value in row[name]:
                item = dict(value)
                item["offer_reference"] = ref(item["offer_reference"])
                if name == "matches":
                    item["deterministic_evidence"] = _pack(item["deterministic_evidence"], _DETERMINISTIC)
                records.append(_pack(item, fields))
            row[name] = records
        for name in ("recommended_offer_reference", "review_candidate_reference"):
            row[name] = ref(row[name])
        selection = dict(row["commercial_selection"])
        selection["selected_reference"] = ref(selection["selected_reference"])
        selection["candidate_references"] = [ref(value) for value in selection["candidate_references"]]
        row["commercial_selection"] = _pack(selection, _SELECTION)
        rows.append(_pack(row, _ROW))
    output["rows"] = rows
    return output


def _read_wire_payload(value: dict) -> dict:
    if value.get("wire_format") != WIRE_FORMAT or value.get("projection_policy") != PROJECTION_POLICY:
        raise ValueError("unknown wire or projection revision")
    run = dict(value)
    del run["wire_format"]
    _pack(run, _RUN)
    rows = []
    retained_count = 0
    for value in _list(run["rows"], MAX_ACTUAL_ITEMS):
        row = _unpack(value, _ROW)
        offers = []
        retained_count += len(_list(row["offers"], MAX_PROVIDER_EXECUTION_OFFERS))
        if retained_count > MAX_RETAINED_OFFERS:
            raise ValueError("retained record bound exceeded")
        for value in row["offers"]:
            item = _unpack(value, _OFFER)
            item["offer_reference"] = _unpack(item["offer_reference"], _REFERENCE)
            proof = _list(item["provenance"], 5)
            if not proof or type(proof[0]) is not str or proof[0] not in _PROOF:
                raise ValueError("unknown proof record")
            item["provenance"] = _unpack(proof, _PROOF[proof[0]])
            offers.append(item)
        row["offers"] = offers
        references = [item["offer_reference"] for item in offers]

        def ref(index, *, nullable=False):
            if index is None and nullable:
                return None
            if type(index) is not int or not 0 <= index < len(references):
                raise ValueError("invalid row reference index")
            return dict(references[index])

        outcomes = []
        for value in _list(row["outcomes"], MAX_PROVIDER_SELECTION_SIZE):
            item = _unpack(value, _OUTCOME)
            item["affinity"] = _unpack(item["affinity"], _AFFINITY)
            item["retained_offer_references"] = [ref(v) for v in _list(item["retained_offer_references"], MAX_PROVIDER_OUTCOME_OFFERS)]
            outcomes.append(item)
        row["outcomes"] = outcomes
        for name, fields in (("matches", _MATCH), ("commercial_evidence", _EVIDENCE)):
            records = []
            for value in _list(row[name], MAX_PROVIDER_EXECUTION_OFFERS):
                item = _unpack(value, fields)
                item["offer_reference"] = ref(item["offer_reference"])
                if name == "matches":
                    item["deterministic_evidence"] = _unpack(item["deterministic_evidence"], _DETERMINISTIC)
                records.append(item)
            row[name] = records
        for name in ("recommended_offer_reference", "review_candidate_reference"):
            row[name] = ref(row[name], nullable=True)
        selection = _unpack(row["commercial_selection"], _SELECTION)
        selection["selected_reference"] = ref(selection["selected_reference"], nullable=True)
        selection["candidate_references"] = [ref(v) for v in _list(selection["candidate_references"], MAX_PROVIDER_EXECUTION_OFFERS)]
        row["commercial_selection"] = selection
        rows.append(row)
    run["rows"] = rows
    return run
