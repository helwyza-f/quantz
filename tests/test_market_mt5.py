import pytest

from quantz.market import Mt5Connection, Mt5MarketFeed


class FakeMt5:
    TIMEFRAME_M15 = 15

    def __init__(self):
        self.initialized = False

    def initialize(self):
        self.initialized = True
        return True

    def last_error(self):
        return (0, "ok")

    def symbol_info(self, symbol):
        return type("SymbolInfo", (), {"visible": True, "point": 0.01})()

    def symbol_info_tick(self, symbol):
        return type("Tick", (), {"bid": 2348.1, "ask": 2348.3, "time": 1})()

    def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        return [
            {"high": 2340 + idx, "low": 2339 + idx, "close": 2339.5 + idx}
            for idx in range(64)
        ]


def test_mt5_market_feed_maps_terminal_data():
    connection = Mt5Connection.__new__(Mt5Connection)
    connection.mt5 = FakeMt5()
    connection._initialized = False

    snapshot = Mt5MarketFeed(connection).snapshot("XAUUSD")

    assert snapshot.symbol == "XAUUSD"
    assert snapshot.bid == 2348.1
    assert snapshot.ask == 2348.3
    assert snapshot.spread_points == pytest.approx(20)
    assert snapshot.features["source"] == "mt5"
