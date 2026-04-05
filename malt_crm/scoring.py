"""Priorité « intelligente » et scores affichés dans le CRM (règles locales, pas d’appel IA)."""

from __future__ import annotations

from typing import Any

from .models import AIWorkflowStatus


def conversation_smart_tier(
    *,
    effective_workflow: str | None,
    ai_urgency: str | None,
    ai_category: str | None,
    ai_needs_reply: bool | None,
    priority: str,
    follow_up_due: bool,
    max_linked_budget: float | None,
) -> dict[str, Any]:
    """
    Tier business : hot / follow_up / low.

    Basé sur urgence IA, statut effectif, relance due, budget max lié, priorité CRM.
    """

    wf = (effective_workflow or "").strip()
    urg = (ai_urgency or "").strip().lower()
    cat = (ai_category or "").strip().lower()
    budget = float(max_linked_budget or 0)
    needs = bool(ai_needs_reply)
    pr = (priority or "").strip().lower()

    hot = False
    if wf == AIWorkflowStatus.A_REPONDRE.value:
        hot = True
    elif urg == "high":
        hot = True
    elif pr == "high" and needs:
        hot = True
    elif budget >= 8000 and needs:
        hot = True

    if hot:
        return {
            "id": "hot",
            "emoji": "🔴",
            "label": "Lead chaud",
            "hint": "Réponse ou relance prioritaire",
        }

    follow = False
    if wf == AIWorkflowStatus.ATTENTE_REPONSE.value:
        follow = True
    elif follow_up_due:
        follow = True
    elif urg == "medium":
        follow = True
    elif cat in ("relance", "projet_actif", "closing"):
        follow = True
    elif 2000 <= budget < 8000 and needs:
        follow = True

    if follow:
        return {
            "id": "follow_up",
            "emoji": "🟡",
            "label": "À relancer / suivre",
            "hint": "Suivi ou attente client",
        }

    return {
        "id": "low",
        "emoji": "🟢",
        "label": "Priorité basse",
        "hint": "Traiter quand tu peux",
    }


def conversation_strength(
    *,
    effective_workflow: str | None,
    ai_urgency: str | None,
    ai_category: str | None,
    ai_needs_reply: bool | None,
    ai_confidence: float | None,
    max_linked_budget: float | None,
    message_count: int,
) -> dict[str, Any]:
    """Score 1–10 + libellés type portfolio (règles locales, plafonnées)."""

    raw = 0.0
    wf = (effective_workflow or "").strip()
    urg = (ai_urgency or "").strip().lower()
    cat = (ai_category or "").strip().lower()
    budget = float(max_linked_budget or 0)
    conf = float(ai_confidence or 0.5)

    if wf == AIWorkflowStatus.A_REPONDRE.value:
        raw += 3.0
    elif wf == AIWorkflowStatus.ATTENTE_REPONSE.value:
        raw += 1.8

    if urg == "high":
        raw += 2.0
    elif urg == "medium":
        raw += 1.0

    if cat == "lead":
        raw += 1.6
    elif cat in ("projet_actif", "closing"):
        raw += 1.2
    elif cat == "relance":
        raw += 0.8

    if ai_needs_reply:
        raw += 1.0

    if budget >= 10000:
        raw += 2.0
    elif budget >= 5000:
        raw += 1.2
    elif budget >= 1500:
        raw += 0.5

    if message_count >= 4:
        raw += 0.4

    raw += min(1.0, conf)

    score = max(1, min(10, int(round(min(10.0, raw)))))
    if score >= 8:
        label = f"🔥 Opportunité forte ({score}/10)"
        short = "Forte"
    elif score >= 5:
        label = f"⚡ Potentiel correct ({score}/10)"
        short = "Correct"
    else:
        label = f"💤 Faible potentiel ({score}/10)"
        short = "Faible"

    return {"score": score, "label": label, "short_label": short}


def opportunity_strength(record: Any) -> dict[str, Any]:
    """Score 1–10 pour une opportunité (fit IA 0–100 + budget + intention de répondre)."""

    fit = float(record.ai_fit_score or 0)
    budget = float(record.budget or 0)
    should = record.ai_should_reply
    conf = float(record.ai_confidence or 0.5)

    # Fit IA : jusqu’à ~6 pts ; budget / réponse / conf complètent jusqu’à 10
    points = (fit / 100.0) * 6.0
    if budget >= 8000:
        points += 2.2
    elif budget >= 3000:
        points += 1.4
    elif budget > 0:
        points += 0.6
    if should is True:
        points += 1.2
    elif should is False:
        points -= 0.4
    points += min(1.0, conf)

    score = max(1, min(10, int(round(points))))
    if score >= 8:
        label = f"🔥 Opportunité forte ({score}/10)"
        short = "Forte"
    elif score >= 5:
        label = f"⚡ Bonne opportunité ({score}/10)"
        short = "Correct"
    else:
        label = f"💤 Faible potentiel ({score}/10)"
        short = "Faible"

    return {"score": score, "label": label, "short_label": short}
