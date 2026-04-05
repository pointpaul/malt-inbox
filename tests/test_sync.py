from __future__ import annotations

from datetime import datetime, timezone

import malt_crm.sync as sync_module
from malt_crm.ai import OpenAISettings
from malt_crm.db import (
    create_session_factory,
    get_conversation,
    get_opportunity,
    upsert_conversation,
    upsert_message,
    upsert_opportunity,
)
from malt_crm.dirs import malt_local_dir
from malt_crm.models import (
    AICategory,
    AIUrgency,
    AIWorkflowStatus,
    Conversation,
    ConversationAIAnalysis,
    CRMPriority,
    CRMStatus,
    Message,
    Opportunity,
    OpportunityAIAnalysis,
)
from malt_crm.sync import auto_score_opportunity


def test_auto_score_opportunity_marks_urgent_as_high_priority() -> None:
    priority = auto_score_opportunity("Besoin urgent d'une réponse aujourd'hui.")

    assert priority is CRMPriority.HIGH


def test_auto_score_opportunity_marks_large_budget_as_high_priority() -> None:
    priority = auto_score_opportunity("Mission API backend", budget=7000)

    assert priority is CRMPriority.HIGH


def test_auto_score_opportunity_marks_empty_payload_as_low_priority() -> None:
    priority = auto_score_opportunity(None)

    assert priority is CRMPriority.LOW


def test_malt_local_dir_creates_dot_local(tmp_path) -> None:
    project = tmp_path / "repo"
    project.mkdir()

    d = malt_local_dir(project)

    assert d == project / ".local"
    assert d.is_dir()


def test_sync_ai_reports_progress_and_persists_analysis(tmp_path, monkeypatch) -> None:
    class FakeAnalyzer:
        def __init__(self, settings: OpenAISettings, profile) -> None:
            self.profile = profile

        def analyze(self, *, conversation, messages, opportunities) -> ConversationAIAnalysis:
            return ConversationAIAnalysis(
                workflow_status=AIWorkflowStatus.A_REPONDRE,
                category=AICategory.LEAD,
                urgency=AIUrgency.MEDIUM,
                needs_reply=True,
                summary=f"Résumé {conversation['id']}",
                next_action="Répondre au client.",
                suggested_reply="Bonjour, merci pour votre message.",
                confidence=0.9,
            )

        def analyze_opportunity(self, *, opportunity) -> OpportunityAIAnalysis:
            return OpportunityAIAnalysis(
                fit_label="bon_match",
                fit_score=82,
                summary=f"Fit {opportunity['id']}",
                should_reply=True,
                suggested_reply="Bonjour, le sujet correspond bien à mon profil.",
                confidence=0.8,
            )

    monkeypatch.setattr(sync_module, "OpenAIConversationAnalyzer", FakeAnalyzer)

    session_factory = create_session_factory(tmp_path / "malt.sqlite3")
    service = sync_module.MaltSyncService(client=object(), session_factory=session_factory)
    service.ai_settings = OpenAISettings(api_key="test-key")

    with session_factory() as session:
        upsert_conversation(
            session,
            Conversation(
                id="conv-1",
                client_name="Client One",
                last_message="Bonjour",
                updated_at=datetime(2026, 3, 20, 10, 0, tzinfo=timezone.utc),
                status=CRMStatus.NEW,
                priority=CRMPriority.MEDIUM,
            ),
        )
        upsert_conversation(
            session,
            Conversation(
                id="conv-2",
                client_name="Client Two",
                last_message="Bonsoir",
                updated_at=datetime(2026, 3, 20, 11, 0, tzinfo=timezone.utc),
                status=CRMStatus.NEW,
                priority=CRMPriority.MEDIUM,
            ),
        )
        upsert_message(
            session,
            Message(
                id="msg-1",
                conversation_id="conv-1",
                sender="Client One",
                content="Bonjour",
                created_at=datetime(2026, 3, 20, 10, 0, tzinfo=timezone.utc),
            ),
        )
        upsert_message(
            session,
            Message(
                id="msg-2",
                conversation_id="conv-2",
                sender="Client Two",
                content="Bonsoir",
                created_at=datetime(2026, 3, 20, 11, 0, tzinfo=timezone.utc),
            ),
        )
        upsert_opportunity(
            session,
            Opportunity(
                id="opp-1",
                conversation_id=None,
                title="Mission backend Python",
                budget=4000,
                description="Automatisation et intégration API",
                updated_at=datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc),
            ),
        )
        session.commit()

    progress_events: list[tuple[int, int]] = []
    report = service.sync_ai(
        max_workers=20,
        progress_callback=lambda completed, total: progress_events.append((completed, total)),
    )

    assert report.ai_analyses == 3
    assert progress_events[0] == (0, 3)
    assert progress_events[-1] == (3, 3)

    with session_factory() as session:
        record = get_conversation(session, "conv-1")
        assert record is not None
        assert record.ai_workflow_status == AIWorkflowStatus.A_REPONDRE.value
        assert record.ai_summary == "Résumé conv-1"
        opportunity = get_opportunity(session, "opp-1")
        assert opportunity is not None
        assert opportunity.ai_fit_score == 82
        assert opportunity.ai_fit_label == "bon_match"
        assert opportunity.ai_summary == "Fit opp-1"
