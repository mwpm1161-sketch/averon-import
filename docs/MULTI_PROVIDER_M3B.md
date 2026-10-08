# M3B inactive capacity-safe durable v2 projection

Base: `53de150aaa9a5242362ed406319f5bdcafe5c0c0` on
`feature/manual-tender-xlsx-v1`. M3A/M3A.1/M3A.2 remain the validation foundation.
M3B evolves only the inactive v2 representation and adds pure in-memory tools.
TenderSourcingRunStore create/complete/fail/recovery still writes/reads v1. There
is no production callsite, v2 file, dual/shadow write, feature switch, migration,
API/UI/export change, VI integration, supplier traffic or deployment.

## Capacity study before changing the representation

The v1 repository hard cap is 1,048,576 bytes; v2 keeps 786,432 bytes (25%
headroom). The real parser/selected-row maximum is 500. Runtime retrieval is
100 offers per provider, at most 8 selected providers and 400 merged offers per
row. The inactive M3A DTO additionally capped the whole run at 400 offers.
That could not express 500x1 or 370x2 even before measuring bytes.

Study used real M3A field names and compact UTF-8 JSON, with two selected
providers, distinct 32-character row IDs, Cyrillic industrial product titles,
22-character source IDs, articles, manufacturer/brand, availability, URLs,
provider environment/config/adapter revisions, allowlisted ETM/Lemana proof,
identity evidence and complete/incomplete commercial facts. These are measured
payloads, not a multiplication estimate; oversized inputs were measured without
pretending the old reader would accept them.

| Original M3A shape | Retained offers | Compact JSON bytes |
| --- | ---: | ---: |
| 370 rows, one offer each | 370 | 988,620 |
| 370 rows, two offers each | 740 | 1,545,100 |
| 500 rows, one offer each | 500 | 1,335,850 |
| 500 rows, two offers each | 1,000 | 2,087,850 |
| One row, legal 400-candidate cohort / four providers | 400 | 593,685 |

The existing v1 history projection uses closed positional candidate records and
a deterministic text pool. M3B uses closed positional JSON records and exact
row reference indices. No text pooling, compression or binary storage is needed.

## Runtime boundary and exact chain

`project_durable_provider_row_v2` requires exact approved types:
ProviderExecutionResult, ProviderMatchEvaluation, ProviderCommercialEvaluation,
ProviderCommercialSelection and DurableRowIdentity. Dictionaries are rejected.
Row source ID must equal matching intent's source ID; physical Excel row is a
strict bounded integer. No IDs or timestamps are generated here.

M2A already owns a deep validation witness. M3B adds a private deep construction
witness/accessor to M2B selection, without changing its constructor's decision
algorithm. It verifies decision fields and the exact owned commercial snapshot
have not changed. Projection compares all supplied phases against that owned
snapshot, including intent, full execution/outcomes/offers, matches, commercial
evidence and explicit Offer field presence. Prices/amounts must still be Decimal.
Same composite IDs with different prices, proof, intent or metadata cannot splice
different chains together. Unwitnessed objects fail closed. No M1D post-init,
cohort derivation, commercial resolver, comparability or selector is run here.

## Projection policy and retained facts

The required closed literal is `projection_policy = decision-closure-v1`.
For each evaluated row retain exactly the union of:

- every commercial candidate reference;
- selected reference, if present;
- identity recommended reference, if present;
- review reference, if present.

Deduplicate and order by `(provider_key, offer_id)`. Each retained reference gets
exactly one offer, match and commercial evidence record. All M2B candidates are
non-negotiable, including losers and incomplete/invalid/ambiguous context. No
provider preference, first-N, completion-order or price-based retention exists.
Attribute-name lists are sorted without changing their contents or decisions.
Only explicit allowlisted offer/proof/match/evidence fields are copied. Full
Offer dumps, attributes, arbitrary provenance, timings, headers, exceptions and
settings are never serialized. Unknown/non-projectable facts fail explicitly.

Each outcome stores factual `offers_returned_count` and
`retained_offer_references`. SUCCESS/PARTIAL_SUCCESS requires positive returned
count; EMPTY/FAILURE/NOT_ATTEMPTED/SUPPRESSED requires zero. Partial/failure
categories and unattempted/suppressed request rules survive. Retained references
are unique, provider-owned and no more numerous than returned count. Per-provider
returned count is at most result_limit. Reused results preserve keys and outbound
request count, including zero. No cache lookup occurs.

Reader validates exact closure, all outcome retained references, record
cardinality, composite identities, all M3A.1 selection/evidence rules and exactly
the six M3A.2 reason families. Success can truthfully retain zero offers if all
returned offers lie outside closure. NO_IDENTITY_CANDIDATE can coexist with
positive returned counts; its candidate set remains empty. ETM success plus
Lemana failure retains both outcomes and exposes partial_failure without a
fabricated Lemana offer.

A decision-closure snapshot preserves facts supporting the approved decision.
It is not a complete supplier-search archive or proof of global market
optimality, especially when providers failed. Omitted offers existed as shown
by returned counts; their identities/payloads are not fabricated or archived.

## Compact JSON and exact Decimal

`encode_tender_sourcing_run_v2` accepts only DurableTenderSourcingRunV2 and
revalidates its deep explicit fields, including objects created with bypass
methods. It returns bytes only after complete bounded encoding. Metadata and
provider selection stay named JSON fields; required `wire_format` is the closed
literal `compact-records-v1`. Nested field layouts are defined explicitly in
durable_wire.py; arrays must have exactly their declared widths. Offers contain
full composite references. Other references are strict integer indices into
that row's sorted offer table; bool/null (except nullable references), negative,
out-of-range, duplicate and foreign-provider references reject. Proof records
have a closed discriminator and exact provider-specific layout.

The decoder reconstructs the friendly DTO and then applies the existing reader
validation. It checks row/per-provider/global record bounds before expansion.
Unknown root fields/layout revisions and duplicate JSON keys reject. Expanded
DTO dictionaries/JSON remain debug/read inputs with their own source-byte guard;
they are not the storage encoding and may be larger than canonical wire bytes.

Decimal is a JSON string. From the exact tuple remove trailing coefficient
zeros while exponent is below 100; zero (including negative zero) becomes `0`.
Write the remaining coefficient alone if exponent is zero, otherwise
`coefficientEexponent`, with no plus sign or leading exponent zeros. Thus
1234.5600 and 123456E-2 both encode as `123456E-2`. At exponent 100 keep any
required coefficient zeros, so 10E100 remains within the approved exponent
bound. This uses no Decimal arithmetic/normalize/context rounding or float.
Non-finite/negative values, more than 80 digits or absolute input exponent above
100 reject. Canonical text stays inside 120 characters. Equivalent accepted
values produce identical bytes. V1 keeps its old Decimal string size accounting.

JSON is UTF-8, compact separators, sorted metadata keys, closed positional field
order, canonical collections, allow_nan=False and Decimal-only fallback. There
is no generic default=str. Identical DTOs and equivalent runtime ordering yield
byte-identical output. DTO -> encode -> existing reader is an exact value round
trip, including selected/no-safe, partial failure, same IDs across providers,
review and no-identity states.

## Bounds and final measured envelope

Runtime bounds remain 400 offers per row, 100 per provider, 8 providers and 500
selected rows. Old global 400 is replaced with MAX_RETAINED_OFFERS = 4,096:
768 KiB / 192 record allocation units, far below the theoretical 500x400 runtime
fanout. This is an independent allocation/anti-DoS ceiling, not a claim that
4,096 records fit. The authoritative canonical-wire byte cap remains 786,432;
repository 1 MiB and per-record M3A bounds remain unchanged. Reader, builder and
encoder reject over-limit runs, rather than dropping rows/candidates.

The final deterministic fixtures vary product/article/source IDs across rows,
use the representative strings above, two selected providers, and additional
weaker returned runtime offers outside closure. Fields are not shortened for
capacity. Tests pin exact sizes:

| Final wire fixture | Retained offers | Encoded bytes |
| --- | ---: | ---: |
| 370 rows, one decision offer each | 370 | 432,217 |
| 370 rows, two M2B candidates each | 740 | 734,507 |
| 500 rows, one decision offer each | 500 | 583,927 |
| 500 rows, two M2B candidates each (explicit TOO_LARGE) | 1,000 | 992,427 |
| One legal row, full 400-candidate cohort | 400 | 300,508 |
| Largest successful near-budget fixture: 370x2, legal title/evidence text | 740 | 786,432 |
| First over-budget member: same closure, one extra ASCII title character | 740 | 786,433 |
| Near-worst legal strings, 500 rows (explicit TOO_LARGE) | 500 | 1,220,427 |

The near-budget fixture contains actual bounded product/match text, not ignored
padding or raw payload. The first excess raises DURABLE_V2_TOO_LARGE from both
builder and encoder, without a partial return. A separate near-worst legal
string fixture and a 500x2 run are explicitly rejected on bytes. Multiple legal
400-candidate rows likewise fail rather than shrinking cohorts. The successful
740-record baseline proves that obsolete global-400 behavior cannot block a
fitting run. Larger/cohort-heavy tenders have explicit capacity failure; M3B
does not promise every 500-row runtime fanout fits this storage architecture.

## Safe errors and inactive status

Projection errors have one fixed message and closed codes
DURABLE_V2_CORRUPT_INPUT / DURABLE_V2_TOO_LARGE. Wrapped Pydantic size failures
are mapped to TOO_LARGE without exposing inputs, raw diagnostics or paths.
Guards forbid runner/matcher/M1D/M2A/M2B functions, provider-specific resolvers,
full Offer dumping and filesystem/network calls while projecting, building,
encoding and reading. Existing v1 provider/history recommendation/export tests
remain; an additional spy regression covers create/complete/fail/recovery and
asserts only v1 files are produced. No production module imports this encoder.
