import json
import time

from quantz.web import WebApp
from quantz.models import AccountState, AnalystOutput, ExecutionResult, MarketSnapshot, OrderRequest, OrderSide
from quantz.paper import PaperPortfolio


def test_web_index_lists_configs(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "paper-demo.json").write_text(json.dumps({"symbols": ["XAUUSD"]}), encoding="utf-8")

    html = WebApp(tmp_path)._index()

    assert "Quantz" in html
    assert "paper-demo.json" in html


def test_web_config_page_shows_parsed_settings(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "paper-demo.json").write_text(json.dumps({"symbols": ["XAUUSD"], "analyst": "rule"}), encoding="utf-8")

    html = WebApp(tmp_path)._config_page("paper-demo.json")

    assert "Valid" in html
    assert "analyst" in html
    assert "rule" in html


def test_web_api_lists_experiments(tmp_path):
    experiment = tmp_path / "data" / "experiments" / "run-001"
    experiment.mkdir(parents=True)

    payload = WebApp(tmp_path)._api("/api/experiments")

    assert payload == {"experiments": ["run-001"]}


def test_web_performance_summary_aggregates_paper_pnl(tmp_path):
    experiment = tmp_path / "data" / "experiments" / "run-001"
    experiment.mkdir(parents=True)
    (experiment / "base-config.json").write_text(
        json.dumps({"paper_start_equity": 10000, "default_risk_percent": 0.25}),
        encoding="utf-8",
    )
    (experiment / "base-report.json").write_text(
        json.dumps(
            {
                "total_r_multiple": 2.0,
                "closed_position_count": 2,
                "open_position_count": 1,
                "win_count": 2,
                "loss_count": 0,
            }
        ),
        encoding="utf-8",
    )
    (experiment / "review.json").write_text(json.dumps({"promotion_status": "paper_only"}), encoding="utf-8")
    (experiment / "comparison.json").write_text(json.dumps({"verdict": "continue_paper_test"}), encoding="utf-8")

    summary = WebApp(tmp_path)._performance_summary()

    assert summary["paper_total_r"] == 2.0
    assert summary["estimated_pnl"] == 50.0
    assert summary["win_rate"] == 1.0


def test_web_index_shows_agent_performance(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "paper-demo.json").write_text(json.dumps({"symbols": ["XAUUSD"]}), encoding="utf-8")

    html = WebApp(tmp_path)._index()

    assert "Agent Performance" in html
    assert "Paper PnL (R)" in html


def test_web_operations_summary_reads_paper_state_and_memory(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "paper-demo.json").write_text(
        json.dumps(
            {
                "symbols": ["XAUUSD"],
                "memory_path": "data/experience.jsonl",
                "paper_state_path": "data/paper-state.json",
                "paper_start_equity": 10000,
                "default_risk_percent": 0.25,
            }
        ),
        encoding="utf-8",
    )
    data = tmp_path / "data"
    data.mkdir()
    (data / "paper-state.json").write_text(
        json.dumps(
            {
                "open_positions": [{"symbol": "XAUUSD", "side": "buy", "entry_price": 100}],
                "closed_positions": [{"symbol": "XAUUSD", "side": "buy", "r_multiple": 2.0}],
            }
        ),
        encoding="utf-8",
    )
    (data / "experience.jsonl").write_text(
        json.dumps(
            {
                "decision": {
                    "timestamp": "2026-06-05T00:00:00+00:00",
                    "symbol": "XAUUSD",
                    "action": "open_position",
                    "confidence": 0.8,
                    "reason_codes": ["bullish_market_structure"],
                },
                "risk": {"status": "approved"},
                "execution": {"message": "paper_order_filled"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    summary = WebApp(tmp_path)._operations_summary()

    assert summary["open_position_count"] == 1
    assert summary["closed_position_count"] == 1
    assert summary["experience_count"] == 1
    assert summary["paper_total_r"] == 2.0
    assert summary["estimated_pnl"] == 50.0
    assert summary["recent_decisions"][0]["symbol"] == "XAUUSD"


def test_web_operations_page_shows_agent_status(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "paper-demo.json").write_text(json.dumps({"symbols": ["XAUUSD"]}), encoding="utf-8")

    html = WebApp(tmp_path)._operations_page()

    assert "Agent Status" in html
    assert "Open Positions" in html
    assert "Recent Decisions" in html
    assert "/api/monitor" in html
    assert "/api/operations" in html
    assert "setInterval(refreshLivePanels" in html


def test_web_agent_page_shows_control_and_console(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "mt5-paper.json").write_text(
        json.dumps(
            {
                "symbols": ["XAUUSD"],
                "market_source": "mt5",
                "mode": "paper",
                "memory_path": "data/mt5-paper-experience.jsonl",
                "paper_state_path": "data/mt5-paper-state.json",
            }
        ),
        encoding="utf-8",
    )
    data = tmp_path / "data"
    data.mkdir()
    (data / "mt5-paper-experience.jsonl").write_text(
        json.dumps(
            {
                "decision": {
                    "timestamp": "2026-06-05T00:00:00+00:00",
                    "symbol": "XAUUSD",
                    "action": "hold",
                    "confidence": 0.4,
                    "reason_codes": ["spread_too_wide"],
                    "metadata": {
                        "analyst": {
                            "model_version": "llm_analyst:gpt-5.4-mini",
                            "bias": "neutral",
                            "market_regime": "mixed",
                            "avoid_trade": True,
                            "risk_notes": ["llm_api_key_missing"],
                            "metadata": {
                                "llm_trace": {
                                    "request": {
                                        "model": "gpt-5.4-mini",
                                        "input": {"market": {"symbol": "XAUUSD"}},
                                    },
                                    "response": {
                                        "parsed": {"avoid_trade": True},
                                    },
                                }
                            },
                        }
                    },
                },
                "risk": {"status": "rejected", "reasons": ["planner_chose_hold"]},
                "execution": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (data / "mt5-paper-state.json").write_text(
        json.dumps({"open_positions": [], "closed_positions": []}),
        encoding="utf-8",
    )

    app = WebApp(tmp_path)
    html = app._agent_page()
    payload = app._api("/api/agent-console")

    assert "Agent Control" in html
    assert "Decision Console" in html
    assert "Start Session" in html
    assert 'name="next" value="/agent"' in html
    assert payload["config"] == "mt5-paper.json"
    assert payload["brain"] == "planner-only"
    assert payload["latest_decision"]["action"] == "hold"
    assert payload["latest_decision"]["risk_status"] == "rejected"
    assert payload["latest_decision"]["analyst_model"] == "llm_analyst:gpt-5.4-mini"
    assert payload["latest_decision"]["analyst_risk_notes"] == ["llm_api_key_missing"]
    assert payload["latest_decision"]["llm_trace"]["request"]["model"] == "gpt-5.4-mini"
    assert "Analyst Risk Notes" in html
    assert "LLM Trace" in html
    assert "Request sent to LLM" in html


def test_web_control_page_unifies_market_agent_and_sse(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "mt5-paper.json").write_text(
        json.dumps(
            {
                "symbols": ["XAUUSD"],
                "market_source": "mt5",
                "mode": "paper",
                "memory_path": "data/mt5-paper-experience.jsonl",
                "paper_state_path": "data/mt5-paper-state.json",
            }
        ),
        encoding="utf-8",
    )
    data = tmp_path / "data"
    data.mkdir()
    (data / "mt5-paper-experience.jsonl").write_text(
        json.dumps(
            {
                "decision": {
                    "timestamp": "2026-06-05T00:00:00+00:00",
                    "symbol": "XAUUSD",
                    "action": "hold",
                    "confidence": 0.4,
                    "reason_codes": ["spread_too_wide"],
                },
                "risk": {"status": "rejected", "reasons": ["planner_chose_hold"]},
                "execution": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (data / "mt5-paper-state.json").write_text(
        json.dumps({"open_positions": [], "closed_positions": []}),
        encoding="utf-8",
    )

    app = WebApp(tmp_path)
    app._mt5_open_positions = lambda _symbols: [
        {
            "ticket": 123,
            "symbol": "XAUUSD",
            "side": "buy",
            "volume": 0.01,
            "entry_price": 4400.0,
            "stop_loss": 4390.0,
            "take_profit": 4420.0,
            "profit": 1.25,
            "opened_at": "05 Jun 2026 20:00:00 WIB",
        }
    ]
    app._record_live_ticks(
        [{"symbol": "XAUUSD", "bid": 100.1, "ask": 100.2, "mid": 100.15, "spread_points": 10, "tick_time": 1780650000}]
    )

    html = app._control_page()
    payload = app._api("/api/control")

    assert "Live Market" in html
    assert "Decision History" in html
    assert "control-market-candles" in html
    assert "new EventSource(\"/events\")" in html
    assert 'name="next" value="/control"' in html
    assert payload["stream"]["transport"] == "sse"
    assert payload["stream"]["browser_polling"] is False
    assert payload["live"]["latest_tick"]["symbol"] == "XAUUSD"
    assert payload["agent"]["latest_decision"]["action"] == "hold"
    assert "MT5 Positions" in html
    assert payload["agent"]["mt5_open_position_count"] == 1
    assert payload["agent"]["mt5_open_positions"][0]["ticket"] == 123


def test_web_pnl_summary_groups_symbol_performance_and_reasons(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "paper-demo.json").write_text(
        json.dumps(
            {
                "symbols": ["XAUUSD", "EURUSD"],
                "memory_path": "data/experience.jsonl",
                "paper_state_path": "data/paper-state.json",
                "paper_start_equity": 10000,
                "default_risk_percent": 0.25,
            }
        ),
        encoding="utf-8",
    )
    data = tmp_path / "data"
    data.mkdir()
    (data / "paper-state.json").write_text(
        json.dumps(
            {
                "open_positions": [],
                "closed_positions": [
                    {
                        "symbol": "XAUUSD",
                        "closed_at": "2026-06-05T00:00:00+00:00",
                        "r_multiple": 1.7,
                        "reason_codes": ["spread_acceptable"],
                    },
                    {
                        "symbol": "EURUSD",
                        "closed_at": "2026-06-05T01:00:00+00:00",
                        "r_multiple": -1.0,
                        "reason_codes": ["spread_too_wide"],
                    },
                    {
                        "symbol": "XAUUSD",
                        "closed_at": "2026-06-05T02:00:00+00:00",
                        "r_multiple": 1.7,
                        "reason_codes": ["spread_acceptable"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "decision": {"symbol": "XAUUSD", "reason_codes": ["spread_acceptable"]},
            "risk": {"status": "approved", "reasons": ["risk_checks_passed"]},
        },
        {
            "decision": {"symbol": "EURUSD", "reason_codes": ["spread_too_wide"]},
            "risk": {"status": "rejected", "reasons": ["spread_above_limit"]},
        },
    ]
    (data / "experience.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    summary = WebApp(tmp_path)._pnl_summary()

    assert summary["closed_position_count"] == 3
    assert summary["paper_total_r"] == 2.4
    assert summary["estimated_pnl"] == 60.0
    assert summary["max_drawdown_r"] == -1.0
    assert summary["by_symbol"][0]["symbol"] == "XAUUSD"
    assert summary["by_symbol"][0]["total_r"] == 3.4
    assert summary["decision_reasons"]["spread_acceptable"] == 1
    assert summary["rejection_reasons"]["spread_above_limit"] == 1
    assert summary["reason_profit"]["spread_acceptable"]["total_r"] == 3.4
    assert summary["reason_quality"][0]["reason"] in {"spread_acceptable", "spread_too_wide"}


def test_web_pnl_page_shows_dashboard_sections(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "paper-demo.json").write_text(
        json.dumps({"symbols": ["XAUUSD"], "paper_state_path": "data/paper-state.json"}),
        encoding="utf-8",
    )
    data = tmp_path / "data"
    data.mkdir()
    (data / "paper-state.json").write_text(
        json.dumps({"open_positions": [], "closed_positions": [{"symbol": "XAUUSD", "closed_at": "t", "r_multiple": 1.7}]}),
        encoding="utf-8",
    )

    html = WebApp(tmp_path)._pnl_page()

    assert "PnL Summary" in html
    assert "Equity Curve" in html
    assert "Visual Charts" in html
    assert "Reason Quality" in html
    assert "Symbol Performance" in html
    assert "symbol-r-chart" in html
    assert "<svg" in html
    assert "/api/pnl" in html
    assert "setInterval(refreshLivePanels" in html


def test_web_visual_digest_exposes_agent_readable_chart_data(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "paper-demo.json").write_text(
        json.dumps(
            {
                "symbols": ["XAUUSD"],
                "memory_path": "data/experience.jsonl",
                "paper_state_path": "data/paper-state.json",
                "paper_start_equity": 10000,
                "default_risk_percent": 0.25,
            }
        ),
        encoding="utf-8",
    )
    data = tmp_path / "data"
    data.mkdir()
    (data / "paper-state.json").write_text(
        json.dumps(
            {
                "open_positions": [],
                "closed_positions": [
                    {"symbol": "XAUUSD", "closed_at": "t", "r_multiple": 1.7, "reason_codes": ["spread_acceptable"]}
                ],
            }
        ),
        encoding="utf-8",
    )
    (data / "experience.jsonl").write_text(
        json.dumps(
            {
                "decision": {"symbol": "XAUUSD", "confidence": 0.8, "reason_codes": ["spread_acceptable"]},
                "risk": {"status": "approved"},
                "execution": {"accepted": True},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    digest = WebApp(tmp_path)._api("/api/visual-digest")

    assert digest["purpose"] == "structured_visual_digest_for_agent_review"
    assert digest["summary"]["paper_total_r"] == 1.7
    assert digest["charts"]["reason_quality"][0]["reason"] == "spread_acceptable"
    assert digest["charts"]["reason_profit"]["spread_acceptable"]["total_r"] == 1.7
    assert digest["agent_read"]["best_symbols"][0]["symbol"] == "XAUUSD"


def test_web_market_chart_reads_simulated_candles(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "paper-demo.json").write_text(
        json.dumps({"symbols": ["XAUUSD"], "sim_state_path": "data/sim-market-state.json"}),
        encoding="utf-8",
    )
    data = tmp_path / "data"
    data.mkdir()
    (data / "sim-market-state.json").write_text(
        json.dumps(
            {
                "XAUUSD": {
                    "step": 2,
                    "mid": 101,
                    "history": [
                        {"step": 1, "open": 100, "high": 102, "low": 99, "close": 101},
                        {"step": 2, "open": 101, "high": 103, "low": 100, "close": 102},
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    (data / "paper-state.json").write_text(
        json.dumps(
            {
                "open_positions": [
                    {
                        "symbol": "XAUUSD",
                        "side": "buy",
                        "entry_price": 101,
                        "stop_loss": 99,
                        "take_profit": 103,
                    }
                ],
                "closed_positions": [
                    {
                        "symbol": "XAUUSD",
                        "exit_reason": "take_profit",
                        "exit_price": 102,
                        "r_multiple": 1.7,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (data / "experience.jsonl").write_text(
        json.dumps(
            {
                "decision": {
                    "symbol": "XAUUSD",
                    "action": "open_position",
                    "side": "buy",
                    "confidence": 0.8,
                    "reason_codes": ["spread_acceptable"],
                },
                "risk": {"status": "approved"},
                "execution": {"accepted": True},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    app = WebApp(tmp_path)
    payload = app._api("/api/market-chart")
    html = app._market_page()

    assert payload["series"]["XAUUSD"][0]["open"] == 100
    assert payload["overlays"]["XAUUSD"]["indicators"][0]["ma_fast"] == 101
    assert payload["overlays"]["XAUUSD"]["signals"][0]["side"] == "buy"
    assert payload["overlays"]["XAUUSD"]["open_positions"][0]["entry_price"] == 101
    assert "Candlestick" in html
    assert "market-symbol-select" in html
    assert "entry" in html
    assert "TP" in html
    assert "market-ma-fast" in html
    assert "market-atr" in html
    assert "<svg" in html


def test_web_market_page_shows_live_mt5_panel_when_disabled(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "paper-demo.json").write_text(
        json.dumps({"symbols": ["XAUUSD"], "market_source": "sim"}),
        encoding="utf-8",
    )

    app = WebApp(tmp_path)
    payload = app._api("/api/live-market")
    html = app._market_page()

    assert payload["status"] == "disabled"
    assert "market-live-strip" in html
    assert "Tick Tape" in html
    assert "tick-tape-table" not in html
    assert "/api/live-market" in html


def test_web_live_market_summary_reads_mt5_feed(tmp_path, monkeypatch):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "mt5-paper.json").write_text(
        json.dumps({"symbols": ["XAUUSD"], "market_source": "mt5", "mode": "paper"}),
        encoding="utf-8",
    )

    class FakeConnection:
        pass

    class FakeMarketFeed:
        def __init__(self, connection):
            self.connection = connection

        def snapshot(self, symbol):
            return MarketSnapshot(
                symbol=symbol,
                bid=100.1,
                ask=100.2,
                spread_points=10,
                atr_points=120,
                trend_score=0.7,
                volatility_score=0.5,
                session="test",
                features={"last_tick_time": 123, "rates_loaded": 64},
            )

    class FakeAccountFeed:
        def __init__(self, connection):
            self.connection = connection

        def state(self):
            return AccountState(equity=50, balance=50, free_margin=45, open_positions=1)

    monkeypatch.setattr("quantz.web.Mt5Connection", FakeConnection)
    monkeypatch.setattr("quantz.web.Mt5MarketFeed", FakeMarketFeed)
    monkeypatch.setattr("quantz.web.Mt5AccountFeed", FakeAccountFeed)

    payload = WebApp(tmp_path)._api("/api/live-market")

    assert payload["status"] == "connected"
    assert payload["account"]["equity"] == 50
    assert payload["ticks"][0]["symbol"] == "XAUUSD"
    assert payload["ticks"][0]["bid"] == 100.1
    assert payload["recent_ticks"] == []
    assert not (tmp_path / "data" / "mt5-ticks.jsonl").exists()


def test_web_live_market_tick_tape_deduplicates_ticks(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "paper-demo.json").write_text(json.dumps({"symbols": ["XAUUSD"]}), encoding="utf-8")
    app = WebApp(tmp_path)

    first = app._record_live_ticks(
        [{"symbol": "XAUUSD", "bid": 100.1, "ask": 100.2, "mid": 100.15, "spread_points": 10, "tick_time": 123}]
    )
    second = app._record_live_ticks(
        [{"symbol": "XAUUSD", "bid": 100.1, "ask": 100.2, "mid": 100.15, "spread_points": 10, "tick_time": 123}]
    )
    third = app._record_live_ticks(
        [{"symbol": "XAUUSD", "bid": 100.3, "ask": 100.4, "mid": 100.35, "spread_points": 10, "tick_time": 124}]
    )

    assert len(first) == 1
    assert len(second) == 1
    assert len(third) == 2
    payload = app._api("/api/tick-tape", "limit=1")

    assert payload["ticks"][0]["tick_time"] == 124
    assert payload["collector"]["running"] is False
    assert payload["summary"]["count"] == 1
    assert payload["summary"]["latest_bid"] == 100.3
    assert "ticks_per_minute" in payload["summary"]
    assert "Tick Tape" in app._tick_tape_chart(third)


def test_web_bridge_tick_ingest_records_external_on_tick_event(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "paper-demo.json").write_text(json.dumps({"symbols": ["XAUUSD"]}), encoding="utf-8")
    app = WebApp(tmp_path)

    result = app._ingest_bridge_tick(
        json.dumps({"symbol": "XAUUSD", "bid": 100.1, "ask": 100.2, "point": 0.01, "digits": 2, "tick_time": 1780650000})
    )
    tape = app._api("/api/tick-tape", "limit=1")

    assert result["status"] == "accepted"
    assert result["received"] == 1
    assert tape["ticks"][0]["symbol"] == "XAUUSD"
    assert tape["ticks"][0]["spread_points"] == 10.0
    assert tape["ticks"][0]["tick_time_display"] == "05 Jun 2026 16:00:00 WIB"
    assert tape["ticks"][0]["source"] == "ea_socket"
    assert tape["summary"]["latest_source_label"] == "EA socket"


def test_web_ea_socket_tick_suppresses_python_polling_collector(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "mt5-paper.json").write_text(
        json.dumps({"symbols": ["XAUUSD"], "market_source": "mt5", "mode": "paper"}),
        encoding="utf-8",
    )
    app = WebApp(tmp_path)
    calls = {"count": 0}

    def fake_read_live_market():
        calls["count"] += 1
        return {
            "status": "connected",
            "ticks": [
                {
                    "symbol": "XAUUSD",
                    "bid": 101,
                    "ask": 101.2,
                    "mid": 101.1,
                    "spread_points": 10,
                    "tick_time": calls["count"],
                }
            ],
        }

    app._read_live_market = fake_read_live_market
    app._ingest_bridge_tick(
        json.dumps({"symbol": "XAUUSD", "bid": 100.1, "ask": 100.2, "point": 0.01, "digits": 2, "tick_time": 1780650000})
    )
    app.start_tick_collector(interval_seconds=0.01)
    time.sleep(0.05)
    app.stop_tick_collector()

    payload = app._api("/api/tick-tape", "limit=1")

    assert calls["count"] == 0
    assert payload["collector"]["polling_suppressed_by_ea"] is True
    assert payload["collector"]["active_source_label"] == "EA socket"
    assert payload["ticks"][0]["source"] == "ea_socket"


def test_web_background_tick_collector_does_not_poll_when_socket_only(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "mt5-paper.json").write_text(
        json.dumps({"symbols": ["XAUUSD"], "market_source": "mt5", "mode": "paper"}),
        encoding="utf-8",
    )
    app = WebApp(tmp_path)
    calls = {"count": 0}

    def fake_read_live_market():
        calls["count"] += 1
        return {
            "status": "connected",
            "ticks": [
                {
                    "symbol": "XAUUSD",
                    "bid": 100 + calls["count"],
                    "ask": 100.2 + calls["count"],
                    "mid": 100.1 + calls["count"],
                    "spread_points": 10,
                    "tick_time": calls["count"],
                }
            ],
        }

    app._read_live_market = fake_read_live_market
    app.start_tick_collector(interval_seconds=0.01)
    time.sleep(0.05)
    app.stop_tick_collector()

    payload = app._api("/api/tick-tape")

    assert calls["count"] == 0
    assert payload["ticks"] == []
    assert payload["collector"]["active_source_label"] == "EA socket"


def test_web_market_chart_uses_mt5_ohlc_when_available(tmp_path, monkeypatch):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "paper-demo.json").write_text(
        json.dumps({"symbols": ["EURUSD"], "market_source": "sim"}),
        encoding="utf-8",
    )
    (configs / "mt5-paper.json").write_text(
        json.dumps(
            {
                "symbols": ["XAUUSD"],
                "market_source": "mt5",
                "mode": "paper",
                "memory_path": "data/mt5-paper-experience.jsonl",
                "paper_state_path": "data/mt5-paper-state.json",
            }
        ),
        encoding="utf-8",
    )

    class Info:
        visible = True
        digits = 3

    class FakeMt5:
        TIMEFRAME_H1 = 1
        TIMEFRAME_M15 = 2

        def symbol_info(self, symbol):
            return Info()

        def symbol_info_tick(self, symbol):
            return type("Tick", (), {"bid": 101.7, "ask": 101.9, "time": 1780657200})()

        def symbol_select(self, symbol, visible):
            return True

        def copy_rates_from_pos(self, symbol, timeframe, start, count):
            return [
                {"time": 1780650000, "open": 100.1111, "high": 101.2222, "low": 99.3333, "close": 100.4444, "tick_volume": 7},
                {"time": 1780653600, "open": 100.4444, "high": 102.5555, "low": 100.1111, "close": 101.7777, "tick_volume": 9},
            ]

    class FakeConnection:
        def __init__(self):
            self.mt5 = FakeMt5()

        def initialize(self):
            return None

    monkeypatch.setattr("quantz.web.Mt5Connection", FakeConnection)

    payload = WebApp(tmp_path)._api("/api/market-chart")

    assert payload["source"] == "mt5_ohlc_history"
    assert payload["timeframe"] == "H1"
    assert payload["symbols"] == ["XAUUSD"]
    assert payload["series"]["XAUUSD"][0]["open"] == 100.111
    assert payload["series"]["XAUUSD"][1]["tick_volume"] == 9
    assert payload["overlays"]["XAUUSD"]["current_tick"]["bid"] == 101.7

    m15_payload = WebApp(tmp_path)._api("/api/market-chart", "timeframe=M15&candles=20")

    assert m15_payload["timeframe"] == "M15"


def test_web_market_chart_overlays_latest_ea_socket_tick(tmp_path, monkeypatch):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "mt5-paper.json").write_text(
        json.dumps(
            {
                "symbols": ["XAUUSD"],
                "market_source": "mt5",
                "mode": "paper",
                "memory_path": "data/mt5-paper-experience.jsonl",
                "paper_state_path": "data/mt5-paper-state.json",
            }
        ),
        encoding="utf-8",
    )

    class Info:
        visible = True
        digits = 3

    class FakeMt5:
        TIMEFRAME_H1 = 1
        TIMEFRAME_M15 = 2

        def symbol_info(self, symbol):
            return Info()

        def symbol_info_tick(self, symbol):
            return type("Tick", (), {"bid": 101.7, "ask": 101.9, "time": 1780657200})()

        def symbol_select(self, symbol, visible):
            return True

        def copy_rates_from_pos(self, symbol, timeframe, start, count):
            return [
                {"time": 1780650000, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "tick_volume": 7},
                {"time": 1780653600, "open": 100.5, "high": 101.0, "low": 100.0, "close": 100.8, "tick_volume": 9},
            ]

    class FakeConnection:
        def __init__(self):
            self.mt5 = FakeMt5()

        def initialize(self):
            return None

    monkeypatch.setattr("quantz.web.Mt5Connection", FakeConnection)

    app = WebApp(tmp_path)
    app._ingest_bridge_tick(
        json.dumps({"symbol": "XAUUSD", "bid": 105.1, "ask": 105.3, "point": 0.01, "digits": 3, "tick_time": 1780657200})
    )

    payload = app._api("/api/market-chart")
    latest_candle = payload["series"]["XAUUSD"][-1]
    current_tick = payload["overlays"]["XAUUSD"]["current_tick"]

    assert payload["source"] == "mt5_ohlc_history + ea_socket_live_tick"
    assert current_tick["source"] == "ea_socket"
    assert current_tick["bid"] == 105.1
    assert latest_candle["close"] == 105.2
    assert latest_candle["high"] == 105.2


def test_web_candlestick_chart_shows_latest_agent_decision(tmp_path):
    html = WebApp(tmp_path)._candlestick_chart(
        "XAUUSD",
        [
            {"step": 1, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5},
            {"step": 2, "open": 100.5, "high": 102.0, "low": 100.0, "close": 101.5},
        ],
        {
            "signals": [
                {
                    "action": "hold",
                    "side": "",
                    "confidence": 0.8045,
                    "risk_status": "rejected",
                    "reason_codes": ["spread_too_wide"],
                }
            ],
            "current_tick": {"bid": 101.4, "ask": 101.6, "mid": 101.5},
        },
    )

    assert "Latest Decision: hold" in html
    assert "spread_too_wide" in html
    assert "BID 101.400" in html
    assert "ASK 101.600" in html


def test_web_stream_agent_runs_from_new_ea_socket_tick(tmp_path, monkeypatch):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "mt5-paper.json").write_text(
        json.dumps(
            {
                "symbols": ["XAUUSD"],
                "market_source": "mt5",
                "mode": "paper",
                "analyst": "rule",
                "memory_path": "data/mt5-paper-experience.jsonl",
                "paper_state_path": "data/mt5-paper-state.json",
                "paper_start_equity": 10000,
                "min_confidence": 0.65,
            }
        ),
        encoding="utf-8",
    )

    class FakeConnection:
        pass

    class FakeMarketFeed:
        def __init__(self, connection):
            self.connection = connection

        def snapshot(self, symbol):
            return MarketSnapshot(
                symbol=symbol,
                bid=100.0,
                ask=100.1,
                spread_points=10,
                atr_points=120,
                trend_score=0.8,
                volatility_score=0.5,
                session="test",
                news_risk="low",
                features={"source": "mt5", "rates_loaded": 64},
            )

    class FakeAccountFeed:
        def __init__(self, connection):
            self.connection = connection

        def state(self):
            return AccountState(equity=10000, balance=10000, free_margin=10000, open_positions=0)

    monkeypatch.setattr("quantz.web.Mt5Connection", FakeConnection)
    monkeypatch.setattr("quantz.web.Mt5MarketFeed", FakeMarketFeed)
    monkeypatch.setattr("quantz.web.Mt5AccountFeed", FakeAccountFeed)

    app = WebApp(tmp_path)
    result = app._start_monitor_from_form(
        {
            "config": ["mt5-paper.json"],
            "max_iterations": ["1"],
            "interval_seconds": ["0.1"],
            "trigger_mode": ["stream"],
        }
    )
    app._ingest_bridge_tick(
        json.dumps({"symbol": "XAUUSD", "bid": 100.1, "ask": 100.2, "point": 0.01, "digits": 2, "tick_time": 1780650000})
    )
    app.monitor_thread.join(timeout=2)
    summary = app._monitor_summary()
    console = app._api("/api/agent-console")

    assert result == {"status": "started"}
    assert summary["status"] == "completed"
    assert summary["trigger_mode"] == "stream"
    assert summary["event_count"] == 1
    assert summary["recent_events"][0]["trigger"] == "ea_socket_tick"
    assert summary["recent_events"][0]["source"] == "ea_socket_stream"
    assert console["latest_decision"]["action"] == "open_position"
    assert console["latest_decision"]["risk_status"] == "approved"
    assert console["open_position_count"] == 1


def test_web_stream_agent_can_use_explicit_live_mt5_config(tmp_path, monkeypatch):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "mt5-demo-live.json").write_text(
        json.dumps(
            {
                "symbols": ["XAUUSD"],
                "market_source": "mt5",
                "mode": "live",
                "execution_source": "mt5",
                "allow_live_execution": True,
                "analyst": "llm",
                "memory_path": "data/live-experience.jsonl",
                "paper_state_path": "data/live-paper-state.json",
                "paper_start_equity": 10000,
                "min_confidence": 0.65,
            }
        ),
        encoding="utf-8",
    )

    class FakeConnection:
        pass

    class FakeMarketFeed:
        def __init__(self, connection):
            self.connection = connection

        def snapshot(self, symbol):
            return MarketSnapshot(
                symbol=symbol,
                bid=100.0,
                ask=100.1,
                spread_points=10,
                atr_points=120,
                trend_score=0.8,
                volatility_score=0.5,
                session="test",
                news_risk="low",
                features={"source": "mt5", "rates_loaded": 64},
            )

    class FakeAccountFeed:
        def __init__(self, connection):
            self.connection = connection

        def state(self):
            return AccountState(equity=10000, balance=10000, free_margin=10000, open_positions=0)

    class FakeLLMAnalyst:
        def __init__(self, *args, **kwargs):
            pass

        def analyze(self, context):
            return AnalystOutput(
                market_regime="trend",
                bias="buy",
                confidence_adjustment=0.05,
                avoid_trade=False,
                reason_codes=["llm_trend_confirmed"],
                model_version="fake_llm",
            )

    class FakeMt5Broker:
        placed = []

        def place_order(self, order):
            self.placed.append(order)
            return ExecutionResult(True, "mt5-1", "mt5_retcode:10009", filled_price=order.entry_price)

    monkeypatch.setattr("quantz.web.Mt5Connection", FakeConnection)
    monkeypatch.setattr("quantz.web.Mt5MarketFeed", FakeMarketFeed)
    monkeypatch.setattr("quantz.web.Mt5AccountFeed", FakeAccountFeed)
    monkeypatch.setattr("quantz.web.LLMAnalyst", FakeLLMAnalyst)
    monkeypatch.setattr("quantz.web.Mt5BrokerAdapter", FakeMt5Broker)

    app = WebApp(tmp_path)
    app._mt5_open_positions = lambda _symbols: []
    result = app._start_monitor_from_form(
        {
            "config": ["mt5-demo-live.json"],
            "max_iterations": ["1"],
            "interval_seconds": ["0.1"],
            "trigger_mode": ["stream"],
        }
    )
    app._ingest_bridge_tick(
        json.dumps({"symbol": "XAUUSD", "bid": 100.1, "ask": 100.2, "point": 0.01, "digits": 2, "tick_time": 1780650000})
    )
    app.monitor_thread.join(timeout=2)
    console = app._api("/api/agent-console")

    assert result == {"status": "started"}
    assert FakeMt5Broker.placed
    assert console["mode"] == "live"
    assert console["brain"] == "llm:gpt-5.4-mini"
    assert console["latest_decision"]["risk_status"] == "approved"


def test_web_stream_agent_rejects_live_config_without_explicit_opt_in(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "mt5-live.json").write_text(
        json.dumps({"symbols": ["XAUUSD"], "market_source": "mt5", "mode": "live", "execution_source": "mt5"}),
        encoding="utf-8",
    )

    result = WebApp(tmp_path)._start_monitor_from_form(
        {
            "config": ["mt5-live.json"],
            "max_iterations": ["1"],
            "interval_seconds": ["0.1"],
            "trigger_mode": ["stream"],
        }
    )

    assert result["error"] == "live mode requires allow_live_execution=true in the selected config"


def monitor_with_events(settings, iterations, quiet=False, stop_event=None, event_sink=None):
    for iteration in range(1, iterations + 1):
        if stop_event is not None and stop_event.is_set():
            break
        if event_sink is not None:
            event_sink(
                {
                    "timestamp": f"t-{iteration}",
                    "iteration": iteration,
                    "symbol": settings.symbols[0],
                    "action": "open_position",
                    "risk_status": "approved",
                    "execution": "paper_order_filled",
                    "closed_positions": 0,
                }
            )


def slow_monitor(settings, iterations, quiet=False, stop_event=None, event_sink=None):
    for iteration in range(1, iterations + 1):
        if stop_event is not None and stop_event.is_set():
            break
        if event_sink is not None:
            event_sink({"timestamp": f"t-{iteration}", "iteration": iteration, "symbol": settings.symbols[0]})
        time.sleep(0.01)


def test_web_monitor_start_records_events_and_completes(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "paper-demo.json").write_text(
        json.dumps({"symbols": ["XAUUSD"], "market_source": "sim", "mode": "paper"}),
        encoding="utf-8",
    )
    app = WebApp(tmp_path, monitor_fn=monitor_with_events)

    result = app._start_monitor_from_form(
        {"config": ["paper-demo.json"], "max_iterations": ["2"], "interval_seconds": ["0.1"]}
    )
    app.monitor_thread.join(timeout=1)
    summary = app._monitor_summary()

    assert result == {"status": "started"}
    assert summary["running"] is False
    assert summary["status"] == "completed"
    assert summary["event_count"] == 2
    assert summary["recent_events"][0]["iteration"] == 2
    assert (tmp_path / "data" / "monitor-session.json").exists()


def test_web_monitor_session_persists_after_restart(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "paper-demo.json").write_text(
        json.dumps({"symbols": ["XAUUSD"], "market_source": "sim", "mode": "paper"}),
        encoding="utf-8",
    )
    app = WebApp(tmp_path, monitor_fn=monitor_with_events)

    app._start_monitor_from_form({"config": ["paper-demo.json"], "max_iterations": ["2"], "interval_seconds": ["0.1"]})
    app.monitor_thread.join(timeout=1)
    restored = WebApp(tmp_path, monitor_fn=monitor_with_events)._monitor_summary()

    assert restored["status"] == "completed"
    assert restored["event_count"] == 2
    assert restored["recent_events"][0]["iteration"] == 2


def test_web_monitor_running_snapshot_restores_as_interrupted(tmp_path):
    state_path = tmp_path / "data" / "monitor-session.json"
    state_path.parent.mkdir()
    state_path.write_text(
        json.dumps(
            {
                "state": {
                    "running": True,
                    "status": "running",
                    "config": "paper-demo.json",
                    "max_iterations": 100,
                    "started_at": "2026-06-05T00:00:00+00:00",
                    "stopped_at": None,
                    "error": None,
                },
                "events": [{"iteration": 1, "symbol": "XAUUSD"}],
            }
        ),
        encoding="utf-8",
    )

    summary = WebApp(tmp_path, monitor_fn=monitor_with_events)._monitor_summary()

    assert summary["running"] is False
    assert summary["status"] == "interrupted"
    assert summary["event_count"] == 1


def test_web_monitor_stop_marks_stopping(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "paper-demo.json").write_text(
        json.dumps({"symbols": ["XAUUSD"], "market_source": "sim", "mode": "paper"}),
        encoding="utf-8",
    )
    app = WebApp(tmp_path, monitor_fn=slow_monitor)

    app._start_monitor_from_form({"config": ["paper-demo.json"], "max_iterations": ["100"], "interval_seconds": ["0.1"]})
    stop_result = app._stop_monitor()
    app.monitor_thread.join(timeout=1)
    summary = app._monitor_summary()

    assert stop_result == {"status": "stopping"}
    assert summary["running"] is False
    assert summary["status"] == "stopped"


def fake_monitor(settings, iterations, quiet=False):
    portfolio = PaperPortfolio(settings.paper_state_path)
    portfolio.open_position(
        OrderRequest(
            symbol="XAUUSD",
            side=OrderSide.BUY,
            volume=0.01,
            entry_price=100,
            stop_loss=99,
            take_profit=101,
            comment="test",
        ),
        ExecutionResult(True, "paper-1", "filled", filled_price=100),
    )
    portfolio.reconcile(
        MarketSnapshot(
            symbol="XAUUSD",
            bid=101.1,
            ask=101.2,
            spread_points=10,
            atr_points=100,
            trend_score=0.5,
            volatility_score=0.5,
            session="test",
        )
    )
    with open(settings.memory_path, "a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "decision": {"symbol": "XAUUSD", "confidence": 0.8},
                    "risk": {"status": "approved", "reasons": ["risk_checks_passed"]},
                    "execution": {"accepted": True},
                }
            )
            + "\n"
        )


def test_web_index_includes_run_experiment_form(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "paper-demo.json").write_text(json.dumps({"symbols": ["XAUUSD"]}), encoding="utf-8")

    html = WebApp(tmp_path, monitor_fn=fake_monitor)._index()

    assert "Run Experiment" in html
    assert "/experiments/run" in html


def test_web_run_experiment_creates_dashboard(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "paper-demo.json").write_text(json.dumps({"symbols": ["XAUUSD"], "market_source": "sim"}), encoding="utf-8")

    result = WebApp(tmp_path, monitor_fn=fake_monitor)._run_experiment_from_form(
        {"config": ["paper-demo.json"], "run_name": ["run-001"], "iterations": ["1"]}
    )

    assert "error" not in result
    assert (tmp_path / "data" / "experiments" / "run-001" / "dashboard.html").exists()
    assert (tmp_path / "data" / "experiments" / "run-001" / "comparison.json").exists()
