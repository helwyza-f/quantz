from __future__ import annotations

from abc import ABC, abstractmethod

from quantz.models import AgentContext, AnalystOutput


class Analyst(ABC):
    @abstractmethod
    def analyze(self, context: AgentContext) -> AnalystOutput:
        """Return structured reasoning that can influence, not bypass, planning and risk."""


class NoOpAnalyst(Analyst):
    def analyze(self, context: AgentContext) -> AnalystOutput:
        return AnalystOutput(
            market_regime="unknown",
            bias="neutral",
            reason_codes=["analyst_disabled"],
            model_version="noop_analyst_v1",
        )


class RuleBasedAnalyst(Analyst):
    model_version = "rule_analyst_v1"

    def analyze(self, context: AgentContext) -> AnalystOutput:
        market = context.market
        reason_codes: list[str] = []
        risk_notes: list[str] = []

        if abs(market.trend_score) >= 0.65:
            market_regime = "trend"
            reason_codes.append("strong_trend")
        elif market.volatility_score < 0.2:
            market_regime = "quiet"
            reason_codes.append("low_volatility")
        elif market.volatility_score > 0.85:
            market_regime = "volatile"
            reason_codes.append("high_volatility")
        else:
            market_regime = "mixed"
            reason_codes.append("mixed_regime")

        if market.trend_score >= 0.45:
            bias = "buy"
        elif market.trend_score <= -0.45:
            bias = "sell"
        else:
            bias = "neutral"

        avoid_trade = False
        confidence_adjustment = 0.0

        if market.spread_points > float(context.constraints.get("analyst_max_spread_points", 60)):
            avoid_trade = True
            risk_notes.append("spread_too_wide_for_analyst")
        if market.news_risk == "high":
            avoid_trade = True
            risk_notes.append("high_news_risk")
        if market_regime == "volatile":
            confidence_adjustment -= 0.06
            risk_notes.append("volatile_regime_reduce_confidence")
        if market_regime == "trend" and market.news_risk == "low":
            confidence_adjustment += 0.03
            reason_codes.append("trend_low_news_support")

        return AnalystOutput(
            market_regime=market_regime,
            bias=bias,
            confidence_adjustment=round(confidence_adjustment, 4),
            avoid_trade=avoid_trade,
            reason_codes=reason_codes,
            risk_notes=risk_notes,
            model_version=self.model_version,
        )
