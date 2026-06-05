import json

from quantz.report import ReportBuilder


def test_report_builder_summarizes_symbol_outcomes(tmp_path):
    rows = [
        {
            "decision": {"symbol": "XAUUSD", "confidence": 0.8},
            "risk": {"status": "approved", "reasons": ["risk_checks_passed"]},
            "execution": {"accepted": True},
            "outcome": {"closed_positions": [{"position_id": "p1", "symbol": "XAUUSD", "r_multiple": 1.7}]},
        },
        {
            "decision": {"symbol": "XAUUSD", "confidence": 0.7},
            "risk": {"status": "rejected", "reasons": ["symbol_already_has_open_paper_position"]},
            "execution": None,
        },
        {
            "decision": {"symbol": "EURUSD", "confidence": 0.6},
            "risk": {"status": "rejected", "reasons": ["confidence_below_minimum"]},
            "execution": None,
        },
    ]
    paper_state = {
        "open_positions": [{"position_id": "p2", "symbol": "EURUSD"}],
        "closed_positions": [{"position_id": "p1", "symbol": "XAUUSD", "r_multiple": 1.7}],
    }
    paper_state_path = tmp_path / "paper-state.json"
    paper_state_path.write_text(json.dumps(paper_state), encoding="utf-8")

    report = ReportBuilder().build(rows, paper_state_path)

    assert report.sample_size == 3
    assert report.closed_position_count == 1
    assert report.win_rate == 1.0
    assert report.average_r_multiple == 1.7
    assert report.by_symbol["XAUUSD"].wins == 1
    assert report.by_symbol["EURUSD"].open_positions == 1
    assert report.rejection_reasons["confidence_below_minimum"] == 1
