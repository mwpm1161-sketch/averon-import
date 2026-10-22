# AI-first extraction foundation (V2A)

## Decision and boundary

The experimental AI-first path treats a multimodal extraction as an untrusted, loss-preserving document draft. Qwen may interpret tables, rows, columns, page continuations and handwriting. Averon stores the observations, raises review issues, coordinates explicit human decisions, and can later produce deterministic exports from approved data.

Local geometric reconstruction and `physical_row_refs` are not prerequisites in this path. Model candidate boxes may be retained as unverified hints, but the draft does not need them. Existing OCR, Human Review, Excel, sourcing, M6, API routes and provider registration remain unchanged. The package under `services/ai_first/` is not called by production code.

An `extracted` page status means that a transcription payload was received and parsed. It does not mean the page or document is complete or correct. Neither a model role, an empty issue list, nor a set of candidate values can mark a row verified or authorize export.

## Data contract

```text
source document
└── AIDocumentDraft (source ID, revision ID, ordered pages, document issues)
    └── AIPageExtraction (source page, status, raw model payload, fingerprint)
        └── AITable (original headers/columns, raw table payload)
            └── AIRow (model_kind, raw cells in source order, raw row payload)
                ├── AICell (raw value, alternatives, handwriting/strike flags, optional box)
                └── AIValueProposal (optional suggested value + source cell IDs)
```

Every page, table, row, column and cell is linked to an opaque source-document ID and page. IDs include the page payload fingerprint, so they are stable for the same page revision and change when that page changes. The entire parsed page payload and nested table/row/cell payloads are retained; unknown keys, columns, notes, flags and values are not discarded. The original cell array order is retained.

Headers may have any names and tables may have any number of columns or multiple tables per page. Canonical column names are optional hints only. The raw header and all cells remain authoritative as source observations. The input transcription contract has no separate canonical-value proposals, so replay leaves that field empty instead of inventing normalized values.

A row keeps the model's `kind` as `model_kind`; `server_role_candidate` is empty until a future, separately tested rule supplies a hypothesis. `role_verified` is fixed to false. Strike-through, handwriting, alternatives and unreadable signals remain attached at their reported scope. No value is selected as the effective quantity or unit by the conversion layer.

## Document assembly

`assemble_document_draft` orders pages, keeps each table and row distinct, carries an explicit expected-page manifest supplied by the trusted caller, represents missing requested pages as failed page records, and records candidate issues such as duplicate/gapped position numbers, unresolved continuation markers, unreadable text, and unlocalized handwriting/strike-through signals. The caller must derive the manifest from the source PDF itself, never from the set of model responses. The replay CLI reads the PDF page count locally and uses that full range, so an omitted model page remains an explicit failed page. It does not merge cross-page rows, fill gaps, fix duplicates, or infer that a page is complete. Candidate row-number checks run only when unique, recognized position and name headers are available; they are review prompts, not row identity or verification, and gap checks compare adjacent sorted values without allocating a range from model-provided integers.

The draft revision is derived from the expected-page manifest and page fingerprints/statuses. Reprocessing the same page payload preserves that page's IDs. Changed page content gets new IDs; a review-state reconciliation deactivates decisions, page-completeness confirmations, and issue resolutions attached to changed pages while retaining the old decisions as inactive history. Unchanged pages may keep their decisions. If a server supplies a page number, a different or malformed page number in the original model payload is rejected.

## Human Review V2 service state

`AIReviewState` records explicit reviewer decisions, manual rows, completeness confirmations, issue resolutions and a timestamped event sequence that preserves reviewer-entered values. Service functions support confirming/correcting/excluding a row; adding a row; splitting or merging rows; undoing the latest non-dependent structural edit; confirming a handwritten reading; confirming page completeness; resolving review issues; and reconciling a reprocessed page. Structural events and manual rows are bound to the page extraction fingerprint. Reprocessing deactivates old manual rows and decisions; undo rejects events whose page fingerprint or status has changed. The export guard also rejects active manual rows with missing or stale page provenance. Structural edits deactivate consumed manual rows, keep source rows explicitly excluded, prevent superseded sources from being reactivated while descendants remain active, and invalidate page-completeness confirmation. Undo deactivates the generated rows and restores the source decisions, while leaving an audit event. There is no UI or persistent database adapter in V2A.

The guard `assert_review_ready_for_export` fails unless the draft has a nonempty expected-page manifest that exactly matches its pages, every page completed processing (no failed/incomplete status or error code), at least one included row, every page has a human completeness confirmation, every source or active manual row has an active human decision, active lineage has no included ancestor/descendant conflict, and all review/blocking issues are resolved. Resolving an issue or confirming a failed/incomplete page cannot override page-processing failure. The guard is only a boundary primitive: this package contains no Excel exporter, sourcing adapter, production route, or automatic admission path. A later exporter must consume reviewed decisions and write from those explicit values.

## Offline replay

`python -m averon_import.tools.ai_first_replay --input <saved-transcription.json> --source-pdf <original.pdf> --output <protected-draft.json>` converts a saved V1 transcription without making an HTTP request. It reads only the source PDF's page count to build the expected-page manifest. It prints only counts and status codes; it never prints cell values, model payloads, or the PDF path. Output creation is exclusive and the caller must choose a directory protected for the source document.

Offline replay confirmed that source rows and cells, including nonstandard fields and model misclassification, can be preserved without requiring geometric reconstruction. Such results do not receive automatic export authorization.

## V2A exclusions

No Qwen request, prompt change, production switch, user interface, database persistence, Human Review integration, Excel output, sourcing integration or M6 change is included. A later stage must decide retention/access controls for persisted drafts, reviewer identity/audit requirements, page completeness evidence, and how a reviewed draft is mapped into export-specific rows.

## V2B orchestration requirements

Before processing 30 or more PDF pages, orchestration must use a model-agnostic page adapter and bounded parallel requests (initial concurrency target 3–4, configurable) with API rate and token-budget controls. It must preserve deterministic page ordering and the full page manifest derived from the local PDF, enforce a total cost cap, support cancellation, and checkpoint/resume completed page results safely. An ambiguous timeout after submission must not trigger an automatic retry. Checkpoints, rendered images, transcripts, and logs must preserve customer-data isolation; logs must not contain credentials, image data, request bodies, or document text. Parallelism must be bounded by service limits and the caller's budget, not by page count.


## V2B.1 parallel orchestration core

The isolated services/ai_first/orchestration.py module defines a model-neutral PageExtractionAdapter protocol. An adapter exposes provider/model, prompt and schema versions, a conservative pre-send token reservation, and one asynchronous extraction call. It returns a typed AIPageExtraction plus optional per-request token usage. The scheduler does not import Qwen schemas or old OCR providers. The Qwen adapter and production wiring are out of scope for this stage.

inspect_pdf hashes the original PDF and reads its page count locally before any adapter call. The checkpoint identity binds that SHA-256, page count, run ID, adapter descriptor fingerprint, DPI, image limits and pricing fingerprint. PageOrchestrator.run rechecks the input identity before opening the checkpoint. A changed source, model, prompt, schema, DPI or pricing configuration rejects resume.

The scheduler uses a bounded queue and a fixed worker pool (default concurrency 3, configurable up to 8). Workers render one page only when they can process it; rendered images are released after the adapter call. RPM and TPM are enforced at the send boundary. Before dispatch, the scheduler reserves the adapter's conservative input/output token maxima at uncached input prices against a required RUB cap. Known usage settles the reservation at actual rates. Missing usage or an ambiguous outcome keeps the reservation as possible exposure; reported actual tokens and cost remain null when they cannot be computed. If actual cost exceeds the reservation, scheduling stops.

A private checkpoint stores a manifest and atomic per-page JSON records; it never stores the PDF or rendered image. On Windows it applies a restricted ACL to the checkpoint tree, and on POSIX it uses owner-only directory/file permissions. The checkpoint contains the raw model transcription in plaintext at rest, so callers must use a trusted local disk and manage retention. An OS file lock prevents two processes from writing the same run concurrently. A restarted running page becomes unknown_outcome; completed pages are skipped, while failed/unknown pages are retried only when the caller explicitly names them in retry_pages. There is no automatic API retry.

Progress events and scheduler logs carry page numbers, states and counts only. They omit document text, raw model payload, image bytes, request bodies, credentials and local source paths. Every source page appears in page order in the report and V2A AIDocumentDraft; pages without an extraction become explicit failed-page observations. The draft preserves unknown raw payload fields and always keeps extraction_is_verified=false and export_authorized=false. No Human Review, exporter, sourcing flow or production route is called. The run report includes the safe adapter descriptor, selected orchestration limits, per-attempt usage and aggregate usage/cost; unavailable measurements remain null.

Offline synthetic tests cover 30-page ordering and measured concurrency, 50-page in-flight image bounds, isolated page failure, resume identity and no-retry behavior, explicit retry, rate/budget gates, cancellation before and after dispatch with partial-result resume, running-to-unknown recovery, atomic writes, log redaction, unknown usage, and V2A draft assembly. The in-flight byte metric measures image bytes held by active workers; it is not process RSS.

## V2B.2 integration decisions

Before a real provider adapter is added, specify how it maps transport errors into definite versus ambiguous outcomes, which provider response usage fields are billable, and how it supplies conservative per-page token maxima without sending a request. Confirm provider-specific RPM/TPM limits and current pricing from authoritative configuration. Decide whether private checkpoint files need encryption at rest and define retention/deletion controls. The operator flow must preserve the run ID and require an explicit per-page decision before retrying any unknown_outcome record. A real adapter must remain offline-tested and separately approved before any paid call.
