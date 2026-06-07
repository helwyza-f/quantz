# Windows MT5 Handoff

This document preserves the current context for moving Quantz from macOS paper development to a Windows laptop or Windows VPS that can talk to MetaTrader 5.

## Current Status

- Quantz runs on the Mac in demo/paper mode.
- Paper execution works and writes experience to `data/experience.jsonl`.
- Learning analysis works through `quantz.cli learn`.
- Direct MT5 Python connection on the Mac is not available in the current environment because the `MetaTrader5` package could not be installed.
- Live execution is intentionally not enabled yet.

## Why Windows Matters

The official `MetaTrader5` Python package is the direct rail used by Quantz for:

- reading MT5 ticks/candles/account state
- reading open positions
- sending approved orders through `order_send`

That package is normally used on Windows with the MT5 terminal installed and logged in. Installing the MT5 desktop app on macOS does not automatically expose a Python API that Quantz can import.

## Option A: Run Quantz Directly On Windows

Use this when the Windows laptop will run both Quantz and MT5.

For the chart EA tick bridge, also follow [mt5-ea-bridge-setup.md](mt5-ea-bridge-setup.md).

1. Install Python 3.11 or 3.12 on Windows.
2. Install MetaTrader 5.
3. Log in to the Exness MT5 account in the terminal.
4. Enable algo trading in MT5.
5. Open PowerShell in the Quantz project.
6. Create the environment:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev,mt5]"
```

7. Confirm tests:

```powershell
.\.venv\Scripts\python -m pytest
```

8. First read live MT5 data but keep execution in paper mode:

```powershell
$env:PYTHONPATH="src"
.\.venv\Scripts\python -m quantz.cli run-once --symbol XAUUSD --market-source mt5 --mode paper
```

9. Continuous paper monitoring:

```powershell
$env:PYTHONPATH="src"
.\.venv\Scripts\python -m quantz.cli monitor --symbol XAUUSD --market-source mt5 --mode paper --interval-seconds 60
```

10. Only after demo validation, live execution direct to MT5:

```powershell
$env:PYTHONPATH="src"
.\.venv\Scripts\python -m quantz.cli run-once --symbol XAUUSD --market-source mt5 --mode live
```

## Option B: Mac Agent Plus Windows Bridge

Use this when Quantz stays on the Mac but MT5 runs on Windows.

On Windows:

```powershell
py -3.12 -m pip install MetaTrader5
python scripts\mt5_bridge_server.py --host 0.0.0.0 --port 8765
```

On the Mac:

```bash
PYTHONPATH=src .venv/bin/python -m quantz.cli run-once \
  --symbol XAUUSD \
  --market-source bridge \
  --bridge-url http://WINDOWS_IP:8765 \
  --mode paper
```

Live through the bridge, after demo validation:

```bash
PYTHONPATH=src .venv/bin/python -m quantz.cli run-once \
  --symbol XAUUSD \
  --market-source bridge \
  --execution-source bridge \
  --bridge-url http://WINDOWS_IP:8765 \
  --mode live
```

## Live Checklist

Do not use `--mode live` until all of these are true:

- MT5 is logged into the correct Exness demo account first.
- Symbol names match the broker terminal exactly.
- `run-once --market-source mt5 --mode paper` returns real bid/ask/account values.
- `monitor --market-source mt5 --mode paper` has collected enough samples.
- `learn` report is reviewed.
- Risk config is intentionally set, especially max daily loss and max risk per trade.
- A manual kill process is clear: stop terminal, stop Python process, or disable algo trading.
- Live starts with minimum lot only.

## Commands To Remember

Mac local paper:

```bash
PYTHONPATH=src .venv/bin/python -m quantz.cli monitor --symbol XAUUSD --market-source demo --mode paper --interval-seconds 60
```

Mac config-driven multi-symbol paper with simulated moving prices:

```bash
PYTHONPATH=src .venv/bin/python -m quantz.cli monitor --config configs/paper-demo.json
```

This config uses `market_source: "sim"` and `analyst: "rule"` so paper positions can hit TP/SL and create learning outcomes before MT5 is available. The analyst enriches decisions but does not bypass the risk governor.

Learning report:

```bash
PYTHONPATH=src .venv/bin/python -m quantz.cli learn --memory-path data/experience.jsonl
```

Session report:

```bash
PYTHONPATH=src .venv/bin/python -m quantz.cli report \
  --memory-path data/experience.jsonl \
  --paper-state-path data/paper-state.json
```

Self-learning recommendations:

```bash
PYTHONPATH=src .venv/bin/python -m quantz.cli review \
  --memory-path data/experience.jsonl \
  --paper-state-path data/paper-state.json
```

Candidate config proposal:

```bash
PYTHONPATH=src .venv/bin/python -m quantz.cli propose-config \
  --config configs/paper-demo.json \
  --memory-path data/experience.jsonl \
  --paper-state-path data/paper-state.json \
  --output configs/candidates/paper-demo-candidate.json
```

Compare base vs candidate paper runs:

```bash
PYTHONPATH=src .venv/bin/python -m quantz.cli compare-runs \
  --base-memory-path data/base-experience.jsonl \
  --base-paper-state-path data/base-paper-state.json \
  --candidate-memory-path data/candidate-experience.jsonl \
  --candidate-paper-state-path data/candidate-paper-state.json
```

Full local experiment:

```bash
PYTHONPATH=src .venv/bin/python -m quantz.cli experiment \
  --config configs/paper-demo.json \
  --output-dir data/experiments/run-001 \
  --iterations 24
```

Static experiment dashboard:

```bash
PYTHONPATH=src .venv/bin/python -m quantz.cli dashboard \
  --experiment-dir data/experiments/run-001 \
  --output data/experiments/run-001/dashboard.html
```

Local web UI:

```bash
PYTHONPATH=src .venv/bin/python -m quantz.cli web --host 127.0.0.1 --port 8787 --root .
```

Open `http://127.0.0.1:8787`.

The web UI can edit local configs, start/stop local paper monitor sessions, run paper experiments, render dashboards, list experiment results, inspect operations state, and track paper PnL/performance. Use `/operations` for auto-refreshing monitor controls, current paper positions, recent closes, and recent decisions. The latest monitor state/events persist in `data/monitor-session.json` across web server restarts. Use `/pnl` for auto-refreshing SVG charts covering equity curve, per-symbol R, decision reasons, rejection reasons, and reason quality. New paper positions persist `decision_id` and `reason_codes`, so future closed trades can attribute R back to the opening rationale. Use `/market` for terminal-style simulated OHLC candlestick charts with symbol switching, MA fast/slow, ATR band, decision markers, open-position entry/SL/TP overlays, and closed-trade markers; on Windows this can later be backed by MT5 rates. Use `/api/monitor`, `/api/operations`, `/api/performance`, `/api/pnl`, `/api/market-chart`, and `/api/visual-digest` for JSON payloads that can feed a richer dashboard or an agent review loop later. Keep live execution disabled until the MT5 demo flow has been validated.

Tests:

```bash
.venv/bin/python -m pytest
```
