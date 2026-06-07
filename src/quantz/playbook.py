from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from quantz.models import AgentContext


@dataclass(frozen=True)
class Playbook:
    name: str
    symbols: list[str] = field(default_factory=list)
    sessions: list[str] = field(default_factory=list)
    allowed_regimes: list[str] = field(default_factory=list)
    entry_conditions: dict[str, float] = field(default_factory=dict)
    risk: dict[str, float] = field(default_factory=dict)
    status: str = "training"
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlaybookSelection:
    playbook: Playbook | None
    status: str
    reasons: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.playbook is not None and self.status == "allowed"

    def to_context(self) -> dict[str, Any]:
        return {
            "selected": self.playbook.to_dict() if self.playbook else None,
            "status": self.status,
            "reasons": list(self.reasons),
        }


class PlaybookLoader:
    def load_many(self, paths: list[str | Path]) -> list[Playbook]:
        playbooks: list[Playbook] = []
        for path in paths:
            playbooks.extend(self.load_path(path))
        return playbooks

    def load_path(self, path: str | Path) -> list[Playbook]:
        playbook_path = Path(path)
        if playbook_path.is_dir():
            return [
                self.load_file(child)
                for child in sorted(playbook_path.glob("*.json"))
            ]
        return [self.load_file(playbook_path)]

    def load_file(self, path: str | Path) -> Playbook:
        with Path(path).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return Playbook(
            name=str(payload["name"]),
            symbols=[str(symbol).upper() for symbol in payload.get("symbols", [])],
            sessions=[str(session) for session in payload.get("sessions", [])],
            allowed_regimes=[str(regime) for regime in payload.get("allowed_regimes", [])],
            entry_conditions={str(key): float(value) for key, value in payload.get("entry_conditions", {}).items()},
            risk={str(key): float(value) for key, value in payload.get("risk", {}).items()},
            status=str(payload.get("status", "training")),
            description=str(payload.get("description", "")),
        )


class PlaybookSelector:
    LIVE_STATUSES = {"live_tiny_allowed", "live_allowed"}
    PAPER_STATUSES = {"training", "paper_allowed", "demo_allowed", "live_tiny_allowed", "live_allowed"}
    DEMO_STATUSES = {"demo_allowed", "live_tiny_allowed", "live_allowed"}

    def __init__(self, playbooks: list[Playbook]) -> None:
        self.playbooks = playbooks

    def select(self, context: AgentContext, mode: str = "paper") -> PlaybookSelection:
        if not self.playbooks:
            return PlaybookSelection(None, "not_configured", ["no_playbooks_configured"])

        candidates = [
            playbook
            for playbook in self.playbooks
            if not playbook.symbols or context.market.symbol.upper() in playbook.symbols
        ]
        if not candidates:
            return PlaybookSelection(None, "blocked", ["no_playbook_for_symbol"])

        blocked_reasons: list[str] = []
        for playbook in candidates:
            reasons = self._block_reasons(playbook, context, mode)
            if not reasons:
                return PlaybookSelection(playbook, "allowed", ["playbook_selected"])
            blocked_reasons.extend(f"{playbook.name}:{reason}" for reason in reasons)
        return PlaybookSelection(None, "blocked", blocked_reasons[:8])

    def _block_reasons(self, playbook: Playbook, context: AgentContext, mode: str) -> list[str]:
        reasons: list[str] = []
        market = context.market
        memory_context = context.constraints.get("memory_context", {})
        regime = str(memory_context.get("market_regime", "")) if isinstance(memory_context, dict) else ""

        if playbook.sessions and market.session not in playbook.sessions:
            reasons.append("session_not_allowed")
        if playbook.allowed_regimes and regime and regime not in playbook.allowed_regimes:
            reasons.append("regime_not_allowed")
        if not self._status_allowed(playbook.status, mode):
            reasons.append(f"status_not_allowed_for_{mode}")

        conditions = playbook.entry_conditions
        if "trend_score_min" in conditions and abs(market.trend_score) < conditions["trend_score_min"]:
            reasons.append("trend_score_below_playbook_minimum")
        if "volatility_min" in conditions and market.volatility_score < conditions["volatility_min"]:
            reasons.append("volatility_below_playbook_minimum")
        if "volatility_max" in conditions and market.volatility_score > conditions["volatility_max"]:
            reasons.append("volatility_above_playbook_maximum")
        if "spread_max" in conditions and market.spread_points > conditions["spread_max"]:
            reasons.append("spread_above_playbook_maximum")
        return reasons

    def _status_allowed(self, status: str, mode: str) -> bool:
        if mode == "live":
            return status in self.LIVE_STATUSES
        if mode == "demo":
            return status in self.DEMO_STATUSES
        return status in self.PAPER_STATUSES
