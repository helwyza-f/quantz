from __future__ import annotations

from abc import ABC, abstractmethod
import json
from datetime import datetime, timezone
from math import fsum
from pathlib import Path

from quantz.models import AccountState, MarketSnapshot


class MarketFeed(ABC):
    @abstractmethod
    def snapshot(self, symbol: str) -> MarketSnapshot:
        """Return the current market state for a symbol."""


class AccountFeed(ABC):
    @abstractmethod
    def state(self) -> AccountState:
        """Return the current trading account state."""


class DemoMarketFeed(MarketFeed):
    PRICES = {
        "XAUUSD": (2348.12, 2348.31, 19, 145, 0.72),
        "EURUSD": (1.08421, 1.08429, 8, 42, 0.51),
        "GBPUSD": (1.27315, 1.27327, 12, 55, -0.58),
        "USDJPY": (156.421, 156.433, 12, 68, 0.47),
        "BTCUSD": (68420.5, 68435.0, 145, 680, 0.62),
    }

    def snapshot(self, symbol: str) -> MarketSnapshot:
        bid, ask, spread_points, atr_points, trend_score = self.PRICES.get(
            symbol.upper(),
            (2348.12, 2348.31, 19, 145, 0.72),
        )
        return MarketSnapshot(
            symbol=symbol,
            bid=bid,
            ask=ask,
            spread_points=spread_points,
            atr_points=atr_points,
            trend_score=trend_score,
            volatility_score=0.48,
            session="london_new_york_overlap",
            news_risk="low",
            features={
                "m15_higher_high": True,
                "h1_above_vwap": True,
                "liquidity_sweep_seen": False,
            },
        )


class DemoAccountFeed(AccountFeed):
    def state(self) -> AccountState:
        return AccountState(
            equity=10_000,
            balance=10_000,
            free_margin=9_500,
            daily_realized_pnl=0,
            open_risk_percent=0,
            open_positions=0,
        )


class SimulatedMarketFeed(MarketFeed):
    """Persistent deterministic demo feed for paper outcome testing."""

    def __init__(self, path: str | Path = "data/sim-market-state.json") -> None:
        self.path = Path(path)
        self.base = DemoMarketFeed.PRICES

    def snapshot(self, symbol: str) -> MarketSnapshot:
        state = self._read_state()
        key = symbol.upper()
        bid, ask, spread_points, atr_points, trend_score = self.base.get(
            key,
            (2348.12, 2348.31, 19, 145, 0.72),
        )
        symbol_state = state.setdefault(key, {"step": 0, "mid": (bid + ask) / 2, "history": []})
        symbol_state["step"] = int(symbol_state.get("step", 0)) + 1

        point = self._point_size(key)
        direction = 1 if trend_score >= 0 else -1
        cycle = symbol_state["step"] % 7
        pulse = 1.0 if cycle not in {0, 6} else -0.45
        move_points = max(atr_points * 0.42, spread_points * 2) * direction * pulse
        previous_mid = float(symbol_state["mid"])
        symbol_state["mid"] = previous_mid + move_points * point

        mid = float(symbol_state["mid"])
        half_spread = spread_points * point / 2
        simulated_bid = mid - half_spread
        simulated_ask = mid + half_spread
        history = list(symbol_state.get("history", []))
        candle_range = max(abs(mid - previous_mid), spread_points * point)
        history.append(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "step": symbol_state["step"],
                "open": round(previous_mid, self._digits(key)),
                "high": round(max(previous_mid, mid) + candle_range * 0.25, self._digits(key)),
                "low": round(min(previous_mid, mid) - candle_range * 0.25, self._digits(key)),
                "close": round(mid, self._digits(key)),
                "bid": round(simulated_bid, self._digits(key)),
                "ask": round(simulated_ask, self._digits(key)),
                "spread_points": spread_points,
                "trend_score": trend_score,
                "volatility_score": 0.52,
            }
        )
        symbol_state["history"] = history[-160:]
        self._write_state(state)

        return MarketSnapshot(
            symbol=symbol,
            bid=round(simulated_bid, self._digits(key)),
            ask=round(simulated_ask, self._digits(key)),
            spread_points=spread_points,
            atr_points=atr_points,
            trend_score=trend_score,
            volatility_score=0.52,
            session="simulated_london_new_york_overlap",
            news_risk="low",
            features={
                "source": "sim",
                "step": symbol_state["step"],
                "m15_higher_high": trend_score > 0,
                "h1_above_vwap": trend_score > 0,
                "liquidity_sweep_seen": cycle == 6,
            },
        )

    def _read_state(self) -> dict[str, dict[str, object]]:
        if not self.path.exists():
            return {}
        with self.path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _write_state(self, state: dict[str, dict[str, object]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)

    def _point_size(self, symbol: str) -> float:
        if symbol.startswith("XAU"):
            return 0.01
        if "JPY" in symbol:
            return 0.001
        if symbol.startswith("BTC"):
            return 0.1
        return 0.00001

    def _digits(self, symbol: str) -> int:
        if symbol.startswith("XAU"):
            return 2
        if "JPY" in symbol:
            return 3
        if symbol.startswith("BTC"):
            return 1
        return 5


class Mt5Connection:
    def __init__(self) -> None:
        try:
            import MetaTrader5 as mt5  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("Install optional dependency with: .venv/bin/python -m pip install '.[mt5]'") from exc
        self.mt5 = mt5
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        if not self.mt5.initialize():
            raise RuntimeError(f"mt5_initialize_failed:{self.mt5.last_error()}")
        self._initialized = True


class Mt5MarketFeed(MarketFeed):
    def __init__(self, connection: Mt5Connection | None = None, timeframe: int | None = None) -> None:
        self.connection = connection or Mt5Connection()
        self.timeframe = timeframe

    def snapshot(self, symbol: str) -> MarketSnapshot:
        mt5 = self.connection.mt5
        self.connection.initialize()
        self._ensure_symbol(symbol)

        tick = mt5.symbol_info_tick(symbol)
        info = mt5.symbol_info(symbol)
        if tick is None or info is None:
            raise RuntimeError(f"mt5_tick_unavailable:{symbol}")

        point = float(info.point or self._fallback_point(symbol))
        bid = float(tick.bid)
        ask = float(tick.ask)
        spread_points = max(0.0, (ask - bid) / point)
        rates = self._rates(symbol)
        atr_points = self._atr_points(rates, point) if rates else max(spread_points * 4, 1)
        trend_score = self._trend_score(rates) if rates else 0.0
        volatility_score = self._volatility_score(atr_points, spread_points)

        return MarketSnapshot(
            symbol=symbol,
            bid=bid,
            ask=ask,
            spread_points=round(spread_points, 2),
            atr_points=round(atr_points, 2),
            trend_score=trend_score,
            volatility_score=volatility_score,
            session=self._session(datetime.now(timezone.utc)),
            news_risk="low",
            features={
                "source": "mt5",
                "point": point,
                "rates_loaded": len(rates),
                "last_tick_time": int(getattr(tick, "time", 0)),
            },
        )

    def _ensure_symbol(self, symbol: str) -> None:
        mt5 = self.connection.mt5
        info = mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"mt5_symbol_not_found:{symbol}")
        if not info.visible and not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"mt5_symbol_select_failed:{symbol}")

    def _rates(self, symbol: str) -> list[object]:
        mt5 = self.connection.mt5
        timeframe = self.timeframe or mt5.TIMEFRAME_M15
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, 64)
        if rates is None:
            return []
        return list(rates)

    def _atr_points(self, rates: list[object], point: float) -> float:
        true_ranges: list[float] = []
        previous_close: float | None = None
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
        if not true_ranges:
            return 0.0
        return fsum(true_ranges) / len(true_ranges) / point

    def _trend_score(self, rates: list[object]) -> float:
        if len(rates) < 20:
            return 0.0
        closes = [float(rate["close"]) for rate in rates]
        fast = fsum(closes[-8:]) / 8
        slow = fsum(closes[-21:]) / 21
        recent_range = max(closes[-21:]) - min(closes[-21:])
        if recent_range <= 0:
            return 0.0
        return round(max(-1.0, min(1.0, (fast - slow) / recent_range * 3)), 4)

    def _volatility_score(self, atr_points: float, spread_points: float) -> float:
        if atr_points <= 0:
            return 0.0
        spread_load = min(spread_points / max(atr_points, 1), 1.0)
        normalized_atr = min(atr_points / 500, 1.0)
        return round(max(0.0, min(1.0, normalized_atr * 0.8 + spread_load * 0.2)), 4)

    def _session(self, now: datetime) -> str:
        hour = now.hour
        if 7 <= hour < 12:
            return "london"
        if 12 <= hour < 16:
            return "london_new_york_overlap"
        if 16 <= hour < 21:
            return "new_york"
        if 0 <= hour < 7:
            return "asia"
        return "off_hours"

    def _fallback_point(self, symbol: str) -> float:
        if symbol.upper().startswith("XAU"):
            return 0.01
        if "JPY" in symbol.upper():
            return 0.001
        return 0.00001


class Mt5AccountFeed(AccountFeed):
    def __init__(self, connection: Mt5Connection | None = None) -> None:
        self.connection = connection or Mt5Connection()

    def state(self) -> AccountState:
        mt5 = self.connection.mt5
        self.connection.initialize()

        account = mt5.account_info()
        if account is None:
            raise RuntimeError(f"mt5_account_info_unavailable:{mt5.last_error()}")

        positions = mt5.positions_get()
        open_positions = len(positions or [])

        equity = float(account.equity)
        balance = float(account.balance)
        free_margin = float(account.margin_free)
        currency = str(getattr(account, "currency", "USD"))

        return AccountState(
            equity=equity,
            balance=balance,
            free_margin=free_margin,
            daily_realized_pnl=0.0,
            open_risk_percent=0.0,
            open_positions=open_positions,
            currency=currency,
        )
