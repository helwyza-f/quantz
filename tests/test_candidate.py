from quantz.candidate import CandidateConfigBuilder
from quantz.config import AgentSettings
from quantz.review import AgentReview, Recommendation


def test_candidate_config_applies_cooldown_and_keeps_paper_mode():
    base = AgentSettings(symbols=["XAUUSD", "EURUSD"], mode="live")
    review = AgentReview(
        promotion_status="paper_only",
        recommendations=[
            Recommendation(
                type="increase_monitor_interval_or_add_position_cooldown",
                target="global",
                change={"position_cooldown": "until_closed"},
                reason="duplicate scans",
            )
        ],
    )

    candidate = CandidateConfigBuilder().build(base, review)

    assert candidate.mode == "paper"
    assert candidate.position_cooldown == "until_closed"
    assert candidate.symbols == ["XAUUSD", "EURUSD"]


def test_candidate_config_can_disable_symbol():
    base = AgentSettings(symbols=["XAUUSD", "EURUSD"])
    review = AgentReview(
        promotion_status="paper_only",
        recommendations=[
            Recommendation(
                type="disable_symbol_candidate",
                target="EURUSD",
                change={"enabled": False},
                reason="negative average R",
            )
        ],
    )

    candidate = CandidateConfigBuilder().build(base, review)

    assert candidate.symbols == ["XAUUSD"]
    assert candidate.disabled_symbols == ["EURUSD"]
