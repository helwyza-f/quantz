from __future__ import annotations

from dataclasses import dataclass

from quantz.models import AgentContext, DecisionStatus, RiskDecision, TradeAction, TradeDecision


@dataclass(frozen=True)
class RiskConfig:
    max_risk_per_trade_percent: float = 0.5
    max_daily_loss_percent: float = 2.0
    max_open_positions: int = 3
    max_open_risk_percent: float = 1.5
    max_spread_points: float = 50
    min_confidence: float = 0.65
    min_free_margin_percent: float = 30
    lot_step: float = 0.01
    min_lot: float = 0.01
    max_lot: float = 1.0
    contract_size: float = 1_000
    allow_min_lot_when_below_minimum: bool = False


class RiskGovernor:
    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()

    def evaluate(self, context: AgentContext, decision: TradeDecision) -> RiskDecision:
        reasons: list[str] = []

        if decision.action == TradeAction.HOLD:
            return RiskDecision(DecisionStatus.REJECTED, 0.0, ["planner_chose_hold"])

        if decision.side is None:
            reasons.append("missing_side")
        if decision.stop_loss is None:
            reasons.append("missing_stop_loss")
        if decision.take_profit is None:
            reasons.append("missing_take_profit")
        if decision.entry_price is None:
            reasons.append("missing_entry_price")
        if decision.confidence < self.config.min_confidence:
            reasons.append("confidence_below_minimum")
        if decision.risk_percent <= 0 or decision.risk_percent > self.config.max_risk_per_trade_percent:
            reasons.append("risk_percent_out_of_bounds")
        if context.market.spread_points > self.config.max_spread_points:
            reasons.append("spread_above_limit")
        if context.account.open_positions >= self.config.max_open_positions:
            reasons.append("too_many_open_positions")
        if context.constraints.get("block_when_symbol_open") and context.constraints.get("open_symbol_positions", 0) > 0:
            reasons.append("symbol_already_has_open_paper_position")
        if context.account.open_risk_percent >= self.config.max_open_risk_percent:
            reasons.append("open_risk_above_limit")

        daily_loss_percent = abs(min(context.account.daily_realized_pnl, 0)) / context.account.equity * 100
        if daily_loss_percent >= self.config.max_daily_loss_percent:
            reasons.append("daily_loss_limit_reached")

        free_margin_percent = context.account.free_margin / context.account.equity * 100
        if free_margin_percent < self.config.min_free_margin_percent:
            reasons.append("free_margin_below_limit")

        if reasons:
            return RiskDecision(DecisionStatus.REJECTED, 0.0, reasons)

        lot = self._position_size(context, decision)
        if lot < self.config.min_lot:
            if not self.config.allow_min_lot_when_below_minimum:
                return RiskDecision(DecisionStatus.REJECTED, 0.0, ["computed_lot_below_minimum"])
            lot = min(self.config.min_lot, self.config.max_lot)
            return RiskDecision(DecisionStatus.APPROVED, lot, ["risk_checks_passed", "min_lot_demo_override"])

        return RiskDecision(DecisionStatus.APPROVED, lot, ["risk_checks_passed"])

    def _position_size(self, context: AgentContext, decision: TradeDecision) -> float:
        assert decision.entry_price is not None
        assert decision.stop_loss is not None

        risk_amount = context.account.equity * (decision.risk_percent / 100)
        stop_distance = abs(decision.entry_price - decision.stop_loss)
        if stop_distance <= 0:
            return 0.0

        raw_lot = risk_amount / (stop_distance * self.config.contract_size)
        stepped = int(raw_lot / self.config.lot_step) * self.config.lot_step
        return round(min(stepped, self.config.max_lot), 2)
