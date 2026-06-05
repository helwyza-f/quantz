from __future__ import annotations

from dataclasses import replace

from quantz.config import AgentSettings
from quantz.review import AgentReview


class CandidateConfigBuilder:
    """Applies review recommendations to a paper-only candidate config."""

    def build(self, base: AgentSettings, review: AgentReview) -> AgentSettings:
        settings = replace(base, mode="paper")

        disabled = set(settings.disabled_symbols)
        min_closed_trades = settings.min_closed_trades_before_demo
        position_cooldown = settings.position_cooldown
        min_confidence = settings.min_confidence

        for recommendation in review.recommendations:
            if recommendation.type in {
                "reduce_duplicate_scans",
                "increase_monitor_interval_or_add_position_cooldown",
            }:
                position_cooldown = "until_closed"
            elif recommendation.type == "disable_symbol_candidate":
                disabled.add(recommendation.target)
            elif recommendation.type == "collect_more_paper_data":
                min_closed_trades = int(
                    recommendation.change.get("min_closed_trades_before_demo", min_closed_trades)
                )
            elif recommendation.type == "raise_confidence_threshold":
                min_confidence = round(
                    min(0.95, min_confidence + float(recommendation.change.get("min_confidence_delta", 0.05))),
                    2,
                )

        enabled_symbols = [symbol for symbol in settings.symbols if symbol not in disabled]
        if not enabled_symbols:
            enabled_symbols = settings.symbols
            disabled.clear()

        return replace(
            settings,
            symbols=enabled_symbols,
            disabled_symbols=sorted(disabled),
            position_cooldown=position_cooldown,
            min_closed_trades_before_demo=min_closed_trades,
            min_confidence=min_confidence,
        )
