from __future__ import annotations

from datetime import datetime, timezone

from malt_crm.models import Conversation, Opportunity, parse_datetime


def test_parse_datetime_keeps_timezone_information() -> None:
    parsed = parse_datetime("2026-03-20T10:15:30Z")

    assert parsed == datetime(2026, 3, 20, 10, 15, 30, tzinfo=timezone.utc)


def test_conversation_from_api_normalizes_counterparty_and_message() -> None:
    conversation = Conversation.from_api(
        {
            "conversationId": "conv_123",
            "participants": [
                {"participantType": "CURRENT_USER", "firstName": "Paul", "lastName": "Maylie"},
                {"participantType": "CLIENT", "company": {"name": "Acme"}},
            ],
            "lastMessage": {"content": "Bonjour, on peut avancer cette semaine ?"},
            "lastEventDate": "2026-03-20T10:15:30Z",
        }
    )

    assert conversation.id == "conv_123"
    assert conversation.client_name == "Acme"
    assert conversation.last_message == "Bonjour, on peut avancer cette semaine ?"


def test_opportunity_from_api_extracts_budget_and_description() -> None:
    opportunity = Opportunity.from_api(
        {
            "clientProjectId": "opp_123",
            "title": "API Python",
            "projectDescription": "Construction d'une API métier.",
            "budget": {"amount": 7200},
            "date": "2026-03-20T10:15:30Z",
        }
    )

    assert opportunity.id == "opp_123"
    assert opportunity.title == "API Python"
    assert opportunity.description == "Construction d'une API métier."
    assert opportunity.budget == 7200.0
