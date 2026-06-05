import json

from quantz.config import AgentSettings
from quantz.experiment import ExperimentRunner
from quantz.models import ExecutionResult, MarketSnapshot, OrderRequest, OrderSide
from quantz.paper import PaperPortfolio


def fake_monitor(settings, iterations, quiet=False):
    portfolio = PaperPortfolio(settings.paper_state_path)
    order = OrderRequest(
        symbol="XAUUSD",
        side=OrderSide.BUY,
        volume=0.01,
        entry_price=100,
        stop_loss=99,
        take_profit=101,
        comment="test",
    )
    portfolio.open_position(order, ExecutionResult(True, "paper-1", "filled", filled_price=100))
    portfolio.reconcile(
        MarketSnapshot(
            symbol="XAUUSD",
            bid=101.1,
            ask=101.2,
            spread_points=10,
            atr_points=100,
            trend_score=0.5,
            volatility_score=0.5,
            session="test",
        )
    )
    with open(settings.memory_path, "a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "decision": {"symbol": "XAUUSD", "confidence": 0.8},
                    "risk": {"status": "approved", "reasons": ["risk_checks_passed"]},
                    "execution": {"accepted": True},
                }
            )
            + "\n"
        )


def test_experiment_runner_writes_artifacts(tmp_path):
    summary = ExperimentRunner(fake_monitor).run(
        AgentSettings(symbols=["XAUUSD"], market_source="sim"),
        tmp_path / "experiment",
        iterations=1,
    )

    paths = summary["paths"]
    assert (tmp_path / "experiment" / "base-config.json").exists()
    assert (tmp_path / "experiment" / "candidate-config.json").exists()
    assert (tmp_path / "experiment" / "comparison.json").exists()
    assert summary["comparison"]["verdict"] in {
        "candidate_preferred",
        "candidate_operationally_better",
        "continue_paper_test",
        "reject_candidate",
        "no_clear_winner",
    }
    candidate_config = json.loads(open(paths["candidate_config"], encoding="utf-8").read())
    assert candidate_config["mode"] == "paper"
