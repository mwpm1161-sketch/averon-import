# AI-first local review MVP (V2D)

V2D adds an experimental AI-first workflow inside the existing Averon Import interface. It uses the existing document workspace and PDF renderer, and stores `transcription.json`, a content-free technical `report.json`, page progress, review decisions, and the orchestration checkpoint in a separate `ai_first` directory for that document. It does not write the legacy `result.json` or review ledger and does not enable AI-first Excel export or sourcing.

## Offline manual test on Windows

Start the existing Averon app from the isolated feature worktree with the synthetic adapter enabled:

```powershell
$env:AVERON_AI_FIRST_UI_ENABLED = '1'
$env:AVERON_AI_FIRST_FAKE_OCR = '1'
$env:AVERON_AI_FIRST_LIVE_ENABLED = '0'
python -m averon_import.main --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765`, sign in through the configured local test authentication, upload a PDF, and choose **AI-first · синтетический тест** in the page panel. The AI-first mode selects every PDF page by default; the existing range field remains available to narrow the selection. The fake adapter returns clearly labelled synthetic rows and never reads the rendered page or makes HTTP requests. The PDF is rendered locally so the side-by-side review and navigation can be exercised.

The review screen supports editing cell values, adding a manual row, excluding a row through an explicit review decision, resolving remarks with a note, confirming selected rows, and confirming page completeness separately. Every change is saved to the private local review ledger with its reviewer and audit event. The original extracted cells remain in the draft. Rows with linked remarks or uncertain cells are highlighted. A successful row confirmation does not resolve page remarks or confirm page completeness.

To return to a saved AI-first draft, reopen that document and use **Открыть AI-first проверку**. The app stores the source transcription and review ledger in the document's private `ai_first` subdirectory. The checkpoint uses the existing fail-closed ACL verification. On Windows, each created directory and protected file is checked against its final DACL before use; parent workspace ACLs are not modified. Use Averon's existing document deletion workflow to remove the complete workspace, including the AI-first checkpoint. Do not delete a checkpoint while a job is active.

## Qwen mode boundary

The Qwen option is hidden unless the local server is explicitly restarted with `AVERON_AI_FIRST_LIVE_ENABLED=1` as well as `AVERON_AI_FIRST_UI_ENABLED=1`. The operator must also supply local request, token, input-reservation, price, and budget caps through `AVERON_AI_FIRST_MAX_REQUESTS`, `AVERON_AI_FIRST_RPM_LIMIT`, `AVERON_AI_FIRST_TPM_LIMIT`, `AVERON_AI_FIRST_INPUT_TOKEN_UPPER_BOUND`, `AVERON_AI_FIRST_MAX_BUDGET_RUB`, `AVERON_AI_FIRST_INPUT_RUB_PER_1K`, `AVERON_AI_FIRST_OUTPUT_RUB_PER_1K`, and optionally `AVERON_AI_FIRST_CACHED_INPUT_RUB_PER_1K`. The output cap, DPI, and request timeout use `AVERON_AI_FIRST_MAX_OUTPUT_TOKENS` (maximum 32768), `AVERON_AI_FIRST_DPI`, and `AVERON_AI_FIRST_REQUEST_TIMEOUT_S` (maximum 300 seconds). The existing Yandex AI Studio model settings and secret store supply the model URI, endpoint, and credential; the local workflow accepts only the documented Qwen3.6 35B model URI shape and Yandex AI Studio endpoint.

These values are local application limits. They do not verify account RPM/TPM. The live UI requires a separate per-run confirmation before submitting the selected pages; that control is not permission for a future paid experiment. No live Qwen request was made while preparing V2D. The Qwen context fingerprint binds the exact user hint without placing its text in public reports. After reopening the app, re-enter the same hint to resume the same checkpoint; a changed hint safely creates an identity mismatch and cannot reuse the old extraction.

The local Qwen runtime uses concurrency 1, no automatic retry, and the existing orchestration budget and checkpoint rules. Failed, incomplete, and unknown-outcome pages appear as retry candidates only; the operator must select pages for a retry explicitly. A retry of an unknown outcome can incur a second charge. Cancellation waits for an in-flight client call to return and preserves any completed page records and usage in the checkpoint.

## Review and export boundary

AI-first review decisions do not create a legacy Yandex OCR result, do not change M6 or sourcing inputs, and do not call the Excel exporter. The AI-first export guard is displayed as a readiness diagnostic, but this MVP has no AI-first export action. Any future export integration must consume the established review guard without relaxing it.
