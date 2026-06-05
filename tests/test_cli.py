from quantz.cli import _reconcile_for_cooldown, _run_once
from quantz.config import AgentSettings
from quantz.models import ExecutionResult, MarketSnapshot, OrderRequest, OrderSide
from quantz.paper import PaperPortfolio


def test_run_once_cli_helper_uses_demo_source(tmp_path):
    settings = AgentSettings(
        memory_path=str(tmp_path / "experience.jsonl"),
        paper_state_path=str(tmp_path / "paper-state.json"),
        mode="paper",
        market_source="demo",
        execution_source="mt5",
        bridge_url="http://127.0.0.1:8765",
    )
    record = _run_once(settings, symbol="XAUUSD")

    assert record.execution is not None
    assert record.execution.accepted is True


def test_reconcile_for_cooldown_can_close_position(tmp_path):
    settings = AgentSettings(
        paper_state_path=str(tmp_path / "paper-state.json"),
        position_cooldown="until_closed",
        mode="paper",
    )
    portfolio = PaperPortfolio(settings.paper_state_path)
    portfolio.open_position(
        OrderRequest(
            symbol="XAUUSD",
            side=OrderSide.BUY,
            volume=0.01,
            entry_price=100,
            stop_loss=99,
            take_profit=101,
            comment="test",
        ),
        ExecutionResult(True, "paper-1", "filled", filled_price=100),
    )

    closed = _reconcile_for_cooldown(
        settings,
        MarketSnapshot(
            symbol="XAUUSD",
            bid=101.1,
            ask=101.2,
            spread_points=10,
            atr_points=100,
            trend_score=0.5,
            volatility_score=0.5,
            session="test",
        ),
    )

    assert len(closed) == 1
    assert portfolio.open_count("XAUUSD") == 0
