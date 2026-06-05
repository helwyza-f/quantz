from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any

from quantz.learning import ExperienceLearner


@dataclass(frozen=True)
class SymbolReport:
    symbol: str
    decisions: int = 0
    approved: int = 0
    rejected: int = 0
    executed: int = 0
    closed: int = 0
    wins: int = 0
    losses: int = 0
    open_positions: int = 0
    average_confidence: float = 0.0
    average_r_multiple: float = 0.0
    total_r_multiple: float = 0.0
    rejection_reasons: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionReport:
    sample_size: int
    approved_count: int
    rejected_count: int
    executed_count: int
    closed_position_count: int
    open_position_count: int
    win_count: int
    loss_count: int
    win_rate: float
    average_confidence: float
    average_r_multiple: float
    total_r_multiple: float
    rejection_reasons: dict[str, int]
    by_symbol: dict[str, SymbolReport]
    candidate_constraints: dict[str, float]
    notes: list[str]


class ReportBuilder:
    def build(self, experience_rows: list[dict[str, Any]], paper_state_path: str | Path) -> SessionReport:
        paper_state = self._read_paper_state(paper_state_path)
        closed_positions = self._closed_positions(experience_rows, paper_state)
        open_positions = list(paper_state.get("open_positions", []))
        learner_report = ExperienceLearner().analyze(experience_rows)

        confidences = [
            float(row.get("decision", {}).get("confidence", 0.0))
            for row in experience_rows
            if "decision" in row
        ]
        r_multiples = [float(position.get("r_multiple", 0.0)) for position in closed_positions]
        wins = [value for value in r_multiples if value > 0]
        losses = [value for value in r_multiples if value < 0]
        rejection_reasons = self._rejection_reasons(experience_rows)
        by_symbol = self._by_symbol(experience_rows, closed_positions, open_positions)

        return SessionReport(
            sample_size=len(experience_rows),
            approved_count=sum(1 for row in experience_rows if row.get("risk", {}).get("status") == "approved"),
            rejected_count=sum(1 for row in experience_rows if row.get("risk", {}).get("status") == "rejected"),
            executed_count=sum(1 for row in experience_rows if (row.get("execution") or {}).get("accepted") is True),
            closed_position_count=len(closed_positions),
            open_position_count=len(open_positions),
            win_count=len(wins),
            loss_count=len(losses),
            win_rate=round(len(wins) / len(r_multiples), 4) if r_multiples else 0.0,
            average_confidence=round(mean(confidences), 4) if confidences else 0.0,
            average_r_multiple=round(mean(r_multiples), 4) if r_multiples else 0.0,
            total_r_multiple=round(sum(r_multiples), 4),
            rejection_reasons=rejection_reasons,
            by_symbol=by_symbol,
            candidate_constraints=learner_report.candidate_constraints,
            notes=learner_report.notes,
        )

    def to_dict(self, report: SessionReport) -> dict[str, Any]:
        return asdict(report)

    def _read_paper_state(self, paper_state_path: str | Path) -> dict[str, list[dict[str, Any]]]:
        path = Path(paper_state_path)
        if not path.exists():
            return {"open_positions": [], "closed_positions": []}
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return {
            "open_positions": list(payload.get("open_positions", [])),
            "closed_positions": list(payload.get("closed_positions", [])),
        }

    def _closed_positions(
        self,
        experience_rows: list[dict[str, Any]],
        paper_state: dict[str, list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        by_id: dict[str, dict[str, Any]] = {}
        for position in paper_state.get("closed_positions", []):
            by_id[str(position.get("position_id"))] = position
        for row in experience_rows:
            for position in (row.get("outcome") or {}).get("closed_positions", []):
                by_id[str(position.get("position_id"))] = position
        return list(by_id.values())

    def _rejection_reasons(self, experience_rows: list[dict[str, Any]]) -> dict[str, int]:
        reasons: dict[str, int] = {}
        for row in experience_rows:
            if row.get("risk", {}).get("status") != "rejected":
                continue
            for reason in row.get("risk", {}).get("reasons", []):
                reasons[reason] = reasons.get(reason, 0) + 1
        return dict(sorted(reasons.items(), key=lambda item: item[1], reverse=True))

    def _by_symbol(
        self,
        experience_rows: list[dict[str, Any]],
        closed_positions: list[dict[str, Any]],
        open_positions: list[dict[str, Any]],
    ) -> dict[str, SymbolReport]:
        symbols = sorted(
            {
                *[str(row.get("decision", {}).get("symbol")) for row in experience_rows if row.get("decision")],
                *[str(position.get("symbol")) for position in closed_positions],
                *[str(position.get("symbol")) for position in open_positions],
            }
            - {"None"}
        )
        reports: dict[str, SymbolReport] = {}
        for symbol in symbols:
            rows = [row for row in experience_rows if row.get("decision", {}).get("symbol") == symbol]
            closed = [position for position in closed_positions if position.get("symbol") == symbol]
            open_count = sum(1 for position in open_positions if position.get("symbol") == symbol)
            confidences = [float(row.get("decision", {}).get("confidence", 0.0)) for row in rows]
            r_multiples = [float(position.get("r_multiple", 0.0)) for position in closed]
            reasons = self._rejection_reasons(rows)
            reports[symbol] = SymbolReport(
                symbol=symbol,
                decisions=len(rows),
                approved=sum(1 for row in rows if row.get("risk", {}).get("status") == "approved"),
                rejected=sum(1 for row in rows if row.get("risk", {}).get("status") == "rejected"),
                executed=sum(1 for row in rows if (row.get("execution") or {}).get("accepted") is True),
                closed=len(closed),
                wins=sum(1 for value in r_multiples if value > 0),
                losses=sum(1 for value in r_multiples if value < 0),
                open_positions=open_count,
                average_confidence=round(mean(confidences), 4) if confidences else 0.0,
                average_r_multiple=round(mean(r_multiples), 4) if r_multiples else 0.0,
                total_r_multiple=round(sum(r_multiples), 4),
                rejection_reasons=reasons,
            )
        return reports
