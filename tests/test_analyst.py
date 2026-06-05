from quantz.analyst import RuleBasedAnalyst
from quantz.models import AccountState, AgentContext, MarketSnapshot, TradeAction
from quantz.planner import VariableDrivenPlanner


def context(spread_points=10, trend_score=0.8, volatility_score=0.5, news_risk="low"):
    return AgentContext(
        market=MarketSnapshot(
            symbol="XAUUSD",
            bid=100,
            ask=100.1,
            spread_points=spread_points,
            atr_points=100,
            trend_score=trend_score,
            volatility_score=volatility_score,
            session="test",
            news_risk=news_risk,
        ),
        account=AccountState(equity=10000, balance=10000, free_margin=9000),
        constraints={"min_confidence": 0.65},
    )


def test_rule_analyst_can_block_wide_spread_trade():
    base = context(spread_points=100)
    analysis = RuleBasedAnalyst().analyze(base)
    enriched = AgentContext(base.market, base.account, {**base.constraints, "analyst": analysis})

    decision = VariableDrivenPlanner().decide(enriched)

    assert analysis.avoid_trade is True
    assert decision.action == TradeAction.HOLD
    assert "spread_too_wide_for_analyst" in decision.reason_codes


def test_rule_analyst_adjusts_confidence_for_strong_trend():
    base = context(spread_points=10, trend_score=0.8)
    plain = VariableDrivenPlanner().decide(base)
    analysis = RuleBasedAnalyst().analyze(base)
    enriched = AgentContext(base.market, base.account, {**base.constraints, "analyst": analysis})

    adjusted = VariableDrivenPlanner().decide(enriched)

    assert analysis.confidence_adjustment > 0
    assert adjusted.confidence > plain.confidence
    assert "strong_trend" in adjusted.reason_codes
