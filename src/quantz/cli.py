from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from quantz.agent import TradingAgent
from quantz.analyst import LLMAnalyst, NoOpAnalyst, RuleBasedAnalyst
from quantz.bridge import BridgeAccountFeed, BridgeBrokerAdapter, BridgeClient, BridgeMarketFeed
from quantz.broker import Mt5BrokerAdapter, PaperBrokerAdapter
from quantz.candidate import CandidateConfigBuilder
from quantz.compare import RunComparator
from quantz.config import AgentSettings, load_settings, merge_settings
from quantz.config import write_settings
from quantz.dashboard import DashboardRenderer
from quantz.experiment import ExperimentRunner
from quantz.learning import ExperienceLearner
from quantz.market import (
    DemoAccountFeed,
    DemoMarketFeed,
    Mt5AccountFeed,
    Mt5Connection,
    Mt5MarketFeed,
    SimulatedMarketFeed,
)
from quantz.memory import experience_store
from quantz.models import AgentContext
from quantz.paper import PaperPortfolio
from quantz.audit import DecisionAuditStore
from quantz.planner import AIDecisionPlanner, DecisionPolicy, VariableDrivenPlanner
from quantz.playbook import PlaybookLoader, PlaybookSelector
from quantz.report import ReportBuilder
from quantz.replay import ReplayRunner
from quantz.promotion import PromotionGateEvaluator
from quantz.review import ReviewEngine
from quantz.risk import RiskConfig, RiskGovernor
from quantz.services import AgentRuntimeService
from quantz.stack import StackConfig, run_stack
from quantz.teacher import TeacherExampleStore
from quantz.vector_memory import LocalSQLiteVectorMemoryStore
from quantz.agent_goal import AgentGoal
from quantz.agent_tools import TrainingToolFactory
from quantz.orchestrator import LLMTrainingBrain, TrainingOrchestrator


def main() -> None:
    parser = argparse.ArgumentParser(prog="quantz")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_once = subparsers.add_parser("run-once")
    run_once.add_argument("--config")
    run_once.add_argument("--symbol", default="XAUUSD")
    run_once.add_argument("--memory-path")
    run_once.add_argument("--paper-state-path")
    run_once.add_argument("--sim-state-path")
    run_once.add_argument("--mode", choices=["paper", "live"])
    run_once.add_argument("--analyst", choices=["none", "rule", "llm"])
    run_once.add_argument("--market-source", choices=["demo", "sim", "mt5", "bridge"])
    run_once.add_argument("--execution-source", choices=["mt5", "bridge"])
    run_once.add_argument("--bridge-url")

    monitor = subparsers.add_parser("monitor")
    monitor.add_argument("--config")
    monitor.add_argument("--symbol")
    monitor.add_argument("--symbols", nargs="+")
    monitor.add_argument("--memory-path")
    monitor.add_argument("--paper-state-path")
    monitor.add_argument("--sim-state-path")
    monitor.add_argument("--mode", choices=["paper", "live"])
    monitor.add_argument("--analyst", choices=["none", "rule", "llm"])
    monitor.add_argument("--market-source", choices=["demo", "sim", "mt5", "bridge"])
    monitor.add_argument("--execution-source", choices=["mt5", "bridge"])
    monitor.add_argument("--bridge-url")
    monitor.add_argument("--interval-seconds", type=float)
    monitor.add_argument("--max-iterations", type=int, default=0)

    learn = subparsers.add_parser("learn")
    learn.add_argument("--memory-path", default="data/experience.jsonl")

    import_memory = subparsers.add_parser("import-memory")
    import_memory.add_argument("--jsonl-path", required=True)
    import_memory.add_argument("--sqlite-path", default="data/quantz-memory.db")

    inspect_decision = subparsers.add_parser("inspect-decision")
    inspect_decision.add_argument("--memory-path", default="data/quantz-memory.db")
    inspect_decision.add_argument("--decision-id", required=True)

    report = subparsers.add_parser("report")
    report.add_argument("--memory-path", default="data/experience.jsonl")
    report.add_argument("--paper-state-path", default="data/paper-state.json")

    review = subparsers.add_parser("review")
    review.add_argument("--memory-path", default="data/experience.jsonl")
    review.add_argument("--paper-state-path", default="data/paper-state.json")

    propose = subparsers.add_parser("propose-config")
    propose.add_argument("--config", required=True)
    propose.add_argument("--memory-path", default="data/experience.jsonl")
    propose.add_argument("--paper-state-path", default="data/paper-state.json")
    propose.add_argument("--output", required=True)

    compare = subparsers.add_parser("compare-runs")
    compare.add_argument("--base-memory-path", required=True)
    compare.add_argument("--base-paper-state-path", required=True)
    compare.add_argument("--candidate-memory-path", required=True)
    compare.add_argument("--candidate-paper-state-path", required=True)

    experiment = subparsers.add_parser("experiment")
    experiment.add_argument("--config", required=True)
    experiment.add_argument("--output-dir", required=True)
    experiment.add_argument("--iterations", type=int, default=24)

    dashboard = subparsers.add_parser("dashboard")
    dashboard.add_argument("--experiment-dir", required=True)
    dashboard.add_argument("--output", required=True)

    replay = subparsers.add_parser("replay")
    replay.add_argument("--config", required=True)
    replay.add_argument("--symbol", required=True)
    replay.add_argument("--csv", required=True)

    backtest = subparsers.add_parser("backtest")
    backtest.add_argument("--config", required=True)
    backtest.add_argument("--symbol", required=True)
    backtest.add_argument("--csv", required=True)
    backtest.add_argument("--output-dir", required=True)
    backtest.add_argument("--promotion-stage", default="replay_to_paper", choices=["replay_to_paper", "paper_to_demo", "demo_to_tiny_live"])

    promotion_gate = subparsers.add_parser("promotion-gate")
    promotion_gate.add_argument("--stage", required=True, choices=["replay_to_paper", "paper_to_demo", "demo_to_tiny_live"])
    promotion_gate.add_argument("--memory-path", default="data/experience.jsonl")
    promotion_gate.add_argument("--paper-state-path", default="data/paper-state.json")

    train_agent = subparsers.add_parser("train-agent")
    train_agent.add_argument("--config", required=True)
    train_agent.add_argument("--symbol", required=True)
    train_agent.add_argument("--csv", required=True)
    train_agent.add_argument("--playbook-path", required=True)
    train_agent.add_argument("--output-dir", required=True)
    train_agent.add_argument("--stage", default="replay_to_paper", choices=["replay_to_paper", "paper_to_demo", "demo_to_tiny_live"])
    train_agent.add_argument("--max-steps", type=int, default=4)
    train_agent.add_argument("--brain", choices=["rule", "llm"], default="rule")

    web = subparsers.add_parser("web")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8787)
    web.add_argument("--root", default=".")

    stack = subparsers.add_parser("stack")
    stack.add_argument("--host", default="127.0.0.1")
    stack.add_argument("--backend-port", type=int, default=8787)
    stack.add_argument("--frontend-port", type=int, default=3000)
    stack.add_argument("--bridge-port", type=int, default=8765)
    stack.add_argument("--root", default=".")
    stack.add_argument("--no-frontend", action="store_true")
    stack.add_argument("--with-bridge", action="store_true")
    stack.add_argument("--reload-backend", action="store_true")

    args = parser.parse_args()
    if args.command == "run-once":
        settings = _settings(args)
        record = _run_once(
            settings,
            args.symbol,
        )
        print(json.dumps(record, default=_json_default, indent=2, sort_keys=True))
    elif args.command == "learn":
        report = ExperienceLearner().analyze(experience_store(args.memory_path).read_raw())
        print(json.dumps(report, default=_json_default, indent=2, sort_keys=True))
    elif args.command == "import-memory":
        from quantz.memory_sqlite import SQLiteExperienceStore

        imported = SQLiteExperienceStore(args.sqlite_path).import_jsonl(args.jsonl_path)
        print(json.dumps({"imported": imported, "sqlite_path": args.sqlite_path}, indent=2, sort_keys=True))
    elif args.command == "inspect-decision":
        store = experience_store(args.memory_path)
        if not hasattr(store, "decision_lifecycle"):
            raise ValueError("inspect-decision requires a SQLite memory path")
        print(json.dumps(store.decision_lifecycle(args.decision_id), default=_json_default, indent=2, sort_keys=True))
    elif args.command == "monitor":
        settings = _settings(args)
        _monitor(settings, args.max_iterations)
    elif args.command == "report":
        builder = ReportBuilder()
        session_report = builder.build(
            experience_store(args.memory_path).read_raw(),
            args.paper_state_path,
        )
        print(json.dumps(builder.to_dict(session_report), default=_json_default, indent=2, sort_keys=True))
    elif args.command == "review":
        builder = ReportBuilder()
        session_report = builder.build(
            experience_store(args.memory_path).read_raw(),
            args.paper_state_path,
        )
        review_result = ReviewEngine().review(session_report)
        print(json.dumps(ReviewEngine().to_dict(review_result), default=_json_default, indent=2, sort_keys=True))
    elif args.command == "propose-config":
        report_builder = ReportBuilder()
        session_report = report_builder.build(
            experience_store(args.memory_path).read_raw(),
            args.paper_state_path,
        )
        review_result = ReviewEngine().review(session_report)
        candidate = CandidateConfigBuilder().build(load_settings(args.config), review_result)
        write_settings(candidate, args.output)
        print(json.dumps({"output": args.output, "config": candidate}, default=_json_default, indent=2, sort_keys=True))
    elif args.command == "compare-runs":
        builder = ReportBuilder()
        base_report = builder.build(
            experience_store(args.base_memory_path).read_raw(),
            args.base_paper_state_path,
        )
        candidate_report = builder.build(
            experience_store(args.candidate_memory_path).read_raw(),
            args.candidate_paper_state_path,
        )
        comparison = RunComparator().compare(base_report, candidate_report)
        print(json.dumps(RunComparator().to_dict(comparison), default=_json_default, indent=2, sort_keys=True))
    elif args.command == "experiment":
        summary = ExperimentRunner(_monitor).run(
            load_settings(args.config),
            args.output_dir,
            args.iterations,
        )
        print(json.dumps(summary, default=_json_default, indent=2, sort_keys=True))
    elif args.command == "dashboard":
        output = DashboardRenderer().render_experiment(args.experiment_dir, args.output)
        print(json.dumps({"output": str(output)}, indent=2, sort_keys=True))
    elif args.command == "replay":
        settings = merge_settings(load_settings(args.config), mode="paper")
        agent = _agent(settings, BridgeClient(settings.bridge_url))
        summary = ReplayRunner(agent).run_csv(args.symbol, args.csv, constraints=settings.constraints)
        print(json.dumps(asdict(summary), indent=2, sort_keys=True))
    elif args.command == "backtest":
        summary = _backtest(args)
        print(json.dumps(summary, default=_json_default, indent=2, sort_keys=True))
    elif args.command == "promotion-gate":
        builder = ReportBuilder()
        session_report = builder.build(experience_store(args.memory_path).read_raw(), args.paper_state_path)
        gate = PromotionGateEvaluator().evaluate(session_report, args.stage)
        print(json.dumps(gate.to_dict(), default=_json_default, indent=2, sort_keys=True))
    elif args.command == "train-agent":
        goal = AgentGoal(
            objective=f"Improve {args.symbol} playbook until {args.stage} gate passes",
            symbol=args.symbol,
            playbook=Path(args.playbook_path).stem,
            stage=args.stage,
            allowed_tools=["run_backtest", "inspect_report", "evaluate_promotion_gate", "propose_playbook_adjustment"],
            max_steps=args.max_steps,
        )
        output_dir = Path(args.output_dir)
        state = {
            "config": args.config,
            "csv": args.csv,
            "playbook_path": args.playbook_path,
            "output_dir": str(output_dir),
        }
        brain = LLMTrainingBrain() if args.brain == "llm" else None
        orchestrator = TrainingOrchestrator(goal, TrainingToolFactory(".").registry(), brain=brain, state=state)
        payload = orchestrator.run()
        run_path = orchestrator.write_run(output_dir / "agent-run.json", payload)
        print(json.dumps({"run_path": str(run_path), **payload}, default=_json_default, indent=2, sort_keys=True))
    elif args.command == "web":
        import uvicorn

        from quantz.api.app import create_app

        uvicorn.run(create_app(args.root), host=args.host, port=args.port)
    elif args.command == "stack":
        raise SystemExit(
            run_stack(
                StackConfig(
                    root=Path(args.root),
                    host=args.host,
                    backend_port=args.backend_port,
                    frontend_port=args.frontend_port,
                    bridge_port=args.bridge_port,
                    with_frontend=not args.no_frontend,
                    with_bridge=args.with_bridge,
                    reload_backend=args.reload_backend,
                )
            )
        )


def _run_once(
    settings: AgentSettings,
    symbol: str | None = None,
) -> Any:
    selected_symbol = symbol or settings.symbols[0]
    bridge_client = BridgeClient(settings.bridge_url)
    market_feed, account_feed = _feeds_for_settings(settings, bridge_client)
    agent = _agent(settings, bridge_client)
    market = market_feed.snapshot(selected_symbol)
    account = account_feed.state()
    context = AgentContext(
        market=market,
        account=account,
        constraints=settings.constraints,
    )
    return agent.run_once(context)


def _backtest(args: Any) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    memory_path = output_dir / "experience.jsonl"
    paper_state_path = output_dir / "paper-state.json"
    sim_state_path = output_dir / "sim-market-state.json"
    report_path = output_dir / "report.json"
    gate_path = output_dir / "promotion-gate.json"

    settings = merge_settings(
        load_settings(args.config),
        mode="paper",
        memory_path=str(memory_path),
        paper_state_path=str(paper_state_path),
        sim_state_path=str(sim_state_path),
        vector_memory_path=str(output_dir / "vector-memory.db"),
    )
    agent = _agent(settings, BridgeClient(settings.bridge_url))
    replay_summary = ReplayRunner(agent).run_csv(args.symbol, args.csv, constraints=settings.constraints)

    report_builder = ReportBuilder()
    report = report_builder.build(experience_store(memory_path).read_raw(), paper_state_path)
    report_payload = report_builder.to_dict(report)
    gate = PromotionGateEvaluator().evaluate(report, args.promotion_stage)
    gate_payload = gate.to_dict()

    with report_path.open("w", encoding="utf-8") as handle:
        json.dump(report_payload, handle, indent=2, sort_keys=True)
    with gate_path.open("w", encoding="utf-8") as handle:
        json.dump(gate_payload, handle, indent=2, sort_keys=True)

    return {
        "output_dir": str(output_dir),
        "replay": asdict(replay_summary),
        "report": report_payload,
        "promotion_gate": gate_payload,
        "paths": {
            "memory": str(memory_path),
            "paper_state": str(paper_state_path),
            "report": str(report_path),
            "promotion_gate": str(gate_path),
        },
    }


def _monitor(
    settings: AgentSettings,
    max_iterations: int,
    quiet: bool = False,
    stop_event: Any | None = None,
    event_sink: Any | None = None,
) -> None:
    if settings.interval_seconds <= 0:
        raise ValueError("interval_seconds must be greater than 0")

    bridge_client = BridgeClient(settings.bridge_url)
    market_feed, account_feed = _feeds_for_settings(settings, bridge_client)
    agent = _agent(settings, bridge_client)

    iteration = 0
    while max_iterations == 0 or iteration < max_iterations:
        if stop_event is not None and stop_event.is_set():
            break
        iteration += 1
        for symbol in settings.symbols:
            if stop_event is not None and stop_event.is_set():
                break
            if symbol in settings.disabled_symbols:
                _print_event(
                    {
                        "iteration": iteration,
                        "symbol": symbol,
                        "skipped": True,
                        "reason": "symbol_disabled",
                        "timestamp": datetime.now().isoformat(),
                    },
                    quiet,
                    event_sink,
                )
                continue
            market = market_feed.snapshot(symbol)
            cooldown_closed = _reconcile_for_cooldown(settings, market)
            if settings.mode == "paper" and settings.position_cooldown == "until_closed":
                open_count = PaperPortfolio(settings.paper_state_path).open_count(symbol)
                if open_count > 0:
                    _print_event(
                        {
                            "iteration": iteration,
                            "symbol": symbol,
                            "skipped": True,
                            "reason": "position_cooldown_until_closed",
                            "closed_positions": len(cooldown_closed),
                            "timestamp": datetime.now().isoformat(),
                        },
                        quiet,
                        event_sink,
                    )
                    continue
            account = account_feed.state()
            context = AgentContext(market=market, account=account, constraints=settings.constraints)
            record = agent.run_once(context)
            closed = (record.outcome or {}).get("closed_positions", [])
            _print_event(
                {
                    "iteration": iteration,
                    "symbol": symbol,
                    "action": record.decision.action,
                    "confidence": record.decision.confidence,
                    "risk_status": record.risk.status,
                    "execution": record.execution.message if record.execution else None,
                    "closed_positions": len(closed),
                    "timestamp": datetime.now().isoformat(),
                },
                quiet,
                event_sink,
            )
        if max_iterations == 0 or iteration < max_iterations:
            if stop_event is not None:
                if stop_event.wait(settings.interval_seconds):
                    break
            else:
                time.sleep(settings.interval_seconds)


def _agent(settings: AgentSettings, bridge_client: BridgeClient) -> TradingAgent:
    return AgentRuntimeService(settings, bridge_client).build_agent()


def _planner(settings: AgentSettings) -> Any:
    if settings.planner == "variable":
        return VariableDrivenPlanner()
    if settings.planner == "ai":
        return AIDecisionPlanner(
            model=settings.ai_planner_model,
            api_key_env=settings.ai_planner_api_key_env,
            timeout_seconds=settings.ai_planner_timeout_seconds,
            fallback=VariableDrivenPlanner(),
            policy=DecisionPolicy(
                min_confidence=settings.min_confidence,
                max_risk_percent=settings.risk_max_risk_per_trade_percent,
                max_spread_points=settings.risk_max_spread_points,
                require_memory_for_live=settings.require_memory_for_live_ai,
                min_live_memory_samples=settings.min_live_memory_samples,
                fail_closed_on_ai_error=settings.ai_planner_fail_closed,
            ),
            audit_store=DecisionAuditStore(settings.decision_audit_path),
        )
    raise ValueError(f"Unknown planner: {settings.planner}")


def _risk_config(settings: AgentSettings) -> RiskConfig:
    return RiskConfig(
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


def _print_event(payload: dict[str, Any], quiet: bool, event_sink: Any | None = None) -> None:
    if event_sink is not None:
        event_sink(payload)
    if quiet:
        return
    print(json.dumps(payload, default=_json_default, sort_keys=True), flush=True)


def _reconcile_for_cooldown(settings: AgentSettings, market: Any) -> list[Any]:
    if settings.mode != "paper" or settings.position_cooldown != "until_closed":
        return []
    return PaperPortfolio(settings.paper_state_path).reconcile(market)


def _analyst(settings: AgentSettings) -> Any:
    name = settings.analyst
    if name == "none":
        return None
    if name == "rule":
        return RuleBasedAnalyst()
    if name == "llm":
        return LLMAnalyst(
            model=settings.llm_model,
            api_key_env=settings.llm_api_key_env,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    if name == "noop":
        return NoOpAnalyst()
    raise ValueError(f"Unknown analyst: {name}")


def _playbook_selector(settings: AgentSettings) -> PlaybookSelector | None:
    if not settings.playbook_paths:
        return None
    return PlaybookSelector(PlaybookLoader().load_many(settings.playbook_paths))


def _teacher_examples(settings: AgentSettings) -> list[dict[str, Any]]:
    if not settings.teacher_example_paths:
        return []
    examples = TeacherExampleStore(settings.teacher_example_paths).load()
    return [example.to_dict() for example in examples]


def _vector_memory(settings: AgentSettings) -> Any | None:
    if not settings.vector_memory_enabled:
        return None
    default_settings = AgentSettings()
    vector_path = Path(settings.vector_memory_path)
    if (
        settings.vector_memory_path == default_settings.vector_memory_path
        and settings.memory_path != default_settings.memory_path
    ):
        vector_path = Path(settings.memory_path).with_name("vector-memory.db")
    return LocalSQLiteVectorMemoryStore(vector_path)


def _feeds(market_source: str, bridge_client: BridgeClient) -> tuple[Any, Any]:
    if market_source == "demo":
        return DemoMarketFeed(), DemoAccountFeed()
    if market_source == "mt5":
        connection = Mt5Connection()
        return Mt5MarketFeed(connection), Mt5AccountFeed(connection)
    if market_source == "bridge":
        return BridgeMarketFeed(bridge_client), BridgeAccountFeed(bridge_client)
    raise ValueError(f"Unknown market source: {market_source}")


def _feeds_for_settings(settings: AgentSettings, bridge_client: BridgeClient) -> tuple[Any, Any]:
    if settings.market_source == "sim":
        return SimulatedMarketFeed(settings.sim_state_path), DemoAccountFeed()
    return _feeds(settings.market_source, bridge_client)


def _broker(mode: str, execution_source: str, bridge_client: BridgeClient) -> Any:
    if mode == "paper":
        return PaperBrokerAdapter()
    if execution_source == "bridge":
        return BridgeBrokerAdapter(bridge_client)
    if execution_source == "mt5":
        return Mt5BrokerAdapter()
    raise ValueError(f"Unknown execution source: {execution_source}")


def _settings(args: Any) -> AgentSettings:
    base = load_settings(args.config)
    symbol_override = [args.symbol] if getattr(args, "symbol", None) else None
    symbols_override = getattr(args, "symbols", None)
    return merge_settings(
        base,
        symbols=symbols_override or symbol_override,
        memory_path=getattr(args, "memory_path", None),
        paper_state_path=getattr(args, "paper_state_path", None),
        sim_state_path=getattr(args, "sim_state_path", None),
        mode=getattr(args, "mode", None),
        analyst=getattr(args, "analyst", None),
        market_source=getattr(args, "market_source", None),
        execution_source=getattr(args, "execution_source", None),
        bridge_url=getattr(args, "bridge_url", None),
        interval_seconds=getattr(args, "interval_seconds", None),
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


if __name__ == "__main__":
    main()
