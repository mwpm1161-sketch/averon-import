# M5C: VI configuration and controlled runtime

M5C starts at `4738ba7579d231c8f5b496314972827b68355d7f` on
`feature/manual-tender-xlsx-v1`. It adds local configuration and conditional
registration for the existing outcome-native VI adapter.

## Configuration and credentials

`AppSettings.sourcing.vseinstrumenti` owns `enabled`, `environment`, FIAS
`region_id`, and request timeout. A server-generated random credential
generation is persisted with the settings so replacing or deleting the token
changes VI request affinity. The generation is omitted from public settings;
only its opaque hash appears in execution affinity.

The Bearer token is stored under `vseinstrumenti.bearer_token` in the existing
`SecretStore`. A separate non-secret presence marker supports status and
runtime registration without asking the Windows backend to load the token.
Admin-only settings operations can save, replace, and delete it. Responses
expose only a configured boolean. The token is not copied to `settings.json`
or returned by settings APIs.

Runtime composition checks local settings and secret presence, then registers
VI only in `SourcingRuntime.execution_providers`. It does not add VI to the
legacy provider map or the M4A public provider list. The SecretStore-backed
client loads the token only when an explicit outcome-native search executes.
Startup, status, and runtime rebuild do not contact VI or read the token value.
Credential rotation and search credential resolution share a lock; a stale
runtime fails closed if the configured generation changed before execution.

## Compatibility and boundaries

M4A public capabilities remain limited to ETM and Lemana. M3D's internal
provider allowlist remains unchanged. Legacy sourcing, 1C history, durable v2,
and export behavior are not routed through VI. No UI activation, provider
probe, startup search, sync, retry, or health request is added. M5D is outside
this stage.

M5D follow-up: the authenticated Excel tender comparison now includes VI only
when the active outcome-native runtime, enabled local settings, valid region,
and generation-bound credential marker all agree. See
[MULTI_PROVIDER_M5D.md](MULTI_PROVIDER_M5D.md) for the public-selection boundary.

## Verification

Tests use `MemorySecretStore` and fake transports. They cover write-only token
handling, replacement/deletion, generation-based dedup separation, conditional
execution-only registration, explicit fake search, and silent startup/status/
runtime rebuild.
