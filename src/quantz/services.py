from __future__ import annotations

from pathlib import Path
from typing import Any

from quantz.agent import TradingAgent
from quantz.analyst import LLMAnalyst, NoOpAnalyst, RuleBasedAnalyst
from quantz.audit import DecisionAuditStore
from quantz.bridge import BridgeBrokerAdapter, BridgeClient
from quantz.broker import Mt5BrokerAdapter, PaperBrokerAdapter
from quantz.config import AgentSettings
from quantz.memory import experience_store
from quantz.paper import PaperPortfolio
from quantz.planner import AIDecisionPlanner, DecisionPolicy, VariableDrivenPlanner
from quantz.playbook import PlaybookLoader, PlaybookSelector
from quantz.risk import RiskConfig, RiskGovernor
from quantz.teacher import TeacherExampleStore
from quantz.vector_memory import LocalSQLiteVectorMemoryStore


class MemoryService:
    def __init__(self, settings: AgentSettings) -> None:
        self.settings = settings

    def experience(self) -> Any:
        return experience_store(self.settings.memory_path)

    def vector(self) -> Any | None:
        if not self.settings.vector_memory_enabled:
            return None
        default_path = AgentSettings().vector_memory_path
        vector_path = Path(self.settings.vector_memory_path)
        if self.settings.vector_memory_path == default_path and self.settings.memory_path != AgentSettings().memory_path:
            vector_path = Path(self.settings.memory_path).with_name("vector-memory.db")
        return LocalSQLiteVectorMemoryStore(vector_path)

    def audit(self) -> DecisionAuditStore:
        return DecisionAuditStore(self.settings.decision_audit_path)


class AgentRuntimeService:
    """Factory for the autonomous runtime without depending on legacy web UI."""

    def __init__(self, settings: AgentSettings, bridge_client: BridgeClient | None = None) -> None:
        self.settings = settings
        self.bridge_client = bridge_client or BridgeClient(settings.bridge_url)
        self.memory = MemoryService(settings)

    def build_agent(self) -> TradingAgent:
        settings = self.settings
        return TradingAgent(
            planner=self._planner(),
            risk_governor=RiskGovernor(self._risk_config()),
            broker=self._broker(),
            memory=self.memory.experience(),
            paper_portfolio=PaperPortfolio(settings.paper_state_path) if settings.mode == "paper" else None,
            analyst=self._analyst(),
            playbook_selector=self._playbook_selector(),
            mode=settings.mode,
            teacher_examples=self._teacher_examples(),
            vector_memory=self.memory.vector(),
            max_memory_context_items=settings.vector_memory_context_items,
        )

    def _planner(self) -> Any:
        settings = self.settings
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
                audit_store=self.memory.audit(),
            )
        if settings.planner == "variable":
            return VariableDrivenPlanner()
        raise ValueError(f"Unknown planner: {settings.planner}")

    def _risk_config(self) -> RiskConfig:
        settings = self.settings
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

    def _broker(self) -> Any:
        settings = self.settings
        if settings.mode == "paper":
            return PaperBrokerAdapter()
        if settings.execution_source == "bridge":
            return BridgeBrokerAdapter(self.bridge_client)
        if settings.execution_source == "mt5":
            return Mt5BrokerAdapter()
        raise ValueError(f"Unknown execution source: {settings.execution_source}")

    def _analyst(self) -> Any:
        settings = self.settings
        if settings.analyst == "none":
            return None
        if settings.analyst == "rule":
            return RuleBasedAnalyst()
        if settings.analyst == "llm":
            return LLMAnalyst(
                model=settings.llm_model,
                api_key_env=settings.llm_api_key_env,
                timeout_seconds=settings.llm_timeout_seconds,
            )
        if settings.analyst == "noop":
            return NoOpAnalyst()
        raise ValueError(f"Unknown analyst: {settings.analyst}")

    def _playbook_selector(self) -> PlaybookSelector | None:
        if not self.settings.playbook_paths:
            return None
        return PlaybookSelector(PlaybookLoader().load_many(self.settings.playbook_paths))

    def _teacher_examples(self) -> list[dict[str, Any]]:
        if not self.settings.teacher_example_paths:
            return []
        examples = TeacherExampleStore(self.settings.teacher_example_paths).load()
        return [example.to_dict() for example in examples]
