from quantz.agent import TradingAgent
from quantz.analyst import RuleBasedAnalyst
from quantz.broker import PaperBrokerAdapter
from quantz.market import DemoAccountFeed, DemoMarketFeed
from quantz.memory import JsonlExperienceStore
from quantz.models import AgentContext, AnalystOutput, DecisionStatus, TradeAction
from quantz.paper import PaperPortfolio
from quantz.planner import VariableDrivenPlanner
from quantz.risk import RiskConfig, RiskGovernor


class CaptureBroker(PaperBrokerAdapter):
    def __init__(self):
        self.orders = []

    def place_order(self, order):
        self.orders.append(order)
        return super().place_order(order)


class CaptureAnalyst:
    def __init__(self):
        self.contexts = []

    def analyze(self, context):
        self.contexts.append(context)
        return AnalystOutput(
            market_regime="trend",
            bias="buy",
            reason_codes=["capture_analyst"],
            model_version="capture_analyst_v1",
        )


def test_experience_store_skips_corrupt_jsonl_rows(tmp_path):
    path = tmp_path / "experience.jsonl"
    path.write_text('not-json\n{"decision":{"symbol":"XAUUSD"}}\n[1,2,3]\n', encoding="utf-8")

    rows = JsonlExperienceStore(path).read_raw()

    assert rows == [{"decision": {"symbol": "XAUUSD"}}]


def test_experience_store_builds_symbol_context_summary(tmp_path):
    path = tmp_path / "experience.jsonl"
    path.write_text(
        "\n".join(
            [
                '{"context":{"market":{"symbol":"XAUUSD"}},"decision":{"symbol":"XAUUSD","action":"open_position","confidence":0.7,"reason_codes":["bullish_market_structure"],"timestamp":"t1"},"risk":{"status":"approved","reasons":["risk_checks_passed"]},"execution":{"accepted":true},"outcome":{"closed_positions":[{"r_multiple":1.7,"reason_codes":["bullish_market_structure"]}]}}',
                '{"context":{"market":{"symbol":"XAUUSD"}},"decision":{"symbol":"XAUUSD","action":"hold","confidence":0.5,"reason_codes":["spread_too_wide"],"timestamp":"t2"},"risk":{"status":"rejected","reasons":["spread_above_limit"]},"execution":null}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = JsonlExperienceStore(path).context_summary("XAUUSD")

    assert summary["sample_size"] == 2
    assert summary["closed_trade_summary"]["average_r"] == 1.7
    assert summary["rejection_reasons"]["spread_above_limit"] == 1
    assert summary["reason_quality"]["bullish_market_structure"]["average_r"] == 1.7


def test_agent_can_place_paper_trade(tmp_path):
    context = AgentContext(
        market=DemoMarketFeed().snapshot("XAUUSD"),
        account=DemoAccountFeed().state(),
        constraints={"default_risk_percent": 0.25, "min_confidence": 0.65},
    )
    agent = TradingAgent(
        planner=VariableDrivenPlanner(),
        risk_governor=RiskGovernor(),
        broker=PaperBrokerAdapter(),
        memory=JsonlExperienceStore(tmp_path / "experience.jsonl"),
    )

    record = agent.run_once(context)

    assert record.decision.action == TradeAction.OPEN_POSITION
    assert record.risk.status == DecisionStatus.APPROVED
    assert record.execution is not None
    assert record.execution.accepted is True
    assert (tmp_path / "experience.jsonl").exists()


def test_agent_uses_mt5_safe_order_comment(tmp_path):
    broker = CaptureBroker()
    context = AgentContext(
        market=DemoMarketFeed().snapshot("XAUUSD"),
        account=DemoAccountFeed().state(),
        constraints={"default_risk_percent": 0.25, "min_confidence": 0.65},
    )
    agent = TradingAgent(
        planner=VariableDrivenPlanner(),
        risk_governor=RiskGovernor(),
        broker=broker,
        memory=JsonlExperienceStore(tmp_path / "experience.jsonl"),
    )

    agent.run_once(context)

    assert broker.orders
    assert broker.orders[0].comment.startswith("QZ")
    assert len(broker.orders[0].comment) <= 20
    assert ":" not in broker.orders[0].comment
    assert "_" not in broker.orders[0].comment


def test_risk_governor_rejects_wide_spread(tmp_path):
    market = DemoMarketFeed().snapshot("XAUUSD")
    market = market.__class__(**{**market.__dict__, "spread_points": 999})
    context = AgentContext(market=market, account=DemoAccountFeed().state())
    agent = TradingAgent(
        planner=VariableDrivenPlanner(),
        risk_governor=RiskGovernor(),
        broker=PaperBrokerAdapter(),
        memory=JsonlExperienceStore(tmp_path / "experience.jsonl"),
    )

    record = agent.run_once(context)

    assert record.execution is None
    assert record.risk.status == DecisionStatus.REJECTED


def test_paper_agent_blocks_duplicate_symbol_position(tmp_path):
    context = AgentContext(
        market=DemoMarketFeed().snapshot("XAUUSD"),
        account=DemoAccountFeed().state(),
        constraints={"default_risk_percent": 0.25, "min_confidence": 0.65},
    )
    agent = TradingAgent(
        planner=VariableDrivenPlanner(),
        risk_governor=RiskGovernor(),
        broker=PaperBrokerAdapter(),
        memory=JsonlExperienceStore(tmp_path / "experience.jsonl"),
        paper_portfolio=PaperPortfolio(tmp_path / "paper-state.json"),
    )

    first = agent.run_once(context)
    second = agent.run_once(context)

    assert first.execution is not None
    assert second.execution is None
    assert "symbol_already_has_open_paper_position" in second.risk.reasons


def test_paper_agent_counts_external_symbol_position(tmp_path):
    context = AgentContext(
        market=DemoMarketFeed().snapshot("XAUUSD"),
        account=DemoAccountFeed().state(),
        constraints={
            "default_risk_percent": 0.25,
            "min_confidence": 0.65,
            "external_open_symbol_positions": 1,
        },
    )
    agent = TradingAgent(
        planner=VariableDrivenPlanner(),
        risk_governor=RiskGovernor(),
        broker=PaperBrokerAdapter(),
        memory=JsonlExperienceStore(tmp_path / "experience.jsonl"),
        paper_portfolio=PaperPortfolio(tmp_path / "paper-state.json"),
    )

    record = agent.run_once(context)

    assert record.execution is None
    assert record.risk.status == DecisionStatus.REJECTED
    assert "symbol_already_has_open_paper_position" in record.risk.reasons


def test_risk_governor_rejects_position_size_below_min_lot(tmp_path):
    market = DemoMarketFeed().snapshot("XAUUSD")
    context = AgentContext(
        market=market,
        account=DemoAccountFeed().state(),
        constraints={"default_risk_percent": 0.01, "min_confidence": 0.65},
    )
    agent = TradingAgent(
        planner=VariableDrivenPlanner(),
        risk_governor=RiskGovernor(RiskConfig(contract_size=1_000_000)),
        broker=PaperBrokerAdapter(),
        memory=JsonlExperienceStore(tmp_path / "experience.jsonl"),
    )

    record = agent.run_once(context)

    assert record.execution is None
    assert record.risk.status == DecisionStatus.REJECTED
    assert "computed_lot_below_minimum" in record.risk.reasons


def test_risk_governor_can_explicitly_allow_demo_min_lot_override(tmp_path):
    market = DemoMarketFeed().snapshot("XAUUSD")
    context = AgentContext(
        market=market,
        account=DemoAccountFeed().state(),
        constraints={"default_risk_percent": 0.01, "min_confidence": 0.65},
    )
    agent = TradingAgent(
        planner=VariableDrivenPlanner(),
        risk_governor=RiskGovernor(RiskConfig(contract_size=1_000_000, max_lot=0.01, allow_min_lot_when_below_minimum=True)),
        broker=PaperBrokerAdapter(),
        memory=JsonlExperienceStore(tmp_path / "experience.jsonl"),
    )

    record = agent.run_once(context)

    assert record.execution is not None
    assert record.risk.status == DecisionStatus.APPROVED
    assert record.risk.approved_lot == 0.01
    assert "min_lot_demo_override" in record.risk.reasons


def test_agent_records_analyst_metadata(tmp_path):
    context = AgentContext(
        market=DemoMarketFeed().snapshot("XAUUSD"),
        account=DemoAccountFeed().state(),
        constraints={"default_risk_percent": 0.25, "min_confidence": 0.65},
    )
    agent = TradingAgent(
        planner=VariableDrivenPlanner(),
        risk_governor=RiskGovernor(),
        broker=PaperBrokerAdapter(),
        memory=JsonlExperienceStore(tmp_path / "experience.jsonl"),
        analyst=RuleBasedAnalyst(),
    )

    record = agent.run_once(context)

    assert "analyst" in record.decision.metadata
    assert record.decision.metadata["analyst"].model_version == "rule_analyst_v1"


def test_agent_passes_experience_memory_to_analyst(tmp_path):
    memory = JsonlExperienceStore(tmp_path / "experience.jsonl")
    memory.path.write_text(
        '{"context":{"market":{"symbol":"XAUUSD"}},"decision":{"symbol":"XAUUSD","action":"hold","confidence":0.5,"reason_codes":["spread_too_wide"],"timestamp":"t1"},"risk":{"status":"rejected","reasons":["spread_above_limit"]},"execution":null}\n',
        encoding="utf-8",
    )
    analyst = CaptureAnalyst()
    context = AgentContext(
        market=DemoMarketFeed().snapshot("XAUUSD"),
        account=DemoAccountFeed().state(),
        constraints={"default_risk_percent": 0.25, "min_confidence": 0.65},
    )
    agent = TradingAgent(
        planner=VariableDrivenPlanner(),
        risk_governor=RiskGovernor(),
        broker=PaperBrokerAdapter(),
        memory=memory,
        analyst=analyst,
    )

    agent.run_once(context)

    assert analyst.contexts
    memory_context = analyst.contexts[0].constraints["memory_context"]
    assert memory_context["sample_size"] == 1
    assert memory_context["rejection_focus"] == [{"reason": "spread_above_limit", "count": 1}]
    assert analyst.contexts[0].constraints["experience_memory"] == memory_context
