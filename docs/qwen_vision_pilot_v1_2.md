# Qwen Vision isolated pilot CLI (V1.2 and V1.3 output-budget and timeout controls)

`python -m averon_import.tools.qwen_vision_pilot` runs an isolated diagnostic using the experimental `QwenVisionProvider` and `QwenVisionClient`. The command is not registered with the app runtime, API, coordinator, Human Review, export, or sourcing.

Dry-run is the default. It opens the local PDF, validates the selected pages and render limits, renders images in memory, applies an optional crop, and measures the exact local Chat Completions JSON body size. It does not resolve the API credential or send HTTP. The report marks token usage and price as not applicable instead of inventing zero usage.

Every invocation requires `--max-requests`; it must cover all selected pages and cannot exceed the provider's 20-request cap. Live calls additionally require `--allow-paid-api`. Before any live call, the same local preflight runs and the credential is resolved only after the explicit flag. V1.3 acceptance checks are offline only; no paid calls are part of them.

`--max-output-tokens` controls the Chat Completions `max_completion_tokens` request field. It defaults to 8192 and accepts positive integers up to the isolated pilot's local cap of 32768. The current API reference recommends `max_completion_tokens` over the deprecated `max_tokens` field and defines it as covering visible and reasoning output. A larger cap only permits more generated output; it does not guarantee that the model finishes the transcription. The CLI does not retry a response whose `finish_reason` is `length`. Reports include the selected cap and safe response-content character/byte counts when the API returns text, without storing that text in technical diagnostics.

`--disable-reasoning` is an optional explicit opt-in that sends `reasoning_effort: "none"`; the default omits this parameter. Yandex documents `none` for Chat Completions and says Qwen 3.6 reasoning can be disabled through the API ([Chat Completions API](https://aistudio.yandex.ru/ru/docs/ai-studio/api/Chat-Completions/createChatCompletion), [Qwen 3.6 release](https://yandex.cloud/en/blog/digest-april-2026)). This flag never triggers a fallback request.

The CLI creates a unique run folder under `%LOCALAPPDATA%/Averon Import/vision-pilot` on Windows or `$XDG_STATE_HOME/averon-import/vision-pilot` (falling back to `~/.local/state/averon-import/vision-pilot`) elsewhere. It disables inherited Windows ACL entries and grants access to the current user, SYSTEM, and local Administrators, or uses mode `0700` on POSIX. Failure to establish these permissions stops the run. `report.json` contains only technical diagnostics and whitelisted API usage. A live successful response is stored separately in `transcription.json`; optional reference comparisons go in `reference_comparison.json`. The CLI never writes images or Base64, and does not log request/response bodies, keys, PDF metadata, or extracted document text.

## Invocation

```powershell
python -m averon_import.tools.qwen_vision_pilot `
  --pdf "C:\path\to\specification.pdf" `
  --pages 1,3-4 `
  --dpi 150 `
  --crop 0.02,0.02,0.96,0.96 `
  --max-requests 3 `
  --max-output-tokens 32768 `
  --disable-reasoning `
  --request-timeout-s 300
```

`--crop` is optional and uses normalized `x,y,width,height` coordinates. `--request-timeout-s` defaults to 120 seconds and accepts finite positive values up to 300 seconds; the common total run timeout remains 600 seconds. The chosen request and total timeouts appear in the safe report. Reasoning can optionally be disabled with `--disable-reasoning`; omitting it keeps the API default. The configured model and HTTPS base URL come from the existing non-secret Yandex settings and model/base URL environment overrides. The configured Qwen Vision model should be `gpt://<folder_id>/qwen3.6-35b-a3b`.

For an explicitly authorized future live pilot, add `--allow-paid-api`, and provide real tariff values from the applicable Yandex AI Studio price schedule:

```powershell
python -m averon_import.tools.qwen_vision_pilot `
  --pdf "C:\path\to\specification.pdf" `
  --pages 1,3-4 `
  --dpi 150 `
  --max-requests 3 `
  --max-output-tokens 32768 `
  --disable-reasoning `
  --request-timeout-s 300 `
  --input-rub-per-1k <input-rate> `
  --output-rub-per-1k <output-rate> `
  --allow-paid-api
```

The optional `--cached-input-rub-per-1k` applies only when the API reports cached input tokens. A cost estimate is withheld if required usage or tariff data is missing. Failed API requests with no usage remain unknown because a request may have reached the service. A request may fail even after every local preflight check passes. A timeout after sending does not trigger an automatic retry because the service may already have processed and billed the request.

Returned `completion_tokens_details.reasoning_tokens` is recorded when present and remains unknown when absent. Reasoning-token counts are part of completion usage and are not added to the cost a second time.

## Exit codes

The CLI returns `0` for `dry_run_ready` and a fully successful `completed` run. It returns a nonzero code for `completed_with_errors`, `preflight_failed`, and internal run failures. A page failure preserves successful page transcriptions and reported usage. The report keeps known per-page values and partial subtotals; incomplete aggregate totals and cost estimates remain unknown.

## Structural row diagnostics

Live runs save `semantic_review.json` beside `transcription.json`, under the same private run-directory permissions. It contains the model transcription values and row/cell annotations for human review; it is separate from the technical `report.json`. The semantic summary in the report contains counts only.

The server preserves the raw `model_kind` and independently records `server_role_candidate`, `role_conflict`, and `role_verified: false`. The classifier distinguishes table headings, column-numbering rows, section titles, numbered position candidates, continuations, ambiguous rows, and other content using headers, cell content, row position, and adjacent rows. A numbered candidate requires a positive integer in the position column and nonnumeric text in the name column. Position gaps and duplicates are diagnostic signals only.

`candidate_row_count` now counts potential numbered positions and continuation candidates, independent of the model's `kind`; `model_kind_candidate_row_count` preserves the former kind-based count. `all_structured_row_count` counts all source rows. The report separately exposes model-kind counts, server-role candidate counts, ambiguous rows, role conflicts, and zero confirmed positions. A candidate is never physically verified by this heuristic, and these fields do not create a `USABLE` state or authorize sourcing. The Qwen provider remains isolated from production.

Corrections and struck values retain their original cell readings and annotations. The diagnostic projection marks fields suppressed by the existing isolated projection rules and cells retained only in raw form. A handwritten number beside a struck quantity is never converted into an effective quantity by this classifier.

These prompt additions are unvalidated hypotheses, not evidence of improved OCR. Any future quality claim requires a controlled comparison against human-checked reference labels from the original page.

## Reference CSV

The optional `--reference-csv` expects `page,table_index,row_index` plus at least one canonical column from `position,name,type_mark,code,manufacturer,unit,quantity,mass,note`. Location indices are zero-based for table and row. Comparisons pair only equal location keys; there is no fuzzy alignment. Output records textual equality after whitespace/case normalization or differing fields, and unmatched locations. Equality is a transcription comparison only and is never treated as correctness or approval.

## Acceptance checks

```text
python -m pytest -q tests/test_qwen_vision_pilot.py tests/test_qwen_vision.py
```

The pilot tests use synthetic PDFs and deny socket access. They verify the dry-run path, safe output handling, strict page/crop parsing, reference comparison semantics, unknown usage handling, and tariff arithmetic. The API's actual image/schema compatibility, billing behavior, model response quality and latency remain unverified until a separately authorized live experiment.
