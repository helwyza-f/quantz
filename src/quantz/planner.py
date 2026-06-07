from __future__ import annotations

import json
import os
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from quantz.audit import DecisionAuditStore, stable_hash
from quantz.models import AgentContext, AnalystOutput, OrderSide, TradeAction, TradeDecision


class AgentPlanner(ABC):
    @abstractmethod
    def decide(self, context: AgentContext) -> TradeDecision:
        """Return a structured trade decision from the current world state."""


class VariableDrivenPlanner(AgentPlanner):
    """Baseline planner that mimics the agent contract without hidden execution side effects."""

    model_version = "variable_planner_v1"

    def decide(self, context: AgentContext) -> TradeDecision:
        market = context.market
        constraints = context.constraints
        analyst = self._analyst_output(constraints)
        memory_context = constraints.get("memory_context", {})
        playbook_context = constraints.get("playbook", {})
        playbook_selected = playbook_context.get("selected") if isinstance(playbook_context, dict) else None

        min_confidence = float(constraints.get("min_confidence", 0.65))
        if isinstance(memory_context, dict):
            closed = memory_context.get("closed_trade_summary", {})
            if int(closed.get("count", 0) or 0) >= 20 and float(closed.get("average_r", 0.0) or 0.0) < 0:
                min_confidence = min(0.95, min_confidence + 0.05)
        max_news_risk = str(constraints.get("max_news_risk", "medium"))
        news_blocked = max_news_risk == "low" and market.news_risk != "low"
        playbook_blocked = bool(
            isinstance(playbook_context, dict)
            and playbook_context.get("status") == "blocked"
        )

        direction: OrderSide | None = None
        reason_codes: list[str] = []

        min_trend_score = float(constraints.get("planner_min_trend_score", 0.45))
        if market.trend_score >= min_trend_score:
            direction = OrderSide.BUY
            reason_codes.append("bullish_market_structure")
        elif market.trend_score <= -min_trend_score:
            direction = OrderSide.SELL
            reason_codes.append("bearish_market_structure")

        volatility_ok = 0.15 <= market.volatility_score <= 0.85
        if volatility_ok:
            reason_codes.append("volatility_in_range")
        else:
            reason_codes.append("volatility_out_of_range")

        if market.spread_points <= float(constraints.get("planner_max_spread_points", 40)):
            reason_codes.append("spread_acceptable")
        else:
            reason_codes.append("spread_too_wide")

        if news_blocked:
            reason_codes.append("news_risk_blocked")
        if isinstance(playbook_context, dict):
            reason_codes.extend(str(reason) for reason in playbook_context.get("reasons", []))
        if analyst:
            reason_codes.extend(analyst.reason_codes)

        confidence = self._confidence(market.trend_score, market.volatility_score, market.spread_points)
        if isinstance(memory_context, dict):
            weak_reasons = {
                str(item.get("reason"))
                for item in memory_context.get("weak_reasons", [])
                if float(item.get("average_r", 0.0) or 0.0) < 0
            }
            if weak_reasons.intersection(reason_codes):
                confidence = round(max(0.0, confidence - 0.04), 4)
                reason_codes.append("memory_weak_reason_penalty")
        if analyst:
            confidence = round(max(0.0, min(1.0, confidence + analyst.confidence_adjustment)), 4)
        analyst_blocked = bool(
            analyst
            and analyst.avoid_trade
            and bool(constraints.get("analyst_can_veto", True))
        )
        if analyst and analyst.avoid_trade and not analyst_blocked:
            reason_codes.append("analyst_avoid_recorded_without_veto")
            reason_codes.extend(analyst.risk_notes)
        elif analyst_blocked:
            reason_codes.extend(analyst.risk_notes)

        if direction is None or confidence < min_confidence or not volatility_ok or news_blocked or analyst_blocked or playbook_blocked:
            return TradeDecision(
                action=TradeAction.HOLD,
                symbol=market.symbol,
                side=None,
                confidence=confidence,
                entry_price=None,
                stop_loss=None,
                take_profit=None,
                risk_percent=0.0,
                model_version=self.model_version,
                reason_codes=reason_codes or ["no_trade_edge"],
                metadata=self._metadata(analyst, playbook_context),
            )

        playbook_risk = playbook_selected.get("risk", {}) if isinstance(playbook_selected, dict) else {}
        sl_atr = float(playbook_risk.get("sl_atr", 1.5))
        reward_risk_ratio = float(playbook_risk.get("tp_r", constraints.get("reward_risk_ratio", 1.7)))
        sl_distance = max(market.atr_points * sl_atr, float(constraints.get("min_sl_points", 120)))
        tp_distance = sl_distance * reward_risk_ratio
        entry = market.ask if direction == OrderSide.BUY else market.bid

        if direction == OrderSide.BUY:
            stop_loss = entry - sl_distance * self._point_size(market.symbol)
            take_profit = entry + tp_distance * self._point_size(market.symbol)
        else:
            stop_loss = entry + sl_distance * self._point_size(market.symbol)
            take_profit = entry - tp_distance * self._point_size(market.symbol)

        return TradeDecision(
            action=TradeAction.OPEN_POSITION,
            symbol=market.symbol,
            side=direction,
            confidence=confidence,
            entry_price=entry,
            stop_loss=round(stop_loss, 5),
            take_profit=round(take_profit, 5),
            risk_percent=float(playbook_risk.get("risk_percent", constraints.get("default_risk_percent", 0.25))),
            model_version=self.model_version,
            reason_codes=reason_codes,
            metadata=self._metadata(analyst, playbook_context),
        )

    def _confidence(self, trend_score: float, volatility_score: float, spread_points: float) -> float:
        trend_component = min(abs(trend_score), 1.0) * 0.65
        volatility_component = (1.0 - abs(volatility_score - 0.5)) * 0.25
        spread_penalty = min(spread_points / 200, 0.25)
        return round(max(0.0, min(1.0, trend_component + volatility_component + 0.2 - spread_penalty)), 4)

    def _point_size(self, symbol: str) -> float:
        if symbol.upper().startswith("XAU"):
            return 0.01
        if "JPY" in symbol.upper():
            return 0.001
        return 0.00001

    def _analyst_output(self, constraints: dict) -> AnalystOutput | None:
        value = constraints.get("analyst")
        return value if isinstance(value, AnalystOutput) else None

    def _metadata(self, analyst: AnalystOutput | None, playbook_context: object) -> dict:
        metadata = {}
        if analyst:
            metadata["analyst"] = analyst
        if isinstance(playbook_context, dict):
            metadata["playbook"] = playbook_context
        return metadata


@dataclass(frozen=True)
class DecisionPolicy:
    """Hard planner-side guardrails before the RiskGovernor runs."""

    allow_open_position: bool = True
    allow_close_position: bool = False
    min_confidence: float = 0.65
    max_risk_percent: float = 0.5
    max_spread_points: float = 50
    require_memory_for_live: bool = True
    min_live_memory_samples: int = 20
    fail_closed_on_ai_error: bool = True

    def apply(self, context: AgentContext, decision: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        reasons: list[str] = []
        action = str(decision.get("action", "hold"))
        confidence = self._float(decision.get("confidence"), 0.0)
        risk_percent = self._float(decision.get("risk_percent"), 0.0)

        if action == TradeAction.OPEN_POSITION.value and not self.allow_open_position:
            reasons.append("policy_open_position_disabled")
        if action == TradeAction.CLOSE_POSITION.value and not self.allow_close_position:
            reasons.append("policy_close_position_disabled")
        if context.market.spread_points > self.max_spread_points:
            reasons.append("policy_spread_above_limit")
        if confidence < self.min_confidence:
            reasons.append("policy_confidence_below_minimum")
        if risk_percent <= 0 or risk_percent > self.max_risk_percent:
            reasons.append("policy_risk_percent_out_of_bounds")
        if str(context.constraints.get("mode", "")).lower() == "live" and self.require_memory_for_live:
            memory = context.constraints.get("memory_context", {})
            sample_size = int(memory.get("sample_size", 0) or 0) if isinstance(memory, dict) else 0
            if sample_size < self.min_live_memory_samples:
                reasons.append("policy_live_memory_sample_too_small")

        if reasons:
            held = dict(decision)
            held["action"] = TradeAction.HOLD.value
            held["side"] = None
            held["entry_price"] = None
            held["stop_loss"] = None
            held["take_profit"] = None
            held["risk_percent"] = 0.0
            held["reason_codes"] = list(held.get("reason_codes", [])) + reasons
            return held, reasons
        return decision, reasons

    def _float(self, value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default


class AIDecisionPlanner(AgentPlanner):
    """LLM-driven decision maker with deterministic fallback, policy guard, and audit."""

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["hold", "open_position"]},
            "side": {"type": ["string", "null"], "enum": ["buy", "sell", None]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "entry_price": {"type": ["number", "null"]},
            "stop_loss": {"type": ["number", "null"]},
            "take_profit": {"type": ["number", "null"]},
            "risk_percent": {"type": "number", "minimum": 0, "maximum": 5},
            "reason_codes": {
                "type": "array",
                "items": {"type": "string", "maxLength": 64},
                "maxItems": 12,
            },
            "decision_brief": {"type": "string", "maxLength": 900},
            "memory_used": {
                "type": "array",
                "items": {"type": "string", "maxLength": 180},
                "maxItems": 8,
            },
            "risk_notes": {
                "type": "array",
                "items": {"type": "string", "maxLength": 180},
                "maxItems": 8,
            },
        },
        "required": [
            "action",
            "side",
            "confidence",
            "entry_price",
            "stop_loss",
            "take_profit",
            "risk_percent",
            "reason_codes",
            "decision_brief",
            "memory_used",
            "risk_notes",
        ],
    }

    def __init__(
        self,
        model: str = "gpt-5.4-mini",
        api_key_env: str = "OPENAI_API_KEY",
        timeout_seconds: float = 12.0,
        request_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        fallback: AgentPlanner | None = None,
        policy: DecisionPolicy | None = None,
        audit_store: DecisionAuditStore | None = None,
    ) -> None:
        self.model = model
        self.api_key_env = api_key_env
        self.timeout_seconds = timeout_seconds
        self.uses_openai = request_fn is None
        self.request_fn = request_fn or self._request_openai_response
        self.fallback = fallback or VariableDrivenPlanner()
        self.policy = policy or DecisionPolicy()
        self.audit_store = audit_store

    @property
    def model_version(self) -> str:
        return f"ai_decision_planner:{self.model}"

    def decide(self, context: AgentContext) -> TradeDecision:
        request_payload = self._request_payload(context)
        error: str | None = None
        raw_response: dict[str, Any] | None = None
        parsed: dict[str, Any] | None = None
        source = "ai"

        if not os.getenv(self.api_key_env, "") and self.uses_openai:
            error = "ai_api_key_missing"
        else:
            try:
                raw_response = self.request_fn(request_payload)
                parsed = self._extract_json(raw_response)
            except Exception as exc:
                error = f"ai_error:{type(exc).__name__}"

        if parsed is None:
            if self.policy.fail_closed_on_ai_error:
                parsed = self._hold_payload(context, [error or "ai_unavailable"])
                source = "fail_closed"
            else:
                fallback_decision = self.fallback.decide(context)
                parsed = self._payload_from_decision(fallback_decision, ["ai_fallback_used", error or "ai_unavailable"])
                source = "fallback"

        parsed = self._normalize_payload(context, parsed)
        guarded, policy_reasons = self.policy.apply(context, parsed)
        decision = self._decision_from_payload(context, guarded)
        audit = self._audit_event(context, request_payload, parsed, guarded, raw_response, error, policy_reasons, source)
        if audit:
            decision = TradeDecision(
                action=decision.action,
                symbol=decision.symbol,
                side=decision.side,
                confidence=decision.confidence,
                entry_price=decision.entry_price,
                stop_loss=decision.stop_loss,
                take_profit=decision.take_profit,
                risk_percent=decision.risk_percent,
                model_version=decision.model_version,
                reason_codes=decision.reason_codes,
                metadata={**decision.metadata, "decision_audit": audit},
            )
        return decision

    def _request_payload(self, context: AgentContext) -> dict[str, Any]:
        market = context.market
        account = context.account
        payload = {
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
            "account": asdict(account),
            "constraints": {
                key: value
                for key, value in context.constraints.items()
                if key not in {"analyst"} and isinstance(value, (str, int, float, bool, list, dict, type(None)))
            },
            "analyst": self._analyst_payload(context.constraints.get("analyst")),
            "memory_context": context.constraints.get("memory_context", {}),
            "playbook": context.constraints.get("playbook", {}),
            "policy": asdict(self.policy),
        }
        return {
            "model": self.model,
            "instructions": (
                "You are Quantz autonomous trading decision brain. Make one executable decision from the structured "
                "market/account/memory/playbook context. Return strict JSON only. Do not reveal hidden chain-of-thought. "
                "Prefer hold when data quality, memory quality, spread, or risk is not clean. RiskGovernor will still "
                "be the final hard gate, so include entry, stop_loss, take_profit, and risk_percent only for open_position."
            ),
            "input": json.dumps(payload, default=str, sort_keys=True),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "quantz_ai_decision",
                    "strict": True,
                    "schema": self.schema,
                }
            },
            "max_output_tokens": 1400,
        }

    def _request_openai_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        api_key = os.getenv(self.api_key_env, "")
        if not api_key:
            raise RuntimeError("missing_api_key")
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    def _extract_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        if "output_text" in payload:
            return json.loads(str(payload["output_text"]))
        for item in payload.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return json.loads(str(content.get("text", "{}")))
        raise ValueError("ai_response_missing_output_text")

    def _normalize_payload(self, context: AgentContext, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action", "hold"))
        if action not in {TradeAction.HOLD.value, TradeAction.OPEN_POSITION.value}:
            action = TradeAction.HOLD.value
        side = payload.get("side")
        if side not in {OrderSide.BUY.value, OrderSide.SELL.value}:
            side = None
        if action == TradeAction.OPEN_POSITION.value and side is None:
            action = TradeAction.HOLD.value
        return {
            "action": action,
            "side": side,
            "confidence": round(max(0.0, min(1.0, self._float(payload.get("confidence"), 0.0))), 4),
            "entry_price": self._optional_float(payload.get("entry_price")),
            "stop_loss": self._optional_float(payload.get("stop_loss")),
            "take_profit": self._optional_float(payload.get("take_profit")),
            "risk_percent": round(max(0.0, self._float(payload.get("risk_percent"), 0.0)), 4),
            "reason_codes": [str(item) for item in payload.get("reason_codes", [])][:12] or ["ai_no_reason"],
            "decision_brief": str(payload.get("decision_brief", ""))[:900],
            "memory_used": [str(item) for item in payload.get("memory_used", [])][:8],
            "risk_notes": [str(item) for item in payload.get("risk_notes", [])][:8],
            "symbol": str(payload.get("symbol") or context.market.symbol),
        }

    def _decision_from_payload(self, context: AgentContext, payload: dict[str, Any]) -> TradeDecision:
        action = TradeAction(payload["action"])
        side = OrderSide(payload["side"]) if payload.get("side") else None
        return TradeDecision(
            action=action,
            symbol=context.market.symbol,
            side=side,
            confidence=float(payload["confidence"]),
            entry_price=payload.get("entry_price") if action == TradeAction.OPEN_POSITION else None,
            stop_loss=payload.get("stop_loss") if action == TradeAction.OPEN_POSITION else None,
            take_profit=payload.get("take_profit") if action == TradeAction.OPEN_POSITION else None,
            risk_percent=float(payload.get("risk_percent", 0.0)) if action == TradeAction.OPEN_POSITION else 0.0,
            model_version=self.model_version,
            reason_codes=list(payload.get("reason_codes", [])),
            metadata={
                "ai_decision": {
                    "decision_brief": payload.get("decision_brief", ""),
                    "memory_used": payload.get("memory_used", []),
                    "risk_notes": payload.get("risk_notes", []),
                }
            },
        )

    def _audit_event(
        self,
        context: AgentContext,
        request_payload: dict[str, Any],
        parsed: dict[str, Any],
        guarded: dict[str, Any],
        raw_response: dict[str, Any] | None,
        error: str | None,
        policy_reasons: list[str],
        source: str,
    ) -> dict[str, Any]:
        request_input = self._decode_input(request_payload.get("input", ""))
        event = {
            "kind": "ai_decision",
            "source": source,
            "model": self.model,
            "symbol": context.market.symbol,
            "context_hash": stable_hash(request_input),
            "request": {
                "instructions": request_payload.get("instructions", ""),
                "input": request_input,
                "schema": request_payload.get("text", {}).get("format", {}).get("name", ""),
            },
            "response": {
                "parsed": parsed,
                "raw_preview": json.dumps(raw_response, default=str, sort_keys=True)[:2200] if raw_response else "",
                "error": error,
            },
            "policy": {
                "reasons": policy_reasons,
                "final_payload": guarded,
            },
        }
        if self.audit_store:
            return self.audit_store.append(event)
        return event

    def _hold_payload(self, context: AgentContext, reasons: list[str]) -> dict[str, Any]:
        return {
            "action": TradeAction.HOLD.value,
            "side": None,
            "confidence": 0.0,
            "entry_price": None,
            "stop_loss": None,
            "take_profit": None,
            "risk_percent": 0.0,
            "reason_codes": ["ai_hold"] + reasons,
            "decision_brief": "AI decision brain unavailable or blocked; hold by policy.",
            "memory_used": [],
            "risk_notes": reasons,
            "symbol": context.market.symbol,
        }

    def _payload_from_decision(self, decision: TradeDecision, reasons: list[str]) -> dict[str, Any]:
        return {
            "action": decision.action.value,
            "side": decision.side.value if decision.side else None,
            "confidence": decision.confidence,
            "entry_price": decision.entry_price,
            "stop_loss": decision.stop_loss,
            "take_profit": decision.take_profit,
            "risk_percent": decision.risk_percent,
            "reason_codes": list(decision.reason_codes) + reasons,
            "decision_brief": "Fallback planner decision used because AI planner was unavailable.",
            "memory_used": [],
            "risk_notes": reasons,
            "symbol": decision.symbol,
        }

    def _analyst_payload(self, value: Any) -> Any:
        if isinstance(value, AnalystOutput):
            return asdict(value)
        return value

    def _decode_input(self, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    def _float(self, value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _optional_float(self, value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
