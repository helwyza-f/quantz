from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

from quantz.agent_goal import AgentGoal, AgentObservation, AgentStep, AgentThought
from quantz.agent_tools import AgentToolRegistry, ToolResult


class AgentBrain(Protocol):
    def think(self, observation: AgentObservation, available_tools: list[str]) -> AgentThought:
        ...


class RuleTrainingBrain:
    """Deterministic brain used when no LLM planner is configured."""

    def think(self, observation: AgentObservation, available_tools: list[str]) -> AgentThought:
        goal = observation.goal
        state = observation.state
        last_result = observation.last_result or {}
        output_dir = state["output_dir"]
        report_path = str(Path(output_dir) / "report.json")
        gate_path = str(Path(output_dir) / "promotion-gate.json")

        if not last_result:
            return AgentThought(
                tool="run_backtest",
                arguments={
                    "config": state["config"],
                    "symbol": goal["symbol"],
                    "csv": state["csv"],
                    "output_dir": output_dir,
                    "promotion_stage": goal["stage"],
                },
                rationale="Start by observing the current playbook performance on historical replay.",
            )

        payload = last_result.get("payload", {}) if last_result.get("ok") else {}
        gate = payload.get("promotion_gate") or payload
        if isinstance(gate, dict) and gate.get("allowed") is True:
            return AgentThought(tool="stop", rationale="Goal gate is satisfied.", stop=True)

        if last_result.get("tool") == "run_backtest":
            return AgentThought(
                tool="propose_playbook_adjustment",
                arguments={
                    "playbook_path": state["playbook_path"],
                    "report_path": report_path,
                    "output_path": str(Path(output_dir) / "candidate-playbook.json"),
                },
                rationale="Replay did not pass the gate; propose a stricter candidate playbook from observed failures.",
            )

        if last_result.get("tool") == "propose_playbook_adjustment":
            return AgentThought(
                tool="inspect_report",
                arguments={"report_path": report_path},
                rationale="Inspect the baseline report so the training log captures why the candidate was proposed.",
            )

        return AgentThought(
            tool="evaluate_promotion_gate",
            arguments={"gate_path": gate_path},
            rationale="Check whether the current run satisfies the goal gate.",
        )


class LLMTrainingBrain:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "tool": {"type": "string"},
            "arguments": {"type": "object"},
            "rationale": {"type": "string"},
            "stop": {"type": "boolean"},
        },
        "required": ["tool", "arguments", "rationale", "stop"],
    }

    def __init__(self, model: str = "gpt-5.4-mini", api_key_env: str = "OPENAI_API_KEY", timeout_seconds: float = 20) -> None:
        self.model = model
        self.api_key_env = api_key_env
        self.timeout_seconds = timeout_seconds
        self.fallback = RuleTrainingBrain()

    def think(self, observation: AgentObservation, available_tools: list[str]) -> AgentThought:
        api_key = os.getenv(self.api_key_env, "")
        if not api_key:
            return self.fallback.think(observation, available_tools)
        payload = {
            "model": self.model,
            "instructions": (
                "You are the Quantz training orchestrator brain. Choose exactly one next tool "
                "to move the goal forward. Use only available tools. Stop only when the goal "
                "gate is clearly satisfied or no useful action remains. Return strict JSON."
            ),
            "input": json.dumps(
                {
                    "available_tools": available_tools,
                    "observation": asdict(observation),
                },
                default=str,
                sort_keys=True,
            ),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "quantz_training_thought",
                    "strict": True,
                    "schema": self.schema,
                }
            },
            "max_output_tokens": 900,
        }
        try:
            request = urllib.request.Request(
                "https://api.openai.com/v1/responses",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = json.loads(response.read().decode("utf-8"))
            data = self._extract_json(raw)
            tool = str(data.get("tool", ""))
            if tool not in available_tools and not bool(data.get("stop")):
                return self.fallback.think(observation, available_tools)
            return AgentThought(
                tool=tool or "stop",
                arguments=dict(data.get("arguments", {})),
                rationale=str(data.get("rationale", "")),
                stop=bool(data.get("stop", False)),
            )
        except Exception:
            return self.fallback.think(observation, available_tools)

    def _extract_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        if "output_text" in payload:
            return json.loads(str(payload["output_text"]))
        for item in payload.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return json.loads(str(content.get("text", "{}")))
        raise ValueError("llm_response_missing_output_text")


class TrainingOrchestrator:
    def __init__(
        self,
        goal: AgentGoal,
        tools: AgentToolRegistry,
        brain: AgentBrain | None = None,
        state: dict[str, Any] | None = None,
    ) -> None:
        self.goal = goal
        self.tools = tools
        self.brain = brain or RuleTrainingBrain()
        self.state = state or {}
        self.steps: list[AgentStep] = []

    def run(self) -> dict[str, Any]:
        last_result: dict[str, Any] | None = None
        for step_index in range(1, self.goal.max_steps + 1):
            observation = AgentObservation(goal=self.goal.to_dict(), state=self.state, last_result=last_result)
            thought = self.brain.think(observation, self._allowed_tools())
            if thought.stop:
                result = ToolResult(True, {"stopped": True, "reason": thought.rationale})
            else:
                result = self.tools.call(thought.tool, thought.arguments)
            step = AgentStep(
                step=step_index,
                observation=asdict(observation),
                thought=thought.to_dict(),
                result={**result.to_dict(), "tool": thought.tool},
            )
            self.steps.append(step)
            last_result = step.result
            if thought.stop:
                break
        return {
            "goal": self.goal.to_dict(),
            "state": self.state,
            "steps": [step.to_dict() for step in self.steps],
            "final_result": last_result or {},
        }

    def write_run(self, path: str | Path, payload: dict[str, Any]) -> Path:
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        return output_path

    def _allowed_tools(self) -> list[str]:
        return self.goal.allowed_tools or self.tools.names()
