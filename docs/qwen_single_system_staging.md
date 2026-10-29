# Isolated staging result — 2026-10-29

Runtime candidate: `8179b490e0cba59ccf8ba90f3f65325290a1983b`.
Baseline: `9d1a46f2c9a06f7ed3fb1187ac0dad30e5f38af0`.
The candidate was tested from a clean detached checkout. No application source,
dependency, production config, service or systemd override was changed.

## Evidence boundary

**Local Windows staging: 42 PASS, 0 FAIL. VPS staging: not performed.**
The SSH ED25519 pin matched the previously trusted fingerprint, but BatchMode
authorization returned `Permission denied (publickey,password)` both from the
sandbox and the normal Windows account. No authenticated VPS command ran.
Actual production MainPID, effective WorkingDirectory/ExecStart, active drop-ins,
loaded SHA and Ubuntu/Caddy compatibility therefore remain **unverified**.
The local result must not be substituted for a production inventory or a VPS
staging result. Production deployment remains blocked on those checks and
separate approval.

The last operator-confirmed production path is `/opt/averon/pilot-next`, selected
by `/etc/systemd/system/averon.service.d/zz-luna-9d1a46.conf`. The corrected
[runbook](qwen_single_system_release.md) inventories the current process before
choosing a checkout and rolls back the same verified checkout, preserving all
unit/drop-in files byte for byte. It does not assume `/opt/averon/app` is active.

## Test environment

* Separate private temporary data root; all databases, documents, review,
  checkpoints, test accounts, TLS material and exported files remained inside it.
* Three synthetic documents: old no-context result, old contextual v1 result, and
  a fresh document. Old checkpoints were generated with the actual baseline
  client/adapter loaded read-only from Git and a fake transport. No customer
  document, historical customer transcript or production database was copied.
* Original table fixture: 25 rows, nine columns, 225 source cells. Role decisions
  and corrections were explicit synthetic Human Review data, not model approvals.
* Real Uvicorn HTTPS on `127.0.0.1:8767`, temporary local certificate trusted only
  by the test client. Global TLS trust and existing servers were unchanged.
* Session auth, Secure/HttpOnly session cookie, separate owner/other/admin users.
  No dependency override bypassed authentication. `local_dev` was not used.
* AI-first UI/fake flags enabled for staging; live flag disabled. An external
  test harness supplied the fake Qwen adapter through the runtime-builder seam
  for fake-mode requests. Qwen mode itself stayed disabled and returned 403.
* Process-scoped empty memory secret store and cleared inherited Averon/Yandex
  credentials; Windows Credential Manager and production secrets were not used.
  Non-loopback socket connections and the real Qwen HTTPS transport were blocked.
* Historical fixture generation and the staging runtime used separate normal
  process-local quota registries. Application limiter rules were not modified.

This was an actual HTTPS/API integration test with a server restart, not browser
automation or a new hosted-Qwen accuracy test. The staging server was stopped
afterward. Only its own child processes were terminated.

## Acceptance matrix

| Criterion | Result | Evidence |
| --- | --- | --- |
| Exact candidate / separate port and storage | PASS | Detached `8179b490`, loopback 8767, all SQLite/document/checkpoint files under a fresh private root |
| Unauthenticated access | PASS | `/api/me` returned 401 |
| Session login / authenticated health | PASS | Login and `/api/me`, `/api/health` returned 200; Secure/HttpOnly cookies retained |
| CSRF | PASS | Mutation without CSRF returned 403; valid session/CSRF mutation returned 200 |
| Cross-user and admin boundaries | PASS | Other user's document access returned 404; ordinary user diagnostic export returned 403 |
| Saved baseline Human Review | PASS | Old no-context and contextual results opened: 25 rows / 225 cells each, JSON bytes unchanged by reading |
| PDF rendering | PASS | Authenticated page PNG returned 200 |
| Corrections / original / confirmation | PASS | Synthetic quantity changed from the saved reviewed value to a new value; confirmation retained it and the original OCR value stayed separate |
| Old contextual checkpoint | PASS | HTTP 409 `checkpoint_identity_mismatch` before transport; checkpoint/review/report JSON hashes unchanged |
| No-context checkpoint | PASS | Resume completed with zero new fake transport calls |
| Fresh contextual request | PASS | One fake request, one system message, output cap 32768, reasoning `none`; no real Qwen transport |
| Preview / user XLSX / diagnostic XLSX | PASS | Three states: before editing, after confirmation, after restart; HTTP 200 for each export |
| Export content / admission | PASS | Nine original headings/order, all 225 raw cells in admin audit, one confirmed standalone position on the specification; other rows were not promoted |
| Formula injection | PASS | A synthetic formula-like source string stayed text; no formula cells in any of the six workbooks |
| Restart persistence | PASS | Session remained valid; draft, corrected/confirmed values, role decisions and full review history matched before/after restart |
| Private storage | PASS | Native DACL/owner checks, including strict AI-first storage checks; no ACL changes used to pass the tests |
| Real Qwen HTTP | PASS | Zero; real mode disabled, external transport blocked |
| Actual production process / active checkout | FAIL — blocked | SSH authorization unavailable; last known path is not represented as freshly verified |
| Ubuntu VPS / actual Caddy smoke | FAIL — not run | No VPS access; local HTTPS does not attest production proxy configuration |

The 42 automated assertions expand the grouped local criteria above; they do not
include a claimed PASS for either blocked VPS criterion. All workbooks were opened
with openpyxl and source/current values compared programmatically. No customer
values, fixture secrets, private TLS keys or absolute personal paths are in Git.

## Windows harness findings

Initial harness runs exposed fixture/test-assumption issues, not candidate source
regressions: Windows short/long path aliases, implicit text decoding, expecting
the admin audit sheet in the user workbook, missing save-draft token/edits, and
mixing historical fixture reservations into the current fake quota window. These
were corrected only in the external test harness; application assertions and
runtime guards were not relaxed.

The final permission check also needed to distinguish the exact AI-first ACL
contract from four legacy `chmod(0700)` directories. Those directories contained
`S-1-3-4` (Owner Rights), SYSTEM and Administrators, not an arbitrary extra user.
[Microsoft documents Owner Rights as the object's current owner](https://learn.microsoft.com/en-us/windows-server/identity/ad-ds/manage/understand-security-identifiers).
The harness checked their actual owner SID through `GetNamedSecurityInfoW`,
required it to match the current process user, and required the exact protected
three-ACE/full-access/inheritance pattern. Every AI-first path still passed its
unchanged strict helper. No original or staging DACL was reset to make the test pass.

The external harness and detailed private JSON report remain local. Prior failed
harness reports were retained; the final run used a fresh root and passed all 42
checks. The previously reported 463-test profile was not relabeled as a fresh
Ubuntu run. Only documentation and the read-only inventory helper change after
the accepted runtime candidate.
