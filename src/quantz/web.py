from __future__ import annotations

import html
import json
import sys
import threading
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from quantz.agent import TradingAgent
from quantz.analyst import LLMAnalyst, RuleBasedAnalyst
from quantz.broker import Mt5BrokerAdapter, PaperBrokerAdapter
from quantz.config import load_settings, merge_settings, settings_to_dict
from quantz.dashboard import DashboardRenderer
from quantz.experiment import ExperimentRunner
from quantz.market import Mt5AccountFeed, Mt5Connection, Mt5MarketFeed
from quantz.memory import JsonlExperienceStore
from quantz.models import AccountState, AgentContext, MarketSnapshot
from quantz.paper import PaperPortfolio
from quantz.planner import VariableDrivenPlanner
from quantz.risk import RiskConfig, RiskGovernor

LOCAL_TZ = timezone(timedelta(hours=7), "WIB")


class WebApp:
    def __init__(self, root: str | Path = ".", monitor_fn: Any | None = None) -> None:
        self.root = Path(root).resolve()
        self.configs_dir = self.root / "configs"
        self.experiments_dir = self.root / "data" / "experiments"
        self.monitor_session_path = self.root / "data" / "monitor-session.json"
        self.tick_tape_path = self.root / "data" / "mt5-ticks.jsonl"
        self.tick_tape_lock = threading.Lock()
        self.tick_recent_keys: set[tuple[str, str, str, str]] = set()
        self.last_ea_tick_at: datetime | None = None
        self.last_python_poll_at: datetime | None = None
        self.event_condition = threading.Condition()
        self.event_sequence = 0
        self.tick_collector_thread: threading.Thread | None = None
        self.tick_collector_stop_event: threading.Event | None = None
        self.monitor_fn = monitor_fn
        self.monitor_lock = threading.Lock()
        self.monitor_thread: threading.Thread | None = None
        self.monitor_stop_event: threading.Event | None = None
        self.monitor_state: dict[str, Any] = self._default_monitor_state()
        self.monitor_events: list[dict[str, Any]] = []
        self._load_monitor_snapshot()

    def _default_monitor_state(self) -> dict[str, Any]:
        return {
            "running": False,
            "status": "stopped",
            "config": None,
            "max_iterations": 0,
            "trigger_mode": "interval",
            "started_at": None,
            "stopped_at": None,
            "error": None,
        }

    def handler(self) -> type[BaseHTTPRequestHandler]:
        app = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                app.handle_get(self)

            def do_POST(self) -> None:
                app.handle_post(self)

            def log_message(self, format: str, *args: Any) -> None:
                return

        return Handler

    def handle_get(self, request: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(request.path)
        if parsed.path == "/":
            self._html(request, self._index())
            return
        if parsed.path == "/config":
            name = parse_qs(parsed.query).get("name", ["paper-demo.json"])[0]
            self._html(request, self._config_page(name))
            return
        if parsed.path == "/experiments":
            self._html(request, self._experiments_page())
            return
        if parsed.path == "/operations":
            self._html(request, self._operations_page())
            return
        if parsed.path == "/agent":
            self._html(request, self._agent_page())
            return
        if parsed.path == "/control":
            self._html(request, self._control_page())
            return
        if parsed.path == "/pnl":
            self._html(request, self._pnl_page())
            return
        if parsed.path == "/market":
            self._html(request, self._market_page())
            return
        if parsed.path == "/events":
            self._events(request)
            return
        if parsed.path == "/bridge/tick":
            self._json(request, self._ingest_bridge_tick_query(parsed.query))
            return
        if parsed.path.startswith("/experiments/") and parsed.path.endswith("/dashboard.html"):
            experiment_name = parsed.path.split("/")[2]
            self._file(request, self.experiments_dir / experiment_name / "dashboard.html", "text/html")
            return
        if parsed.path.startswith("/api/"):
            self._json(request, self._api(parsed.path, parsed.query))
            return
        self._not_found(request)

    def handle_post(self, request: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(request.path)
        length = int(request.headers.get("Content-Length", "0"))
        body = request.rfile.read(length).decode("utf-8")
        if parsed.path == "/bridge/tick":
            self._json(request, self._ingest_bridge_tick(body))
            return
        form = parse_qs(body)
        if parsed.path == "/config/save":
            name = form.get("name", ["paper-demo.json"])[0]
            content = form.get("content", ["{}"])[0]
            self._save_config(name, content)
            self._redirect(request, f"/config?name={name}")
            return
        if parsed.path == "/experiments/run":
            result = self._run_experiment_from_form(form)
            if "error" in result:
                self._html(request, self._layout("Experiment Error", f"<section><h2>Error</h2><pre>{self._escape(result['error'])}</pre><a class=\"button secondary\" href=\"/\">Back</a></section>"))
                return
            self._redirect(request, "/experiments")
            return
        if parsed.path == "/monitor/start":
            result = self._start_monitor_from_form(form)
            if "error" in result:
                self._html(request, self._layout("Monitor Error", f"<section><h2>Error</h2><pre>{self._escape(result['error'])}</pre><a class=\"button secondary\" href=\"/operations\">Back</a></section>"))
                return
            self._redirect(request, "/operations")
            return
        if parsed.path == "/monitor/stop":
            self._stop_monitor()
            self._redirect(request, "/operations")
            return
        if parsed.path == "/agent/monitor/start":
            result = self._start_monitor_from_form(form)
            next_path = self._safe_redirect_path(form.get("next", ["/agent"])[0])
            if "error" in result:
                self._html(request, self._layout("Agent Error", f"<section><h2>Error</h2><pre>{self._escape(result['error'])}</pre><a class=\"button secondary\" href=\"{self._escape(next_path)}\">Back</a></section>"))
                return
            self._redirect(request, next_path)
            return
        if parsed.path == "/agent/monitor/stop":
            next_path = self._safe_redirect_path(form.get("next", ["/agent"])[0])
            self._stop_monitor()
            self._redirect(request, next_path)
            return
        self._not_found(request)

    def _index(self) -> str:
        configs = self._config_names()
        latest_experiment = self._experiment_names()[-1] if self._experiment_names() else None
        performance = self._performance_summary()
        return self._layout(
            "Overview",
            f"""
            <section>
              <h2>Agent Performance</h2>
              <div class="metrics">{self._performance_metrics(performance)}</div>
            </section>
            <section>
              <h2>Control Surface</h2>
              <div class="actions">
                <a class="button" href="/config?name={configs[0] if configs else 'paper-demo.json'}">Edit Config</a>
                <a class="button secondary" href="/operations">Operations</a>
                <a class="button secondary" href="/pnl">PnL Dashboard</a>
                <a class="button secondary" href="/market">Market Chart</a>
                <a class="button secondary" href="/experiments">Experiments</a>
                {f'<a class="button secondary" href="/experiments/{latest_experiment}/dashboard.html">Latest Dashboard</a>' if latest_experiment else ''}
              </div>
            </section>
            <section>
              <h2>Configs</h2>
              {self._config_list(configs)}
            </section>
            <section>
              <h2>Run Experiment</h2>
              {self._run_experiment_form(configs)}
            </section>
            <section>
              <h2>Run Monitor</h2>
              {self._run_monitor_form(configs)}
            </section>
            <section>
              <h2>Runtime Commands</h2>
              <pre>PYTHONPATH=src .venv/bin/python -m quantz.cli experiment --config configs/paper-demo.json --output-dir data/experiments/run-001 --iterations 24
PYTHONPATH=src .venv/bin/python -m quantz.cli dashboard --experiment-dir data/experiments/run-001 --output data/experiments/run-001/dashboard.html</pre>
            </section>
            """,
        )

    def _config_page(self, name: str) -> str:
        path = self._safe_config_path(name)
        content = path.read_text(encoding="utf-8") if path.exists() else "{}"
        parsed: dict[str, Any]
        try:
            parsed = settings_to_dict(load_settings(str(path))) if path.exists() else {}
            status = "Valid"
        except Exception as exc:
            parsed = {}
            status = f"Invalid: {exc}"
        return self._layout(
            f"Config: {name}",
            f"""
            <section>
              <h2>{self._escape(name)}</h2>
              <p>Status: <code>{self._escape(status)}</code></p>
              <form method="post" action="/config/save">
                <input type="hidden" name="name" value="{self._escape(name)}">
                <textarea name="content" spellcheck="false">{self._escape(content)}</textarea>
                <div class="actions"><button type="submit">Save Config</button><a class="button secondary" href="/">Back</a></div>
              </form>
            </section>
            <section>
              <h2>Parsed Settings</h2>
              <div class="metrics">{self._settings_metrics(parsed)}</div>
            </section>
            """,
        )

    def _experiments_page(self) -> str:
        names = self._experiment_names()
        rows = []
        for name in names:
            comparison = self._read_json(self.experiments_dir / name / "comparison.json")
            review = self._read_json(self.experiments_dir / name / "review.json")
            report = self._read_json(self.experiments_dir / name / "base-report.json")
            config = self._read_json(self.experiments_dir / name / "base-config.json")
            verdict = comparison.get("verdict", "missing")
            status = review.get("promotion_status", "missing")
            total_r = float(report.get("total_r_multiple", 0.0))
            estimated_pnl = self._estimated_pnl(total_r, config)
            dashboard = self.experiments_dir / name / "dashboard.html"
            dashboard_link = (
                f'<a href="/experiments/{self._escape(name)}/dashboard.html">Open</a>'
                if dashboard.exists()
                else "Not rendered"
            )
            rows.append(
                "<tr>"
                f"<td>{self._escape(name)}</td>"
                f"<td>{self._escape(verdict)}</td>"
                f"<td>{self._escape(status)}</td>"
                f"<td>{self._escape(total_r)}</td>"
                f"<td>{self._escape(self._money(estimated_pnl))}</td>"
                f"<td>{self._escape(report.get('win_rate', 0))}</td>"
                f"<td>{self._escape(report.get('open_position_count', 0))}</td>"
                f"<td>{dashboard_link}</td>"
                "</tr>"
            )
        table = "<p>No experiments yet.</p>" if not rows else "<table><thead><tr><th>Run</th><th>Verdict</th><th>Status</th><th>Total R</th><th>Est. PnL</th><th>Win Rate</th><th>Open</th><th>Dashboard</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        return self._layout("Experiments", f"<section><h2>Experiments</h2>{table}</section>")

    def _operations_page(self) -> str:
        operations = self._operations_summary()
        monitor = self._monitor_summary()
        return self._layout(
            "Operations",
            f"""
            <section>
              <h2>Monitor Session</h2>
              <div id="monitor-metrics" class="metrics">{self._monitor_metrics(monitor)}</div>
              <div id="monitor-controls">{self._monitor_controls()}</div>
              <template id="monitor-start-template">{self._run_monitor_form(self._config_names())}</template>
              <template id="monitor-stop-template">{self._monitor_stop_form()}</template>
            </section>
            <section>
              <h2>Agent Status</h2>
              <div id="operations-metrics" class="metrics">{self._operations_metrics(operations)}</div>
            </section>
            <section>
              <h2>Open Positions</h2>
              <div id="open-positions-table">{self._positions_table(operations["open_positions"])}</div>
            </section>
            <section>
              <h2>Recent Decisions</h2>
              <div id="recent-decisions-table">{self._decisions_table(operations["recent_decisions"])}</div>
            </section>
            <section>
              <h2>Monitor Events</h2>
              <div id="monitor-events-table">{self._monitor_events_table(monitor["recent_events"])}</div>
            </section>
            <section>
              <h2>Recent Closes</h2>
              <div id="recent-closes-table">{self._closes_table(operations["recent_closes"])}</div>
            </section>
            """,
        )

    def _pnl_page(self) -> str:
        pnl = self._pnl_summary()
        return self._layout(
            "PnL Dashboard",
            f"""
            <section>
              <h2>PnL Summary</h2>
              <div id="pnl-metrics" class="metrics">{self._pnl_metrics(pnl)}</div>
            </section>
            <section>
              <h2>Equity Curve</h2>
              <div id="equity-curve">{self._equity_curve(pnl["equity_curve"])}</div>
            </section>
            <section>
              <h2>Visual Charts</h2>
              <div class="chart-grid">
                <div id="symbol-r-chart" class="chart-box">{self._bar_chart("Symbol Total R", pnl["by_symbol"], "symbol", "total_r")}</div>
                <div id="decision-reason-chart" class="chart-box">{self._count_bar_chart("Decision Reasons", pnl["decision_reasons"])}</div>
                <div id="rejection-reason-chart" class="chart-box">{self._count_bar_chart("Rejection Reasons", pnl["rejection_reasons"])}</div>
              </div>
            </section>
            <section>
              <h2>Reason Quality</h2>
              <div id="reason-quality-table">{self._reason_quality_table(pnl["reason_quality"])}</div>
            </section>
            <section>
              <h2>Symbol Performance</h2>
              <div id="symbol-performance-table">{self._symbol_performance_table(pnl["by_symbol"])}</div>
            </section>
            <section>
              <h2>Decision Reasons</h2>
              <div id="decision-reasons-table">{self._count_table(pnl["decision_reasons"], "Reason", "Count")}</div>
            </section>
            <section>
              <h2>Rejection Reasons</h2>
              <div id="rejection-reasons-table">{self._count_table(pnl["rejection_reasons"], "Reason", "Count")}</div>
            </section>
            """,
        )

    def _market_page(self) -> str:
        chart = self._market_chart_summary()
        live = self._live_market_summary()
        selected = chart["symbols"][0] if chart["symbols"] else ""
        return self._layout(
            "Market",
            f"""
            <section>
              <h2>Market</h2>
              <div id="market-live-strip" class="market-strip">{self._market_live_strip(live)}</div>
              <div class="market-toolbar">
                <label>Symbol<select id="market-symbol-select">{self._market_symbol_options(chart["symbols"], selected)}</select></label>
                <label>Timeframe<select id="market-timeframe-select">{self._market_timeframe_options(chart.get("timeframe", "H1"))}</select></label>
                <label>Candles<input id="market-candle-limit" type="number" min="20" max="300" step="10" value="{self._escape(len(chart["series"].get(selected, [])) or 80)}"></label>
                <button id="market-zoom-in" type="button">Zoom In</button>
                <button id="market-zoom-out" class="secondary" type="button">Zoom Out</button>
                <button id="market-reset-view" class="secondary" type="button">Reset</button>
              </div>
              <div class="market-meta">
                <span>Source <strong>{self._escape(chart["source"])}</strong></span>
                <span>Timeframe <strong id="market-timeframe-label">{self._escape(chart.get("timeframe", ""))}</strong></span>
                <span>Selected <strong id="market-selected-symbol">{self._escape(selected)}</strong></span>
                <span>Candles <strong id="market-candle-count">{self._escape(len(chart["series"].get(selected, [])))}</strong></span>
              </div>
              <div class="market-legend">
                <span><i class="legend-up"></i>Bull candle</span>
                <span><i class="legend-down"></i>Bear candle</span>
                <span><i class="legend-ma-fast"></i>MA fast</span>
                <span><i class="legend-ma-slow"></i>MA slow</span>
                <span><i class="legend-atr"></i>ATR band</span>
                <span><i class="legend-entry"></i>Entry / SL / TP</span>
              </div>
              <div id="market-candles">{self._candlestick_chart(selected, chart["series"].get(selected, []), chart["overlays"].get(selected, {}))}</div>
            </section>
            <section>
              <h2>Tick Tape</h2>
              <div class="market-toolbar compact">
                <label>Window<select id="tick-tape-limit"><option value="50">50 ticks</option><option value="120" selected>120 ticks</option><option value="300">300 ticks</option></select></label>
              </div>
              <div id="tick-tape-metrics" class="market-strip">{self._tick_tape_metrics(live.get("recent_ticks", []))}</div>
              <div id="tick-tape-chart">{self._tick_tape_chart(live.get("recent_ticks", []))}</div>
            </section>
            """,
        )

    def _safe_redirect_path(self, value: str) -> str:
        if not value.startswith("/") or value.startswith("//"):
            return "/agent"
        return value

    def _control_page(self) -> str:
        summary = self._control_summary(include_chart=True)
        chart = summary.get("chart", {})
        selected = chart.get("symbols", [""])[0] if chart.get("symbols") else ""
        agent = summary.get("agent", {})
        operations = summary.get("operations", {})
        return self._layout(
            "Control",
            f"""
            <div id="control-root">
              <section class="control-shell">
                <div class="control-main">
                  <div class="control-header">
                    <div>
                      <h2>Market Control</h2>
                      <p>Live MT5 tick, current price level, and candle context in one surface.</p>
                    </div>
                    <strong id="control-stream-state" class="control-pill">connecting</strong>
                  </div>
                  <div id="control-live-strip" class="market-strip compact-strip">{self._control_live_strip(summary)}</div>
                  <div class="market-toolbar control-toolbar">
                    <label>Symbol<select id="control-symbol-select">{self._market_symbol_options(chart.get("symbols", []), selected)}</select></label>
                    <label>Timeframe<select id="control-timeframe-select">{self._market_timeframe_options(chart.get("timeframe", "H1"))}</select></label>
                    <label>Candles<input id="control-candle-limit" type="number" min="20" max="300" step="10" value="{self._escape(len(chart.get("series", {}).get(selected, [])) or 80)}"></label>
                    <button id="control-zoom-in" type="button">Zoom In</button>
                    <button id="control-zoom-out" class="secondary" type="button">Zoom Out</button>
                    <button id="control-reset-view" class="secondary" type="button">Reset</button>
                  </div>
                  <div class="market-meta">
                    <span>Source <strong id="control-chart-source">{self._escape(chart.get("source", ""))}</strong></span>
                    <span>Timeframe <strong id="control-timeframe-label">{self._escape(chart.get("timeframe", ""))}</strong></span>
                    <span>Selected <strong id="control-selected-symbol">{self._escape(selected)}</strong></span>
                    <span>Candles <strong id="control-candle-count">{self._escape(len(chart.get("series", {}).get(selected, [])))}</strong></span>
                  </div>
                  <div id="control-market-candles">{self._candlestick_chart(selected, chart.get("series", {}).get(selected, []), chart.get("overlays", {}).get(selected, {}))}</div>
                </div>
                <aside class="control-side">
                  <div class="control-panel">
                    <h2>Tick</h2>
                    <div id="control-tick-status" class="control-status-list">{self._control_tick_status(summary)}</div>
                    <div id="control-tick-chart">{self._tick_tape_chart(summary.get("ticks", []))}</div>
                  </div>
                  <div class="control-panel">
                    <h2>Agent</h2>
                    <div id="control-agent-strip" class="market-strip compact-strip">{self._agent_status_strip(agent)}</div>
                    <div id="control-agent-controls">{self._agent_controls(agent.get("monitor", {}), "/control")}</div>
                  </div>
                  <div class="control-panel">
                    <h2>Reasoning</h2>
                    <div id="control-decision-card">{self._agent_decision_card(agent.get("latest_decision", {}))}</div>
                  </div>
                </aside>
              </section>
              <section class="control-lower">
                <div class="control-panel">
                  <h2>MT5 Positions</h2>
                  <div id="control-mt5-open-positions">{self._mt5_positions_table(agent.get("mt5_open_positions", []))}</div>
                </div>
                <div class="control-panel">
                  <h2>Paper Positions</h2>
                  <div id="control-open-positions">{self._positions_table(agent.get("open_positions", []))}</div>
                </div>
                <div class="control-panel wide-panel">
                  <h2>History</h2>
                  <div id="control-history">{self._decisions_table(agent.get("recent_decisions", []))}</div>
                </div>
                <div class="control-panel">
                  <h2>Closed</h2>
                  <div id="control-recent-closes">{self._closes_table(operations.get("recent_closes", []))}</div>
                </div>
                <div class="control-panel">
                  <h2>Agent Events</h2>
                  <div id="control-agent-events">{self._monitor_events_table(agent.get("monitor", {}).get("recent_events", []))}</div>
                </div>
              </section>
            </div>
            """,
        )

    def _agent_page(self) -> str:
        console = self._agent_console_summary()
        monitor = console["monitor"]
        return self._layout(
            "Agent",
            f"""
            <section>
              <h2>Agent Control</h2>
              <div id="agent-status-strip" class="market-strip">{self._agent_status_strip(console)}</div>
              <div id="agent-controls">{self._agent_controls(monitor, "/agent")}</div>
            </section>
            <section>
              <h2>Decision Console</h2>
              <div id="agent-decision-card">{self._agent_decision_card(console["latest_decision"])}</div>
            </section>
            <section>
              <h2>Recent Decisions</h2>
              <div id="agent-decisions-table">{self._decisions_table(console["recent_decisions"])}</div>
            </section>
            <section>
              <h2>MT5 Positions</h2>
              <div id="agent-mt5-open-positions">{self._mt5_positions_table(console.get("mt5_open_positions", []))}</div>
            </section>
            <section>
              <h2>Paper Positions</h2>
              <div id="agent-open-positions">{self._positions_table(console["open_positions"])}</div>
            </section>
            <section>
              <h2>Agent Events</h2>
              <div id="agent-events-table">{self._monitor_events_table(monitor["recent_events"])}</div>
            </section>
            """,
        )

    def _api(self, path: str, query: str = "") -> dict[str, Any]:
        if path == "/api/configs":
            return {"configs": self._config_names()}
        if path == "/api/experiments":
            return {"experiments": self._experiment_names()}
        if path == "/api/performance":
            return self._performance_summary()
        if path == "/api/operations":
            return self._operations_summary()
        if path == "/api/agent-console":
            return self._agent_console_summary()
        if path == "/api/control":
            return self._control_summary(include_chart=True)
        if path == "/api/pnl":
            return self._pnl_summary()
        if path == "/api/monitor":
            return self._monitor_summary()
        if path == "/api/visual-digest":
            return self._visual_digest()
        if path == "/api/market-chart":
            params = parse_qs(query)
            try:
                candle_count = int(params.get("candles", ["80"])[0])
            except ValueError:
                candle_count = 80
            return self._market_chart_summary(
                timeframe=params.get("timeframe", ["H1"])[0],
                candle_count=candle_count,
            )
        if path == "/api/live-market":
            return self._live_market_summary()
        if path == "/api/tick-tape":
            params = parse_qs(query)
            try:
                limit = int(params.get("limit", ["120"])[0])
            except ValueError:
                limit = 120
            ticks = self._recent_ticks(limit=max(1, min(limit, 500)))
            return {"collector": self._tick_collector_summary(), "summary": self._tick_tape_summary(ticks), "ticks": ticks}
        return {"error": "not_found"}

    def _layout(self, title: str, body: str) -> str:
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Quantz - {self._escape(title)}</title>
  <style>
    :root {{ --bg:#f5f7fa; --panel:#fff; --ink:#15202b; --muted:#657386; --line:#dce3ea; --accent:#145c48; --good:#146c43; --bad:#a52727; --bar:#d9e8e2; --warn:#b7791f; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--ink); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    nav {{ height:56px; display:flex; align-items:center; justify-content:space-between; padding:0 24px; background:var(--panel); border-bottom:1px solid var(--line); }}
    nav a {{ color:var(--ink); text-decoration:none; margin-left:14px; font-weight:650; }}
    main {{ max-width:1500px; margin:0 auto; padding:24px; }}
    h1 {{ font-size:26px; margin:0 0 18px; }}
    h2 {{ font-size:17px; margin:0 0 12px; }}
    p {{ color:var(--muted); margin:0 0 12px; }}
    section {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px; margin-bottom:14px; }}
    .actions {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:12px; }}
    button,.button {{ border:0; border-radius:7px; background:var(--accent); color:#fff; padding:9px 12px; font-weight:700; text-decoration:none; cursor:pointer; }}
    .secondary {{ background:#edf2f5; color:var(--ink); }}
    textarea {{ width:100%; min-height:430px; font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; border:1px solid var(--line); border-radius:8px; padding:12px; resize:vertical; }}
    input,select {{ width:100%; border:1px solid var(--line); border-radius:7px; padding:9px 10px; background:#fff; color:var(--ink); margin-top:6px; }}
    label {{ color:var(--muted); font-size:12px; font-weight:700; }}
    .form-grid {{ display:grid; grid-template-columns:2fr 2fr 1fr; gap:12px; }}
    pre {{ white-space:pre-wrap; background:#111827; color:#f9fafb; border-radius:8px; padding:14px; overflow:auto; }}
    table {{ width:100%; border-collapse:collapse; }}
    th,td {{ padding:10px 8px; border-bottom:1px solid var(--line); text-align:left; }}
    th {{ color:var(--muted); font-size:12px; }}
    .metrics {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; }}
    .metric {{ border:1px solid var(--line); border-radius:8px; padding:12px; }}
    .metric span {{ display:block; color:var(--muted); font-size:12px; }}
    .metric strong {{ display:block; margin-top:4px; font-size:16px; overflow-wrap:anywhere; }}
    .positive {{ color:var(--good); font-weight:700; }}
    .negative {{ color:var(--bad); font-weight:700; }}
    .curve {{ display:flex; align-items:end; gap:4px; min-height:180px; border:1px solid var(--line); border-radius:8px; padding:12px; overflow-x:auto; }}
    .bar {{ min-width:20px; background:var(--bar); border-top:3px solid var(--accent); border-radius:4px 4px 0 0; }}
    .bar.negative {{ background:#f3dada; border-color:var(--bad); }}
    .chart-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }}
    .chart-box {{ border:1px solid var(--line); border-radius:8px; padding:12px; min-width:0; overflow-x:auto; }}
    .chart-title {{ font-weight:750; font-size:13px; fill:var(--ink); }}
    .chart-axis {{ stroke:#cbd5df; stroke-width:1; }}
    .chart-line {{ fill:none; stroke:var(--accent); stroke-width:3; }}
    .chart-fill-good {{ fill:var(--accent); }}
    .chart-fill-bad {{ fill:var(--bad); }}
    .chart-fill-muted {{ fill:#9fb7ad; }}
    .chart-label {{ fill:var(--muted); font-size:11px; }}
    #market-candles {{ background:#0f1720; border:1px solid #263241; border-radius:8px; padding:10px; overflow-x:auto; }}
    #market-candles svg {{ display:block; width:100%; min-width:860px; }}
    .market-grid {{ stroke:#223041; stroke-width:1; }}
    .market-axis {{ stroke:#3a4656; stroke-width:1; }}
    .market-label {{ fill:#9aa8ba; font-size:12px; }}
    .market-title {{ fill:#e8eef6; font-weight:760; font-size:15px; }}
    .market-signal-label {{ fill:#0f1720; font-size:10px; font-weight:800; }}
    .market-decision-title {{ fill:#e5eef8; font-size:12px; font-weight:800; }}
    .market-decision-reason {{ fill:#a8bfd5; font-size:10px; }}
    .market-ma-fast {{ fill:none; stroke:#f2c94c; stroke-width:2.2; }}
    .market-ma-slow {{ fill:none; stroke:#56ccf2; stroke-width:2.2; }}
    .market-atr {{ fill:rgba(86,204,242,0.10); stroke:#335f72; stroke-width:1; }}
    .market-strip {{ display:grid; grid-template-columns:repeat(7,minmax(0,1fr)); gap:1px; border:1px solid var(--line); border-radius:8px; overflow:hidden; margin-bottom:12px; background:var(--line); }}
    .market-strip span {{ display:block; min-width:0; background:#fff; padding:9px 10px; }}
    .market-strip small {{ display:block; color:var(--muted); font-size:11px; font-weight:700; }}
    .market-strip strong {{ display:block; margin-top:2px; font-size:14px; overflow-wrap:anywhere; }}
    .market-toolbar {{ display:grid; grid-template-columns:2fr 1.2fr 0.8fr auto auto auto; gap:8px; align-items:end; margin-bottom:10px; }}
    .market-toolbar.compact {{ grid-template-columns:180px; margin-bottom:10px; }}
    .market-toolbar button {{ height:40px; margin-top:0; white-space:nowrap; }}
    .market-toolbar input,.market-toolbar select {{ margin-top:4px; }}
    .market-meta {{ display:flex; gap:12px; flex-wrap:wrap; color:var(--muted); font-size:12px; margin:0 0 10px; }}
    .market-meta strong {{ color:var(--ink); margin-left:4px; }}
    .decision-card {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; }}
    .decision-card > div {{ border:1px solid var(--line); border-radius:8px; padding:12px; min-width:0; }}
    .decision-card span {{ display:block; color:var(--muted); font-size:12px; font-weight:700; }}
    .decision-card strong {{ display:block; margin-top:4px; font-size:15px; overflow-wrap:anywhere; }}
    .decision-reasons {{ grid-column:1 / -1; }}
    .decision-reasons ul {{ margin:8px 0 0; padding-left:18px; }}
    .llm-trace {{ grid-column:1 / -1; }}
    .llm-trace details {{ margin-top:8px; }}
    .llm-trace summary {{ cursor:pointer; color:var(--ink); font-weight:750; }}
    .llm-trace pre {{ margin:8px 0 0; max-height:360px; font-size:12px; }}
    .decision-history {{ display:grid; gap:10px; }}
    .decision-history-item {{ border:1px solid var(--line); border-radius:8px; padding:12px; background:#fff; min-width:0; }}
    .decision-history-head {{ display:grid; grid-template-columns:1.2fr 0.75fr 0.75fr 0.75fr 0.9fr; gap:8px; align-items:start; }}
    .decision-history-head span,.decision-history-reasons span {{ display:block; color:var(--muted); font-size:11px; font-weight:750; }}
    .decision-history-head strong {{ display:block; margin-top:2px; font-size:14px; overflow-wrap:anywhere; }}
    .decision-history-reasons {{ margin-top:10px; padding-top:10px; border-top:1px solid var(--line); }}
    .decision-history-reasons ul {{ margin:6px 0 0; padding-left:18px; }}
    .decision-history-reasons li {{ margin:3px 0; overflow-wrap:anywhere; }}
    .market-legend {{ display:flex; flex-wrap:wrap; gap:14px; align-items:center; margin:0 0 10px; color:var(--muted); font-size:12px; }}
    .market-legend span {{ display:inline-flex; align-items:center; gap:6px; }}
    .market-legend i {{ width:18px; height:3px; display:inline-block; border-radius:3px; background:#9aa8ba; }}
    .legend-up {{ background:#16a06a !important; }}
    .legend-down {{ background:#d94c4c !important; }}
    .legend-ma-fast {{ background:#f2c94c !important; }}
    .legend-ma-slow {{ background:#56ccf2 !important; }}
    .legend-atr {{ background:#335f72 !important; }}
    .legend-entry {{ background:#6b8cff !important; }}
    .control-shell {{ display:grid; grid-template-columns:minmax(0,1.65fr) 390px; gap:14px; align-items:start; }}
    .control-main,.control-panel {{ border:1px solid var(--line); border-radius:8px; background:#fff; padding:14px; min-width:0; }}
    .control-header {{ display:flex; justify-content:space-between; gap:12px; align-items:start; margin-bottom:10px; }}
    .control-header p {{ margin:2px 0 0; }}
    .control-pill {{ display:inline-flex; align-items:center; min-height:28px; padding:5px 9px; border-radius:999px; background:#edf2f5; color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:0; }}
    .control-pill.live {{ background:#d9e8e2; color:var(--accent); }}
    .control-pill.error {{ background:#f3dada; color:var(--bad); }}
    .control-side {{ display:grid; gap:14px; }}
    .control-lower {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; background:transparent; border:0; padding:0; }}
    .control-lower .control-panel {{ overflow-x:auto; }}
    .control-lower .wide-panel {{ grid-column:1 / -1; }}
    .control-toolbar {{ grid-template-columns:2fr 1.1fr 0.75fr auto auto auto; }}
    .compact-strip {{ grid-template-columns:repeat(4,minmax(0,1fr)); }}
    .control-status-list {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; margin-bottom:10px; }}
    .control-status-list div {{ border:1px solid var(--line); border-radius:7px; padding:9px 10px; min-width:0; }}
    .control-status-list span {{ display:block; color:var(--muted); font-size:11px; font-weight:700; }}
    .control-status-list strong {{ display:block; margin-top:2px; font-size:14px; overflow-wrap:anywhere; }}
    #control-market-candles,#control-tick-chart {{ background:#0f1720; border:1px solid #263241; border-radius:8px; padding:10px; overflow-x:auto; }}
    #control-market-candles svg {{ display:block; width:100%; min-width:820px; }}
    #control-tick-chart svg {{ display:block; width:100%; min-width:360px; }}
    #control-decision-card .decision-card {{ grid-template-columns:1fr 1fr; }}
    #control-agent-controls .market-toolbar {{ grid-template-columns:1fr 0.8fr 0.8fr auto; }}
    @media (max-width:760px) {{ main {{ padding:16px; }} .metrics,.form-grid,.market-toolbar,.market-strip,.decision-card,.decision-history-head,.control-shell,.control-lower,.control-status-list {{ grid-template-columns:1fr; }} nav {{ padding:0 16px; }} nav div {{ overflow-x:auto; white-space:nowrap; }} }}
    @media (max-width:980px) {{ .market-toolbar,.control-toolbar {{ grid-template-columns:1fr 1fr; }} .market-strip,.compact-strip {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .control-shell,.control-lower {{ grid-template-columns:1fr; }} }}
    @media (max-width:980px) {{ .chart-grid {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
<nav><strong>Quantz</strong><div><a href="/control">Control</a><a href="/">Overview</a><a href="/agent">Agent</a><a href="/operations">Operations</a><a href="/pnl">PnL</a><a href="/market">Market</a><a href="/experiments">Experiments</a></div></nav>
<main><h1>{self._escape(title)}</h1>{body}</main>
{self._live_refresh_script()}
</body>
</html>"""

    def _settings_metrics(self, settings: dict[str, Any]) -> str:
        keys = [
            "mode",
            "market_source",
            "execution_source",
            "analyst",
            "llm_model",
            "allow_live_execution",
            "symbols",
            "position_cooldown",
            "paper_start_equity",
            "min_confidence",
            "reward_risk_ratio",
            "interval_seconds",
        ]
        return "".join(
            f'<div class="metric"><span>{self._escape(key)}</span><strong>{self._escape(settings.get(key, ""))}</strong></div>'
            for key in keys
        )

    def _performance_summary(self) -> dict[str, Any]:
        experiments = self._experiment_names()
        total_r = 0.0
        estimated_pnl = 0.0
        closed = 0
        open_positions = 0
        wins = 0
        losses = 0
        latest_verdict = "none"
        latest_status = "none"
        for name in experiments:
            root = self.experiments_dir / name
            report = self._read_json(root / "base-report.json")
            config = self._read_json(root / "base-config.json")
            review = self._read_json(root / "review.json")
            comparison = self._read_json(root / "comparison.json")
            run_r = float(report.get("total_r_multiple", 0.0))
            total_r += run_r
            estimated_pnl += self._estimated_pnl(run_r, config)
            closed += int(report.get("closed_position_count", 0) or 0)
            open_positions += int(report.get("open_position_count", 0) or 0)
            wins += int(report.get("win_count", 0) or 0)
            losses += int(report.get("loss_count", 0) or 0)
            latest_verdict = str(comparison.get("verdict", latest_verdict))
            latest_status = str(review.get("promotion_status", latest_status))
        total_outcomes = wins + losses
        return {
            "experiments": len(experiments),
            "paper_total_r": round(total_r, 4),
            "estimated_pnl": round(estimated_pnl, 2),
            "closed_positions": closed,
            "open_positions": open_positions,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / total_outcomes, 4) if total_outcomes else 0.0,
            "latest_verdict": latest_verdict,
            "latest_promotion_status": latest_status,
        }

    def _performance_metrics(self, performance: dict[str, Any]) -> str:
        items = [
            ("Experiments", performance.get("experiments", 0)),
            ("Paper PnL (R)", performance.get("paper_total_r", 0)),
            ("Est. PnL", self._money(performance.get("estimated_pnl", 0))),
            ("Win Rate", performance.get("win_rate", 0)),
            ("Closed Positions", performance.get("closed_positions", 0)),
            ("Open Positions", performance.get("open_positions", 0)),
            ("Latest Verdict", performance.get("latest_verdict", "none")),
            ("Promotion", performance.get("latest_promotion_status", "none")),
        ]
        return "".join(
            f'<div class="metric"><span>{self._escape(label)}</span><strong>{self._escape(value)}</strong></div>'
            for label, value in items
        )

    def _operations_summary(self) -> dict[str, Any]:
        settings = self._default_settings()
        paper_state_path = self._rooted_path(getattr(settings, "paper_state_path", "data/paper-state.json"))
        memory_path = self._rooted_path(getattr(settings, "memory_path", "data/experience.jsonl"))
        paper_state = self._read_json(paper_state_path)
        open_positions = [self._position_row(position) for position in paper_state.get("open_positions", [])]
        mt5_positions = (
            self._mt5_open_positions(getattr(settings, "symbols", []))
            if getattr(settings, "market_source", "demo") == "mt5"
            else []
        )
        closed_positions = [self._close_row(position) for position in paper_state.get("closed_positions", [])]
        experiences = self._read_jsonl(memory_path)
        recent_decisions = [self._decision_row(row) for row in experiences[-12:]][::-1]
        total_r = sum(float(position.get("r_multiple", 0.0) or 0.0) for position in closed_positions)
        wins = sum(1 for position in closed_positions if float(position.get("r_multiple", 0.0) or 0.0) > 0)
        losses = sum(1 for position in closed_positions if float(position.get("r_multiple", 0.0) or 0.0) < 0)
        return {
            "mode": getattr(settings, "mode", "paper"),
            "market_source": getattr(settings, "market_source", "demo"),
            "brain": self._agent_brain_label(settings),
            "symbols": getattr(settings, "symbols", []),
            "memory_path": str(memory_path.relative_to(self.root) if memory_path.is_relative_to(self.root) else memory_path),
            "paper_state_path": str(paper_state_path.relative_to(self.root) if paper_state_path.is_relative_to(self.root) else paper_state_path),
            "experience_count": len(experiences),
            "open_position_count": len(open_positions),
            "closed_position_count": len(closed_positions),
            "paper_total_r": round(total_r, 4),
            "estimated_pnl": round(self._estimated_pnl(total_r, settings_to_dict(settings)), 2),
            "win_rate": round(wins / (wins + losses), 4) if wins + losses else 0.0,
            "open_positions": open_positions,
            "mt5_open_positions": mt5_positions,
            "mt5_open_position_count": len(mt5_positions),
            "recent_closes": closed_positions[-10:][::-1],
            "recent_decisions": recent_decisions,
        }

    def _agent_console_summary(self) -> dict[str, Any]:
        config_name, settings = self._agent_settings()
        paper_state_path = self._rooted_path(getattr(settings, "paper_state_path", "data/paper-state.json"))
        memory_path = self._rooted_path(getattr(settings, "memory_path", "data/experience.jsonl"))
        paper_state = self._read_json(paper_state_path)
        experiences = self._read_jsonl(memory_path)
        decisions = [self._decision_row(row) for row in experiences[-20:]][::-1]
        latest = decisions[0] if decisions else {}
        open_positions = [self._position_row(position) for position in paper_state.get("open_positions", [])]
        mt5_positions = (
            self._mt5_open_positions(getattr(settings, "symbols", []))
            if getattr(settings, "market_source", "demo") == "mt5"
            else []
        )
        closed_positions = [self._close_row(position) for position in paper_state.get("closed_positions", [])]
        total_r = sum(float(position.get("r_multiple", 0.0) or 0.0) for position in closed_positions)
        wins = sum(1 for position in closed_positions if float(position.get("r_multiple", 0.0) or 0.0) > 0)
        losses = sum(1 for position in closed_positions if float(position.get("r_multiple", 0.0) or 0.0) < 0)
        monitor = self._monitor_summary()
        return {
            "config": config_name,
            "mode": getattr(settings, "mode", "paper"),
            "market_source": getattr(settings, "market_source", "demo"),
            "brain": self._agent_brain_label(settings),
            "symbols": getattr(settings, "symbols", []),
            "memory_path": str(memory_path.relative_to(self.root) if memory_path.is_relative_to(self.root) else memory_path),
            "paper_state_path": str(paper_state_path.relative_to(self.root) if paper_state_path.is_relative_to(self.root) else paper_state_path),
            "experience_count": len(experiences),
            "latest_decision": latest,
            "recent_decisions": decisions,
            "open_positions": open_positions,
            "open_position_count": len(open_positions),
            "mt5_open_positions": mt5_positions,
            "mt5_open_position_count": len(mt5_positions),
            "recent_closes": closed_positions[-10:][::-1],
            "closed_position_count": len(closed_positions),
            "paper_total_r": round(total_r, 4),
            "win_rate": round(wins / (wins + losses), 4) if wins + losses else 0.0,
            "monitor": monitor,
        }

    def _control_summary(self, include_chart: bool = False) -> dict[str, Any]:
        ticks = self._recent_ticks(limit=120)
        live_summary = self._tick_tape_summary(ticks)
        agent = self._agent_console_summary()
        payload = {
            "generated_at": self._now(),
            "generated_at_display": self._format_local_time(self._now()),
            "sequence": self.event_sequence,
            "stream": {
                "transport": "sse",
                "event_source": "/events",
                "browser_polling": False,
            },
            "live": {
                "collector": self._tick_collector_summary(),
                "summary": live_summary,
                "latest_tick": ticks[0] if ticks else {},
            },
            "ticks": ticks,
            "agent": agent,
            "operations": {
                "open_position_count": agent.get("open_position_count", 0),
                "closed_position_count": agent.get("closed_position_count", 0),
                "paper_total_r": agent.get("paper_total_r", 0),
                "win_rate": agent.get("win_rate", 0),
                "mt5_open_position_count": agent.get("mt5_open_position_count", 0),
                "mt5_open_positions": agent.get("mt5_open_positions", []),
                "recent_closes": agent.get("recent_closes", []),
            },
        }
        if include_chart:
            payload["chart"] = self._market_chart_summary()
        return payload

    def _pnl_summary(self) -> dict[str, Any]:
        settings = self._default_settings()
        paper_state_path = self._rooted_path(getattr(settings, "paper_state_path", "data/paper-state.json"))
        memory_path = self._rooted_path(getattr(settings, "memory_path", "data/experience.jsonl"))
        paper_state = self._read_json(paper_state_path)
        closed_positions = list(paper_state.get("closed_positions", []))
        experiences = self._read_jsonl(memory_path)
        risk_amount = self._risk_amount(settings_to_dict(settings))

        sorted_closes = sorted(closed_positions, key=lambda position: str(position.get("closed_at", "")))
        equity_curve = []
        cumulative_r = 0.0
        peak_r = 0.0
        max_drawdown_r = 0.0
        for index, position in enumerate(sorted_closes, start=1):
            r_multiple = float(position.get("r_multiple", 0.0) or 0.0)
            cumulative_r += r_multiple
            peak_r = max(peak_r, cumulative_r)
            max_drawdown_r = min(max_drawdown_r, cumulative_r - peak_r)
            equity_curve.append(
                {
                    "index": index,
                    "symbol": position.get("symbol", ""),
                    "closed_at": self._format_local_time(position.get("closed_at", "")),
                    "closed_at_raw": position.get("closed_at", ""),
                    "r_multiple": round(r_multiple, 4),
                    "cumulative_r": round(cumulative_r, 4),
                    "estimated_pnl": round(cumulative_r * risk_amount, 2),
                }
            )

        by_symbol = self._pnl_by_symbol(closed_positions, risk_amount)
        wins = [float(position.get("r_multiple", 0.0) or 0.0) for position in closed_positions if float(position.get("r_multiple", 0.0) or 0.0) > 0]
        losses = [float(position.get("r_multiple", 0.0) or 0.0) for position in closed_positions if float(position.get("r_multiple", 0.0) or 0.0) < 0]
        return {
            "closed_position_count": len(closed_positions),
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": round(len(wins) / len(closed_positions), 4) if closed_positions else 0.0,
            "paper_total_r": round(cumulative_r, 4),
            "estimated_pnl": round(cumulative_r * risk_amount, 2),
            "risk_amount": round(risk_amount, 2),
            "average_win_r": round(sum(wins) / len(wins), 4) if wins else 0.0,
            "average_loss_r": round(sum(losses) / len(losses), 4) if losses else 0.0,
            "max_drawdown_r": round(max_drawdown_r, 4),
            "max_drawdown_estimated": round(max_drawdown_r * risk_amount, 2),
            "by_symbol": by_symbol,
            "equity_curve": equity_curve,
            "decision_reasons": self._decision_reason_counts(experiences),
            "rejection_reasons": self._rejection_reason_counts(experiences),
            "reason_quality": self._reason_quality(experiences, closed_positions),
            "reason_profit": self._reason_profit(closed_positions),
        }

    def _pnl_by_symbol(self, closed_positions: list[dict[str, Any]], risk_amount: float) -> list[dict[str, Any]]:
        grouped: dict[str, list[float]] = {}
        for position in closed_positions:
            symbol = str(position.get("symbol", "unknown"))
            grouped.setdefault(symbol, []).append(float(position.get("r_multiple", 0.0) or 0.0))
        rows = []
        for symbol, values in grouped.items():
            wins = [value for value in values if value > 0]
            losses = [value for value in values if value < 0]
            total_r = sum(values)
            rows.append(
                {
                    "symbol": symbol,
                    "closed": len(values),
                    "wins": len(wins),
                    "losses": len(losses),
                    "win_rate": round(len(wins) / len(values), 4) if values else 0.0,
                    "average_r": round(total_r / len(values), 4) if values else 0.0,
                    "total_r": round(total_r, 4),
                    "estimated_pnl": round(total_r * risk_amount, 2),
                }
            )
        return sorted(rows, key=lambda row: float(row["total_r"]), reverse=True)

    def _pnl_metrics(self, pnl: dict[str, Any]) -> str:
        items = [
            ("Closed Trades", pnl.get("closed_position_count", 0)),
            ("Paper PnL (R)", pnl.get("paper_total_r", 0)),
            ("Est. PnL", self._money(pnl.get("estimated_pnl", 0))),
            ("Risk / Trade", self._money(pnl.get("risk_amount", 0))),
            ("Win Rate", pnl.get("win_rate", 0)),
            ("Avg Win R", pnl.get("average_win_r", 0)),
            ("Avg Loss R", pnl.get("average_loss_r", 0)),
            ("Max DD R", pnl.get("max_drawdown_r", 0)),
        ]
        return "".join(
            f'<div class="metric"><span>{self._escape(label)}</span><strong>{self._escape(value)}</strong></div>'
            for label, value in items
        )

    def _visual_digest(self) -> dict[str, Any]:
        pnl = self._pnl_summary()
        operations = self._operations_summary()
        monitor = self._monitor_summary()
        best_symbols = pnl["by_symbol"][:3]
        weak_reasons = [
            row
            for row in pnl["reason_quality"]
            if row["count"] >= 3 and row["approved_rate"] < 0.5
        ][:5]
        return {
            "purpose": "structured_visual_digest_for_agent_review",
            "summary": {
                "monitor_status": monitor.get("status"),
                "open_positions": operations.get("open_position_count", 0),
                "closed_positions": pnl.get("closed_position_count", 0),
                "paper_total_r": pnl.get("paper_total_r", 0),
                "estimated_pnl": pnl.get("estimated_pnl", 0),
                "win_rate": pnl.get("win_rate", 0),
                "max_drawdown_r": pnl.get("max_drawdown_r", 0),
            },
            "charts": {
                "equity_curve": pnl["equity_curve"],
                "symbol_total_r": pnl["by_symbol"],
                "decision_reasons": pnl["decision_reasons"],
                "rejection_reasons": pnl["rejection_reasons"],
                "reason_quality": pnl["reason_quality"],
                "reason_profit": pnl["reason_profit"],
                "market_chart": self._market_chart_summary(),
            },
            "agent_read": {
                "best_symbols": best_symbols,
                "weak_reasons": weak_reasons,
                "notes": self._visual_digest_notes(pnl, weak_reasons),
            },
        }

    def _visual_digest_notes(self, pnl: dict[str, Any], weak_reasons: list[dict[str, Any]]) -> list[str]:
        notes = []
        if pnl.get("closed_position_count", 0) < 20:
            notes.append("sample_size_too_small_for_live_promotion")
        if pnl.get("max_drawdown_r", 0) < -3:
            notes.append("drawdown_requires_risk_review")
        if weak_reasons:
            notes.append("some_reason_codes_have_low_approval_rate")
        if not notes:
            notes.append("continue_collecting_paper_evidence")
        return notes

    def _market_chart_summary(self, timeframe: str = "H1", candle_count: int = 80) -> dict[str, Any]:
        safe_timeframe = self._safe_timeframe(timeframe)
        safe_candle_count = max(20, min(int(candle_count or 80), 300))
        live_config_name, live_settings = self._live_market_settings()
        if getattr(live_settings, "market_source", "demo") == "mt5":
            try:
                mt5_summary = self._mt5_market_chart_summary(live_config_name, live_settings, safe_timeframe, safe_candle_count)
                if mt5_summary.get("status") == "connected":
                    return mt5_summary
            except Exception:
                pass

        settings = self._default_settings()
        state_path = self._rooted_path(getattr(settings, "sim_state_path", "data/sim-market-state.json"))
        paper_state_path = self._rooted_path(getattr(settings, "paper_state_path", "data/paper-state.json"))
        state = self._read_json(state_path)
        paper_state = self._read_json(paper_state_path)
        experiences = self._read_jsonl(self._rooted_path(getattr(settings, "memory_path", "data/experience.jsonl")))
        series: dict[str, list[dict[str, Any]]] = {}
        overlays: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for symbol in getattr(settings, "symbols", []):
            symbol_state = state.get(symbol.upper(), {})
            history = list(symbol_state.get("history", [])) if isinstance(symbol_state, dict) else []
            if not history and isinstance(symbol_state, dict) and "mid" in symbol_state:
                mid = float(symbol_state.get("mid", 0.0) or 0.0)
                history = [{"step": symbol_state.get("step", 0), "open": mid, "high": mid, "low": mid, "close": mid}]
            series[symbol] = history[-80:]
            overlays[symbol] = {
                "indicators": self._market_indicators(history[-80:]),
                "signals": self._market_signals(experiences, symbol),
                "open_positions": [
                    position
                    for position in paper_state.get("open_positions", [])
                    if position.get("symbol") == symbol
                ],
                "closed_positions": [
                    position
                    for position in paper_state.get("closed_positions", [])[-80:]
                    if position.get("symbol") == symbol
                ],
            }
        return {
            "source": "simulated_ohlc_history",
            "timeframe": "sim",
            "state_path": str(state_path.relative_to(self.root) if state_path.is_relative_to(self.root) else state_path),
            "symbols": list(getattr(settings, "symbols", [])),
            "series": series,
            "overlays": overlays,
        }

    def _mt5_market_chart_summary(self, config_name: str, settings: Any, timeframe_label: str, candle_count: int) -> dict[str, Any]:
        from datetime import datetime, timezone

        connection = Mt5Connection()
        connection.initialize()
        mt5 = connection.mt5
        timeframe = self._mt5_timeframe(mt5, timeframe_label)
        paper_state_path = self._rooted_path(getattr(settings, "paper_state_path", "data/paper-state.json"))
        paper_state = self._read_json(paper_state_path)
        experiences = self._read_jsonl(self._rooted_path(getattr(settings, "memory_path", "data/experience.jsonl")))
        series: dict[str, list[dict[str, Any]]] = {}
        overlays: dict[str, dict[str, list[dict[str, Any]]]] = {}

        for symbol in getattr(settings, "symbols", []):
            info = mt5.symbol_info(symbol)
            if info is None:
                raise RuntimeError(f"mt5_symbol_not_found:{symbol}")
            if not getattr(info, "visible", True) and not mt5.symbol_select(symbol, True):
                raise RuntimeError(f"mt5_symbol_select_failed:{symbol}")

            tick = mt5.symbol_info_tick(symbol)
            point = float(getattr(info, "point", 0.0) or 0.0)
            rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, candle_count)
            history = []
            digits = int(getattr(info, "digits", 5) or 5)
            if rates is not None:
                for index, rate in enumerate(list(rates), start=1):
                    timestamp = datetime.fromtimestamp(int(rate["time"]), timezone.utc).isoformat()
                    try:
                        tick_volume = int(rate["tick_volume"])
                    except (KeyError, TypeError, ValueError):
                        tick_volume = 0
                    history.append(
                        {
                            "time": timestamp,
                            "step": index,
                            "open": round(float(rate["open"]), digits),
                            "high": round(float(rate["high"]), digits),
                            "low": round(float(rate["low"]), digits),
                            "close": round(float(rate["close"]), digits),
                            "tick_volume": tick_volume,
                        }
                    )
            series[symbol] = history[-candle_count:]
            current_tick = {}
            ea_tick = self._latest_tick_for_symbol(symbol)
            if ea_tick:
                current_tick = {
                    "bid": ea_tick.get("bid", ""),
                    "ask": ea_tick.get("ask", ""),
                    "mid": ea_tick.get("mid", ""),
                    "spread_points": ea_tick.get("spread_points", ""),
                    "time": ea_tick.get("tick_time", ""),
                    "source": ea_tick.get("source", "ea_socket"),
                }
                if history:
                    mid = float(ea_tick.get("mid", 0.0) or 0.0)
                    if mid > 0:
                        history[-1]["close"] = round(mid, digits)
                        history[-1]["high"] = round(max(float(history[-1].get("high", mid) or mid), mid), digits)
                        history[-1]["low"] = round(min(float(history[-1].get("low", mid) or mid), mid), digits)
                    series[symbol] = history[-candle_count:]
            elif tick is not None:
                bid = float(getattr(tick, "bid", 0.0) or 0.0)
                ask = float(getattr(tick, "ask", 0.0) or 0.0)
                spread_points = round((ask - bid) / point, 2) if point > 0 else 0.0
                current_tick = {
                    "bid": round(bid, digits),
                    "ask": round(ask, digits),
                    "mid": round((bid + ask) / 2, digits),
                    "spread_points": spread_points,
                    "time": int(getattr(tick, "time", 0) or 0),
                    "source": "mt5_python",
                }
            overlays[symbol] = {
                "indicators": self._market_indicators(history[-candle_count:]),
                "current_tick": current_tick,
                "signals": self._market_signals(experiences, symbol),
                "open_positions": [
                    position
                    for position in paper_state.get("open_positions", [])
                    if position.get("symbol") == symbol
                ],
                "closed_positions": [
                    position
                    for position in paper_state.get("closed_positions", [])[-80:]
                    if position.get("symbol") == symbol
                ],
            }

        chart_source = "mt5_ohlc_history"
        if any((overlay.get("current_tick") or {}).get("source") == "ea_socket" for overlay in overlays.values()):
            chart_source = "mt5_ohlc_history + ea_socket_live_tick"
        return {
            "source": chart_source,
            "status": "connected",
            "config": config_name,
            "timeframe": timeframe_label,
            "candle_count": candle_count,
            "state_path": "MetaTrader5.copy_rates_from_pos",
            "symbols": list(getattr(settings, "symbols", [])),
            "series": series,
            "overlays": overlays,
        }

    def _live_market_summary(self) -> dict[str, Any]:
        try:
            payload = self._read_live_market()
            if payload.get("status") == "connected":
                payload["recent_ticks"] = self._recent_ticks()
            else:
                payload["recent_ticks"] = self._recent_ticks()
            return payload
        except Exception as exc:
            config_name, _settings = self._live_market_settings()
            return {
                "enabled": True,
                "status": "error",
                "config": config_name,
                "source": "mt5",
                "updated_at": self._now(),
                "updated_at_display": self._format_local_time(self._now()),
                "account": {},
                "ticks": [],
                "recent_ticks": self._recent_ticks(),
                "error": str(exc),
            }

    def _read_live_market(self) -> dict[str, Any]:
        config_name, settings = self._live_market_settings()
        if getattr(settings, "market_source", "demo") != "mt5":
            return {
                "enabled": False,
                "status": "disabled",
                "config": config_name,
                "source": getattr(settings, "market_source", "demo"),
                "updated_at": self._now(),
                "updated_at_display": self._format_local_time(self._now()),
                "account": {},
                "ticks": [],
                "recent_ticks": self._recent_ticks(),
                "error": "selected config is not using market_source: mt5",
            }

        connection = Mt5Connection()
        market_feed = Mt5MarketFeed(connection)
        account = Mt5AccountFeed(connection).state()
        ticks = []
        for symbol in getattr(settings, "symbols", []):
            snapshot = market_feed.snapshot(symbol)
            ticks.append(
                {
                    "symbol": snapshot.symbol,
                    "bid": snapshot.bid,
                    "ask": snapshot.ask,
                    "mid": round(snapshot.mid, 6),
                    "spread_points": snapshot.spread_points,
                    "atr_points": snapshot.atr_points,
                    "trend_score": snapshot.trend_score,
                    "volatility_score": snapshot.volatility_score,
                    "session": snapshot.session,
                    "tick_time": snapshot.features.get("last_tick_time", ""),
                    "tick_time_display": self._format_local_time(snapshot.features.get("last_tick_time", "")),
                    "rates_loaded": snapshot.features.get("rates_loaded", 0),
                }
            )
        return {
            "enabled": True,
            "status": "connected",
            "config": config_name,
            "source": "mt5",
            "updated_at": self._now(),
            "updated_at_display": self._format_local_time(self._now()),
            "account": {
                "balance": account.balance,
                "equity": account.equity,
                "free_margin": account.free_margin,
                "currency": account.currency,
                "open_positions": account.open_positions,
            },
            "ticks": ticks,
            "recent_ticks": [],
            "error": None,
        }

    def start_tick_collector(self, interval_seconds: float = 1.0) -> None:
        if self.tick_collector_thread and self.tick_collector_thread.is_alive():
            return
        stop_event = threading.Event()
        self.tick_collector_stop_event = stop_event
        self.tick_collector_thread = threading.Thread(
            target=self._tick_collector_target,
            args=(max(0.25, interval_seconds), stop_event),
            daemon=True,
            name="quantz-mt5-tick-collector",
        )
        self.tick_collector_thread.start()

    def _tick_collector_target(self, interval_seconds: float, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            if stop_event.wait(interval_seconds):
                break

    def stop_tick_collector(self) -> None:
        if self.tick_collector_stop_event is not None:
            self.tick_collector_stop_event.set()
        if self.tick_collector_thread is not None:
            self.tick_collector_thread.join(timeout=2)
        self.tick_collector_stop_event = None
        self.tick_collector_thread = None

    def _live_market_metrics(self, live: dict[str, Any]) -> str:
        account = live.get("account", {})
        items = [
            ("Status", live.get("status", "")),
            ("Config", live.get("config", "")),
            ("Source", live.get("source", "")),
            ("Updated", live.get("updated_at", "")),
            ("Equity", self._money(account.get("equity", 0)) if account else ""),
            ("Free Margin", self._money(account.get("free_margin", 0)) if account else ""),
            ("Open Positions", account.get("open_positions", "") if account else ""),
            ("Error", live.get("error") or ""),
        ]
        return "".join(
            f'<div class="metric"><span>{self._escape(label)}</span><strong>{self._escape(value)}</strong></div>'
            for label, value in items
        )

    def _market_live_strip(self, live: dict[str, Any]) -> str:
        account = live.get("account", {})
        ticks = live.get("ticks", [])
        tick = ticks[0] if ticks else {}
        items = [
            ("Status", live.get("status", "")),
            ("Bid", tick.get("bid", "")),
            ("Ask", tick.get("ask", "")),
            ("Spread", tick.get("spread_points", "")),
            ("Equity", self._money(account.get("equity", 0)) if account else ""),
            ("Open", account.get("open_positions", "") if account else ""),
            ("Updated", live.get("updated_at_display") or self._format_local_time(live.get("updated_at", ""))),
        ]
        return "".join(
            f'<span><small>{self._escape(label)}</small><strong>{self._escape(value)}</strong></span>'
            for label, value in items
        )

    def _control_live_strip(self, summary: dict[str, Any]) -> str:
        live = summary.get("live", {})
        tick = live.get("latest_tick", {})
        tick_summary = live.get("summary", {})
        agent = summary.get("agent", {})
        latest = agent.get("latest_decision", {})
        items = [
            ("Symbol", tick_summary.get("latest_symbol", "")),
            ("Bid", tick_summary.get("latest_bid", "")),
            ("Ask", tick_summary.get("latest_ask", "")),
            ("Spread", tick_summary.get("latest_spread", "")),
            ("Source", tick_summary.get("latest_source_label", "")),
            ("Tick Time", tick.get("tick_time_display", "")),
            ("Agent", latest.get("action", "none")),
            ("Risk", latest.get("risk_status", "")),
        ]
        return "".join(
            f'<span><small>{self._escape(label)}</small><strong>{self._escape(value)}</strong></span>'
            for label, value in items
        )

    def _control_tick_status(self, summary: dict[str, Any]) -> str:
        live = summary.get("live", {})
        collector = live.get("collector", {})
        tick_summary = live.get("summary", {})
        items = [
            ("Source", tick_summary.get("latest_source_label") or collector.get("active_source_label", "")),
            ("Collector", "polling" if collector.get("polling_active") else "suppressed" if collector.get("polling_suppressed_by_ea") else "stopped"),
            ("Tape", tick_summary.get("count", 0)),
            ("Ticks/min", tick_summary.get("ticks_per_minute", 0)),
            ("Age", "" if tick_summary.get("latest_age_seconds") is None else f"{tick_summary.get('latest_age_seconds')}s"),
            ("Delta", tick_summary.get("bid_delta", 0)),
            ("Spread", f"{tick_summary.get('spread_min', 0)} / {tick_summary.get('spread_max', 0)}"),
        ]
        return "".join(
            f'<div><span>{self._escape(label)}</span><strong>{self._escape(value)}</strong></div>'
            for label, value in items
        )

    def _live_market_table(self, ticks: list[dict[str, Any]]) -> str:
        if not ticks:
            return "<p>No MT5 tick data yet.</p>"
        rows = []
        for tick in ticks:
            rows.append(
                "<tr>"
                f"<td>{self._escape(tick.get('symbol', ''))}</td>"
                f"<td>{self._escape(tick.get('bid', ''))}</td>"
                f"<td>{self._escape(tick.get('ask', ''))}</td>"
                f"<td>{self._escape(tick.get('mid', ''))}</td>"
                f"<td>{self._escape(tick.get('spread_points', ''))}</td>"
                f"<td>{self._escape(tick.get('atr_points', ''))}</td>"
                f"<td>{self._escape(tick.get('trend_score', ''))}</td>"
                f"<td>{self._escape(tick.get('volatility_score', ''))}</td>"
                f"<td>{self._escape(tick.get('tick_time_display') or self._format_local_time(tick.get('tick_time', '')))}</td>"
                "</tr>"
            )
        return "<table><thead><tr><th>Symbol</th><th>Bid</th><th>Ask</th><th>Mid</th><th>Spread</th><th>ATR</th><th>Trend</th><th>Volatility</th><th>Tick Time</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"

    def _tick_tape_table(self, ticks: list[dict[str, Any]]) -> str:
        if not ticks:
            return "<p>No tick tape yet.</p>"
        rows = []
        for tick in ticks[:20]:
            rows.append(
                "<tr>"
                f"<td>{self._escape(tick.get('captured_at_display') or self._format_local_time(tick.get('captured_at', '')))}</td>"
                f"<td>{self._escape(tick.get('symbol', ''))}</td>"
                f"<td>{self._escape(tick.get('bid', ''))}</td>"
                f"<td>{self._escape(tick.get('ask', ''))}</td>"
                f"<td>{self._escape(tick.get('mid', ''))}</td>"
                f"<td>{self._escape(tick.get('spread_points', ''))}</td>"
                f"<td>{self._escape(tick.get('tick_time_display') or self._format_local_time(tick.get('tick_time', '')))}</td>"
                "</tr>"
            )
        return "<table><thead><tr><th>Captured</th><th>Symbol</th><th>Bid</th><th>Ask</th><th>Mid</th><th>Spread</th><th>Tick Time</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"

    def _tick_tape_summary(self, ticks: list[dict[str, Any]]) -> dict[str, Any]:
        if not ticks:
            return {
                "count": 0,
                "latest_symbol": "",
                "latest_bid": "",
                "latest_ask": "",
                "latest_spread": "",
                "latest_captured_at": "",
                "latest_captured_at_display": "",
                "latest_source": "",
                "latest_source_label": "",
                "bid_delta": 0.0,
                "spread_min": 0.0,
                "spread_max": 0.0,
                "latest_age_seconds": None,
                "ticks_per_minute": 0,
            }
        latest = ticks[0]
        oldest = ticks[-1]
        spreads = [float(tick.get("spread_points", 0.0) or 0.0) for tick in ticks]
        latest_bid = float(latest.get("bid", 0.0) or 0.0)
        oldest_bid = float(oldest.get("bid", 0.0) or 0.0)
        latest_captured_at = str(latest.get("captured_at", ""))
        latest_age_seconds = self._seconds_since(latest_captured_at)
        ticks_per_minute = sum(1 for tick in ticks if self._seconds_since(str(tick.get("captured_at", ""))) <= 60)
        return {
            "count": len(ticks),
            "latest_symbol": latest.get("symbol", ""),
            "latest_bid": latest.get("bid", ""),
            "latest_ask": latest.get("ask", ""),
            "latest_spread": latest.get("spread_points", ""),
            "latest_captured_at": latest_captured_at,
            "latest_captured_at_display": self._format_local_time(latest_captured_at),
            "latest_source": latest.get("source", ""),
            "latest_source_label": latest.get("source_label") or self._tick_source_label(latest.get("source", "")),
            "bid_delta": round(latest_bid - oldest_bid, 6),
            "spread_min": round(min(spreads), 4) if spreads else 0.0,
            "spread_max": round(max(spreads), 4) if spreads else 0.0,
            "latest_age_seconds": latest_age_seconds,
            "ticks_per_minute": ticks_per_minute,
        }

    def _tick_collector_summary(self) -> dict[str, Any]:
        running = bool(self.tick_collector_thread and self.tick_collector_thread.is_alive())
        ea_active = self._ea_socket_active()
        return {
            "running": running,
            "polling_active": False,
            "polling_suppressed_by_ea": True,
            "active_source": "ea_socket",
            "active_source_label": self._tick_source_label("ea_socket"),
            "last_ea_tick_at": self.last_ea_tick_at.isoformat() if self.last_ea_tick_at else "",
            "last_ea_tick_at_display": self._format_local_time(self.last_ea_tick_at.isoformat()) if self.last_ea_tick_at else "",
            "thread": self.tick_collector_thread.name if self.tick_collector_thread else "",
            "tape_path": str(self.tick_tape_path.relative_to(self.root) if self.tick_tape_path.is_relative_to(self.root) else self.tick_tape_path),
        }

    def _seconds_since(self, iso_timestamp: str) -> float:
        if not iso_timestamp:
            return 0.0
        try:
            parsed = self._parse_datetime(iso_timestamp)
            if parsed is None:
                return 0.0
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return round(max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds()), 3)
        except ValueError:
            return 0.0

    def _ea_socket_active(self, threshold_seconds: float = 5.0) -> bool:
        if self.last_ea_tick_at is None:
            return False
        return (datetime.now(timezone.utc) - self.last_ea_tick_at.astimezone(timezone.utc)).total_seconds() <= threshold_seconds

    def _tick_source_label(self, source: Any) -> str:
        value = str(source or "")
        if value == "ea_socket":
            return "EA socket"
        if value == "python_polling":
            return "Python polling"
        if value == "bridge_http":
            return "HTTP bridge"
        return value or "unknown"

    def _tick_tape_metrics(self, ticks: list[dict[str, Any]]) -> str:
        summary = self._tick_tape_summary(ticks)
        collector = self._tick_collector_summary()
        items = [
            ("Source", summary.get("latest_source_label") or collector.get("active_source_label", "")),
            ("Collector", "polling" if collector.get("polling_active") else "suppressed" if collector.get("polling_suppressed_by_ea") else "stopped"),
            ("Ticks", summary["count"]),
            ("Ticks/min", summary["ticks_per_minute"]),
            ("Tick Age", "" if summary["latest_age_seconds"] is None else f"{summary['latest_age_seconds']}s"),
            ("Latest Symbol", summary["latest_symbol"]),
            ("Latest Bid", summary["latest_bid"]),
            ("Latest Ask", summary["latest_ask"]),
            ("Latest Spread", summary["latest_spread"]),
        ]
        return "".join(
            f'<span><small>{self._escape(label)}</small><strong>{self._escape(value)}</strong></span>'
            for label, value in items
        )

    def _tick_tape_chart(self, ticks: list[dict[str, Any]]) -> str:
        if len(ticks) < 2:
            return "<p>No tick chart yet.</p>"
        chronological = list(reversed(ticks[-120:]))
        bids = [float(tick.get("bid", 0.0) or 0.0) for tick in chronological]
        asks = [float(tick.get("ask", 0.0) or 0.0) for tick in chronological]
        spreads = [float(tick.get("spread_points", 0.0) or 0.0) for tick in chronological]
        prices = bids + asks
        upper = max(prices)
        lower = min(prices)
        price_span = upper - lower or 1.0
        spread_upper = max(spreads) if spreads else 1.0
        spread_span = spread_upper or 1.0
        width, height = 1080, 260
        left, right, top, bottom = 58, 78, 34, 44
        plot_width = width - left - right
        plot_height = height - top - bottom
        count = max(len(chronological) - 1, 1)

        def point(index: int, value: float) -> str:
            x = left + (index / count) * plot_width
            y = top + ((upper - value) / price_span) * plot_height
            return f"{x:.2f},{y:.2f}"

        def spread_point(index: int, value: float) -> str:
            x = left + (index / count) * plot_width
            y = top + plot_height - (value / spread_span) * min(52, plot_height * 0.35)
            return f"{x:.2f},{y:.2f}"

        bid_points = " ".join(point(index, value) for index, value in enumerate(bids))
        ask_points = " ".join(point(index, value) for index, value in enumerate(asks))
        spread_points = " ".join(spread_point(index, value) for index, value in enumerate(spreads))
        latest = chronological[-1]
        return f"""
        <svg viewBox="0 0 {width} {height}" role="img" aria-label="Tick tape bid ask spread chart">
          <rect x="0" y="0" width="{width}" height="{height}" fill="#0f1720"></rect>
          <text x="{left}" y="22" class="market-title">Tick Tape</text>
          <line x1="{left}" y1="{top + plot_height}" x2="{width - right}" y2="{top + plot_height}" class="market-grid"></line>
          <line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" class="market-axis"></line>
          <polyline points="{bid_points}" fill="none" stroke="#22c55e" stroke-width="2.4"></polyline>
          <polyline points="{ask_points}" fill="none" stroke="#ef4444" stroke-width="1.8" stroke-dasharray="5 4"></polyline>
          <polyline points="{spread_points}" fill="none" stroke="#f2c94c" stroke-width="1.6"></polyline>
          <text x="{width - right - 180}" y="22" class="market-label">bid {self._escape(latest.get('bid', ''))} / ask {self._escape(latest.get('ask', ''))}</text>
          <text x="{left}" y="{height - 14}" class="market-label">{len(chronological)} ticks, spread max {max(spreads):.2f}</text>
        </svg>
        """

    def _record_live_ticks(self, ticks: list[dict[str, Any]], return_recent: bool = True, source: str = "python_polling") -> list[dict[str, Any]]:
        if not ticks:
            return self._recent_ticks()
        created = False
        new_rows = []
        with self.tick_tape_lock:
            captured_at = self._now()
            for tick in ticks:
                row = {
                    "captured_at": captured_at,
                    "captured_at_display": self._format_local_time(captured_at),
                    "symbol": tick.get("symbol", ""),
                    "bid": tick.get("bid", ""),
                    "ask": tick.get("ask", ""),
                    "mid": tick.get("mid", ""),
                    "spread_points": tick.get("spread_points", ""),
                    "tick_time": tick.get("tick_time", ""),
                    "tick_time_display": self._format_local_time(tick.get("tick_time", "")),
                    "source": tick.get("source") or source,
                    "source_label": self._tick_source_label(tick.get("source") or source),
                }
                key = (str(row["symbol"]), str(row["tick_time"]), str(row["bid"]), str(row["ask"]))
                if key in self.tick_recent_keys:
                    continue
                self.tick_recent_keys.add(key)
                new_rows.append(row)
            if len(self.tick_recent_keys) > 1000:
                self.tick_recent_keys = set(list(self.tick_recent_keys)[-500:])
            if new_rows:
                self.tick_tape_path.parent.mkdir(parents=True, exist_ok=True)
                with self.tick_tape_path.open("a", encoding="utf-8") as handle:
                    for row in new_rows:
                        handle.write(json.dumps(row, sort_keys=True) + "\n")
                created = True
            recent = self._recent_ticks_unlocked(limit=50) if return_recent else list(reversed(new_rows))
            if created and source == "ea_socket":
                self.last_ea_tick_at = datetime.now(timezone.utc)
            if created and source == "python_polling":
                self.last_python_poll_at = datetime.now(timezone.utc)
        if created:
            self._notify_event_stream()
        return recent

    def _ingest_bridge_tick(self, body: str) -> dict[str, Any]:
        try:
            payload = json.loads(body or "{}")
        except json.JSONDecodeError as exc:
            return {"status": "error", "error": f"invalid_json:{exc.msg}"}
        rows = payload if isinstance(payload, list) else [payload]
        ticks = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol", "")).upper()
            if not symbol:
                continue
            try:
                bid = float(row.get("bid", 0.0) or 0.0)
                ask = float(row.get("ask", 0.0) or 0.0)
                point = float(row.get("point", 0.0) or 0.0)
                digits = int(row.get("digits", 6) or 6)
            except (TypeError, ValueError):
                continue
            spread_points = row.get("spread_points")
            if spread_points is None:
                spread_points = round((ask - bid) / point, 2) if point > 0 else round(ask - bid, 6)
            ticks.append(
                {
                    "symbol": symbol,
                    "bid": round(bid, digits),
                    "ask": round(ask, digits),
                    "mid": round((bid + ask) / 2, digits),
                    "spread_points": spread_points,
                    "tick_time": row.get("tick_time") or row.get("time") or self._now(),
                    "source": "ea_socket",
                }
            )
        if not ticks:
            return {"status": "error", "error": "no_valid_ticks"}
        self.last_ea_tick_at = datetime.now(timezone.utc)
        recent = self._record_live_ticks(ticks, return_recent=False, source="ea_socket")
        return {
            "status": "accepted",
            "received": len(ticks),
            "latest_tick": recent[0] if recent else {},
            "sequence": self.event_sequence,
        }

    def _ingest_bridge_tick_query(self, query: str) -> dict[str, Any]:
        params = parse_qs(query)

        def value(name: str, default: Any = "") -> Any:
            values = params.get(name)
            return values[0] if values else default

        payload = {
            "symbol": value("symbol"),
            "bid": value("bid", 0),
            "ask": value("ask", 0),
            "point": value("point", 0),
            "digits": value("digits", 6),
            "spread_points": value("spread_points", None),
            "tick_time": value("tick_time", self._now()),
        }
        return self._ingest_bridge_tick(json.dumps(payload))

    def _recent_ticks(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.tick_tape_lock:
            return self._recent_ticks_unlocked(limit=limit)

    def _latest_tick_for_symbol(self, symbol: str) -> dict[str, Any]:
        wanted = str(symbol).upper()
        for tick in self._recent_ticks(limit=250):
            if str(tick.get("symbol", "")).upper() == wanted and str(tick.get("source", "")) == "ea_socket":
                return tick
        return {}

    def _recent_ticks_unlocked(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.tick_tape_path.exists():
            return []
        rows: deque[dict[str, Any]] = deque(maxlen=limit)
        with self.tick_tape_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped:
                    row = json.loads(stripped)
                    row.setdefault("captured_at_display", self._format_local_time(row.get("captured_at", "")))
                    row.setdefault("tick_time_display", self._format_local_time(row.get("tick_time", "")))
                    row.setdefault("source", "unknown")
                    row.setdefault("source_label", self._tick_source_label(row.get("source", "")))
                    rows.append(row)
        return list(rows)[::-1]

    def _market_indicators(self, candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        indicators = []
        closes = [float(candle.get("close", 0.0) or 0.0) for candle in candles]
        ranges = [
            abs(float(candle.get("high", candle.get("close", 0.0)) or 0.0) - float(candle.get("low", candle.get("close", 0.0)) or 0.0))
            for candle in candles
        ]
        for index, candle in enumerate(candles):
            fast_window = closes[max(0, index - 2) : index + 1]
            slow_window = closes[max(0, index - 7) : index + 1]
            atr_window = ranges[max(0, index - 4) : index + 1]
            ma_fast = sum(fast_window) / len(fast_window) if fast_window else 0.0
            ma_slow = sum(slow_window) / len(slow_window) if slow_window else 0.0
            atr = sum(atr_window) / len(atr_window) if atr_window else 0.0
            close = float(candle.get("close", 0.0) or 0.0)
            indicators.append(
                {
                    "step": candle.get("step", index + 1),
                    "ma_fast": round(ma_fast, 6),
                    "ma_slow": round(ma_slow, 6),
                    "atr_upper": round(close + atr, 6),
                    "atr_lower": round(close - atr, 6),
                }
            )
        return indicators

    def _market_signals(self, experiences: list[dict[str, Any]], symbol: str) -> list[dict[str, Any]]:
        signals = []
        for row in experiences:
            decision = row.get("decision", {})
            if decision.get("symbol") != symbol:
                continue
            signals.append(
                {
                    "timestamp": decision.get("timestamp", ""),
                    "action": decision.get("action", ""),
                    "side": decision.get("side", ""),
                    "confidence": decision.get("confidence", 0),
                    "reason_codes": list(decision.get("reason_codes", [])),
                    "risk_status": row.get("risk", {}).get("status", ""),
                    "executed": (row.get("execution") or {}).get("accepted") is True,
                }
            )
        return signals[-24:]

    def _operations_metrics(self, operations: dict[str, Any]) -> str:
        items = [
            ("Mode", operations.get("mode", "paper")),
            ("Market Source", operations.get("market_source", "demo")),
            ("Brain", operations.get("brain", "")),
            ("Symbols", ", ".join(operations.get("symbols", []))),
            ("Experiences", operations.get("experience_count", 0)),
            ("Open Positions", operations.get("open_position_count", 0)),
            ("MT5 Open", operations.get("mt5_open_position_count", 0)),
            ("Closed Positions", operations.get("closed_position_count", 0)),
            ("Paper PnL (R)", operations.get("paper_total_r", 0)),
            ("Est. PnL", self._money(operations.get("estimated_pnl", 0))),
            ("Win Rate", operations.get("win_rate", 0)),
            ("Memory", operations.get("memory_path", "")),
            ("Paper State", operations.get("paper_state_path", "")),
        ]
        return "".join(
            f'<div class="metric"><span>{self._escape(label)}</span><strong>{self._escape(value)}</strong></div>'
            for label, value in items
        )

    def _agent_status_strip(self, console: dict[str, Any]) -> str:
        monitor = console.get("monitor", {})
        latest = console.get("latest_decision", {})
        items = [
            ("Monitor", monitor.get("status", "stopped")),
            ("Trigger", monitor.get("trigger_mode", "interval")),
            ("Config", console.get("config", "")),
            ("Mode", console.get("mode", "")),
            ("Brain", console.get("brain", "")),
            ("Market", console.get("market_source", "")),
            ("Symbol", ", ".join(console.get("symbols", []))),
            ("MT5 Open", console.get("mt5_open_position_count", 0)),
            ("Latest", latest.get("action", "none")),
            ("Risk", latest.get("risk_status", "")),
        ]
        return "".join(
            f'<span><small>{self._escape(label)}</small><strong>{self._escape(value)}</strong></span>'
            for label, value in items
        )

    def _agent_controls(self, monitor: dict[str, Any], return_to: str = "/agent") -> str:
        safe_return_to = self._safe_redirect_path(return_to)
        if monitor.get("running"):
            return f"""
            <form method="post" action="/agent/monitor/stop">
              <input type="hidden" name="next" value="{self._escape(safe_return_to)}">
              <div class="actions"><button type="submit">Stop Session</button></div>
            </form>
            """
        configs = self._agent_config_names()
        if not configs:
            return "<p>No safe agent config available.</p>"
        preferred = "mt5-paper.json" if "mt5-paper.json" in configs else configs[0]
        options = "".join(
            f'<option value="{self._escape(name)}" {"selected" if name == preferred else ""}>{self._escape(name)}</option>'
            for name in configs
        )
        return f"""
        <form method="post" action="/agent/monitor/start">
          <input type="hidden" name="next" value="{self._escape(safe_return_to)}">
          <input type="hidden" name="trigger_mode" value="stream">
          <div class="market-toolbar">
            <label>Config<select name="config">{options}</select></label>
            <label>Max decisions<input name="max_iterations" type="number" min="1" max="10000" value="100"></label>
            <label>Decision gap<input name="interval_seconds" type="number" min="0.1" max="3600" step="0.1" value="1"></label>
            <button type="submit">Start Session</button>
          </div>
        </form>
        """

    def _agent_decision_card(self, decision: dict[str, Any]) -> str:
        if not decision:
            return "<p>No decisions recorded yet. Start the agent with mt5-paper.json to collect decisions.</p>"
        reasons = decision.get("reasons", [])
        reason_items = "".join(f"<li>{self._escape(reason)}</li>" for reason in reasons) if reasons else "<li>none</li>"
        risk_notes = decision.get("analyst_risk_notes", [])
        risk_note_items = "".join(f"<li>{self._escape(note)}</li>" for note in risk_notes) if risk_notes else "<li>none</li>"
        llm_trace = self._llm_trace_details(decision.get("llm_trace", {}))
        return f"""
        <div class="decision-card">
          <div><span>Time</span><strong>{self._escape(decision.get("timestamp", ""))}</strong></div>
          <div><span>Symbol</span><strong>{self._escape(decision.get("symbol", ""))}</strong></div>
          <div><span>Action</span><strong>{self._escape(decision.get("action", ""))}</strong></div>
          <div><span>Confidence</span><strong>{self._escape(decision.get("confidence", ""))}</strong></div>
          <div><span>Risk</span><strong>{self._escape(decision.get("risk_status", ""))}</strong></div>
          <div><span>Execution</span><strong>{self._escape(decision.get("execution", ""))}</strong></div>
          <div><span>Brain</span><strong>{self._escape(decision.get("analyst_model", ""))}</strong></div>
          <div><span>Bias</span><strong>{self._escape(decision.get("analyst_bias", ""))}</strong></div>
          <div><span>Regime</span><strong>{self._escape(decision.get("analyst_regime", ""))}</strong></div>
          <div><span>Avoid</span><strong>{self._escape(decision.get("analyst_avoid_trade", ""))}</strong></div>
          <div class="decision-reasons"><span>Reasons</span><ul>{reason_items}</ul></div>
          <div class="decision-reasons"><span>Analyst Risk Notes</span><ul>{risk_note_items}</ul></div>
          {llm_trace}
        </div>
        """

    def _llm_trace_details(self, trace: dict[str, Any]) -> str:
        if not trace:
            return ""
        request_payload = trace.get("request", {})
        response_payload = trace.get("response", {})
        request_json = json.dumps(request_payload, indent=2, sort_keys=True, default=str)
        response_json = json.dumps(response_payload, indent=2, sort_keys=True, default=str)
        return f"""
        <div class="llm-trace">
          <span>LLM Trace</span>
          <details>
            <summary>Request sent to LLM</summary>
            <pre>{self._escape(request_json)}</pre>
          </details>
          <details>
            <summary>Response from LLM</summary>
            <pre>{self._escape(response_json)}</pre>
          </details>
        </div>
        """

    def _monitor_summary(self) -> dict[str, Any]:
        self._refresh_monitor_state()
        with self.monitor_lock:
            return {
                **self.monitor_state,
                "started_at_display": self._format_local_time(self.monitor_state.get("started_at")),
                "stopped_at_display": self._format_local_time(self.monitor_state.get("stopped_at")),
                "recent_events": [self._monitor_event_row(event) for event in list(self.monitor_events[-20:])[::-1]],
                "event_count": len(self.monitor_events),
            }

    def _monitor_event_row(self, event: dict[str, Any]) -> dict[str, Any]:
        timestamp = event.get("timestamp", "")
        return {
            **event,
            "timestamp": self._format_local_time(timestamp),
            "timestamp_raw": timestamp,
        }

    def _monitor_metrics(self, monitor: dict[str, Any]) -> str:
        items = [
            ("Status", monitor.get("status", "stopped")),
            ("Running", monitor.get("running", False)),
            ("Trigger", monitor.get("trigger_mode", "interval")),
            ("Config", monitor.get("config") or ""),
            ("Max Iterations", monitor.get("max_iterations", 0)),
            ("Events", monitor.get("event_count", 0)),
            ("Started", monitor.get("started_at_display") or ""),
            ("Stopped", monitor.get("stopped_at_display") or ""),
            ("Error", monitor.get("error") or ""),
        ]
        return "".join(
            f'<div class="metric"><span>{self._escape(label)}</span><strong>{self._escape(value)}</strong></div>'
            for label, value in items
        )

    def _monitor_controls(self) -> str:
        configs = self._config_names()
        monitor = self._monitor_summary()
        if monitor["running"]:
            return self._monitor_stop_form()
        return self._run_monitor_form(configs)

    def _monitor_stop_form(self) -> str:
        return """
        <form method="post" action="/monitor/stop">
          <div class="actions"><button type="submit">Stop Monitor</button></div>
        </form>
        """

    def _run_monitor_form(self, configs: list[str]) -> str:
        if self.monitor_fn is None:
            return "<p>Monitor runner is not configured for this web app instance.</p>"
        if not configs:
            return "<p>No config available.</p>"
        options = "".join(f'<option value="{self._escape(name)}">{self._escape(name)}</option>' for name in configs)
        return f"""
        <form method="post" action="/monitor/start">
          <div class="form-grid">
            <label>Config<select name="config">{options}</select></label>
            <label>Max iterations<input name="max_iterations" type="number" min="1" max="10000" value="50"></label>
            <label>Interval seconds<input name="interval_seconds" type="number" min="0.1" max="3600" step="0.1" value="1"></label>
          </div>
          <div class="actions"><button type="submit">Start Monitor</button></div>
        </form>
        """

    def _monitor_events_table(self, events: list[dict[str, Any]]) -> str:
        if not events:
            return "<p>No monitor events yet.</p>"
        rows = []
        for event in events:
            rows.append(
                "<tr>"
                f"<td>{self._escape(event.get('timestamp', ''))}</td>"
                f"<td>{self._escape(event.get('iteration', ''))}</td>"
                f"<td>{self._escape(event.get('symbol', ''))}</td>"
                f"<td>{self._escape(event.get('action') or event.get('reason') or '')}</td>"
                f"<td>{self._escape(event.get('risk_status', ''))}</td>"
                f"<td>{self._escape(event.get('execution', ''))}</td>"
                f"<td>{self._escape(event.get('closed_positions', 0))}</td>"
                "</tr>"
            )
        return "<table><thead><tr><th>Time</th><th>Iter</th><th>Symbol</th><th>Action/Reason</th><th>Risk</th><th>Execution</th><th>Closed</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"

    def _positions_table(self, positions: list[dict[str, Any]]) -> str:
        if not positions:
            return "<p>No open paper positions.</p>"
        rows = []
        for position in positions:
            rows.append(
                "<tr>"
                f"<td>{self._escape(position.get('symbol', ''))}</td>"
                f"<td>{self._escape(position.get('side', ''))}</td>"
                f"<td>{self._escape(position.get('volume', ''))}</td>"
                f"<td>{self._escape(position.get('entry_price', ''))}</td>"
                f"<td>{self._escape(position.get('stop_loss', ''))}</td>"
                f"<td>{self._escape(position.get('take_profit', ''))}</td>"
                f"<td>{self._escape(self._format_local_time(position.get('opened_at', '')))}</td>"
                "</tr>"
            )
        return "<table><thead><tr><th>Symbol</th><th>Side</th><th>Lot</th><th>Entry</th><th>SL</th><th>TP</th><th>Opened</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"

    def _mt5_positions_table(self, positions: list[dict[str, Any]]) -> str:
        if not positions:
            return "<p>No open MT5 positions.</p>"
        rows = []
        for position in positions:
            rows.append(
                "<tr>"
                f"<td>{self._escape(position.get('ticket', ''))}</td>"
                f"<td>{self._escape(position.get('symbol', ''))}</td>"
                f"<td>{self._escape(position.get('side', ''))}</td>"
                f"<td>{self._escape(position.get('volume', ''))}</td>"
                f"<td>{self._escape(position.get('entry_price', ''))}</td>"
                f"<td>{self._escape(position.get('stop_loss', ''))}</td>"
                f"<td>{self._escape(position.get('take_profit', ''))}</td>"
                f"<td>{self._escape(position.get('profit', ''))}</td>"
                f"<td>{self._escape(position.get('opened_at', ''))}</td>"
                "</tr>"
            )
        return "<table><thead><tr><th>Ticket</th><th>Symbol</th><th>Side</th><th>Lot</th><th>Entry</th><th>SL</th><th>TP</th><th>Profit</th><th>Opened</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"

    def _mt5_open_positions(self, symbols: list[str]) -> list[dict[str, Any]]:
        wanted = {symbol.upper() for symbol in symbols}
        try:
            connection = Mt5Connection()
            connection.initialize()
            positions = connection.mt5.positions_get() or []
        except Exception:
            return []

        rows = []
        for position in positions:
            raw = position._asdict() if hasattr(position, "_asdict") else {
                key: getattr(position, key, "")
                for key in [
                    "ticket",
                    "symbol",
                    "type",
                    "volume",
                    "price_open",
                    "sl",
                    "tp",
                    "profit",
                    "time",
                ]
            }
            symbol = str(raw.get("symbol", "")).upper()
            if wanted and symbol not in wanted:
                continue
            try:
                side = "buy" if int(raw.get("type", -1)) == 0 else "sell" if int(raw.get("type", -1)) == 1 else str(raw.get("type", ""))
            except (TypeError, ValueError):
                side = str(raw.get("type", ""))
            opened_raw = raw.get("time", "")
            rows.append(
                {
                    "source": "mt5",
                    "ticket": raw.get("ticket", ""),
                    "symbol": symbol,
                    "side": side,
                    "volume": raw.get("volume", ""),
                    "entry_price": raw.get("price_open", raw.get("price", "")),
                    "stop_loss": raw.get("sl", ""),
                    "take_profit": raw.get("tp", ""),
                    "profit": raw.get("profit", ""),
                    "opened_at": self._format_local_time(opened_raw),
                    "opened_at_raw": opened_raw,
                }
            )
        return rows

    def _decisions_table(self, decisions: list[dict[str, Any]]) -> str:
        if not decisions:
            return "<p>No decisions recorded yet.</p>"
        rows = []
        for decision in decisions:
            reasons = list(decision.get("reasons", []))
            reason_items = "".join(f"<li>{self._escape(reason)}</li>" for reason in reasons) if reasons else "<li>none</li>"
            execution = decision.get("execution") or "not sent"
            rows.append(
                '<article class="decision-history-item">'
                '<div class="decision-history-head">'
                f'<div><span>Time</span><strong>{self._escape(decision.get("timestamp", ""))}</strong></div>'
                f'<div><span>Symbol</span><strong>{self._escape(decision.get("symbol", ""))}</strong></div>'
                f'<div><span>Action</span><strong>{self._escape(decision.get("action", ""))}</strong></div>'
                f'<div><span>Confidence</span><strong>{self._escape(decision.get("confidence", ""))}</strong></div>'
                f'<div><span>Risk</span><strong>{self._escape(decision.get("risk_status", ""))}</strong></div>'
                f'<div><span>Brain</span><strong>{self._escape(decision.get("analyst_model", ""))}</strong></div>'
                f'<div><span>Execution</span><strong>{self._escape(execution)}</strong></div>'
                "</div>"
                f'<div class="decision-history-reasons"><span>Reasons</span><ul>{reason_items}</ul></div>'
                "</article>"
            )
        return '<div class="decision-history">' + "".join(rows) + "</div>"

    def _closes_table(self, closes: list[dict[str, Any]]) -> str:
        if not closes:
            return "<p>No closed paper positions yet.</p>"
        rows = []
        for close in closes:
            r_multiple = float(close.get("r_multiple", 0.0) or 0.0)
            r_class = "positive" if r_multiple > 0 else "negative" if r_multiple < 0 else ""
            rows.append(
                "<tr>"
                f"<td>{self._escape(self._format_local_time(close.get('closed_at', '')))}</td>"
                f"<td>{self._escape(close.get('symbol', ''))}</td>"
                f"<td>{self._escape(close.get('side', ''))}</td>"
                f"<td>{self._escape(close.get('exit_reason', ''))}</td>"
                f"<td>{self._escape(close.get('entry_price', ''))}</td>"
                f"<td>{self._escape(close.get('exit_price', ''))}</td>"
                f"<td class=\"{r_class}\">{self._escape(r_multiple)}</td>"
                "</tr>"
            )
        return "<table><thead><tr><th>Closed</th><th>Symbol</th><th>Side</th><th>Reason</th><th>Entry</th><th>Exit</th><th>R</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"

    def _equity_curve(self, points: list[dict[str, Any]]) -> str:
        if not points:
            return "<p>No closed paper positions yet.</p>"
        return self._line_chart("Cumulative R", points, "index", "cumulative_r", "symbol")

    def _line_chart(
        self,
        title: str,
        rows: list[dict[str, Any]],
        x_key: str,
        y_key: str,
        label_key: str,
    ) -> str:
        values = [float(row.get(y_key, 0.0) or 0.0) for row in rows]
        lower = min(0.0, min(values))
        upper = max(0.0, max(values))
        span = upper - lower or 1.0
        width = 620
        height = 260
        left = 44
        right = 18
        top = 34
        bottom = 34
        plot_width = width - left - right
        plot_height = height - top - bottom
        count = max(len(rows) - 1, 1)
        points = []
        dots = []
        for index, row in enumerate(rows):
            value = float(row.get(y_key, 0.0) or 0.0)
            x = left + (index / count) * plot_width
            y = top + ((upper - value) / span) * plot_height
            points.append(f"{x:.2f},{y:.2f}")
            label = f"{row.get(x_key)} {row.get(label_key, '')}: {value}R"
            dots.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4" class="chart-fill-good"><title>{self._escape(label)}</title></circle>')
        zero_y = top + ((upper - 0.0) / span) * plot_height
        return f"""
        <svg viewBox="0 0 {width} {height}" role="img" aria-label="{self._escape(title)}">
          <text x="{left}" y="20" class="chart-title">{self._escape(title)}</text>
          <line x1="{left}" y1="{zero_y:.2f}" x2="{width - right}" y2="{zero_y:.2f}" class="chart-axis"></line>
          <line x1="{left}" y1="{top}" x2="{left}" y2="{height - bottom}" class="chart-axis"></line>
          <polyline points="{' '.join(points)}" class="chart-line"></polyline>
          {''.join(dots)}
          <text x="{left}" y="{height - 8}" class="chart-label">{len(rows)} closed trade points</text>
          <text x="{width - 120}" y="{height - 8}" class="chart-label">min {lower:.2f}R / max {upper:.2f}R</text>
        </svg>
        """

    def _bar_chart(self, title: str, rows: list[dict[str, Any]], label_key: str, value_key: str) -> str:
        if not rows:
            return "<p>No chart data yet.</p>"
        selected = rows[:8]
        values = [float(row.get(value_key, 0.0) or 0.0) for row in selected]
        lower = min(0.0, min(values))
        upper = max(0.0, max(values))
        span = upper - lower or 1.0
        width = 620
        height = 260
        left = 44
        right = 18
        top = 34
        bottom = 58
        plot_width = width - left - right
        plot_height = height - top - bottom
        gap = 10
        bar_width = max(18, (plot_width - gap * (len(selected) - 1)) / len(selected))
        zero_y = top + ((upper - 0.0) / span) * plot_height
        bars = []
        labels = []
        for index, row in enumerate(selected):
            value = float(row.get(value_key, 0.0) or 0.0)
            x = left + index * (bar_width + gap)
            y = top + ((upper - max(value, 0.0)) / span) * plot_height
            if value < 0:
                y = zero_y
            bar_height = max(2, abs(value / span) * plot_height)
            klass = "chart-fill-good" if value >= 0 else "chart-fill-bad"
            label = str(row.get(label_key, ""))
            bars.append(
                f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_width:.2f}" height="{bar_height:.2f}" class="{klass}"><title>{self._escape(label)} {value}</title></rect>'
            )
            labels.append(f'<text x="{x:.2f}" y="{height - 28}" class="chart-label">{self._escape(label[:10])}</text>')
            labels.append(f'<text x="{x:.2f}" y="{height - 12}" class="chart-label">{value:.2f}</text>')
        return f"""
        <svg viewBox="0 0 {width} {height}" role="img" aria-label="{self._escape(title)}">
          <text x="{left}" y="20" class="chart-title">{self._escape(title)}</text>
          <line x1="{left}" y1="{zero_y:.2f}" x2="{width - right}" y2="{zero_y:.2f}" class="chart-axis"></line>
          <line x1="{left}" y1="{top}" x2="{left}" y2="{height - bottom}" class="chart-axis"></line>
          {''.join(bars)}
          {''.join(labels)}
        </svg>
        """

    def _count_bar_chart(self, title: str, counts: dict[str, int]) -> str:
        rows = [{"label": key, "value": value} for key, value in list(counts.items())[:8]]
        return self._bar_chart(title, rows, "label", "value")

    def _market_symbol_options(self, symbols: list[str], selected: str) -> str:
        return "".join(
            f'<option value="{self._escape(symbol)}" {"selected" if symbol == selected else ""}>{self._escape(symbol)}</option>'
            for symbol in symbols
        )

    def _market_timeframe_options(self, selected: str) -> str:
        safe_selected = self._safe_timeframe(selected)
        return "".join(
            f'<option value="{label}" {"selected" if label == safe_selected else ""}>{label}</option>'
            for label in ["M1", "M5", "M15", "M30", "H1", "H4", "D1"]
        )

    def _safe_timeframe(self, value: str) -> str:
        label = str(value or "H1").upper()
        return label if label in {"M1", "M5", "M15", "M30", "H1", "H4", "D1"} else "H1"

    def _mt5_timeframe(self, mt5: Any, label: str) -> Any:
        mapping = {
            "M1": "TIMEFRAME_M1",
            "M5": "TIMEFRAME_M5",
            "M15": "TIMEFRAME_M15",
            "M30": "TIMEFRAME_M30",
            "H1": "TIMEFRAME_H1",
            "H4": "TIMEFRAME_H4",
            "D1": "TIMEFRAME_D1",
        }
        attr = mapping.get(self._safe_timeframe(label), "TIMEFRAME_H1")
        if hasattr(mt5, attr):
            return getattr(mt5, attr)
        return getattr(mt5, "TIMEFRAME_M15")

    def _candlestick_chart(
        self,
        symbol: str,
        candles: list[dict[str, Any]],
        overlays: dict[str, list[dict[str, Any]]] | None = None,
    ) -> str:
        if not candles:
            return "<p>No market candle history yet. Run Monitor with `market_source: sim` to generate chart data.</p>"
        overlays = overlays or {}
        selected = candles[-80:]
        indicators = list(overlays.get("indicators", []))[-len(selected) :]
        highs = [float(candle.get("high", candle.get("close", 0.0)) or 0.0) for candle in selected]
        lows = [float(candle.get("low", candle.get("close", 0.0)) or 0.0) for candle in selected]
        current_tick = overlays.get("current_tick", {})
        for key in ("bid", "ask", "mid"):
            if current_tick.get(key) is not None:
                highs.append(float(current_tick[key]))
                lows.append(float(current_tick[key]))
        for indicator in indicators:
            for key in ("ma_fast", "ma_slow", "atr_upper", "atr_lower"):
                if indicator.get(key) is not None:
                    highs.append(float(indicator[key]))
                    lows.append(float(indicator[key]))
        for position in overlays.get("open_positions", []):
            for key in ("entry_price", "stop_loss", "take_profit"):
                if position.get(key) is not None:
                    highs.append(float(position[key]))
                    lows.append(float(position[key]))
        for close in overlays.get("closed_positions", []):
            for key in ("entry_price", "exit_price"):
                if close.get(key) is not None:
                    highs.append(float(close[key]))
                    lows.append(float(close[key]))
        upper = max(highs)
        lower = min(lows)
        span = upper - lower or 1.0
        width = 1080
        height = 560
        left = 68
        right = 126
        top = 48
        bottom = 72
        plot_width = width - left - right
        plot_height = height - top - bottom
        candle_width = max(9, min(30, (plot_width / max(len(selected), 1)) * 0.55))
        bodies = []
        count = max(len(selected) - 1, 1)
        for index, candle in enumerate(selected):
            open_price = float(candle.get("open", candle.get("close", 0.0)) or 0.0)
            high = float(candle.get("high", open_price) or open_price)
            low = float(candle.get("low", open_price) or open_price)
            close = float(candle.get("close", open_price) or open_price)
            x = left + (index / count) * plot_width
            high_y = top + ((upper - high) / span) * plot_height
            low_y = top + ((upper - low) / span) * plot_height
            open_y = top + ((upper - open_price) / span) * plot_height
            close_y = top + ((upper - close) / span) * plot_height
            body_y = min(open_y, close_y)
            body_height = max(2, abs(close_y - open_y))
            klass = "chart-fill-good" if close >= open_price else "chart-fill-bad"
            title = f"{candle.get('step', index + 1)} O:{open_price} H:{high} L:{low} C:{close}"
            wick_color = "#1fbf86" if close >= open_price else "#e05f5f"
            bodies.append(f'<line x1="{x:.2f}" y1="{high_y:.2f}" x2="{x:.2f}" y2="{low_y:.2f}" stroke="{wick_color}" stroke-width="1.4"><title>{self._escape(title)}</title></line>')
            bodies.append(f'<rect x="{x - candle_width / 2:.2f}" y="{body_y:.2f}" width="{candle_width:.2f}" height="{body_height:.2f}" class="{klass}"><title>{self._escape(title)}</title></rect>')
        grid_nodes = self._market_grid_nodes(upper, lower, top, plot_height, left, width - right)
        indicator_nodes = self._market_indicator_nodes(indicators, upper, lower, top, plot_height, left, plot_width)
        overlay_nodes = self._market_overlay_nodes(
            overlays,
            selected,
            upper,
            lower,
            top,
            plot_height,
            left,
            plot_width,
            width - right,
        )
        return f"""
        <svg viewBox="0 0 {width} {height}" role="img" aria-label="{self._escape(symbol)} candlestick chart">
          <rect x="0" y="0" width="{width}" height="{height}" fill="#0f1720"></rect>
          <text x="{left}" y="28" class="market-title">{self._escape(symbol)} Candlestick</text>
          {grid_nodes}
          {indicator_nodes}
          {''.join(bodies)}
          {overlay_nodes}
          <text x="{left}" y="{height - 20}" class="market-label">{len(selected)} candles</text>
          <text x="{width - 220}" y="{height - 20}" class="market-label">low {lower} / high {upper}</text>
        </svg>
        """

    def _market_grid_nodes(self, upper: float, lower: float, top: int, plot_height: int, left: int, right_x: int) -> str:
        span = upper - lower or 1.0
        nodes = []
        for index in range(6):
            price = lower + (span / 5) * index
            y = top + ((upper - price) / span) * plot_height
            nodes.append(f'<line x1="{left}" y1="{y:.2f}" x2="{right_x}" y2="{y:.2f}" class="market-grid"></line>')
            nodes.append(f'<text x="{right_x + 10}" y="{y + 4:.2f}" class="market-label">{price:.5g}</text>')
        nodes.append(f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" class="market-axis"></line>')
        nodes.append(f'<line x1="{left}" y1="{top + plot_height}" x2="{right_x}" y2="{top + plot_height}" class="market-axis"></line>')
        return "".join(nodes)

    def _market_indicator_nodes(
        self,
        indicators: list[dict[str, Any]],
        upper: float,
        lower: float,
        top: int,
        plot_height: int,
        left: int,
        plot_width: int,
    ) -> str:
        if not indicators:
            return ""
        span = upper - lower or 1.0
        count = max(len(indicators) - 1, 1)

        def point(index: int, value: float) -> str:
            x = left + (index / count) * plot_width
            y = top + ((upper - value) / span) * plot_height
            return f"{x:.2f},{y:.2f}"

        def poly(key: str, klass: str) -> str:
            points = [
                point(index, float(row[key]))
                for index, row in enumerate(indicators)
                if row.get(key) is not None
            ]
            return f'<polyline points="{" ".join(points)}" class="{klass}"></polyline>' if points else ""

        upper_points = [point(index, float(row["atr_upper"])) for index, row in enumerate(indicators) if row.get("atr_upper") is not None]
        lower_points = [point(index, float(row["atr_lower"])) for index, row in reversed(list(enumerate(indicators))) if row.get("atr_lower") is not None]
        atr = f'<polygon points="{" ".join(upper_points + lower_points)}" class="market-atr"></polygon>' if upper_points and lower_points else ""
        return atr + poly("ma_fast", "market-ma-fast") + poly("ma_slow", "market-ma-slow")

    def _market_overlay_nodes(
        self,
        overlays: dict[str, list[dict[str, Any]]],
        candles: list[dict[str, Any]],
        upper: float,
        lower: float,
        top: int,
        plot_height: int,
        left: int,
        plot_width: int,
        right_x: int,
    ) -> str:
        span = upper - lower or 1.0

        def y_for(price: Any) -> float:
            return top + ((upper - float(price)) / span) * plot_height

        nodes = []
        styles = {
            "entry_price": ("entry", "#2563eb"),
            "stop_loss": ("SL", "#a52727"),
            "take_profit": ("TP", "#146c43"),
        }
        for position in overlays.get("open_positions", []):
            for key, (label, color) in styles.items():
                if position.get(key) is None:
                    continue
                y = y_for(position[key])
                title = f"open {position.get('side')} {label}: {position[key]}"
                nodes.append(f'<line x1="{left}" y1="{y:.2f}" x2="{right_x}" y2="{y:.2f}" stroke="{color}" stroke-width="1.5" stroke-dasharray="5 4"><title>{self._escape(title)}</title></line>')
                nodes.append(f'<text x="{right_x - 80}" y="{y - 4:.2f}" fill="{color}" font-size="11">{self._escape(label)}</text>')
        current_tick = overlays.get("current_tick", {})
        for key, label, color, dash in [
            ("bid", "BID", "#22c55e", ""),
            ("ask", "ASK", "#ef4444", "6 4"),
        ]:
            if current_tick.get(key) is None:
                continue
            value = float(current_tick[key])
            y = y_for(value)
            label_y = max(top + 16, min(top + plot_height - 8, y))
            dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
            nodes.append(f'<line x1="{left}" y1="{y:.2f}" x2="{right_x}" y2="{y:.2f}" stroke="{color}" stroke-width="2"{dash_attr}></line>')
            nodes.append(f'<rect x="{right_x + 6}" y="{label_y - 14:.2f}" width="108" height="20" rx="4" fill="{color}"></rect>')
            nodes.append(f'<text x="{right_x + 11}" y="{label_y + 1:.2f}" fill="#fff" font-size="11" font-weight="800">{label} {value:.3f}</text>')
        signals = overlays.get("signals", [])
        if signals:
            display_count = min(len(signals), max(len(candles), 1), 12)
            display_signals = signals[-display_count:]
            candle_offset = max(len(candles) - display_count, 0)
            candle_count = max(len(candles) - 1, 1)
            for index, signal in enumerate(display_signals):
                candle_index = min(candle_offset + index, max(len(candles) - 1, 0))
                candle = candles[candle_index] if candles else {}
                x = left + (candle_index / candle_count) * plot_width
                action = str(signal.get("action", ""))
                side = str(signal.get("side") or "")
                if action == "hold":
                    low = float(candle.get("low", candle.get("close", lower)) or lower)
                    y = min(top + plot_height - 16, y_for(low) + 22)
                    color = "#f2c94c"
                    label = "H"
                elif side == "sell":
                    high = float(candle.get("high", candle.get("close", upper)) or upper)
                    y = max(top + 16, y_for(high) - 22)
                    color = "#e05f5f"
                    label = "S"
                else:
                    low = float(candle.get("low", candle.get("close", lower)) or lower)
                    y = min(top + plot_height - 16, y_for(low) + 22)
                    color = "#1fbf86"
                    label = "B"
                title = f"{action} {side} conf={signal.get('confidence')} risk={signal.get('risk_status')}"
                nodes.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="8" fill="{color}"><title>{self._escape(title)}</title></circle>')
                nodes.append(f'<text x="{x - 3:.2f}" y="{y + 4:.2f}" fill="#0f1720" font-size="10" font-weight="800">{label}</text>')
            latest_signal = display_signals[-1]
            latest_action = str(latest_signal.get("action", "none") or "none")
            latest_risk = str(latest_signal.get("risk_status", "") or "")
            latest_confidence = latest_signal.get("confidence", "")
            latest_reasons = list(latest_signal.get("reason_codes", []))
            latest_reason = str(latest_reasons[0]) if latest_reasons else "no_reason"
            badge_color = "#f2c94c" if latest_action == "hold" else "#1fbf86" if str(latest_signal.get("side", "")) != "sell" else "#e05f5f"
            nodes.append(f'<rect x="{left + 178}" y="{top - 34}" width="390" height="34" rx="6" fill="#111827" stroke="{badge_color}" stroke-width="1.5"></rect>')
            nodes.append(f'<text x="{left + 192}" y="{top - 14}" fill="#e5eef8" font-size="12" font-weight="800">Latest Decision: {self._escape(latest_action)} conf {self._escape(latest_confidence)} risk {self._escape(latest_risk)}</text>')
            nodes.append(f'<text x="{left + 192}" y="{top - 3}" fill="#a8bfd5" font-size="10">{self._escape(latest_reason[:48])}</text>')
        closes = overlays.get("closed_positions", [])[-12:]
        if closes:
            step = max(1, (right_x - left) / max(len(closes), 1))
            for index, close in enumerate(closes):
                if close.get("exit_price") is None:
                    continue
                x = left + index * step + step / 2
                y = top + 16
                r_multiple = float(close.get("r_multiple", 0.0) or 0.0)
                color = "#146c43" if r_multiple >= 0 else "#a52727"
                title = f"closed {close.get('exit_reason')} {r_multiple}R at {close.get('exit_price')}"
                points = f"{x:.2f},{y - 7:.2f} {x + 7:.2f},{y + 7:.2f} {x - 7:.2f},{y + 7:.2f}"
                nodes.append(f'<polygon points="{points}" fill="{color}"><title>{self._escape(title)}</title></polygon>')
        return "".join(nodes)

    def _symbol_performance_table(self, rows: list[dict[str, Any]]) -> str:
        if not rows:
            return "<p>No symbol performance yet.</p>"
        rendered = []
        for row in rows:
            total_r = float(row.get("total_r", 0.0) or 0.0)
            r_class = "positive" if total_r > 0 else "negative" if total_r < 0 else ""
            rendered.append(
                "<tr>"
                f"<td>{self._escape(row.get('symbol', ''))}</td>"
                f"<td>{self._escape(row.get('closed', 0))}</td>"
                f"<td>{self._escape(row.get('wins', 0))}</td>"
                f"<td>{self._escape(row.get('losses', 0))}</td>"
                f"<td>{self._escape(row.get('win_rate', 0))}</td>"
                f"<td>{self._escape(row.get('average_r', 0))}</td>"
                f"<td class=\"{r_class}\">{self._escape(total_r)}</td>"
                f"<td>{self._escape(self._money(row.get('estimated_pnl', 0)))}</td>"
                "</tr>"
            )
        return "<table><thead><tr><th>Symbol</th><th>Closed</th><th>Wins</th><th>Losses</th><th>Win Rate</th><th>Avg R</th><th>Total R</th><th>Est. PnL</th></tr></thead><tbody>" + "".join(rendered) + "</tbody></table>"

    def _count_table(self, counts: dict[str, int], key_label: str, value_label: str) -> str:
        if not counts:
            return "<p>No data yet.</p>"
        rows = "".join(
            f"<tr><td>{self._escape(key)}</td><td>{self._escape(value)}</td></tr>"
            for key, value in counts.items()
        )
        return f"<table><thead><tr><th>{self._escape(key_label)}</th><th>{self._escape(value_label)}</th></tr></thead><tbody>{rows}</tbody></table>"

    def _reason_quality_table(self, rows: list[dict[str, Any]]) -> str:
        if not rows:
            return "<p>No reason quality data yet.</p>"
        rendered = []
        for row in rows:
            approved_rate = float(row.get("approved_rate", 0.0) or 0.0)
            klass = "positive" if approved_rate >= 0.7 else "negative" if approved_rate < 0.4 else ""
            rendered.append(
                "<tr>"
                f"<td>{self._escape(row.get('reason', ''))}</td>"
                f"<td>{self._escape(row.get('count', 0))}</td>"
                f"<td>{self._escape(row.get('approved', 0))}</td>"
                f"<td>{self._escape(row.get('rejected', 0))}</td>"
                f"<td>{self._escape(row.get('executed', 0))}</td>"
                f"<td class=\"{klass}\">{self._escape(approved_rate)}</td>"
                f"<td>{self._escape(row.get('executed_rate', 0))}</td>"
                f"<td>{self._escape(row.get('average_confidence', 0))}</td>"
                f"<td>{self._escape(row.get('closed_count', 0))}</td>"
                f"<td>{self._escape(row.get('total_r', 0))}</td>"
                f"<td>{self._escape(row.get('average_r', 0))}</td>"
                "</tr>"
            )
        return "<table><thead><tr><th>Reason</th><th>Count</th><th>Approved</th><th>Rejected</th><th>Executed</th><th>Approval Rate</th><th>Execution Rate</th><th>Avg Confidence</th><th>Closed</th><th>Total R</th><th>Avg R</th></tr></thead><tbody>" + "".join(rendered) + "</tbody></table>"

    def _decision_row(self, row: dict[str, Any]) -> dict[str, Any]:
        decision = row.get("decision", {})
        risk = row.get("risk", {})
        execution = row.get("execution") or {}
        analyst = (decision.get("metadata") or {}).get("analyst") or {}
        raw_timestamp = decision.get("timestamp") or row.get("timestamp", "")
        return {
            "timestamp": self._format_local_time(raw_timestamp),
            "timestamp_raw": raw_timestamp,
            "symbol": decision.get("symbol", ""),
            "action": decision.get("action", ""),
            "confidence": decision.get("confidence", ""),
            "risk_status": risk.get("status", ""),
            "execution": execution.get("message") or execution.get("accepted", ""),
            "reasons": decision.get("reason_codes", []) or risk.get("reasons", []),
            "analyst_model": analyst.get("model_version", ""),
            "analyst_bias": analyst.get("bias", ""),
            "analyst_regime": analyst.get("market_regime", ""),
            "analyst_avoid_trade": analyst.get("avoid_trade", ""),
            "analyst_risk_notes": analyst.get("risk_notes", []),
            "llm_trace": (analyst.get("metadata") or {}).get("llm_trace", {}),
        }

    def _position_row(self, row: dict[str, Any]) -> dict[str, Any]:
        opened_at = row.get("opened_at", "")
        return {
            **row,
            "opened_at": self._format_local_time(opened_at),
            "opened_at_raw": opened_at,
        }

    def _close_row(self, row: dict[str, Any]) -> dict[str, Any]:
        closed_at = row.get("closed_at", "")
        opened_at = row.get("opened_at", "")
        return {
            **row,
            "closed_at": self._format_local_time(closed_at),
            "closed_at_raw": closed_at,
            "opened_at": self._format_local_time(opened_at),
            "opened_at_raw": opened_at,
        }

    def _decision_reason_counts(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            for reason in row.get("decision", {}).get("reason_codes", []):
                counts[str(reason)] = counts.get(str(reason), 0) + 1
        return dict(sorted(counts.items(), key=lambda item: item[1], reverse=True))

    def _rejection_reason_counts(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            if row.get("risk", {}).get("status") != "rejected":
                continue
            for reason in row.get("risk", {}).get("reasons", []):
                counts[str(reason)] = counts.get(str(reason), 0) + 1
        return dict(sorted(counts.items(), key=lambda item: item[1], reverse=True))

    def _reason_profit(self, closed_positions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        stats: dict[str, dict[str, Any]] = {}
        for position in closed_positions:
            r_multiple = float(position.get("r_multiple", 0.0) or 0.0)
            for reason in position.get("reason_codes", []):
                item = stats.setdefault(str(reason), {"count": 0, "total_r": 0.0, "wins": 0, "losses": 0})
                item["count"] += 1
                item["total_r"] += r_multiple
                if r_multiple > 0:
                    item["wins"] += 1
                if r_multiple < 0:
                    item["losses"] += 1
        return {
            reason: {
                **item,
                "total_r": round(float(item["total_r"]), 4),
                "average_r": round(float(item["total_r"]) / int(item["count"]), 4) if item["count"] else 0.0,
            }
            for reason, item in sorted(stats.items(), key=lambda pair: pair[1]["total_r"], reverse=True)
        }

    def _reason_quality(self, rows: list[dict[str, Any]], closed_positions: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        profit_by_reason = self._reason_profit(closed_positions or [])
        stats: dict[str, dict[str, Any]] = {}
        for row in rows:
            decision = row.get("decision", {})
            risk = row.get("risk", {})
            execution = row.get("execution") or {}
            reasons = list(decision.get("reason_codes", []))
            status = risk.get("status")
            accepted = execution.get("accepted") is True
            confidence = float(decision.get("confidence", 0.0) or 0.0)
            for reason in reasons:
                item = stats.setdefault(
                    str(reason),
                    {
                        "reason": str(reason),
                        "count": 0,
                        "approved": 0,
                        "rejected": 0,
                        "executed": 0,
                        "confidence_total": 0.0,
                    },
                )
                item["count"] += 1
                item["confidence_total"] += confidence
                if status == "approved":
                    item["approved"] += 1
                if status == "rejected":
                    item["rejected"] += 1
                if accepted:
                    item["executed"] += 1
        output = []
        for item in stats.values():
            count = int(item["count"])
            output.append(
                {
                    "reason": item["reason"],
                    "count": count,
                    "approved": item["approved"],
                    "rejected": item["rejected"],
                    "executed": item["executed"],
                    "approved_rate": round(item["approved"] / count, 4) if count else 0.0,
                    "executed_rate": round(item["executed"] / count, 4) if count else 0.0,
                    "average_confidence": round(item["confidence_total"] / count, 4) if count else 0.0,
                    "closed_count": profit_by_reason.get(item["reason"], {}).get("count", 0),
                    "total_r": profit_by_reason.get(item["reason"], {}).get("total_r", 0.0),
                    "average_r": profit_by_reason.get(item["reason"], {}).get("average_r", 0.0),
                }
            )
        return sorted(output, key=lambda row: (row["count"], row["approved_rate"]), reverse=True)

    def _risk_amount(self, config: dict[str, Any]) -> float:
        equity = float(config.get("paper_start_equity", 10_000) or 10_000)
        risk_percent = float(config.get("default_risk_percent", 0.25) or 0.25)
        return equity * (risk_percent / 100)

    def _estimated_pnl(self, total_r: float, config: dict[str, Any]) -> float:
        return total_r * self._risk_amount(config)

    def _money(self, value: Any) -> str:
        return f"${float(value or 0):,.2f}"

    def _run_experiment_form(self, configs: list[str]) -> str:
        if not configs:
            return "<p>No config available.</p>"
        options = "".join(f'<option value="{self._escape(name)}">{self._escape(name)}</option>' for name in configs)
        return f"""
        <form method="post" action="/experiments/run">
          <div class="form-grid">
            <label>Config<select name="config">{options}</select></label>
            <label>Run name<input name="run_name" value="{self._escape(self._next_run_name())}"></label>
            <label>Iterations<input name="iterations" type="number" min="1" max="500" value="24"></label>
          </div>
          <div class="actions"><button type="submit">Run Experiment</button></div>
        </form>
        """

    def _config_list(self, configs: list[str]) -> str:
        if not configs:
            return "<p>No configs found.</p>"
        return "<table><tbody>" + "".join(f'<tr><td>{self._escape(name)}</td><td><a href="/config?name={self._escape(name)}">Edit</a></td></tr>' for name in configs) + "</tbody></table>"

    def _config_names(self) -> list[str]:
        if not self.configs_dir.exists():
            return []
        return sorted(path.name for path in self.configs_dir.glob("*.json"))

    def _agent_config_names(self) -> list[str]:
        names = []
        for name in self._config_names():
            try:
                settings = load_settings(str(self._safe_config_path(name)))
            except Exception:
                continue
            if getattr(settings, "mode", "paper") == "paper" or (
                getattr(settings, "mode", "paper") == "live" and bool(getattr(settings, "allow_live_execution", False))
            ):
                names.append(name)
        return names

    def _agent_brain_label(self, settings: Any) -> str:
        analyst = getattr(settings, "analyst", "none")
        if analyst == "rule":
            return "rule-based"
        if analyst == "llm":
            return f"llm:{getattr(settings, 'llm_model', '')}"
        if analyst == "none":
            return "planner-only"
        return str(analyst)

    def _experiment_names(self) -> list[str]:
        if not self.experiments_dir.exists():
            return []
        return sorted(path.name for path in self.experiments_dir.iterdir() if path.is_dir())

    def _save_config(self, name: str, content: str) -> None:
        json.loads(content)
        self._safe_config_path(name).write_text(content, encoding="utf-8")

    def _run_experiment_from_form(self, form: dict[str, list[str]]) -> dict[str, Any]:
        if self.monitor_fn is None:
            return {"error": "Experiment runner is not configured for this web app instance."}
        try:
            config_name = form.get("config", ["paper-demo.json"])[0]
            run_name = form.get("run_name", [self._next_run_name()])[0]
            iterations = int(form.get("iterations", ["24"])[0])
            if iterations < 1 or iterations > 500:
                raise ValueError("iterations must be between 1 and 500")
            output_dir = self._safe_experiment_path(run_name)
            summary = ExperimentRunner(self.monitor_fn).run(
                load_settings(str(self._safe_config_path(config_name))),
                output_dir,
                iterations,
            )
            DashboardRenderer().render_experiment(output_dir, output_dir / "dashboard.html")
            return summary
        except Exception as exc:
            return {"error": str(exc)}

    def _start_monitor_from_form(self, form: dict[str, list[str]]) -> dict[str, Any]:
        trigger_mode = form.get("trigger_mode", ["interval"])[0]
        if trigger_mode == "interval" and self.monitor_fn is None:
            return {"error": "Monitor runner is not configured for this web app instance."}
        self._refresh_monitor_state()
        with self.monitor_lock:
            if self.monitor_state["running"]:
                return {"error": "Monitor is already running."}
        try:
            config_name = form.get("config", ["paper-demo.json"])[0]
            max_iterations = int(form.get("max_iterations", ["50"])[0])
            interval_seconds = float(form.get("interval_seconds", ["1"])[0])
            if max_iterations < 1 or max_iterations > 10_000:
                raise ValueError("max_iterations must be between 1 and 10000")
            if interval_seconds < 0.1 or interval_seconds > 3600:
                raise ValueError("interval_seconds must be between 0.1 and 3600")
            if trigger_mode not in {"interval", "stream"}:
                raise ValueError("trigger_mode must be interval or stream")
            settings = load_settings(str(self._safe_config_path(config_name)))
            if settings.mode == "live" and not bool(getattr(settings, "allow_live_execution", False)):
                raise ValueError("live mode requires allow_live_execution=true in the selected config")
            if trigger_mode == "stream" and settings.market_source != "mt5":
                raise ValueError("stream agent requires market_source: mt5 and EA socket ticks")
            settings = merge_settings(
                settings,
                interval_seconds=interval_seconds,
                memory_path=str(self._rooted_path(settings.memory_path)),
                paper_state_path=str(self._rooted_path(settings.paper_state_path)),
                sim_state_path=str(self._rooted_path(settings.sim_state_path)),
            )
            stop_event = threading.Event()
            target = self._stream_monitor_target if trigger_mode == "stream" else self._monitor_target
            thread = threading.Thread(
                target=target,
                args=(settings, max_iterations, stop_event, config_name),
                daemon=True,
                name="quantz-web-stream-agent" if trigger_mode == "stream" else "quantz-web-monitor",
            )
            with self.monitor_lock:
                self.monitor_events = []
                self.monitor_stop_event = stop_event
                self.monitor_thread = thread
                self.monitor_state = {
                    "running": True,
                    "status": "running",
                    "config": config_name,
                    "max_iterations": max_iterations,
                    "trigger_mode": trigger_mode,
                    "started_at": self._now(),
                    "stopped_at": None,
                    "error": None,
                }
                self._write_monitor_snapshot_locked()
            thread.start()
            self._notify_event_stream()
            return {"status": "started"}
        except Exception as exc:
            return {"error": str(exc)}

    def _monitor_target(self, settings: Any, max_iterations: int, stop_event: threading.Event, config_name: str) -> None:
        error = None
        try:
            self.monitor_fn(settings, max_iterations, quiet=True, stop_event=stop_event, event_sink=self._record_monitor_event)
        except TypeError:
            try:
                self.monitor_fn(settings, max_iterations, quiet=True)
            except Exception as exc:
                error = str(exc)
        except Exception as exc:
            error = str(exc)
        with self.monitor_lock:
            self.monitor_state.update(
                {
                    "running": False,
                    "status": "error" if error else "stopped" if stop_event.is_set() else "completed",
                    "config": config_name,
                    "stopped_at": self._now(),
                    "error": error,
                }
            )
            self._write_monitor_snapshot_locked()
        self._notify_event_stream()

    def _stream_monitor_target(self, settings: Any, max_iterations: int, stop_event: threading.Event, config_name: str) -> None:
        error = None
        processed_keys: set[tuple[str, str, str, str]] = set()
        iteration = 0
        last_decision_at: datetime | None = None
        with self.monitor_lock:
            started_after = self._parse_datetime(self.monitor_state.get("started_at")) or datetime.now(timezone.utc)
        try:
            while iteration < max_iterations:
                if stop_event.is_set():
                    break

                tick = self._latest_stream_tick(settings.symbols, processed_keys, started_after)
                if not tick:
                    with self.event_condition:
                        sequence = self.event_sequence
                        self.event_condition.wait_for(
                            lambda: stop_event.is_set() or self.event_sequence != sequence,
                            timeout=30,
                        )
                    if stop_event.is_set():
                        break
                    tick = self._latest_stream_tick(settings.symbols, processed_keys, started_after)
                if not tick:
                    continue
                now = datetime.now(timezone.utc)
                if last_decision_at is not None:
                    elapsed = (now - last_decision_at).total_seconds()
                    if elapsed < float(settings.interval_seconds):
                        continue

                iteration += 1
                last_decision_at = now
                record = self._run_stream_agent_once(settings, tick)
                closed = (record.outcome or {}).get("closed_positions", [])
                self._record_monitor_event(
                    {
                        "timestamp": self._now(),
                        "iteration": iteration,
                        "trigger": "ea_socket_tick",
                        "source": "ea_socket_stream",
                        "symbol": record.decision.symbol,
                        "action": record.decision.action,
                        "confidence": record.decision.confidence,
                        "risk_status": record.risk.status,
                        "execution": record.execution.message if record.execution else None,
                        "closed_positions": len(closed),
                        "tick_bid": tick.get("bid", ""),
                        "tick_ask": tick.get("ask", ""),
                        "reason_codes": record.decision.reason_codes,
                    }
                )
        except Exception as exc:
            error = str(exc)
        with self.monitor_lock:
            self.monitor_state.update(
                {
                    "running": False,
                    "status": "error" if error else "stopped" if stop_event.is_set() else "completed",
                    "config": config_name,
                    "trigger_mode": "stream",
                    "stopped_at": self._now(),
                    "error": error,
                }
            )
            self._write_monitor_snapshot_locked()
        self._notify_event_stream()

    def _latest_stream_tick(
        self,
        symbols: list[str],
        processed_keys: set[tuple[str, str, str, str]],
        started_after: datetime,
    ) -> dict[str, Any]:
        wanted = {symbol.upper() for symbol in symbols}
        for tick in self._recent_ticks(limit=50):
            if str(tick.get("source", "")) != "ea_socket":
                continue
            if str(tick.get("symbol", "")).upper() not in wanted:
                continue
            captured_at = self._parse_datetime(tick.get("captured_at", ""))
            if captured_at is not None and captured_at <= started_after:
                continue
            key = (
                str(tick.get("symbol", "")),
                str(tick.get("tick_time", "")),
                str(tick.get("bid", "")),
                str(tick.get("ask", "")),
            )
            if key in processed_keys:
                continue
            processed_keys.add(key)
            return tick
        return {}

    def _run_stream_agent_once(self, settings: Any, tick: dict[str, Any]) -> Any:
        symbol = str(tick.get("symbol", settings.symbols[0])).upper()
        market = self._market_snapshot_from_stream_tick(settings, tick)
        account = self._account_state_for_stream_agent(settings)
        mt5_positions = self._mt5_open_positions(getattr(settings, "symbols", []))
        external_open_symbol_positions = sum(
            1 for position in mt5_positions if str(position.get("symbol", "")).upper() == symbol
        )
        agent = TradingAgent(
            planner=VariableDrivenPlanner(),
            risk_governor=RiskGovernor(RiskConfig()),
            broker=self._stream_broker(settings),
            memory=JsonlExperienceStore(settings.memory_path),
            paper_portfolio=PaperPortfolio(settings.paper_state_path) if settings.mode == "paper" else None,
            analyst=self._stream_analyst(settings),
        )
        return agent.run_once(
            AgentContext(
                market=market,
                account=account,
                constraints={
                    **settings.constraints,
                    "stream_source": "ea_socket",
                    "stream_symbol": symbol,
                    "external_open_symbol_positions": external_open_symbol_positions,
                    "external_open_positions_source": "mt5",
                    "block_when_symbol_open": True,
                    "open_symbol_positions": external_open_symbol_positions,
                },
            )
        )

    def _stream_broker(self, settings: Any) -> Any:
        if settings.mode == "paper":
            return PaperBrokerAdapter()
        if settings.mode == "live" and getattr(settings, "execution_source", "mt5") == "mt5":
            if not bool(getattr(settings, "allow_live_execution", False)):
                raise RuntimeError("live_execution_not_allowed")
            return Mt5BrokerAdapter()
        raise RuntimeError(f"unsupported_stream_execution:{settings.mode}:{getattr(settings, 'execution_source', '')}")

    def _stream_analyst(self, settings: Any) -> Any:
        if settings.analyst == "rule":
            return RuleBasedAnalyst()
        if settings.analyst == "llm":
            return LLMAnalyst(
                model=getattr(settings, "llm_model", "gpt-5.4-mini"),
                api_key_env=getattr(settings, "llm_api_key_env", "OPENAI_API_KEY"),
                timeout_seconds=float(getattr(settings, "llm_timeout_seconds", 12.0) or 12.0),
            )
        return None

    def _market_snapshot_from_stream_tick(self, settings: Any, tick: dict[str, Any]) -> MarketSnapshot:
        symbol = str(tick.get("symbol", settings.symbols[0])).upper()
        bid = float(tick.get("bid", 0.0) or 0.0)
        ask = float(tick.get("ask", 0.0) or 0.0)
        spread_points = float(tick.get("spread_points", 0.0) or 0.0)
        timestamp = self._parse_datetime(tick.get("tick_time")) or self._parse_datetime(tick.get("captured_at")) or datetime.now(timezone.utc)
        try:
            base = Mt5MarketFeed(Mt5Connection()).snapshot(symbol)
            return replace(
                base,
                bid=bid,
                ask=ask,
                spread_points=round(spread_points, 2),
                timestamp=timestamp,
                features={
                    **base.features,
                    "source": "ea_socket_stream",
                    "tick_source": "ea_socket",
                    "tick_time": tick.get("tick_time", ""),
                    "captured_at": tick.get("captured_at", ""),
                },
            )
        except Exception:
            return MarketSnapshot(
                symbol=symbol,
                bid=bid,
                ask=ask,
                spread_points=round(spread_points, 2),
                atr_points=max(spread_points * 4, 1),
                trend_score=0.0,
                volatility_score=0.5,
                session="ea_socket",
                news_risk="low",
                timestamp=timestamp,
                features={
                    "source": "ea_socket_stream",
                    "tick_source": "ea_socket",
                    "tick_time": tick.get("tick_time", ""),
                    "captured_at": tick.get("captured_at", ""),
                },
            )

    def _account_state_for_stream_agent(self, settings: Any) -> AccountState:
        try:
            return Mt5AccountFeed(Mt5Connection()).state()
        except Exception:
            equity = float(getattr(settings, "paper_start_equity", 10_000) or 10_000)
            return AccountState(
                equity=equity,
                balance=equity,
                free_margin=equity,
                open_positions=0,
            )

    def _record_monitor_event(self, event: dict[str, Any]) -> None:
        with self.monitor_lock:
            self.monitor_events.append(dict(event))
            self.monitor_events = self.monitor_events[-500:]
            self._write_monitor_snapshot_locked()
        self._notify_event_stream()

    def _stop_monitor(self) -> dict[str, Any]:
        with self.monitor_lock:
            if self.monitor_stop_event is None or not self.monitor_state["running"]:
                return {"status": "not_running"}
            self.monitor_state["status"] = "stopping"
            self.monitor_stop_event.set()
            self._write_monitor_snapshot_locked()
        self._notify_event_stream()
        return {"status": "stopping"}

    def _notify_event_stream(self) -> None:
        with self.event_condition:
            self.event_sequence += 1
            self.event_condition.notify_all()

    def _refresh_monitor_state(self) -> None:
        with self.monitor_lock:
            thread = self.monitor_thread
            running = bool(thread and thread.is_alive())
            if self.monitor_state["running"] and not running:
                self.monitor_state["running"] = False
                if self.monitor_state["status"] == "running":
                    self.monitor_state["status"] = "completed"
                if self.monitor_state["stopped_at"] is None:
                    self.monitor_state["stopped_at"] = self._now()
                self._write_monitor_snapshot_locked()

    def _load_monitor_snapshot(self) -> None:
        payload = self._read_json(self.monitor_session_path)
        if not payload:
            return
        state = {**self._default_monitor_state(), **payload.get("state", {})}
        state["running"] = False
        if state.get("status") in {"running", "stopping"}:
            state["status"] = "interrupted"
            if state.get("stopped_at") is None:
                state["stopped_at"] = self._now()
        self.monitor_state = state
        self.monitor_events = list(payload.get("events", []))[-500:]
        if state.get("status") == "interrupted":
            self._write_monitor_snapshot_locked()

    def _write_monitor_snapshot_locked(self) -> None:
        payload = {
            "state": self.monitor_state,
            "events": self.monitor_events[-500:],
            "updated_at": self._now(),
        }
        self.monitor_session_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.monitor_session_path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        tmp_path.replace(self.monitor_session_path)

    def _safe_config_path(self, name: str) -> Path:
        if "/" in name or "\\" in name or not name.endswith(".json"):
            raise ValueError("Invalid config name")
        return self.configs_dir / name

    def _safe_experiment_path(self, name: str) -> Path:
        if "/" in name or "\\" in name or not name:
            raise ValueError("Invalid experiment name")
        safe = "".join(char for char in name if char.isalnum() or char in {"-", "_"}).strip("-_")
        if not safe:
            raise ValueError("Invalid experiment name")
        return self.experiments_dir / safe

    def _next_run_name(self) -> str:
        existing = set(self._experiment_names())
        index = len(existing) + 1
        while True:
            name = f"run-{index:03d}"
            if name not in existing:
                return name
            index += 1

    def _read_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped:
                    rows.append(json.loads(stripped))
        return rows

    def _default_settings(self) -> Any:
        configs = self._config_names()
        preferred = "paper-demo.json"
        if preferred in configs:
            try:
                return load_settings(str(self._safe_config_path(preferred)))
            except Exception:
                pass
        if configs:
            try:
                return load_settings(str(self._safe_config_path(configs[0])))
            except Exception:
                pass
        return load_settings(None)

    def _live_market_settings(self) -> tuple[str, Any]:
        configs = self._config_names()
        for preferred in ["mt5-paper.json", "mt5-demo-live.json"]:
            if preferred in configs:
                try:
                    return preferred, load_settings(str(self._safe_config_path(preferred)))
                except Exception:
                    pass
        for name in configs:
            try:
                settings = load_settings(str(self._safe_config_path(name)))
            except Exception:
                continue
            if getattr(settings, "market_source", "demo") == "mt5":
                return name, settings
        return "default", load_settings(None)

    def _agent_settings(self) -> tuple[str, Any]:
        monitor_config = self.monitor_state.get("config")
        if monitor_config:
            try:
                return str(monitor_config), load_settings(str(self._safe_config_path(str(monitor_config))))
            except Exception:
                pass
        configs = self._config_names()
        for preferred in ["mt5-paper.json", "paper-demo.json"]:
            if preferred in configs:
                try:
                    return preferred, load_settings(str(self._safe_config_path(preferred)))
                except Exception:
                    pass
        if configs:
            try:
                return configs[0], load_settings(str(self._safe_config_path(configs[0])))
            except Exception:
                pass
        return "default", load_settings(None)

    def _rooted_path(self, path: str | Path) -> Path:
        candidate = Path(path)
        if candidate.is_absolute():
            return candidate
        return self.root / candidate

    def _now(self) -> str:
        return datetime.now(LOCAL_TZ).isoformat()

    def _parse_datetime(self, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), timezone.utc)
        text = str(value).strip()
        if not text:
            return None
        if text.isdigit():
            return datetime.fromtimestamp(int(text), timezone.utc)
        try:
            normalized = text.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            return None

    def _format_local_time(self, value: Any) -> str:
        parsed = self._parse_datetime(value)
        if parsed is None:
            return str(value or "")
        return parsed.astimezone(LOCAL_TZ).strftime("%d %b %Y %H:%M:%S WIB")

    def _live_refresh_script(self) -> str:
        return """
<script>
(() => {
  const refreshMs = 3000;
  const $ = (id) => document.getElementById(id);
  const money = (value) => `$${Number(value || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  let marketCandleLimit = 80;
  let marketTimeframe = "H1";
  let tickTapeLimit = 120;
  let controlChartState = null;
  let controlChartCache = new Map();
  let controlChartRequestId = 0;

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function metricGrid(id, items) {
    const root = $(id);
    if (!root) return;
    clear(root);
    for (const [label, value] of items) {
      const metric = document.createElement("div");
      metric.className = "metric";
      const span = document.createElement("span");
      span.textContent = label;
      const strong = document.createElement("strong");
      strong.textContent = value ?? "";
      metric.append(span, strong);
      root.append(metric);
    }
  }

  function table(id, columns, rows, emptyText) {
    const root = $(id);
    if (!root) return;
    clear(root);
    if (!rows || rows.length === 0) {
      const p = document.createElement("p");
      p.textContent = emptyText;
      root.append(p);
      return;
    }
    const tableEl = document.createElement("table");
    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    for (const column of columns) {
      const th = document.createElement("th");
      th.textContent = column.label;
      headRow.append(th);
    }
    thead.append(headRow);
    const tbody = document.createElement("tbody");
    for (const row of rows) {
      const tr = document.createElement("tr");
      for (const column of columns) {
        const td = document.createElement("td");
        const value = typeof column.value === "function" ? column.value(row) : row[column.value];
        td.textContent = value ?? "";
        if (column.className) td.className = column.className(row);
        tr.append(td);
      }
      tbody.append(tr);
    }
    tableEl.append(thead, tbody);
    root.append(tableEl);
  }

  function countTable(id, counts, emptyText) {
    const rows = Object.entries(counts || {}).map(([key, value]) => ({ key, value }));
    table(id, [{ label: "Reason", value: "key" }, { label: "Count", value: "value" }], rows, emptyText);
  }

  function marketStrip(id, items) {
    const root = $(id);
    if (!root) return;
    clear(root);
    for (const [label, value] of items) {
      const item = document.createElement("span");
      const small = document.createElement("small");
      small.textContent = label;
      const strong = document.createElement("strong");
      strong.textContent = value ?? "";
      item.append(small, strong);
      root.append(item);
    }
  }

  function tickTapeChart(id, ticks) {
    const root = $(id);
    if (!root) return;
    clear(root);
    if (!ticks || ticks.length < 2) {
      const p = document.createElement("p");
      p.textContent = "No tick chart yet.";
      root.append(p);
      return;
    }
    const chronological = ticks.slice(0, 120).reverse();
    const bids = chronological.map((tick) => Number(tick.bid || 0));
    const asks = chronological.map((tick) => Number(tick.ask || 0));
    const spreads = chronological.map((tick) => Number(tick.spread_points || 0));
    const prices = bids.concat(asks);
    const upper = Math.max(...prices);
    const lower = Math.min(...prices);
    const priceSpan = upper - lower || 1;
    const spreadUpper = Math.max(...spreads) || 1;
    const width = 1080, height = 260, left = 58, right = 78, top = 34, bottom = 44;
    const plotWidth = width - left - right;
    const plotHeight = height - top - bottom;
    const count = Math.max(chronological.length - 1, 1);
    const pricePoint = (index, value) => `${(left + (index / count) * plotWidth).toFixed(2)},${(top + ((upper - value) / priceSpan) * plotHeight).toFixed(2)}`;
    const spreadPoint = (index, value) => `${(left + (index / count) * plotWidth).toFixed(2)},${(top + plotHeight - (value / spreadUpper) * Math.min(52, plotHeight * 0.35)).toFixed(2)}`;
    const svg = svgEl("svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Tick tape bid ask spread chart");
    const bg = svgEl("rect");
    bg.setAttribute("x", 0);
    bg.setAttribute("y", 0);
    bg.setAttribute("width", width);
    bg.setAttribute("height", height);
    bg.setAttribute("fill", "#0f1720");
    svg.append(bg);
    addSvgText(svg, "Tick Tape", left, 22, "market-title");
    const axis = svgEl("line");
    axis.setAttribute("x1", left);
    axis.setAttribute("y1", top + plotHeight);
    axis.setAttribute("x2", width - right);
    axis.setAttribute("y2", top + plotHeight);
    axis.setAttribute("class", "market-grid");
    svg.append(axis);
    const addLine = (points, color, widthValue, dash = "") => {
      const line = svgEl("polyline");
      line.setAttribute("points", points);
      line.setAttribute("fill", "none");
      line.setAttribute("stroke", color);
      line.setAttribute("stroke-width", widthValue);
      if (dash) line.setAttribute("stroke-dasharray", dash);
      svg.append(line);
    };
    addLine(bids.map((value, index) => pricePoint(index, value)).join(" "), "#22c55e", "2.4");
    addLine(asks.map((value, index) => pricePoint(index, value)).join(" "), "#ef4444", "1.8", "5 4");
    addLine(spreads.map((value, index) => spreadPoint(index, value)).join(" "), "#f2c94c", "1.6");
    const latest = chronological[chronological.length - 1] || {};
    addSvgText(svg, `bid ${latest.bid || ""} / ask ${latest.ask || ""}`, width - right - 180, 22, "market-label");
    addSvgText(svg, `${chronological.length} ticks, spread max ${Math.max(...spreads).toFixed(2)}`, left, height - 14, "market-label");
    root.append(svg);
  }

  function svgEl(name) {
    return document.createElementNS("http://www.w3.org/2000/svg", name);
  }

  function addSvgText(svg, text, x, y, className) {
    const node = svgEl("text");
    node.setAttribute("x", x);
    node.setAttribute("y", y);
    node.setAttribute("class", className);
    node.textContent = text;
    svg.append(node);
  }

  function lineChart(id, title, rows, xKey, yKey, labelKey) {
    const root = $(id);
    if (!root) return;
    clear(root);
    if (!rows || rows.length === 0) {
      const p = document.createElement("p");
      p.textContent = "No chart data yet.";
      root.append(p);
      return;
    }
    const values = rows.map((row) => Number(row[yKey] || 0));
    const lower = Math.min(0, ...values);
    const upper = Math.max(0, ...values);
    const span = upper - lower || 1;
    const width = 620, height = 260, left = 44, right = 18, top = 34, bottom = 34;
    const plotWidth = width - left - right;
    const plotHeight = height - top - bottom;
    const count = Math.max(rows.length - 1, 1);
    const svg = svgEl("svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", title);
    addSvgText(svg, title, left, 20, "chart-title");
    const zeroY = top + ((upper - 0) / span) * plotHeight;
    const xAxis = svgEl("line");
    xAxis.setAttribute("x1", left);
    xAxis.setAttribute("y1", zeroY);
    xAxis.setAttribute("x2", width - right);
    xAxis.setAttribute("y2", zeroY);
    xAxis.setAttribute("class", "chart-axis");
    svg.append(xAxis);
    const yAxis = svgEl("line");
    yAxis.setAttribute("x1", left);
    yAxis.setAttribute("y1", top);
    yAxis.setAttribute("x2", left);
    yAxis.setAttribute("y2", height - bottom);
    yAxis.setAttribute("class", "chart-axis");
    svg.append(yAxis);
    const points = [];
    rows.forEach((row, index) => {
      const value = Number(row[yKey] || 0);
      const x = left + (index / count) * plotWidth;
      const y = top + ((upper - value) / span) * plotHeight;
      points.push(`${x.toFixed(2)},${y.toFixed(2)}`);
      const dot = svgEl("circle");
      dot.setAttribute("cx", x);
      dot.setAttribute("cy", y);
      dot.setAttribute("r", 4);
      dot.setAttribute("class", "chart-fill-good");
      const titleNode = svgEl("title");
      titleNode.textContent = `${row[xKey]} ${row[labelKey] || ""}: ${value}R`;
      dot.append(titleNode);
      svg.append(dot);
    });
    const line = svgEl("polyline");
    line.setAttribute("points", points.join(" "));
    line.setAttribute("class", "chart-line");
    svg.append(line);
    addSvgText(svg, `${rows.length} closed trade points`, left, height - 8, "chart-label");
    addSvgText(svg, `min ${lower.toFixed(2)}R / max ${upper.toFixed(2)}R`, width - 140, height - 8, "chart-label");
    root.append(svg);
  }

  function barChart(id, title, rows, labelKey, valueKey) {
    const root = $(id);
    if (!root) return;
    clear(root);
    if (!rows || rows.length === 0) {
      const p = document.createElement("p");
      p.textContent = "No chart data yet.";
      root.append(p);
      return;
    }
    const selected = rows.slice(0, 8);
    const values = selected.map((row) => Number(row[valueKey] || 0));
    const lower = Math.min(0, ...values);
    const upper = Math.max(0, ...values);
    const span = upper - lower || 1;
    const width = 620, height = 260, left = 44, right = 18, top = 34, bottom = 58;
    const plotWidth = width - left - right;
    const plotHeight = height - top - bottom;
    const gap = 10;
    const barWidth = Math.max(18, (plotWidth - gap * (selected.length - 1)) / selected.length);
    const zeroY = top + ((upper - 0) / span) * plotHeight;
    const svg = svgEl("svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", title);
    addSvgText(svg, title, left, 20, "chart-title");
    const xAxis = svgEl("line");
    xAxis.setAttribute("x1", left);
    xAxis.setAttribute("y1", zeroY);
    xAxis.setAttribute("x2", width - right);
    xAxis.setAttribute("y2", zeroY);
    xAxis.setAttribute("class", "chart-axis");
    svg.append(xAxis);
    selected.forEach((row, index) => {
      const value = Number(row[valueKey] || 0);
      const x = left + index * (barWidth + gap);
      let y = top + ((upper - Math.max(value, 0)) / span) * plotHeight;
      if (value < 0) y = zeroY;
      const rect = svgEl("rect");
      rect.setAttribute("x", x);
      rect.setAttribute("y", y);
      rect.setAttribute("width", barWidth);
      rect.setAttribute("height", Math.max(2, Math.abs(value / span) * plotHeight));
      rect.setAttribute("class", value >= 0 ? "chart-fill-good" : "chart-fill-bad");
      const titleNode = svgEl("title");
      titleNode.textContent = `${row[labelKey] || ""} ${value}`;
      rect.append(titleNode);
      svg.append(rect);
      addSvgText(svg, String(row[labelKey] || "").slice(0, 10), x, height - 28, "chart-label");
      addSvgText(svg, value.toFixed(2), x, height - 12, "chart-label");
    });
    root.append(svg);
  }

  function candlestickChart(id, symbol, candles, overlays = {}, visibleLimit = marketCandleLimit) {
    const root = $(id);
    if (!root) return;
    clear(root);
    if (!candles || candles.length === 0) {
      const p = document.createElement("p");
      p.textContent = "No market candle history yet. Run Monitor with market_source: sim to generate chart data.";
      root.append(p);
      return;
    }
    const selected = candles.slice(-visibleLimit);
    const indicators = (overlays.indicators || []).slice(-selected.length);
    const highs = selected.map((row) => Number(row.high ?? row.close ?? 0));
    const lows = selected.map((row) => Number(row.low ?? row.close ?? 0));
    const currentTick = overlays.current_tick || {};
    for (const key of ["bid", "ask", "mid"]) {
      if (currentTick[key] !== undefined && currentTick[key] !== null) {
        highs.push(Number(currentTick[key]));
        lows.push(Number(currentTick[key]));
      }
    }
    for (const row of indicators) {
      for (const key of ["ma_fast", "ma_slow", "atr_upper", "atr_lower"]) {
        if (row[key] !== undefined && row[key] !== null) {
          highs.push(Number(row[key]));
          lows.push(Number(row[key]));
        }
      }
    }
    for (const position of overlays.open_positions || []) {
      for (const key of ["entry_price", "stop_loss", "take_profit"]) {
        if (position[key] !== undefined && position[key] !== null) {
          highs.push(Number(position[key]));
          lows.push(Number(position[key]));
        }
      }
    }
    for (const close of overlays.closed_positions || []) {
      for (const key of ["entry_price", "exit_price"]) {
        if (close[key] !== undefined && close[key] !== null) {
          highs.push(Number(close[key]));
          lows.push(Number(close[key]));
        }
      }
    }
    const upper = Math.max(...highs);
    const lower = Math.min(...lows);
    const span = upper - lower || 1;
    const width = 1080, height = 560, left = 68, right = 126, top = 48, bottom = 72;
    const plotWidth = width - left - right;
    const plotHeight = height - top - bottom;
    const count = Math.max(selected.length - 1, 1);
    const candleWidth = Math.max(9, Math.min(30, (plotWidth / Math.max(selected.length, 1)) * 0.55));
    const svg = svgEl("svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", `${symbol} candlestick chart`);
    const bg = svgEl("rect");
    bg.setAttribute("x", 0);
    bg.setAttribute("y", 0);
    bg.setAttribute("width", width);
    bg.setAttribute("height", height);
    bg.setAttribute("fill", "#0f1720");
    svg.append(bg);
    addSvgText(svg, `${symbol} Candlestick`, left, 28, "market-title");
    const yFor = (price) => top + ((upper - Number(price)) / span) * plotHeight;
    for (let i = 0; i < 6; i++) {
      const price = lower + ((upper - lower) / 5) * i;
      const y = yFor(price);
      const grid = svgEl("line");
      grid.setAttribute("x1", left);
      grid.setAttribute("y1", y);
      grid.setAttribute("x2", width - right);
      grid.setAttribute("y2", y);
      grid.setAttribute("class", "market-grid");
      svg.append(grid);
      addSvgText(svg, Number(price).toPrecision(6), width - right + 10, y + 4, "market-label");
    }
    const yAxis = svgEl("line");
    yAxis.setAttribute("x1", left);
    yAxis.setAttribute("y1", top);
    yAxis.setAttribute("x2", left);
    yAxis.setAttribute("y2", height - bottom);
    yAxis.setAttribute("class", "market-axis");
    svg.append(yAxis);
    const xAxis = svgEl("line");
    xAxis.setAttribute("x1", left);
    xAxis.setAttribute("y1", height - bottom);
    xAxis.setAttribute("x2", width - right);
    xAxis.setAttribute("y2", height - bottom);
    xAxis.setAttribute("class", "market-axis");
    svg.append(xAxis);
    const point = (index, value) => `${(left + (index / Math.max(indicators.length - 1, 1)) * plotWidth).toFixed(2)},${yFor(value).toFixed(2)}`;
    const poly = (key, klass) => {
      const points = indicators.map((row, index) => row[key] === undefined || row[key] === null ? null : point(index, Number(row[key]))).filter(Boolean);
      if (!points.length) return;
      const line = svgEl("polyline");
      line.setAttribute("points", points.join(" "));
      line.setAttribute("class", klass);
      svg.append(line);
    };
    const atrUpper = indicators.map((row, index) => row.atr_upper === undefined ? null : point(index, Number(row.atr_upper))).filter(Boolean);
    const atrLower = indicators.map((row, index) => row.atr_lower === undefined ? null : point(index, Number(row.atr_lower))).filter(Boolean).reverse();
    if (atrUpper.length && atrLower.length) {
      const atr = svgEl("polygon");
      atr.setAttribute("points", atrUpper.concat(atrLower).join(" "));
      atr.setAttribute("class", "market-atr");
      svg.append(atr);
    }
    poly("ma_fast", "market-ma-fast");
    poly("ma_slow", "market-ma-slow");
    selected.forEach((candle, index) => {
      const openPrice = Number(candle.open ?? candle.close ?? 0);
      const high = Number(candle.high ?? openPrice);
      const low = Number(candle.low ?? openPrice);
      const close = Number(candle.close ?? openPrice);
      const x = left + (index / count) * plotWidth;
      const highY = top + ((upper - high) / span) * plotHeight;
      const lowY = top + ((upper - low) / span) * plotHeight;
      const openY = top + ((upper - openPrice) / span) * plotHeight;
      const closeY = top + ((upper - close) / span) * plotHeight;
      const wick = svgEl("line");
      wick.setAttribute("x1", x);
      wick.setAttribute("y1", highY);
      wick.setAttribute("x2", x);
      wick.setAttribute("y2", lowY);
      wick.setAttribute("stroke", close >= openPrice ? "#1fbf86" : "#e05f5f");
      wick.setAttribute("stroke-width", "1.4");
      svg.append(wick);
      const rect = svgEl("rect");
      rect.setAttribute("x", x - candleWidth / 2);
      rect.setAttribute("y", Math.min(openY, closeY));
      rect.setAttribute("width", candleWidth);
      rect.setAttribute("height", Math.max(2, Math.abs(closeY - openY)));
      rect.setAttribute("class", close >= openPrice ? "chart-fill-good" : "chart-fill-bad");
      const titleNode = svgEl("title");
      titleNode.textContent = `${candle.step ?? index + 1} O:${openPrice} H:${high} L:${low} C:${close}`;
      rect.append(titleNode);
      svg.append(rect);
    });
    const lineStyles = [
      ["entry_price", "entry", "#2563eb"],
      ["stop_loss", "SL", "#a52727"],
      ["take_profit", "TP", "#146c43"],
    ];
    for (const position of overlays.open_positions || []) {
      for (const [key, label, color] of lineStyles) {
        if (position[key] === undefined || position[key] === null) continue;
        const y = yFor(position[key]);
        const line = svgEl("line");
        line.setAttribute("x1", left);
        line.setAttribute("y1", y);
        line.setAttribute("x2", width - right);
        line.setAttribute("y2", y);
        line.setAttribute("stroke", color);
        line.setAttribute("stroke-width", "1.5");
        line.setAttribute("stroke-dasharray", "5 4");
        const titleNode = svgEl("title");
        titleNode.textContent = `open ${position.side || ""} ${label}: ${position[key]}`;
        line.append(titleNode);
        svg.append(line);
        addSvgText(svg, label, width - right - 80, y - 4, "chart-label");
      }
    }
    for (const [key, label, color] of [["bid", "BID", "#22c55e"], ["ask", "ASK", "#ef4444"]]) {
      if (currentTick[key] === undefined || currentTick[key] === null) continue;
      const value = Number(currentTick[key]);
      const y = yFor(value);
      const labelY = Math.max(top + 16, Math.min(top + plotHeight - 8, y));
      const line = svgEl("line");
      line.setAttribute("x1", left);
      line.setAttribute("y1", y);
      line.setAttribute("x2", width - right);
      line.setAttribute("y2", y);
      line.setAttribute("stroke", color);
      line.setAttribute("stroke-width", key === "bid" ? "2" : "1.5");
      line.setAttribute("stroke-dasharray", key === "bid" ? "0" : "6 4");
      const titleNode = svgEl("title");
      titleNode.textContent = `${label} ${value}`;
      line.append(titleNode);
      svg.append(line);
      const badge = svgEl("rect");
      badge.setAttribute("x", width - right + 6);
      badge.setAttribute("y", labelY - 14);
      badge.setAttribute("width", 108);
      badge.setAttribute("height", 20);
      badge.setAttribute("rx", 4);
      badge.setAttribute("fill", color);
      svg.append(badge);
      const text = svgEl("text");
      text.setAttribute("x", width - right + 11);
      text.setAttribute("y", labelY + 1);
      text.setAttribute("fill", "#ffffff");
      text.setAttribute("font-size", "11");
      text.setAttribute("font-weight", "800");
      text.textContent = `${label} ${value.toFixed(3)}`;
      svg.append(text);
    }
    const signals = overlays.signals || [];
    if (signals.length) {
      const displayCount = Math.min(signals.length, Math.max(selected.length, 1), 12);
      const displaySignals = signals.slice(-displayCount);
      const candleOffset = Math.max(selected.length - displayCount, 0);
      displaySignals.forEach((signal, index) => {
        const candleIndex = Math.min(candleOffset + index, Math.max(selected.length - 1, 0));
        const candle = selected[candleIndex] || {};
        const x = left + (candleIndex / count) * plotWidth;
        const action = String(signal.action || "");
        const side = String(signal.side || "");
        let y = top + plotHeight - 16;
        let color = "#f2c94c";
        let label = "H";
        if (action !== "hold" && side === "sell") {
          const high = Number(candle.high ?? candle.close ?? upper);
          y = Math.max(top + 16, yFor(high) - 22);
          color = "#e05f5f";
          label = "S";
        } else {
          const low = Number(candle.low ?? candle.close ?? lower);
          y = Math.min(top + plotHeight - 16, yFor(low) + 22);
          if (action !== "hold") {
            color = "#1fbf86";
            label = "B";
          }
        }
        const circle = svgEl("circle");
        circle.setAttribute("cx", x);
        circle.setAttribute("cy", y);
        circle.setAttribute("r", 8);
        circle.setAttribute("fill", color);
        const titleNode = svgEl("title");
        titleNode.textContent = `${action} ${side} conf=${signal.confidence} risk=${signal.risk_status}`;
        circle.append(titleNode);
        svg.append(circle);
        addSvgText(svg, label, x - 3, y + 4, "market-signal-label");
      });
      const latestSignal = displaySignals[displaySignals.length - 1];
      const latestAction = String(latestSignal.action || "none");
      const latestRisk = String(latestSignal.risk_status || "");
      const latestConfidence = latestSignal.confidence ?? "";
      const latestReason = String((latestSignal.reason_codes || [])[0] || "no_reason").slice(0, 48);
      const badgeColor = latestAction === "hold" ? "#f2c94c" : String(latestSignal.side || "") === "sell" ? "#e05f5f" : "#1fbf86";
      const badge = svgEl("rect");
      badge.setAttribute("x", left + 178);
      badge.setAttribute("y", top - 34);
      badge.setAttribute("width", 390);
      badge.setAttribute("height", 34);
      badge.setAttribute("rx", 6);
      badge.setAttribute("fill", "#111827");
      badge.setAttribute("stroke", badgeColor);
      badge.setAttribute("stroke-width", "1.5");
      svg.append(badge);
      addSvgText(svg, `Latest Decision: ${latestAction} conf ${latestConfidence} risk ${latestRisk}`, left + 192, top - 14, "market-decision-title");
      addSvgText(svg, latestReason, left + 192, top - 3, "market-decision-reason");
    }
    const closes = (overlays.closed_positions || []).slice(-12);
    if (closes.length) {
      const step = Math.max(1, (width - right - left) / Math.max(closes.length, 1));
      closes.forEach((close, index) => {
        if (close.exit_price === undefined || close.exit_price === null) return;
        const x = left + index * step + step / 2;
        const y = top + 16;
        const marker = svgEl("polygon");
        marker.setAttribute("points", `${x},${y - 7} ${x + 7},${y + 7} ${x - 7},${y + 7}`);
        marker.setAttribute("fill", Number(close.r_multiple || 0) >= 0 ? "#146c43" : "#a52727");
        const titleNode = svgEl("title");
        titleNode.textContent = `closed ${close.exit_reason || ""} ${close.r_multiple || 0}R at ${close.exit_price}`;
        marker.append(titleNode);
        svg.append(marker);
      });
    }
    addSvgText(svg, `${selected.length} candles`, left, height - 20, "market-label");
    addSvgText(svg, `low ${lower} / high ${upper}`, width - 220, height - 20, "market-label");
    root.append(svg);
  }

  function countBarChart(id, title, counts) {
    const rows = Object.entries(counts || {}).slice(0, 8).map(([label, value]) => ({ label, value }));
    barChart(id, title, rows, "label", "value");
  }

  function renderControls(monitor) {
    const root = $("monitor-controls");
    if (!root) return;
    const template = monitor.running ? $("monitor-stop-template") : $("monitor-start-template");
    if (template) root.innerHTML = template.innerHTML;
  }

  function renderOperations(operations, monitor) {
    metricGrid("monitor-metrics", [
      ["Status", monitor.status],
      ["Running", monitor.running],
      ["Config", monitor.config || ""],
      ["Max Iterations", monitor.max_iterations || 0],
      ["Events", monitor.event_count || 0],
      ["Started", monitor.started_at || ""],
      ["Stopped", monitor.stopped_at || ""],
      ["Error", monitor.error || ""],
    ]);
    metricGrid("operations-metrics", [
      ["Mode", operations.mode],
      ["Market Source", operations.market_source],
      ["Brain", operations.brain || ""],
      ["Symbols", (operations.symbols || []).join(", ")],
      ["Experiences", operations.experience_count || 0],
      ["Open Positions", operations.open_position_count || 0],
      ["MT5 Open", operations.mt5_open_position_count || 0],
      ["Closed Positions", operations.closed_position_count || 0],
      ["Paper PnL (R)", operations.paper_total_r || 0],
      ["Est. PnL", money(operations.estimated_pnl)],
      ["Win Rate", operations.win_rate || 0],
      ["Memory", operations.memory_path || ""],
      ["Paper State", operations.paper_state_path || ""],
    ]);
    renderControls(monitor);
    table("open-positions-table", [
      { label: "Symbol", value: "symbol" },
      { label: "Side", value: "side" },
      { label: "Lot", value: "volume" },
      { label: "Entry", value: "entry_price" },
      { label: "SL", value: "stop_loss" },
      { label: "TP", value: "take_profit" },
      { label: "Opened", value: "opened_at" },
    ], operations.open_positions, "No open paper positions.");
    decisionHistory("recent-decisions-table", operations.recent_decisions);
    table("monitor-events-table", [
      { label: "Time", value: "timestamp" },
      { label: "Iter", value: "iteration" },
      { label: "Symbol", value: "symbol" },
      { label: "Action/Reason", value: (row) => row.action || row.reason || "" },
      { label: "Risk", value: "risk_status" },
      { label: "Execution", value: "execution" },
      { label: "Closed", value: "closed_positions" },
    ], monitor.recent_events, "No monitor events yet.");
    table("recent-closes-table", [
      { label: "Closed", value: "closed_at" },
      { label: "Symbol", value: "symbol" },
      { label: "Side", value: "side" },
      { label: "Reason", value: "exit_reason" },
      { label: "Entry", value: "entry_price" },
      { label: "Exit", value: "exit_price" },
      { label: "R", value: "r_multiple", className: (row) => Number(row.r_multiple || 0) > 0 ? "positive" : Number(row.r_multiple || 0) < 0 ? "negative" : "" },
    ], operations.recent_closes, "No closed paper positions yet.");
  }

  function renderPnl(pnl) {
    metricGrid("pnl-metrics", [
      ["Closed Trades", pnl.closed_position_count || 0],
      ["Paper PnL (R)", pnl.paper_total_r || 0],
      ["Est. PnL", money(pnl.estimated_pnl)],
      ["Risk / Trade", money(pnl.risk_amount)],
      ["Win Rate", pnl.win_rate || 0],
      ["Avg Win R", pnl.average_win_r || 0],
      ["Avg Loss R", pnl.average_loss_r || 0],
      ["Max DD R", pnl.max_drawdown_r || 0],
    ]);
    lineChart("equity-curve", "Cumulative R", pnl.equity_curve, "index", "cumulative_r", "symbol");
    barChart("symbol-r-chart", "Symbol Total R", pnl.by_symbol, "symbol", "total_r");
    countBarChart("decision-reason-chart", "Decision Reasons", pnl.decision_reasons);
    countBarChart("rejection-reason-chart", "Rejection Reasons", pnl.rejection_reasons);
    table("reason-quality-table", [
      { label: "Reason", value: "reason" },
      { label: "Count", value: "count" },
      { label: "Approved", value: "approved" },
      { label: "Rejected", value: "rejected" },
      { label: "Executed", value: "executed" },
      { label: "Approval Rate", value: "approved_rate", className: (row) => Number(row.approved_rate || 0) >= 0.7 ? "positive" : Number(row.approved_rate || 0) < 0.4 ? "negative" : "" },
      { label: "Execution Rate", value: "executed_rate" },
      { label: "Avg Confidence", value: "average_confidence" },
      { label: "Closed", value: "closed_count" },
      { label: "Total R", value: "total_r" },
      { label: "Avg R", value: "average_r" },
    ], pnl.reason_quality, "No reason quality data yet.");
    table("symbol-performance-table", [
      { label: "Symbol", value: "symbol" },
      { label: "Closed", value: "closed" },
      { label: "Wins", value: "wins" },
      { label: "Losses", value: "losses" },
      { label: "Win Rate", value: "win_rate" },
      { label: "Avg R", value: "average_r" },
      { label: "Total R", value: "total_r", className: (row) => Number(row.total_r || 0) > 0 ? "positive" : Number(row.total_r || 0) < 0 ? "negative" : "" },
      { label: "Est. PnL", value: (row) => money(row.estimated_pnl) },
    ], pnl.by_symbol, "No symbol performance yet.");
    countTable("decision-reasons-table", pnl.decision_reasons, "No data yet.");
    countTable("rejection-reasons-table", pnl.rejection_reasons, "No data yet.");
  }

  function decisionCard(id, decision) {
    const root = $(id);
    if (!root) return;
    clear(root);
    if (!decision || !decision.action) {
      const p = document.createElement("p");
      p.textContent = "No decisions recorded yet. Start the agent with mt5-paper.json to collect decisions.";
      root.append(p);
      return;
    }
    const card = document.createElement("div");
    card.className = "decision-card";
    const addItem = (label, value) => {
      const item = document.createElement("div");
      const span = document.createElement("span");
      span.textContent = label;
      const strong = document.createElement("strong");
      strong.textContent = value ?? "";
      item.append(span, strong);
      card.append(item);
    };
    addItem("Time", decision.timestamp || "");
    addItem("Symbol", decision.symbol || "");
    addItem("Action", decision.action || "");
    addItem("Confidence", decision.confidence || "");
    addItem("Risk", decision.risk_status || "");
    addItem("Execution", decision.execution || "");
    addItem("Brain", decision.analyst_model || "");
    addItem("Bias", decision.analyst_bias || "");
    addItem("Regime", decision.analyst_regime || "");
    addItem("Avoid", decision.analyst_avoid_trade === undefined ? "" : String(decision.analyst_avoid_trade));
    const reasons = document.createElement("div");
    reasons.className = "decision-reasons";
    const span = document.createElement("span");
    span.textContent = "Reasons";
    const ul = document.createElement("ul");
    for (const reason of decision.reasons || ["none"]) {
      const li = document.createElement("li");
      li.textContent = reason;
      ul.append(li);
    }
    reasons.append(span, ul);
    card.append(reasons);
    const notes = document.createElement("div");
    notes.className = "decision-reasons";
    const notesSpan = document.createElement("span");
    notesSpan.textContent = "Analyst Risk Notes";
    const notesList = document.createElement("ul");
    for (const note of decision.analyst_risk_notes || ["none"]) {
      const li = document.createElement("li");
      li.textContent = note;
      notesList.append(li);
    }
    notes.append(notesSpan, notesList);
    card.append(notes);
    if (decision.llm_trace && Object.keys(decision.llm_trace).length) {
      const trace = document.createElement("div");
      trace.className = "llm-trace";
      const title = document.createElement("span");
      title.textContent = "LLM Trace";
      trace.append(title);
      for (const [summaryText, value] of [
        ["Request sent to LLM", decision.llm_trace.request || {}],
        ["Response from LLM", decision.llm_trace.response || {}],
      ]) {
        const details = document.createElement("details");
        const summary = document.createElement("summary");
        summary.textContent = summaryText;
        const pre = document.createElement("pre");
        pre.textContent = JSON.stringify(value, null, 2);
        details.append(summary, pre);
        trace.append(details);
      }
      card.append(trace);
    }
    root.append(card);
  }

  function decisionHistory(id, rows) {
    const root = $(id);
    if (!root) return;
    clear(root);
    if (!rows || !rows.length) {
      const p = document.createElement("p");
      p.textContent = "No decisions recorded yet.";
      root.append(p);
      return;
    }
    const list = document.createElement("div");
    list.className = "decision-history";
    for (const row of rows) {
      const item = document.createElement("article");
      item.className = "decision-history-item";
      const head = document.createElement("div");
      head.className = "decision-history-head";
      for (const [label, value] of [
        ["Time", row.timestamp || ""],
        ["Symbol", row.symbol || ""],
        ["Action", row.action || ""],
        ["Confidence", row.confidence || ""],
        ["Risk", row.risk_status || ""],
        ["Brain", row.analyst_model || ""],
        ["Execution", row.execution || "not sent"],
      ]) {
        const cell = document.createElement("div");
        const span = document.createElement("span");
        span.textContent = label;
        const strong = document.createElement("strong");
        strong.textContent = value;
        cell.append(span, strong);
        head.append(cell);
      }
      const reasons = document.createElement("div");
      reasons.className = "decision-history-reasons";
      const label = document.createElement("span");
      label.textContent = "Reasons";
      const ul = document.createElement("ul");
      for (const reason of row.reasons || ["none"]) {
        const li = document.createElement("li");
        li.textContent = reason;
        ul.append(li);
      }
      reasons.append(label, ul);
      item.append(head, reasons);
      list.append(item);
    }
    root.append(list);
  }

  function renderAgent(consoleState) {
    const monitor = consoleState.monitor || {};
    const latest = consoleState.latest_decision || {};
    marketStrip("agent-status-strip", [
      ["Monitor", monitor.status || "stopped"],
      ["Trigger", monitor.trigger_mode || "interval"],
      ["Config", consoleState.config || ""],
      ["Mode", consoleState.mode || ""],
      ["Brain", consoleState.brain || ""],
      ["Market", consoleState.market_source || ""],
      ["Symbol", (consoleState.symbols || []).join(", ")],
      ["MT5 Open", consoleState.mt5_open_position_count || 0],
      ["Latest", latest.action || "none"],
      ["Risk", latest.risk_status || ""],
    ]);
    decisionCard("agent-decision-card", latest);
    decisionHistory("agent-decisions-table", consoleState.recent_decisions);
    table("agent-open-positions", [
      { label: "Symbol", value: "symbol" },
      { label: "Side", value: "side" },
      { label: "Lot", value: "volume" },
      { label: "Entry", value: "entry_price" },
      { label: "SL", value: "stop_loss" },
      { label: "TP", value: "take_profit" },
      { label: "Opened", value: "opened_at" },
    ], consoleState.open_positions, "No open paper positions.");
    table("agent-mt5-open-positions", [
      { label: "Ticket", value: "ticket" },
      { label: "Symbol", value: "symbol" },
      { label: "Side", value: "side" },
      { label: "Lot", value: "volume" },
      { label: "Entry", value: "entry_price" },
      { label: "SL", value: "stop_loss" },
      { label: "TP", value: "take_profit" },
      { label: "Profit", value: "profit" },
      { label: "Opened", value: "opened_at" },
    ], consoleState.mt5_open_positions, "No open MT5 positions.");
    table("agent-events-table", [
      { label: "Time", value: "timestamp" },
      { label: "Iter", value: "iteration" },
      { label: "Symbol", value: "symbol" },
      { label: "Action/Reason", value: (row) => row.action || row.reason || "" },
      { label: "Risk", value: "risk_status" },
      { label: "Execution", value: "execution" },
      { label: "Closed", value: "closed_positions" },
    ], (monitor.recent_events || []), "No monitor events yet.");
  }

  async function refreshOperations() {
    if (!$("monitor-metrics")) return;
    const [operations, monitor] = await Promise.all([
      fetch("/api/operations").then((res) => res.json()),
      fetch("/api/monitor").then((res) => res.json()),
    ]);
    renderOperations(operations, monitor);
  }

  async function refreshPnl() {
    if (!$("pnl-metrics")) return;
    const pnl = await fetch("/api/pnl").then((res) => res.json());
    renderPnl(pnl);
  }

  async function refreshAgent() {
    if (!$("agent-status-strip")) return;
    const consoleState = await fetch("/api/agent-console").then((res) => res.json());
    renderAgent(consoleState);
  }

  async function refreshMarket() {
    if (!$("market-candles")) return;
    const timeframeSelect = $("market-timeframe-select");
    const candleInput = $("market-candle-limit");
    marketTimeframe = timeframeSelect ? timeframeSelect.value : marketTimeframe;
    marketCandleLimit = Math.max(20, Math.min(300, Number(candleInput && candleInput.value ? candleInput.value : marketCandleLimit)));
    if (candleInput) candleInput.value = marketCandleLimit;
    const chart = await fetch(`/api/market-chart?timeframe=${encodeURIComponent(marketTimeframe)}&candles=${marketCandleLimit}`).then((res) => res.json());
    const select = $("market-symbol-select");
    const selected = (select && select.value) || (chart.symbols || [])[0] || "";
    const count = ((chart.series || {})[selected] || []).length;
    if ($("market-selected-symbol")) $("market-selected-symbol").textContent = selected;
    if ($("market-candle-count")) $("market-candle-count").textContent = count;
    if ($("market-timeframe-label")) $("market-timeframe-label").textContent = chart.timeframe || marketTimeframe;
    candlestickChart("market-candles", selected, (chart.series || {})[selected] || [], (chart.overlays || {})[selected] || {});
  }

  async function refreshLiveMarket() {
    if (!$("market-live-strip")) return;
    const live = await fetch("/api/live-market").then((res) => res.json());
    const account = live.account || {};
    const tick = (live.ticks || [])[0] || {};
    marketStrip("market-live-strip", [
      ["Status", live.status || ""],
      ["Bid", tick.bid ?? ""],
      ["Ask", tick.ask ?? ""],
      ["Spread", tick.spread_points ?? ""],
      ["Equity", account.equity === undefined ? "" : money(account.equity)],
      ["Open", account.open_positions ?? ""],
      ["Updated", live.updated_at_display || live.updated_at || ""],
    ]);
  }

  async function refreshTickTape() {
    if (!$("tick-tape-metrics")) return;
    const limitSelect = $("tick-tape-limit");
    tickTapeLimit = Math.max(1, Math.min(500, Number(limitSelect && limitSelect.value ? limitSelect.value : tickTapeLimit)));
    const payload = await fetch(`/api/tick-tape?limit=${tickTapeLimit}`).then((res) => res.json());
    const summary = payload.summary || {};
    const collector = payload.collector || {};
    marketStrip("tick-tape-metrics", [
      ["Source", summary.latest_source_label || collector.active_source_label || ""],
      ["Collector", collector.polling_active ? "polling" : collector.polling_suppressed_by_ea ? "suppressed" : "stopped"],
      ["Ticks", summary.count || 0],
      ["Ticks/min", summary.ticks_per_minute || 0],
      ["Tick Age", summary.latest_age_seconds === null || summary.latest_age_seconds === undefined ? "" : `${summary.latest_age_seconds}s`],
      ["Latest Symbol", summary.latest_symbol || ""],
      ["Latest Bid", summary.latest_bid ?? ""],
      ["Latest Ask", summary.latest_ask ?? ""],
      ["Latest Spread", summary.latest_spread ?? ""],
    ]);
    tickTapeChart("tick-tape-chart", payload.ticks || []);
  }

  function controlStatusList(id, items) {
    const root = $(id);
    if (!root) return;
    clear(root);
    for (const [label, value] of items) {
      const item = document.createElement("div");
      const span = document.createElement("span");
      span.textContent = label;
      const strong = document.createElement("strong");
      strong.textContent = value ?? "";
      item.append(span, strong);
      root.append(item);
    }
  }

  function renderControl(payload) {
    if (!$("control-root")) return;
    const live = payload.live || {};
    const summary = live.summary || {};
    const tick = live.latest_tick || {};
    const collector = live.collector || {};
    const agent = payload.agent || {};
    const monitor = agent.monitor || {};
    const latest = agent.latest_decision || {};
    const operations = payload.operations || {};
    marketStrip("control-live-strip", [
      ["Symbol", summary.latest_symbol || ""],
      ["Bid", summary.latest_bid ?? ""],
      ["Ask", summary.latest_ask ?? ""],
      ["Spread", summary.latest_spread ?? ""],
      ["Source", summary.latest_source_label || ""],
      ["Tick Time", tick.tick_time_display || ""],
      ["Agent", latest.action || "none"],
      ["Risk", latest.risk_status || ""],
    ]);
    controlStatusList("control-tick-status", [
      ["Source", summary.latest_source_label || collector.active_source_label || ""],
      ["Collector", collector.polling_active ? "polling" : collector.polling_suppressed_by_ea ? "suppressed" : "stopped"],
      ["Tape", summary.count || 0],
      ["Ticks/min", summary.ticks_per_minute || 0],
      ["Age", summary.latest_age_seconds === null || summary.latest_age_seconds === undefined ? "" : `${summary.latest_age_seconds}s`],
      ["Delta", summary.bid_delta || 0],
      ["Spread", `${summary.spread_min || 0} / ${summary.spread_max || 0}`],
    ]);
    tickTapeChart("control-tick-chart", payload.ticks || []);
    updateControlChartFromTick(tick);
    marketStrip("control-agent-strip", [
      ["Monitor", monitor.status || "stopped"],
      ["Trigger", monitor.trigger_mode || "interval"],
      ["Config", agent.config || ""],
      ["Mode", agent.mode || ""],
      ["Brain", agent.brain || ""],
      ["Market", agent.market_source || ""],
      ["Symbol", (agent.symbols || []).join(", ")],
      ["MT5 Open", agent.mt5_open_position_count || 0],
      ["Latest", latest.action || "none"],
      ["Risk", latest.risk_status || ""],
    ]);
    decisionCard("control-decision-card", latest);
    table("control-open-positions", [
      { label: "Symbol", value: "symbol" },
      { label: "Side", value: "side" },
      { label: "Lot", value: "volume" },
      { label: "Entry", value: "entry_price" },
      { label: "SL", value: "stop_loss" },
      { label: "TP", value: "take_profit" },
      { label: "Opened", value: "opened_at" },
    ], agent.open_positions || [], "No open paper positions.");
    table("control-mt5-open-positions", [
      { label: "Ticket", value: "ticket" },
      { label: "Symbol", value: "symbol" },
      { label: "Side", value: "side" },
      { label: "Lot", value: "volume" },
      { label: "Entry", value: "entry_price" },
      { label: "SL", value: "stop_loss" },
      { label: "TP", value: "take_profit" },
      { label: "Profit", value: "profit" },
      { label: "Opened", value: "opened_at" },
    ], agent.mt5_open_positions || [], "No open MT5 positions.");
    decisionHistory("control-history", agent.recent_decisions || []);
    table("control-recent-closes", [
      { label: "Closed", value: "closed_at" },
      { label: "Symbol", value: "symbol" },
      { label: "Side", value: "side" },
      { label: "Reason", value: "exit_reason" },
      { label: "Entry", value: "entry_price" },
      { label: "Exit", value: "exit_price" },
      { label: "R", value: "r_multiple", className: (row) => Number(row.r_multiple || 0) > 0 ? "positive" : Number(row.r_multiple || 0) < 0 ? "negative" : "" },
    ], operations.recent_closes || [], "No closed paper positions yet.");
    table("control-agent-events", [
      { label: "Time", value: "timestamp" },
      { label: "Iter", value: "iteration" },
      { label: "Symbol", value: "symbol" },
      { label: "Action/Reason", value: (row) => row.action || row.reason || "" },
      { label: "Risk", value: "risk_status" },
      { label: "Execution", value: "execution" },
      { label: "Closed", value: "closed_positions" },
    ], monitor.recent_events || [], "No monitor events yet.");
    if (payload.chart) {
      renderControlChart(payload.chart);
    }
  }

  function renderControlChart(chart) {
    if (!$("control-market-candles")) return;
    controlChartState = chart;
    const select = $("control-symbol-select");
    const selected = (select && select.value) || (chart.symbols || [])[0] || "";
    const candleInput = $("control-candle-limit");
    const visibleLimit = Math.max(20, Math.min(300, Number(candleInput && candleInput.value ? candleInput.value : 80)));
    if (candleInput) candleInput.value = visibleLimit;
    const series = (chart.series || {})[selected] || [];
    const count = Math.min(series.length, visibleLimit);
    if ($("control-chart-source")) $("control-chart-source").textContent = chart.source || "";
    if ($("control-selected-symbol")) $("control-selected-symbol").textContent = selected;
    if ($("control-candle-count")) $("control-candle-count").textContent = count;
    if ($("control-timeframe-label")) $("control-timeframe-label").textContent = chart.timeframe || "";
    const key = `${selected}:${chart.timeframe || ""}`;
    if (series.length) controlChartCache.set(key, chart);
    candlestickChart("control-market-candles", selected, series, (chart.overlays || {})[selected] || {}, visibleLimit);
  }

  function updateControlChartFromTick(tick) {
    if (!controlChartState || !tick || !tick.symbol || !$("control-market-candles")) return;
    const select = $("control-symbol-select");
    const selected = (select && select.value) || tick.symbol;
    if (selected !== tick.symbol) return;
    const series = controlChartState.series || {};
    const overlays = controlChartState.overlays || {};
    const candles = (series[selected] || []).map((row) => ({ ...row }));
    if (!candles.length) return;
    const bid = Number(tick.bid || 0);
    const ask = Number(tick.ask || 0);
    const mid = Number(tick.mid || ((bid + ask) / 2));
    if (!Number.isFinite(mid) || mid <= 0) return;
    const last = { ...candles[candles.length - 1] };
    const high = Number(last.high ?? last.close ?? mid);
    const low = Number(last.low ?? last.close ?? mid);
    last.close = mid;
    last.high = Math.max(high, mid, bid, ask);
    last.low = Math.min(low, mid, bid, ask);
    last.live_source = tick.source || "ea_socket";
    candles[candles.length - 1] = last;
    series[selected] = candles;
    overlays[selected] = {
      ...(overlays[selected] || {}),
      current_tick: {
        bid,
        ask,
        mid,
        spread_points: tick.spread_points,
        time: tick.tick_time,
        source: tick.source || "ea_socket",
      },
    };
    controlChartState = { ...controlChartState, source: "mt5_ohlc_history + ea_socket_live_tick", series, overlays };
    if ($("control-chart-source")) $("control-chart-source").textContent = controlChartState.source;
    candlestickChart("control-market-candles", selected, candles, overlays[selected] || {}, Math.max(20, Math.min(300, Number($("control-candle-limit")?.value || 80))));
  }

  async function refreshControlChart(options = {}) {
    if (!$("control-market-candles")) return;
    const forceFetch = Boolean(options.forceFetch);
    const timeframeSelect = $("control-timeframe-select");
    const symbolSelect = $("control-symbol-select");
    const candleInput = $("control-candle-limit");
    const selected = symbolSelect ? symbolSelect.value : ((controlChartState?.symbols || [])[0] || "");
    const timeframe = timeframeSelect ? timeframeSelect.value : "H1";
    const candles = Math.max(20, Math.min(300, Number(candleInput && candleInput.value ? candleInput.value : 80)));
    if (candleInput) candleInput.value = candles;
    const cacheKey = `${selected}:${timeframe}`;
    const cached = controlChartCache.get(cacheKey);
    const cachedSeries = cached ? ((cached.series || {})[selected] || []) : [];
    if (!forceFetch && cached && cachedSeries.length >= candles) {
      renderControlChart(cached);
      return;
    }
    const requestId = ++controlChartRequestId;
    if ($("control-chart-source")) $("control-chart-source").textContent = "loading candles...";
    const chart = await fetch(`/api/market-chart?timeframe=${encodeURIComponent(timeframe)}&candles=300`).then((res) => res.json());
    if (requestId !== controlChartRequestId) return;
    renderControlChart(chart);
  }

  function connectControlStream() {
    if (!$("control-root") || !window.EventSource) return;
    const state = $("control-stream-state");
    const setState = (label, klass) => {
      if (!state) return;
      state.textContent = label;
      state.className = `control-pill ${klass || ""}`.trim();
    };
    const source = new EventSource("/events");
    source.addEventListener("open", () => setState("sse live", "live"));
    source.addEventListener("snapshot", (event) => {
      setState("sse live", "live");
      renderControl(JSON.parse(event.data));
    });
    source.addEventListener("update", (event) => {
      setState("sse live", "live");
      renderControl(JSON.parse(event.data));
    });
    source.addEventListener("error", () => setState("reconnecting", "error"));
  }

  async function refreshLivePanels() {
    try {
      await Promise.all([refreshOperations(), refreshPnl(), refreshAgent(), refreshMarket(), refreshLiveMarket(), refreshTickTape()]);
    } catch (error) {
      console.warn("live refresh failed", error);
    }
  }

  if ($("monitor-metrics") || $("pnl-metrics") || $("market-candles")) {
    const candleInput = $("market-candle-limit");
    if (candleInput) marketCandleLimit = Math.max(20, Math.min(300, Number(candleInput.value || 80)));
    const timeframeSelect = $("market-timeframe-select");
    if (timeframeSelect) marketTimeframe = timeframeSelect.value || "H1";
    const tickTapeLimitSelect = $("tick-tape-limit");
    if (tickTapeLimitSelect) {
      tickTapeLimit = Number(tickTapeLimitSelect.value || 120);
      tickTapeLimitSelect.addEventListener("change", refreshTickTape);
    }
    if ($("market-symbol-select")) {
      $("market-symbol-select").addEventListener("change", refreshMarket);
    }
    if (timeframeSelect) {
      timeframeSelect.addEventListener("change", refreshMarket);
    }
    if (candleInput) {
      candleInput.addEventListener("change", refreshMarket);
    }
    if ($("market-zoom-in")) {
      $("market-zoom-in").addEventListener("click", () => {
        marketCandleLimit = Math.max(20, marketCandleLimit - 20);
        if (candleInput) candleInput.value = marketCandleLimit;
        refreshMarket();
      });
    }
    if ($("market-zoom-out")) {
      $("market-zoom-out").addEventListener("click", () => {
        marketCandleLimit = Math.min(300, marketCandleLimit + 20);
        if (candleInput) candleInput.value = marketCandleLimit;
        refreshMarket();
      });
    }
    if ($("market-reset-view")) {
      $("market-reset-view").addEventListener("click", () => {
        marketCandleLimit = 80;
        marketTimeframe = "H1";
        if (candleInput) candleInput.value = marketCandleLimit;
        if (timeframeSelect) timeframeSelect.value = marketTimeframe;
        refreshMarket();
      });
    }
    refreshLivePanels();
    setInterval(refreshLivePanels, refreshMs);
    if ($("live-market-metrics")) {
      setInterval(refreshLiveMarket, 1000);
    }
  }
  if ($("control-root")) {
    const candleInput = $("control-candle-limit");
    const timeframeSelect = $("control-timeframe-select");
    if ($("control-symbol-select")) {
      $("control-symbol-select").addEventListener("change", () => refreshControlChart({ forceFetch: true }));
    }
    if (timeframeSelect) {
      timeframeSelect.addEventListener("change", () => refreshControlChart({ forceFetch: true }));
    }
    if (candleInput) {
      candleInput.addEventListener("change", refreshControlChart);
    }
    if ($("control-zoom-in")) {
      $("control-zoom-in").addEventListener("click", () => {
        const current = Number(candleInput && candleInput.value ? candleInput.value : 80);
        if (candleInput) candleInput.value = Math.max(20, current - 20);
        refreshControlChart();
      });
    }
    if ($("control-zoom-out")) {
      $("control-zoom-out").addEventListener("click", () => {
        const current = Number(candleInput && candleInput.value ? candleInput.value : 80);
        if (candleInput) candleInput.value = Math.min(300, current + 20);
        refreshControlChart();
      });
    }
    if ($("control-reset-view")) {
      $("control-reset-view").addEventListener("click", () => {
        if (candleInput) candleInput.value = 80;
        if (timeframeSelect) timeframeSelect.value = "H1";
        refreshControlChart({ forceFetch: true });
      });
    }
    refreshControlChart({ forceFetch: true });
    connectControlStream();
  }
})();
</script>
"""

    def _events(self, request: BaseHTTPRequestHandler) -> None:
        request.send_response(200)
        request.send_header("Content-Type", "text/event-stream; charset=utf-8")
        request.send_header("Cache-Control", "no-cache")
        request.send_header("Connection", "keep-alive")
        request.end_headers()

        def send(event_name: str, payload: dict[str, Any]) -> bool:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            try:
                request.wfile.write(f"event: {event_name}\n".encode("utf-8"))
                request.wfile.write(b"data: ")
                request.wfile.write(body)
                request.wfile.write(b"\n\n")
                request.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        last_sequence = self.event_sequence
        if not send("snapshot", self._control_summary(include_chart=False)):
            return

        while True:
            with self.event_condition:
                self.event_condition.wait(timeout=20)
                sequence = self.event_sequence
            if sequence == last_sequence:
                try:
                    request.wfile.write(b": heartbeat\n\n")
                    request.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                continue
            last_sequence = sequence
            if not send("update", self._control_summary(include_chart=False)):
                return

    def _html(self, request: BaseHTTPRequestHandler, content: str) -> None:
        self._send(request, 200, content.encode("utf-8"), "text/html; charset=utf-8")

    def _json(self, request: BaseHTTPRequestHandler, payload: dict[str, Any]) -> None:
        self._send(request, 200, json.dumps(payload).encode("utf-8"), "application/json")

    def _file(self, request: BaseHTTPRequestHandler, path: Path, content_type: str) -> None:
        if not path.exists():
            self._not_found(request)
            return
        self._send(request, 200, path.read_bytes(), content_type)

    def _redirect(self, request: BaseHTTPRequestHandler, location: str) -> None:
        request.send_response(303)
        request.send_header("Location", location)
        request.end_headers()

    def _not_found(self, request: BaseHTTPRequestHandler) -> None:
        self._send(request, 404, b"Not found", "text/plain")

    def _send(self, request: BaseHTTPRequestHandler, status: int, body: bytes, content_type: str) -> None:
        try:
            request.send_response(status)
            request.send_header("Content-Type", content_type)
            request.send_header("Content-Length", str(len(body)))
            request.send_header("Connection", "close")
            request.end_headers()
            request.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _escape(self, value: Any) -> str:
        return html.escape(str(value), quote=True)


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def serve(host: str = "127.0.0.1", port: int = 8787, root: str | Path = ".", monitor_fn: Any | None = None) -> None:
    app = WebApp(root, monitor_fn=monitor_fn)
    app.start_tick_collector(interval_seconds=1.0)
    server = QuietThreadingHTTPServer((host, port), app.handler())
    print(f"Quantz web UI listening on http://{host}:{port}")
    try:
        server.serve_forever()
    finally:
        app.stop_tick_collector()
