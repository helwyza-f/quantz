from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import fsum
from urllib.parse import unquote, urlparse

try:
    import MetaTrader5 as mt5
except ImportError as exc:
    raise SystemExit("Install MetaTrader5 on the Windows/VPS machine: python -m pip install MetaTrader5") from exc


def ensure_mt5() -> None:
    if not mt5.initialize():
        raise RuntimeError(f"mt5_initialize_failed:{mt5.last_error()}")


def market_snapshot(symbol: str) -> dict:
    ensure_mt5()
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"symbol_not_found:{symbol}")
    if not info.visible and not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"symbol_select_failed:{symbol}")

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        raise RuntimeError(f"tick_unavailable:{symbol}")

    point = float(info.point or fallback_point(symbol))
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 64)
    rates = list(rates) if rates is not None else []
    bid = float(tick.bid)
    ask = float(tick.ask)
    spread_points = max(0.0, (ask - bid) / point)

    return {
        "symbol": symbol,
        "bid": bid,
        "ask": ask,
        "spread_points": round(spread_points, 2),
        "atr_points": round(atr_points(rates, point), 2) if rates else max(spread_points * 4, 1),
        "trend_score": trend_score(rates) if rates else 0.0,
        "volatility_score": volatility_score(rates, point, spread_points) if rates else 0.0,
        "session": "mt5_bridge",
        "news_risk": "low",
        "features": {
            "source": "mt5_bridge",
            "point": point,
            "rates_loaded": len(rates),
            "last_tick_time": int(getattr(tick, "time", 0)),
        },
    }


def account_state() -> dict:
    ensure_mt5()
    account = mt5.account_info()
    if account is None:
        raise RuntimeError(f"account_info_unavailable:{mt5.last_error()}")
    positions = mt5.positions_get()
    return {
        "equity": float(account.equity),
        "balance": float(account.balance),
        "free_margin": float(account.margin_free),
        "daily_realized_pnl": 0.0,
        "open_risk_percent": 0.0,
        "open_positions": len(positions or []),
        "currency": str(getattr(account, "currency", "USD")),
    }


def place_order(payload: dict) -> dict:
    ensure_mt5()
    symbol = str(payload["symbol"])
    side = str(payload["side"])
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"symbol_not_found:{symbol}")
    if not info.visible and not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"symbol_select_failed:{symbol}")
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    base_request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(payload["volume"]),
        "type": order_type,
        "price": float(payload["entry_price"]),
        "sl": float(payload["stop_loss"]),
        "tp": float(payload["take_profit"]),
        "deviation": 20,
        "magic": 20260605,
        "comment": safe_comment(str(payload.get("comment", "quantz"))),
        "type_time": mt5.ORDER_TIME_GTC,
    }
    result = None
    request = dict(base_request)
    attempted_fillings = []
    for filling in filling_candidates(info):
        request = {**base_request, "type_filling": filling}
        attempted_fillings.append(filling)
        result = mt5.order_send(request)
        if result is None:
            return {
                "accepted": False,
                "broker_order_id": None,
                "message": f"order_send_none:{mt5.last_error()}",
                "filled_price": None,
                "raw": request,
            }
        if getattr(result, "retcode", None) != getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030):
            break
    if result is None:
        return {
            "accepted": False,
            "broker_order_id": None,
            "message": "order_send_not_attempted",
            "filled_price": None,
            "raw": request,
        }

    raw = result._asdict() if hasattr(result, "_asdict") else {"retcode": getattr(result, "retcode", None)}
    raw["request"] = request
    raw["attempted_fillings"] = attempted_fillings
    retcode = getattr(result, "retcode", None)
    accepted = retcode in {mt5.TRADE_RETCODE_DONE, getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", mt5.TRADE_RETCODE_DONE)}
    return {
        "accepted": accepted,
        "broker_order_id": str(getattr(result, "order", "")) if accepted else None,
        "message": f"retcode:{retcode}:{retcode_label(retcode)}",
        "filled_price": getattr(result, "price", None),
        "raw": raw,
    }


def safe_comment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 _-]", "", value or "")
    return (cleaned[:20] or "QZ").strip() or "QZ"


def filling_candidates(info: object) -> list[int]:
    mode = int(getattr(info, "filling_mode", 0) or 0)
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
    unique = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def retcode_label(retcode: object) -> str:
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


def atr_points(rates: list, point: float) -> float:
    true_ranges = []
    previous_close = None
    for rate in rates[-15:]:
        high = float(rate["high"])
        low = float(rate["low"])
        close = float(rate["close"])
        if previous_close is None:
            true_range = high - low
        else:
            true_range = max(high - low, abs(high - previous_close), abs(low - previous_close))
        true_ranges.append(true_range)
        previous_close = close
    return fsum(true_ranges) / len(true_ranges) / point if true_ranges else 0.0


def trend_score(rates: list) -> float:
    if len(rates) < 21:
        return 0.0
    closes = [float(rate["close"]) for rate in rates]
    fast = fsum(closes[-8:]) / 8
    slow = fsum(closes[-21:]) / 21
    recent_range = max(closes[-21:]) - min(closes[-21:])
    if recent_range <= 0:
        return 0.0
    return round(max(-1.0, min(1.0, (fast - slow) / recent_range * 3)), 4)


def volatility_score(rates: list, point: float, spread_points: float) -> float:
    current_atr = atr_points(rates, point)
    if current_atr <= 0:
        return 0.0
    spread_load = min(spread_points / max(current_atr, 1), 1.0)
    normalized_atr = min(current_atr / 500, 1.0)
    return round(max(0.0, min(1.0, normalized_atr * 0.8 + spread_load * 0.2)), 4)


def fallback_point(symbol: str) -> float:
    if symbol.upper().startswith("XAU"):
        return 0.01
    if "JPY" in symbol.upper():
        return 0.001
    return 0.00001


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path.startswith("/market/"):
                symbol = unquote(path.removeprefix("/market/"))
                self.respond(200, market_snapshot(symbol))
                return
            if path == "/account":
                self.respond(200, account_state())
                return
            self.respond(404, {"error": "not_found"})
        except Exception as exc:
            self.respond(500, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            if urlparse(self.path).path != "/orders":
                self.respond(404, {"error": "not_found"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            self.respond(200, place_order(payload))
        except Exception as exc:
            self.respond(500, {"error": str(exc)})

    def respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Quantz MT5 bridge listening on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
