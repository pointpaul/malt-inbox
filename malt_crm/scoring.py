"""Priorité « intelligente » et scores affichés dans le CRM (règles locales, pas d’appel IA)."""

from __future__ import annotations

from typing import Any

from .models import AIWorkflowStatus

SCORE_EXPLANATION_CONVERSATION = (
    "Score basé sur le budget des offres liées, l’urgence et le statut de la discussion, "
    "la réactivité (messages, besoin de répondre) et la confiance du classement IA."
)

SCORE_EXPLANATION_OPPORTUNITY = (
    "Score basé sur l’adéquation projet / profil (IA), le budget annoncé, "
    "l’intention de répondre et la confiance du modèle."
)


def _unique_preserve(items: list[str], *, max_n: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
        if len(out) >= max_n:
            break
    return out


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

    # Attente client « normale » (pas encore en fenêtre de relance automatique).
    if wf == AIWorkflowStatus.ATTENTE_REPONSE.value and not follow_up_due:
        return {
            "id": "waiting_client",
            "emoji": "🔵",
            "label": "Attente réponse client",
            "hint": "Dernier envoi effectué ; le prochain pas est côté client.",
        }

    follow = False
    if wf == AIWorkflowStatus.ATTENTE_REPONSE.value and follow_up_due:
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
    follow_up_due: bool = False,
) -> dict[str, Any]:
    """Score 1–10 + libellés type portfolio (règles locales, plafonnées)."""

    raw = 0.0
    wf = (effective_workflow or "").strip()
    urg = (ai_urgency or "").strip().lower()
    cat = (ai_category or "").strip().lower()
    budget = float(max_linked_budget or 0)
    conf = float(ai_confidence or 0.5)
    needs = bool(ai_needs_reply)

    why: list[str] = []

    if wf == AIWorkflowStatus.A_REPONDRE.value:
        raw += 3.0
        why.append("Statut : une réponse de ta part est attendue")
    elif wf == AIWorkflowStatus.ATTENTE_REPONSE.value:
        raw += 1.8
        why.append("En attente d’un retour client")

    if urg == "high":
        raw += 2.0
        why.append("Urgence détectée : élevée")
    elif urg == "medium":
        raw += 1.0
        why.append("Urgence : moyenne")

    if cat == "lead":
        raw += 1.6
        why.append("Besoin typé « lead » — intention claire")
    elif cat in ("projet_actif", "closing"):
        raw += 1.2
        why.append("Projet actif ou phase de closing")
    elif cat == "relance":
        raw += 0.8
        why.append("Contexte de relance ou suivi")

    if needs:
        raw += 1.0
        why.append("Le fil suggère qu’une réponse est utile")

    if budget >= 10000:
        raw += 2.0
        why.append("Budget lié élevé (≥ 10 k€)")
    elif budget >= 5000:
        raw += 1.2
        why.append("Budget lié notable (≥ 5 k€)")
    elif budget >= 1500:
        raw += 0.5
        why.append("Budget ou enveloppe mentionné sur une offre liée")

    if message_count >= 4:
        raw += 0.4
        why.append("Plusieurs messages — échange déjà engagé")

    raw += min(1.0, conf)
    if conf >= 0.72:
        why.append("Confiance élevée sur l’analyse IA")

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

    actions: list[str] = []
    if follow_up_due:
        actions.append("Relancer maintenant")
    if wf == AIWorkflowStatus.A_REPONDRE.value:
        actions.append("Répondre dans la foulée")
    if wf == AIWorkflowStatus.ATTENTE_REPONSE.value and budget >= 5000:
        actions.append("Proposer un call de synchro")
    if urg == "high" and wf != AIWorkflowStatus.A_REPONDRE.value:
        actions.append("Prioriser une réponse courte")
    if needs and wf == AIWorkflowStatus.A_REPONDRE.value:
        actions.append("Structurer offre / prochaine étape dans la réponse")

    if not why:
        why.append("Peu de signaux forts — à arbitrer à la lecture du fil")

    return {
        "score": score,
        "label": label,
        "short_label": short,
        "explanation": SCORE_EXPLANATION_CONVERSATION,
        "why": _unique_preserve(why, max_n=8),
        "suggested_actions": _unique_preserve(actions, max_n=4)
        or ["Parcourir le fil et ajuster le statut si besoin"],
    }


def opportunity_strength(record: Any) -> dict[str, Any]:
    """Score 1–10 pour une opportunité (fit IA 0–100 + budget + intention de répondre)."""

    fit = float(record.ai_fit_score or 0)
    budget = float(record.budget or 0)
    should = record.ai_should_reply
    conf = float(record.ai_confidence or 0.5)

    why: list[str] = []

    # Fit IA : jusqu’à ~6 pts ; budget / réponse / conf complètent jusqu’à 10
    points = (fit / 100.0) * 6.0
    if fit >= 72:
        why.append("Adéquation forte avec ton profil (score IA)")
    elif fit >= 48:
        why.append("Adéquation correcte avec le besoin exprimé")
    elif fit > 0:
        why.append("Score d’adéquation IA modéré")

    if budget >= 8000:
        points += 2.2
        why.append("Budget annoncé significatif")
    elif budget >= 3000:
        points += 1.4
        why.append("Budget renseigné — marge de négociation à clarifier")
    elif budget > 0:
        points += 0.6
        why.append("Montant ou enveloppe budgétaire mentionné")

    if should is True:
        points += 1.2
        why.append("L’IA recommande de répondre rapidement")
    elif should is False:
        points -= 0.4
        why.append("Peu urgent côté réponse immédiate (selon l’analyse)")

    points += min(1.0, conf)
    if conf >= 0.72:
        why.append("Confiance élevée sur l’analyse IA")

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

    actions: list[str] = []
    if should is True:
        actions.append("Répondre sur Malt maintenant")
    if fit >= 65:
        actions.append("Proposer un appel de découverte")
    if budget >= 5000:
        actions.append("Clarifier périmètre, budget et calendrier")
    if should is not True and fit >= 55:
        actions.append("Qualifier le besoin avant d’engager du temps")

    if not why:
        why.append("Peu de signaux automatiques — à juger à la lecture de l’offre")

    return {
        "score": score,
        "label": label,
        "short_label": short,
        "explanation": SCORE_EXPLANATION_OPPORTUNITY,
        "why": _unique_preserve(why, max_n=8),
        "suggested_actions": _unique_preserve(actions, max_n=4)
        or ["Relire l’offre et ajuster ton angle"],
    }
