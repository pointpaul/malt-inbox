"""OpenAI-powered conversation and opportunity analysis."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping, Sequence

import requests

from .models import (
    AICategory,
    AIUrgency,
    AIWorkflowStatus,
    ConversationAIAnalysis,
    MaltProfileSnapshot,
    OpportunityAIAnalysis,
)

OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
FOLLOW_UP_DELAY_DAYS = 4


class OpenAIError(RuntimeError):
    """Raised when the OpenAI API request fails."""


@dataclass(frozen=True)
class OpenAISettings:
    """Runtime configuration for OpenAI-backed enrichment."""

    api_key: str
    model: str = DEFAULT_OPENAI_MODEL
    base_url: str = OPENAI_BASE_URL
    timeout: float = 45.0

    @classmethod
    def from_env(cls) -> "OpenAISettings | None":
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            return None
        model = os.getenv("MALT_CRM_OPENAI_MODEL", DEFAULT_OPENAI_MODEL).strip() or DEFAULT_OPENAI_MODEL
        return cls(api_key=api_key, model=model)


@dataclass(frozen=True)
class FreelancerProfile:
    """Context injected into AI prompts to personalize answers."""

    name: str
    title: str | None = None
    tagline: str | None = None
    skills: str | None = None
    experience: str | None = None
    missions: str | None = None
    preferences: str | None = None

    @classmethod
    def fallback(cls) -> "FreelancerProfile":
        return cls(name="Le freelance")

    @classmethod
    def from_snapshot(cls, snapshot: MaltProfileSnapshot | None) -> "FreelancerProfile":
        if snapshot is None:
            return cls.fallback()
        fallback = cls.fallback()
        skills = ", ".join(snapshot.skills[:12]) if snapshot.skills else fallback.skills
        missions = "\n".join(f"- {item}" for item in snapshot.missions[:5]) if snapshot.missions else fallback.missions
        preferences = fallback.preferences
        if snapshot.daily_rate:
            extra = f"TJM indicatif Malt: {int(snapshot.daily_rate)} EUR."
            preferences = f"{preferences}\n{extra}".strip() if preferences else extra
        return cls(
            name=snapshot.full_name or fallback.name,
            title=snapshot.headline or fallback.title,
            tagline=snapshot.summary or fallback.tagline,
            skills=skills,
            experience=snapshot.summary or fallback.experience,
            missions=missions,
            preferences=preferences,
        )

    def to_prompt_block(self) -> str:
        lines = [f"Nom: {self.name}"]
        if self.title:
            lines.append(f"Titre: {self.title}")
        if self.tagline:
            lines.append(f"Positionnement: {self.tagline}")
        if self.skills:
            lines.append(f"Compétences: {self.skills}")
        if self.experience:
            lines.append(f"Expérience: {self.experience}")
        if self.missions:
            lines.append(f"Missions pertinentes: {self.missions}")
        if self.preferences:
            lines.append(f"Préférences: {self.preferences}")
        return "\n".join(lines)


def _extract_json_object(content: str) -> dict[str, object]:
    text = content.strip()
    if not text:
        raise OpenAIError("OpenAI returned an empty payload")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise OpenAIError("OpenAI did not return valid JSON")
        payload = json.loads(text[start : end + 1])

    if not isinstance(payload, dict):
        raise OpenAIError("OpenAI returned a non-object JSON payload")
    return payload


def _normalize_person_name(value: str) -> str:
    return " ".join(value.lower().strip().split())


def _name_tokens(value: str) -> tuple[str, ...]:
    normalized = _normalize_person_name(value)
    if not normalized:
        return ()
    return tuple(token for token in re.split(r"[^a-z0-9]+", normalized) if token)


def _person_match_score(candidate: str, reference: str) -> int:
    candidate_normalized = _normalize_person_name(candidate)
    reference_normalized = _normalize_person_name(reference)
    if not candidate_normalized or not reference_normalized:
        return 0
    if candidate_normalized == reference_normalized:
        return 100

    candidate_tokens = set(_name_tokens(candidate))
    reference_tokens = set(_name_tokens(reference))
    if not candidate_tokens or not reference_tokens:
        return 0
    if candidate_tokens == reference_tokens:
        return 90

    overlap = candidate_tokens & reference_tokens
    if not overlap:
        return 0
    if candidate_tokens.issubset(reference_tokens) or reference_tokens.issubset(candidate_tokens):
        return 80
    if len(overlap) >= 2:
        return 60
    return 0


def _message_role(
    *,
    sender: str,
    client_name: str,
    freelancer_name: str | None = None,
) -> str:
    client_score = _person_match_score(sender, client_name)
    freelancer_score = _person_match_score(sender, freelancer_name or "")
    if client_score == freelancer_score:
        return "unknown"
    if client_score > freelancer_score:
        return "client"
    if freelancer_score > 0:
        return "toi"
    return "unknown"


def _stringify_messages(
    messages: Sequence[Mapping[str, object]],
    *,
    client_name: str,
    freelancer_name: str | None,
    limit: int = 10,
) -> str:
    lines: list[str] = []
    for message in messages[-limit:]:
        sender = str(message.get("sender") or "Unknown")
        content = str(message.get("content") or "").strip()
        created_at = str(message.get("created_at") or "")
        if not content:
            continue
        role = _message_role(
            sender=sender,
            client_name=client_name,
            freelancer_name=freelancer_name,
        )
        lines.append(f"[{created_at}] role={role} sender={sender}: {content}")
    return "\n".join(lines) or "Aucun message."


def _stringify_opportunities(opportunities: Iterable[Mapping[str, object]]) -> str:
    rows: list[str] = []
    for item in opportunities:
        title = str(item.get("title") or "Sans titre").strip()
        budget = item.get("budget")
        description = str(item.get("description") or "").strip()
        rows.append(f"- {title} | budget={budget} | {description[:180]}")
    return "\n".join(rows) or "Aucune opportunité liée."


def _conversation_tone_signals(messages: Sequence[Mapping[str, object]]) -> str:
    joined = "\n".join(str(message.get("content") or "") for message in messages[-8:])
    lowered = joined.lower()
    informal_markers = [
        "tu ",
        "t'es",
        "te ",
        "ton ",
        "ta ",
        "on en parle",
        "hello",
        "salut",
        "ok ?",
        "🙂",
        "😉",
    ]
    formal_markers = [
        "vous ",
        "votre ",
        "vos ",
        "bonjour",
        "cordialement",
        "seriez-vous",
        "souhaitez-vous",
    ]
    informal_score = sum(marker in lowered for marker in informal_markers)
    formal_score = sum(marker in lowered for marker in formal_markers)
    if informal_score > formal_score:
        return "conversation plutôt informelle, tutoiement ou ton direct probable"
    if formal_score > informal_score:
        return "conversation plutôt formelle, vouvoiement probable"
    return "ton neutre à conserver"


def _days_since(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    return max(0, int((datetime.now(tz=timezone.utc) - dt).total_seconds() // 86400))


class OpenAIConversationAnalyzer:
    """Analyze a Malt conversation and propose a reply draft."""

    def __init__(self, settings: OpenAISettings, profile: FreelancerProfile | None = None) -> None:
        self.settings = settings
        self.profile = profile or FreelancerProfile.fallback()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {settings.api_key}",
                "Content-Type": "application/json",
            }
        )

    def analyze(
        self,
        *,
        conversation: Mapping[str, object],
        messages: Sequence[Mapping[str, object]],
        opportunities: Sequence[Mapping[str, object]],
    ) -> ConversationAIAnalysis:
        """Return one AI analysis for a conversation thread."""

        last_role, last_content, last_created_at, days_since_last_message = _last_message_context(
            conversation=conversation,
            messages=messages,
        )
        follow_up_due = (
            last_role == "toi"
            and days_since_last_message is not None
            and days_since_last_message >= FOLLOW_UP_DELAY_DAYS
        )
        tone_signals = _conversation_tone_signals(messages)

        system_prompt = (
            "Tu es un freelance expérimenté qui répond à des leads sur Malt. "
            "Tu agis comme le freelance lui-même, pas comme un assistant externe. "
            "Ton objectif n'est pas juste de répondre, mais de qualifier rapidement, inspirer confiance, "
            "créer une dynamique de discussion et maximiser la probabilité d'un call ou d'une prochaine étape utile. "
            "Analyse la conversation et retourne uniquement un JSON valide. "
            "Important: dans l'historique des messages, role=toi signifie message envoyé par le freelance, "
            "role=client signifie message envoyé par le client. "
            "role=unknown signifie que l'auteur n'a pas pu être identifié avec certitude. "
            "Ne confonds jamais qui relance qui. "
            "Tu dois aussi classifier le statut de workflow dans une seule valeur parmi: "
            "a_repondre, attente_reponse, repondu, clos. "
            "Tu dois classifier la conversation dans une seule catégorie parmi: "
            "lead, relance, projet_actif, closing, support, spam. "
            "Tu dois aussi évaluer l'urgence parmi: low, medium, high. "
            "summary doit faire une phrase courte, concrète, en français. "
            "next_action doit être une action courte, en français, orientée business. "
            "needs_reply vaut true seulement si le prochain message doit probablement venir du freelance. "
            "Mets needs_reply à false si le client dit qu'il réfléchit, si on attend son retour, "
            "si le dernier message est purement informatif, si le freelance a déjà répondu, "
            "ou si la balle est clairement dans le camp du client. "
            "suggested_reply doit être null si aucune réponse n'est utile. "
            "Si une réponse est utile, suggested_reply doit être excellente, professionnelle, concise, en français, prête à être envoyée. "
            "Le ton doit être humain, fluide, sûr, légèrement commercial mais jamais pushy, naturel, concret et orienté vers la prochaine étape. "
            "La réponse ne doit jamais sonner IA, jamais être générique, jamais être du remplissage. "
            "L'objectif est de faire avancer la mission ou de closer un échange, pas de faire un discours. "
            "Tu identifies implicitement si le lead est chaud, tiède ou froid, et si le besoin est clair ou flou. "
            "Adapte alors la réponse: chaud -> direct + call rapide; flou -> rassurer + cadrer; froid -> relance soft + créer de l'intérêt. "
            "La réponse doit d'abord traiter l'intention réelle du dernier message client: question, relance, demande de dispo, demande de devis, besoin de précision. "
            "Réponds d'abord au point principal, puis fais avancer l'échange. "
            "Quand c'est pertinent, montre rapidement le fit en une idée concrète liée au besoin, pas une auto-présentation générique. "
            "Quand c'est pertinent, propose explicitement une seule prochaine étape simple: échange court, précision sur le besoin, cadrage, créneau. "
            "Une bonne réponse contient en général: 1) accroche courte liée au contexte, 2) preuve de compréhension ou de fit, 3) prochaine étape claire. "
            "La réponse finale doit faire 4 à 7 lignes maximum, sans bullet points, sans phrases longues. "
            "Tu peux poser au maximum UNE question pertinente. "
            "Tu peux proposer une approche rapidement. "
            "Tu peux mentionner une expérience similaire seulement si elle renforce vraiment la crédibilité et si elle est cohérente avec le profil. "
            "Vise en général 45 à 110 mots, sauf si une relance doit être plus courte. "
            "Adapte impérativement le ton à la discussion existante: "
            "si le client tutoie ou a un ton direct/cool, garde un ton naturel du même niveau sans forcer; "
            "si le fil est formel, reste en vouvoiement propre; "
            "ne change pas brutalement de registre. "
            "Le tutoiement/vouvoiement doit suivre les signaux du fil, pas ton habitude. "
            "N'invente jamais de disponibilité précise, de tarif, de référence client ou de compétence absente du profil. "
            "N'invente jamais qu'un problème est résolu, qu'une solution a été trouvée, ou que le client a validé quelque chose si ce n'est pas écrit. "
            "Ne confonds jamais relance du freelance et relance du client. "
            "Évite les formules creuses du type 'je reste à votre disposition', 'au plaisir', 'n'hésitez pas', 'ravie d'échanger', 'ce serait un plaisir'. "
            "Évite aussi les phrases génériques comme 'votre projet correspond à mon profil' sans reformulation utile du besoin. "
            "Évite les listes, les pavés, les emojis, les compliments gratuits, les majuscules agressives et les tournures trop corporate. "
            "Pour une relance, sois encore plus court: un rappel poli, une disponibilité intacte, une ouverture simple. "
            "Si le dernier message vient du freelance et qu'il n'y a pas de retour client depuis plusieurs jours, "
            "considère qu'une relance courte peut être utile et classe alors le dossier en a_repondre."
        )
        user_prompt = (
            f"Profil freelance:\n{self.profile.to_prompt_block()}\n\n"
            f"Freelance: {conversation.get('freelancer_name') or self.profile.name}\n"
            f"Client: {conversation.get('client_name')}\n"
            f"Statut CRM actuel: {conversation.get('status')}\n"
            f"Priorité CRM actuelle: {conversation.get('priority')}\n"
            f"Dernier message: {conversation.get('last_message')}\n\n"
            f"Dernier rôle détecté: {last_role}\n"
            f"Dernier contenu détecté: {last_content}\n"
            f"Signaux de ton: {tone_signals}\n"
            f"Jours depuis le dernier message: {days_since_last_message}\n"
            f"Relance potentiellement due (>= {FOLLOW_UP_DELAY_DAYS} jours): {follow_up_due}\n\n"
            f"Messages:\n{_stringify_messages(messages, client_name=str(conversation.get('client_name') or ''), freelancer_name=(str(conversation.get('freelancer_name')) if conversation.get('freelancer_name') else None))}\n\n"
            f"Opportunités liées:\n{_stringify_opportunities(opportunities)}\n\n"
            "Priorité de rédaction:\n"
            "1. Comprendre qui doit parler ensuite.\n"
            "2. Si une réponse est utile, répondre au dernier besoin explicite du client.\n"
            "3. Faire avancer l'échange avec une prochaine étape claire.\n"
            "4. Rester crédible, sobre et spécifique.\n\n"
            "Si tu rédiges une réponse, écris-la comme si tu étais le freelance lui-même.\n\n"
            "Retourne strictement ce JSON:\n"
            "{"
            '"workflow_status":"a_repondre|attente_reponse|repondu|clos",'
            '"category":"lead|relance|projet_actif|closing|support|spam",'
            '"urgency":"low|medium|high",'
            '"needs_reply":true,'
            '"summary":"...",'
            '"next_action":"...",'
            '"suggested_reply":"..." ou null,'
            '"confidence":0.0'
            "}"
        )

        payload = {
            "model": self.settings.model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        response = self.session.post(
            f"{self.settings.base_url}/chat/completions",
            json=payload,
            timeout=self.settings.timeout,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise OpenAIError(f"OpenAI request failed with status {response.status_code}: {response.text[:500]}") from exc

        raw = response.json()
        content = (
            raw.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        data = _extract_json_object(str(content))
        category = _normalize_category(data.get("category", AICategory.LEAD.value))
        suggested_reply = (
            str(data.get("suggested_reply")).strip()
            if data.get("suggested_reply") not in (None, "", "null")
            else None
        )
        next_action = str(data.get("next_action", "")).strip() or "Vérifier manuellement."
        needs_reply = bool(data.get("needs_reply", False))
        if suggested_reply is None:
            needs_reply = False
        if next_action.lower().startswith("attendre"):
            needs_reply = False
        workflow_status = derive_workflow_status(
            conversation=conversation,
            messages=messages,
            category=category,
            fallback=_normalize_workflow_status(data.get("workflow_status")),
            suggested_reply=suggested_reply,
            next_action=next_action,
        )
        if follow_up_due and workflow_status == AIWorkflowStatus.ATTENTE_REPONSE:
            workflow_status = AIWorkflowStatus.A_REPONDRE
            needs_reply = True
            next_action = "Relancer le client."
            if suggested_reply is None:
                suggested_reply = self._build_follow_up_reply(
                    client_name=str(conversation.get("client_name") or "").strip(),
                )
        if workflow_status in {AIWorkflowStatus.CLOS, AIWorkflowStatus.ATTENTE_REPONSE, AIWorkflowStatus.REPONDU}:
            needs_reply = False
        if workflow_status == AIWorkflowStatus.ATTENTE_REPONSE:
            suggested_reply = None
            next_action = "Attendre le retour du client."
            if summary_mentions_client_relaunch(str(data.get("summary", ""))):
                summary = "Tu as relancé le client. En attente de retour."
            else:
                summary = str(data.get("summary", "")).strip() or "En attente du retour du client."
        elif workflow_status == AIWorkflowStatus.A_REPONDRE:
            if follow_up_due and last_role == "toi":
                summary = f"Aucun retour depuis {days_since_last_message} jours. Une relance courte est prête."
            else:
                summary = str(data.get("summary", "")).strip() or "Le client attend une réponse."
        else:
            summary = str(data.get("summary", "")).strip() or "Pas de résumé IA."

        return ConversationAIAnalysis(
            workflow_status=workflow_status,
            category=category,
            urgency=_normalize_urgency(data.get("urgency", AIUrgency.MEDIUM.value)),
            needs_reply=needs_reply,
            summary=summary,
            next_action=next_action,
            suggested_reply=suggested_reply,
            confidence=_coerce_confidence(data.get("confidence")),
        )

    def _build_follow_up_reply(self, *, client_name: str) -> str:
        greeting = f"Bonjour {client_name}," if client_name else "Bonjour,"
        return (
            f"{greeting} je me permets de vous relancer au sujet du projet. "
            "Je suis toujours disponible pour avancer dessus si c'est d'actualité de votre côté. "
            "Dites-moi si vous souhaitez que l'on en parle ou si vous avez besoin d'un point rapide."
        )

    def analyze_opportunity(
        self,
        *,
        opportunity: Mapping[str, object],
    ) -> OpportunityAIAnalysis:
        """Return one AI qualification and first reply draft for an opportunity."""

        system_prompt = (
            "Tu es un assistant commercial pour un freelance sur Malt. "
            "Tu dois décider si une opportunité correspond vraiment au profil du freelance. "
            "Réponds uniquement avec un JSON valide. "
            "fit_label doit être une seule valeur parmi: bon_match, a_verifier, hors_scope. "
            "fit_score doit être un entier entre 0 et 100. "
            "should_reply vaut true seulement si le projet semble cohérent avec le profil. "
            "summary doit être une phrase courte, concrète, orientée business. "
            "suggested_reply doit être null si le projet est hors scope ou trop ambigu. "
            "Si should_reply vaut true, suggested_reply doit être une première réponse courte en français, "
            "professionnelle, positive, prête à être collée pour accepter l'échange et proposer la suite. "
            "Le message doit donner envie de continuer avec le freelance, sans sonner artificiel. "
            "Il doit montrer rapidement le fit avec le besoin, reformuler en une phrase utile si nécessaire, "
            "et proposer une prochaine étape simple pour avancer. "
            "Évite tout ton trop commercial, toute flatterie, tout bla-bla, toute formule creuse. "
            "Pas de promesse exagérée, pas d'informations inventées, pas de tarif inventé. "
            "Vise 3 à 5 phrases maximum, avec un style fluide, crédible et orienté conversion."
        )
        user_prompt = (
            f"Profil freelance:\n{self.profile.to_prompt_block()}\n\n"
            f"Titre opportunité: {opportunity.get('title') or 'Sans titre'}\n"
            f"Budget: {opportunity.get('budget')}\n"
            f"Description:\n{opportunity.get('description') or 'Aucune description'}\n\n"
            "Retourne strictement ce JSON:\n"
            "{"
            '"fit_label":"bon_match|a_verifier|hors_scope",'
            '"fit_score":0,'
            '"summary":"...",'
            '"should_reply":true,'
            '"suggested_reply":"..." ou null,'
            '"confidence":0.0'
            "}"
        )
        payload = {
            "model": self.settings.model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        response = self.session.post(
            f"{self.settings.base_url}/chat/completions",
            json=payload,
            timeout=self.settings.timeout,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise OpenAIError(f"OpenAI request failed with status {response.status_code}: {response.text[:500]}") from exc

        raw = response.json()
        content = (
            raw.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        data = _extract_json_object(str(content))
        fit_label = _normalize_fit_label(data.get("fit_label"))
        should_reply = bool(data.get("should_reply", False))
        suggested_reply = (
            str(data.get("suggested_reply")).strip()
            if data.get("suggested_reply") not in (None, "", "null")
            else None
        )
        if fit_label == "hors_scope":
            should_reply = False
            suggested_reply = None
        if not should_reply:
            suggested_reply = None

        return OpportunityAIAnalysis(
            fit_label=fit_label,
            fit_score=_coerce_score(data.get("fit_score")),
            summary=str(data.get("summary", "")).strip() or "Qualification IA indisponible.",
            should_reply=should_reply,
            suggested_reply=suggested_reply,
            confidence=_coerce_confidence(data.get("confidence")),
        )


def _coerce_confidence(value: object) -> float | None:
    if value is None:
        return None
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, confidence))


def _coerce_score(value: object) -> int:
    try:
        score = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, score))


def _normalize_fit_label(value: object) -> str:
    normalized = str(value or "").strip().lower().replace(" ", "_")
    aliases = {
        "good_fit": "bon_match",
        "match": "bon_match",
        "maybe": "a_verifier",
        "to_check": "a_verifier",
        "poor_fit": "hors_scope",
        "no_fit": "hors_scope",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"bon_match", "a_verifier", "hors_scope"}:
        return "a_verifier"
    return normalized


def summary_mentions_client_relaunch(value: str) -> bool:
    normalized = value.lower()
    patterns = [
        "le client relance",
        "client relance",
        "le client attend une réponse",
        "client attend une réponse",
        "répondre au client",
    ]
    return any(pattern in normalized for pattern in patterns)


def client_reply_pending_signal(value: str) -> bool:
    normalized = value.lower()
    patterns = [
        "je réfléch",
        "je regarde",
        "je compare",
        "je reviens vers vous",
        "je vous reviens",
        "je vous tiens au courant",
        "je vous fais un retour",
        "je te redis",
        "je vous redis",
        "je reviendrai",
        "je revient vers vous",
        "je reviens vers toi",
        "je vous dirai",
        "je valide et je reviens",
        "je vais revenir vers vous",
    ]
    return any(pattern in normalized for pattern in patterns)


def client_requires_answer_signal(value: str) -> bool:
    normalized = value.lower()
    patterns = [
        "?",
        "si t'es toujours dispo",
        "si tu es toujours dispo",
        "si vous êtes disponible",
        "si vous etes disponible",
        "on en parle",
        "on peut en parler",
        "souhaite discuter",
        "on échange",
        "on echange",
        "dis-moi",
        "dites-moi",
        "asap",
    ]
    return any(pattern in normalized for pattern in patterns)


def _normalize_category(value: object) -> AICategory:
    normalized = str(value or "").strip().lower().replace(" ", "_")
    aliases = {
        "projet-actif": AICategory.PROJET_ACTIF.value,
        "project_active": AICategory.PROJET_ACTIF.value,
        "active_project": AICategory.PROJET_ACTIF.value,
        "follow_up": AICategory.RELANCE.value,
    }
    normalized = aliases.get(normalized, normalized)
    try:
        return AICategory(normalized)
    except ValueError:
        return AICategory.LEAD


def _normalize_urgency(value: object) -> AIUrgency:
    normalized = str(value or "").strip().lower()
    if normalized in {"urgent", "haute", "high"}:
        return AIUrgency.HIGH
    if normalized in {"basse", "low"}:
        return AIUrgency.LOW
    return AIUrgency.MEDIUM


def _normalize_workflow_status(value: object) -> AIWorkflowStatus:
    normalized = str(value or "").strip().lower().replace(" ", "_")
    aliases = {
        "a-répondre": AIWorkflowStatus.A_REPONDRE.value,
        "a_repondre": AIWorkflowStatus.A_REPONDRE.value,
        "à_répondre": AIWorkflowStatus.A_REPONDRE.value,
        "to_reply": AIWorkflowStatus.A_REPONDRE.value,
        "waiting_reply": AIWorkflowStatus.ATTENTE_REPONSE.value,
        "attente": AIWorkflowStatus.ATTENTE_REPONSE.value,
        "answered": AIWorkflowStatus.REPONDU.value,
        "done": AIWorkflowStatus.REPONDU.value,
        "closed": AIWorkflowStatus.CLOS.value,
    }
    normalized = aliases.get(normalized, normalized)
    try:
        return AIWorkflowStatus(normalized)
    except ValueError:
        return AIWorkflowStatus.REPONDU


def derive_workflow_status(
    *,
    conversation: Mapping[str, object],
    messages: Sequence[Mapping[str, object]],
    category: AICategory,
    fallback: AIWorkflowStatus,
    suggested_reply: str | None,
    next_action: str,
) -> AIWorkflowStatus:
    """Compute a stable workflow status from the thread and AI result."""

    if category == AICategory.SPAM:
        return AIWorkflowStatus.CLOS
    if str(conversation.get("status") or "").strip().lower() == "closed":
        return AIWorkflowStatus.CLOS

    client_name = str(conversation.get("client_name") or "").strip().lower()
    freelancer_name = str(conversation.get("freelancer_name") or "")
    last_role = "unknown"
    last_created_at: datetime | None = None
    for message in reversed(messages):
        sender = str(message.get("sender") or "").strip()
        if not sender or sender.lower() == "unknown":
            continue
        last_role = _message_role(
            sender=sender,
            client_name=client_name,
            freelancer_name=freelancer_name,
        )
        last_created_at = _parse_message_datetime(message.get("created_at"))
        last_content = str(message.get("content") or "")
        break
    else:
        last_content = ""

    if last_role == "client" and client_requires_answer_signal(last_content):
        return AIWorkflowStatus.A_REPONDRE
    if last_role == "client" and client_reply_pending_signal(last_content):
        return AIWorkflowStatus.ATTENTE_REPONSE

    if last_role == "client":
        return AIWorkflowStatus.A_REPONDRE

    if last_role == "toi":
        if next_action.lower().startswith("attendre"):
            return AIWorkflowStatus.ATTENTE_REPONSE
        if last_created_at is not None:
            age_days = (datetime.now(tz=timezone.utc) - last_created_at).total_seconds() / 86400
            if age_days <= 7:
                return AIWorkflowStatus.ATTENTE_REPONSE
        return AIWorkflowStatus.REPONDU

    if next_action.lower().startswith("attendre"):
        return AIWorkflowStatus.ATTENTE_REPONSE
    if suggested_reply:
        return AIWorkflowStatus.A_REPONDRE
    if fallback == AIWorkflowStatus.ATTENTE_REPONSE:
        return AIWorkflowStatus.ATTENTE_REPONSE
    return fallback


def _parse_message_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _last_message_context(
    *,
    conversation: Mapping[str, object],
    messages: Sequence[Mapping[str, object]],
) -> tuple[str, str, datetime | None, int | None]:
    client_name = str(conversation.get("client_name") or "").strip().lower()
    freelancer_name = str(conversation.get("freelancer_name") or "")
    last_role = "unknown"
    last_content = ""
    last_created_at: datetime | None = None
    for message in reversed(messages):
        sender = str(message.get("sender") or "").strip()
        if not sender or sender.lower() == "unknown":
            continue
        last_role = _message_role(
            sender=sender,
            client_name=client_name,
            freelancer_name=freelancer_name,
        )
        last_created_at = _parse_message_datetime(message.get("created_at"))
        last_content = str(message.get("content") or "")
        break
    return last_role, last_content, last_created_at, _days_since(last_created_at)


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def analysis_due(
    *,
    conversation_updated_at: datetime,
    last_analyzed_at: datetime | None,
) -> bool:
    """Return whether a conversation should be analyzed again."""

    current = _aware_utc(conversation_updated_at)
    previous = _aware_utc(last_analyzed_at)
    return previous is None or (current is not None and current > previous)


def waiting_review_due(
    *,
    last_analyzed_at: datetime | None,
    workflow_status: str | None,
) -> bool:
    """Re-run analysis periodically for waiting threads so relances can surface."""

    if workflow_status != AIWorkflowStatus.ATTENTE_REPONSE.value:
        return False
    if last_analyzed_at is None:
        return True
    age_hours = (datetime.now(tz=timezone.utc) - _aware_utc(last_analyzed_at)).total_seconds() / 3600
    return age_hours >= 18
