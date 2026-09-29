# 1C-first sourcing routing (Phase 2B-1)

The backend accepts three `source_mode` values on row and project sourcing requests:

| Mode | Behavior |
| --- | --- |
| `provider_only` | Backward-compatible default. Runs the existing provider path and does not read the 1C history snapshot. |
| `one_c_only` | Uses 1C history only. SAFE results can be accepted; REVIEW candidates have no automatic recommendation; missing or unavailable history returns a typed notice without provider fallback. |
| `one_c_then_provider` | Checks 1C per row. A SAFE result suppresses the provider call; a completed lookup that is not SAFE falls back once to the configured provider for that row. A history-update admission conflict is returned as a typed 409 and never causes silent fallback. |

The current UI exposes all three modes. With active history, the UI initially selects `one_c_only`; with no active history it selects `provider_only`. An API request that omits `source_mode` still defaults to `provider_only`. `/api/sourcing/search-intent` rejects history modes because that endpoint does not carry a trusted source baseline.

## History identity policy

Retrieval and the existing matcher use the resolved `ProductIntent`. A history SAFE decision is additionally gated by the deterministic `baseline_intent`, which owns source article, exact name, unit, model, manufacturer, and required attributes. AI-only inferred fields, including preferred attributes, cannot establish the history identity basis.

`EXACT_ARTICLE` retains the strict article match and uniqueness checks: a non-empty baseline article, exact source-backed article, strict matcher result, compatible known units, valid item/variant/event provenance, no item integrity conflicts, one canonical item, a usable positive price, and a valid non-future purchase date. A source article cannot be bypassed by the name policy.

`EXACT_SOURCE_NAME_UNIT` is a separate deterministic policy for source rows without an article. Its name key applies Unicode NFKC, casefolding, `ё` to `е`, NBSP normalization, trimming, and whitespace collapse while preserving punctuation, token order, and model characters. The baseline name must exactly equal a descriptive history variant name, and source, variant, and selected event units must share a known canonical family. The exact name and unit must resolve to one canonical item across the full snapshot. A stronger punctuation/spacing collision key is used only to reject ambiguous identities; it is never positive match evidence.

Name-only acceptance also requires a source-owned specificity signal: an exactly compatible source model, manufacturer, resolved deterministic required attribute, or a strong engineering token in the exact source name. Recognized embedded forms include DN/PN/IP ratings, dimensions such as `5x6`, numeric ranges such as `32-80`, one-letter hyphenated model forms such as `M-500`/`М-500`, and short Latin prefixes with multiple numeric groups such as `AZ-13-770`. Arbitrary product words followed by a hyphen or slash and a number (for example `Кабель-1` or `Насос/2`) are not specificity. A model such as `RDF 310` is sufficient only when it is separately owned by the source row and corroborated by compatible history. Generic labels, unresolved required attributes, fuzzy similarity, popularity, and price similarity do not pass this guard. A fuzzy retrieval may supply REVIEW candidates but is never promoted to exact identity.

For both bases, item integrity and selected provenance must be valid. The selected event must have a finite usable price strictly greater than zero and a valid, non-future source date. No age cutoff is applied; the selected date and derived age are audit metadata. The read model preserves the source currency, including an empty value. When adapting an event to a sourcing `Offer`, an explicit source currency is retained and normalized to uppercase by `Offer`; an absent or blank source currency resolves to the company default `RUB`. `data_provenance.currency_basis` records `source` or `company_default`. The active snapshot is not rewritten. Availability remains unknown, and a historical price is not a current quote.

## Routing, totals, cache, and audit

`one_c_then_provider` is per row, not all-or-nothing for a project. On fallback, only the live provider's offers and matches are returned; history REVIEW candidates are not mixed into provider ranking or AI reranking. A provider failure never promotes a REVIEW history candidate.

Project match counts include SAFE history matches and accepted provider matches, with separate history/provider/fallback, REVIEW, no-match, and unavailable counters. Existing confirmed and alternative monetary totals remain live-provider-only; historical prices never create or enter a current currency total, including when the displayed historical offer uses the company-default currency.

No final routed-result cache is used. The history projection continues to be cached by active snapshot version; existing provider subresult caching remains keyed by provider, provider catalog version, and intent fingerprint. Thus provider-only cache entries cannot answer `one_c_only`, and a snapshot activation is observed before a routed lookup. The routing policy is identified as `one-c-routing-v2` in route metadata; this revision includes the history-offer currency default and its provenance.

Document sourcing run history stores the selected mode and bounded per-row route fields: history outcome, SAFE basis, snapshot version, fallback decision/status (`not_called`, `completed`, or `error`), final source kind, and fallback provider identity/version. A normally completed provider search remains `completed` even with zero offers; health or search failure is `error` and has final source kind `none`. It does not store history candidate lists, contracts, raw source facts, or filesystem paths. The history provider is injected separately from live providers and receives the same repository instance already owned by the application and import service.

## Shared snapshot leases and displayed results

Every sourcing operation that can read 1C history takes a shared lease for its complete operation. Single-row lookup retains the lease until its result is serialized. A project acquires its lease before job submission and holds it through queue ownership, worker completion, or failure; submission errors also release it. At project start the service captures the active `history_catalog_version`. Every history-backed row must use that version; a mixed or unexpected version fails closed rather than combining snapshots.

Final history activation takes an exclusive writer lease. While any reader is active, replacement fails with `ONE_C_HISTORY_IN_USE`; once a writer is admitted, new `one_c_only` and `one_c_then_provider` sourcing requests fail with `ONE_C_HISTORY_UPDATING`. A competing importer receives `ONE_C_HISTORY_UPDATE_IN_PROGRESS` immediately. `provider_only` does not acquire the history lease and remains available while a writer is active. The import UI may prepare and analyze a preview during sourcing, and a busy final-import response preserves that preview and its mapping for retry.

An already displayed result remains tied to its original catalog version after a replacement. The UI shows a neutral notice to rerun for the new history; it does not recalculate or relabel the old result automatically.

`OneCHistoryActivityRegistry` is process-local and coordinates one application process. Supported production topology for this release is Caddy → one Averon Import application process → one uvicorn process / one worker. Multiple workers or replicas are not supported until this coordinator is backed by a cross-process lease. No Redis or distributed lock is currently used.
