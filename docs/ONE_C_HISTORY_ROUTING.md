# 1C-first sourcing routing (Phase 2B-1)

The backend accepts three `source_mode` values on row and project sourcing requests:

| Mode | Behavior |
| --- | --- |
| `provider_only` | Backward-compatible default. Runs the existing provider path and does not read the 1C history snapshot. |
| `one_c_only` | Uses 1C history only. SAFE results can be accepted; REVIEW candidates have no automatic recommendation; missing or unavailable history returns a typed notice without provider fallback. |
| `one_c_then_provider` | Checks 1C per row. A SAFE result suppresses the provider call; every other outcome falls back once to the configured provider for that row. |

The current UI does not send or expose these modes. The default therefore keeps existing user behavior unchanged. `/api/sourcing/search-intent` rejects history modes because that endpoint does not carry a trusted source baseline.

## History identity policy

Retrieval and the existing matcher use the resolved `ProductIntent`. A history SAFE decision is additionally gated by the deterministic `baseline_intent`, which owns source article, exact name, unit, model, manufacturer, and required attributes. AI-only inferred fields, including preferred attributes, cannot establish the history identity basis.

`EXACT_ARTICLE` retains the strict article match and uniqueness checks: a non-empty baseline article, exact source-backed article, strict matcher result, compatible known units, valid item/variant/event provenance, no item integrity conflicts, one canonical item, a usable positive price, and a valid non-future purchase date. A source article cannot be bypassed by the name policy.

`EXACT_SOURCE_NAME_UNIT` is a separate deterministic policy for source rows without an article. Its name key applies Unicode NFKC, casefolding, `ё` to `е`, NBSP normalization, trimming, and whitespace collapse while preserving punctuation, token order, and model characters. The baseline name must exactly equal a descriptive history variant name, and source, variant, and selected event units must share a known canonical family. The exact name and unit must resolve to one canonical item across the full snapshot. A stronger punctuation/spacing collision key is used only to reject ambiguous identities; it is never positive match evidence.

Name-only acceptance also requires a source-owned specificity signal: an exactly compatible source model, manufacturer, resolved deterministic required attribute, or an explicit specification/identifier token in the exact source name. Generic labels, unresolved required attributes, fuzzy similarity, popularity, and price similarity do not pass this guard. A fuzzy retrieval may supply REVIEW candidates but is never promoted to exact identity.

For both bases, item integrity and selected provenance must be valid. The selected event must have a finite usable price strictly greater than zero and a valid, non-future source date. No age cutoff is applied; the selected date and derived age are audit metadata. Currency remains empty when absent in 1C, availability remains unknown, and a historical price is not a current quote.

## Routing, totals, cache, and audit

`one_c_then_provider` is per row, not all-or-nothing for a project. On fallback, only the live provider's offers and matches are returned; history REVIEW candidates are not mixed into provider ranking or AI reranking. A provider failure never promotes a REVIEW history candidate.

Project match counts include SAFE history matches and accepted provider matches, with separate history/provider/fallback, REVIEW, no-match, and unavailable counters. Existing confirmed and alternative monetary totals remain live-provider-only; an unknown historical currency cannot create or enter a currency total.

No final routed-result cache is used. The history projection continues to be cached by active snapshot version; existing provider subresult caching remains keyed by provider, provider catalog version, and intent fingerprint. Thus provider-only cache entries cannot answer `one_c_only`, and a snapshot activation is observed before a routed lookup. The routing policy is identified as `one-c-routing-v1` in route metadata.

Document sourcing run history stores the selected mode and bounded per-row route fields: history outcome, SAFE basis, snapshot version, fallback decision, final source kind, and fallback provider identity/version. It does not store history candidate lists, contracts, raw source facts, or filesystem paths. The history provider is injected separately from live providers and receives the same repository instance already owned by the application and import service.
