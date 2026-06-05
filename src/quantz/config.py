from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AgentSettings:
    symbols: list[str] = field(default_factory=lambda: ["XAUUSD"])
    memory_path: str = "data/experience.jsonl"
    paper_state_path: str = "data/paper-state.json"
    sim_state_path: str = "data/sim-market-state.json"
    mode: str = "paper"
    market_source: str = "demo"
    execution_source: str = "mt5"
    bridge_url: str = "http://127.0.0.1:8765"
    interval_seconds: float = 60
    default_risk_percent: float = 0.25
    paper_start_equity: float = 10_000
    min_confidence: float = 0.65
    planner_min_trend_score: float = 0.45
    planner_max_spread_points: float = 40
    analyst_max_spread_points: float = 60
    analyst_can_veto: bool = True
    risk_max_spread_points: float = 50
    risk_max_risk_per_trade_percent: float = 0.5
    risk_max_daily_loss_percent: float = 2.0
    risk_max_open_positions: int = 3
    risk_max_open_risk_percent: float = 1.5
    risk_min_free_margin_percent: float = 30
    risk_lot_step: float = 0.01
    risk_min_lot: float = 0.01
    risk_max_lot: float = 1.0
    risk_contract_size: float = 1_000
    allow_min_lot_when_below_minimum: bool = False
    min_sl_points: float = 120
    reward_risk_ratio: float = 1.7
    position_cooldown: str = "none"
    disabled_symbols: list[str] = field(default_factory=list)
    min_closed_trades_before_demo: int = 20
    analyst: str = "none"
    llm_model: str = "gpt-5.4-mini"
    llm_api_key_env: str = "OPENAI_API_KEY"
    llm_timeout_seconds: float = 12.0
    allow_live_execution: bool = False

    @property
    def constraints(self) -> dict[str, Any]:
        return {
            "default_risk_percent": self.default_risk_percent,
            "min_confidence": self.min_confidence,
            "planner_min_trend_score": self.planner_min_trend_score,
            "planner_max_spread_points": self.planner_max_spread_points,
            "analyst_max_spread_points": self.analyst_max_spread_points,
            "analyst_can_veto": self.analyst_can_veto,
            "min_sl_points": self.min_sl_points,
            "reward_risk_ratio": self.reward_risk_ratio,
        }


def load_settings(path: str | None = None) -> AgentSettings:
    if path is None:
        return AgentSettings()

    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    allowed = {field.name for field in AgentSettings.__dataclass_fields__.values()}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError(f"Unknown config keys: {', '.join(unknown)}")
    return AgentSettings(**payload)


def merge_settings(settings: AgentSettings, **overrides: Any) -> AgentSettings:
    payload = settings.__dict__.copy()
    for key, value in overrides.items():
        if value is not None:
            payload[key] = value
    return AgentSettings(**payload)


def settings_to_dict(settings: AgentSettings) -> dict[str, Any]:
    return asdict(settings)


def write_settings(settings: AgentSettings, path: str | Path) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(settings_to_dict(settings), handle, indent=2, sort_keys=True)
