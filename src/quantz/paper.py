from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from quantz.models import ExecutionResult, MarketSnapshot, OrderRequest, OrderSide


@dataclass(frozen=True)
class PaperPosition:
    position_id: str
    symbol: str
    side: OrderSide
    volume: float
    entry_price: float
    stop_loss: float
    take_profit: float
    opened_at: str
    source_order_id: str
    decision_id: str | None = None
    reason_codes: list[str] = field(default_factory=list)
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0


@dataclass(frozen=True)
class PaperClose:
    position_id: str
    symbol: str
    side: OrderSide
    entry_price: float
    exit_price: float
    exit_reason: str
    volume: float
    opened_at: str
    closed_at: str
    r_multiple: float
    pnl_points: float
    decision_id: str | None = None
    reason_codes: list[str] = field(default_factory=list)


class PaperPortfolio:
    def __init__(self, path: str | Path = "data/paper-state.json") -> None:
        self.path = Path(path)

    def open_position(
        self,
        order: OrderRequest,
        result: ExecutionResult,
        decision_id: str | None = None,
        reason_codes: list[str] | None = None,
    ) -> PaperPosition:
        position = PaperPosition(
            position_id=f"paper-pos-{uuid4()}",
            symbol=order.symbol,
            side=order.side,
            volume=order.volume,
            entry_price=result.filled_price or order.entry_price,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            opened_at=datetime.now(timezone.utc).isoformat(),
            source_order_id=result.broker_order_id or order.client_order_id,
            decision_id=decision_id,
            reason_codes=list(reason_codes or []),
        )
        state = self._read_state()
        state["open_positions"].append(self._serialize(position))
        self._write_state(state)
        return position

    def open_count(self, symbol: str | None = None) -> int:
        state = self._read_state()
        if symbol is None:
            return len(state["open_positions"])
        return sum(1 for position in state["open_positions"] if position.get("symbol") == symbol)

    def reconcile(self, snapshot: MarketSnapshot) -> list[PaperClose]:
        state = self._read_state()
        remaining: list[dict[str, Any]] = []
        closed: list[PaperClose] = []

        for raw_position in state["open_positions"]:
            position = self._position(raw_position)
            if position.symbol != snapshot.symbol:
                remaining.append(raw_position)
                continue

            updated = self._with_excursions(position, snapshot)
            close = self._close_if_hit(updated, snapshot)
            if close is None:
                remaining.append(self._serialize(updated))
            else:
                closed.append(close)
                state["closed_positions"].append(self._serialize(close))

        state["open_positions"] = remaining
        self._write_state(state)
        return closed

    def _close_if_hit(self, position: PaperPosition, snapshot: MarketSnapshot) -> PaperClose | None:
        if position.side == OrderSide.BUY:
            if snapshot.bid <= position.stop_loss:
                return self._close(position, position.stop_loss, "stop_loss")
            if snapshot.bid >= position.take_profit:
                return self._close(position, position.take_profit, "take_profit")
        else:
            if snapshot.ask >= position.stop_loss:
                return self._close(position, position.stop_loss, "stop_loss")
            if snapshot.ask <= position.take_profit:
                return self._close(position, position.take_profit, "take_profit")
        return None

    def _close(self, position: PaperPosition, exit_price: float, exit_reason: str) -> PaperClose:
        direction = 1 if position.side == OrderSide.BUY else -1
        pnl_points = (exit_price - position.entry_price) * direction
        risk_points = abs(position.entry_price - position.stop_loss)
        r_multiple = pnl_points / risk_points if risk_points else 0.0
        return PaperClose(
            position_id=position.position_id,
            symbol=position.symbol,
            side=position.side,
            entry_price=position.entry_price,
            exit_price=exit_price,
            exit_reason=exit_reason,
            volume=position.volume,
            opened_at=position.opened_at,
            closed_at=datetime.now(timezone.utc).isoformat(),
            r_multiple=round(r_multiple, 4),
            pnl_points=round(pnl_points, 5),
            decision_id=position.decision_id,
            reason_codes=list(position.reason_codes),
        )

    def _with_excursions(self, position: PaperPosition, snapshot: MarketSnapshot) -> PaperPosition:
        direction = 1 if position.side == OrderSide.BUY else -1
        favorable_price = snapshot.bid if position.side == OrderSide.BUY else snapshot.ask
        adverse_price = snapshot.ask if position.side == OrderSide.BUY else snapshot.bid
        favorable = max(0.0, (favorable_price - position.entry_price) * direction)
        adverse = max(0.0, (position.entry_price - adverse_price) * direction)
        return PaperPosition(
            **{
                **asdict(position),
                "max_favorable_excursion": max(position.max_favorable_excursion, favorable),
                "max_adverse_excursion": max(position.max_adverse_excursion, adverse),
            }
        )

    def _read_state(self) -> dict[str, list[dict[str, Any]]]:
        if not self.path.exists():
            return {"open_positions": [], "closed_positions": []}
        with self.path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return {
            "open_positions": list(payload.get("open_positions", [])),
            "closed_positions": list(payload.get("closed_positions", [])),
        }

    def _write_state(self, state: dict[str, list[dict[str, Any]]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)

    def _serialize(self, value: Any) -> dict[str, Any]:
        payload = asdict(value)
        if "side" in payload and hasattr(payload["side"], "value"):
            payload["side"] = payload["side"].value
        return payload

    def _position(self, payload: dict[str, Any]) -> PaperPosition:
        return PaperPosition(
            **{
                **payload,
                "side": OrderSide(payload["side"]),
                "decision_id": payload.get("decision_id"),
                "reason_codes": list(payload.get("reason_codes", [])),
            }
        )
