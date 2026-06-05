from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4


class TradeAction(StrEnum):
    HOLD = "hold"
    OPEN_POSITION = "open_position"
    CLOSE_POSITION = "close_position"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class ExecutionMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class DecisionStatus(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    bid: float
    ask: float
    spread_points: float
    atr_points: float
    trend_score: float
    volatility_score: float
    session: str
    news_risk: str = "low"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    features: dict[str, float | str | bool] = field(default_factory=dict)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class AccountState:
    equity: float
    balance: float
    free_margin: float
    daily_realized_pnl: float = 0.0
    open_risk_percent: float = 0.0
    open_positions: int = 0
    currency: str = "USD"


@dataclass(frozen=True)
class AgentContext:
    market: MarketSnapshot
    account: AccountState
    constraints: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AnalystOutput:
    market_regime: str
    bias: str
    confidence_adjustment: float = 0.0
    avoid_trade: bool = False
    reason_codes: list[str] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)
    model_version: str = "analyst_v1"


@dataclass(frozen=True)
class TradeDecision:
    action: TradeAction
    symbol: str
    side: OrderSide | None
    confidence: float
    entry_price: float | None
    stop_loss: float | None
    take_profit: float | None
    risk_percent: float
    model_version: str
    reason_codes: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)
    decision_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class RiskDecision:
    status: DecisionStatus
    approved_lot: float
    reasons: list[str]

    @property
    def approved(self) -> bool:
        return self.status == DecisionStatus.APPROVED


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: OrderSide
    volume: float
    entry_price: float
    stop_loss: float
    take_profit: float
    comment: str
    client_order_id: str = field(default_factory=lambda: str(uuid4()))


@dataclass(frozen=True)
class ExecutionResult:
    accepted: bool
    broker_order_id: str | None
    message: str
    filled_price: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class ExperienceRecord:
    context: AgentContext
    decision: TradeDecision
    risk: RiskDecision
    execution: ExecutionResult | None
    outcome: dict[str, Any] | None = None
