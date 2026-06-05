from __future__ import annotations

from abc import ABC, abstractmethod

from quantz.models import AgentContext, AnalystOutput, OrderSide, TradeAction, TradeDecision


class AgentPlanner(ABC):
    @abstractmethod
    def decide(self, context: AgentContext) -> TradeDecision:
        """Return a structured trade decision from the current world state."""


class VariableDrivenPlanner(AgentPlanner):
    """Baseline planner that mimics the agent contract without hidden execution side effects."""

    model_version = "variable_planner_v1"

    def decide(self, context: AgentContext) -> TradeDecision:
        market = context.market
        constraints = context.constraints
        analyst = self._analyst_output(constraints)

        min_confidence = float(constraints.get("min_confidence", 0.65))
        max_news_risk = str(constraints.get("max_news_risk", "medium"))
        news_blocked = max_news_risk == "low" and market.news_risk != "low"

        direction: OrderSide | None = None
        reason_codes: list[str] = []

        if market.trend_score >= 0.45:
            direction = OrderSide.BUY
            reason_codes.append("bullish_market_structure")
        elif market.trend_score <= -0.45:
            direction = OrderSide.SELL
            reason_codes.append("bearish_market_structure")

        volatility_ok = 0.15 <= market.volatility_score <= 0.85
        if volatility_ok:
            reason_codes.append("volatility_in_range")
        else:
            reason_codes.append("volatility_out_of_range")

        if market.spread_points <= float(constraints.get("planner_max_spread_points", 40)):
            reason_codes.append("spread_acceptable")
        else:
            reason_codes.append("spread_too_wide")

        if news_blocked:
            reason_codes.append("news_risk_blocked")
        if analyst:
            reason_codes.extend(analyst.reason_codes)

        confidence = self._confidence(market.trend_score, market.volatility_score, market.spread_points)
        if analyst:
            confidence = round(max(0.0, min(1.0, confidence + analyst.confidence_adjustment)), 4)
        analyst_blocked = bool(analyst and analyst.avoid_trade)
        if analyst_blocked:
            reason_codes.extend(analyst.risk_notes)

        if direction is None or confidence < min_confidence or not volatility_ok or news_blocked or analyst_blocked:
            return TradeDecision(
                action=TradeAction.HOLD,
                symbol=market.symbol,
                side=None,
                confidence=confidence,
                entry_price=None,
                stop_loss=None,
                take_profit=None,
                risk_percent=0.0,
                model_version=self.model_version,
                reason_codes=reason_codes or ["no_trade_edge"],
                metadata={"analyst": analyst} if analyst else {},
            )

        sl_distance = max(market.atr_points * 1.5, float(constraints.get("min_sl_points", 120)))
        tp_distance = sl_distance * float(constraints.get("reward_risk_ratio", 1.7))
        entry = market.ask if direction == OrderSide.BUY else market.bid

        if direction == OrderSide.BUY:
            stop_loss = entry - sl_distance * self._point_size(market.symbol)
            take_profit = entry + tp_distance * self._point_size(market.symbol)
        else:
            stop_loss = entry + sl_distance * self._point_size(market.symbol)
            take_profit = entry - tp_distance * self._point_size(market.symbol)

        return TradeDecision(
            action=TradeAction.OPEN_POSITION,
            symbol=market.symbol,
            side=direction,
            confidence=confidence,
            entry_price=entry,
            stop_loss=round(stop_loss, 5),
            take_profit=round(take_profit, 5),
            risk_percent=float(constraints.get("default_risk_percent", 0.25)),
            model_version=self.model_version,
            reason_codes=reason_codes,
            metadata={"analyst": analyst} if analyst else {},
        )

    def _confidence(self, trend_score: float, volatility_score: float, spread_points: float) -> float:
        trend_component = min(abs(trend_score), 1.0) * 0.65
        volatility_component = (1.0 - abs(volatility_score - 0.5)) * 0.25
        spread_penalty = min(spread_points / 200, 0.25)
        return round(max(0.0, min(1.0, trend_component + volatility_component + 0.2 - spread_penalty)), 4)

    def _point_size(self, symbol: str) -> float:
        if symbol.upper().startswith("XAU"):
            return 0.01
        if "JPY" in symbol.upper():
            return 0.001
        return 0.00001

    def _analyst_output(self, constraints: dict) -> AnalystOutput | None:
        value = constraints.get("analyst")
        return value if isinstance(value, AnalystOutput) else None
