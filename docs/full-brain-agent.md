# Full-Brain Agent Pivot

Quantz now separates two agent layers:

```text
TradingAgent
  -> observes one market state
  -> thinks trade/hold
  -> acts through risk + broker

TrainingOrchestrator
  -> owns a goal
  -> observes training state
  -> chooses tools
  -> acts by running backtests/evaluations/playbook adjustments
  -> repeats until the goal is satisfied or max steps is reached
```

## Four Agent Pieces

```text
LLM/reasoning engine
  RuleTrainingBrain now, LLM brain later

hands/tools
  run_backtest
  inspect_report
  evaluate_promotion_gate
  propose_playbook_adjustment

notebook/memory
  JSONL/SQLite experience
  report.json
  promotion-gate.json
  agent-run.json

destination/goals
  AgentGoal
  stage gates such as replay_to_paper
```

## Run A Goal-Directed Training Agent

```powershell
$env:PYTHONPATH="src"
python -m quantz.cli train-agent `
  --config configs/paper-demo.json `
  --symbol XAUUSD `
  --csv data/history/xauusd-sample.csv `
  --playbook-path configs/playbooks/xau_trend_continuation_v1.json `
  --output-dir data/agent-runs/xau-train-001 `
  --stage replay_to_paper `
  --max-steps 4 `
  --brain rule
```

Output:

```text
data/agent-runs/xau-train-001/agent-run.json
data/agent-runs/xau-train-001/report.json
data/agent-runs/xau-train-001/promotion-gate.json
data/agent-runs/xau-train-001/candidate-playbook.json
```

## Current Brain

The orchestrator can run with `RuleTrainingBrain` so it works without an API key:

```text
observe no result
  -> run_backtest

observe failed gate
  -> propose_playbook_adjustment

observe candidate proposed
  -> inspect_report

observe gate passed
  -> stop
```

Use the LLM brain when `OPENAI_API_KEY` is configured:

```powershell
python -m quantz.cli train-agent `
  --config configs/paper-demo.json `
  --symbol XAUUSD `
  --csv data/history/xauusd-sample.csv `
  --playbook-path configs/playbooks/xau_trend_continuation_v1.json `
  --output-dir data/agent-runs/xau-train-llm-001 `
  --stage replay_to_paper `
  --max-steps 4 `
  --brain llm
```

The LLM brain emits structured JSON thoughts:

```json
{
  "tool": "propose_playbook_adjustment",
  "arguments": {},
  "rationale": "Spread caused losses, tighten spread_max and rerun.",
  "stop": false
}
```
