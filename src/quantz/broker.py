from __future__ import annotations

import re
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
        base_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": order.symbol,
            "volume": order.volume,
            "type": order_type,
            "price": order.entry_price,
            "sl": order.stop_loss,
            "tp": order.take_profit,
            "deviation": 20,
            "magic": 20260605,
            "comment": self._comment(order.comment),
            "type_time": mt5.ORDER_TIME_GTC,
        }
        result = None
        request = dict(base_request)
        attempted_fillings = []
        for filling in self._filling_candidates(symbol_info):
            request = {**base_request, "type_filling": filling}
            attempted_fillings.append(filling)
            result = mt5.order_send(request)
            if result is None:
                return ExecutionResult(False, None, f"mt5_order_send_none:{mt5.last_error()}", raw=request)
            retcode = getattr(result, "retcode", None)
            if retcode != getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030):
                break
        if result is None:
            return ExecutionResult(False, None, "mt5_order_send_not_attempted", raw=request)

        retcode = getattr(result, "retcode", None)
        accepted = retcode in {mt5.TRADE_RETCODE_DONE, getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", mt5.TRADE_RETCODE_DONE)}
        raw = result._asdict() if hasattr(result, "_asdict") else {"retcode": retcode}
        raw["request"] = request
        raw["attempted_fillings"] = attempted_fillings
        return ExecutionResult(
            accepted=accepted,
            broker_order_id=str(getattr(result, "order", "")) if accepted else None,
            message=f"mt5_retcode:{retcode}:{self._retcode_label(retcode)}",
            filled_price=getattr(result, "price", None),
            raw=raw,
        )

    def _comment(self, value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9 _-]", "", value or "")
        return (cleaned[:20] or "QZ").strip() or "QZ"

    def _filling_candidates(self, symbol_info: object) -> list[int]:
        mt5 = self.mt5
        mode = int(getattr(symbol_info, "filling_mode", 0) or 0)
        flagged = [
            (getattr(mt5, "SYMBOL_FILLING_FOK", 1), mt5.ORDER_FILLING_FOK),
            (getattr(mt5, "SYMBOL_FILLING_IOC", 2), mt5.ORDER_FILLING_IOC),
        ]
        candidates = [order_filling for flag, order_filling in flagged if mode & int(flag)]
        candidates.extend(
            [
                mt5.ORDER_FILLING_IOC,
                mt5.ORDER_FILLING_FOK,
                getattr(mt5, "ORDER_FILLING_RETURN", mt5.ORDER_FILLING_IOC),
            ]
        )
        unique: list[int] = []
        for candidate in candidates:
            if candidate not in unique:
                unique.append(candidate)
        return unique

    def _retcode_label(self, retcode: object) -> str:
        mt5 = self.mt5
        labels = {
            getattr(mt5, "TRADE_RETCODE_DONE", 10009): "done",
            getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010): "done_partial",
            getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030): "invalid_filling_mode",
            getattr(mt5, "TRADE_RETCODE_REQUOTE", 10004): "requote",
            getattr(mt5, "TRADE_RETCODE_PRICE_CHANGED", 10020): "price_changed",
            getattr(mt5, "TRADE_RETCODE_INVALID_STOPS", 10016): "invalid_stops",
            getattr(mt5, "TRADE_RETCODE_NO_MONEY", 10019): "no_money",
            getattr(mt5, "TRADE_RETCODE_MARKET_CLOSED", 10018): "market_closed",
        }
        return labels.get(retcode, "unknown")
