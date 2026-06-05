from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean
from typing import Any


@dataclass(frozen=True)
class LearningReport:
    sample_size: int
    approved_count: int
    executed_count: int
    rejected_count: int
    closed_position_count: int
    average_r_multiple: float
    average_confidence: float
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    candidate_constraints: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class ExperienceLearner:
    """Turns experience memory into candidate config changes.

    This module does not mutate live behavior. It proposes candidate constraints
    that should be tested in paper/demo before promotion.
    """

    def analyze(self, rows: list[dict[str, Any]]) -> LearningReport:
        if not rows:
            return LearningReport(
                sample_size=0,
                approved_count=0,
                executed_count=0,
                rejected_count=0,
                closed_position_count=0,
                average_r_multiple=0.0,
                average_confidence=0.0,
                notes=["no_experience_records"],
            )

        confidences = [float(row["decision"]["confidence"]) for row in rows if "decision" in row]
        approved = [row for row in rows if row.get("risk", {}).get("status") == "approved"]
        executed = [row for row in rows if (row.get("execution") or {}).get("accepted") is True]
        rejected = [row for row in rows if row.get("risk", {}).get("status") == "rejected"]
        closed_positions = [
            position
            for row in rows
            for position in (row.get("outcome") or {}).get("closed_positions", [])
        ]
        r_multiples = [float(position["r_multiple"]) for position in closed_positions if "r_multiple" in position]

        rejection_reasons: dict[str, int] = {}
        for row in rejected:
            for reason in row.get("risk", {}).get("reasons", []):
                rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

        avg_confidence = round(mean(confidences), 4) if confidences else 0.0
        candidate_constraints: dict[str, float] = {}
        notes: list[str] = []

        rejection_rate = len(rejected) / len(rows)
        if rejection_rate > 0.7 and rejection_reasons.get("confidence_below_minimum", 0) > 0:
            candidate_constraints["min_confidence"] = max(0.55, round(avg_confidence - 0.03, 2))
            notes.append("candidate_relaxes_confidence_for_paper_testing")

        spread_rejections = rejection_reasons.get("spread_above_limit", 0)
        if spread_rejections / len(rows) > 0.35:
            notes.append("spread_filter_blocks_many_decisions_review_symbol_session")

        if len(executed) == len(rows):
            notes.append("no_rejections_seen_keep_collecting_before_tuning")
        if r_multiples:
            average_r = mean(r_multiples)
            if average_r < 0:
                notes.append("negative_average_r_review_planner_filters")
            elif average_r > 0.3:
                notes.append("positive_average_r_keep_collecting_outcomes")
        else:
            average_r = 0.0

        return LearningReport(
            sample_size=len(rows),
            approved_count=len(approved),
            executed_count=len(executed),
            rejected_count=len(rejected),
            closed_position_count=len(closed_positions),
            average_r_multiple=round(average_r, 4),
            average_confidence=avg_confidence,
            rejection_reasons=rejection_reasons,
            candidate_constraints=candidate_constraints,
            notes=notes,
        )
