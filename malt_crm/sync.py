"""Synchronization logic between the Malt API and SQLite storage."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .ai import (
    FreelancerProfile,
    OpenAIConversationAnalyzer,
    OpenAISettings,
    analysis_due,
    waiting_review_due,
)
from .api import MaltAPIClient
from .db import (
    ConversationRecord,
    OpportunityRecord,
    delete_missing_opportunities,
    get_opportunity,
    get_profile_snapshot,
    list_messages_for_conversation,
    list_opportunities,
    list_opportunities_for_conversation,
    upsert_conversation,
    upsert_message,
    upsert_opportunity,
    upsert_profile_snapshot,
)
from .models import (
    AIWorkflowStatus,
    ConversationAIAnalysis,
    CRMPriority,
    OpportunityAIAnalysis,
)
from .profile import MaltProfileFetcher

LOGGER = logging.getLogger(__name__)
ProgressCallback = Callable[[int, int], None]


def auto_score_opportunity(
    message_text: Optional[str],
    budget: Optional[float] = None,
    *,
    high_budget_threshold: float = 5000.0,
) -> CRMPriority:
    """Assign a priority using simple keyword and budget rules."""

    normalized_text = (message_text or "").lower()
    if re.search(r"\burgent\b", normalized_text):
        return CRMPriority.HIGH
    if budget is not None and budget > high_budget_threshold:
        return CRMPriority.HIGH
    if not normalized_text and budget is None:
        return CRMPriority.LOW
    return CRMPriority.MEDIUM


@dataclass
class SyncReport:
    """Simple sync counters."""

    conversations: int = 0
    messages: int = 0
    opportunities: int = 0
    ai_analyses: int = 0
    profile_refreshes: int = 0

    def merge(self, other: "SyncReport") -> "SyncReport":
        self.conversations += other.conversations
        self.messages += other.messages
        self.opportunities += other.opportunities
        self.ai_analyses += other.ai_analyses
        self.profile_refreshes += other.profile_refreshes
        return self


@dataclass(frozen=True)
class _ConversationAICandidate:
    conversation_id: str
    conversation: dict[str, object]
    messages: list[dict[str, object]]
    opportunities: list[dict[str, object]]


@dataclass(frozen=True)
class _ConversationAIResult:
    conversation_id: str
    analysis: ConversationAIAnalysis


@dataclass(frozen=True)
class _OpportunityAICandidate:
    opportunity_id: str
    opportunity: dict[str, object]


@dataclass(frozen=True)
class _OpportunityAIResult:
    opportunity_id: str
    analysis: OpportunityAIAnalysis


def _run_conversation_analysis(
    settings: OpenAISettings,
    profile: FreelancerProfile,
    candidate: _ConversationAICandidate,
) -> _ConversationAIResult:
    analyzer = OpenAIConversationAnalyzer(settings, profile=profile)
    analysis = analyzer.analyze(
        conversation=candidate.conversation,
        messages=candidate.messages,
        opportunities=candidate.opportunities,
    )
    return _ConversationAIResult(
        conversation_id=candidate.conversation_id,
        analysis=analysis,
    )


def _run_opportunity_analysis(
    settings: OpenAISettings,
    profile: FreelancerProfile,
    candidate: _OpportunityAICandidate,
) -> _OpportunityAIResult:
    analyzer = OpenAIConversationAnalyzer(settings, profile=profile)
    analysis = analyzer.analyze_opportunity(opportunity=candidate.opportunity)
    return _OpportunityAIResult(
        opportunity_id=candidate.opportunity_id,
        analysis=analysis,
    )


def _apply_conversation_analysis(
    record: ConversationRecord,
    analysis: ConversationAIAnalysis,
    *,
    analyzed_at: datetime,
) -> None:
    record.ai_category = analysis.category.value
    record.ai_workflow_status = analysis.workflow_status.value
    record.ai_urgency = analysis.urgency.value
    record.ai_needs_reply = bool(analysis.needs_reply)
    record.ai_summary = analysis.summary
    record.ai_next_action = analysis.next_action
    record.ai_reply_draft = analysis.suggested_reply
    record.ai_confidence = analysis.confidence
    record.ai_last_analyzed_at = analyzed_at


def _apply_opportunity_analysis(
    record: OpportunityRecord,
    analysis: OpportunityAIAnalysis,
    *,
    analyzed_at: datetime,
) -> None:
    record.ai_fit_label = analysis.fit_label
    record.ai_fit_score = float(analysis.fit_score)
    record.ai_summary = analysis.summary
    record.ai_should_reply = bool(analysis.should_reply)
    record.ai_reply_draft = analysis.suggested_reply
    record.ai_confidence = analysis.confidence
    record.ai_last_analyzed_at = analyzed_at


class MaltSyncService:
    """Synchronize Malt inbox resources into the local database."""

    def __init__(
        self,
        client: MaltAPIClient,
        session_factory: sessionmaker[Session],
        *,
        inbox_page_size: int = 100,
        message_page_size: int = 100,
        high_budget_threshold: float = 5000.0,
    ) -> None:
        self.client = client
        self.session_factory = session_factory
        self.inbox_page_size = inbox_page_size
        self.message_page_size = message_page_size
        self.high_budget_threshold = high_budget_threshold
        self.ai_settings = OpenAISettings.from_env()

    def sync_conversations(self) -> SyncReport:
        """Fetch conversations and opportunities and persist them."""

        conversations = self.client.get_conversations(page_size=self.inbox_page_size)
        opportunities = self.client.get_opportunities(page_size=self.inbox_page_size)

        for conversation in conversations:
            conversation.priority = auto_score_opportunity(
                conversation.last_message,
                high_budget_threshold=self.high_budget_threshold,
            )

        for opportunity in opportunities:
            opportunity.priority = auto_score_opportunity(
                opportunity.description,
                opportunity.budget,
                high_budget_threshold=self.high_budget_threshold,
            )

        with self.session_factory() as session:
            for conversation in conversations:
                upsert_conversation(session, conversation)
            for opportunity in opportunities:
                upsert_opportunity(session, opportunity)
            delete_missing_opportunities(
                session,
                {opportunity.id for opportunity in opportunities},
            )
            session.commit()

        return SyncReport(
            conversations=len(conversations),
            opportunities=len(opportunities),
        )

    def sync_messages(self, conversation_id: Optional[str] = None) -> SyncReport:
        """Fetch messages for one or many conversations and persist them."""

        if conversation_id:
            conversation_ids = [conversation_id]
        else:
            with self.session_factory() as session:
                statement = select(ConversationRecord.id).order_by(ConversationRecord.updated_at.desc())
                conversation_ids = list(session.scalars(statement))

        if not conversation_ids:
            return SyncReport()

        total_messages = 0
        with self.session_factory() as session:
            for current_id in conversation_ids:
                messages = self.client.get_messages(
                    current_id,
                    page_size=self.message_page_size,
                )
                for message in messages:
                    upsert_message(session, message)
                total_messages += len(messages)
            session.commit()

        return SyncReport(messages=total_messages)

    def sync_all(self) -> SyncReport:
        """Run conversation/opportunity sync followed by message sync."""

        report = self.sync_profile()
        report.merge(self.sync_conversations())
        report.merge(self.sync_messages())
        try:
            report.merge(self.sync_ai())
        except Exception:
            LOGGER.exception("AI sync failed")
        return report

    def sync_profile(self) -> SyncReport:
        """Refresh the freelancer profile snapshot used by AI prompts."""

        try:
            snapshot = MaltProfileFetcher(self.client.session.cookies).fetch()
        except Exception:
            LOGGER.exception("Profile refresh failed")
            return SyncReport()

        with self.session_factory() as session:
            upsert_profile_snapshot(session, snapshot)
            session.commit()
        return SyncReport(profile_refreshes=1)

    def sync_ai(
        self,
        *,
        limit: int | None = None,
        max_workers: int = 20,
        progress_callback: ProgressCallback | None = None,
    ) -> SyncReport:
        """Analyze updated conversations with OpenAI when configured."""

        if self.ai_settings is None:
            return SyncReport()

        with self.session_factory() as session:
            profile = FreelancerProfile.from_snapshot(get_profile_snapshot(session))
            statement = select(ConversationRecord).order_by(ConversationRecord.updated_at.desc())
            conversations = list(session.scalars(statement))
            if limit is not None:
                conversations = conversations[:limit]

            conversation_candidates: list[_ConversationAICandidate] = []
            for record in conversations:
                if not analysis_due(
                    conversation_updated_at=record.updated_at,
                    last_analyzed_at=record.ai_last_analyzed_at,
                ) and not waiting_review_due(
                    last_analyzed_at=record.ai_last_analyzed_at,
                    workflow_status=record.ai_workflow_status,
                ):
                    continue

                messages = list(list_messages_for_conversation(session, record.id))
                opportunities = list(list_opportunities_for_conversation(session, record.id))
                conversation_candidates.append(
                    _ConversationAICandidate(
                        conversation_id=record.id,
                        conversation={
                            "id": record.id,
                            "client_name": record.client_name,
                            "freelancer_name": profile.name,
                            "last_message": record.last_message,
                            "status": record.status,
                            "priority": record.priority,
                        },
                        messages=[
                            {
                                "sender": item.sender,
                                "content": item.content,
                                "created_at": item.created_at.isoformat(),
                            }
                            for item in messages
                        ],
                        opportunities=[
                            {
                                "title": item.title,
                                "budget": item.budget,
                                "description": item.description,
                            }
                            for item in opportunities
                        ],
                    )
                )

            opportunity_records = list(list_opportunities(session, limit=limit or 500))
            opportunity_candidates: list[_OpportunityAICandidate] = []
            for record in opportunity_records:
                if record.archived_at is not None:
                    continue
                if (
                    record.ai_last_analyzed_at is not None
                    and record.updated_at <= record.ai_last_analyzed_at
                ):
                    continue
                opportunity_candidates.append(
                    _OpportunityAICandidate(
                        opportunity_id=record.id,
                        opportunity={
                            "id": record.id,
                            "title": record.title,
                            "budget": record.budget,
                            "description": record.description,
                        },
                    )
                )

        total_candidates = len(conversation_candidates) + len(opportunity_candidates)
        if progress_callback is not None:
            progress_callback(0, total_candidates)
        if total_candidates == 0:
            return SyncReport()

        conversation_results: list[_ConversationAIResult] = []
        opportunity_results: list[_OpportunityAIResult] = []
        worker_count = max(1, min(max_workers, total_candidates))
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="malt-ai") as executor:
            futures = {
                executor.submit(
                    _run_conversation_analysis,
                    self.ai_settings,
                    profile,
                    candidate,
                ): candidate.conversation_id
                for candidate in conversation_candidates
            }
            futures.update(
                {
                    executor.submit(
                        _run_opportunity_analysis,
                        self.ai_settings,
                        profile,
                        candidate,
                    ): candidate.opportunity_id
                    for candidate in opportunity_candidates
                }
            )
            completed = 0
            for future in as_completed(futures):
                item_id = futures[future]
                try:
                    result = future.result()
                    if isinstance(result, _ConversationAIResult):
                        conversation_results.append(result)
                    else:
                        opportunity_results.append(result)
                except Exception:
                    LOGGER.exception("AI analysis failed for item %s", item_id)
                finally:
                    completed += 1
                    if progress_callback is not None:
                        progress_callback(completed, total_candidates)

        if not conversation_results and not opportunity_results:
            return SyncReport()

        analyzed_at = datetime.now(tz=timezone.utc)
        with self.session_factory() as session:
            for result in conversation_results:
                record = session.get(ConversationRecord, result.conversation_id)
                if record is None:
                    continue
                if (
                    record.archived_at is None
                    and result.analysis.workflow_status == AIWorkflowStatus.A_REPONDRE
                    and record.manual_workflow_status == AIWorkflowStatus.ATTENTE_REPONSE.value
                ):
                    record.manual_workflow_status = None
                    record.manual_next_action = None
                _apply_conversation_analysis(
                    record,
                    result.analysis,
                    analyzed_at=analyzed_at,
                )
            for result in opportunity_results:
                record = get_opportunity(session, result.opportunity_id)
                if record is None:
                    continue
                _apply_opportunity_analysis(
                    record,
                    result.analysis,
                    analyzed_at=analyzed_at,
                )
            session.commit()

        return SyncReport(ai_analyses=len(conversation_results) + len(opportunity_results))
