# Historical Training While Market Is Closed

Use historical replay/backtest to teach Quantz before paper/live execution.

## CSV Format

Required columns:

```text
time,open,high,low,close,spread_points,atr_points,trend_score,volatility_score,session
```

Example file:

```text
data/history/xauusd-sample.csv
```

For real training, export M5/M15 candles from MT5 or another local source and convert them into this schema. Keep one symbol/timeframe per file.

## Run Backtest Training

```powershell
$env:PYTHONPATH="src"
python -m quantz.cli backtest `
  --config configs/paper-demo.json `
  --symbol XAUUSD `
  --csv data/history/xauusd-sample.csv `
  --output-dir data/backtests/xauusd-run-001
```

The command writes:

- `experience.jsonl`
- `paper-state.json`
- `report.json`
- `promotion-gate.json`

## Check Promotion Gate

```powershell
python -m quantz.cli promotion-gate `
  --stage replay_to_paper `
  --memory-path data/backtests/xauusd-run-001/experience.jsonl `
  --paper-state-path data/backtests/xauusd-run-001/paper-state.json
```

## Training Loop

```text
historical CSV
  -> backtest
  -> report
  -> promotion gate
  -> adjust playbook
  -> rerun backtest
```

Do not use sample CSV results for real trading decisions. The sample is only a smoke-test/template file.
