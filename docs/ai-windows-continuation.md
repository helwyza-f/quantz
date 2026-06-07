# AI Continuation Notes For Windows

This document is the handoff context for continuing Quantz on a Windows laptop or Windows VPS.

## Current Architecture

Quantz is a FastAPI autonomous backend plus a Next.js operational console for an AI-first market agent.

Runtime flow:

```text
market feed -> context builder -> AI planner/analyst -> policy guard -> risk governor -> broker adapter -> experience/vector/audit memory -> operational console
```

Important modules:

- `src/quantz/cli.py`: command entrypoint.
- `src/quantz/api/app.py`: FastAPI autonomous API.
- `src/quantz/autonomous_runtime.py`: tick ingestion, sessions, SSE, control summaries.
- `src/quantz/services.py`: agent, memory, planner, broker runtime factories.
- `src/quantz/agent.py`: observe, decide, risk-check, execute, record loop.
- `src/quantz/market.py`: demo, simulated, MT5, and bridge market/account feeds.
- `src/quantz/paper.py`: paper portfolio, paper PnL, position close logic.
- `src/quantz/planner.py`: AI decision planner, policy guard, and deterministic fallback planner.
- `src/quantz/analyst.py`: rule-based analyst layer.
- `src/quantz/risk.py`: deterministic risk governor.
- `src/quantz/bridge.py`: HTTP bridge client for Windows MT5 bridge.
- `scripts/mt5_bridge_server.py`: Windows-side MT5 HTTP bridge server.

## What Already Works

- Editable Python install with tests.
- Paper trading run-once and monitor loop.
- Simulated multi-symbol market feed with moving OHLC history.
- Paper positions can open and close at TP/SL.
- Experience memory in `data/experience.jsonl`.
- Paper state in `data/paper-state.json`.
- Monitor session state in `data/monitor-session.json`.
- Sim market state in `data/sim-market-state.json`.
- Learning/report/review/candidate/experiment/dashboard CLI commands.
- Autonomous FastAPI backend on `127.0.0.1:8787`.
- Next.js control console on `127.0.0.1:3000/control`.
- EA socket tick ingestion through `/bridge/tick`.
- Decision audit JSONL for AI planner requests, outputs, policy clamps, and final payloads.
- New paper positions persist `decision_id` and `reason_codes`, so closed trades can attribute R back to the reason code that opened the trade.

## Current Limitation

Live execution is still gated and should start tiny. The core runtime now supports an AI decision planner, policy guard, deterministic risk governor, vector memory, and audit trail. If the AI key/model is unavailable, AI planner configs fail closed.

On macOS, direct `MetaTrader5` Python integration is not expected to work reliably. Windows is the right place to validate direct MT5 access.

## Windows Setup

PowerShell from the project root:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev,mt5]"
.\.venv\Scripts\python -m pytest
```

Run autonomous backend:

```powershell
$env:PYTHONPATH="src"
.\.venv\Scripts\python -m quantz.cli web --host 127.0.0.1 --port 8787 --root .
```

Open backend health:

```text
http://127.0.0.1:8787/health
```

Run the Next.js console separately:

```powershell
$env:NEXT_PUBLIC_API_BASE="http://127.0.0.1:8787"
npm.cmd --prefix frontend run dev
```

Open:

```text
http://127.0.0.1:3000/control
```

Run simulated paper monitor:

```powershell
$env:PYTHONPATH="src"
.\.venv\Scripts\python -m quantz.cli monitor --config configs/paper-demo.json
```

Read live MT5 market/account data, but keep execution paper-only:

```powershell
$env:PYTHONPATH="src"
.\.venv\Scripts\python -m quantz.cli run-once --symbol XAUUSD --market-source mt5 --mode paper
```

## MT5 Checklist On Windows

Before trying MT5 commands:

- Install MetaTrader 5.
- Log in to the Exness demo account first.
- Confirm symbol names exactly as MT5 shows them.
- Enable algo trading in MT5.
- Install package with `.[dev,mt5]`.
- Verify `.\.venv\Scripts\python -m pytest` passes.
- Verify paper-mode MT5 read returns real bid/ask/account values.

Do not use live mode until paper-mode MT5 monitoring has enough samples and the risk config has been reviewed.

## Next Best Steps

1. Run the project on Windows and verify direct `MetaTrader5` import works.
2. Run `run-once --market-source mt5 --mode paper` against Exness demo.
3. Attach `QuantzTickBridge` EA so `/bridge/tick` receives live ticks.
4. Run `mt5-paper.json` with AI planner in paper mode and inspect `data/mt5-paper-decision-audit.jsonl`.
5. Add an agent visual-review endpoint that reads `/api/market-chart` plus reason codes and produces a structured chart assessment.
6. Add an LLM reasoning layer after the deterministic planner, but keep risk governor as the final hard gate.
7. Add broker reconciliation for live/paper state drift.
8. Only after all above, test bridge/direct live execution with minimum lot on demo.

## Guardrails

- Keep `data/` out of git. It contains local runtime state.
- Keep live execution behind `risk.py`.
- Do not let LLM output place trades directly. It can propose or critique; execution must go through risk governor and broker adapter.
- Use paper mode for all MT5 integration tests first.
- If adding OpenAI/LLM keys later, use environment variables or ignored local config, not committed files.

## Useful Commands

```powershell
$env:PYTHONPATH="src"
.\.venv\Scripts\python -m quantz.cli web --host 127.0.0.1 --port 8787 --root .
.\.venv\Scripts\python -m quantz.cli monitor --config configs/paper-demo.json
.\.venv\Scripts\python -m quantz.cli experiment --config configs/paper-demo.json --output-dir data/experiments/run-001 --iterations 24
.\.venv\Scripts\python -m quantz.cli review --memory-path data/experience.jsonl --paper-state-path data/paper-state.json
.\.venv\Scripts\python -m pytest
```
