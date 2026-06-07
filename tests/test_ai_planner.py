import json

from quantz.audit import DecisionAuditStore
from quantz.config import AgentSettings
from quantz.models import AccountState, AgentContext, MarketSnapshot, TradeAction
from quantz.planner import AIDecisionPlanner, DecisionPolicy
from quantz.services import AgentRuntimeService


def context(spread_points=10, trend_score=0.8, mode="paper"):
    return AgentContext(
        market=MarketSnapshot(
            symbol="XAUUSD",
            bid=2300.0,
            ask=2300.1,
            spread_points=spread_points,
            atr_points=100,
            trend_score=trend_score,
            volatility_score=0.5,
            session="london",
            news_risk="low",
        ),
        account=AccountState(equity=10000, balance=10000, free_margin=9000),
        constraints={
            "mode": mode,
            "min_confidence": 0.65,
            "default_risk_percent": 0.25,
            "memory_context": {"sample_size": 30, "closed_trade_summary": {"average_r": 0.2}},
        },
    )


def ai_response(**overrides):
    payload = {
        "action": "open_position",
        "side": "buy",
        "confidence": 0.82,
        "entry_price": 2300.1,
        "stop_loss": 2298.6,
        "take_profit": 2302.65,
        "risk_percent": 0.25,
        "reason_codes": ["ai_trend_memory_alignment"],
        "decision_brief": "Trend and memory support a controlled paper entry.",
        "memory_used": ["recent reason quality is positive"],
        "risk_notes": ["risk remains within policy"],
    }
    payload.update(overrides)
    return {"output_text": json.dumps(payload)}


def test_ai_decision_planner_reads_structured_trade_response(tmp_path):
    captured = {}

    def request_fn(payload):
        captured.update(payload)
        return ai_response()

    planner = AIDecisionPlanner(
        request_fn=request_fn,
        audit_store=None,
        policy=DecisionPolicy(min_confidence=0.65, max_risk_percent=0.5, max_spread_points=50),
    )

    decision = planner.decide(context())

    assert decision.action == TradeAction.OPEN_POSITION
    assert decision.side.value == "buy"
    assert decision.confidence == 0.82
    assert decision.risk_percent == 0.25
    assert decision.metadata["ai_decision"]["memory_used"] == ["recent reason quality is positive"]
    assert json.loads(captured["input"])["memory_context"]["sample_size"] == 30


def test_ai_decision_policy_blocks_unsafe_response_and_audits(tmp_path):
    audit_path = tmp_path / "audit.jsonl"

    planner = AIDecisionPlanner(
        request_fn=lambda _payload: ai_response(confidence=0.4, risk_percent=2.0),
        audit_store=DecisionAuditStore(audit_path),
        policy=DecisionPolicy(min_confidence=0.65, max_risk_percent=0.5, max_spread_points=50),
    )

    decision = planner.decide(context())

    assert decision.action == TradeAction.HOLD
    assert "policy_confidence_below_minimum" in decision.reason_codes
    assert "policy_risk_percent_out_of_bounds" in decision.reason_codes
    rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["kind"] == "ai_decision"
    assert rows[0]["policy"]["reasons"]


def test_ai_decision_planner_fails_closed_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    decision = AIDecisionPlanner().decide(context())

    assert decision.action == TradeAction.HOLD
    assert "ai_api_key_missing" in decision.reason_codes


def test_runtime_service_builds_ai_agent_with_audit_path(tmp_path):
    settings = AgentSettings(
        planner="ai",
        mode="paper",
        market_source="demo",
        memory_path=str(tmp_path / "experience.jsonl"),
        paper_state_path=str(tmp_path / "paper.json"),
        decision_audit_path=str(tmp_path / "audit.jsonl"),
        vector_memory_path=str(tmp_path / "vector.db"),
    )

    agent = AgentRuntimeService(settings).build_agent()

    assert agent.planner.model_version.startswith("ai_decision_planner:")
