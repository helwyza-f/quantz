from quantz.compare import RunComparator
from quantz.report import SessionReport


def report(**overrides):
    payload = {
        "sample_size": 30,
        "approved_count": 5,
        "rejected_count": 25,
        "executed_count": 5,
        "closed_position_count": 5,
        "open_position_count": 0,
        "win_count": 3,
        "loss_count": 2,
        "win_rate": 0.6,
        "average_confidence": 0.7,
        "average_r_multiple": 0.2,
        "total_r_multiple": 1.0,
        "rejection_reasons": {},
        "by_symbol": {},
        "candidate_constraints": {},
        "notes": [],
    }
    payload.update(overrides)
    return SessionReport(**payload)


def test_comparator_prefers_candidate_with_less_rejections_and_more_r():
    comparison = RunComparator().compare(
        report(rejected_count=25, total_r_multiple=1.0, average_r_multiple=0.2),
        report(rejected_count=10, total_r_multiple=2.0, average_r_multiple=0.4),
    )

    assert comparison.verdict == "candidate_preferred"
    assert comparison.metric_deltas["rejected_count"] == -15
    assert "candidate_reduced_rejections" in comparison.notes


def test_comparator_rejects_negative_average_r_candidate():
    comparison = RunComparator().compare(
        report(average_r_multiple=0.2, total_r_multiple=1.0),
        report(average_r_multiple=-0.1, total_r_multiple=-0.5),
    )

    assert comparison.verdict == "reject_candidate"
