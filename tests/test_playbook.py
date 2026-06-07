import json

from quantz.market import DemoAccountFeed, DemoMarketFeed
from quantz.models import AgentContext, TradeAction
from quantz.planner import VariableDrivenPlanner
from quantz.playbook import Playbook, PlaybookLoader, PlaybookSelector


def context(symbol="XAUUSD"):
    return AgentContext(
        market=DemoMarketFeed().snapshot(symbol),
        account=DemoAccountFeed().state(),
        constraints={"memory_context": {"market_regime": "trend"}},
    )


def test_playbook_loader_reads_json_file(tmp_path):
    path = tmp_path / "playbook.json"
    path.write_text(
        json.dumps(
            {
                "name": "xau_test",
                "symbols": ["XAUUSD"],
                "sessions": ["london_new_york_overlap"],
                "allowed_regimes": ["trend"],
                "entry_conditions": {"trend_score_min": 0.5},
                "risk": {"risk_percent": 0.2, "sl_atr": 1.2, "tp_r": 2.0},
                "status": "paper_allowed",
            }
        ),
        encoding="utf-8",
    )

    playbook = PlaybookLoader().load_file(path)

    assert playbook.name == "xau_test"
    assert playbook.symbols == ["XAUUSD"]
    assert playbook.risk["tp_r"] == 2.0


def test_playbook_selector_allows_matching_paper_playbook():
    playbook = Playbook(
        name="xau_test",
        symbols=["XAUUSD"],
        sessions=["london_new_york_overlap"],
        allowed_regimes=["trend"],
        entry_conditions={"trend_score_min": 0.5, "spread_max": 90},
        status="paper_allowed",
    )

    selection = PlaybookSelector([playbook]).select(context(), mode="paper")

    assert selection.allowed is True
    assert selection.playbook == playbook


def test_playbook_selector_blocks_live_when_not_promoted():
    playbook = Playbook(name="xau_test", symbols=["XAUUSD"], status="paper_allowed")

    selection = PlaybookSelector([playbook]).select(context(), mode="live")

    assert selection.allowed is False
    assert "xau_test:status_not_allowed_for_live" in selection.reasons


def test_planner_holds_when_selected_playbook_is_blocked():
    base = context()
    blocked = AgentContext(
        base.market,
        base.account,
        {
            **base.constraints,
            "playbook": {
                "selected": None,
                "status": "blocked",
                "reasons": ["xau_test:spread_above_playbook_maximum"],
            },
        },
    )

    decision = VariableDrivenPlanner().decide(blocked)

    assert decision.action == TradeAction.HOLD
    assert "xau_test:spread_above_playbook_maximum" in decision.reason_codes


def test_planner_uses_playbook_risk_settings():
    base = context()
    enriched = AgentContext(
        base.market,
        base.account,
        {
            **base.constraints,
            "playbook": {
                "selected": {
                    "name": "xau_test",
                    "risk": {"risk_percent": 0.12, "sl_atr": 1.0, "tp_r": 2.0},
                },
                "status": "allowed",
                "reasons": ["playbook_selected"],
            },
        },
    )

    decision = VariableDrivenPlanner().decide(enriched)

    assert decision.action == TradeAction.OPEN_POSITION
    assert decision.risk_percent == 0.12
    assert decision.metadata["playbook"]["selected"]["name"] == "xau_test"
