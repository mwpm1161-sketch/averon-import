# M3A durable v2 read contracts

M2B is FINAL APPROVED at
`f60e02e87bbddc6f8f676705cb1d09b131d67097`. M3A adds an opt-in reader and
immutable contracts only. Current production writes remain schema v1. There is
no v2 writer, storage serializer, feature switch, dual-write, migration, backfill,
or conversion on read. Existing API/UI, D4/history, TenderPriceResolver and XLSX
export do not consume v2. No supplier, AI, VseInstrumenti, deployment, main merge
or release-tag work is introduced.

M3B follow-up: the inactive v2 DTO now declares decision-closure-v1, explicit
returned/retained outcomes and compact-records-v1 JSON. Its pure in-memory
projector/encoder is documented in [MULTI_PROVIDER_M3B.md](MULTI_PROVIDER_M3B.md).
Production remains v1; the original M3A phase did not add that encoder.

## Reader boundary and compatibility

`averon_import.services.manual_tenders.durable_read.read_tender_sourcing_run`
accepts already available JSON UTF-8 bytes/text or a payload dictionary. It does
not open files, construct repositories/stores, recover runs, or call runtime
providers, OfferMatcher, ProviderMatchEvaluator, M2A resolvers/comparability or
the M2B selector. No existing production caller imports this module.

The result is `DurableTenderSourcingRunV1 | DurableTenderSourcingRunV2`:

- Version 1 uses the historical `_read_path` discriminator and ordinary
  `json.loads` interpretation. The wrapper privately owns a detached copy;
  `payload` returns a defensive dictionary copy with the old structure. It does
  not add defaults, expand historical candidates, normalize prices, or upgrade
  schema. File/text v1 limits apply to original bytes, exactly as in the old reader;
  float re-encoding may not introduce a stricter legacy limit. The existing
  `get_public` reader still performs its historical candidate
  expansion, including compact history-v2 slots inside schema-v1 runs.
- Missing version rejects, as the current manual-tender reader already does.
  Legacy equality-to-1 behavior (including JSON `1.0`/`true`) is deliberately
  retained for v1. This quirk does not apply to v2: its version must be integer 2.
- Version 2 uses frozen Pydantic models, `extra="forbid"`, strict scalar types,
  bounded immutable tuples and closed approved enums. Duplicate JSON object
  members reject. Unknown versions, including 3/999, fail closed and never fall
  back to a known schema.
- Errors are `DurableTenderReadError`, with a fixed 37-character message and
  closed code `TENDER_RUN_CORRUPT`, `TENDER_RUN_TOO_LARGE`, or
  `TENDER_RUN_UNSUPPORTED_VERSION`. They do not render payloads, validation
  details, raw exception messages, paths, or upstream diagnostics.

Existing consumers still receive existing dictionaries from existing readers.
No file is rewritten merely because either reader inspected it. Focused tests
create real provider-only and historical 1C runs through the current writer and
canonical projection, compare recommended_offer/recommended_match/route, run
the unchanged price resolver, and verify bytes, mtime and directory contents.
Normal creation and completion explicitly retain schema version 1.

## Exact v2 shape

All fields below are explicit/required, except the four affinity strings, which
default to empty strings. Collections are JSON arrays and immutable tuples in
canonical DTOs. Nullable values are explicit `null`; unknown fields reject.

```text
run:
  schema_version: 2
  projection_policy: decision-closure-v1
  run_id, tender_id, source_sha256, workspace_revision
  status: running | completed | failed | interrupted
  created_at, started_at, completed_at: offset ISO timestamps (completed_at nullable)
  selection: {provider_keys: [provider_key, ...]}
  selected_source_row_ids: [source_row_id, ...]
  rows: [
    {source_row_id, physical_excel_row, result_limit,
     outcomes: [outcome, ...], reused_provider_keys: [provider_key, ...],
     offers: [offer, ...], matches: [match, ...],
     recommended_offer_reference: reference | null,
     review_candidate_reference: reference | null,
     commercial_evidence: [evidence, ...], commercial_selection: selection_result}
  ]

reference: {provider_key, offer_id}
outcome:
  {provider_key, state, request_count, failure_category,
   affinity: {environment, region_id, config_revision, adapter_revision},
   catalog_version, offers_returned_count, retained_offer_references: [reference, ...]}
offer:
  {offer_reference: reference, source_item_id, title, article, manufacturer, brand,
   price, currency, price_unit, availability, availability_text, url, provenance}
match:
  {offer_reference: reference, decision, rank,
   matched_attributes, supporting_attributes, conflicting_attributes, missing_attributes,
   deterministic_evidence: {hard_contradiction, preferred_differences, model_evidence_source},
   explanation}
evidence:
  {offer_reference: reference, amount, currency, vat_basis, price_unit, unit_family,
   evidence_state, issue_codes, basis_revision}
selection_result:
  {state, selected_reference, candidate_references, reason_codes, selection_basis}

ETM provenance:
  {source: etm_ipro, source_item_id, price_field, catalog_version, price_status}
Lemana provenance:
  {source: lemana_b2b, product_item, mirror_revision, region_id}
other/local provenance:
  {source: unproven}
```

V2 represents evaluated live-provider rows. It does not introduce a historical
1C v2 projection or runtime intent objects. The immutable workbook identity
(tender/hash/revision and row IDs) provides source correlation. Evaluated rows
must belong to the explicit selected row set; completed runs require all of
those rows. Running snapshots have no completion timestamp; terminal snapshots
require one. Failure/interruption snapshots may preserve a subset of evaluated
rows. Provider failure is retained by closed outcome facts, without raw run
failure messages or provider response bodies.

## Correlation and stored facts

Provider selection uses the existing bounded ProviderSelection, canonical lexical
order, unique explicit keys, and no inference/registry expansion. Every evaluated
row requires exactly one outcome for each selected key. Each outcome owns only
that provider's references. Outcome state/failure/request invariants follow the
approved execution contract; result_limit bounds per-provider returned counts.
Reused keys are a unique selected subset. Reuse retains the stored outcome request
count, as the execution cache does; reading never performs a cache lookup.

Every identity is `(provider_key, offer_id)`. Same offer_id across providers is
legal; duplicate identities within one row reject. Retained outcome references
must exactly equal the row's offers. Each offer has exactly one match and one
commercial evidence record. Missing, duplicate, foreign and wrong-provider
references reject. Repeated references across different source rows remain legal.
Offers/matches/evidence/outcome references/candidates are sorted by composite
identity; outcomes, provider keys and source rows also have deterministic order.
Stored decisions, ranks and selected references are never recomputed by sorting.
M3B retained offers must equal the exact union of stored commercial candidates,
selected reference, identity recommendation and review reference. Returned counts
describe retrieval even when an outcome retains no offers.

MatchDecision and closed deterministic model-evidence source values are reused
as stored facts. Stored recommended_offer_reference/review_candidate_reference
must reference existing recommendable/REVIEW matches respectively. The immutable
recommended_match/review_candidate properties look up those references without
choosing a candidate. Their uniqueness/quality is not recomputed. Supporting
attributes and preferred differences retain the M1D
quality dimensions. A stored hard_contradiction must agree with stored conflicts.
No AI evidence, arbitrary matcher evidence or Offer.attributes is permitted.

Commercial evidence subclasses the approved M2A scalar contract: closed enums,
basis revisions, normalized currency/unit, unit-family coherence, issue/state
consistency. It does not construct a runtime evaluation or call a resolver.
Amounts/units must match their exact referenced offers; currency must agree,
except approved INVALID evidence may erase an unproved currency to empty. Basis
revision must belong to the referenced provider. COMPLETE ETM facts require
coherent source_item_id, pricewnds, gross VAT, no price-status conflict and no
catalog-version conflict. Noncomplete ETM cannot assert proved VAT. Lemana keeps
UNKNOWN VAT and the stored unproved-basis issue; noninvalid records must correlate
product_item/revision/region with the offer/outcome. Local/other provenance remains
unproven, with UNKNOWN VAT and the approved unproved-basis/unit-untrusted issues.
These checks compare stored facts; they do not derive a new commercial decision.

Commercial selection reuses closed M2B state/reason/basis enums and checks its
scalar structure. SELECTED needs an existing selected candidate, a basis and no
failure reasons. SOLE_STRONGEST_IDENTITY requires one candidate;
LOWEST_COMPARABLE_PRICE requires at least two. NO_SAFE_WINNER requires reasons
and no selected reference/basis. Candidates must be unique existing row offers.
M3A.1 additionally correlates every commercial candidate to an existing stored
MATCH, LIKELY_MATCH or ALTERNATIVE match; REVIEW/REJECT candidates reject in either
selection state. SELECTED requires COMPLETE evidence for every stored candidate,
including the selected reference already required to belong to that set. A
NO_SAFE_WINNER reason COMMERCIAL_EVIDENCE_INCOMPLETE/COMMERCIAL_EVIDENCE_INVALID
requires at least one candidate with the corresponding evidence state; the
reverse implication is not imposed. NO_IDENTITY_CANDIDATE requires an empty
candidate set, without inferring that reason from an empty set. These are small
cross-record checks after exact match/evidence cardinality validation.
M3A.2 restricts NO_SAFE_WINNER to exactly six stored reason families:
{NO_IDENTITY_CANDIDATE}, {COMMERCIAL_EVIDENCE_INCOMPLETE},
{COMMERCIAL_EVIDENCE_INVALID}, {COMMERCIAL_EVIDENCE_INCOMPLETE,
COMMERCIAL_EVIDENCE_INVALID}, {COMMERCIAL_BASIS_NOT_COMPARABLE}, and
{LOWEST_PRICE_TIED}. Cross-phase combinations reject. NO_IDENTITY_CANDIDATE
remains a sole reason with empty candidates; evidence families require nonempty
candidates and retain the one-way M3A.1 reason/state checks. Each comparison
reason must stand alone, with at least two candidates and COMPLETE evidence for
every candidate. Only stored branch prerequisites are checked: the reader does
not compare Decimal prices or prove ties/non-comparability. M3A.2 changes no
capacity bound, byte budget or production writer.
The reader deliberately does not recompute the strongest identity cohort, compare
commercial bases, prove the minimum price, or replace a stored decision. A future
reconciliation phase would be separate from this representation contract.

`row.partial_failure` follows the approved execution-summary rule: a usable
success/empty/partial_success outcome together with a failure/suppressed outcome.
`run.partial_failure` is any evaluated row with this property. ETM SUCCESS plus
Lemana FAILURE therefore differs from EMPTY or an unselected Lemana. A sole
stored ETM winner and failed Lemana remain visible without fabricated offers.

## Exact bounds

The repository hard cap remains **1,048,576 bytes**. V2 source UTF-8 JSON (or its
compact dictionary input) and canonical compact wire projection must each fit
**786,432 bytes (768 KiB)**. M3B measures the lossless compact-records-v1 wire,
instead of expanded DTO field names. This leaves **262,144 bytes (25%)** storage
headroom. All limits reject; no truncation or repaired payloads. Defined omission
of non-decision runtime offers is explicit in returned counts and policy revision.
V1 retains the repository's historical full 1 MiB budget.

| Collection / scalar | Maximum |
| --- | ---: |
| Selected providers / row outcomes / reused keys | 8 |
| Selected source rows / evaluated rows | 500 |
| Offers / matches / evidence / candidates per row | 400 |
| Aggregate retained offers across the whole durable run (M3B) | 4,096 |
| Returned count / retained references per provider outcome / result_limit | 100 |
| Attribute names per list, including preferred differences | 16 |
| Commercial issue codes / selection reasons | 10 / 5 |
| Outbound request_count | 1,000,000 |
| workspace_revision / physical_excel_row / match rank | 1,000,000,000 / 1,048,576 / 400 |
| Lemana positive region_id | 2,147,483,647 |
| Compact offer / match / evidence record bytes | 2,048 / 1,024 / 1,024 |
| Compact outcome / selection-result record bytes | 40 KiB / 128 KiB |

M3B reassessed and replaced the obsolete aggregate 400-offer run cap with an
independent 4,096 retained-record allocation cap. Runtime execution remains
400 offers per row. Per-record maxima cannot all be saturated
simultaneously: the independent global byte guard still applies. No runtime cap
or current storage quota is increased. Timing samples, arbitrary intents and
provider internals are omitted from this compact read projection.

Historical M3A.1 capacity note required reassessment of the inactive whole-run
400-offer bound before M3B writer activation, with no silent truncation. M3B
completed that reassessment with explicit decision closure and count semantics;
the production writer is still not activated. Byte budgets and runtime bounds
are unchanged. Exact measurements and limitations are in MULTI_PROVIDER_M3B.md.

| String | Maximum characters / exact format |
| --- | --- |
| provider_key | 100, existing lowercase key syntax |
| offer_id / source_item_id / provenance source_item_id or product_item | 180; offer_id nonblank |
| title | 320, nonempty |
| article / manufacturer / brand | 100 each |
| currency / price_unit | 3 (uppercase ASCII code or empty) / 80 |
| availability_text / explanation / product URL | 120 / 240 / 500 |
| catalog_version / config_revision / adapter_revision | 120; catalog_version nonempty when present |
| affinity environment / region_id | 24 / 80 |
| attribute name | 40, lowercase ASCII attribute-key syntax |
| ETM price_field / price_status | 40 / 80 |
| run_id / tender_id / source_row_id | exactly 32 lowercase hex |
| source_sha256 / Lemana mirror_revision | exactly 64 lowercase hex |
| timestamps | 40, valid ISO datetime with UTC offset |
| Decimal text / digits / absolute exponent | 120 / 80 / 100 |

Other strings are closed enums/literals (provider source, basis revision,
model-evidence source, lifecycle status). Basis revisions retain exactly the four
approved M2A literals; model-evidence source is empty/explicit_model/title/article.
Decimal strings use ASCII numeric syntax, without whitespace or separators.
Prices/amounts are finite, nonnegative exact Decimal values; integer and Decimal
inputs are permitted, Python floats/bools rejected. V2 JSON fractional number
tokens are parsed directly to Decimal. NaN/Infinity/negative/malformed values
reject. Zero remains representable with the approved PRICE_NON_POSITIVE issue.
There is no float canonical price, epsilon, FX, VAT or unit transformation.

Provenance is a discriminated closed DTO, not a raw data_provenance dictionary.
Passwords/tokens/Authorization/client_secret/settings/request-response headers
and arbitrary extra fields reject at every v2 level. Allowed text rejects control
characters and credential-shaped assignments/Bearer text. Product URLs permit
only HTTP(S), a hostname, no userinfo, query, fragment, whitespace or backslash;
percent-decoded credential-shaped text also rejects. No raw provider objects,
upstream error strings, arbitrary nested mappings or mutable canonical lists are
exposed. This is an allowlisted facts contract, not a copier of provider payloads.

## Verification fixtures

- ETM + Lemana with the same offer_id: two SUCCESS outcomes, distinct exact match
  and evidence references, ETM COMPLETE, Lemana INCOMPLETE/UNKNOWN VAT and stored
  NO_SAFE_WINNER / COMMERCIAL_EVIDENCE_INCOMPLETE.
- Two equal MATCH ETM candidates with COMPLETE comparable evidence and exact
  Decimal amounts differing by 10^-24: stored SELECTED / LOWEST_COMPARABLE_PRICE
  preserves the exact lower composite reference.
- ETM SUCCESS plus Lemana FAILURE/timeout: stored ETM sole winner and partial
  failure remain distinct from EMPTY and ETM-only selection.
- 400-offer compact baseline: 480,653 bytes. A four-byte Unicode title fixture
  made of real compact fields reaches exactly 786,432 raw/canonical bytes and is
  accepted. One-byte raw excess and oversized compact facts reject. Omitting
  default affinity fields cannot bypass the canonical byte budget.
- Corruption cases cover identity/outcome/cardinality/selection/provenance,
  every closed enum, strict types, strings/collections/record/global byte limits,
  forbidden fields, unsafe URLs, duplicate JSON keys and Decimal failures.
- Read guards forbid filesystem/socket/matcher/evaluator/runner/resolver/
  comparability/selector calls. V1 production creation/completion and real price
  export expectations are exercised, without any v2 durable write.

All fixture serialization is confined to tests. Production schema-v1 writer
calls remain in `sourcing.py`: recovery, create_running, complete and fail call
the unchanged `_atomic_write`; none calls this reader or emits schema version 2.
