from __future__ import annotations

from abc import ABC, abstractmethod

from quantz.models import ExecutionResult, OrderRequest


class BrokerAdapter(ABC):
    @abstractmethod
    def place_order(self, order: OrderRequest) -> ExecutionResult:
        """Place an order through the broker rail."""


class PaperBrokerAdapter(BrokerAdapter):
    def place_order(self, order: OrderRequest) -> ExecutionResult:
        return ExecutionResult(
            accepted=True,
            broker_order_id=f"paper-{order.client_order_id}",
            message="paper_order_filled",
            filled_price=order.entry_price,
            raw={
                "symbol": order.symbol,
                "side": order.side,
                "volume": order.volume,
                "stop_loss": order.stop_loss,
                "take_profit": order.take_profit,
            },
        )


class Mt5BrokerAdapter(BrokerAdapter):
    """Execution rail for MetaTrader 5.

    The agent brain should stay outside this adapter. This class only translates
    an approved order into the MT5 terminal request format.
    """

    def __init__(self) -> None:
        try:
            import MetaTrader5 as mt5  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("Install the optional mt5 dependency to use live MT5 execution.") from exc
        self.mt5 = mt5

    def place_order(self, order: OrderRequest) -> ExecutionResult:
        mt5 = self.mt5
        if not mt5.initialize():
            return ExecutionResult(False, None, f"mt5_initialize_failed:{mt5.last_error()}")

        symbol_info = mt5.symbol_info(order.symbol)
        if symbol_info is None:
            return ExecutionResult(False, None, f"symbol_not_found:{order.symbol}")
        if not symbol_info.visible and not mt5.symbol_select(order.symbol, True):
            return ExecutionResult(False, None, f"symbol_select_failed:{order.symbol}")

        order_type = mt5.ORDER_TYPE_BUY if order.side.value == "buy" else mt5.ORDER_TYPE_SELL
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": order.symbol,
            "volume": order.volume,
            "type": order_type,
            "price": order.entry_price,
            "sl": order.stop_loss,
            "tp": order.take_profit,
            "deviation": 20,
            "magic": 20260605,
            "comment": order.comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None:
            return ExecutionResult(False, None, f"mt5_order_send_none:{mt5.last_error()}", raw=request)

        retcode = getattr(result, "retcode", None)
        accepted = retcode == mt5.TRADE_RETCODE_DONE
        return ExecutionResult(
            accepted=accepted,
            broker_order_id=str(getattr(result, "order", "")) if accepted else None,
            message=f"mt5_retcode:{retcode}",
            filled_price=getattr(result, "price", None),
            raw=result._asdict() if hasattr(result, "_asdict") else {"retcode": retcode},
        )
