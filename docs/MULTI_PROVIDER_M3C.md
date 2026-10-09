# M3C controlled durable-v2 storage integration

M3B is FINAL APPROVED at `9a78c94427ae2350beca22f13789421cfc7cbf53` on
`feature/manual-tender-xlsx-v1`. M3C adds an explicit internal terminal-v2
persistence/read boundary, exact approved compact bytes, schema-aware retention,
and atomic no-overwrite publication. Current production writes remain v1.

## Storage audit and boundaries

| Existing path | M3C behavior |
| --- | --- |
| `create_running`, `complete`, `fail`, `_atomic_write` | Existing v1 implementation and file shape unchanged; no v2 encoder/projection calls |
| `_read_path`, `get_public` | Existing v1-only reader/response unchanged; v2 safely rejects as `TENDER_RUN_CORRUPT` |
| `list_public` | Existing v1-only listing; v2 entries omitted |
| Restart recovery | Existing v1 running -> interrupted behavior unchanged; terminal v2 and external running-v2 are never rewritten |
| `_prune_terminal` | Separate internal `_read_any_path` accepts legacy payloads or strictly validated, correlated terminal v2 |
| `reserve_tender_run_storage` | Existing conservative 1 MiB reservation, used by explicit v2 persistence as well |
| `_workspace_storage_bytes` | Already sums every file recursively; counts both schemas, corrupt files and temporary files |
| Workspace delete/expiry | Existing whole-tree removal naturally removes both versions |
| History decisions/export readers | Remain v1-specific; no callsite or format changes |

One run ID maps to one `runs/<run_id>.json` in the confirmed workspace. There
are no sidecars, external directories, alternate copies, upserts or migrations.
The short-lived atomic temporary is in the exact `runs/` directory and is
removed after publication or failure where possible.

`TenderSourcingRunStore.persist_durable_v2(workspace_path, run)` accepts the exact
`DurableTenderSourcingRunV2` type, with `completed`, `failed` or `interrupted`
status. Running and dictionaries reject. The approved M3B encoder performs its
deep revalidation, so bypassed/corrupt model instances cannot reach storage.
The layer consumes only the already-built durable DTO. It never reconstructs
runtime execution, identity matches, commercial evidence or winner decisions.

The explicit method and the reader hold the store lock and repository lock
through their operation. The repository lock prevents its cleanup/delete or
metadata update from racing validation and publication. There are no production
orchestration/API/UI callsites for either explicit method.

## Workspace correlation

Resolve the existing workspace under this repository's confirmed workspace
root; its directory name must be a normal tender ID. Read bounded server-owned
metadata, reject duplicate JSON members and validate exact tender ID, owner
presence, strict integer revision, lowercase source SHA and offset-aware TTLs.
Preview, missing, expired and foreign-root paths reject. Read and hash the
bounded existing source XLSX to verify it still matches metadata. Redirected
metadata/source files and redirected `runs/` directories reject.

Both explicit persistence and explicit read require exact tender ID, source
SHA and current revision equality. The filename must equal the DTO's run ID.
No normalization, repair, revision upgrade, generated ID, timestamp change or
TTL touch occurs. Stale/mismatched snapshots reject.

## Atomic immutable publication

An existing destination, including a v1 file, identical v2 bytes, corrupt file
or broken symlink, conflicts before encoding/temp creation. Independently, the
publication primitive guarantees no overwrite if a competing writer creates
the destination after that initial check.

Encode fully with `encode_tender_sourcing_run_v2` before creating the runs
directory or any temporary file. Preserve both caps: v2 **786,432** bytes and
repository **1,048,576** bytes. Reserve storage through the existing repository
policy before writing. Storage never reserializes the returned bytes.

Create a same-directory `mkstemp`, apply `0600` permissions, write all bytes
(reject a short write), flush and fsync the file, and close it. Publish with
`os.link(temporary, target)`, then remove the temporary. This is an atomic
create-if-absent equivalent to atomic rename, with stronger collision behavior
than `os.replace`, which would overwrite a competing terminal run. A hard link
exposes the complete fsynced inode and fails if the destination already exists.
POSIX file/directory permissions are checked; Windows follows the existing
server-owned directory inheritance model. A filesystem unable to create hard
links fails with a bounded storage error rather than falling back to overwrite.
Directory fsync is not added; durability matches the existing store's file-fsync
standard. Descriptor/temp cleanup covers pre-publication failures.

## Internal reading and coexistence

`read_durable_v2` validates the run ID, reads at most the repository cap plus one byte,
calls the approved `read_tender_sourcing_run`, requires its exact v2 DTO, and
rechecks workspace/file correlation and terminal status. It returns the frozen
DTO. V1/unknown versions, malformed JSON/wire, oversized files and mismatches
produce fixed-message, closed-code errors without paths, payloads or exception
strings. No alternate public JSON serializer is introduced.
The approved discriminator enforces the lower v2 cap. A legal v1 file between
768 KiB and 1 MiB still receives the explicit version error, not a v2 size error.

Housekeeping uses a separate bounded reader, not the legacy `_read_path`.
Terminal v1/v2 share the existing completion-time (created-time fallback)
ordering and unchanged five-run retention cap. Exact timestamp ties use run ID
for deterministic ordering, independent of schema or directory enumeration.
Pruning removes only excess valid correlated terminal runs and their existing
history-decision counterpart, following the existing policy. Inserting an older
snapshot into a full workspace can immediately prune that snapshot.

Recovery still uses the unchanged v1 reader and interruption writer. Terminal
v2 bytes are preserved unless normal mixed retention prunes an old run. External
running-v2, malformed, unknown-version and metadata-mismatched files are ignored
by recovery/retention, left on disk, never repaired/quarantined or counted as
valid terminal runs. Their bytes still count toward the global storage quota.
They cannot disrupt healthy v1 recovery or force deletion of healthy runs.

## Verification scope

Focused tests exercise completed/failed/interrupted round trips, running/type
rejection, byte equality, unchanged input DTO, portable permissions, existing
v1/v2/corrupt conflicts before mutation, concurrent duplicates and a racing v1
publication. Fault injection covers encoder, temporary creation, chmod, fdopen,
write/short write, flush, fsync and the equivalent atomic publish step. Every
pre-publication failure leaves no new final file or temporary and preserves
unrelated run bytes.

Real repository-confirmed temporary workspaces exercise source/workspace/revision
checks, strict IDs, malformed/version/wire/size reads, v1/v2 recovery coexistence,
mixed pruning, deterministic ties, quota blocking/accounting and workspace
delete/expiry. Approved M3B fixtures pass through the real store:

| Fixture | Exact compact stored bytes | Result |
| --- | ---: | --- |
| 370x2 / 740 retained offers | 734,507 | Exact stored bytes and read-back DTO |
| 500x1 / 500 retained offers | 583,927 | Exact stored bytes and read-back DTO |
| Legal exact byte budget | 786,432 | Exact stored bytes and read-back DTO |
| First over budget | 786,433 | Rejected before runs directory/target creation |
| 500x2 | 992,427 | Rejected before runs directory/target creation |

Capacity fixtures retain the original M3B product/decision data; binding to the
confirmed test workspace changes only fixed-width tender ID/source hash values.
Stored bytes are exactly the independent M3B encoder output. Runtime secret-shaped
discarded fields stay absent, and guard tests forbid provider runner, matching,
commercial resolvers/selection, full runtime Offer dumps and network calls.
V1 lifecycle spy tests forbid v2 projection/build/encode/persist calls and prove
all current lifecycle/recovery files still have schema 1.

M3C does not activate v2 in the current production flow, select providers, execute
suppliers, use AI, expose API/UI, alter exports, add VI, add a feature flag,
dual-write, deploy, merge or tag.

## Completed gates

- M3C storage: **67 passing cases** in the final focused/full gates.
- Focused M0/M0.1 through M3C (provider contracts/capabilities/runner/adapters/
  runtime/matching/commercial/selection and durable read/projection/storage):
  **972 passed**, `.mc9` basetemp.
- Manual tender/D4/history/confirmations/provider/export/warehouse regressions:
  **1,706 passed, 1 skipped**, short system-Temp `mce67046` basetemp.
- Full `tests/`: **2,583 passed, 3 skipped, 3 accepted failures**, short
  system-Temp `mc57840c` basetemp. No other final-gate failures.
- All **8/8** JS lifecycle scripts passed with app.js/styles.css arguments.
- `compileall`, `node --check averon_import/static/app.js`, and Git diff checks
  passed.
- UTF-8 AST comparison against the approved base confirms exact unchanged
  implementations of `create_running`, `complete`, `fail`, `_read_path`,
  `_atomic_write`, `_recover_running_runs`, `list_public` and `get_public`.

The only full-suite failures are the already accepted unavailable-Tesseract
environment baseline in `tests/test_processing_coordinator.py`:

- `test_local_profile_resolves_tesseract_adapter`
- `test_settings_processing_mode_influences_resolver`
- `test_local_mode_rejects_cloud_llm_combination`

The sole regression/full warning is the intentional duplicate ZIP member in
the XLSX preflight rejection fixture. Each run used a separate AVERON_DATA_DIR;
no supplier traffic or production workspace was used for these gates.

Earlier Desktop-based retries encountered intermittent WinError 5 at the
unchanged v1 `os.replace` call, and one parallel full run hit an unchanged
one-second worker-start timeout. Isolated checks passed. Final regression/full
gates ran sequentially in system Temp and reproduced neither issue. No retry
logic or other changes were added to the production v1 writer or job coordinator.
