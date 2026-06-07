# MT5 EA Bridge Setup

Quantz already includes an MT5 Expert Advisor bridge:

```text
scripts/QuantzTickBridge.mq5
```

The EA posts every MT5 tick to the Quantz backend:

```text
GET http://127.0.0.1:8787/bridge/tick?...tick fields...
```

This feeds the live control dashboard and stream-triggered agent sessions.

## Install The EA

1. Open MetaTrader 5.
2. Open `File -> Open Data Folder`.
3. Copy:

```text
scripts/QuantzTickBridge.mq5
```

to:

```text
MQL5/Experts/QuantzTickBridge.mq5
```

4. Open MetaEditor.
5. Compile `QuantzTickBridge.mq5`.
6. In MT5, enable `Algo Trading`.
7. Attach `QuantzTickBridge` to the chart you want to stream, for example `XAUUSD`.

Recommended EA inputs:

```text
QuantzHost = 127.0.0.1
QuantzPort = 8787
QuantzPath = /bridge/tick
TimeoutMs = 1000
MinMillisBetweenPosts = 250
```

Use `MinMillisBetweenPosts = 0` only if you really want every tick.

## Run Quantz Backend And Frontend

From the repo:

```powershell
$env:PYTHONPATH="src"
python -m uvicorn quantz.api.app:app --host 127.0.0.1 --port 8787
```

Then run the frontend:

```powershell
npm.cmd --prefix frontend run dev
```

Open:

```text
http://127.0.0.1:3000/control
```

When ticks arrive, the Control page should show EA/socket tick updates.

## Replay Lab

Historical replay/backtest is available in the web UI:

```text
http://127.0.0.1:3000/replay
```

Replay Lab currently reads CSV candle files and streams agent reasoning over SSE from:

```text
GET /replay/events
```

Use this for market-closed training:

```text
data/history/xauusd-sample.csv
```

For real training, export historical candles from MT5 into the replay CSV schema:

```text
time,open,high,low,close,spread_points,atr_points,trend_score,volatility_score,session
```

## Live Tick Bridge vs Historical Replay

Use the EA bridge for live/demo chart ticks.

Use Replay Lab CSV for historical replay. This is more reliable than depending on Strategy Tester networking behavior. If MT5 Strategy Tester allows socket calls in your local terminal, the EA may also stream tester ticks, but Quantz should treat CSV replay as the primary historical training path.

## Quick Troubleshooting

- If no ticks arrive, confirm backend is running on port `8787`.
- Confirm the EA is attached to the active symbol chart.
- Confirm MT5 `Algo Trading` is enabled.
- Check the MT5 Experts tab for `QuantzTickBridge` socket errors.
- If using Docker backend, confirm port `8787:8787` is published.
- If Windows Firewall prompts, allow local network access for MT5/Python.
