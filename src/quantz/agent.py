from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from quantz.analyst import Analyst
from quantz.broker import BrokerAdapter
from quantz.memory_context import MemoryContextBuilder
from quantz.models import AgentContext, ExperienceRecord, ExecutionResult, OrderRequest, TradeAction
from quantz.paper import PaperPortfolio
from quantz.planner import AgentPlanner
from quantz.playbook import PlaybookSelector
from quantz.risk import RiskGovernor
from quantz.vector_memory import ExperienceVectorIndexer


class TradingAgent:
    def __init__(
        self,
        planner: AgentPlanner,
        risk_governor: RiskGovernor,
        broker: BrokerAdapter,
        memory: Any,
        paper_portfolio: PaperPortfolio | None = None,
        analyst: Analyst | None = None,
        playbook_selector: PlaybookSelector | None = None,
        mode: str = "paper",
        teacher_examples: list[dict[str, Any]] | None = None,
        vector_memory: Any | None = None,
        max_memory_context_items: int = 24,
    ) -> None:
        self.planner = planner
        self.risk_governor = risk_governor
        self.broker = broker
        self.memory = memory
        self.paper_portfolio = paper_portfolio
        self.analyst = analyst
        self.playbook_selector = playbook_selector
        self.mode = mode
        self.teacher_examples = teacher_examples or []
        self.vector_memory = vector_memory
        self.max_memory_context_items = max_memory_context_items

    def run_once(self, context: AgentContext) -> ExperienceRecord:
        closed_positions = self.paper_portfolio.reconcile(context.market) if self.paper_portfolio else []
        if self.paper_portfolio:
            external_open_positions = int(context.constraints.get("external_open_symbol_positions", 0) or 0)
            context = replace(
                context,
                constraints={
                    **context.constraints,
                    "block_when_symbol_open": True,
                    "open_symbol_positions": self.paper_portfolio.open_count(context.market.symbol) + external_open_positions,
                },
            )
        memory_context = MemoryContextBuilder(
            self.memory,
            teacher_examples=self.teacher_examples,
            vector_store=self.vector_memory,
            max_context_items=self.max_memory_context_items,
        ).build(context)
        context_with_memory = replace(
            context,
            constraints={
                **context.constraints,
                "memory_context": memory_context,
                "experience_memory": memory_context,
            },
        )
        playbook_context = self.playbook_selector.select(context_with_memory, self.mode).to_context() if self.playbook_selector else {
            "selected": None,
            "status": "not_configured",
            "reasons": ["no_playbook_selector"],
        }
        context = replace(
            context_with_memory,
            constraints={
                **context_with_memory.constraints,
                "playbook": playbook_context,
            },
        )
        if self.analyst:
            analysis = self.analyst.analyze(context)
            context = replace(context, constraints={**context.constraints, "analyst": analysis})
        decision = self.planner.decide(context)
        risk = self.risk_governor.evaluate(context, decision)
        execution: ExecutionResult | None = None

        if risk.approved and decision.action == TradeAction.OPEN_POSITION:
            assert decision.side is not None
            assert decision.entry_price is not None
            assert decision.stop_loss is not None
            assert decision.take_profit is not None
            order = OrderRequest(
                symbol=decision.symbol,
                side=decision.side,
                volume=risk.approved_lot,
                entry_price=decision.entry_price,
                stop_loss=decision.stop_loss,
                take_profit=decision.take_profit,
                comment=safe_order_comment(decision.decision_id),
            )
            execution = self.broker.place_order(order)
            if execution.accepted and self.paper_portfolio:
                self.paper_portfolio.open_position(
                    order,
                    execution,
                    decision_id=decision.decision_id,
                    reason_codes=decision.reason_codes,
                )

        outcome = {"closed_positions": closed_positions} if closed_positions else None
        record = ExperienceRecord(context=context, decision=decision, risk=risk, execution=execution, outcome=outcome)
        self.memory.append(record)
        self._index_semantic_memory(record)
        return record

    def _index_semantic_memory(self, record: ExperienceRecord) -> None:
        if not self.vector_memory:
            return
        try:
            ExperienceVectorIndexer(self.vector_memory).append_record(record)
        except Exception:
            return


def safe_order_comment(decision_id: str) -> str:
    """MT5/broker-safe short ASCII comment."""
    suffix = re.sub(r"[^A-Za-z0-9]", "", decision_id)[:10]
    return f"QZ{suffix}"[:20] or "QZ"
