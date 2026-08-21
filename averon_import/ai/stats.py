from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AIPipelineStats:
    total_rows: int = 0
    skipped_rows: int = 0
    rule_fixed_rows: int = 0
    ai_rows: int = 0
    accepted_rows: int = 0
    rejected_rows: int = 0
    ai_calls: int = 0

    def public(self) -> dict:
        return {
            "total_rows": self.total_rows,
            "skipped_rows": self.skipped_rows,
            "rule_fixed_rows": self.rule_fixed_rows,
            "ai_rows": self.ai_rows,
            "accepted_rows": self.accepted_rows,
            "rejected_rows": self.rejected_rows,
            "ai_calls": self.ai_calls,
        }
