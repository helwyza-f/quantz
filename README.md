# Quantz

Quantz is a foundation for an autonomous market agent that observes market state, reasons over structured variables, validates risk, executes through a broker adapter, monitors outcomes, and stores experience for later learning.

This is not an EA-style fixed-rule bot. The trading loop is intentionally split into independent layers:

```text
market inputs -> world state -> agent planner -> risk governor -> broker adapter -> experience memory
```

## Current MVP

- Structured market/account/trade models.
- Variable-driven decision planner.
- Deterministic risk governor with hard vetoes.
- Paper broker adapter for safe dry runs.
- Demo and MT5 market/account feeds.
- HTTP bridge feed/execution client for running MT5 on a Windows VPS while the agent runs elsewhere.
- MT5 adapter interface stub for future Exness live execution.
- JSONL experience store.
- Learning analyzer that turns recorded experience into candidate tuning suggestions.
- CLI demo and monitor commands for observe-decide-execute-record cycles.
- Rule-based analyst layer that enriches planner decisions without bypassing risk.

## Run

```bash
PYTHONPATH=src python3 -m quantz.cli run-once --symbol XAUUSD
```

Run continuous paper monitoring with static demo data:

```bash
PYTHONPATH=src python3 -m quantz.cli monitor \
  --symbol XAUUSD \
  --market-source demo \
  --mode paper \
  --interval-seconds 60
```

Run the config-driven multi-symbol paper monitor:

```bash
PYTHONPATH=src python3 -m quantz.cli monitor --config configs/paper-demo.json
```

`configs/paper-demo.json` uses `market_source: "sim"` and `analyst: "rule"`. The simulated feed writes `data/sim-market-state.json` so repeated monitor iterations can move price, close paper positions at TP/SL, and produce learning outcomes. The analyst can adjust confidence or block trades, but risk governor remains the final gate.

Short test run:

```bash
PYTHONPATH=src python3 -m quantz.cli monitor \
  --config configs/paper-demo.json \
  --max-iterations 2 \
  --interval-seconds 1
```

Paper state is written to `data/paper-state.json`. Experience memory is written to `data/experience.jsonl`.

Generate a session report:

```bash
PYTHONPATH=src python3 -m quantz.cli report \
  --memory-path data/experience.jsonl \
  --paper-state-path data/paper-state.json
```

Generate self-learning recommendations:

```bash
PYTHONPATH=src python3 -m quantz.cli review \
  --memory-path data/experience.jsonl \
  --paper-state-path data/paper-state.json
```

Propose a paper-only candidate config from the review output:

```bash
PYTHONPATH=src python3 -m quantz.cli propose-config \
  --config configs/paper-demo.json \
  --memory-path data/experience.jsonl \
  --paper-state-path data/paper-state.json \
  --output configs/candidates/paper-demo-candidate.json
```

Compare a base run against a candidate run:

```bash
PYTHONPATH=src python3 -m quantz.cli compare-runs \
  --base-memory-path data/base-experience.jsonl \
  --base-paper-state-path data/base-paper-state.json \
  --candidate-memory-path data/candidate-experience.jsonl \
  --candidate-paper-state-path data/candidate-paper-state.json
```

Run a full self-learning experiment in one command:

```bash
PYTHONPATH=src python3 -m quantz.cli experiment \
  --config configs/paper-demo.json \
  --output-dir data/experiments/run-001 \
  --iterations 24
```

Render a static HTML dashboard from an experiment:

```bash
PYTHONPATH=src python3 -m quantz.cli dashboard \
  --experiment-dir data/experiments/run-001 \
  --output data/experiments/run-001/dashboard.html
```

Run the local web UI for config editing and experiment tracking:

```bash
PYTHONPATH=src python3 -m quantz.cli web --host 127.0.0.1 --port 8787 --root .
```

Open `http://127.0.0.1:8787`.

From the web UI you can:

- edit `configs/*.json`
- run a local paper experiment
- start and stop a local paper monitor session from the Operations page
- keep the latest monitor session state/events in `data/monitor-session.json` across web server restarts
- auto-render `dashboard.html`
- open experiment dashboards from the Experiments page
- inspect the auto-refreshing Operations page for current agent mode, market source, symbols, open paper positions, recent closes, and recent decisions
- inspect the auto-refreshing PnL Dashboard for equity curve, per-symbol performance, decision reasons, and rejection reasons
- inspect visual SVG charts for equity curve, symbol R, decision reasons, rejection reasons, and reason quality
- inspect the Market page for terminal-style simulated OHLC candlestick charts with symbol switching, MA fast/slow, ATR band, decision markers, open-position entry/SL/TP overlays, and closed-trade markers
- attribute closed paper-trade R back to the opening decision reason codes when new paper positions are created
- query `/api/monitor`, `/api/operations`, `/api/performance`, `/api/pnl`, and `/api/visual-digest` for dashboard-ready and agent-readable JSON
- track paper PnL in R units and estimated currency PnL
- track win rate, open positions, latest verdict, and promotion status

Read live MT5 market/account state while still executing in paper mode:

```bash
PYTHONPATH=src python3 -m quantz.cli run-once --symbol XAUUSD --market-source mt5 --mode paper
```

Use the rule-based analyst explicitly:

```bash
PYTHONPATH=src python3 -m quantz.cli run-once \
  --config configs/paper-demo.json \
  --symbol XAUUSD \
  --analyst rule
```

On macOS, the official MetaTrader5 Python package may not be available. In that setup, run an MT5 bridge on a Windows machine/VPS and point Quantz to it:

```bash
PYTHONPATH=src python3 -m quantz.cli run-once \
  --symbol XAUUSD \
  --market-source bridge \
  --bridge-url http://YOUR_WINDOWS_VPS:8765 \
  --mode paper
```

When demo validation is complete, live execution through the bridge uses:

```bash
PYTHONPATH=src python3 -m quantz.cli run-once \
  --symbol XAUUSD \
  --market-source bridge \
  --execution-source bridge \
  --bridge-url http://YOUR_WINDOWS_VPS:8765 \
  --mode live
```

Expected bridge contract:

```text
GET  /market/{symbol}
GET  /account
POST /orders
```

Bridge server script for the Windows/VPS side:

```bash
python scripts/mt5_bridge_server.py --host 0.0.0.0 --port 8765
```

Or after editable install:

```bash
pip install -e ".[dev]"
PYTHONPATH=src python3 -m quantz.cli run-once --symbol XAUUSD
PYTHONPATH=src python3 -m quantz.cli learn --memory-path data/experience.jsonl
PYTHONPATH=src python3 -m quantz.cli report --memory-path data/experience.jsonl --paper-state-path data/paper-state.json
PYTHONPATH=src python3 -m quantz.cli review --memory-path data/experience.jsonl --paper-state-path data/paper-state.json
PYTHONPATH=src python3 -m quantz.cli propose-config --config configs/paper-demo.json --memory-path data/experience.jsonl --paper-state-path data/paper-state.json --output configs/candidates/paper-demo-candidate.json
PYTHONPATH=src python3 -m quantz.cli compare-runs --base-memory-path data/base-experience.jsonl --base-paper-state-path data/base-paper-state.json --candidate-memory-path data/candidate-experience.jsonl --candidate-paper-state-path data/candidate-paper-state.json
PYTHONPATH=src python3 -m quantz.cli experiment --config configs/paper-demo.json --output-dir data/experiments/run-001 --iterations 24
PYTHONPATH=src python3 -m quantz.cli dashboard --experiment-dir data/experiments/run-001 --output data/experiments/run-001/dashboard.html
PYTHONPATH=src python3 -m quantz.cli web --host 127.0.0.1 --port 8787 --root .
pytest
```

## Production Shape

Live execution should stay behind a deterministic risk governor:

```text
AI proposes action
Risk governor approves or rejects
Execution adapter places the trade
Monitor reconciles broker state
Experience store records the full decision and outcome
```

The MT5 adapter is the technical rail for placing trades with an Exness MT5 account. It is not the agent brain.

## Windows MT5 Handoff

See [docs/windows-mt5-handoff.md](docs/windows-mt5-handoff.md) before moving this project to the Windows laptop. It documents the current Mac limitation, direct Windows MT5 setup, bridge setup, and the live checklist.

For an AI/Codex continuation prompt on Windows, also read [docs/ai-windows-continuation.md](docs/ai-windows-continuation.md). It captures the current architecture, what already works, known limitations, and the next best implementation steps.
