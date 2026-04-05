"""Database models and persistence helpers."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    create_engine,
    delete,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from .models import (
    AIWorkflowStatus,
    Conversation,
    ConversationAIAnalysis,
    CRMPriority,
    CRMStatus,
    MaltProfileSnapshot,
    Message,
    Opportunity,
    OpportunityAIAnalysis,
)


class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""


class _ReminderUnset:
    """Sentinel : ne pas modifier reminder_due_at."""


REMINDER_UNCHANGED = _ReminderUnset()


class ConversationRecord(Base):
    """Persisted conversation."""

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_name: Mapped[str] = mapped_column(String(255), nullable=False)
    last_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=CRMStatus.NEW.value)
    priority: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=CRMPriority.MEDIUM.value,
    )
    ai_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ai_workflow_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ai_urgency: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ai_needs_reply: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_next_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_reply_draft: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_last_analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    manual_workflow_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    manual_next_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reminder_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    messages: Mapped[list[MessageRecord]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
    )


class MessageRecord(Base):
    """Persisted message."""

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    sender: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    conversation: Mapped[ConversationRecord] = relationship(back_populates="messages")


class OpportunityRecord(Base):
    """Persisted project opportunity."""

    __tablename__ = "opportunities"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    budget: Mapped[float | None] = mapped_column(Float, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=CRMStatus.NEW.value)
    priority: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=CRMPriority.MEDIUM.value,
    )
    ai_fit_label: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ai_fit_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_should_reply: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ai_reply_draft: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_last_analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TimelineEventRecord(Base):
    """Événement CRM local (relance, message envoyé, changement de statut)."""

    __tablename__ = "timeline_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    opportunity_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("opportunities.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProfileSnapshotRecord(Base):
    """Persisted Malt profile snapshot."""

    __tablename__ = "profile_snapshots"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    headline: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    skills_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    missions_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    profile_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    daily_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_html_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def _aware_utc(value: datetime) -> datetime:
    """Normalize datetimes for safe comparisons."""

    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def make_database_url(database: str | Path) -> str:
    """Build a SQLAlchemy connection string from a path or raw URL."""

    database_str = str(database)
    if "://" in database_str:
        return database_str
    return f"sqlite:///{Path(database_str).expanduser().resolve()}"


def get_engine(database: str | Path) -> Engine:
    """Create the SQLAlchemy engine."""

    database_url = make_database_url(database)
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite:///") else {}
    return create_engine(database_url, future=True, connect_args=connect_args)


def create_session_factory(database: str | Path) -> sessionmaker[Session]:
    """Create the engine, initialize schema, and return a session factory."""

    engine = get_engine(database)
    Base.metadata.create_all(engine)
    _ensure_schema_updates(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _ensure_schema_updates(engine: Engine) -> None:
    """Apply lightweight schema updates for existing SQLite databases."""

    if engine.dialect.name != "sqlite":
        return

    expected_columns = {
        "ai_category": "ALTER TABLE conversations ADD COLUMN ai_category VARCHAR(32)",
        "ai_workflow_status": "ALTER TABLE conversations ADD COLUMN ai_workflow_status VARCHAR(32)",
        "ai_urgency": "ALTER TABLE conversations ADD COLUMN ai_urgency VARCHAR(32)",
        "ai_needs_reply": "ALTER TABLE conversations ADD COLUMN ai_needs_reply BOOLEAN",
        "ai_summary": "ALTER TABLE conversations ADD COLUMN ai_summary TEXT",
        "ai_next_action": "ALTER TABLE conversations ADD COLUMN ai_next_action TEXT",
        "ai_reply_draft": "ALTER TABLE conversations ADD COLUMN ai_reply_draft TEXT",
        "ai_confidence": "ALTER TABLE conversations ADD COLUMN ai_confidence FLOAT",
        "ai_last_analyzed_at": "ALTER TABLE conversations ADD COLUMN ai_last_analyzed_at DATETIME",
        "manual_workflow_status": "ALTER TABLE conversations ADD COLUMN manual_workflow_status VARCHAR(32)",
        "manual_next_action": "ALTER TABLE conversations ADD COLUMN manual_next_action TEXT",
        "archived_at": "ALTER TABLE conversations ADD COLUMN archived_at DATETIME",
        "reminder_due_at": "ALTER TABLE conversations ADD COLUMN reminder_due_at DATETIME",
    }
    opportunity_expected_columns = {
        "ai_fit_label": "ALTER TABLE opportunities ADD COLUMN ai_fit_label VARCHAR(32)",
        "ai_fit_score": "ALTER TABLE opportunities ADD COLUMN ai_fit_score FLOAT",
        "ai_summary": "ALTER TABLE opportunities ADD COLUMN ai_summary TEXT",
        "ai_should_reply": "ALTER TABLE opportunities ADD COLUMN ai_should_reply BOOLEAN",
        "ai_reply_draft": "ALTER TABLE opportunities ADD COLUMN ai_reply_draft TEXT",
        "ai_confidence": "ALTER TABLE opportunities ADD COLUMN ai_confidence FLOAT",
        "ai_last_analyzed_at": "ALTER TABLE opportunities ADD COLUMN ai_last_analyzed_at DATETIME",
        "archived_at": "ALTER TABLE opportunities ADD COLUMN archived_at DATETIME",
    }
    profile_expected_columns = {
        "image_url": "ALTER TABLE profile_snapshots ADD COLUMN image_url VARCHAR(1024)",
    }

    existing_columns = {
        column["name"]
        for column in inspect(engine).get_columns("conversations")
    }
    missing = [ddl for name, ddl in expected_columns.items() if name not in existing_columns]
    if not missing:
        pass

    with engine.begin() as connection:
        for ddl in missing:
            connection.execute(text(ddl))
        opportunity_columns = {
            column["name"]
            for column in inspect(engine).get_columns("opportunities")
        }
        opportunity_missing = [
            ddl for name, ddl in opportunity_expected_columns.items() if name not in opportunity_columns
        ]
        for ddl in opportunity_missing:
            connection.execute(text(ddl))
        profile_columns = {
            column["name"]
            for column in inspect(engine).get_columns("profile_snapshots")
        } if "profile_snapshots" in inspect(engine).get_table_names() else set()
        profile_missing = [
            ddl for name, ddl in profile_expected_columns.items() if name not in profile_columns
        ]
        for ddl in profile_missing:
            connection.execute(text(ddl))

        insp = inspect(connection)
        if "timeline_events" not in insp.get_table_names():
            connection.execute(
                text(
                    """
                    CREATE TABLE timeline_events (
                        id VARCHAR(36) NOT NULL PRIMARY KEY,
                        conversation_id VARCHAR(64) REFERENCES conversations(id) ON DELETE CASCADE,
                        opportunity_id VARCHAR(64) REFERENCES opportunities(id) ON DELETE CASCADE,
                        kind VARCHAR(32) NOT NULL,
                        title VARCHAR(512) NOT NULL,
                        detail TEXT,
                        created_at DATETIME NOT NULL
                    )
                    """
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_timeline_conv "
                    "ON timeline_events (conversation_id)"
                )
            )


def upsert_conversation(session: Session, conversation: Conversation) -> ConversationRecord:
    """Insert or update one conversation."""

    record = session.get(ConversationRecord, conversation.id)
    if record is None:
        record = ConversationRecord(
            id=conversation.id,
            status=conversation.status.value,
            priority=conversation.priority.value,
        )
        session.add(record)
    elif _aware_utc(record.updated_at) < _aware_utc(conversation.updated_at) and record.archived_at is None:
        # New Malt activity invalidates previous manual workflow overrides.
        record.manual_workflow_status = None
        record.manual_next_action = None

    record.client_name = conversation.client_name
    record.last_message = conversation.last_message
    record.updated_at = conversation.updated_at
    record.priority = conversation.priority.value
    return record


def upsert_message(session: Session, message: Message) -> MessageRecord:
    """Insert or update one message."""

    record = session.get(MessageRecord, message.id)
    if record is None:
        record = MessageRecord(id=message.id)
        session.add(record)

    record.conversation_id = message.conversation_id
    record.sender = message.sender
    record.content = message.content
    record.created_at = message.created_at
    return record


def upsert_opportunity(session: Session, opportunity: Opportunity) -> OpportunityRecord:
    """Insert or update one project opportunity."""

    record = session.get(OpportunityRecord, opportunity.id)
    if record is None:
        record = OpportunityRecord(
            id=opportunity.id,
            status=opportunity.status.value,
            priority=opportunity.priority.value,
        )
        session.add(record)

    record.conversation_id = opportunity.conversation_id
    record.title = opportunity.title
    record.budget = opportunity.budget
    record.description = opportunity.description
    record.updated_at = opportunity.updated_at
    record.priority = opportunity.priority.value
    return record


def upsert_profile_snapshot(
    session: Session,
    snapshot: MaltProfileSnapshot,
) -> ProfileSnapshotRecord:
    """Insert or update the current freelancer profile snapshot."""

    record = session.get(ProfileSnapshotRecord, snapshot.key)
    if record is None:
        record = ProfileSnapshotRecord(key=snapshot.key)
        session.add(record)

    record.full_name = snapshot.full_name
    record.headline = snapshot.headline
    record.summary = snapshot.summary
    record.skills_json = json.dumps(snapshot.skills, ensure_ascii=False)
    record.missions_json = json.dumps(snapshot.missions, ensure_ascii=False)
    record.profile_url = snapshot.profile_url
    record.image_url = snapshot.image_url
    record.daily_rate = snapshot.daily_rate
    record.raw_html_hash = snapshot.raw_html_hash
    record.fetched_at = snapshot.fetched_at
    return record


def delete_missing_opportunities(session: Session, active_ids: set[str]) -> int:
    """Delete local opportunities that are no longer returned by Malt."""

    statement = delete(OpportunityRecord)
    if active_ids:
        statement = statement.where(OpportunityRecord.id.not_in(active_ids))
    result = session.execute(statement)
    return int(result.rowcount or 0)


def list_conversations(session: Session, limit: int = 50) -> Iterable[ConversationRecord]:
    """Return conversations ordered by most recent activity."""

    statement = (
        select(ConversationRecord)
        .order_by(ConversationRecord.updated_at.desc())
        .limit(limit)
    )
    return session.scalars(statement).all()


def max_budget_by_conversation_ids(session: Session, conversation_ids: list[str]) -> dict[str, float]:
    """Budget maximum par conversation (opportunités liées)."""

    if not conversation_ids:
        return {}
    rows = session.execute(
        select(OpportunityRecord.conversation_id, func.max(OpportunityRecord.budget))
        .where(
            OpportunityRecord.conversation_id.in_(conversation_ids),
            OpportunityRecord.conversation_id.isnot(None),
        )
        .group_by(OpportunityRecord.conversation_id)
    )
    return {str(cid): float(mx or 0) for cid, mx in rows if cid}


def append_timeline_event(
    session: Session,
    *,
    conversation_id: str | None,
    opportunity_id: str | None,
    kind: str,
    title: str,
    detail: str | None = None,
    created_at: datetime | None = None,
) -> TimelineEventRecord:
    """Ajoute un événement timeline (commit laissé au appelant si besoin)."""

    when = created_at or datetime.now(tz=timezone.utc)
    event = TimelineEventRecord(
        id=str(uuid.uuid4()),
        conversation_id=conversation_id,
        opportunity_id=opportunity_id,
        kind=kind,
        title=title,
        detail=detail,
        created_at=when,
    )
    session.add(event)
    return event


def list_timeline_for_conversation(
    session: Session,
    conversation_id: str,
    *,
    limit: int = 80,
) -> list[TimelineEventRecord]:
    """Événements les plus récents d’abord."""

    stmt = (
        select(TimelineEventRecord)
        .where(TimelineEventRecord.conversation_id == conversation_id)
        .order_by(TimelineEventRecord.created_at.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def get_conversation(session: Session, conversation_id: str) -> ConversationRecord | None:
    """Return a conversation by id."""

    return session.get(ConversationRecord, conversation_id)


def list_messages_for_conversation(
    session: Session,
    conversation_id: str,
) -> Iterable[MessageRecord]:
    """Return messages for a conversation ordered chronologically."""

    statement = (
        select(MessageRecord)
        .where(MessageRecord.conversation_id == conversation_id)
        .order_by(MessageRecord.created_at.asc())
    )
    return session.scalars(statement).all()


def list_opportunities(session: Session, limit: int = 50) -> Iterable[OpportunityRecord]:
    """Return opportunities ordered by most recent activity."""

    statement = (
        select(OpportunityRecord)
        .order_by(OpportunityRecord.updated_at.desc())
        .limit(limit)
    )
    return session.scalars(statement).all()


def list_opportunities_for_conversation(
    session: Session,
    conversation_id: str,
    limit: int = 20,
) -> Iterable[OpportunityRecord]:
    """Return opportunities linked to a conversation."""

    statement = (
        select(OpportunityRecord)
        .where(OpportunityRecord.conversation_id == conversation_id)
        .order_by(OpportunityRecord.updated_at.desc())
        .limit(limit)
    )
    return session.scalars(statement).all()


def get_opportunity(session: Session, opportunity_id: str) -> OpportunityRecord | None:
    """Return one opportunity by id."""

    return session.get(OpportunityRecord, opportunity_id)


def update_opportunity_crm(
    session: Session,
    opportunity_id: str,
    *,
    archived: bool | None = None,
) -> OpportunityRecord | None:
    """Update local CRM fields for one opportunity."""

    record = session.get(OpportunityRecord, opportunity_id)
    if record is None:
        return None

    if archived is not None:
        record.archived_at = datetime.now(tz=timezone.utc) if archived else None

    session.commit()
    session.refresh(record)
    return record


def get_profile_snapshot(session: Session, key: str = "self") -> MaltProfileSnapshot | None:
    """Return the latest stored freelancer profile snapshot."""

    record = session.get(ProfileSnapshotRecord, key)
    if record is None:
        return None
    return MaltProfileSnapshot(
        key=record.key,
        full_name=record.full_name,
        headline=record.headline,
        summary=record.summary,
        skills=json.loads(record.skills_json) if record.skills_json else [],
        missions=json.loads(record.missions_json) if record.missions_json else [],
        profile_url=record.profile_url,
        image_url=record.image_url,
        daily_rate=record.daily_rate,
        raw_html_hash=record.raw_html_hash,
        fetched_at=record.fetched_at,
    )


def update_conversation_crm(
    session: Session,
    conversation_id: str,
    *,
    status: CRMStatus | str | None = None,
    priority: CRMPriority | str | None = None,
    manual_workflow_status: AIWorkflowStatus | str | None = None,
    manual_next_action: str | None = None,
    archived: bool | None = None,
    reminder_due_at: datetime | None | _ReminderUnset = REMINDER_UNCHANGED,
    bump_updated_at: bool = False,
) -> ConversationRecord | None:
    """Update CRM fields for one conversation."""

    record = session.get(ConversationRecord, conversation_id)
    if record is None:
        return None

    if status is not None:
        record.status = CRMStatus(status).value
    if priority is not None:
        record.priority = CRMPriority(priority).value
    if manual_workflow_status is not None:
        raw_workflow = (
            manual_workflow_status.value
            if isinstance(manual_workflow_status, AIWorkflowStatus)
            else str(manual_workflow_status).strip()
        )
        record.manual_workflow_status = (
            AIWorkflowStatus(raw_workflow).value
            if raw_workflow
            else None
        )
    if manual_next_action is not None:
        normalized_action = str(manual_next_action).strip()
        record.manual_next_action = normalized_action or None
    if archived is not None:
        if archived:
            record.archived_at = datetime.now(tz=timezone.utc)
            record.status = CRMStatus.CLOSED.value
            record.manual_workflow_status = AIWorkflowStatus.CLOS.value
            if not record.manual_next_action:
                record.manual_next_action = "Archivé hors Malt."
        else:
            record.archived_at = None
            if record.status == CRMStatus.CLOSED.value:
                record.status = CRMStatus.CONTACTED.value
            if record.manual_workflow_status == AIWorkflowStatus.CLOS.value:
                record.manual_workflow_status = None
            if record.manual_next_action == "Archivé hors Malt.":
                record.manual_next_action = None

    if reminder_due_at is not REMINDER_UNCHANGED:
        record.reminder_due_at = reminder_due_at
    if bump_updated_at:
        record.updated_at = datetime.now(tz=timezone.utc)

    session.commit()
    session.refresh(record)
    return record


def update_conversation_ai(
    session: Session,
    conversation_id: str,
    analysis: ConversationAIAnalysis,
    *,
    analyzed_at: datetime,
) -> ConversationRecord | None:
    """Persist the AI-enriched metadata for one conversation."""

    record = session.get(ConversationRecord, conversation_id)
    if record is None:
        return None

    record.ai_category = analysis.category.value
    record.ai_workflow_status = analysis.workflow_status.value
    record.ai_urgency = analysis.urgency.value
    record.ai_needs_reply = bool(analysis.needs_reply)
    record.ai_summary = analysis.summary
    record.ai_next_action = analysis.next_action
    record.ai_reply_draft = analysis.suggested_reply
    record.ai_confidence = analysis.confidence
    record.ai_last_analyzed_at = analyzed_at
    session.commit()
    session.refresh(record)
    return record


def update_opportunity_ai(
    session: Session,
    opportunity_id: str,
    analysis: OpportunityAIAnalysis,
    *,
    analyzed_at: datetime,
) -> OpportunityRecord | None:
    """Persist the AI-enriched metadata for one opportunity."""

    record = session.get(OpportunityRecord, opportunity_id)
    if record is None:
        return None

    record.ai_fit_label = analysis.fit_label
    record.ai_fit_score = float(analysis.fit_score)
    record.ai_summary = analysis.summary
    record.ai_should_reply = bool(analysis.should_reply)
    record.ai_reply_draft = analysis.suggested_reply
    record.ai_confidence = analysis.confidence
    record.ai_last_analyzed_at = analyzed_at
    session.commit()
    session.refresh(record)
    return record
