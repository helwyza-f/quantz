from quantz.bridge import BridgeAccountFeed, BridgeBrokerAdapter, BridgeMarketFeed
from quantz.models import OrderRequest, OrderSide


class FakeBridgeClient:
    def get_json(self, path):
        if path == "/market/XAUUSD":
            return {
                "symbol": "XAUUSD",
                "bid": 2348.1,
                "ask": 2348.3,
                "spread_points": 20,
                "atr_points": 140,
                "trend_score": 0.7,
                "volatility_score": 0.4,
                "session": "london",
                "news_risk": "low",
                "features": {"source": "bridge"},
            }
        if path == "/account":
            return {
                "equity": 10000,
                "balance": 10000,
                "free_margin": 9500,
                "open_positions": 1,
                "currency": "USD",
            }
        raise AssertionError(path)

    def post_json(self, path, payload):
        assert path == "/orders"
        assert payload["symbol"] == "XAUUSD"
        return {
            "accepted": True,
            "broker_order_id": "123",
            "message": "filled",
            "filled_price": payload["entry_price"],
            "raw": {"payload_seen": True},
        }


def test_bridge_market_and_account_feeds_map_contract():
    client = FakeBridgeClient()

    market = BridgeMarketFeed(client).snapshot("XAUUSD")
    account = BridgeAccountFeed(client).state()

    assert market.features["source"] == "bridge"
    assert account.open_positions == 1


def test_bridge_broker_adapter_maps_execution_result():
    result = BridgeBrokerAdapter(FakeBridgeClient()).place_order(
        OrderRequest(
            symbol="XAUUSD",
            side=OrderSide.BUY,
            volume=0.01,
            entry_price=2348.3,
            stop_loss=2346.0,
            take_profit=2352.0,
            comment="test",
        )
    )

    assert result.accepted is True
    assert result.broker_order_id == "123"
