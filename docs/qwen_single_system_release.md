# Qwen contextual request: minimal release candidate

Base: `9d1a46f2c9a06f7ed3fb1187ac0dad30e5f38af0`. Prepared 2026-10-29.
This is a review/staging/deployment **plan**, not deployment authorization.
No new paid request is needed for the offline checks below.

Latest operator-confirmed production checkout: **`/opt/averon/pilot-next`** at
`9d1a46f`, selected by
`/etc/systemd/system/averon.service.d/zz-luna-9d1a46.conf`. This is historical
evidence, not a substitute for a current process inventory. `/opt/averon/app`
must not be assumed active. A service override can make it irrelevant.

## Read-only production inventory (before selecting any deployment directory)

The following commands do not restart a service, change systemd, or change Git.
Use the previously pinned ED25519 host key; do not replace it using ssh-keyscan.

```powershell
$Pinned = Join-Path $env:USERPROFILE '.codex\known_hosts.averon-d1c-8d5ba60028f04d78ac216076d0981527'
ssh-keygen -F 200.169.191.216 -f $Pinned | ssh-keygen -lf - -E sha256
# Require: SHA256:mM1/01wFFWb5KjyZkEnRpPH4hm08NEPSMET7ivN2Hsk (ED25519).
# Stop on a missing/unreadable pin or mismatch.
$SshReadOnly = @('-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
  '-o', "UserKnownHostsFile=$Pinned", '-o', 'ConnectTimeout=10')
Get-Content -Raw -LiteralPath '.\docs\averon_service_readonly.py' |
  ssh @SshReadOnly root@200.169.191.216 'python3 -B -'
```

The script is sent through stdin; no script is installed on the VPS. It reads
effective MainPID, WorkingDirectory, ExecStart, FragmentPath and **all** drop-ins,
process cwd/executable/arguments, invocation ID/start time, and Git HEAD/cleanliness
for the relevant directories. Drop-in contents and raw command lines are not
printed: drop-ins are hashed, and command arguments are strictly redacted except
for known module/bind/port/app-dir fields. It never reads environment files or
imports Averon. Do not use `systemctl show ... Environment`, raw `/proc/.../environ`,
or an unrestricted `systemctl cat` dump for the report.
Python bytecode writes are disabled with `-B`; Git status uses
`--no-optional-locks` so the inventory does not refresh the index on disk.

In an already trusted SSH session, the basic property checks are also read-only:

```bash
systemctl show averon.service --no-pager \
  -p MainPID -p ActiveState -p SubState -p WorkingDirectory \
  -p FragmentPath -p DropInPaths -p ExecMainStartTimestamp -p InvocationID
# Use the stdin Python inventory above for safe ExecStart/cmdline and Git checks.
```

Record the actual process cwd, effective WorkingDirectory and any `--app-dir`.
Select `ACTIVE_CHECKOUT` only after these agree on the source used by the process;
if they disagree, stop and resolve the import path. Never fall back to a guessed
directory. `disk_checkout_sha` is not proof of an already-loaded Python SHA if
files changed after startup. The script reports `running_sha=null` until a trusted
startup/build record can be tied to the same MainPID/invocation. Do not label a
disk HEAD as a proven loaded SHA. Lack of SSH authorization is a blocked check,
not permission to substitute a local Windows server for production inventory.

## Evidence and scope

The operator reports a controlled pair with the same image, hint, model, schema,
parameters, and equal body length: B (one system message) returned HTTP 200,
`stop`, 49,063 ms, 3,070 input / 12,096 output tokens; A (two system messages)
returned HTTP 400 in 609 ms. There were no retries or production changes.

This confirms an effect of message structure **in this configuration**. It does
not establish the precise internal Yandex validator rule or a universal model
restriction. It does not explain the separate full-page/crop failure scenario.

Only two runtime files change:

* `ocr/qwen_vision_client.py`: for nonempty H, send exactly one system message
  containing `_PROMPT + "\n\n" + _USER_CONTEXT_POLICY`. Both original constants
  are retained verbatim. Exact H remains exclusively in the untrusted user part.
* `ai_first/qwen_vision_adapter.py`: bind the actual contextual request template
  into the existing private checkpoint scope. The exact hint remains HMAC-bound.

The no-context (`None` / empty string) wire bytes, base prompt hash and checkpoint
scope stay unchanged. Whitespace-only H remains a nonempty hint. The 1000-character
limit, body/image bounds, token-budget checks and no-retry behavior remain active.
The production body has no experimental trailing whitespace. Existing reservation
logic still measures the actual added serialized bytes; its 31-byte contextual
overhead reduction follows from removing the second message wrapper, not a token
limit adjustment. No output/input settings, DPI, crop, model, endpoint, schema,
strict mode, reasoning, auth, Human Review, UI, XLSX or legacy OCR logic changes.

The research commit `40da4aa` must **not** be cherry-picked for this release.
The experiment CLI, safe-error diagnostics, sidecars and orchestration changes
from that branch are intentionally excluded from this candidate.

## Checkpoint and Human Review compatibility

The contextual policy version becomes `document-context-v2-single-system`.
An additional private hash binds policy text, message layout, and serialized
parameters using fixed placeholders, not the real document or hint. No public
descriptor field, checkpoint schema, review schema or database schema is added.
No migrations or rewrites of saved data are required.

* No H: existing v1 checkpoints still match and resume normally.
* Nonempty H: existing v1 checkpoints fail identity checks before HTTP. The
  current API returns 409 `checkpoint_identity_mismatch` and offers a new run.
  It does not silently retry or change the manifest.
* Saved transcription, review decisions, confirmed roles, event history, pending
  editor values and old reports remain readable. Reading them does not require
  recomputing the current adapter identity.
* Only an explicit new recognition run creates a new checkpoint; the existing
  application snapshot mechanism preserves the previous state. A newly recognized
  page retains the existing review-invalidation rules. The patch does not transfer
  stale confirmations to a new extraction revision.
* Do not edit fingerprints, clear checkpoints, delete results, or automatically
  restart an interrupted paid run to overcome an identity mismatch.

After a code rollback, new contextual v2 checkpoints likewise cannot resume as v1.
Their stored results remain readable by the old schemas; their files must be kept.
Compatibility means readback and safe refusal, not continued execution of a changed
request under an old identity.

## Offline verification

Use a fresh permitted temporary directory outside the repository and a separate
test data root. Do not change permissions of existing data/venvs or load customer
documents into the test process. Tests use synthetic hints/PDFs and fake transports.

```powershell
$TestRoot = Join-Path $env:TEMP ('averon-context-' + [guid]::NewGuid().ToString('N'))
$env:AVERON_DATA_DIR = Join-Path $TestRoot 'data'
$Tests = @(Get-ChildItem tests/test_ai_first*.py,tests/test_qwen_vision*.py |
  ForEach-Object { $_.FullName })
$Tests += @('tests/test_account_auth.py', 'tests/test_auth_roles.py')
python -m pytest @Tests -q --tb=short -p no:cacheprovider --basetemp $TestRoot
```

New regressions cover exact user-only hint preservation, including synthetic
injection text, Unicode and the length boundary; unchanged no-context bytes with
and without reasoning; unchanged transport parameters and image/schema; contextual
scope changes after policy/layout edits without a manual version bump; old v1
scope compatibility; and refusal to resume v1 context while retaining all JSON
bytes, corrected/confirmed values, roles and review history. Existing tests cover
request limits, output truncation, cancellation, quota/budget, no retries, review,
export guards, session auth and cross-user document access.

Initial preparation results: **463 passed, 0 failed, 0 errors, 0 skipped** across the
16 profile files above. The focused client/adapter run passed 148 tests. Temporary
test storage was outside the repository; customer files and protected workspaces
were not used. Production and staging were not contacted during that initial
preparation. The subsequent isolated staging result and SSH access limitation
are recorded in [the staging report](qwen_single_system_staging.md).

An additional synthetic check loaded the actual client/adapter/readers from
`git show 9d1a46f:...`, not only reconstructed expected dictionaries. It confirmed:
16 byte-identical no-context requests, 24 contextual requests differing only by
the intended message merge, 12 matching public-descriptor/private-scope comparisons
(empty scopes preserved, nonempty scopes invalidated), exact prompt/policy text,
and v1 checkpoint refusal without a new fake call or JSON changes. The actual
baseline reader accepted the candidate's canonical page checkpoint, report and
review data. These are offline compatibility checks, not a new hosted-model test.

## Staging acceptance gate (no Qwen HTTP)

1. After independent review, verify the candidate SHA and exact base. Inspect the
   complete diff: two runtime files, tests and this document only. Dependencies,
   migrations, main routes, configuration and production ACLs must be unchanged.
2. Use a separate staging checkout and private data root, bind only
   `127.0.0.1:8767`. Preserve production dependencies/auth settings, but point all
   storage at staging. Keep `AVERON_AI_FIRST_LIVE_ENABLED=0`. Do not reuse a live
   production job queue, checkpoint path, SQLite file, writable document root or
   credential-changing settings UI. Prevent Qwen network egress in the test process.
3. Run the profile above, including fake HTTP serialization. Do not use a successful
   health response as evidence of hosted-model compatibility.
4. With session auth, verify unauthenticated denial, login/CSRF, owner document
   access and cross-user denial. Do not enable production `local_dev`.
5. Use synthetic old-format checkpoints/review, or an expressly permitted private
   copy: open/reopen saved results, confirm corrected values/roles/history, render
   PDF and generate the test XLSX. Keep the original workspace untouched.
6. Demonstrate old no-context resume with fake HTTP (no new call for completed
   pages); old contextual resume must refuse before HTTP without changing state.
   Explicit new-run snapshot behavior is tested with synthetic/fake processing.
7. Inspect the serialized fake contextual payload: one system, full policy, exact
   H only in user, unchanged model/schema/strict/output/reasoning. Confirm zero
   real Qwen requests. Stage acceptance is not a new paid OCR experiment.

## Deployment gate and rollback (future separate approval only)

No mutating commands in this section have been run against the VPS for this task.

1. Recheck the actual production SHA, service, clean Git state and dependencies.
   If production no longer matches the reviewed base, stop and review the new diff.
   Record the exact previous SHA and verify that it is locally available for rollback.
   Also record `ACTIVE_CHECKOUT`, MainPID/invocation, effective WorkingDirectory,
   safe ExecStart, unit FragmentPath and every drop-in path/hash from the read-only
   inventory. Verify whether the last known `pilot-next` override is still active.
   If this cannot be established, deployment is blocked.
2. Before switching code, create and verify a private backup. Quiesce application
   writes for a coherent document/review snapshot; back up SQLite using its backup
   API (or after confirmed clean shutdown), not an unsafe copy of an active DB.
   Include documents, review/editor state, checkpoints and required service/config
   recovery material without printing secrets. Hash the archive and restore it to
   a temporary private location; run SQLite integrity checks and data readback.
   Stop if backup restoration or previous-code rollback cannot be verified.
3. Only after approval and a verified backup: stop `averon.service`, switch the
   **verified `ACTIVE_CHECKOUT`** detached to the exact reviewed candidate, and
   start the service. If the latest confirmed configuration is still in effect,
   this is `/opt/averon/pilot-next`, not `/opt/averon/app`. Preserve the existing
   `zz-luna-9d1a46.conf` and all other drop-ins byte for byte; recheck their hashes.
   Do not add/remove overrides, change ExecStart/WorkingDirectory, run daemon-reload,
   or start a different checkout to work around ambiguity.
   There is no migration, dependency upgrade, env edit, ACL change or checkpoint
   conversion in this patch. Do not reset dirty files or delete user data.
4. Check internal HTTP, configured Caddy HTTPS host, session auth, old document
   readback, Human Review and test XLSX on isolated test data. Confirm both OCR
   choices remain available under their existing flags. Do not perform paid OCR
   or modify real review decisions during smoke. Preserve existing budgets, quotas,
   concurrency and feature flags. Mark live OCR as not retested after deployment.
5. If health, auth or saved-data readback fails: stop the service, detach the **same
   recorded `ACTIVE_CHECKOUT`** back to the recorded `PREVIOUS_SHA`, verify unchanged
   unit/drop-in hashes, and restart; repeat the same smoke checks. Do not roll back
   an inactive `/opt/averon/app`. Recheck the new MainPID, cwd and safe ExecStart
   after either switch. If unit/drop-in hashes changed, this code-only rollback
   plan no longer applies: stop and obtain a reviewed configuration rollback.
   Keep v2
   checkpoints/results. Do not automatically replace the live data root or database
   from backup: data restoration needs a separate corruption diagnosis and a
   coordinated maintenance decision, to avoid losing newer user edits.

Release remains pending independent review and separate deployment approval.
