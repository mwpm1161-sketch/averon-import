# AI-first quality foundation V2C.1

This offline evaluator compares an `AIDocumentDraft` with an explicit reference. The synthetic set covers two ordinary specification layouts: a multipage table with a section row and wrapped description, and a table whose article, position, quantity, unit, name, and mass columns use a different order. All values in the committed fixture are artificial.

The aggregate report counts expected and recognized positions, missing and duplicate positions, unexpected and extra rows, physical order, missing or incomplete pages, exact article/name/quantity/unit/mass/note errors, column assignment errors, and row classification errors. A position counts as covered only when an `item` row at the reference's physical page/table/row location contains the expected position; a number from a header, section, or extra row cannot fill a missing item. Page completeness uses the same location-bound match, while classification and order remain separate metrics. Comparisons are exact string comparisons; the evaluator does not normalize or correct candidate text. It returns counts only and does not alter a draft, mark a row reviewed, or change export authorization.

The gate for a separately adjudicated control set is 100% position coverage with zero critical article, quantity, unit, column-assignment, and row-classification errors. Passing a finite control set supports only that set; it is not a claim of universal 100% OCR accuracy.

The saved synthetic outputs are deterministic inputs for testing parsing and scoring. They do not exercise Qwen on images and do not establish model OCR accuracy. Any accuracy claim requires a separately adjudicated image reference and a reproducible evaluation run. The first pilot's client material must stay in a local protected reference directory; no source page, transcript, expected client values, or client-derived fixture belongs in GitHub.

## Optional document context boundary

The isolated Qwen client accepts a per-document context of at most 1,000 Unicode characters. The value is sent exactly as entered, without trimming or truncation, with a fixed untrusted-context policy. Empty or absent context follows the legacy request body byte-for-byte and leaves the standard prompt fingerprint unchanged. Context is not included in adapter descriptors, reports, logs, or checkpoint contents. A keyed context fingerprint is included in the private checkpoint scope, so resume requires the same context; changing it rejects the old checkpoint. The user must provide the context again when resuming because the raw hint is not stored.

Preflight includes the context in the exact serialized request-size check. The adapter reserves the full additional serialized body byte delta as extra input tokens on top of the configured base bound. This deliberately conservative reservation is an application guard, not a verified Yandex tokenizer result; if the TPM or budget reservation fails, the page is not sent.

## Future V2C.3 interface proposal

Place an optional multiline field labelled **«Опишите нюансы работы»** directly below the PDF upload and above the primary **«Начать распознавание»** button. Show **«Необязательно»**, use the placeholder **«Например: в документе есть зачёркнутые количества и рукописные исправления. Обратите на них особое внимание»**, state the 1,000-character limit, and display a visible count. An empty field must not change the standard path. A filled value applies only to that document and must not enter public logs.

In the later review screen, keep the full extracted table visible and highlight rows with unresolved issues. Add a selection control and a bulk **«Подтвердить выбранные строки»** action that records ordinary per-row review decisions and an audit event. Bulk confirmation must use the same eligibility checks as individual confirmation, preserve source values and edits separately, and leave page-completeness and export guards in force. It must not confirm excluded, stale-revision, or blocking-issue rows implicitly.
