from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime
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
from quantz.memory import JsonlExperienceStore
from quantz.models import AgentContext
from quantz.paper import PaperPortfolio
from quantz.planner import VariableDrivenPlanner
from quantz.report import ReportBuilder
from quantz.review import ReviewEngine
from quantz.risk import RiskConfig, RiskGovernor
from quantz.web import serve


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

    web = subparsers.add_parser("web")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8787)
    web.add_argument("--root", default=".")

    args = parser.parse_args()
    if args.command == "run-once":
        settings = _settings(args)
        record = _run_once(
            settings,
            args.symbol,
        )
        print(json.dumps(record, default=_json_default, indent=2, sort_keys=True))
    elif args.command == "learn":
        report = ExperienceLearner().analyze(JsonlExperienceStore(args.memory_path).read_raw())
        print(json.dumps(report, default=_json_default, indent=2, sort_keys=True))
    elif args.command == "monitor":
        settings = _settings(args)
        _monitor(settings, args.max_iterations)
    elif args.command == "report":
        builder = ReportBuilder()
        session_report = builder.build(
            JsonlExperienceStore(args.memory_path).read_raw(),
            args.paper_state_path,
        )
        print(json.dumps(builder.to_dict(session_report), default=_json_default, indent=2, sort_keys=True))
    elif args.command == "review":
        builder = ReportBuilder()
        session_report = builder.build(
            JsonlExperienceStore(args.memory_path).read_raw(),
            args.paper_state_path,
        )
        review_result = ReviewEngine().review(session_report)
        print(json.dumps(ReviewEngine().to_dict(review_result), default=_json_default, indent=2, sort_keys=True))
    elif args.command == "propose-config":
        report_builder = ReportBuilder()
        session_report = report_builder.build(
            JsonlExperienceStore(args.memory_path).read_raw(),
            args.paper_state_path,
        )
        review_result = ReviewEngine().review(session_report)
        candidate = CandidateConfigBuilder().build(load_settings(args.config), review_result)
        write_settings(candidate, args.output)
        print(json.dumps({"output": args.output, "config": candidate}, default=_json_default, indent=2, sort_keys=True))
    elif args.command == "compare-runs":
        builder = ReportBuilder()
        base_report = builder.build(
            JsonlExperienceStore(args.base_memory_path).read_raw(),
            args.base_paper_state_path,
        )
        candidate_report = builder.build(
            JsonlExperienceStore(args.candidate_memory_path).read_raw(),
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
    elif args.command == "web":
        serve(args.host, args.port, args.root, monitor_fn=_monitor)


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
    return TradingAgent(
        planner=VariableDrivenPlanner(),
        risk_governor=RiskGovernor(_risk_config(settings)),
        broker=_broker(settings.mode, settings.execution_source, bridge_client),
        memory=JsonlExperienceStore(settings.memory_path),
        paper_portfolio=PaperPortfolio(settings.paper_state_path) if settings.mode == "paper" else None,
        analyst=_analyst(settings),
    )


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
