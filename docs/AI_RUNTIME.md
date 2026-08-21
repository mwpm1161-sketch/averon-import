# Averon AI Runtime

## Flow

Specification row -> AIReviewService -> AIRouter -> AIProvider -> AIReviewResult

## Rules

- AI does not modify specification data directly.
- Human approval is required.
- Cloud providers remain disabled by default.
