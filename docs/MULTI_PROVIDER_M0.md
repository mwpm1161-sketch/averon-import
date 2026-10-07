# Multi-provider sourcing M0 contracts and M1A runner

M0/M0.1 contracts at baseline `5e93518b36efd479e2614709e523e5d5cdf64fd2`
are final approved. M1A adds only the inactive internal `ProviderRunner` in
`averon_import/services/sourcing/providers/execution.py`; no current runtime
caller, API, or UI imports or invokes it.

This note records inactive internal contracts introduced by M0 and the
request-local execution engine introduced by M1A. Current API
models, endpoint payloads, provider resolution, matcher, settings UI, search
execution, export resolver, and tender durable writer remain on their existing
single-provider and schema-version-1 paths.

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
included. The next planned phase is legacy-provider adaptation and routing
integration, subject to independent review.

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
