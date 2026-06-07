from quantz.market import DemoAccountFeed, DemoMarketFeed
from quantz.memory_context import MemoryContextBuilder
from quantz.models import AgentContext


class FakeMemoryStore:
    def context_summary(self, symbol, limit=80):
        return {
            "symbol": symbol,
            "sample_size": 12,
            "recent_decisions": [
                {
                    "action": "open_position",
                    "confidence": 0.72,
                    "risk_status": "approved",
                    "execution_accepted": True,
                    "reason_codes": ["bullish_market_structure"],
                    "timestamp": "t1",
                }
            ],
            "rejection_reasons": {
                "spread_above_limit": 3,
                "confidence_below_minimum": 1,
            },
            "reason_quality": {
                "bullish_market_structure": {"count": 7, "average_r": 0.42, "total_r": 2.94},
                "spread_acceptable": {"count": 4, "average_r": -0.2, "total_r": -0.8},
            },
            "closed_trade_summary": {
                "count": 11,
                "win_count": 6,
                "loss_count": 5,
                "average_r": 0.18,
                "total_r": 1.98,
            },
        }


def test_memory_context_builder_returns_compact_decision_context():
    context = AgentContext(
        market=DemoMarketFeed().snapshot("XAUUSD"),
        account=DemoAccountFeed().state(),
        constraints={},
    )

    memory_context = MemoryContextBuilder(FakeMemoryStore()).build(context)

    assert memory_context["symbol"] == "XAUUSD"
    assert memory_context["market_regime"] == "trend"
    assert memory_context["sample_size"] == 12
    assert memory_context["rejection_focus"][0] == {"reason": "spread_above_limit", "count": 3}
    assert memory_context["best_reasons"][0]["reason"] == "bullish_market_structure"
    assert memory_context["weak_reasons"][0]["reason"] == "spread_acceptable"
    assert memory_context["risk_posture"]["free_margin_percent"] == 95.0
    assert memory_context["lessons"]


def test_memory_context_builder_handles_empty_memory():
    class EmptyStore:
        def context_summary(self, symbol, limit=80):
            return {
                "symbol": symbol,
                "sample_size": 0,
                "recent_decisions": [],
                "rejection_reasons": {},
                "reason_quality": {},
                "closed_trade_summary": {"count": 0, "average_r": 0.0},
            }

    context = AgentContext(
        market=DemoMarketFeed().snapshot("XAUUSD"),
        account=DemoAccountFeed().state(),
        constraints={},
    )

    memory_context = MemoryContextBuilder(EmptyStore()).build(context)

    assert memory_context["sample_size"] == 0
    assert "No closed trades" in memory_context["lessons"][0]


def test_memory_context_builder_adds_compacted_semantic_recall():
    class FakeVectorStore:
        def search(self, query, limit=5, filters=None):
            assert "XAUUSD" in query
            assert filters == {"symbol": "XAUUSD"}
            return [
                {
                    "score": 0.91,
                    "payload": {
                        "kind": "lesson",
                        "text": "Strong trend continuation worked best when spread stayed tight.",
                        "symbol": "XAUUSD",
                        "source": "teacher",
                        "ignored": "not exposed",
                    },
                },
                {
                    "score": 0.82,
                    "payload": {
                        "kind": "closed_trade",
                        "text": "Late-session breakout failed after volatility expansion.",
                        "symbol": "XAUUSD",
                        "outcome": "loss",
                    },
                },
                {"score": 0.7, "payload": {"text": "extra 1", "symbol": "XAUUSD"}},
                {"score": 0.6, "payload": {"text": "extra 2", "symbol": "XAUUSD"}},
                {"score": 0.5, "payload": {"text": "extra 3", "symbol": "XAUUSD"}},
            ]

    context = AgentContext(
        market=DemoMarketFeed().snapshot("XAUUSD"),
        account=DemoAccountFeed().state(),
        constraints={},
    )

    memory_context = MemoryContextBuilder(
        FakeMemoryStore(),
        vector_store=FakeVectorStore(),
        max_context_items=8,
    ).build(context)

    assert len(memory_context["semantic_recall"]) == 4
    assert memory_context["semantic_recall"][0]["kind"] == "lesson"
    assert memory_context["semantic_recall"][0]["metadata"] == {
        "symbol": "XAUUSD",
        "source": "teacher",
    }
