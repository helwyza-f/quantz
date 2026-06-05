from quantz.market import SimulatedMarketFeed


def test_simulated_market_feed_persists_price_steps(tmp_path):
    feed = SimulatedMarketFeed(tmp_path / "sim-state.json")

    first = feed.snapshot("XAUUSD")
    second = feed.snapshot("XAUUSD")

    assert first.features["source"] == "sim"
    assert second.features["step"] == 2
    assert second.mid != first.mid
    state = (tmp_path / "sim-state.json").read_text(encoding="utf-8")
    assert "history" in state
    assert '"open"' in state
    assert '"close"' in state
