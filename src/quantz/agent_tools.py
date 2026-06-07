from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from quantz.agent import TradingAgent
from quantz.analyst import LLMAnalyst, RuleBasedAnalyst
from quantz.broker import PaperBrokerAdapter
from quantz.config import load_settings, merge_settings
from quantz.memory import experience_store
from quantz.paper import PaperPortfolio
from quantz.audit import DecisionAuditStore
from quantz.planner import AIDecisionPlanner, DecisionPolicy, VariableDrivenPlanner
from quantz.playbook import PlaybookLoader, PlaybookSelector
from quantz.promotion import PromotionGateEvaluator
from quantz.replay import ReplayRunner
from quantz.report import ReportBuilder
from quantz.risk import RiskConfig, RiskGovernor
from quantz.teacher import TeacherExampleStore
from quantz.vector_memory import LocalSQLiteVectorMemoryStore


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    payload: dict[str, Any]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AgentToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Callable[[dict[str, Any]], ToolResult]] = {}

    def register(self, name: str, fn: Callable[[dict[str, Any]], ToolResult]) -> None:
        self._tools[name] = fn

    def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if name not in self._tools:
            return ToolResult(False, {}, f"unknown_tool:{name}")
        try:
            return self._tools[name](arguments)
        except Exception as exc:
            return ToolResult(False, {}, f"{type(exc).__name__}:{exc}")

    def names(self) -> list[str]:
        return sorted(self._tools)


class TrainingToolFactory:
    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root).resolve()

    def registry(self) -> AgentToolRegistry:
        registry = AgentToolRegistry()
        registry.register("run_backtest", self.run_backtest)
        registry.register("inspect_report", self.inspect_report)
        registry.register("evaluate_promotion_gate", self.evaluate_promotion_gate)
        registry.register("propose_playbook_adjustment", self.propose_playbook_adjustment)
        return registry

    def run_backtest(self, args: dict[str, Any]) -> ToolResult:
        output_dir = self._path(args["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        memory_path = output_dir / "experience.jsonl"
        paper_state_path = output_dir / "paper-state.json"
        report_path = output_dir / "report.json"
        gate_path = output_dir / "promotion-gate.json"
        settings = merge_settings(
            load_settings(str(self._path(args["config"]))),
            mode="paper",
            memory_path=str(memory_path),
            paper_state_path=str(paper_state_path),
            sim_state_path=str(output_dir / "sim-market-state.json"),
            vector_memory_path=str(output_dir / "vector-memory.db"),
        )
        agent = self._agent(settings)
        replay = ReplayRunner(agent).run_csv(str(args["symbol"]), self._path(args["csv"]), constraints=settings.constraints)
        report_builder = ReportBuilder()
        report = report_builder.build(experience_store(memory_path).read_raw(), paper_state_path)
        report_payload = report_builder.to_dict(report)
        gate = PromotionGateEvaluator().evaluate(report, str(args.get("promotion_stage", "replay_to_paper")))
        gate_payload = gate.to_dict()
        self._write_json(report_path, report_payload)
        self._write_json(gate_path, gate_payload)
        return ToolResult(
            True,
            {
                "output_dir": str(output_dir),
                "replay": asdict(replay),
                "report": report_payload,
                "promotion_gate": gate_payload,
                "paths": {
                    "memory": str(memory_path),
                    "paper_state": str(paper_state_path),
                    "report": str(report_path),
                    "promotion_gate": str(gate_path),
                },
            },
        )

    def inspect_report(self, args: dict[str, Any]) -> ToolResult:
        report_path = self._path(args["report_path"])
        if not report_path.exists():
            return ToolResult(False, {}, f"report_not_found:{report_path}")
        return ToolResult(True, self._read_json(report_path))

    def evaluate_promotion_gate(self, args: dict[str, Any]) -> ToolResult:
        gate_path = self._path(args["gate_path"])
        if not gate_path.exists():
            return ToolResult(False, {}, f"gate_not_found:{gate_path}")
        return ToolResult(True, self._read_json(gate_path))

    def propose_playbook_adjustment(self, args: dict[str, Any]) -> ToolResult:
        playbook_path = self._path(args["playbook_path"])
        report_path = self._path(args["report_path"])
        output_path = self._path(args["output_path"])
        playbook = self._read_json(playbook_path)
        report = self._read_json(report_path)
        adjusted = json.loads(json.dumps(playbook))
        conditions = adjusted.setdefault("entry_conditions", {})
        notes: list[str] = []

        if float(report.get("average_r_multiple", 0.0) or 0.0) < 0:
            if "trend_score_min" in conditions:
                conditions["trend_score_min"] = round(min(0.95, float(conditions["trend_score_min"]) + 0.05), 2)
                notes.append("raised_trend_score_min_after_negative_average_r")
            if "spread_max" in conditions:
                conditions["spread_max"] = round(max(10.0, float(conditions["spread_max"]) * 0.85), 2)
                notes.append("tightened_spread_max_after_negative_average_r")
        if int(report.get("closed_position_count", 0) or 0) < 30:
            adjusted["status"] = "training"
            notes.append("kept_training_status_until_sample_grows")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as handle:
            json.dump(adjusted, handle, indent=2, sort_keys=True)
        return ToolResult(True, {"output_path": str(output_path), "notes": notes, "playbook": adjusted})

    def _path(self, value: str | Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.root / path

    def _read_json(self, path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)

    def _agent(self, settings: Any) -> TradingAgent:
        playbook_selector = None
        if settings.playbook_paths:
            playbook_selector = PlaybookSelector(PlaybookLoader().load_many([self._path(path) for path in settings.playbook_paths]))
        examples = []
        if settings.teacher_example_paths:
            examples = [example.to_dict() for example in TeacherExampleStore([self._path(path) for path in settings.teacher_example_paths]).load()]
        analyst = None
        if settings.analyst == "rule":
            analyst = RuleBasedAnalyst()
        elif settings.analyst == "llm":
            analyst = LLMAnalyst(
                model=settings.llm_model,
                api_key_env=settings.llm_api_key_env,
                timeout_seconds=settings.llm_timeout_seconds,
            )
        return TradingAgent(
            planner=self._planner(settings),
            risk_governor=RiskGovernor(
                RiskConfig(
                    max_risk_per_trade_percent=settings.risk_max_risk_per_trade_percent,
                    max_daily_loss_percent=settings.risk_max_daily_loss_percent,
                    max_open_positions=settings.risk_max_open_positions,
                    max_open_risk_percent=settings.risk_max_open_risk_percent,
                    max_spread_points=settings.risk_max_spread_points,
                    min_confidence=settings.min_confidence,
                    min_free_margin_percent=settings.risk_min_free_margin_percent,
                    lot_step=settings.risk_lot_step,
                    min_lot=settings.risk_min_lot,
                    max_lot=settings.risk_max_lot,
                    contract_size=settings.risk_contract_size,
                    allow_min_lot_when_below_minimum=settings.allow_min_lot_when_below_minimum,
                )
            ),
            broker=PaperBrokerAdapter(),
            memory=experience_store(settings.memory_path),
            paper_portfolio=PaperPortfolio(settings.paper_state_path),
            analyst=analyst,
            playbook_selector=playbook_selector,
            mode="paper",
            teacher_examples=examples,
            vector_memory=LocalSQLiteVectorMemoryStore(
                Path(settings.memory_path).with_name("vector-memory.db")
            )
            if getattr(settings, "vector_memory_enabled", True)
            else None,
            max_memory_context_items=getattr(settings, "vector_memory_context_items", 24),
        )

    def _planner(self, settings: Any) -> Any:
        if getattr(settings, "planner", "variable") == "ai":
            return AIDecisionPlanner(
                model=getattr(settings, "ai_planner_model", settings.llm_model),
                api_key_env=getattr(settings, "ai_planner_api_key_env", settings.llm_api_key_env),
                timeout_seconds=getattr(settings, "ai_planner_timeout_seconds", settings.llm_timeout_seconds),
                fallback=VariableDrivenPlanner(),
                policy=DecisionPolicy(
                    min_confidence=settings.min_confidence,
                    max_risk_percent=settings.risk_max_risk_per_trade_percent,
                    max_spread_points=settings.risk_max_spread_points,
                    require_memory_for_live=getattr(settings, "require_memory_for_live_ai", True),
                    min_live_memory_samples=getattr(settings, "min_live_memory_samples", 20),
                    fail_closed_on_ai_error=getattr(settings, "ai_planner_fail_closed", True),
                ),
                audit_store=DecisionAuditStore(
                    Path(settings.memory_path).with_name("decision-audit.jsonl")
                ),
            )
        return VariableDrivenPlanner()
