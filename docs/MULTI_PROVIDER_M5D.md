# M5D: generalized public provider selection

M5D extends the authenticated Excel tender comparison introduced by M4A. The
existing request without a `providers` field still uses the original single
provider v1 path. The explicit comparison request keeps the M4A provider-only
mode, 100-row limit, 20-result limit, durable v2 storage, and no-export rule.

## Admission and availability

The public selection is a closed allowlist of ETM iPRO, Lemana PRO B2B, and
ВсеИнструменты.ru. A comparison selects two or three distinct providers;
`local_catalog` and all unknown keys remain unavailable in this public flow.
The request is rechecked against the current runtime before a job is admitted.

ETM and Lemana are advertised only when their execution adapter is registered
and its local configuration reports credentials configured. This reads no
supplier status and performs no authentication request. VI is advertised only
when its outcome-native adapter is registered and the current VI settings are
enabled, its region is valid, and its generation-bound SecretStore marker
matches. Capability reads inspect only that marker, never the Bearer token.

Capability reads are authenticated metadata operations. Opening the Excel
tender screen, reading capabilities, and changing source mode or provider
checkboxes do not execute a provider. Supplier calls begin only after an
explicit provider-only search is admitted.

## Execution and projection

The route captures one runtime generation and calls the existing controlled
provider-only orchestration. Every selected supplier gets its own outcome,
retained offers, matching and commercial evidence; composite `(provider_key,
offer_id)` identities keep equal supplier offer IDs distinct. VI runs through
`SourcingRuntime.execution_providers`; it is not added to the legacy provider
map. Its durable allowlisted provenance keeps the supplier SKU, price field,
environment, and region while omitting generation-derived configuration data.

Public v2 DTOs allow only the three named providers and expose provider labels,
outcomes, offers, and independent failure categories. Runtime affinity,
configuration revisions, credential generations, credentials, and raw
provenance remain outside the public response. Durable v2 comparison runs stay
outside the existing XLSX export options. PDF-to-Human-Review, 1C history,
legacy sourcing, and existing export paths are unchanged.

## Verification

M5D tests use local fixtures, `MemorySecretStore`, and fake outcome adapters or
fake transports. They cover conditional VI visibility, missing-generation
marker rejection, two-provider M4A compatibility, three-provider selection,
provider-specific outcomes, VI durable provenance, and safe public projection.
