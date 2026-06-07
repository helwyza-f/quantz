from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from quantz.report import SessionReport


@dataclass(frozen=True)
class PromotionGate:
    stage: str
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PromotionGateEvaluator:
    def evaluate(self, report: SessionReport, stage: str) -> PromotionGate:
        if stage == "replay_to_paper":
            return self._replay_to_paper(report)
        if stage == "paper_to_demo":
            return self._paper_to_demo(report)
        if stage == "demo_to_tiny_live":
            return self._demo_to_tiny_live(report)
        raise ValueError(f"Unknown promotion stage: {stage}")

    def _replay_to_paper(self, report: SessionReport) -> PromotionGate:
        return self._gate(
            "replay_to_paper",
            report,
            min_decisions=200,
            min_closed=30,
            min_average_r=0.0,
        )

    def _paper_to_demo(self, report: SessionReport) -> PromotionGate:
        return self._gate(
            "paper_to_demo",
            report,
            min_decisions=300,
            min_closed=50,
            min_average_r=0.10,
        )

    def _demo_to_tiny_live(self, report: SessionReport) -> PromotionGate:
        return self._gate(
            "demo_to_tiny_live",
            report,
            min_decisions=500,
            min_closed=100,
            min_average_r=0.15,
        )

    def _gate(
        self,
        stage: str,
        report: SessionReport,
        min_decisions: int,
        min_closed: int,
        min_average_r: float,
    ) -> PromotionGate:
        reasons: list[str] = []
        if report.sample_size < min_decisions:
            reasons.append("not_enough_decisions")
        if report.closed_position_count < min_closed:
            reasons.append("not_enough_closed_trades")
        if report.average_r_multiple < min_average_r:
            reasons.append("average_r_below_threshold")
        if report.open_position_count > 0:
            reasons.append("open_positions_still_running")
        if report.loss_count > report.win_count and report.closed_position_count > 0:
            reasons.append("losses_exceed_wins")
        return PromotionGate(
            stage=stage,
            allowed=not reasons,
            reasons=reasons,
            metrics={
                "sample_size": report.sample_size,
                "closed_position_count": report.closed_position_count,
                "open_position_count": report.open_position_count,
                "win_rate": report.win_rate,
                "average_r_multiple": report.average_r_multiple,
                "total_r_multiple": report.total_r_multiple,
                "min_decisions": min_decisions,
                "min_closed": min_closed,
                "min_average_r": min_average_r,
            },
        )
