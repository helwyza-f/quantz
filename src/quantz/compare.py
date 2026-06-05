from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from quantz.report import SessionReport


@dataclass(frozen=True)
class RunComparison:
    verdict: str
    metric_deltas: dict[str, float]
    symbol_deltas: dict[str, dict[str, float]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class RunComparator:
    def compare(self, base: SessionReport, candidate: SessionReport) -> RunComparison:
        deltas = {
            "sample_size": candidate.sample_size - base.sample_size,
            "rejected_count": candidate.rejected_count - base.rejected_count,
            "executed_count": candidate.executed_count - base.executed_count,
            "closed_position_count": candidate.closed_position_count - base.closed_position_count,
            "open_position_count": candidate.open_position_count - base.open_position_count,
            "win_rate": round(candidate.win_rate - base.win_rate, 4),
            "average_r_multiple": round(candidate.average_r_multiple - base.average_r_multiple, 4),
            "total_r_multiple": round(candidate.total_r_multiple - base.total_r_multiple, 4),
        }
        symbol_deltas = self._symbol_deltas(base, candidate)
        notes = self._notes(base, candidate, deltas)
        return RunComparison(
            verdict=self._verdict(deltas, notes),
            metric_deltas=deltas,
            symbol_deltas=symbol_deltas,
            notes=notes,
        )

    def to_dict(self, comparison: RunComparison) -> dict[str, Any]:
        return asdict(comparison)

    def _symbol_deltas(self, base: SessionReport, candidate: SessionReport) -> dict[str, dict[str, float]]:
        symbols = sorted(set(base.by_symbol) | set(candidate.by_symbol))
        deltas: dict[str, dict[str, float]] = {}
        for symbol in symbols:
            base_symbol = base.by_symbol.get(symbol)
            candidate_symbol = candidate.by_symbol.get(symbol)
            deltas[symbol] = {
                "decisions": (candidate_symbol.decisions if candidate_symbol else 0)
                - (base_symbol.decisions if base_symbol else 0),
                "rejected": (candidate_symbol.rejected if candidate_symbol else 0)
                - (base_symbol.rejected if base_symbol else 0),
                "executed": (candidate_symbol.executed if candidate_symbol else 0)
                - (base_symbol.executed if base_symbol else 0),
                "closed": (candidate_symbol.closed if candidate_symbol else 0)
                - (base_symbol.closed if base_symbol else 0),
                "average_r_multiple": round(
                    (candidate_symbol.average_r_multiple if candidate_symbol else 0.0)
                    - (base_symbol.average_r_multiple if base_symbol else 0.0),
                    4,
                ),
                "total_r_multiple": round(
                    (candidate_symbol.total_r_multiple if candidate_symbol else 0.0)
                    - (base_symbol.total_r_multiple if base_symbol else 0.0),
                    4,
                ),
            }
        return deltas

    def _notes(self, base: SessionReport, candidate: SessionReport, deltas: dict[str, float]) -> list[str]:
        notes: list[str] = []
        if deltas["rejected_count"] < 0:
            notes.append("candidate_reduced_rejections")
        if deltas["total_r_multiple"] > 0:
            notes.append("candidate_improved_total_r")
        if deltas["average_r_multiple"] > 0:
            notes.append("candidate_improved_average_r")
        if candidate.closed_position_count < max(5, base.closed_position_count):
            notes.append("candidate_needs_more_closed_trades")
        if candidate.open_position_count > base.open_position_count:
            notes.append("candidate_has_more_open_risk")
        if candidate.average_r_multiple < 0:
            notes.append("candidate_negative_average_r")
        return notes

    def _verdict(self, deltas: dict[str, float], notes: list[str]) -> str:
        if "candidate_negative_average_r" in notes:
            return "reject_candidate"
        if "candidate_needs_more_closed_trades" in notes:
            return "continue_paper_test"
        if deltas["total_r_multiple"] > 0 and deltas["rejected_count"] <= 0:
            return "candidate_preferred"
        if deltas["rejected_count"] < 0 and deltas["average_r_multiple"] >= 0:
            return "candidate_operationally_better"
        return "no_clear_winner"
