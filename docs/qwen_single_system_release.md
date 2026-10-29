# Qwen contextual request: minimal release candidate

Base: `9d1a46f2c9a06f7ed3fb1187ac0dad30e5f38af0`. Prepared 2026-10-29.
This is a review/staging/deployment **plan**, not deployment authorization.
No new paid request is needed for the offline checks below.

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

Preparation results: **463 passed, 0 failed, 0 errors, 0 skipped** across the
16 profile files above. The focused client/adapter run passed 148 tests. Temporary
test storage was outside the repository; customer files and protected workspaces
were not used. Production and staging were not contacted.

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

No commands in this section have been run against the VPS for this task.

1. Recheck the actual production SHA, service, clean Git state and dependencies.
   If production no longer matches the reviewed base, stop and review the new diff.
   Record the exact previous SHA and verify that it is locally available for rollback.
2. Before switching code, create and verify a private backup. Quiesce application
   writes for a coherent document/review snapshot; back up SQLite using its backup
   API (or after confirmed clean shutdown), not an unsafe copy of an active DB.
   Include documents, review/editor state, checkpoints and required service/config
   recovery material without printing secrets. Hash the archive and restore it to
   a temporary private location; run SQLite integrity checks and data readback.
   Stop if backup restoration or previous-code rollback cannot be verified.
3. Only after approval and a verified backup: stop `averon.service`, switch
   `/opt/averon/app` detached to the exact reviewed candidate, and start the service.
   There is no migration, dependency upgrade, env edit, ACL change or checkpoint
   conversion in this patch. Do not reset dirty files or delete user data.
4. Check internal HTTP, configured Caddy HTTPS host, session auth, old document
   readback, Human Review and test XLSX on isolated test data. Confirm both OCR
   choices remain available under their existing flags. Do not perform paid OCR
   or modify real review decisions during smoke. Preserve existing budgets, quotas,
   concurrency and feature flags. Mark live OCR as not retested after deployment.
5. If health, auth or saved-data readback fails: stop the service, detach back to
   the recorded previous SHA and restart; repeat the same smoke checks. Keep v2
   checkpoints/results. Do not automatically replace the live data root or database
   from backup: data restoration needs a separate corruption diagnosis and a
   coordinated maintenance decision, to avoid losing newer user edits.

Release remains pending independent review and separate deployment approval.
