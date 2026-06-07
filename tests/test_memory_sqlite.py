from quantz.memory import experience_store
from quantz.memory_sqlite import SQLiteExperienceStore


def experience_row(decision_id="decision-1", symbol="XAUUSD", risk_status="approved", outcome=None):
    return {
        "context": {
            "market": {
                "symbol": symbol,
                "bid": 100.0,
                "ask": 100.1,
                "timestamp": "2026-06-07T00:00:00+00:00",
            },
            "account": {"equity": 10000, "balance": 10000, "free_margin": 9000},
            "constraints": {},
        },
        "decision": {
            "decision_id": decision_id,
            "symbol": symbol,
            "timestamp": "2026-06-07T00:00:00+00:00",
            "action": "open_position",
            "side": "buy",
            "confidence": 0.72,
            "entry_price": 100.1,
            "stop_loss": 99.1,
            "take_profit": 101.8,
            "risk_percent": 0.25,
            "model_version": "test",
            "reason_codes": ["bullish_market_structure"],
            "metadata": {},
        },
        "risk": {
            "status": risk_status,
            "approved_lot": 0.01,
            "reasons": ["risk_checks_passed"] if risk_status == "approved" else ["spread_above_limit"],
        },
        "execution": {"accepted": risk_status == "approved", "message": "paper_order_filled"},
        "outcome": outcome,
    }


def test_sqlite_experience_store_round_trips_raw_rows(tmp_path):
    store = SQLiteExperienceStore(tmp_path / "memory.db")
    row = experience_row()

    store.append_raw(row)

    assert store.read_raw() == [row]


def test_sqlite_experience_store_context_summary_uses_symbol_index(tmp_path):
    store = SQLiteExperienceStore(tmp_path / "memory.db")
    store.append_raw(
        experience_row(
            outcome={
                "closed_positions": [
                    {
                        "position_id": "pos-1",
                        "symbol": "XAUUSD",
                        "side": "buy",
                        "r_multiple": 1.7,
                        "reason_codes": ["bullish_market_structure"],
                    }
                ]
            }
        )
    )
    store.append_raw(experience_row("decision-2", risk_status="rejected"))

    summary = store.context_summary("XAUUSD")

    assert summary["sample_size"] == 2
    assert summary["closed_trade_summary"]["average_r"] == 1.7
    assert summary["rejection_reasons"]["spread_above_limit"] == 1
    assert summary["reason_quality"]["bullish_market_structure"]["average_r"] == 1.7


def test_sqlite_experience_store_exposes_decision_lifecycle(tmp_path):
    store = SQLiteExperienceStore(tmp_path / "memory.db")
    store.append_raw(
        experience_row(
            outcome={
                "closed_positions": [
                    {
                        "position_id": "pos-1",
                        "symbol": "XAUUSD",
                        "side": "buy",
                        "entry_price": 100.1,
                        "exit_price": 101.8,
                        "r_multiple": 1.7,
                        "exit_reason": "take_profit",
                        "reason_codes": ["bullish_market_structure"],
                    }
                ]
            }
        )
    )

    lifecycle = store.decision_lifecycle("decision-1")

    assert lifecycle["found"] is True
    assert lifecycle["status"]["terminal"] is True
    assert lifecycle["status"]["outcome_count"] == 1
    assert lifecycle["outcomes"][0]["position_id"] == "pos-1"


def test_sqlite_experience_store_flags_accepted_execution_without_closed_outcome(tmp_path):
    store = SQLiteExperienceStore(tmp_path / "memory.db")
    store.append_raw(experience_row())

    lifecycle = store.decision_lifecycle("decision-1")

    assert lifecycle["found"] is True
    assert lifecycle["status"]["terminal"] is False
    assert "accepted_execution_without_closed_outcome" in lifecycle["status"]["gaps"]


def test_sqlite_experience_store_imports_jsonl(tmp_path):
    jsonl_path = tmp_path / "experience.jsonl"
    jsonl_path.write_text(
        '{"decision":{"decision_id":"decision-1","symbol":"XAUUSD","confidence":0.72,"reason_codes":[]},"context":{"market":{"symbol":"XAUUSD"},"account":{}},"risk":{"status":"approved","reasons":[]},"execution":null}\n',
        encoding="utf-8",
    )
    store = SQLiteExperienceStore(tmp_path / "memory.db")

    imported = store.import_jsonl(jsonl_path)

    assert imported == 1
    assert store.read_raw()[0]["decision"]["decision_id"] == "decision-1"


def test_experience_store_factory_selects_sqlite_for_db_paths(tmp_path):
    store = experience_store(tmp_path / "memory.db")

    assert isinstance(store, SQLiteExperienceStore)
