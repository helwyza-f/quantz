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


def test_planner_applies_memory_weak_reason_penalty():
    base = context(spread_points=10, trend_score=0.8)
    plain = VariableDrivenPlanner().decide(base)
    enriched = AgentContext(
        base.market,
        base.account,
        {
            **base.constraints,
            "memory_context": {
                "closed_trade_summary": {"count": 25, "average_r": -0.1},
                "weak_reasons": [
                    {"reason": "bullish_market_structure", "count": 20, "average_r": -0.2}
                ],
            },
        },
    )

    adjusted = VariableDrivenPlanner().decide(enriched)

    assert adjusted.confidence < plain.confidence
    assert "memory_weak_reason_penalty" in adjusted.reason_codes


def test_llm_analyst_fails_closed_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    analysis = LLMAnalyst().analyze(context())

    assert analysis.avoid_trade is True
    assert "llm_analyst_unavailable" in analysis.reason_codes
    assert "llm_api_key_missing" in analysis.risk_notes


def test_llm_analyst_reads_structured_response():
    captured_payload = {}

    def request_fn(payload):
        captured_payload.update(payload)
        return {
            "output_text": (
                '{"market_regime":"trend","bias":"buy","confidence_adjustment":0.05,'
                '"avoid_trade":false,"reason_codes":["llm_trend_confirmed"],"risk_notes":[],'
                '"decision_brief":"Trend confirmed with acceptable risk context.",'
                '"market_read":"Buyer control is visible on the current snapshot.",'
                '"entry_plan":"Only consider entry after risk governor approval.",'
                '"invalidation":"Avoid if spread widens or trend reverses.",'
                '"key_observations":["trend positive"],"memory_notes":["no recent conflict"]}'
            )
        }

    analyst = LLMAnalyst(
        request_fn=request_fn
    )

    analysis = analyst.analyze(context())

    assert analysis.avoid_trade is False
    assert analysis.bias == "buy"
    assert analysis.confidence_adjustment == 0.05
    assert analysis.reason_codes == ["llm_trend_confirmed"]
    assert analysis.metadata["llm_trace"]["request"]["model"] == captured_payload["model"]
    assert analysis.metadata["llm_trace"]["request"]["input"]["market"]["symbol"] == "XAUUSD"
    assert analysis.metadata["llm_trace"]["response"]["parsed"]["bias"] == "buy"
    assert analysis.metadata["llm_trace"]["response"]["parsed"]["decision_brief"]


def test_llm_analyst_payload_includes_experience_memory():
    captured_payload = {}

    def request_fn(payload):
        captured_payload.update(payload)
        return {
            "output_text": (
                '{"market_regime":"mixed","bias":"neutral","confidence_adjustment":-0.05,'
                '"avoid_trade":true,"reason_codes":["memory_reason_quality_weak"],"risk_notes":[],'
                '"decision_brief":"Recent memory is not strong enough.",'
                '"market_read":"Current snapshot is mixed.",'
                '"entry_plan":"Wait for cleaner setup.",'
                '"invalidation":"Trade only after reason quality improves.",'
                '"key_observations":["mixed setup"],"memory_notes":["recent average R is negative"]}'
            )
        }

    base = context()
    enriched = AgentContext(
        base.market,
        base.account,
        {
            **base.constraints,
            "experience_memory": {
                "symbol": "XAUUSD",
                "sample_size": 12,
                "closed_trade_summary": {"average_r": -0.3},
            },
            "memory_context": {
                "symbol": "XAUUSD",
                "sample_size": 12,
                "closed_trade_summary": {"average_r": -0.3},
                "lessons": ["recent average R is negative"],
            },
        },
    )

    LLMAnalyst(request_fn=request_fn).analyze(enriched)

    request_input = captured_payload["input"]
    assert "memory_context" in request_input
    assert '"average_r": -0.3' in request_input


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
    assert analysis.metadata["llm_trace"]["status"] == "error"
    assert analysis.metadata["llm_trace"]["response"]["error"] == "llm_http_error:400:Unsupported model"
