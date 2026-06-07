from __future__ import annotations

import json
import threading
import time
import urllib.request
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from quantz.audit import DecisionAuditStore
from quantz.bridge import BridgeClient
from quantz.config import AgentSettings, load_settings, merge_settings
from quantz.market import (
    DemoAccountFeed,
    DemoMarketFeed,
    Mt5AccountFeed,
    Mt5Connection,
    Mt5MarketFeed,
    SimulatedMarketFeed,
)
from quantz.models import AccountState, AgentContext, MarketSnapshot
from quantz.paper import PaperPortfolio
from quantz.services import AgentRuntimeService

LOCAL_TZ = timezone.utc


class AutonomousRuntime:
    """New backend runtime for the AI-first agent.

    It intentionally does not depend on the retired HTML runtime. It owns:
    live tick ingestion, autonomous monitor sessions, decision summaries,
    vector/audit aware agent construction, and dashboard read models.
    """

    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root).resolve()
        self.configs_dir = self.root / "configs"
        self.tick_tape_path = self.root / "data" / "mt5-ticks.jsonl"
        self.event_condition = threading.Condition()
        self.event_sequence = 0
        self.tick_lock = threading.Lock()
        self.ticks: list[dict[str, Any]] = []
        self.latest_tick: dict[str, Any] | None = None
        self.monitor_lock = threading.Lock()
        self.monitor_stop_event: threading.Event | None = None
        self.monitor_thread: threading.Thread | None = None
        self.monitor_state = {
            "running": False,
            "status": "stopped",
            "config": None,
            "max_iterations": 0,
            "trigger_mode": "interval",
            "started_at": None,
            "stopped_at": None,
            "error": None,
        }
        self.monitor_events: list[dict[str, Any]] = []
        self._load_recent_ticks()

    def config_names(self) -> list[str]:
        if not self.configs_dir.exists():
            return []
        return sorted(path.name for path in self.configs_dir.glob("*.json"))

    def agent_config_names(self) -> list[str]:
        names = []
        for name in self.config_names():
            try:
                load_settings(str(self.configs_dir / name))
            except Exception:
                continue
            names.append(name)
        return names

    def control_summary(self, include_chart: bool = True) -> dict[str, Any]:
        latest_settings = self._summary_settings()
        payload = {
            "generated_at": self._now(),
            "sequence": self.event_sequence,
            "stream": {"transport": "sse", "event_source": "/events", "browser_polling": False},
            "live": self.live_summary(),
            "ticks": self.ticks[-50:],
            "agent": self.agent_summary(latest_settings),
            "operations": self.operations_summary(latest_settings),
            "runtime": self.runtime_status(),
        }
        if include_chart:
            payload["chart"] = self.market_chart_summary()
        return payload

    def settings_agent_config(self, config_name: str | None) -> AgentSettings:
        name = config_name or "mt5-paper.json"
        path = self.configs_dir / name
        if not path.exists():
            path = self.configs_dir / "mt5-paper.json"
        return self._resolve_settings_paths(load_settings(str(path)))

    def live_summary(self) -> dict[str, Any]:
        latest = self.latest_tick or {}
        count = len(self.ticks)
        age = 0.0
        if latest.get("captured_at"):
            try:
                captured = datetime.fromisoformat(str(latest["captured_at"]))
                age = max(0.0, (datetime.now(timezone.utc) - captured.astimezone(timezone.utc)).total_seconds())
            except ValueError:
                age = 0.0
        spreads = [self._float(tick.get("spread_points"), 0.0) for tick in self.ticks[-100:]]
        return {
            "collector": {
                "running": True,
                "polling_active": False,
                "polling_suppressed_by_ea": bool(latest),
                "active_source": latest.get("source", "none"),
                "active_source_label": latest.get("source_label", "No tick"),
                "last_ea_tick_at": latest.get("captured_at"),
                "thread": "autonomous-runtime",
                "tape_path": str(self.tick_tape_path.relative_to(self.root)) if self.tick_tape_path.exists() else str(self.tick_tape_path),
            },
            "summary": {
                "count": count,
                "latest_symbol": latest.get("symbol", ""),
                "latest_bid": latest.get("bid"),
                "latest_ask": latest.get("ask"),
                "latest_spread": latest.get("spread_points", ""),
                "latest_captured_at": latest.get("captured_at"),
                "latest_source": latest.get("source", ""),
                "latest_source_label": latest.get("source_label", ""),
                "bid_delta": self._bid_delta(),
                "spread_min": min(spreads) if spreads else 0,
                "spread_max": max(spreads) if spreads else 0,
                "latest_age_seconds": round(age, 3),
                "ticks_per_minute": self._ticks_per_minute(),
            },
            "latest_tick": latest,
        }

    def agent_summary(self, settings: AgentSettings) -> dict[str, Any]:
        settings = self._resolve_settings_paths(settings)
        memory_rows = AgentRuntimeService(settings).memory.experience().read_raw()
        recent = [self._decision_row(row) for row in memory_rows[-20:]]
        latest = recent[-1] if recent else {}
        audit_rows = DecisionAuditStore(settings.decision_audit_path).read_recent(10)
        if latest and audit_rows:
            latest["decision_audit"] = audit_rows[-1]
        paper = PaperPortfolio(settings.paper_state_path) if settings.mode == "paper" else None
        state = paper._read_state() if paper else {"positions": [], "closed": []}
        open_positions = [position for position in state.get("positions", []) if position.get("status") == "open"]
        closed = list(state.get("closed", []))
        return {
            "config": self._settings_name(settings),
            "mode": settings.mode,
            "market_source": settings.market_source,
            "brain": settings.planner,
            "planner": settings.planner,
            "analyst": settings.analyst,
            "memory_path": settings.memory_path,
            "vector_memory_path": settings.vector_memory_path,
            "decision_audit_path": settings.decision_audit_path,
            "experience_count": len(memory_rows),
            "latest_decision": latest,
            "recent_decisions": recent[-12:],
            "open_positions": open_positions,
            "open_position_count": len(open_positions),
            "mt5_open_positions": [],
            "mt5_open_position_count": 0,
            "recent_closes": closed[-10:],
            "closed_position_count": len(closed),
            "paper_total_r": round(sum(self._float(item.get("r_multiple"), 0.0) for item in closed), 4),
            "win_rate": self._win_rate(closed),
            "monitor": {**self.monitor_state, "recent_events": self.monitor_events[-12:], "event_count": len(self.monitor_events)},
            "audit_recent": audit_rows,
        }

    def operations_summary(self, settings: AgentSettings) -> dict[str, Any]:
        settings = self._resolve_settings_paths(settings)
        agent = self.agent_summary(settings)
        return {
            "open_position_count": agent["open_position_count"],
            "closed_position_count": agent["closed_position_count"],
            "paper_total_r": agent["paper_total_r"],
            "win_rate": agent["win_rate"],
            "mt5_open_position_count": 0,
            "mt5_open_positions": [],
            "recent_closes": agent["recent_closes"],
        }

    def runtime_status(self, public_settings: dict[str, Any] | None = None) -> dict[str, Any]:
        settings = self._summary_settings()
        memory_service = AgentRuntimeService(settings).memory
        experience_rows = memory_service.experience().read_raw()
        vector = memory_service.vector()
        vector_count = vector.count(filters={"symbol": settings.symbols[0]}) if vector and hasattr(vector, "count") else 0
        audit_rows = DecisionAuditStore(settings.decision_audit_path).read_recent(25)
        latest = self.live_summary()["summary"]
        tick_age = self._float(latest.get("latest_age_seconds"), 0.0)
        api_key_set = bool((public_settings or {}).get("openai_api_key_set"))
        return {
            "backend": {
                "status": "ok",
                "runtime": "autonomous",
                "root": str(self.root),
                "sequence": self.event_sequence,
            },
            "agent": {
                "config": self._settings_name(settings) or "mt5-paper.json",
                "mode": settings.mode,
                "planner": settings.planner,
                "analyst": settings.analyst,
                "symbols": list(settings.symbols),
            },
            "ai": {
                "api_key_set": api_key_set,
                "planner_model": settings.ai_planner_model,
                "fail_closed": settings.ai_planner_fail_closed,
                "require_memory_for_live": settings.require_memory_for_live_ai,
                "min_live_memory_samples": settings.min_live_memory_samples,
            },
            "market": {
                "source": settings.market_source,
                "ea_tick_active": bool(self.latest_tick),
                "latest_symbol": latest.get("latest_symbol", ""),
                "latest_tick_age_seconds": tick_age,
                "ticks_per_minute": latest.get("ticks_per_minute", 0),
            },
            "memory": {
                "experience_count": len(experience_rows),
                "vector_enabled": settings.vector_memory_enabled,
                "vector_count": vector_count,
                "audit_count_recent": len(audit_rows),
                "memory_path": settings.memory_path,
                "vector_path": settings.vector_memory_path,
                "audit_path": settings.decision_audit_path,
            },
            "bridge": self._bridge_status(settings),
            "monitor": dict(self.monitor_state),
            "overall": self._overall_status(api_key_set, settings, tick_age),
        }

    def market_chart_summary(self, timeframe: str = "H1", candle_count: int = 80) -> dict[str, Any]:
        symbols = sorted({str(tick.get("symbol", "XAUUSD")) for tick in self.ticks if tick.get("symbol")}) or ["XAUUSD"]
        series: dict[str, list[dict[str, Any]]] = {}
        overlays: dict[str, dict[str, Any]] = {}
        for symbol in symbols:
            rows = self._tick_candles(symbol)[-candle_count:]
            series[symbol] = rows
            overlays[symbol] = {"signals": []}
        return {"timeframe": timeframe, "symbols": symbols, "series": series, "overlays": overlays}

    def start_agent(self, config: str, max_iterations: int, interval_seconds: float, trigger_mode: str = "interval") -> dict[str, Any]:
        with self.monitor_lock:
            if self.monitor_thread and self.monitor_thread.is_alive():
                return {"status": "already_running", "running": True}
            settings = self.settings_agent_config(config)
            self.monitor_stop_event = threading.Event()
            self.monitor_state = {
                "running": True,
                "status": "running",
                "config": config,
                "max_iterations": max_iterations,
                "trigger_mode": trigger_mode,
                "started_at": self._now(),
                "stopped_at": None,
                "error": None,
            }
            self.monitor_thread = threading.Thread(
                target=self._monitor_loop,
                args=(settings, max_iterations, interval_seconds, self.monitor_stop_event),
                name="quantz-autonomous-monitor",
                daemon=True,
            )
            self.monitor_thread.start()
        self._notify()
        return {"status": "started", "running": True, "config": config}

    def stop_agent(self) -> dict[str, Any]:
        with self.monitor_lock:
            if self.monitor_stop_event:
                self.monitor_stop_event.set()
            self.monitor_state = {**self.monitor_state, "running": False, "status": "stopping", "stopped_at": self._now()}
        self._notify()
        return {"status": "stopping", "running": False}

    def ingest_bridge_tick(self, body: str) -> dict[str, Any]:
        return self.ingest_bridge_tick_query(body)

    def ingest_bridge_tick_query(self, query: str) -> dict[str, Any]:
        values = parse_qs(query, keep_blank_values=True)
        tick = self._tick_from_values(values)
        with self.tick_lock:
            self.latest_tick = tick
            self.ticks.append(tick)
            self.ticks = self.ticks[-500:]
            self.tick_tape_path.parent.mkdir(parents=True, exist_ok=True)
            with self.tick_tape_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(tick, sort_keys=True, default=str) + "\n")
        self._notify()
        return {"status": "accepted", "received": 1, "latest_tick": tick, "sequence": self.event_sequence}

    def replay_summary(self) -> dict[str, Any]:
        return {"running": False, "status": "not_configured", "events": []}

    def start_replay(self, _payload: dict[str, Any]) -> dict[str, Any]:
        return {"status": "not_configured", "error": "legacy_replay_ui_removed_use_cli_backtest"}

    def stop_replay(self) -> dict[str, Any]:
        return {"status": "stopped"}

    def _monitor_loop(self, settings: AgentSettings, max_iterations: int, interval_seconds: float, stop_event: threading.Event) -> None:
        error = None
        try:
            bridge = BridgeClient(settings.bridge_url)
            market_feed, account_feed = self._feeds(settings, bridge)
            agent = AgentRuntimeService(settings, bridge).build_agent()
            iteration = 0
            while not stop_event.is_set() and (max_iterations == 0 or iteration < max_iterations):
                iteration += 1
                for symbol in settings.symbols:
                    if stop_event.is_set():
                        break
                    market = market_feed.snapshot(symbol)
                    account = account_feed.state()
                    record = agent.run_once(AgentContext(market=market, account=account, constraints=settings.constraints))
                    self.monitor_events.append(self._event_from_record(iteration, record))
                    self.monitor_events = self.monitor_events[-100:]
                    self._notify()
                if max_iterations == 0 or iteration < max_iterations:
                    stop_event.wait(interval_seconds)
        except Exception as exc:
            error = f"{type(exc).__name__}:{exc}"
        finally:
            with self.monitor_lock:
                self.monitor_state = {
                    **self.monitor_state,
                    "running": False,
                    "status": "error" if error else "stopped",
                    "stopped_at": self._now(),
                    "error": error,
                }
            self._notify()

    def _feeds(self, settings: AgentSettings, bridge: BridgeClient) -> tuple[Any, Any]:
        if settings.market_source == "sim":
            return SimulatedMarketFeed(settings.sim_state_path), DemoAccountFeed()
        if settings.market_source == "demo":
            return DemoMarketFeed(), DemoAccountFeed()
        if settings.market_source == "mt5":
            connection = Mt5Connection()
            return Mt5MarketFeed(connection), Mt5AccountFeed(connection)
        if settings.market_source == "bridge":
            from quantz.bridge import BridgeAccountFeed, BridgeMarketFeed

            return BridgeMarketFeed(bridge), BridgeAccountFeed(bridge)
        raise ValueError(f"Unknown market source: {settings.market_source}")

    def _bridge_status(self, settings: AgentSettings) -> dict[str, Any]:
        url = settings.bridge_url.rstrip("/")
        expected = settings.execution_source == "bridge" or settings.market_source == "bridge"
        status = {
            "expected": expected,
            "url": url,
            "reachable": False,
            "message": "not_required" if not expected else "unchecked",
        }
        if not expected:
            return status
        try:
            request = urllib.request.Request(f"{url}/account", headers={"User-Agent": "QuantzRuntime"})
            with urllib.request.urlopen(request, timeout=1.5) as response:
                status["reachable"] = 200 <= int(response.status) < 500
                status["message"] = f"http:{response.status}"
        except Exception as exc:
            status["message"] = f"{type(exc).__name__}:{exc}"
        return status

    def _overall_status(self, api_key_set: bool, settings: AgentSettings, tick_age: float) -> str:
        if settings.planner == "ai" and not api_key_set:
            return "blocked_ai_key_missing"
        if settings.market_source == "mt5" and not self.latest_tick:
            return "waiting_for_ea_tick"
        if tick_age > 120 and self.latest_tick:
            return "stale_tick"
        if self.monitor_state.get("status") == "error":
            return "monitor_error"
        return "ready"

    def _summary_settings(self) -> AgentSettings:
        preferred = self.configs_dir / "mt5-paper.json"
        if preferred.exists():
            return self._resolve_settings_paths(load_settings(str(preferred)))
        return self._resolve_settings_paths(AgentSettings())

    def _settings_name(self, settings: AgentSettings) -> str:
        for path in self.configs_dir.glob("*.json"):
            try:
                if load_settings(str(path)) == settings:
                    return path.name
            except Exception:
                continue
        return ""

    def _resolve_settings_paths(self, settings: AgentSettings) -> AgentSettings:
        def resolve(value: str) -> str:
            path = Path(value)
            return str(path if path.is_absolute() else self.root / path)

        return merge_settings(
            settings,
            memory_path=resolve(settings.memory_path),
            paper_state_path=resolve(settings.paper_state_path),
            sim_state_path=resolve(settings.sim_state_path),
            vector_memory_path=resolve(settings.vector_memory_path),
            decision_audit_path=resolve(settings.decision_audit_path),
            playbook_paths=[resolve(path) for path in settings.playbook_paths],
            teacher_example_paths=[resolve(path) for path in settings.teacher_example_paths],
        )

    def _tick_from_values(self, values: dict[str, list[str]]) -> dict[str, Any]:
        symbol = self._first(values, "symbol", "XAUUSD").upper()
        bid = self._float(self._first(values, "bid", "0"), 0.0)
        ask = self._float(self._first(values, "ask", "0"), 0.0)
        captured = datetime.now(timezone.utc).isoformat()
        return {
            "captured_at": captured,
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "mid": round((bid + ask) / 2, 6) if bid and ask else 0,
            "spread_points": self._first(values, "spread_points", "0"),
            "tick_time": self._first(values, "tick_time", ""),
            "source": "ea_socket",
            "source_label": "EA socket",
        }

    def _tick_candles(self, symbol: str) -> list[dict[str, Any]]:
        rows = [tick for tick in self.ticks if str(tick.get("symbol", "")).upper() == symbol.upper()]
        candles = []
        for index, tick in enumerate(rows[-120:]):
            mid = self._float(tick.get("mid"), 0.0)
            spread = self._float(tick.get("spread_points"), 0.0) * 0.01
            candles.append(
                {
                    "symbol": symbol,
                    "timestamp": tick.get("captured_at"),
                    "step": index,
                    "open": mid,
                    "high": mid + max(spread, 0.01),
                    "low": mid - max(spread, 0.01),
                    "close": mid,
                }
            )
        return candles

    def _decision_row(self, row: dict[str, Any]) -> dict[str, Any]:
        decision = row.get("decision", {})
        risk = row.get("risk", {})
        metadata = decision.get("metadata", {}) or {}
        ai = metadata.get("ai_decision", {}) if isinstance(metadata, dict) else {}
        analyst = metadata.get("analyst", {}) if isinstance(metadata, dict) else {}
        return {
            "decision_id": decision.get("decision_id"),
            "timestamp": decision.get("timestamp"),
            "timestamp_raw": decision.get("timestamp"),
            "symbol": decision.get("symbol"),
            "action": decision.get("action"),
            "confidence": decision.get("confidence"),
            "risk_status": risk.get("status"),
            "reasons": decision.get("reason_codes", []),
            "llm_brief": ai,
            "llm_trace": metadata.get("decision_audit", {}),
            "analyst_model": analyst.get("model_version") if isinstance(analyst, dict) else "",
            "analyst_bias": analyst.get("bias") if isinstance(analyst, dict) else "",
            "analyst_regime": analyst.get("market_regime") if isinstance(analyst, dict) else "",
            "analyst_risk_notes": analyst.get("risk_notes", []) if isinstance(analyst, dict) else [],
        }

    def _event_from_record(self, iteration: int, record: Any) -> dict[str, Any]:
        return {
            "iteration": iteration,
            "symbol": record.decision.symbol,
            "action": record.decision.action,
            "confidence": record.decision.confidence,
            "risk_status": record.risk.status,
            "execution": record.execution.message if record.execution else None,
            "timestamp": self._now(),
        }

    def _load_recent_ticks(self) -> None:
        if not self.tick_tape_path.exists():
            return
        rows = []
        with self.tick_tape_path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
        self.ticks = rows[-500:]
        self.latest_tick = self.ticks[-1] if self.ticks else None

    def _notify(self) -> None:
        with self.event_condition:
            self.event_sequence += 1
            self.event_condition.notify_all()

    def _first(self, values: dict[str, list[str]], key: str, default: str = "") -> str:
        items = values.get(key, [])
        return str(items[0]) if items else default

    def _float(self, value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _bid_delta(self) -> float:
        if len(self.ticks) < 2:
            return 0.0
        return round(self._float(self.ticks[-1].get("bid"), 0.0) - self._float(self.ticks[-2].get("bid"), 0.0), 6)

    def _ticks_per_minute(self) -> int:
        if not self.ticks:
            return 0
        now = datetime.now(timezone.utc)
        count = 0
        for tick in reversed(self.ticks):
            try:
                captured = datetime.fromisoformat(str(tick.get("captured_at"))).astimezone(timezone.utc)
            except ValueError:
                continue
            if (now - captured).total_seconds() <= 60:
                count += 1
        return count

    def _win_rate(self, closed: list[dict[str, Any]]) -> float:
        if not closed:
            return 0.0
        wins = sum(1 for item in closed if self._float(item.get("r_multiple"), 0.0) > 0)
        return round(wins / len(closed) * 100, 2)

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()


def json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
