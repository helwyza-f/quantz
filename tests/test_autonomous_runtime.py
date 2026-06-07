import json
import time

from quantz.autonomous_runtime import AutonomousRuntime
from quantz.config import AgentSettings, write_settings


def write_config(root, name="mt5-paper.json", **overrides):
    configs = root / "configs"
    configs.mkdir(exist_ok=True)
    payload = {
        "symbols": ["XAUUSD"],
        "mode": "paper",
        "market_source": "demo",
        "analyst": "rule",
        "planner": "variable",
        "memory_path": "data/experience.jsonl",
        "paper_state_path": "data/paper-state.json",
        "vector_memory_enabled": False,
        **overrides,
    }
    settings = AgentSettings(**payload)
    write_settings(settings, configs / name)
    return settings


def test_autonomous_runtime_lists_agent_configs(tmp_path):
    write_config(tmp_path, "mt5-paper.json")
    (tmp_path / "configs" / "notes.txt").write_text("ignore", encoding="utf-8")

    runtime = AutonomousRuntime(tmp_path)

    assert runtime.agent_config_names() == ["mt5-paper.json"]


def test_autonomous_runtime_ingests_ea_tick_and_builds_control_summary(tmp_path):
    write_config(tmp_path)
    runtime = AutonomousRuntime(tmp_path)

    result = runtime.ingest_bridge_tick_query(
        "symbol=XAUUSD&bid=2300.10&ask=2300.20&spread_points=10&tick_time=1780800001"
    )
    summary = runtime.control_summary(include_chart=True)

    assert result["status"] == "accepted"
    assert summary["stream"]["transport"] == "sse"
    assert summary["live"]["latest_tick"]["symbol"] == "XAUUSD"
    assert summary["live"]["summary"]["latest_source_label"] == "EA socket"
    assert summary["chart"]["symbols"] == ["XAUUSD"]
    assert summary["chart"]["series"]["XAUUSD"][-1]["close"] == 2300.15


def test_autonomous_runtime_reads_recent_decisions_and_audit(tmp_path):
    settings = write_config(tmp_path, decision_audit_path="data/audit.jsonl")
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "experience.jsonl").write_text(
        json.dumps(
            {
                "decision": {
                    "decision_id": "d1",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "symbol": "XAUUSD",
                    "action": "hold",
                    "confidence": 0.1,
                    "reason_codes": ["ai_api_key_missing"],
                    "metadata": {
                        "ai_decision": {
                            "decision_brief": "Hold by policy.",
                            "memory_used": [],
                            "risk_notes": ["ai_api_key_missing"],
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
    (data / "audit.jsonl").write_text(
        json.dumps({"kind": "ai_decision", "symbol": "XAUUSD", "policy": {"reasons": ["x"]}}) + "\n",
        encoding="utf-8",
    )

    agent = AutonomousRuntime(tmp_path).agent_summary(settings)

    assert agent["brain"] == "variable"
    assert agent["experience_count"] == 1
    assert agent["latest_decision"]["action"] == "hold"
    assert agent["latest_decision"]["llm_brief"]["decision_brief"] == "Hold by policy."
    assert agent["audit_recent"][0]["kind"] == "ai_decision"


def test_autonomous_runtime_monitor_runs_demo_agent(tmp_path):
    write_config(tmp_path, planner="variable", market_source="demo")
    runtime = AutonomousRuntime(tmp_path)

    result = runtime.start_agent("mt5-paper.json", max_iterations=1, interval_seconds=0.1)
    deadline = time.time() + 3
    while time.time() < deadline and runtime.monitor_state["running"]:
        time.sleep(0.02)

    assert result["status"] == "started"
    assert runtime.monitor_state["status"] == "stopped"
    assert runtime.monitor_events
    assert runtime.monitor_events[0]["symbol"] == "XAUUSD"
    assert (tmp_path / "data" / "experience.jsonl").exists()


def test_autonomous_runtime_rejects_duplicate_monitor_start(tmp_path):
    write_config(tmp_path)
    runtime = AutonomousRuntime(tmp_path)

    first = runtime.start_agent("mt5-paper.json", max_iterations=10, interval_seconds=0.2)
    second = runtime.start_agent("mt5-paper.json", max_iterations=10, interval_seconds=0.2)
    runtime.stop_agent()

    assert first["status"] == "started"
    assert second["status"] == "already_running"


def test_autonomous_runtime_replay_is_explicitly_not_legacy(tmp_path):
    runtime = AutonomousRuntime(tmp_path)

    result = runtime.start_replay({"symbol": "XAUUSD"})

    assert result["status"] == "not_configured"
    assert result["error"] == "legacy_replay_ui_removed_use_cli_backtest"


def test_autonomous_runtime_status_reports_ai_memory_and_tick_state(tmp_path):
    write_config(
        tmp_path,
        planner="ai",
        ai_planner_fail_closed=True,
        vector_memory_enabled=True,
        vector_memory_path="data/vector.db",
        decision_audit_path="data/audit.jsonl",
    )
    runtime = AutonomousRuntime(tmp_path)
    runtime.ingest_bridge_tick_query("symbol=XAUUSD&bid=1.1&ask=1.2&spread_points=10")

    status = runtime.runtime_status({"openai_api_key_set": False})

    assert status["backend"]["runtime"] == "autonomous"
    assert status["agent"]["planner"] == "ai"
    assert status["ai"]["api_key_set"] is False
    assert status["memory"]["vector_enabled"] is True
    assert status["market"]["ea_tick_active"] is True
    assert status["overall"] == "blocked_ai_key_missing"
