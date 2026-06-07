from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from quantz.models import AgentContext


@dataclass(frozen=True)
class MemoryContext:
    symbol: str
    session: str
    market_regime: str
    sample_size: int
    recent_decisions: list[dict[str, Any]] = field(default_factory=list)
    rejection_focus: list[dict[str, Any]] = field(default_factory=list)
    best_reasons: list[dict[str, Any]] = field(default_factory=list)
    weak_reasons: list[dict[str, Any]] = field(default_factory=list)
    closed_trade_summary: dict[str, Any] = field(default_factory=dict)
    risk_posture: dict[str, Any] = field(default_factory=dict)
    lessons: list[str] = field(default_factory=list)
    semantic_recall: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MemoryContextBuilder:
    """Build compact decision memory for the next agent cycle."""

    def __init__(
        self,
        store: Any,
        recent_limit: int = 80,
        teacher_examples: list[dict[str, Any]] | None = None,
        vector_store: Any | None = None,
        max_context_items: int = 24,
    ) -> None:
        self.store = store
        self.recent_limit = recent_limit
        self.teacher_examples = teacher_examples or []
        self.vector_store = vector_store
        self.max_context_items = max(8, max_context_items)

    def build(self, context: AgentContext) -> dict[str, Any]:
        symbol = context.market.symbol
        summary = self._summary(symbol)
        recent_decisions = list(summary.get("recent_decisions", []))[-12:]
        reason_quality = dict(summary.get("reason_quality", {}))
        closed = dict(summary.get("closed_trade_summary", {}))
        memory_context = MemoryContext(
            symbol=symbol,
            session=context.market.session,
            market_regime=self._market_regime(context),
            sample_size=int(summary.get("sample_size", 0) or 0),
            recent_decisions=recent_decisions,
            rejection_focus=self._top_counts(summary.get("rejection_reasons", {}), limit=5),
            best_reasons=self._reason_rank(reason_quality, reverse=True),
            weak_reasons=self._reason_rank(reason_quality, reverse=False),
            closed_trade_summary=closed,
            risk_posture=self._risk_posture(context, recent_decisions, closed),
            lessons=self._lessons(summary, context),
            semantic_recall=self._semantic_recall(context),
        )
        payload = memory_context.to_dict()
        payload["teacher_examples"] = self._teacher_examples(symbol)
        return self._compact(payload)

    def _summary(self, symbol: str) -> dict[str, Any]:
        if hasattr(self.store, "context_summary"):
            return self.store.context_summary(symbol, limit=self.recent_limit)
        return {
            "symbol": symbol,
            "sample_size": 0,
            "recent_decisions": [],
            "rejection_reasons": {},
            "reason_quality": {},
            "closed_trade_summary": {},
        }

    def _market_regime(self, context: AgentContext) -> str:
        market = context.market
        if abs(market.trend_score) >= 0.65:
            return "trend"
        if market.volatility_score < 0.2:
            return "quiet"
        if market.volatility_score > 0.85:
            return "volatile"
        if abs(market.trend_score) < 0.25:
            return "range"
        return "mixed"

    def _risk_posture(
        self,
        context: AgentContext,
        recent_decisions: list[dict[str, Any]],
        closed_trade_summary: dict[str, Any],
    ) -> dict[str, Any]:
        account = context.account
        loss_streak = 0
        for decision in reversed(recent_decisions):
            if decision.get("risk_status") == "rejected":
                continue
            reasons = decision.get("reason_codes", [])
            if "stop_loss" in reasons:
                loss_streak += 1
            else:
                break
        daily_loss_percent = abs(min(account.daily_realized_pnl, 0.0)) / account.equity * 100 if account.equity else 0.0
        free_margin_percent = account.free_margin / account.equity * 100 if account.equity else 0.0
        return {
            "account_equity": account.equity,
            "open_positions": account.open_positions,
            "open_risk_percent": account.open_risk_percent,
            "free_margin_percent": round(free_margin_percent, 4),
            "daily_loss_percent": round(daily_loss_percent, 4),
            "recent_loss_streak": loss_streak,
            "closed_trades": int(closed_trade_summary.get("count", 0) or 0),
            "average_r": float(closed_trade_summary.get("average_r", 0.0) or 0.0),
        }

    def _lessons(self, summary: dict[str, Any], context: AgentContext) -> list[str]:
        lessons: list[str] = []
        closed = summary.get("closed_trade_summary", {})
        average_r = float(closed.get("average_r", 0.0) or 0.0)
        count = int(closed.get("count", 0) or 0)
        if count == 0:
            lessons.append("No closed trades for this symbol yet; treat memory confidence as low.")
        elif count < 20:
            lessons.append("Closed-trade sample is still small; avoid overfitting recent outcomes.")
        elif average_r < 0:
            lessons.append("Recent closed-trade expectancy is negative; require stronger confirmation.")
        elif average_r > 0.25:
            lessons.append("Recent closed-trade expectancy is positive; keep risk discipline unchanged.")

        rejection_reasons = summary.get("rejection_reasons", {})
        if rejection_reasons.get("spread_above_limit", 0) > 0 and context.market.spread_points > 0:
            lessons.append("Spread has caused recent rejections; prefer cleaner execution conditions.")
        if rejection_reasons.get("symbol_already_has_open_paper_position", 0) > 0:
            lessons.append("Duplicate scans occurred while a position was open; respect cooldown/open-position state.")
        for example in self._teacher_examples(context.market.symbol)[:3]:
            lesson = example.get("lesson")
            if lesson:
                lessons.append(f"Teacher example: {lesson}")
        return lessons[:6]

    def _teacher_examples(self, symbol: str) -> list[dict[str, Any]]:
        return [
            example
            for example in self.teacher_examples
            if str(example.get("symbol", "")).upper() == symbol.upper()
        ][:6]

    def _semantic_recall(self, context: AgentContext) -> list[dict[str, Any]]:
        if not self.vector_store or not hasattr(self.vector_store, "search"):
            return []
        market = context.market
        query = " ".join(
            [
                market.symbol,
                market.session,
                self._market_regime(context),
                f"trend={market.trend_score:.2f}",
                f"volatility={market.volatility_score:.2f}",
                f"spread={market.spread_points:.1f}",
                f"news={market.news_risk}",
            ]
        )
        try:
            matches = self.vector_store.search(query, limit=6, filters={"symbol": market.symbol})
        except Exception:
            return []
        recalled: list[dict[str, Any]] = []
        for match in matches:
            payload = dict(match.get("payload", {}) or {})
            recalled.append(
                {
                    "score": round(float(match.get("score", 0.0) or 0.0), 4),
                    "kind": payload.get("kind", "memory"),
                    "text": str(payload.get("text", ""))[:700],
                    "metadata": {
                        key: value
                        for key, value in payload.items()
                        if key not in {"text"} and key in {"symbol", "stage", "source", "decision_id", "outcome"}
                    },
                }
            )
        return recalled

    def _compact(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Keep decision context bounded before it is sent to planners/LLMs."""
        budget = self.max_context_items
        payload["recent_decisions"] = list(payload.get("recent_decisions", []))[-min(8, budget):]
        payload["rejection_focus"] = list(payload.get("rejection_focus", []))[:5]
        payload["best_reasons"] = list(payload.get("best_reasons", []))[:4]
        payload["weak_reasons"] = list(payload.get("weak_reasons", []))[:4]
        payload["lessons"] = list(payload.get("lessons", []))[:6]
        payload["teacher_examples"] = list(payload.get("teacher_examples", []))[:4]
        payload["semantic_recall"] = list(payload.get("semantic_recall", []))[:4]
        return payload

    def _top_counts(self, counts: dict[str, int], limit: int) -> list[dict[str, Any]]:
        return [
            {"reason": reason, "count": count}
            for reason, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)[:limit]
        ]

    def _reason_rank(self, reason_quality: dict[str, dict[str, Any]], reverse: bool) -> list[dict[str, Any]]:
        ranked = sorted(
            reason_quality.items(),
            key=lambda item: (float(item[1].get("average_r", 0.0) or 0.0), int(item[1].get("count", 0) or 0)),
            reverse=reverse,
        )
        output = []
        for reason, stats in ranked[:5]:
            count = int(stats.get("count", 0) or 0)
            if count <= 0:
                continue
            output.append(
                {
                    "reason": reason,
                    "count": count,
                    "average_r": float(stats.get("average_r", 0.0) or 0.0),
                    "total_r": float(stats.get("total_r", 0.0) or 0.0),
                }
            )
        return output
