# Multi-provider sourcing M0 contracts

This note records inactive internal contracts introduced by M0. Current API
models, endpoint payloads, provider resolution, matcher, settings UI, search
execution, export resolver, and tender durable writer remain on their existing
single-provider and schema-version-1 paths.

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

Search outcomes are request-local DTOs, not tender run records. Persistent
tender writes stay schema version 1 in M0, retain the current 1 MiB run limit,
and continue to use the existing history projection and decision authority.
There is no schema-version-2 writer.

Local status snapshots and explicit connectivity probes are separate
interfaces. Any future local status implementation must make zero provider
HTTP calls; only an explicit administrator action may run a connectivity probe.
M0 does not modify existing provider health behavior.

## Commercial evidence and traffic

Commercial evidence must identify provider and source item, price field and
basis, currency and its basis, unit and its basis, applicable tax basis, and
relevant environment/region/configuration. Missing facts stay unknown. This
contract does not authorize an export. No VseInstrumenti export allowlist or
price semantics are added.

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
