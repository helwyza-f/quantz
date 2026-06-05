from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from quantz.broker import BrokerAdapter
from quantz.market import AccountFeed, MarketFeed
from quantz.models import AccountState, ExecutionResult, MarketSnapshot, OrderRequest


class BridgeClient:
    """HTTP client for a remote MT5 bridge running near the terminal."""

    def __init__(self, base_url: str, timeout_seconds: float = 10) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def get_json(self, path: str) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            with urlopen(url, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError) as exc:
            raise RuntimeError(f"bridge_get_failed:{url}:{exc}") from exc

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        body = json.dumps(payload).encode("utf-8")
        request = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError) as exc:
            raise RuntimeError(f"bridge_post_failed:{url}:{exc}") from exc


class BridgeMarketFeed(MarketFeed):
    def __init__(self, client: BridgeClient) -> None:
        self.client = client

    def snapshot(self, symbol: str) -> MarketSnapshot:
        payload = self.client.get_json(f"/market/{symbol}")
        return MarketSnapshot(
            symbol=str(payload["symbol"]),
            bid=float(payload["bid"]),
            ask=float(payload["ask"]),
            spread_points=float(payload["spread_points"]),
            atr_points=float(payload["atr_points"]),
            trend_score=float(payload["trend_score"]),
            volatility_score=float(payload["volatility_score"]),
            session=str(payload.get("session", "unknown")),
            news_risk=str(payload.get("news_risk", "low")),
            features=dict(payload.get("features", {})),
        )


class BridgeAccountFeed(AccountFeed):
    def __init__(self, client: BridgeClient) -> None:
        self.client = client

    def state(self) -> AccountState:
        payload = self.client.get_json("/account")
        return AccountState(
            equity=float(payload["equity"]),
            balance=float(payload["balance"]),
            free_margin=float(payload["free_margin"]),
            daily_realized_pnl=float(payload.get("daily_realized_pnl", 0.0)),
            open_risk_percent=float(payload.get("open_risk_percent", 0.0)),
            open_positions=int(payload.get("open_positions", 0)),
            currency=str(payload.get("currency", "USD")),
        )


class BridgeBrokerAdapter(BrokerAdapter):
    def __init__(self, client: BridgeClient) -> None:
        self.client = client

    def place_order(self, order: OrderRequest) -> ExecutionResult:
        payload = self.client.post_json("/orders", asdict(order))
        return ExecutionResult(
            accepted=bool(payload["accepted"]),
            broker_order_id=payload.get("broker_order_id"),
            message=str(payload["message"]),
            filled_price=float(payload["filled_price"]) if payload.get("filled_price") is not None else None,
            raw=dict(payload.get("raw", {})),
        )
