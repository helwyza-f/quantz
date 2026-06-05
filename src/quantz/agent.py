from __future__ import annotations

from dataclasses import replace

from quantz.analyst import Analyst
from quantz.broker import BrokerAdapter
from quantz.memory import JsonlExperienceStore
from quantz.models import AgentContext, ExperienceRecord, ExecutionResult, OrderRequest, TradeAction
from quantz.paper import PaperPortfolio
from quantz.planner import AgentPlanner
from quantz.risk import RiskGovernor


class TradingAgent:
    def __init__(
        self,
        planner: AgentPlanner,
        risk_governor: RiskGovernor,
        broker: BrokerAdapter,
        memory: JsonlExperienceStore,
        paper_portfolio: PaperPortfolio | None = None,
        analyst: Analyst | None = None,
    ) -> None:
        self.planner = planner
        self.risk_governor = risk_governor
        self.broker = broker
        self.memory = memory
        self.paper_portfolio = paper_portfolio
        self.analyst = analyst

    def run_once(self, context: AgentContext) -> ExperienceRecord:
        closed_positions = self.paper_portfolio.reconcile(context.market) if self.paper_portfolio else []
        if self.paper_portfolio:
            context = replace(
                context,
                constraints={
                    **context.constraints,
                    "block_when_symbol_open": True,
                    "open_symbol_positions": self.paper_portfolio.open_count(context.market.symbol),
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
                comment=f"quantz:{decision.model_version}:{decision.decision_id[:8]}",
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
        return record
