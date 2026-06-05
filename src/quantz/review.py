from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from quantz.report import SessionReport, SymbolReport


@dataclass(frozen=True)
class Recommendation:
    type: str
    target: str
    change: dict[str, Any]
    reason: str
    confidence: str = "low"


@dataclass(frozen=True)
class AgentReview:
    promotion_status: str
    recommendations: list[Recommendation] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


class ReviewEngine:
    min_closed_trades_for_demo: int = 20
    min_closed_trades_for_live: int = 100

    def review(self, report: SessionReport) -> AgentReview:
        recommendations: list[Recommendation] = []
        risk_notes: list[str] = []

        if report.closed_position_count < self.min_closed_trades_for_demo:
            risk_notes.append("not_enough_closed_trades_for_demo_promotion")
        if report.open_position_count > 0:
            risk_notes.append("open_paper_positions_still_running")
        if report.loss_count > report.win_count and report.closed_position_count > 0:
            risk_notes.append("losses_exceed_wins")
        if report.average_r_multiple < 0:
            risk_notes.append("negative_average_r")

        recommendations.extend(self._symbol_recommendations(report.by_symbol))
        recommendations.extend(self._global_recommendations(report))

        promotion_status = self._promotion_status(report, risk_notes)
        return AgentReview(
            promotion_status=promotion_status,
            recommendations=recommendations,
            risk_notes=risk_notes,
            summary={
                "sample_size": report.sample_size,
                "closed_position_count": report.closed_position_count,
                "open_position_count": report.open_position_count,
                "win_rate": report.win_rate,
                "average_r_multiple": report.average_r_multiple,
                "total_r_multiple": report.total_r_multiple,
            },
        )

    def to_dict(self, review: AgentReview) -> dict[str, Any]:
        return asdict(review)

    def _promotion_status(self, report: SessionReport, risk_notes: list[str]) -> str:
        if report.closed_position_count < self.min_closed_trades_for_demo:
            return "paper_only"
        if report.average_r_multiple <= 0 or report.win_rate < 0.45:
            return "paper_only"
        if report.closed_position_count < self.min_closed_trades_for_live:
            return "demo_candidate"
        if risk_notes:
            return "demo_only"
        return "live_candidate_min_lot"

    def _symbol_recommendations(self, by_symbol: dict[str, SymbolReport]) -> list[Recommendation]:
        recommendations: list[Recommendation] = []
        for symbol, symbol_report in by_symbol.items():
            duplicate_rejections = symbol_report.rejection_reasons.get("symbol_already_has_open_paper_position", 0)
            if duplicate_rejections > max(3, symbol_report.executed * 3):
                recommendations.append(
                    Recommendation(
                        type="reduce_duplicate_scans",
                        target=symbol,
                        change={"cooldown_until_position_closed": True},
                        reason="many scans happened while a paper position was already open",
                        confidence="medium",
                    )
                )

            if symbol_report.closed >= 5 and symbol_report.average_r_multiple < 0:
                recommendations.append(
                    Recommendation(
                        type="disable_symbol_candidate",
                        target=symbol,
                        change={"enabled": False},
                        reason="symbol has negative average R over enough closed paper trades",
                        confidence="medium",
                    )
                )

            if symbol_report.closed >= 5 and symbol_report.average_r_multiple > 0.5 and symbol_report.wins > symbol_report.losses:
                recommendations.append(
                    Recommendation(
                        type="keep_symbol_candidate",
                        target=symbol,
                        change={"enabled": True},
                        reason="symbol has positive average R and wins exceed losses",
                        confidence="medium",
                    )
                )
        return recommendations

    def _global_recommendations(self, report: SessionReport) -> list[Recommendation]:
        recommendations: list[Recommendation] = []

        if report.closed_position_count < self.min_closed_trades_for_demo:
            recommendations.append(
                Recommendation(
                    type="collect_more_paper_data",
                    target="global",
                    change={"min_closed_trades_before_demo": self.min_closed_trades_for_demo},
                    reason="closed sample is too small for reliable promotion",
                    confidence="high",
                )
            )

        if report.rejected_count > report.approved_count * 5 and report.rejection_reasons.get(
            "symbol_already_has_open_paper_position", 0
        ):
            recommendations.append(
                Recommendation(
                    type="increase_monitor_interval_or_add_position_cooldown",
                    target="global",
                    change={"position_cooldown": "until_closed"},
                    reason="most rejections come from rescanning symbols with open paper positions",
                    confidence="medium",
                )
            )

        if report.closed_position_count >= 10 and report.average_r_multiple < 0:
            recommendations.append(
                Recommendation(
                    type="raise_confidence_threshold",
                    target="global",
                    change={"min_confidence_delta": 0.05},
                    reason="average R is negative over enough closed paper trades",
                    confidence="medium",
                )
            )

        if report.closed_position_count >= 10 and report.average_r_multiple > 0.5 and report.win_rate >= 0.5:
            recommendations.append(
                Recommendation(
                    type="demo_candidate_review",
                    target="global",
                    change={"next_stage": "demo_account_paper_to_demo_execution"},
                    reason="paper outcomes are positive over enough closed trades",
                    confidence="medium",
                )
            )

        return recommendations
