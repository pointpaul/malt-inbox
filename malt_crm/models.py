"""Pydantic models and payload normalization helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict


class CRMStatus(str, Enum):
    """High-level CRM status."""

    NEW = "new"
    CONTACTED = "contacted"
    CLOSED = "closed"


class CRMPriority(str, Enum):
    """Priority used by the local CRM."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AICategory(str, Enum):
    """AI-derived conversation category."""

    LEAD = "lead"
    RELANCE = "relance"
    PROJET_ACTIF = "projet_actif"
    CLOSING = "closing"
    SUPPORT = "support"
    SPAM = "spam"


class AIUrgency(str, Enum):
    """AI-derived urgency level."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AIWorkflowStatus(str, Enum):
    """AI-derived workflow status for one conversation."""

    A_REPONDRE = "a_repondre"
    ATTENTE_REPONSE = "attente_reponse"
    REPONDU = "repondu"
    CLOS = "clos"


def parse_datetime(value: Any, *, fallback_to_now: bool = True) -> datetime:
    """Convert API date values into timezone-aware datetimes."""

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    if isinstance(value, str) and value:
        normalized = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            match = re.match(
                r"^(?P<prefix>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?P<fraction>\.\d+)?(?P<offset>Z|[+-]\d{2}:\d{2})?$",
                value.strip(),
            )
            if match:
                prefix = match.group("prefix")
                fraction = match.group("fraction") or ""
                offset = match.group("offset") or ""

                if fraction:
                    microseconds = fraction[1:7].ljust(6, "0")
                    fraction = f".{microseconds}"

                if offset == "Z":
                    offset = "+00:00"

                parsed = datetime.fromisoformat(f"{prefix}{fraction}{offset}")
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    if fallback_to_now:
        return datetime.now(tz=timezone.utc)

    raise ValueError("Missing datetime value")


def _first_non_empty(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
    return None


def _display_name_from_identity(identity: Mapping[str, Any]) -> str:
    company = identity.get("company")
    if isinstance(company, Mapping):
        company_name = _first_non_empty(company.get("name"))
        if company_name:
            return company_name

    full_name = _first_non_empty(
        " ".join(
            part
            for part in [
                str(identity.get("firstName", "")).strip(),
                str(identity.get("lastName", "")).strip(),
            ]
            if part
        )
    )
    if full_name:
        return full_name

    return "Unknown"


def _extract_counterparty_name(payload: Mapping[str, Any]) -> str:
    participants = payload.get("participants")
    if isinstance(participants, list):
        for participant in participants:
            if not isinstance(participant, Mapping):
                continue
            if participant.get("participantType") in {"INTERLOCUTOR", "CLIENT"}:
                return _display_name_from_identity(participant)
        for participant in participants:
            if not isinstance(participant, Mapping):
                continue
            if participant.get("participantType") != "CURRENT_USER":
                return _display_name_from_identity(participant)

    client = payload.get("client")
    if isinstance(client, Mapping):
        client_name = _first_non_empty(
            client.get("fullName"),
            client.get("name"),
        )
        if client_name:
            return client_name

    company = payload.get("company")
    if isinstance(company, Mapping):
        company_name = _first_non_empty(company.get("name"))
        if company_name:
            return company_name

    return _first_non_empty(payload.get("title")) or "Unknown client"


def _extract_message_content(payload: Mapping[str, Any]) -> str:
    nested_message = payload.get("message")
    if isinstance(nested_message, Mapping):
        content = _extract_message_content(nested_message)
        if content:
            return content

    content = _first_non_empty(
        payload.get("content"),
        payload.get("description"),
        payload.get("contextDescription"),
        payload.get("projectDescription"),
        payload.get("title"),
    )
    if content:
        return content

    message_type = _first_non_empty(payload.get("type"))
    if message_type:
        return f"[{message_type}]"

    return ""


def _extract_sender_name(payload: Mapping[str, Any]) -> str:
    for key in ("author", "sender", "initiator", "recipient"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            return _display_name_from_identity(value)
    return "Unknown"


def _extract_budget(payload: Mapping[str, Any]) -> float | None:
    candidates = [
        payload.get("budget"),
        payload.get("dailyRate"),
        payload.get("freelancerDailyRate"),
    ]

    project_details = payload.get("projectDetails")
    if isinstance(project_details, Mapping):
        candidates.extend(
            [
                project_details.get("budget"),
                project_details.get("freelancerDailyRate"),
            ]
        )

    for candidate in candidates:
        if isinstance(candidate, (int, float)):
            return float(candidate)
        if isinstance(candidate, str):
            try:
                return float(candidate)
            except ValueError:
                continue
        if isinstance(candidate, Mapping):
            for key in ("amount", "value", "maximum", "minimum"):
                raw = candidate.get(key)
                if isinstance(raw, (int, float)):
                    return float(raw)
    return None


class Conversation(BaseModel):
    """Normalized conversation model."""

    model_config = ConfigDict(extra="allow")

    id: str
    client_name: str
    last_message: str | None = None
    updated_at: datetime
    status: CRMStatus = CRMStatus.NEW
    priority: CRMPriority = CRMPriority.MEDIUM

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> Conversation:
        conversation_id = payload.get("conversationId") or payload.get("id")
        if not conversation_id:
            raise ValueError("Conversation payload has no identifier")

        last_message_payload = payload.get("lastMessage") or payload.get("matchingMessage")
        last_message = None
        if isinstance(last_message_payload, Mapping):
            last_message = _extract_message_content(last_message_payload)

        return cls(
            id=str(conversation_id),
            client_name=_extract_counterparty_name(payload),
            last_message=last_message,
            updated_at=parse_datetime(
                payload.get("lastEventDate")
                or payload.get("date")
                or payload.get("creationDate")
                or payload.get("startDate")
            ),
        )


class Message(BaseModel):
    """Normalized message model."""

    model_config = ConfigDict(extra="allow")

    id: str
    conversation_id: str
    sender: str
    content: str
    created_at: datetime

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> Message:
        message_id = payload.get("messageId") or payload.get("id")
        conversation_id = payload.get("conversationId")
        if not message_id or not conversation_id:
            raise ValueError("Message payload is missing identifiers")

        return cls(
            id=str(message_id),
            conversation_id=str(conversation_id),
            sender=_extract_sender_name(payload),
            content=_extract_message_content(payload),
            created_at=parse_datetime(payload.get("date") or payload.get("createdAt")),
        )


class Opportunity(BaseModel):
    """Normalized client project opportunity model."""

    model_config = ConfigDict(extra="allow")

    id: str
    title: str
    budget: float | None = None
    description: str | None = None
    updated_at: datetime
    conversation_id: str | None = None
    status: CRMStatus = CRMStatus.NEW
    priority: CRMPriority = CRMPriority.MEDIUM

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> Opportunity:
        opportunity_id = payload.get("clientProjectId") or payload.get("id")
        if not opportunity_id:
            raise ValueError("Opportunity payload has no identifier")

        project_details = payload.get("projectDetails")
        description = None
        if isinstance(project_details, Mapping):
            description = _first_non_empty(
                project_details.get("description"),
                project_details.get("contextDescription"),
            )

        return cls(
            id=str(opportunity_id),
            title=_first_non_empty(payload.get("title")) or "Untitled opportunity",
            budget=_extract_budget(payload),
            description=description or _extract_message_content(payload) or None,
            updated_at=parse_datetime(
                payload.get("lastEventDate")
                or payload.get("offerDate")
                or payload.get("date")
            ),
            conversation_id=(
                str(payload.get("conversationId"))
                if payload.get("conversationId") is not None
                else None
            ),
        )


class ConversationAIAnalysis(BaseModel):
    """AI-enriched metadata used to prioritize a conversation."""

    workflow_status: AIWorkflowStatus
    category: AICategory
    urgency: AIUrgency
    needs_reply: bool
    summary: str
    next_action: str
    suggested_reply: str | None = None
    confidence: float | None = None


class OpportunityAIAnalysis(BaseModel):
    """AI-enriched metadata used to qualify one Malt opportunity."""

    fit_label: str
    fit_score: int
    summary: str
    should_reply: bool
    suggested_reply: str | None = None
    confidence: float | None = None


class MaltProfileSnapshot(BaseModel):
    """Parsed Malt profile used to enrich AI prompts."""

    model_config = ConfigDict(extra="allow")

    key: str = "self"
    full_name: str
    headline: str | None = None
    summary: str | None = None
    skills: list[str] = []
    missions: list[str] = []
    profile_url: str | None = None
    image_url: str | None = None
    daily_rate: float | None = None
    raw_html_hash: str | None = None
    fetched_at: datetime
