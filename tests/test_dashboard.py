import json

from quantz.dashboard import DashboardRenderer


def test_dashboard_renderer_writes_html(tmp_path):
    experiment = tmp_path / "experiment"
    experiment.mkdir()
    (experiment / "comparison.json").write_text(
        json.dumps({"verdict": "continue_paper_test", "metric_deltas": {"rejected_count": -10}, "notes": []}),
        encoding="utf-8",
    )
    (experiment / "review.json").write_text(
        json.dumps({"promotion_status": "paper_only", "risk_notes": [], "recommendations": []}),
        encoding="utf-8",
    )
    (experiment / "base-report.json").write_text(
        json.dumps({"total_r_multiple": 1.0, "by_symbol": {"XAUUSD": {"decisions": 1}}}),
        encoding="utf-8",
    )
    (experiment / "candidate-report.json").write_text(
        json.dumps({"total_r_multiple": 1.5, "by_symbol": {"XAUUSD": {"decisions": 1}}}),
        encoding="utf-8",
    )

    output = DashboardRenderer().render_experiment(experiment, tmp_path / "dashboard.html")

    html = output.read_text(encoding="utf-8")
    assert "Quantz Experiment Dashboard" in html
    assert "continue_paper_test" in html
    assert "XAUUSD" in html
