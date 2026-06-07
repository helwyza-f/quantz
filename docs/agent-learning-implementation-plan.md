# Quantz Agent Learning Implementation Plan

This document is the implementation roadmap for moving Quantz from a rule-assisted trading loop into an agent system that can observe, reason, act, record outcomes, and improve from experience.

The goal is not to let an LLM freely trade. The goal is to build a disciplined agent loop where AI reasoning is grounded by structured memory, playbooks, historical replay, paper outcomes, and deterministic risk controls.

## Target Lifecycle

```text
Bootstrapped knowledge
  -> historical replay
  -> paper trading
  -> demo execution
  -> tiny live execution
  -> continuous learning
```

Each stage must produce auditable memory before the next stage is allowed.

## Target Architecture

```text
Market feed
  -> short-term memory
  -> memory context builder
  -> playbook selector
  -> AI analyst / planner
  -> risk governor
  -> broker adapter
  -> execution/outcome reconciler
  -> long-term structured memory
  -> playbook/stat evaluator
  -> semantic vector memory
  -> next decision context
```

## Memory Strategy

### Short-Term Memory

Short-term memory is the current working state of the agent. It should be fast, small, and windowed.

Store:

- recent ticks/candles summary
- current market regime
- latest N decisions
- open positions
- recent rejected decisions
- recent execution errors
- symbol/session cooldown state
- current risk posture

Recommended storage:

- in-memory cache for active runtime
- SQLite table for restart persistence

This memory feeds every decision cycle.

### Long-Term Structured Memory

Long-term structured memory is the source of truth. This is where learning should start.

Use SQLite first. JSONL is fine for early MVP logs, but it becomes weak once we need queries like:

- performance by symbol
- performance by session
- performance by setup
- reason-code expectancy
- max drawdown by playbook
- trade lifecycle reconstruction

Core tables:

- `decisions`
- `orders`
- `positions`
- `outcomes`
- `risk_events`
- `reason_stats`
- `playbook_stats`
- `agent_reviews`
- `market_snapshots`

### Semantic Vector Memory

Vector memory is a secondary recall layer, not the source of truth.

Use it for:

- similar setup recall
- teacher examples
- decision briefs
- market reads
- entry plans
- invalidation notes
- post-trade reviews
- lesson-learned records

Do not use it for:

- raw ticks
- every candle
- canonical trade lifecycle state
- financial calculations
- risk limits

Recommended local stack:

```text
SQLite = structured source of truth
Qdrant local = semantic/vector memory
sentence-transformers = local embeddings
```

Qdrant should run locally:

```powershell
docker run -p 6333:6333 -v ${PWD}\qdrant_storage:/qdrant/storage qdrant/qdrant
```

Initial embedding model:

```text
sentence-transformers/all-MiniLM-L6-v2
```

This keeps memory local. No hosted vector service is required.

## Phase 1: Structured Memory Foundation

Objective: replace JSONL-only memory with queryable SQLite memory while keeping backward compatibility.

Implementation steps:

1. Add `quantz.memory_sqlite`.
2. Create a `SQLiteExperienceStore` with append/read APIs compatible with `JsonlExperienceStore`.
3. Add migration/import from existing JSONL experience files.
4. Add schema initialization.
5. Add indexes by `symbol`, `decision_id`, `timestamp`, `action`, `risk_status`, and `position_id`.
6. Keep JSONL append as optional audit log during transition.

Initial schema sketch:

```sql
create table decisions (
  decision_id text primary key,
  symbol text not null,
  timestamp text not null,
  action text not null,
  side text,
  confidence real not null,
  entry_price real,
  stop_loss real,
  take_profit real,
  risk_percent real not null,
  model_version text not null,
  reason_codes_json text not null,
  market_snapshot_json text not null,
  account_snapshot_json text not null,
  context_json text not null,
  metadata_json text not null
);

create table executions (
  client_order_id text primary key,
  decision_id text not null,
  broker_order_id text,
  accepted integer not null,
  message text not null,
  filled_price real,
  timestamp text not null,
  raw_json text not null
);

create table outcomes (
  outcome_id text primary key,
  decision_id text not null,
  symbol text not null,
  side text not null,
  opened_at text,
  closed_at text,
  entry_price real,
  exit_price real,
  stop_loss real,
  take_profit real,
  r_multiple real not null,
  exit_reason text,
  reason_codes_json text not null,
  raw_json text not null
);
```

Acceptance criteria:

- Existing tests still pass.
- Existing JSONL memory can be imported.
- Agent can write to SQLite memory.
- Reports/reviews can read from SQLite memory.
- No trading behavior changes yet.

## Phase 2: Trade Lifecycle Integrity

Objective: make every trade auditable from decision to outcome.

Current desired chain:

```text
decision_id -> client_order_id -> broker_order_id/position_id -> close outcome
```

Implementation steps:

1. Add explicit `position_id` to paper positions.
2. Persist `decision_id`, `client_order_id`, and `position_id` together.
3. Update paper reconciliation so closed positions always include the original decision metadata.
4. Add lifecycle queries:
   - decision by id
   - open positions by decision/playbook
   - outcome by decision
   - unresolved executed decisions
5. Add lifecycle integrity tests.

Acceptance criteria:

- Every closed paper trade can be traced back to its opening decision.
- Every execution can be traced back to a decision.
- Reports can compute performance by reason code and setup without guessing.

## Phase 3: Memory Context Builder

Objective: every decision should receive compact, useful memory context.

Implementation steps:

1. Add `quantz.memory_context.MemoryContextBuilder`.
2. Build context per symbol/session/regime.
3. Include:
   - recent decisions
   - recent rejected reasons
   - open position state
   - reason-code quality
   - playbook stats
   - similar historical cases
   - current risk posture
4. Inject memory context into `AgentContext.constraints["memory_context"]`.
5. Deprecate ad hoc memory summaries once the builder is stable.

Example output:

```json
{
  "symbol": "XAUUSD",
  "session": "london",
  "recent_decisions": [],
  "reason_quality": {
    "bullish_market_structure": {
      "trades": 34,
      "average_r": 0.21,
      "win_rate": 0.52
    }
  },
  "risk_posture": {
    "open_positions": 0,
    "recent_loss_streak": 1,
    "daily_r": -0.4
  },
  "lessons": [
    "Avoid trend continuation if spread is above 120 points."
  ]
}
```

Acceptance criteria:

- LLM analyst payload includes memory context.
- Rule planner can read memory-derived constraints.
- Context is small enough for repeated decision cycles.
- Context generation is deterministic and test-covered.

## Phase 4: Playbook Engine

Objective: give the agent explicit starting strategies instead of making it learn from zero.

Implementation steps:

1. Add `configs/playbooks/*.json`.
2. Add `Playbook` model.
3. Add `PlaybookSelector`.
4. Add playbook conditions:
   - symbol
   - session
   - regime
   - trend score
   - volatility band
   - spread max
   - news filter
5. Add playbook risk settings:
   - default risk percent
   - stop ATR multiple
   - take profit R multiple
   - max trades per session
6. Add playbook status:
   - `training`
   - `paper_allowed`
   - `demo_allowed`
   - `live_tiny_allowed`
   - `disabled`

Example:

```json
{
  "name": "xau_trend_pullback_v1",
  "symbols": ["XAUUSD"],
  "sessions": ["london", "new_york"],
  "allowed_regimes": ["trend"],
  "entry_conditions": {
    "trend_score_min": 0.65,
    "volatility_min": 0.25,
    "volatility_max": 0.75,
    "spread_max": 80
  },
  "risk": {
    "risk_percent": 0.25,
    "sl_atr": 1.5,
    "tp_r": 1.7
  },
  "status": "training"
}
```

Acceptance criteria:

- Planner decisions include `playbook_id`.
- Reports can show performance by playbook.
- Disabled playbooks cannot open trades.
- Live mode only allows playbooks explicitly promoted to live status.

## Phase 5: Teacher Examples

Objective: bootstrap the agent with curated examples before it has its own history.

Implementation steps:

1. Add `data/teacher_examples/*.json`.
2. Create examples for:
   - good trade
   - good hold
   - bad spread
   - bad volatility
   - bad news risk
   - duplicate position avoidance
3. Import examples into structured memory as synthetic lessons.
4. Embed narrative parts into vector memory.
5. Include top matching teacher examples in decision context.

Acceptance criteria:

- New agent has useful lessons before first trade.
- Teacher examples are tagged as synthetic, not real outcomes.
- Reports do not mix synthetic examples with actual PnL.

## Phase 6: Historical Replay Simulator

Objective: let the agent generate experience cheaply before paper/live execution.

Implementation steps:

1. Add historical candle/tick data format.
2. Add `ReplayMarketFeed`.
3. Add replay broker/paper simulator.
4. Replay loop:

```text
load candle t
build context
agent decide
risk evaluate
sim execute
advance market
close TP/SL if hit
write memory
```

5. Add replay CLI:

```powershell
quantz replay --symbol XAUUSD --data data/history/xauusd-m5.csv --playbook xau_trend_pullback_v1
```

6. Add replay report and promotion review.

Acceptance criteria:

- Agent can run thousands of historical decisions without broker access.
- Outcomes are written to structured memory.
- Same replay seed produces deterministic results.
- Replay can compare playbook variants.

## Phase 7: Memory Evaluator And Playbook Learning

Objective: convert experience into measurable playbook changes.

Implementation steps:

1. Add `MemoryEvaluator`.
2. Compute:
   - trades
   - win rate
   - average R
   - total R
   - max drawdown R
   - loss streak
   - profit factor
   - expectancy by reason code
   - expectancy by session
   - expectancy by volatility bucket
3. Add playbook recommendations:
   - keep
   - disable
   - tighten spread
   - raise confidence
   - reduce session
   - reduce risk
   - collect more data
4. Add candidate config/playbook generation.

Acceptance criteria:

- Learning recommendations are backed by metrics.
- Candidate changes can be replay-tested before promotion.
- No live config is mutated automatically.

## Phase 8: Local Vector Memory

Objective: add semantic recall for similar setups and lessons.

Implementation steps:

1. Add optional `qdrant-client` dependency.
2. Add optional `sentence-transformers` dependency.
3. Add `VectorMemoryStore` interface.
4. Add `QdrantVectorMemoryStore`.
5. Add embedding model loader.
6. Store documents:
   - teacher examples
   - decision briefs
   - market reads
   - invalidations
   - post-trade reviews
   - lessons learned
7. Query top K similar memories during context build.

Document shape:

```json
{
  "id": "lesson:xau_trend_pullback_v1:2026-06-07",
  "text": "XAUUSD trend continuation during New York session performed poorly when spread exceeded 120 points.",
  "metadata": {
    "symbol": "XAUUSD",
    "playbook": "xau_trend_pullback_v1",
    "memory_type": "lesson",
    "source": "post_trade_review"
  }
}
```

Acceptance criteria:

- App works without Qdrant when vector memory is disabled.
- Qdrant runs locally.
- Embeddings can be generated locally.
- Retrieved memories are included in analyst context with source metadata.

## Phase 9: Promotion Gates

Objective: prevent premature movement from replay to paper, demo, and live.

Suggested gates:

### Replay -> Paper

- at least 200 replay decisions
- at least 30 closed simulated trades
- average R above 0
- max drawdown under configured limit

### Paper -> Demo

- at least 50 closed paper trades
- average R above 0.10
- no unresolved lifecycle gaps
- execution simulation stable

### Demo -> Tiny Live

- at least 100 closed demo trades
- average R above 0.15
- max drawdown below 6R
- no critical broker errors
- playbook status explicitly promoted

### Tiny Live -> Increased Live

- at least 100 tiny live trades
- stable expectancy
- no operational incidents
- manual approval

Acceptance criteria:

- Promotion status is computed from metrics.
- UI/API show why a playbook is blocked.
- Live mode cannot use unpromoted playbooks.

## Implementation Order

Recommended sequence:

1. SQLite structured memory.
2. Trade lifecycle integrity.
3. Memory context builder.
4. Playbook engine.
5. Teacher examples.
6. Historical replay.
7. Memory evaluator.
8. Local Qdrant vector memory.
9. Promotion gates.
10. UI visibility for memory/playbooks/replay results.

## Near-Term Tasks

### Task 1: Add SQLite Memory Store

Files likely touched:

- `src/quantz/memory.py`
- `src/quantz/memory_sqlite.py`
- `tests/test_memory_sqlite.py`
- `pyproject.toml`

Deliverable:

- Agent can write/read experience through SQLite.

### Task 2: Add Memory Context Builder

Files likely touched:

- `src/quantz/memory_context.py`
- `src/quantz/agent.py`
- `src/quantz/analyst.py`
- `tests/test_memory_context.py`

Deliverable:

- Every agent decision includes structured memory context.

### Task 3: Add Playbook Models

Files likely touched:

- `src/quantz/playbook.py`
- `configs/playbooks/xau_trend_pullback_v1.json`
- `src/quantz/planner.py`
- `tests/test_playbook.py`

Deliverable:

- Planner can select and tag an active playbook.

### Task 4: Add Replay Runner

Files likely touched:

- `src/quantz/replay.py`
- `src/quantz/cli.py`
- `tests/test_replay.py`

Deliverable:

- Agent can train on historical data before paper trading.

## Product Principle

The agent should become smarter through measurable feedback, not vibes.

Every AI-generated suggestion should be grounded by:

- current market context
- active playbook
- structured memory metrics
- similar historical cases
- deterministic risk limits
- post-trade outcome review

LLM reasoning is useful, but the memory and risk system must remain the source of discipline.
