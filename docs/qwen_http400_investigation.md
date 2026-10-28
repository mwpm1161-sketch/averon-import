# AI-first Qwen HTTP 400 investigation — 2026-10-28

> Follow-up: the original production serialization/evidence below is retained
> as the investigation record. The initial inline-checkpoint diagnostic patch
> and four-case experiment proposal are superseded by
> [the minimal A/B preparation](qwen_context_ab_plan.md). The owner subsequently
> confirmed production output cap 32768. Zero subject positions before human
> role assignment is expected UI behavior, not an OCR classification defect.

## Finding and evidence boundary

The exact provider rejection reason is **not established**. The same PS1 page
succeeding after removing context strongly implicates the context request path.
That path changes both message segmentation and user content; it is not a
controlled test of either factor. A fake transport cannot establish Yandex's
actual behavior. The initial investigation used only code, synthetic fixtures,
and operator-supplied observations. Subsequent offline A/B preparation may read
operator-authorized local PDF/H/configuration into a new private bundle, without
publishing their contents. No cloud request or production mutation is performed.

Baseline: production commit `9d1a46f2c9a06f7ed3fb1187ac0dad30e5f38af0`, tree
`b20e006da46e2a7e7a976775b99195c66881ecec`. The initial local baseline `7f89365`
had that same tree; the publication branch now starts at exact `9d1a46f`.
User-supplied observations
are evidence, not newly independently measured production requests.

| Hypothesis | Assessment | Evidence / missing proof |
| --- | --- | --- |
| Context changes one system message into two | Confirmed in code | `QwenVisionClient._request_body`; offline serialized payload comparison |
| Hosted Qwen rejects the second system message | Leading hypothesis, unproved | Contextual failure / non-contextual success; no model-specific prohibition found in Yandex documentation; structured error reason was discarded |
| A universal 1,000,000-byte body cap explains PS1 | Contradicted if the reported successful no-context body remains 1,045,051 bytes | That body already exceeds 1,000,000; no such documented Chat Completions cap found |
| A 1,048,576-byte body cap explains PS1 | Unproved; ordinary Russian context fits below it | Exact rejected context is unavailable; JSON escaping and UTF-8 determine the byte delta |
| Pixel count alone explains PS1 | Strongly contradicted for PS1 | The same page/image geometry succeeds without context; compressed-byte differences cannot be inferred from pixel counts |
| Large AR full-page image violates hosted visual preprocessing limits | Plausible, unproved | Crop changes pixels, dimensions, encoded size, visual tokens and content simultaneously; it is not a size-only comparison |
| Schema / reasoning / output-cap are globally unsupported | Not supported as a blanket explanation | They are common request features, and successful calls exist; per-attempt parameter equality still needs the saved adapter parameters |
| HTTP 400 is output truncation / row classification | Excluded | No completion or finish reason; classification of 18 rows into zero subject positions is a separate downstream question |

## Exact request construction

The live runtime allowlists `https://ai.api.cloud.yandex.net/v1`; the client adds
`/chat/completions`. It sends a single POST, without redirects, automatic retry,
parameter fallback, multipart encoding or HTTP compression.

Application headers: `Content-Type: application/json`,
`Authorization: Api-Key <secret>`, `OpenAI-Project: <folder_id>`,
`x-data-logging-enabled: false`, `x-client-request-id: <fresh UUID>`.
Python's HTTP connection also supplies Host, Content-Length and
`Accept-Encoding: identity`. These values are not a payload logging recipe.

The JSON uses `json.dumps(..., ensure_ascii=False, allow_nan=False)` with the
default separators (spaces retained), then UTF-8 encoding. Image bytes use
ordinary Base64 (no line breaks) in `data:image/png;base64,...`.

```text
model = gpt://<folder_id>/qwen3.6-35b-a3b
stream = false; store = false; temperature = 0
max_completion_tokens = configured output cap (locally <= 32768)
reasoning_effort = "none" in build_live_qwen_runtime
response_format.type = "json_schema"
response_format.json_schema.name = "averon_vision_page_v1"
response_format.json_schema.strict = true
response_format.json_schema.schema = VisionPage.model_json_schema()

Without context:
  system: exact _PROMPT
  user: [text(source page), image_url(data URI)]
With nonempty context:
  system: exact _PROMPT
  system: exact _USER_CONTEXT_POLICY
  user: [text(source page), text(label + exact untrusted context), image_url(data URI)]
```

Empty string is treated as absent; nonempty whitespace is preserved. Context is
limited to 1000 Unicode code points with no silent truncation. The submitted
output cap and environment values for the three historical requests were not
available to this audit; 32768 is a local allowed maximum, not proof that every
request used it. Compare saved `report.adapter.parameters` before claiming that
all parameters were identical.

The current prompt hash covers `_PROMPT`; contextual checkpoint scope binds
the exact hint with an HMAC and the policy version string. It does not hash
the policy text/message segmentation themselves. A future contextual layout
fix must bind those exact inputs as well, rather than relying only on someone
remembering to update a version label.

## Byte evidence

For the current serializer, exactly:

`contextual_body_bytes = baseline_body_bytes + 456 + J(context)`

where `J(context) = len(json.dumps(context, ensure_ascii=False).encode('utf-8')) - 2`.
The fixed delta includes the policy, second system wrapper, user part and label.
It is independent of the image, model URI length and page number when the same
configuration is compared. The policy itself is 344 bytes; the label is 46 bytes.
The current serialized schema is 4216 bytes (default serializer).

| Synthetic context | Added bytes | Applied to reported PS1 baseline 1,045,051 |
| --- | ---: | ---: |
| One ASCII character | 457 | 1,045,508 |
| Synthetic Russian sentence (87 UTF-8 bytes) | 543 | 1,045,594 |
| 1000 Cyrillic characters | 2456 | 1,047,507 |
| 1000 four-byte emoji | 4456 | 1,049,507 |
| 1000 NUL characters, escaped by JSON | 6456 | 1,051,507 |

PS1 baseline is 3525 bytes below 1 MiB. It crosses that boundary only if
`J(context) > 3069`. Ordinary Cyrillic/ASCII text of up to 1000 characters
(including ordinary quotes/newlines) cannot cross it. The exact historical
context was not supplied and was not read from customer storage, so its exact
contextual body size is unknown. PNG sizes described as “about KiB” also cannot
reconstruct exact image bytes. Base64 length is `4 * ceil(png_bytes / 3)`.

## Official Yandex documentation checked

These are Yandex sources, not Alibaba/model-upstream limits. Checked 2026-10-28.

* [Model catalog](https://aistudio.yandex.ru/ru/docs/ai-studio/concepts/generation/models):
  Qwen URI has no `/latest`; context is 262144 tokens. Exact excerpt:
  “Модель Qwen3.6 35B работает с изображениями в кодировке Base64.”
* [Chat Completions reference](https://aistudio.yandex.ru/ru/docs/ai-studio/api/Chat-Completions/createChatCompletion):
  describes image_url as URL or Base64, the system/user message types,
  `json_schema`/`strict`, and `max_completion_tokens` including reasoning.
  Exact excerpts: “Parameter support can differ depending on the model”;
  “Enum: none, minimal, low, medium, high, xhigh”;
  “Only a subset of JSON Schema is supported”. No per-Qwen guarantee for
  two system messages or output cap 32768 was found. Its 8 MB note concerns
  image storage under `store`, not a documented overall request-byte cap.
* [Reasoning](https://aistudio.yandex.ru/ru/docs/ai-studio/concepts/generation/chain-of-thought):
  exact excerpt: “модели семейства Qwen позволяют выключить рассуждения, но не позволяют выбрать глубину рассуждений”.
  Together with the Chat Completions enum, this supports the existing `none`
  setting; it does not prove the actual reasoning usage of an individual call.
* [Quotas and limits](https://aistudio.yandex.ru/ru/docs/ai-studio/concepts/limits):
  no 1,000,000/1,048,576-byte Chat Completions body limit or per-Qwen visual-pixel
  cap was found. Its 10 MB / 20 megapixel restrictions explicitly concern OCR
  API; they must not be used as Qwen acceptance limits.
* [Error codes](https://aistudio.yandex.ru/ru/docs/ai-studio/troubleshooting/error-codes):
  exact table excerpt: “3 | 400 | INVALID_ARGUMENT | Некорректный запрос”.
  The accompanying causes include invalid URI, prompt length and invalid
  parameter values. A 100–150 ms response suggests early validation but does
  not identify which validator rejected the request.
* [Support headers](https://aistudio.yandex.ru/ru/docs/ai-studio/api-ref/support-headers):
  exact excerpt: “x-client-request-id — уникальный идентификатор запроса”.
  It also documents response request/trace IDs and disabling data logging.

## Diagnostic-only patch

The request, system prompt, schema, caps, context policy version and descriptor
fingerprints are unchanged. The existing two-system path remains reproducible.

The client records exact request/image sizes and dimensions, system count,
context presence/byte delta, a generated client UUID and UUID-only response IDs.
For errors, at most 64 KiB is parsed in memory using duplicate-key/nonfinite
rejection. Only exact allowlisted structured `code`, `type` and `param` values
survive; known numeric gRPC codes are mapped to their fixed names. Body messages,
details, arbitrary strings, URLs, headers, prompts and image data never survive
this boundary. Invalid/oversized bodies retain HTTP status and a fixed parse
state. Unknown fields/codes remain null. No error message substring matching.

The adapter carries the typed evidence into optional `PageAttempt.request_diagnostics`
in the private report/checkpoint, including cancellation/error paths. Previously
the adapter discarded even the client's safe request IDs. Old checkpoints with
no field still load; extraction/manifest identity and retry rules do not change.
Missing usage/cost remain unknown and the possible-cost reservation remains.

Compatibility is backward reading by the new version. A pre-patch reader using
`extra="forbid"` cannot load a new page record containing diagnostic fields.
Any controlled diagnostic trial must therefore use a separate new workspace/run
and retain the original checkpoints for rollback. No existing customer
checkpoint was rewritten here. This patch requires review before deployment.

This cannot recover already discarded error responses. If a future response
has only free-text detail, the allowlisted fields may still be null: use the
client/request UUID with Yandex support to request the validator's reason.
Do not enable data logging or copy a customer payload into a support ticket.

## Proposed controlled experiment (requires separate paid authorization)

First ask support about existing failed requests if their safe IDs are
available; that can avoid paid calls. Otherwise use the same authorized page,
identical PNG bytes, model/endpoint/schema/caps and one-at-a-time requests.

Minimum complete factorial matrix: four separately authorized requests:

| Case | System arrangement | User hint |
| --- | --- | --- |
| A | One system: `_PROMPT` + policy | Absent |
| B | Two systems: `_PROMPT`, policy | Absent |
| C | One system: `_PROMPT` + policy | Exact agreed synthetic neutral hint |
| D | Two systems: `_PROMPT`, policy | Same hint |

All four bodies are padded **only with legal trailing JSON whitespace** to the
same measured byte length; their image data, schema, reasoning and output cap
are identical. Safety policy text is present in all four. A/B and C/D isolate
segmentation; A/C and B/D isolate hint presence. These variants exist only in
offline tests, not as a production switch. The test harness distinguishes a
fake “at most one system” server from a fake byte-limit server; this is not
remote-provider evidence. Results inconsistent with the matrix require support
investigation rather than automatic retry.

Only if still necessary and separately authorized, add a size-only pair using
one already successful arrangement: exact body lengths 1,048,575 and 1,048,577,
using trailing JSON whitespace. This leaves model content unchanged and probes
the suspected 1 MiB boundary. No arbitrary new API fields or client text padding.
Check reservation/TPM/budget and fixed per-case request IDs before each case;
no automatic repeat/fallback, even on timeout or 5xx. Success in one trial is
evidence about that deployment, not a universal API guarantee.

If the system-count cause is demonstrated, the minimal proposed functional fix
is to append the existing policy to the first system message for nonempty
context and keep the hint in user content. Empty-context bytes and prompt hash
stay identical. Bump/bind the contextual policy/serialization identity so an old
contextual checkpoint is never silently resumed with different messages; retain
old checkpoint data and require an explicit new run. Do not remove the policy,
schema, image detail or output budget to force acceptance.

## Offline validation

The Qwen client, adapter, orchestration, two pilot CLIs, diagnostics, Human Review
workflow and reliability profile passed: **312 tests**, no failures, errors or
skips. The included audit-only suite passed **23 tests**, including a wire-header
capture test that uses an in-memory socket substitute.
The new adapter regressions also exercise checkpoint persistence and legacy
checkpoint loading. Real socket/HTTPS calls are blocked by the test fixtures.
Five serialized request comparisons against the unmodified baseline were
byte-for-byte identical (absent, empty, ASCII hint, Cyrillic and escaped control
text). Prompt/schema/descriptor/private contextual identity were unchanged.
Offline success is evidence for this patch and experiment harness, not proof
that the hosted model accepts any particular variant.
