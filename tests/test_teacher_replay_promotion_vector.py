import json
from argparse import Namespace

from quantz.cli import _backtest
from quantz.agent import TradingAgent
from quantz.broker import PaperBrokerAdapter
from quantz.memory import JsonlExperienceStore
from quantz.models import AccountState
from quantz.paper import PaperPortfolio
from quantz.planner import VariableDrivenPlanner
from quantz.promotion import PromotionGateEvaluator
from quantz.replay import ReplayRunner
from quantz.report import ReportBuilder
from quantz.risk import RiskGovernor
from quantz.teacher import TeacherExampleStore
from quantz.vector_memory import LocalHashEmbedder, LocalSQLiteVectorMemoryStore, VectorMemoryDocument


def test_teacher_example_store_loads_examples(tmp_path):
    path = tmp_path / "examples.json"
    path.write_text(
        json.dumps(
            {
                "example_id": "ex1",
                "symbol": "XAUUSD",
                "playbook": "pb",
                "setup": "trend",
                "decision": "hold",
                "lesson": "Avoid bad spread.",
            }
        ),
        encoding="utf-8",
    )

    examples = TeacherExampleStore([path]).for_symbol("XAUUSD")

    assert examples[0]["example_id"] == "ex1"
    assert examples[0]["lesson"] == "Avoid bad spread."


def test_replay_runner_reads_csv_and_records_decisions(tmp_path):
    csv_path = tmp_path / "candles.csv"
    csv_path.write_text(
        "time,open,high,low,close,spread_points,atr_points,trend_score,volatility_score,session\n"
        "2026-01-01T00:00:00+00:00,100,101,99,100.5,10,80,0.8,0.5,london\n",
        encoding="utf-8",
    )
    memory = JsonlExperienceStore(tmp_path / "experience.jsonl")
    agent = TradingAgent(
        planner=VariableDrivenPlanner(),
        risk_governor=RiskGovernor(),
        broker=PaperBrokerAdapter(),
        memory=memory,
        paper_portfolio=PaperPortfolio(tmp_path / "paper-state.json"),
    )

    summary = ReplayRunner(agent, AccountState(10000, 10000, 9500)).run_csv(
        "XAUUSD",
        csv_path,
        constraints={"min_confidence": 0.65, "default_risk_percent": 0.25},
    )

    assert summary.decisions == 1
    assert memory.read_raw()


def test_backtest_helper_writes_isolated_outputs(tmp_path):
    config_path = tmp_path / "config.json"
    csv_path = tmp_path / "candles.csv"
    output_dir = tmp_path / "backtest"
    config_path.write_text(
        json.dumps(
            {
                "symbols": ["XAUUSD"],
                "memory_path": "unused.jsonl",
                "paper_state_path": "unused-paper.json",
                "mode": "paper",
                "market_source": "demo",
                "execution_source": "mt5",
                "analyst": "none",
            }
        ),
        encoding="utf-8",
    )
    csv_path.write_text(
        "time,open,high,low,close,spread_points,atr_points,trend_score,volatility_score,session\n"
        "2026-01-01T00:00:00+00:00,100,101,99,100.5,10,80,0.8,0.5,london\n",
        encoding="utf-8",
    )

    result = _backtest(
        Namespace(
            config=str(config_path),
            symbol="XAUUSD",
            csv=str(csv_path),
            output_dir=str(output_dir),
            promotion_stage="replay_to_paper",
        )
    )

    assert result["replay"]["decisions"] == 1
    assert (output_dir / "experience.jsonl").exists()
    assert (output_dir / "report.json").exists()
    assert (output_dir / "promotion-gate.json").exists()


def test_promotion_gate_blocks_small_sample(tmp_path):
    report = ReportBuilder().build([], tmp_path / "missing-paper-state.json")

    gate = PromotionGateEvaluator().evaluate(report, "replay_to_paper")

    assert gate.allowed is False
    assert "not_enough_decisions" in gate.reasons


def test_local_hash_embedder_is_deterministic():
    embedder = LocalHashEmbedder(dimensions=16)

    first = embedder.embed("trend normal spread")
    second = embedder.embed("trend normal spread")

    assert first == second
    assert len(first) == 16


def test_local_sqlite_vector_memory_searches_and_compacts(tmp_path):
    store = LocalSQLiteVectorMemoryStore(tmp_path / "vector.db", dimensions=16)
    documents = [
        VectorMemoryDocument(
            doc_id=f"doc-{index}",
            text=f"XAUUSD trend setup memory {index}",
            metadata={
                "kind": "experience",
                "symbol": "XAUUSD",
                "action": "open_position",
                "risk_status": "approved",
                "reason_codes": ["bullish_market_structure"],
                "timestamp": f"2026-01-01T00:0{index}:00+00:00",
            },
        )
        for index in range(4)
    ]

    assert store.upsert(documents) == 4
    matches = store.search("XAUUSD bullish trend setup", filters={"symbol": "XAUUSD"})
    assert matches
    assert matches[0]["payload"]["symbol"] == "XAUUSD"

    result = store.compact(filters={"symbol": "XAUUSD"}, max_documents=3, target_documents=2)

    assert result["compacted"] == 2
    assert store.count(filters={"symbol": "XAUUSD"}) == 3
    assert store.search("compacted trend memory", filters={"symbol": "XAUUSD"})


def test_agent_indexes_experience_into_vector_memory(tmp_path):
    csv_path = tmp_path / "candles.csv"
    csv_path.write_text(
        "time,open,high,low,close,spread_points,atr_points,trend_score,volatility_score,session\n"
        "2026-01-01T00:00:00+00:00,100,101,99,100.5,10,80,0.8,0.5,london\n",
        encoding="utf-8",
    )
    vector_memory = LocalSQLiteVectorMemoryStore(tmp_path / "vector.db", dimensions=16)
    agent = TradingAgent(
        planner=VariableDrivenPlanner(),
        risk_governor=RiskGovernor(),
        broker=PaperBrokerAdapter(),
        memory=JsonlExperienceStore(tmp_path / "experience.jsonl"),
        paper_portfolio=PaperPortfolio(tmp_path / "paper-state.json"),
        vector_memory=vector_memory,
    )

    ReplayRunner(agent, AccountState(10000, 10000, 9500)).run_csv(
        "XAUUSD",
        csv_path,
        constraints={"min_confidence": 0.65, "default_risk_percent": 0.25},
    )

    assert vector_memory.count(filters={"symbol": "XAUUSD"}) == 1
    matches = vector_memory.search("XAUUSD open position bullish", filters={"symbol": "XAUUSD"})
    assert matches[0]["payload"]["kind"] == "experience"
    assert matches[0]["payload"]["decision_id"]
