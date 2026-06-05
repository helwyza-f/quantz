from quantz.agent import TradingAgent
from quantz.analyst import RuleBasedAnalyst
from quantz.broker import PaperBrokerAdapter
from quantz.market import DemoAccountFeed, DemoMarketFeed
from quantz.memory import JsonlExperienceStore
from quantz.models import AgentContext, DecisionStatus, TradeAction
from quantz.paper import PaperPortfolio
from quantz.planner import VariableDrivenPlanner
from quantz.risk import RiskGovernor


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
