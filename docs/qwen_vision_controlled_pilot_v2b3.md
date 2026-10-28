# Qwen Vision controlled pilot runner (V2B.3)

`python -m averon_import.tools.qwen_vision_controlled_pilot` is an isolated
diagnostic runner. It does not select or change the production OCR provider.
It requires one local PDF and one explicit 1-based page number.

## Default and live modes

The default mode is offline preflight. It checks the configured model and
endpoint against the pilot allowlist, resolves whether credentials are
available in a secure store without printing them, validates the operator's
RPM/TPM and tariff values, renders only the selected page, measures the exact
local request body, and checks the declared input-token bound and worst-case
budget reservation. It does not send HTTP. Its `report.json` is written to a
private per-run directory. The live request key must come from the configured
secure secret store; an API-key environment override fails preflight.

A live request requires `--allow-paid-api` and `--confirm-live-preflight`.
The latter confirms the successful local checks: allowed model and endpoint,
rendered request size, protected output ACLs, configured operator limits,
tariff, budget, and input-token bound. It does not confirm provider-account
RPM/TPM quotas. If either account quota is unconfirmed, live also requires the
separate `--accept-unverified-account-quotas` acknowledgement. That flag records
acceptance of a possible provider quota rejection; it does not turn local
limits into verified Yandex quotas.

For quotas confirmed by the operator in the applicable account, pass both
`--verified-account-rpm` and `--verified-account-tpm`. The runner records those
operator-supplied values as verified and rejects local limits above them. It
does not query the account or independently validate that assertion. If either
value is omitted, `account_quota_status` remains `unconfirmed` and the separate
risk acknowledgement is required for live mode. Reports distinguish local
limits from account quota verification. The per-user lock only covers this
pilot runner; the runner cannot verify whether another application or machine
is making requests with the same credential.

The runner fixes concurrency and request count at one, has no retry path, and
only sends the selected page. The request may still fail after all local checks
pass. An HTTP 5xx, timeout, or cancellation after dispatch can have an unknown
provider-side outcome; the runner records that outcome and does not retry it.
Ctrl+C requests cooperative cancellation; after dispatch the runner waits for
the in-flight transport to settle or time out so it can write the final
diagnostic report.

Example offline preflight (replace the PDF and values with those reviewed for
the intended pilot):

```powershell
python -m averon_import.tools.qwen_vision_controlled_pilot `
  --pdf 'C:\path\to\document.pdf' --page 1 --dpi 150 `
  --requests-per-minute 1 --tokens-per-minute 50000 `
  --input-token-upper-bound 10000 `
  --input-rub-per-1k 0.20 --cached-input-rub-per-1k 0.05 `
  --output-rub-per-1k 0.30 --pricing-profile-id 'operator-reviewed-2026-10' `
  --budget-rub 20
```

After the dry-run report has been reviewed and the user has explicitly
authorized one paid request, repeat the same command with
`--allow-paid-api --confirm-live-preflight`. If account quotas remain unknown,
also pass `--accept-unverified-account-quotas`; if verified, pass both verified
account quota values instead. Do not change the model, PDF, selected page, token
bound, output limit, pricing, or budget between the reviewed preflight and live
run. There is no automatic fallback request.

The `--input-token-upper-bound` is an operator-supplied conservative reservation.
This runner cannot independently derive the provider's image-token count, so a
small or guessed value is not safe merely because preflight accepts it. Confirm
it against current provider guidance or a separately approved conservative
calculation, then ensure input bound plus maximum output tokens fit within the
confirmed TPM and the model context window. Yandex publishes a rounded `262k`
context window and a default maximum output of 32,768 tokens for Qwen 3.6
35B-A3B in its [model announcement](https://yandex.cloud/ru/blog/digest-april-2026).
The Chat Completions reference counts completion tokens against context limits
and defines `max_completion_tokens` as including visible and reasoning tokens
([API reference](https://aistudio.yandex.ru/ru/docs/ai-studio/api/Chat-Completions/createChatCompletion)).
This runner conservatively treats the context as 262,000 tokens and
blocks preflight when the input upper bound plus output cap exceeds it. The
bound is a reservation, not a measurement of actual image tokens; a value that
exceeds the context cannot guarantee that the largest permitted prompt and
completion fit. Do not lower it just to pass preflight: establish a defensible
image-token upper bound first. For example, the pair 262,144 input plus 32,768
output reserves 294,912 tokens and is rejected against this context limit.

The tariff flags are also operator-supplied: verify the active
model's uncached input, cached input, and output prices and include a stable,
non-secret profile identifier. The runner checks positive finite values and
that the budget covers the maximum reserved cost; it cannot verify a tariff
against a billing account.

## Quota and launch order

The V2B.2 RPM/TPM quota registry is process-local. It does not know the recent
usage of another process, another machine, or requests made before a process
restart. Report fields `local_rpm_limit` and `local_tpm_limit` are application
caps, not provider quota claims. `account_quota_status`,
`account_rpm_verified`, and `account_tpm_verified` reflect only the operator's
explicitly supplied verification state. This pilot runner adds a per-user
cross-process lock and a conservative
360-second launch cooldown in the stable application data directory. The
cooldown exceeds the 300-second maximum request timeout by one minute, so a
restart after a client timeout cannot immediately submit another pilot request.
It prevents overlapping live runs of this runner and avoids a rapid restart of
the runner; it cannot coordinate unrelated clients using the same credential.

Before a live run, stop other jobs using that credential, confirm the provider's
current limits and prices, and ensure no active request is outstanding. Run one
offline preflight, review its page, request size, token reservation, and maximum
cost, then request explicit authorization. Start exactly one live command and
wait for its final report. If it times out, returns 5xx, or reports
`unknown_outcome`, treat the request as potentially billed and do not run it
again without a separate operator decision.

## Reports and local retention

`report.json` contains safe configuration checks, selected page, source digest,
request state, available HTTP status and finish reason, request latency and page
elapsed time, token usage, estimated cost from usage and the operator tariff,
reserved possible cost, structural counts, and the number of candidate rows
that still require human review. Unknown token and cost values stay `null`. If
the API does not report cached input tokens, the estimate uses the uncached
input rate; billing remains subject to the provider's invoice.

The original structured transcription is stored separately as
`transcription.json`. Both it and the checkpoint are inside the private per-run
directory; neither is written to logs. Retain that directory for no longer
than seven calendar days after the run. The report records `delete_by_utc` and
the policy. Deletion is manual: after review, delete the complete run directory
(including `transcription.json` and `checkpoint`) from the pilot output folder.
For example, in PowerShell, after checking the exact path:

```powershell
Remove-Item -LiteralPath 'C:\path\to\vision-pilot\run-YYYYMMDDTHHMMSSZ-id' -Recurse -Force
```

The runner does not automatically delete results. The operator is responsible
for removing the run directory by its recorded deadline.

On Windows, the run directory is created with a protected DACL and its final
permissions are read back through the Windows security APIs. The check accepts
only the current user, SYSTEM, and built-in Administrators with Full Control;
unknown or unverifiable ACEs fail closed. Checkpoint directories and files are
checked before use, and each protected file is verified around its write. The
runner does not change the output root's parent ACL. Safe reports include only
the verification status, not SIDs or account names.
