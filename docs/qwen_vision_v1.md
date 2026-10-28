# Qwen Vision V1: isolated experimental core

This provider is deliberately absent from the provider registry, processing
coordinator, API settings and frontend. Import it directly for offline tests.
Existing OCR, Human Review, Excel and sourcing code is unchanged.

## Boundaries and configuration

* `qwen_vision_contract.py`: strict Pydantic/JSON Schema extraction and exact-header
  projection. It accepts arbitrary column headers, sparse/spanning cells, multiple
  tables, separate continuation candidates, unreadable text and correction flags.
* `qwen_vision_client.py`: configuration, injected HTTP transport and validated
  per-call response. The default transport makes one HTTPS request with bounded
  response reads; it has no redirects, retries, auth fallback or payload logging.
* `qwen_vision.py`: existing `OcrProvider` signature, per-page PDF rendering via
  `PdfService`, optional normalized crop, request/job budgets and DTO mapping.

Construct `QwenVisionConfig` explicitly, or call `from_app_settings` with the
existing `YandexCloudSettings`, `SecretStore` and `VisionLimits`. The factory uses
`llm_model`, `llm_base_url`, `folder_id`, the existing `AVERON_YANDEX_AI_*` overrides
and **only** `YANDEX_AI_API_KEY` (`yandex.ai_api_key`). It never falls back to the
Vision OCR key. No settings/global store is instantiated on import. The default
application limits are local conservative caps, not documented remote limits;
all can be replaced in `VisionLimits`. `dpi` supplied to `recognize` cannot exceed
the configured rendering DPI cap.

## Extraction and output

The response is a `VisionPage`: requested page number, ordered tables, original
headers, ordered candidate rows and cells, plus unreadable fragments. Cells retain
`raw_text`, machine-reading alternatives, spans, correction flags and optional
candidate boxes. Handwriting replacements are separate typed records with text
and an optional box candidate. They can be attached to a cell, to a row when the
row is known but the cell is not, or to the page when no row can be established.
A replacement at row/page scope conservatively suppresses canonical projection in
that scope. A localized cell correction suppresses only that cell; other printed
cells remain available for review. No handwritten text is applied automatically.
`logical_row_index`, `continuation_of` and cross-page continuation flags are
hypotheses only. No automatic row merge, strike-through deletion, correction
replacement, inferred value, number/unit conversion or fuzzy header mapping occurs.
`raw_text` is the unchanged model transcription; it is not independently proven
document text. V1 cannot establish transcription correctness or completeness.

The provider returns the existing `OcrResult` / `PageOcrResult` / `OcrRow` DTOs.
The first row's `metadata.vision_raw_page` retains the complete intermediate page;
each row also retains its own raw reading and original headers. A header-only or
unreadable page uses a blank `vision_evidence_only` record to retain page evidence
because `PageOcrResult` has no metadata slot. The existing assembler classifies
that record as `skip`, and the normal exportable-position filter omits it; the page
remains `NO_SPEC_OUTPUT` with `empty_extraction`. Invalid responses are discarded
without storing their body.

Only uniquely mapped, single-cell, unchanged readings enter `BASE_COLUMNS`.
Unknown/duplicate headers, spans, alternatives, unreadable values and handwritten
or crossed-out readings remain in raw metadata; ambiguous canonical fields stay
empty. Unknown columns are never dropped from raw extraction. Cell addresses are
validated across the whole table, including cells occupied by earlier `row_span`
and `column_span` values. One overlap rejects the whole page response, so no
partial projection can escape.

`source_row` is a local ordinal in candidate reading order, not a physical ID.
The server sets `provider_has_explicit_rows` to stop legacy text-fragment repair
from joining these candidate records. This means only that the provider returned
separate candidates; it does not establish physical rows. The companion server
marker `vision_rows_are_candidate_observations` makes that limit explicit.
`physical_row_refs` and trusted row/page geometry remain empty. Candidate boxes
remain in metadata in normalized **submitted-image** coordinates; crop origin is
retained separately. No model confidence or verified status is accepted. Extra
model fields, including safety flags, fail schema validation.

Every valid nonempty page is `REVIEW_REQUIRED`; failure or no valid output rows is
`NO_SPEC_OUTPUT`. Every page has explicit experimental/layout blockers. **USABLE
is impossible in V1.** Existing export and Review are exercised only in tests to
prove fail-closed behavior. This is not a production or sourcing integration.

`pages_dir` is accepted for protocol compatibility but not used: rendered images
stay in memory and cannot accidentally reuse another document's cached page.
Pages are processed in numeric order, with duplicates removed. Cancellation raises
`QwenVisionError('cancelled')`; a cancelled call does not return partial results.
Completed errors are isolated per page, allowing later pages within budget.

## Requests, privacy and accounting

Requests use `image_url` with a PNG Base64 data URL and
`response_format={type: json_schema, json_schema: {strict: true, ...}}`.
The client checks `finish_reason == stop`, refusal/tool calls, strict JSON decoding
(including duplicate-key/non-finite rejection), application schema, addresses and
source page. There is no fallback to a weaker response format.

Each request sends `store: false` and `x-data-logging-enabled: false`. Raw PDF,
image/Base64, credentials, model replies and remote error text are never logged or
copied to diagnostic fields. Raw transcription is stored solely in result metadata
for review; consumers must not log/serialize whole results as diagnostic events.
Exception messages contain owned codes only. Auth/config repr hides credentials.

Size caps cover image bytes/pixels, serialized request, HTTP response and extracted
table/column/row/cell counts before schema/projection work. PDF page
dimensions are checked before rendering. Job/page/request counts, output tokens,
render DPI, request timeout and total elapsed budget are explicit. No request is
retried, including after timeout, 429 or 5xx: an uncertain submission may already
have been billed. Cancellation is checked before/after rendering and network calls
and between response chunks. An in-flight socket/DNS/PDF operation cannot be
interrupted immediately; late responses are discarded and no further page request
is sent after the elapsed budget. There is no hard process-level rendering deadline.

Per-call diagnostics retain owned client UUID, UUID-shaped response request ID,
HTTP status, finish reason, image/request sizes, HTTP latency, extraction elapsed
time and whitelisted nonnegative usage
counts. Missing counts stay absent; `reported_usage` is only a sum of reported
counts and `usage_complete_pages` exposes incompleteness. No image-token counts or
monetary cost are invented. Actual invoice reconciliation and tariff snapshots are
V2 work; timeouts without a usage response have unknown consumption.

## Validation and V2 gates

`tests/test_qwen_vision.py` uses synthetic PDFs/images and injected transports. Its
socket fixture rejects network connections. Run with the normal project test
environment: `python -m pytest -q tests/test_qwen_vision.py`.

No paid API request was performed. The documented Qwen multimodal Base64 request
and generic Chat Completions JSON Schema contract have been implemented; the
specific model's combined image + strict-schema support, effective image/request
limits, billing/image-token behavior and real latency remain unverified.

Official references:

* [Multimodal Qwen request](https://aistudio.yandex.ru/ru/docs/ai-studio/operations/generation/multimodels-request)
* [Chat Completions](https://aistudio.yandex.ru/ru/docs/ai-studio/api/Chat-Completions/createChatCompletion)
* [Disable logging](https://aistudio.yandex.ru/ru/docs/ai-studio/operations/disable-logging)

V2 requires independently verified local geometry and stable physical row identity,
all-table/all-row coverage, safe schema mapping and critical-value evidence. It
also needs an explicit policy for unruled tables/cross-page relations, representative
gold data and a separately authorized real-API compatibility/benchmark run before
any UI/coordinator registration or production activation.

### Execution record: 2026-10-21

* V1.1 `test_qwen_vision.py`: **100 passed**. The suite includes cross-row span
  collisions, separate handwriting replacement roles, legacy assembly and
  evidence-only export checks.
* Existing regression selection: **150 passed, 3 failed**. Selection:
  `test_ocr_provider`, `test_ocr_dict_output`, `test_processing_coordinator`,
  `test_yandex_vision_provider`, `test_export`, `test_human_field_confirmations`,
  `test_qwen_structured`, `test_stage71_semantic_separation`.
* The same three coordinator tests fail against the original baseline source:
  `test_local_profile_resolves_tesseract_adapter`,
  `test_settings_processing_mode_influences_resolver`,
  `test_local_mode_rejects_cloud_llm_combination`. They require an available local
  Tesseract executable; none was found. No legacy tests or OCR health behavior
  were modified to hide these failures. A fully green native regression gate
  remains pending a Tesseract-equipped environment.

Tests ran with Python 3.12.14 from the bundled runtime and existing dependencies
from the original `.venv/Lib/site-packages` (the venv launcher references a missing
Python executable). `-B` disabled bytecode writes, pytest cache was disabled for
the final runs, temporary data was outside both checkouts, and a Python audit hook
blocked all socket connections. The regression runner blanked credential override
names without reading their values and used `AVERON_SECRET_STORE=memory`.

Equivalent pytest invocations after preparing a working project environment:

```text
python -m pytest -q tests/test_qwen_vision.py
python -m pytest -q tests/test_ocr_provider.py tests/test_ocr_dict_output.py tests/test_processing_coordinator.py tests/test_yandex_vision_provider.py tests/test_export.py tests/test_human_field_confirmations.py tests/test_qwen_structured.py tests/test_stage71_semantic_separation.py
```
