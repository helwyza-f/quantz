from quantz.models import ExecutionResult, MarketSnapshot, OrderRequest, OrderSide
from quantz.paper import PaperPortfolio


def test_paper_portfolio_closes_buy_at_take_profit(tmp_path):
    portfolio = PaperPortfolio(tmp_path / "paper-state.json")
    order = OrderRequest(
        symbol="XAUUSD",
        side=OrderSide.BUY,
        volume=0.01,
        entry_price=100.0,
        stop_loss=99.0,
        take_profit=102.0,
        comment="test",
    )
    portfolio.open_position(
        order,
        ExecutionResult(True, "paper-1", "filled", filled_price=100.0),
        decision_id="decision-1",
        reason_codes=["spread_acceptable"],
    )

    closed = portfolio.reconcile(
        MarketSnapshot(
            symbol="XAUUSD",
            bid=102.1,
            ask=102.2,
            spread_points=10,
            atr_points=100,
            trend_score=0.5,
            volatility_score=0.5,
            session="test",
        )
    )

    assert len(closed) == 1
    assert closed[0].exit_reason == "take_profit"
    assert closed[0].r_multiple == 2.0
    assert closed[0].decision_id == "decision-1"
    assert closed[0].reason_codes == ["spread_acceptable"]
