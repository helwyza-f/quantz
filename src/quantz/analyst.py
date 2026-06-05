from __future__ import annotations

import json
import os
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Callable
from urllib.error import HTTPError, URLError

from quantz.models import AgentContext, AnalystOutput


class Analyst(ABC):
    @abstractmethod
    def analyze(self, context: AgentContext) -> AnalystOutput:
        """Return structured reasoning that can influence, not bypass, planning and risk."""


class NoOpAnalyst(Analyst):
    def analyze(self, context: AgentContext) -> AnalystOutput:
        return AnalystOutput(
            market_regime="unknown",
            bias="neutral",
            reason_codes=["analyst_disabled"],
            model_version="noop_analyst_v1",
        )


class RuleBasedAnalyst(Analyst):
    model_version = "rule_analyst_v1"

    def analyze(self, context: AgentContext) -> AnalystOutput:
        market = context.market
        reason_codes: list[str] = []
        risk_notes: list[str] = []

        if abs(market.trend_score) >= 0.65:
            market_regime = "trend"
            reason_codes.append("strong_trend")
        elif market.volatility_score < 0.2:
            market_regime = "quiet"
            reason_codes.append("low_volatility")
        elif market.volatility_score > 0.85:
            market_regime = "volatile"
            reason_codes.append("high_volatility")
        else:
            market_regime = "mixed"
            reason_codes.append("mixed_regime")

        if market.trend_score >= 0.45:
            bias = "buy"
        elif market.trend_score <= -0.45:
            bias = "sell"
        else:
            bias = "neutral"

        avoid_trade = False
        confidence_adjustment = 0.0

        if market.spread_points > float(context.constraints.get("analyst_max_spread_points", 60)):
            avoid_trade = True
            risk_notes.append("spread_too_wide_for_analyst")
        if market.news_risk == "high":
            avoid_trade = True
            risk_notes.append("high_news_risk")
        if market_regime == "volatile":
            confidence_adjustment -= 0.06
            risk_notes.append("volatile_regime_reduce_confidence")
        if market_regime == "trend" and market.news_risk == "low":
            confidence_adjustment += 0.03
            reason_codes.append("trend_low_news_support")

        return AnalystOutput(
            market_regime=market_regime,
            bias=bias,
            confidence_adjustment=round(confidence_adjustment, 4),
            avoid_trade=avoid_trade,
            reason_codes=reason_codes,
            risk_notes=risk_notes,
            model_version=self.model_version,
        )


class LLMAnalyst(Analyst):
    """Structured LLM reasoning layer.

    The LLM can influence bias and confidence, but cannot place orders. Planner
    and RiskGovernor still make the final executable decision.
    """

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "market_regime": {"type": "string", "enum": ["trend", "range", "quiet", "volatile", "mixed", "unknown"]},
            "bias": {"type": "string", "enum": ["buy", "sell", "neutral"]},
            "confidence_adjustment": {"type": "number", "minimum": -0.2, "maximum": 0.2},
            "avoid_trade": {"type": "boolean"},
            "reason_codes": {
                "type": "array",
                "items": {"type": "string", "maxLength": 64},
                "maxItems": 8,
            },
            "risk_notes": {
                "type": "array",
                "items": {"type": "string", "maxLength": 96},
                "maxItems": 8,
            },
        },
        "required": ["market_regime", "bias", "confidence_adjustment", "avoid_trade", "reason_codes", "risk_notes"],
    }

    def __init__(
        self,
        model: str = "gpt-5.4-mini",
        api_key_env: str = "OPENAI_API_KEY",
        timeout_seconds: float = 12.0,
        request_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.model = model
        self.api_key_env = api_key_env
        self.timeout_seconds = timeout_seconds
        self.uses_openai = request_fn is None
        self.request_fn = request_fn or self._request_openai_response

    @property
    def model_version(self) -> str:
        return f"llm_analyst:{self.model}"

    def analyze(self, context: AgentContext) -> AnalystOutput:
        api_key = os.getenv(self.api_key_env, "")
        if not api_key and self.uses_openai:
            return self._fail_closed("llm_api_key_missing")
        try:
            payload = self.request_fn(self._request_payload(context))
            data = self._extract_json(payload)
            return AnalystOutput(
                market_regime=str(data.get("market_regime", "unknown")),
                bias=str(data.get("bias", "neutral")),
                confidence_adjustment=round(float(data.get("confidence_adjustment", 0.0) or 0.0), 4),
                avoid_trade=bool(data.get("avoid_trade", True)),
                reason_codes=[str(item) for item in data.get("reason_codes", [])][:8],
                risk_notes=[str(item) for item in data.get("risk_notes", [])][:8],
                model_version=self.model_version,
            )
        except Exception as exc:
            return self._fail_closed(self._error_reason(exc))

    def _request_payload(self, context: AgentContext) -> dict[str, Any]:
        market = context.market
        account = context.account
        return {
            "model": self.model,
            "instructions": (
                "You are Quantz analyst. Read the structured market, account, and risk context. "
                "Return only the schema fields. Do not place trades. If market quality, spread, "
                "position state, or data quality is unsafe, set avoid_trade true."
            ),
            "input": json.dumps(
                {
                    "market": {
                        "symbol": market.symbol,
                        "bid": market.bid,
                        "ask": market.ask,
                        "mid": market.mid,
                        "spread_points": market.spread_points,
                        "atr_points": market.atr_points,
                        "trend_score": market.trend_score,
                        "volatility_score": market.volatility_score,
                        "session": market.session,
                        "news_risk": market.news_risk,
                        "timestamp": market.timestamp.isoformat(),
                        "features": market.features,
                    },
                    "account": {
                        "equity": account.equity,
                        "balance": account.balance,
                        "free_margin": account.free_margin,
                        "daily_realized_pnl": account.daily_realized_pnl,
                        "open_risk_percent": account.open_risk_percent,
                        "open_positions": account.open_positions,
                        "currency": account.currency,
                    },
                    "constraints": {
                        key: value
                        for key, value in context.constraints.items()
                        if key not in {"analyst"} and isinstance(value, (str, int, float, bool, list, dict, type(None)))
                    },
                },
                default=str,
                sort_keys=True,
            ),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "quantz_llm_analyst",
                    "strict": True,
                    "schema": self.schema,
                }
            },
            "max_output_tokens": 600,
        }

    def _request_openai_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        api_key = os.getenv(self.api_key_env, "")
        if not api_key:
            raise RuntimeError("missing_api_key")
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    def _error_reason(self, exc: Exception) -> str:
        if isinstance(exc, HTTPError):
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            message = self._http_error_message(body)
            suffix = f":{message}" if message else ""
            return f"llm_http_error:{exc.code}{suffix}"
        if isinstance(exc, URLError):
            return f"llm_url_error:{exc.reason}"
        return f"llm_error:{type(exc).__name__}"

    def _http_error_message(self, body: str) -> str:
        if not body:
            return ""
        try:
            payload = json.loads(body)
            error = payload.get("error", {})
            message = str(error.get("message", "") or error.get("code", ""))
        except Exception:
            message = body
        return message.replace("\n", " ").replace("\r", " ")[:220]

    def _extract_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        if "output_text" in payload:
            return json.loads(str(payload["output_text"]))
        for item in payload.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return json.loads(str(content.get("text", "{}")))
        raise ValueError("llm_response_missing_output_text")

    def _fail_closed(self, reason: str) -> AnalystOutput:
        return AnalystOutput(
            market_regime="unknown",
            bias="neutral",
            confidence_adjustment=-0.2,
            avoid_trade=True,
            reason_codes=["llm_analyst_unavailable"],
            risk_notes=[reason],
            model_version=self.model_version,
        )
