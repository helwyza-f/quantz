# Quantz Autonomous Agent Architecture

## Target

Quantz should converge on a fully autonomous trading agent with:

- AI reasoning that can propose, critique, and evolve decisions.
- Deterministic risk governance as the final execution gate.
- Structured experience memory as the source of truth.
- Vector memory for semantic recall of lessons, teacher examples, market regimes, and prior decisions.
- Auto-compacted context before any planner or LLM call.
- Training and promotion loops that let the agent improve playbooks without skipping replay, paper, demo, and tiny-live gates.

The agent may reason autonomously, but order placement must always flow through approved typed decisions, risk checks, and broker adapters.

## Desired Module Boundaries

```text
quantz.agent_core
  TradingAgent, decisions, lifecycle hooks

quantz.reasoning
  LLM analyst, critic, planner proposals, reflection payloads

quantz.memory
  JSONL/SQLite truth store, vector recall, compaction, lesson extraction

quantz.training
  replay, backtest, promotion gates, playbook evolution

quantz.runtime
  sessions, event bus, tick ingestion, monitor/replay runners

quantz.api
  FastAPI routes only, no business logic

frontend
  operational console only
```

## Local Orchestration

One local command should run the working stack:

```text
quantz.cli stack
  -> backend: uvicorn quantz.api.app:app
  -> frontend: Next.js control console
  -> optional mt5-bridge: scripts/mt5_bridge_server.py
```

The stack supervisor owns process lifecycle, environment variables, log prefixing, health checks, and graceful shutdown. It does not contain trading logic. Trading logic remains inside `AutonomousRuntime`, `AgentRuntimeService`, planners, risk, memory, and broker adapters.

## Legacy Retired

`src/quantz/web.py` has been removed. It used to mix:

- HTML page rendering.
- API summary generation.
- Tick tape ingestion.
- Monitor/replay session runtime.
- Agent construction.
- PnL/chart/report adapters.

Those responsibilities now live in `AutonomousRuntime`, `AgentRuntimeService`, `MemoryService`, FastAPI routes, and the Next.js control console.

## Memory Direction

Decision context should be assembled in this order:

```text
market/account snapshot
  -> structured recent memory summary
  -> teacher examples
  -> vector semantic recall
  -> compacted decision context
  -> planner/LLM analyst
```

Context compaction happens before the planner sees memory. This prevents a long memory trail from overwhelming reasoning and keeps the LLM payload bounded.

## Evolution Loop

The agent evolves through explicit stages:

```text
replay
  -> paper
  -> demo
  -> tiny live
  -> scaled live
```

Each stage needs a promotion gate. The AI engine can propose playbook/config changes, but only validated candidates should be promoted.

## First Refactor Slices

1. Done: memory context is vector-aware and bounded before planner/LLM use.
2. Done: `TradingAgent` accepts vector memory and auto-indexes experience.
3. Done: `AgentRuntimeService` builds AI/variable agents outside `web.py`.
4. Done: `AutonomousRuntime` owns tick ingestion, sessions, control summaries, and SSE without importing `web.py`.
5. In progress: PnL, operations, chart, replay, and audit read models now exist in the autonomous runtime, but can be split into smaller query services later.
6. Done: legacy HTML rendering was removed and its tests were replaced with autonomous runtime tests.

## Current Runtime Path

```text
FastAPI
  -> AutonomousRuntime
  -> AgentRuntimeService
  -> AIDecisionPlanner
  -> DecisionPolicy
  -> RiskGovernor
  -> BrokerAdapter
  -> JSONL/SQLite experience
  -> vector memory
  -> decision audit
```

`src/quantz/api/app.py` and `quantz.cli web` now run the autonomous runtime directly.
