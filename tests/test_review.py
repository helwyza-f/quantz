from quantz.report import SessionReport, SymbolReport
from quantz.review import ReviewEngine


def test_review_stays_paper_only_with_small_sample():
    report = SessionReport(
        sample_size=36,
        approved_count=4,
        rejected_count=32,
        executed_count=4,
        closed_position_count=1,
        open_position_count=3,
        win_count=1,
        loss_count=0,
        win_rate=1.0,
        average_confidence=0.77,
        average_r_multiple=1.7,
        total_r_multiple=1.7,
        rejection_reasons={"symbol_already_has_open_paper_position": 32},
        by_symbol={
            "XAUUSD": SymbolReport(
                symbol="XAUUSD",
                decisions=12,
                approved=2,
                rejected=10,
                executed=2,
                closed=1,
                wins=1,
                open_positions=1,
                average_confidence=0.81,
                average_r_multiple=1.7,
                total_r_multiple=1.7,
                rejection_reasons={"symbol_already_has_open_paper_position": 10},
            )
        },
        candidate_constraints={},
        notes=["positive_average_r_keep_collecting_outcomes"],
    )

    review = ReviewEngine().review(report)

    assert review.promotion_status == "paper_only"
    assert "not_enough_closed_trades_for_demo_promotion" in review.risk_notes
    assert any(item.type == "collect_more_paper_data" for item in review.recommendations)
    assert any(item.type == "reduce_duplicate_scans" for item in review.recommendations)


def test_review_marks_positive_large_sample_as_demo_candidate():
    report = SessionReport(
        sample_size=120,
        approved_count=45,
        rejected_count=75,
        executed_count=45,
        closed_position_count=25,
        open_position_count=0,
        win_count=15,
        loss_count=10,
        win_rate=0.6,
        average_confidence=0.72,
        average_r_multiple=0.55,
        total_r_multiple=13.75,
        rejection_reasons={},
        by_symbol={},
        candidate_constraints={},
        notes=[],
    )

    review = ReviewEngine().review(report)

    assert review.promotion_status == "demo_candidate"
