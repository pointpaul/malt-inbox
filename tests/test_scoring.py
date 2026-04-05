from __future__ import annotations

from malt_crm.models import AIWorkflowStatus
from malt_crm.scoring import conversation_smart_tier, conversation_strength, opportunity_strength


def test_smart_tier_hot_when_must_reply() -> None:
    t = conversation_smart_tier(
        effective_workflow=AIWorkflowStatus.A_REPONDRE.value,
        ai_urgency="low",
        ai_category="lead",
        ai_needs_reply=False,
        priority="medium",
        follow_up_due=False,
        max_linked_budget=None,
    )
    assert t["id"] == "hot"


def test_smart_tier_follow_when_waiting() -> None:
    t = conversation_smart_tier(
        effective_workflow=AIWorkflowStatus.ATTENTE_REPONSE.value,
        ai_urgency="low",
        ai_category="spam",
        ai_needs_reply=False,
        priority="low",
        follow_up_due=False,
        max_linked_budget=None,
    )
    assert t["id"] == "follow_up"


def test_conversation_strength_bounds() -> None:
    s = conversation_strength(
        effective_workflow=AIWorkflowStatus.A_REPONDRE.value,
        ai_urgency="high",
        ai_category="lead",
        ai_needs_reply=True,
        ai_confidence=0.9,
        max_linked_budget=12000,
        message_count=5,
    )
    assert 1 <= s["score"] <= 10
    assert "/10" in s["label"]
    assert s["explanation"]
    assert isinstance(s["why"], list) and s["why"]
    assert isinstance(s["suggested_actions"], list) and s["suggested_actions"]


def test_conversation_strength_relance_when_follow_up_due() -> None:
    s = conversation_strength(
        effective_workflow=AIWorkflowStatus.ATTENTE_REPONSE.value,
        ai_urgency="low",
        ai_category="spam",
        ai_needs_reply=False,
        ai_confidence=0.5,
        max_linked_budget=None,
        message_count=1,
        follow_up_due=True,
    )
    assert "Relancer maintenant" in s["suggested_actions"]


def test_opportunity_strength_uses_fit() -> None:
    class FakeOpp:
        ai_fit_score = 85.0
        budget = 5000.0
        ai_should_reply = True
        ai_confidence = 0.8

    s = opportunity_strength(FakeOpp())
    assert 1 <= s["score"] <= 10
    assert s["explanation"]
    assert isinstance(s["why"], list) and s["why"]
    assert isinstance(s["suggested_actions"], list) and s["suggested_actions"]
