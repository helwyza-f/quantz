from quantz.learning import ExperienceLearner


def test_learning_report_summarizes_experience_rows():
    rows = [
        {
            "decision": {"confidence": 0.62},
            "risk": {"status": "rejected", "reasons": ["confidence_below_minimum"]},
            "execution": None,
        },
        {
            "decision": {"confidence": 0.61},
            "risk": {"status": "rejected", "reasons": ["confidence_below_minimum"]},
            "execution": None,
        },
        {
            "decision": {"confidence": 0.63},
            "risk": {"status": "rejected", "reasons": ["confidence_below_minimum"]},
            "execution": None,
        },
    ]

    report = ExperienceLearner().analyze(rows)

    assert report.sample_size == 3
    assert report.rejected_count == 3
    assert report.closed_position_count == 0
    assert report.rejection_reasons["confidence_below_minimum"] == 3
    assert report.candidate_constraints["min_confidence"] == 0.59


def test_learning_report_reads_closed_position_outcomes():
    rows = [
        {
            "decision": {"confidence": 0.8},
            "risk": {"status": "approved", "reasons": ["risk_checks_passed"]},
            "execution": {"accepted": True},
            "outcome": {"closed_positions": [{"r_multiple": 1.7}]},
        },
        {
            "decision": {"confidence": 0.7},
            "risk": {"status": "approved", "reasons": ["risk_checks_passed"]},
            "execution": {"accepted": True},
            "outcome": {"closed_positions": [{"r_multiple": -1.0}]},
        },
    ]

    report = ExperienceLearner().analyze(rows)

    assert report.closed_position_count == 2
    assert report.average_r_multiple == 0.35
