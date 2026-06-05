from io import BytesIO
from urllib.error import HTTPError

from quantz.analyst import LLMAnalyst, RuleBasedAnalyst
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


def test_llm_analyst_fails_closed_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    analysis = LLMAnalyst().analyze(context())

    assert analysis.avoid_trade is True
    assert "llm_analyst_unavailable" in analysis.reason_codes
    assert "llm_api_key_missing" in analysis.risk_notes


def test_llm_analyst_reads_structured_response():
    analyst = LLMAnalyst(
        request_fn=lambda _payload: {
            "output_text": (
                '{"market_regime":"trend","bias":"buy","confidence_adjustment":0.05,'
                '"avoid_trade":false,"reason_codes":["llm_trend_confirmed"],"risk_notes":[]}'
            )
        }
    )

    analysis = analyst.analyze(context())

    assert analysis.avoid_trade is False
    assert analysis.bias == "buy"
    assert analysis.confidence_adjustment == 0.05
    assert analysis.reason_codes == ["llm_trend_confirmed"]


def test_llm_analyst_reports_http_error_message():
    def request_fn(_payload):
        raise HTTPError(
            url="https://api.openai.com/v1/responses",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=BytesIO(b'{"error":{"message":"Unsupported model"}}'),
        )

    analysis = LLMAnalyst(request_fn=request_fn).analyze(context())

    assert analysis.avoid_trade is True
    assert analysis.risk_notes == ["llm_http_error:400:Unsupported model"]
