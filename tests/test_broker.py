from quantz.broker import Mt5BrokerAdapter
from quantz.models import OrderRequest, OrderSide


class FakeResult:
    def __init__(self, retcode, order=0, price=0.0):
        self.retcode = retcode
        self.order = order
        self.price = price

    def _asdict(self):
        return {"retcode": self.retcode, "order": self.order, "price": self.price}


class FakeSymbolInfo:
    visible = True
    filling_mode = 0


class FakeMt5:
    TRADE_ACTION_DEAL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_INVALID_FILL = 10030

    def __init__(self):
        self.requests = []

    def initialize(self):
        return True

    def symbol_info(self, _symbol):
        return FakeSymbolInfo()

    def order_send(self, request):
        self.requests.append(dict(request))
        if request["type_filling"] == self.ORDER_FILLING_IOC:
            return FakeResult(self.TRADE_RETCODE_INVALID_FILL)
        return FakeResult(self.TRADE_RETCODE_DONE, order=123, price=request["price"])

    def last_error(self):
        return (0, "")


def test_mt5_broker_retries_filling_mode_after_invalid_fill():
    adapter = Mt5BrokerAdapter.__new__(Mt5BrokerAdapter)
    adapter.mt5 = FakeMt5()

    result = adapter.place_order(
        OrderRequest(
            symbol="XAUUSD",
            side=OrderSide.BUY,
            volume=0.01,
            entry_price=4400.0,
            stop_loss=4390.0,
            take_profit=4420.0,
            comment="Quantz test",
        )
    )

    assert result.accepted is True
    assert result.message == "mt5_retcode:10009:done"
    assert [request["type_filling"] for request in adapter.mt5.requests] == [1, 0]
    assert result.raw["attempted_fillings"] == [1, 0]
