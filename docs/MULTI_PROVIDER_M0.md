# Multi-provider sourcing contracts and inactive execution (M0–M1B)

M0/M0.1 contracts at baseline `5e93518b36efd479e2614709e523e5d5cdf64fd2`
are final approved. M1A adds only the inactive internal `ProviderRunner` in
`averon_import/services/sourcing/providers/execution.py`; no current runtime
caller, API, or UI imports or invokes it.

This note records inactive internal contracts introduced by M0, the
request-local execution engine introduced by M1A, and the outcome-native
provider adapters introduced by M1B. Current API models, endpoint payloads,
provider resolution, matcher, settings UI, search execution, export resolver,
and tender durable writer remain on their existing single-provider and
schema-version-1 paths.

## M1A execution boundary

`ProviderRunner` accepts an explicit canonical `ProviderSelection`, one
`ProductIntent`, a result limit, a fresh `ProviderExecutionScope`, and an
explicit registry of outcome-native adapters. It runs selected providers
sequentially in canonical order, isolates adapter failures, and returns their
provider-local outcomes plus offers ordered by `(provider_key, offer_id)`.
Equal offer IDs from different providers remain distinct. A conflicting
duplicate identity inside one provider invalidates that provider's offer
result while preserving the other providers. The aggregate has a 400-offer
request-local ceiling; exceeding it raises a typed error without truncation.

The execution adapter protocol supplies a counter for actual outbound
provider/HTTP attempts. The adapter must increment it immediately before each
outbound attempt, and its outcome count must exactly match that counter. Local
work does not increment it. The runner rejects count mismatches and converts
adapter exceptions to a closed safe failure category, preserving the number
already counted. A legacy `SourcingProvider.search(intent, limit)` adapter is
not eligible because its return type does not reveal the true number of
underlying HTTP attempts. Request counts must never be guessed.

Equivalent work can be reused only from the in-memory cache held by the same
`ProviderExecutionScope` instance and only when provider, request fingerprint,
affinity/revisions, limit, and scope match. Cache hits report zero new outbound
requests and carry no replayed provider timings; `reused_provider_keys`
identifies reuse explicitly. There is no persisted or cross-run live commercial
cache. The runner does not call `stats()`, local status, or connectivity probes
and contains no matcher decisions.

M1A does not change API/UI behavior, durable data or schema version 1, current
`SourcingService` routing, matcher authority, export authority, or any existing
provider adapter. No VseInstrumenti network integration or credential work is
included. M1A was final approved at `087bcf99554d498a31741c584c811ee95895e03e`.

## Provider selection and identity

`ProviderSelection` is an explicit, non-empty collection of at most eight
registered provider keys. It rejects duplicates and unknown registry keys and
canonicalizes order for future job fingerprints. A legacy singular provider
or existing default resolves to one provider only; adding a configured
provider does not widen that selection. No `providers` field is exposed in the
API during M0.

`ProviderOfferReference` identifies a live offer by `(provider_key, offer_id)`.
Existing `Offer.offer_id` values are unchanged. New code can index and
deterministically order colliding supplier IDs without changing singleton
matcher tie-break behavior.

## Outcomes and health

`ProviderSearchOutcome` represents attempted, suppressed, empty, successful,
partially successful, and failed searches. It contains normalized `Offer`
objects, safe bounded diagnostics and per-provider affinity. It has no match
decision. `ProviderExecutionSummary` records each provider independently, so
one provider's failure does not erase another provider's offers.

In both outcome and summary contracts, `request_count` counts actual outbound
provider requests or HTTP attempts represented by that result. An attempted
local success or local failure may therefore have a count of zero; no logical
execution is fabricated as a network request. `NOT_ATTEMPTED` and `SUPPRESSED`
always have a zero request count. Summary offer counts also follow the state:
`NOT_ATTEMPTED`, `SUPPRESSED`, `EMPTY`, and `FAILURE` contain zero offers;
`SUCCESS` and `PARTIAL_SUCCESS` contain at least one.

Search outcomes are request-local DTOs, not tender run records. Persistent
tender writes stay schema version 1 in M0, retain the current 1 MiB run limit,
and continue to use the existing history projection and decision authority.
There is no schema-version-2 writer.

Local status snapshots and explicit connectivity probes are separate
interfaces. Any future local status implementation must make zero provider
HTTP calls; only an explicit administrator action may run a connectivity probe.
M0 does not modify existing provider health behavior.

## Commercial evidence and traffic

Commercial evidence is bound to `ProviderOfferReference(provider_key,
offer_id)` and independently records `provider_key` and `source_item_id`. The
contract rejects a provider mismatch, and `is_for_offer()` checks the exact
provider, offer ID, and source item before evidence is associated with an
`Offer`. Reusing a source item ID cannot bind the evidence to a different offer,
and equal offer IDs from different providers remain distinct. Evidence still
records price field and basis, currency and its basis, unit and its basis,
applicable tax basis, and relevant environment/region/configuration. Missing
facts stay unknown. This contract does not authorize an export. No VseInstrumenti
export allowlist or price semantics are added.

Health snapshots reject contradictory configured/status combinations: an
unconfigured provider cannot be reachable; `NOT_CONFIGURED` requires
`configured=False`; a reachable provider cannot carry a failure category.
Snapshot validation is local and makes no provider HTTP calls.

`ProviderSearchRequestIdentity` is scoped to one execution and includes the
provider request fingerprint, limit and provider affinity. A future runner can
reuse equivalent provider search work within one run, then match the resulting
offers separately against each row. M0 adds no cache, network request, polling,
or search-as-you-type behavior. Cross-run reuse of live commercial data is not
approved.

## Deferred production prerequisite

On Linux, `create_secret_store(auto)` selects `InsecureFileSecretStore`. A
future VseInstrumenti live-enablement phase must provide an explicitly approved
credential source and must not silently store the token in that plaintext
fallback. M0 does not choose a production credential mechanism, change
credentials, call a supplier API, or enable multi-provider execution.

## M1B inactive adapters and transport accounting

M1B adds explicit outcome-native adapters for `LocalCatalogProvider`,
`LemanaB2BProvider`, and `EtmIproProvider` in
`averon_import/services/sourcing/providers/execution_adapters.py`. They accept
provider instances explicitly and satisfy the M1A adapter protocol, but no
production runtime registry or caller is added. `ProviderRunner` remains
inactive.

Actual outbound attempts are observed request-locally at the configured
transport boundary. Lemana records immediately before its transport call, so
token acquisition, product/price requests, and a price attempt retried after a
401 are counted only when each attempt is actually sent. ETM records
immediately before `_transport` in its normal request path, after its existing
rate-limit and quarantine checks; a login is included only when sent. Local
mirror lookup, token/session cache hits, rate-limit waiting, local validation,
and requests suppressed by ETM quarantine do not increment the counter. No
global counter or persistent traffic state is introduced.

The existing providers' ordinary `.search()` paths retain their prior call
shape and output. The observer is optional and passed only by the new explicit
adapter path. ETM's process-wide outbound gate, login interval, persistent
12-hour auth quarantine, authenticated-403 handling, and no-blind-retry rule
are unchanged. Instrumentation adds no provider requests or preflight checks.

The adapter error mapping is bounded and excludes exception text:

| Existing `SourcingProviderError.category` | Outcome category |
| --- | --- |
| `auth`, `authentication` | `AUTHENTICATION` |
| `rate_limit`, `rate_limited` | `RATE_LIMITED` |
| `network`, `transport` | `TRANSPORT` |
| `timeout` | `TIMEOUT` |
| `invalid_response` | `INVALID_RESPONSE` |
| `not_configured`, `invalid_request`, `health_error` | `MISCONFIGURED` |
| `upstream_error`, `storage_error` | `UNAVAILABLE` |
| unrecognized category | `UNKNOWN` |

Local results keep a zero request count. Successful searches produce
`SUCCESS`, no-result searches produce `EMPTY`, and provider-local exceptions
produce `FAILURE`; no partial result is invented. Search identities include
the intent fingerprint, requested limit, provider key, and non-secret
provider affinity. Lemana affinity includes environment, region, and mirror
revision. ETM affinity includes environment, mirror/index revision, warehouse
selection, live-candidate cap, and an opaque hash of its configured API base
override. Local affinity includes only the deterministic catalog revision and
adapter revision, with no fabricated environment or region.

M1B was final approved at `545fabaa4092149b93424283fd713e7e2b34316d`.

## M1C controlled runtime composition and retrieval boundary

M1C composes `ProviderRunner` and the approved adapters from the same
`LocalCatalogProvider`, `LemanaB2BProvider`, and `EtmIproProvider` instances
already held by `SourcingRuntime.providers`. The explicit
`SourcingRuntime.execution_providers` registry contains only
`etm_ipro`, `lemana_b2b`, and `local_catalog`, with a canonical
`execution_provider_keys` view. It is deliberately separate from the legacy
provider map: `demo_store_http` remains available to existing singleton
search, but is not execution-capable. Runtime composition constructs no
supplier request, stats, health, or connectivity probe.

`SourcingService.execute_provider_selection(intent, *, selection, limit,
execution_scope)` is the internal retrieval-only boundary. The caller must
provide an explicit `ProviderSelection` and its own `ProviderExecutionScope`;
there is no implicit all-provider selection or process-global scope. A service
without a configured runner fails closed. The method delegates to
`ProviderRunner` and returns `ProviderExecutionResult`; it does not call
matcher/ranking, health/stats, legacy search caches, or durable writers.

Existing `search_intent()` and `search_project()` continue through their
legacy singleton implementation, including existing provider fields,
defaults, stats checks, cache behavior, circuit behavior, history routing, and
durable schema version 1. Multi-provider offers are not passed to the current
matcher or tender projection. No API/UI selection or capability endpoint,
durable format, export, matcher, D4, or VseInstrumenti integration is added.

M1C was final approved at `9b5262b914741c2abc6660a28fddc82098b95ad5`.

## M1D inactive composite-aware deterministic matching

M1D adds a request-local `ProviderMatchEvaluator`, exposed internally through
`SourcingService.evaluate_provider_execution(intent, execution)`. It evaluates
only the already returned `ProviderExecutionResult`; retrieval, provider
health/stats, AI ranking, legacy caches, and persistence are not part of this
call. `ProviderMatchEvaluation` retains the exact intent and execution,
deterministic `MatchResult` values, composite recommendation/review references,
and the original provider outcomes including partial failures. It is bounded
by the existing 400-offer execution limit and is not a durable or HTTP model.

Multi-provider identity and correlation use
`ProviderOfferReference(provider_key, offer_id)`. Every execution offer must
have exactly one match with the same composite identity and offer facts;
duplicate, malformed, missing, or foreign matches fail closed. `OfferMatcher`
uses `(provider, offer_id)` only as its final deterministic ordering key after
the existing evidence dimensions. The same-provider legacy tie order therefore
continues to use `offer_id`, while provider identity never changes a decision
or evidence quality.

Identity recommendation keeps the existing `MATCH`, `LIKELY_MATCH`, then
`ALTERNATIVE` precedence. Equal best deterministic evidence remains ambiguous
and yields no recommendation, regardless of provider order, price, currency,
availability, or retrieval time. A unique stronger evidence result may be
recommended. Strong `REVIEW` candidates use the existing identity eligibility
principles without rank/provider ordering as authority; tied strongest review
candidates yield no unique review reference. Provider failures remain visible
in the retained execution and never become synthetic `REJECT` matches.

Tender projection now correlates `recommended_offer` with its
`MatchResult.offer` by `(provider, offer_id)` whenever provider identity is
available, while retaining offer-id-only correlation for legacy payloads that
have no provider identity. Public projection shape and durable schema version
1 are unchanged. Legacy `search_intent()`, `search_project()`, recommendation
and review callers retain their existing behavior. No API/UI activation,
commercial winner selection, AI ranking, export or D4 changes, durable v2
writer, or VseInstrumenti work is included. Any API or durable multi-provider
activation requires independent review in a later phase.

M1D.1 keeps the validated evaluation matches in a private, deep-owned snapshot.
The `matches`, `recommended_match`, and `review_candidate` accessors return
defensive deep copies, so mutable legacy `MatchResult` objects cannot change
the evaluation's validated recommendation or review state. Legacy
`MatchResult` semantics remain unchanged outside this request-local boundary;
commercial selection has not been activated.

M1D (including M1D.1 invariant hardening) is FINAL APPROVED at
`a122cd0578ce2125ebb6cd32ca70d39a92f667c6`.

## M2A request-local commercial evidence and comparability

`provider_commercial.py` adds an independent commercial evidence layer over an
already validated `ProviderMatchEvaluation`. The explicit internal method
`SourcingService.evaluate_provider_commercial_evidence(evaluation)` performs
only local deterministic validation. Retrieval and identity evaluation do not
call it automatically. It makes no runner, provider search, stats, health,
access, supplier HTTP, AI, cache, or persistence call.

`CommercialOfferEvidence` contains only a composite `offer_reference`,
`amount: Decimal | None`, normalized `currency`, closed `vat_basis`, bounded
`price_unit`, shared normalized `unit_family`, closed `evidence_state`, a
canonical tuple of closed `issue_codes`, and a fixed non-secret
`basis_revision`. It retains no raw payload, provenance dictionary, provider
configuration, token, mirror revision, or region value. Currency codes are
explicit uppercase three-letter values; empty/malformed currency stays unknown.
Amounts must be finite, positive for comparison, and within a bounded decimal
representation (80 digits, exponent magnitude at most 100). Zero is incomplete;
malformed, negative, nonfinite, or out-of-bound prices are invalid, never replaced
with zero. An invalid legacy execution is rejected at the aggregate boundary.

The commercial resolver registry explicitly contains only `etm_ipro`,
`lemana_b2b`, and `local_catalog`, independently of runtime provider registration.
An unknown provider receives unproven incomplete evidence rather than implicit
commercial authority. Evidence states are `COMPLETE`, `INCOMPLETE`, `INVALID`;
VAT bases are `GROSS_INCLUDING_VAT`, `NET_EXCLUDING_VAT`, `UNKNOWN`.

ETM's existing documented VAT-inclusive `pricewnds` path can produce complete
gross evidence only with exact provider/source `etm_ipro`, a nonempty bounded
matching `source_item_id`, `price_field == pricewnds`, a positive finite amount,
an explicit actual Offer currency, and an explicitly supplied trusted price
unit. The legacy Offer default unit is not supplier evidence. Contradictory
source, currency/unit/VAT claims, price status, or retained catalog metadata
produce invalid evidence. Source identity inconsistency does not authorize
retaining a currency as proved. There is no generic ETM-to-RUB inference.

Lemana preserves factual price/currency/unit but always leaves VAT `UNKNOWN`,
with `VAT_BASIS_UNKNOWN` and `PROVIDER_PRICE_BASIS_UNPROVEN`. Its exact source and
`product_item` must match the Offer; `mirror_revision` must have the current
mirror's 64-character lowercase SHA-256 format, and `region_id` must be a
positive bounded integer. Retained outcome region/catalog-version metadata,
when present, must agree. These metadata values are checked locally and are
not copied into evidence. Even a raw VAT claim cannot grant Lemana authority.
Local catalog prices likewise have no automatically trusted VAT, commercial
source, or price-unit basis and normally remain incomplete.

`ProviderCommercialEvaluation` owns a private deep snapshot of the exact
matching evaluation, exposes defensive matching/execution/outcome copies, and
contains an immutable evidence tuple ordered by
`ProviderOfferReference(provider_key, offer_id)`. Every actual execution offer
has exactly one record, with exact resolver-derived facts; duplicate, missing,
foreign, forged, or malformed records fail closed. The existing 400-offer cap
applies. Failed, suppressed, empty, and partial provider outcomes remain
separate from commercial completeness: no nonexistent offer receives synthetic
`PRICE_MISSING` evidence. Identity decisions and recommendation/review references
remain unchanged.

`compare_commercial_evidence(left, right)` returns only left/right composite
references, `comparable`, and canonical closed `reason_codes`. Both records
must be complete and have positive amounts, the same explicit currency, the
same proved non-unknown VAT basis, and the same trusted unit family. It uses
the existing shared sourcing alias normalization and permits only exact
`piece`, `kilogram`, `tonne`, `meter`, `square_meter`, `cubic_meter`, or `litre`
families. Packages (`pack`, `set`) remain unproven even when both units agree;
there is no package inference, physical-unit conversion, or FX conversion.
Reversing the pair preserves the same result and reason set. Amounts are
checked only for usability, never ordered against each other. Comparability
does not select a winner, cheapest offer, preferred provider, export offer, or
price ranking.

M2A does not activate API/UI fields, provider selectors, durable multi-provider
writes, caching, or runtime callers. Durable schema remains version 1. Existing
matching, D4/history, `TenderPriceResolver`, ETM quarantine, manual tender runs,
and XLSX export remain authoritative and unchanged. There is no VseInstrumenti
client, adapter, configuration, token/region handling, deployment, or VI work.
