from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from quantz.agent import TradingAgent
from quantz.models import AccountState, AgentContext, MarketSnapshot, OrderSide


@dataclass(frozen=True)
class ReplaySummary:
    symbol: str
    candles: int
    decisions: int
    opened: int
    closed: int


class ReplayRunner:
    def __init__(self, agent: TradingAgent, account: AccountState | None = None) -> None:
        self.agent = agent
        self.account = account or AccountState(equity=10_000, balance=10_000, free_margin=9_500)

    def run_csv(self, symbol: str, path: str | Path, constraints: dict[str, Any] | None = None) -> ReplaySummary:
        candles = self._read_candles(path)
        opened = 0
        closed = 0
        decisions = 0
        for candle in candles:
            market = self._snapshot(symbol, candle)
            context = AgentContext(market=market, account=self.account, constraints=constraints or {})
            record = self.agent.run_once(context)
            decisions += 1
            if record.execution and record.execution.accepted:
                opened += 1
            closed += self._replay_close_count(record, candle)
        return ReplaySummary(symbol=symbol, candles=len(candles), decisions=decisions, opened=opened, closed=closed)

    def _replay_close_count(self, record: Any, candle: dict[str, Any]) -> int:
        if not self.agent.paper_portfolio:
            return 0
        state = self.agent.paper_portfolio._read_state()
        remaining: list[dict[str, Any]] = []
        closed = 0
        high = float(candle["high"])
        low = float(candle["low"])
        close_price = float(candle["close"])
        for raw in state["open_positions"]:
            side = raw.get("side")
            hit_price = None
            reason = ""
            if side == OrderSide.BUY.value:
                if low <= float(raw["stop_loss"]):
                    hit_price = float(raw["stop_loss"])
                    reason = "stop_loss"
                elif high >= float(raw["take_profit"]):
                    hit_price = float(raw["take_profit"])
                    reason = "take_profit"
            else:
                if high >= float(raw["stop_loss"]):
                    hit_price = float(raw["stop_loss"])
                    reason = "stop_loss"
                elif low <= float(raw["take_profit"]):
                    hit_price = float(raw["take_profit"])
                    reason = "take_profit"
            if hit_price is None:
                remaining.append(raw)
                continue
            position = self.agent.paper_portfolio._position(raw)
            state["closed_positions"].append(self.agent.paper_portfolio._serialize(self.agent.paper_portfolio._close(position, hit_price, reason)))
            closed += 1
        state["open_positions"] = remaining
        self.agent.paper_portfolio._write_state(state)
        return closed

    def _read_candles(self, path: str | Path) -> list[dict[str, Any]]:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    def _snapshot(self, symbol: str, row: dict[str, Any]) -> MarketSnapshot:
        close = float(row["close"])
        spread_points = float(row.get("spread_points") or 20)
        point = 0.01 if symbol.upper().startswith("XAU") else 0.00001
        half_spread = spread_points * point / 2
        timestamp = self._timestamp(row.get("time", ""))
        return MarketSnapshot(
            symbol=symbol,
            bid=round(close - half_spread, 5),
            ask=round(close + half_spread, 5),
            spread_points=spread_points,
            atr_points=float(row.get("atr_points") or max(spread_points * 4, 1)),
            trend_score=float(row.get("trend_score") or 0.0),
            volatility_score=float(row.get("volatility_score") or 0.5),
            session=str(row.get("session") or "replay"),
            news_risk=str(row.get("news_risk") or "low"),
            timestamp=timestamp,
            features={"source": "replay", "open": float(row["open"]), "high": float(row["high"]), "low": float(row["low"]), "close": close},
        )

    def _timestamp(self, value: str) -> datetime:
        if not value:
            return datetime.now(timezone.utc)
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)
