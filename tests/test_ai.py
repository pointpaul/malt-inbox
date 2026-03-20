from __future__ import annotations

from datetime import datetime, timedelta, timezone

from malt_crm.ai import analysis_due, waiting_review_due
from malt_crm.models import AIWorkflowStatus


def test_analysis_due_accepts_naive_database_datetimes() -> None:
    updated_at = datetime(2026, 3, 20, 10, 0, 0, tzinfo=timezone.utc)
    last_analyzed_at = datetime(2026, 3, 20, 9, 0, 0)

    assert analysis_due(
        conversation_updated_at=updated_at,
        last_analyzed_at=last_analyzed_at,
    )


def test_waiting_review_due_accepts_naive_database_datetimes() -> None:
    last_analyzed_at = datetime.now(tz=timezone.utc).replace(tzinfo=None) - timedelta(hours=24)

    assert waiting_review_due(
        last_analyzed_at=last_analyzed_at,
        workflow_status=AIWorkflowStatus.ATTENTE_REPONSE.value,
    )
