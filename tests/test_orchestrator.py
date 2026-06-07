from quantz.agent_goal import AgentGoal
from quantz.agent_tools import AgentToolRegistry, ToolResult
from quantz.orchestrator import TrainingOrchestrator


def test_training_orchestrator_runs_goal_tool_loop(tmp_path):
    calls = []
    registry = AgentToolRegistry()

    def run_backtest(_args):
        calls.append("run_backtest")
        return ToolResult(
            True,
            {
                "promotion_gate": {"allowed": False, "reasons": ["average_r_below_threshold"]},
                "paths": {"report": str(tmp_path / "report.json")},
            },
        )

    def propose(_args):
        calls.append("propose_playbook_adjustment")
        return ToolResult(True, {"output_path": str(tmp_path / "candidate.json")})

    registry.register("run_backtest", run_backtest)
    registry.register("propose_playbook_adjustment", propose)
    registry.register("inspect_report", lambda _args: ToolResult(True, {"sample_size": 1}))
    registry.register("evaluate_promotion_gate", lambda _args: ToolResult(True, {"allowed": False}))
    goal = AgentGoal(
        objective="Improve playbook",
        symbol="XAUUSD",
        playbook="xau",
        max_steps=2,
    )

    payload = TrainingOrchestrator(
        goal,
        registry,
        state={
            "config": "configs/paper-demo.json",
            "csv": "data/history/xauusd-sample.csv",
            "output_dir": str(tmp_path),
            "playbook_path": "configs/playbooks/xau_trend_continuation_v1.json",
        },
    ).run()

    assert calls == ["run_backtest", "propose_playbook_adjustment"]
    assert payload["steps"][0]["thought"]["tool"] == "run_backtest"
    assert payload["steps"][1]["thought"]["tool"] == "propose_playbook_adjustment"
