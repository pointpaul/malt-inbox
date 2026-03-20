"""Local dashboard server with automatic Malt synchronization."""

from __future__ import annotations

import json
import logging
import mimetypes
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from sqlalchemy import func, select

from .ai import (
    FOLLOW_UP_DELAY_DAYS,
    FreelancerProfile,
    OpenAIConversationAnalyzer,
    OpenAISettings,
)
from .api import MaltAPIClient
from .db import (
    ConversationRecord,
    MessageRecord,
    OpportunityRecord,
    create_session_factory,
    get_conversation,
    get_opportunity,
    get_profile_snapshot,
    list_conversations,
    list_messages_for_conversation,
    list_opportunities,
    list_opportunities_for_conversation,
    update_conversation_ai,
    update_conversation_crm,
    update_opportunity_ai,
    update_opportunity_crm,
)
from .env import load_project_env
from .models import AIWorkflowStatus
from .sync import MaltSyncService, SyncReport

LOGGER = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_SYNC_INTERVAL_SECONDS = 30 * 60
ASSETS_DIR = Path(__file__).resolve().parent / "assets"


@dataclass(frozen=True)
class DashboardConfig:
    """Runtime configuration for the local dashboard."""

    project_root: Path
    database_path: Path
    cookie_path: Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    sync_interval_seconds: int = DEFAULT_SYNC_INTERVAL_SECONDS


@dataclass
class SyncStatus:
    """In-memory status for the sync worker."""

    running: bool = False
    last_started_at: Optional[datetime] = None
    last_finished_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    last_error: Optional[str] = None
    last_report: Optional[SyncReport] = None
    cookie_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("last_started_at", "last_finished_at", "next_run_at"):
            value = payload[key]
            payload[key] = value.isoformat() if value else None
        payload["last_report"] = asdict(self.last_report) if self.last_report else None
        return payload


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _effective_workflow_status(record: ConversationRecord) -> str | None:
    if record.archived_at is not None:
        return AIWorkflowStatus.CLOS.value
    if record.manual_workflow_status:
        return record.manual_workflow_status
    if _follow_up_due(record):
        return AIWorkflowStatus.A_REPONDRE.value
    if record.ai_next_action and record.ai_next_action.lower().startswith("attendre"):
        return AIWorkflowStatus.ATTENTE_REPONSE.value
    return record.ai_workflow_status


def _effective_next_action(record: ConversationRecord) -> str | None:
    if record.archived_at is not None:
        return record.manual_next_action or "Archivé hors Malt."
    if record.manual_next_action:
        return record.manual_next_action
    if _follow_up_due(record):
        return "Relancer le client."
    return record.ai_next_action


def _follow_up_due(record: ConversationRecord) -> bool:
    if record.archived_at is not None:
        return False
    if record.manual_workflow_status == AIWorkflowStatus.ATTENTE_REPONSE.value:
        base_status = AIWorkflowStatus.ATTENTE_REPONSE.value
    elif record.ai_next_action and record.ai_next_action.lower().startswith("attendre"):
        base_status = AIWorkflowStatus.ATTENTE_REPONSE.value
    else:
        base_status = record.ai_workflow_status
    if base_status != AIWorkflowStatus.ATTENTE_REPONSE.value:
        return False
    updated_at = _aware_utc(record.updated_at)
    if updated_at is None:
        return False
    age_days = (_utcnow() - updated_at).total_seconds() / 86400
    return age_days >= FOLLOW_UP_DELAY_DAYS


def _follow_up_reply(record: ConversationRecord) -> str:
    client_name = record.client_name.strip()
    greeting = f"Bonjour {client_name}," if client_name else "Bonjour,"
    return (
        f"{greeting} je me permets de vous relancer au sujet du projet. "
        "Je suis toujours disponible pour avancer si c'est d'actualité de votre côté. "
        "Dites-moi si vous souhaitez que l'on en parle rapidement."
    )


def _serialize_conversation(
    record: ConversationRecord,
    *,
    message_count: int = 0,
    opportunity_count: int = 0,
) -> dict[str, Any]:
    follow_up_due = _follow_up_due(record)
    reply_draft = record.ai_reply_draft
    if follow_up_due and not reply_draft:
        reply_draft = _follow_up_reply(record)
    return {
        "id": record.id,
        "client_name": record.client_name,
        "last_message": record.last_message,
        "updated_at": record.updated_at.isoformat(),
        "status": record.status,
        "priority": record.priority,
        "message_count": message_count,
        "opportunity_count": opportunity_count,
        "workflow_status": _effective_workflow_status(record),
        "next_action": _effective_next_action(record),
        "follow_up_due": follow_up_due,
        "ai_category": record.ai_category,
        "ai_workflow_status": record.ai_workflow_status,
        "ai_urgency": record.ai_urgency,
        "ai_needs_reply": record.ai_needs_reply,
        "ai_summary": record.ai_summary,
        "ai_next_action": record.ai_next_action,
        "ai_reply_draft": reply_draft,
        "ai_confidence": record.ai_confidence,
        "manual_workflow_status": record.manual_workflow_status,
        "manual_next_action": record.manual_next_action,
        "archived_at": record.archived_at.isoformat() if record.archived_at else None,
        "ai_last_analyzed_at": (
            record.ai_last_analyzed_at.isoformat()
            if record.ai_last_analyzed_at
            else None
        ),
    }


def _serialize_message(record: MessageRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "conversation_id": record.conversation_id,
        "sender": record.sender,
        "content": record.content,
        "created_at": record.created_at.isoformat(),
    }


def _serialize_opportunity(record: OpportunityRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "conversation_id": record.conversation_id,
        "title": record.title,
        "budget": record.budget,
        "description": record.description,
        "updated_at": record.updated_at.isoformat(),
        "status": record.status,
        "priority": record.priority,
        "ai_fit_label": record.ai_fit_label,
        "ai_fit_score": record.ai_fit_score,
        "ai_summary": record.ai_summary,
        "ai_should_reply": record.ai_should_reply,
        "ai_reply_draft": record.ai_reply_draft,
        "ai_confidence": record.ai_confidence,
        "archived_at": record.archived_at.isoformat() if record.archived_at else None,
        "ai_last_analyzed_at": (
            record.ai_last_analyzed_at.isoformat()
            if record.ai_last_analyzed_at
            else None
        ),
    }


def _serialize_profile(profile: Any | None) -> dict[str, Any] | None:
    if profile is None:
        return None
    return {
        "full_name": profile.full_name,
        "headline": profile.headline,
        "summary": profile.summary,
        "profile_url": profile.profile_url,
        "image_url": profile.image_url,
        "daily_rate": profile.daily_rate,
        "fetched_at": profile.fetched_at.isoformat(),
    }


def load_stored_cookies(cookie_path: Path) -> int:
    """Count cookies available in the local cookie store."""

    try:
        payload = json.loads(cookie_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if isinstance(payload, dict):
        return len([value for value in payload.values() if str(value).strip()])
    if isinstance(payload, list):
        return len(payload)
    return 0


def _build_api_client(config: DashboardConfig) -> MaltAPIClient:
    """Create a Malt API client from the local cookie store."""

    return MaltAPIClient.from_cookies(
        cookies_json_path=config.cookie_path,
    )


class SyncManager:
    """Periodic synchronization controller."""

    def __init__(self, config: DashboardConfig) -> None:
        self.config = config
        self.status = SyncStatus(next_run_at=_utcnow())
        self._lock = Lock()
        self._stop_event = Event()
        self._wake_event = Event()
        self._skip_initial_sync = False
        self._thread = Thread(target=self._run_loop, name="malt-sync-loop", daemon=True)

    def start(self, *, skip_initial_sync: bool = False) -> None:
        """Start the background sync thread."""

        self._skip_initial_sync = skip_initial_sync
        self._thread.start()

    def stop(self) -> None:
        """Stop the background sync thread."""

        self._stop_event.set()
        self._wake_event.set()
        self._thread.join(timeout=5)

    def trigger_sync(self) -> bool:
        """Request an immediate synchronization."""

        with self._lock:
            if self.status.running:
                return False
            self.status.next_run_at = _utcnow()
        self._wake_event.set()
        return True

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-ready copy of the current sync status."""

        with self._lock:
            return self.status.to_dict()

    def record_completed_sync(
        self,
        *,
        report: SyncReport,
        cookie_count: int,
    ) -> None:
        """Seed the status after a successful foreground sync."""

        now = _utcnow()
        with self._lock:
            self.status.running = False
            self.status.last_error = None
            self.status.cookie_count = cookie_count
            self.status.last_report = report
            self.status.last_started_at = now
            self.status.last_finished_at = now
            self.status.next_run_at = now + timedelta(seconds=self.config.sync_interval_seconds)

    def _run_loop(self) -> None:
        first_iteration = True
        while not self._stop_event.is_set():
            if not (first_iteration and self._skip_initial_sync):
                self._run_sync_once()
            if self._stop_event.is_set():
                return
            first_iteration = False
            self._wake_event.clear()
            self._wake_event.wait(timeout=self.config.sync_interval_seconds)

    def _run_sync_once(self) -> None:
        with self._lock:
            self.status.running = True
            self.status.last_started_at = _utcnow()
            self.status.last_error = None

        try:
            cookie_count = load_stored_cookies(self.config.cookie_path)
            client = _build_api_client(self.config)
            session_factory = create_session_factory(self.config.database_path)
            report = MaltSyncService(client, session_factory).sync_all()
            with self._lock:
                self.status.cookie_count = cookie_count
                self.status.last_report = report
        except Exception as exc:
            LOGGER.exception("Dashboard sync failed")
            with self._lock:
                self.status.last_error = str(exc)
        finally:
            with self._lock:
                self.status.running = False
                self.status.last_finished_at = _utcnow()
                self.status.next_run_at = self.status.last_finished_at + timedelta(
                    seconds=self.config.sync_interval_seconds
                )


class DashboardApp:
    """HTTP server and JSON API for the local dashboard."""

    def __init__(self, config: DashboardConfig) -> None:
        self.config = config
        self.session_factory = create_session_factory(config.database_path)
        self.sync_manager = SyncManager(config)
        self.httpd = ThreadingHTTPServer(
            (config.host, config.port),
            self._build_handler(),
        )

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        app = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                app.handle_get(self)

            def do_POST(self) -> None:  # noqa: N802
                app.handle_post(self)

            def do_HEAD(self) -> None:  # noqa: N802
                app.handle_head(self)

            def address_string(self) -> str:
                return str(self.client_address[0])

            def log_message(self, format: str, *args: Any) -> None:
                LOGGER.info("%s - %s", self.address_string(), format % args)

        return Handler

    def start(self, *, skip_initial_sync: bool = False) -> None:
        """Start sync loop and serve HTTP requests."""

        self.sync_manager.start(skip_initial_sync=skip_initial_sync)
        url = f"http://{self.config.host}:{self.config.port}"
        LOGGER.info("Dashboard available at %s", url)
        Thread(target=lambda: webbrowser.open(url), daemon=True).start()
        try:
            self.httpd.serve_forever()
        finally:
            self.sync_manager.stop()
            self.httpd.server_close()

    def handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(handler.path)
        path = parsed.path

        if path == "/favicon.ico":
            self._send_empty(handler, status=HTTPStatus.NO_CONTENT)
            return

        if path.startswith("/assets/"):
            asset_path = (ASSETS_DIR / path.removeprefix("/assets/")).resolve()
            if ASSETS_DIR not in asset_path.parents or not asset_path.exists() or not asset_path.is_file():
                self._send_empty(handler, status=HTTPStatus.NOT_FOUND)
                return
            self._send_file(handler, asset_path)
            return

        if path == "/":
            self._send_html(handler, DASHBOARD_HTML)
            return

        if path == "/api/status":
            self._send_json(handler, self._build_status_payload())
            return

        if path == "/api/conversations":
            params = parse_qs(parsed.query)
            limit = max(1, min(int(params.get("limit", ["100"])[0]), 500))
            query = params.get("q", [""])[0].strip().lower()
            self._send_json(handler, self._load_conversations(limit=limit, query=query))
            return

        if path.startswith("/api/conversations/"):
            conversation_id = path.removeprefix("/api/conversations/").strip("/")
            payload = self._load_conversation_detail(conversation_id)
            if payload is None:
                self._send_json(handler, {"error": "Conversation not found"}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json(handler, payload)
            return

        if path == "/api/opportunities":
            self._send_json(handler, self._load_opportunities(limit=100))
            return

        if path.startswith("/api/opportunities/"):
            opportunity_id = path.removeprefix("/api/opportunities/").strip("/")
            payload = self._load_opportunity(opportunity_id)
            if payload is None:
                self._send_json(handler, {"error": "Opportunity not found"}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json(handler, payload)
            return

        if path.startswith("/api/messages/"):
            conversation_id = path.removeprefix("/api/messages/")
            self._send_json(handler, self._load_messages(conversation_id))
            return

        self._send_json(handler, {"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(handler.path)
        if parsed.path == "/api/sync":
            started = self.sync_manager.trigger_sync()
            status = HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT
            self._send_json(handler, {"started": started}, status=status)
            return

        if parsed.path.startswith("/api/conversations/") and parsed.path.endswith("/ai-refresh"):
            conversation_id = parsed.path.removeprefix("/api/conversations/").removesuffix("/ai-refresh").strip("/")
            try:
                payload = self._refresh_conversation_ai(conversation_id)
            except RuntimeError as exc:
                self._send_json(handler, {"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return

            if payload is None:
                self._send_json(handler, {"error": "Conversation not found"}, status=HTTPStatus.NOT_FOUND)
                return

            self._send_json(handler, payload)
            return

        if parsed.path.startswith("/api/conversations/") and parsed.path.endswith("/crm"):
            conversation_id = parsed.path.removeprefix("/api/conversations/").removesuffix("/crm").strip("/")
            try:
                payload = self._read_json_body(handler)
                updated = self._update_conversation_fields(conversation_id, payload)
            except ValueError as exc:
                self._send_json(handler, {"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return

            if updated is None:
                self._send_json(handler, {"error": "Conversation not found"}, status=HTTPStatus.NOT_FOUND)
                return

            self._send_json(handler, updated)
            return

        if parsed.path.startswith("/api/opportunities/") and parsed.path.endswith("/ai-draft"):
            opportunity_id = parsed.path.removeprefix("/api/opportunities/").removesuffix("/ai-draft").strip("/")
            try:
                payload = self._refresh_opportunity_ai(opportunity_id)
            except RuntimeError as exc:
                self._send_json(handler, {"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return

            if payload is None:
                self._send_json(handler, {"error": "Opportunity not found"}, status=HTTPStatus.NOT_FOUND)
                return

            self._send_json(handler, payload)
            return

        if parsed.path.startswith("/api/opportunities/") and parsed.path.endswith("/crm"):
            opportunity_id = parsed.path.removeprefix("/api/opportunities/").removesuffix("/crm").strip("/")
            try:
                payload = self._read_json_body(handler)
                updated = self._update_opportunity_fields(opportunity_id, payload)
            except ValueError as exc:
                self._send_json(handler, {"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return

            if updated is None:
                self._send_json(handler, {"error": "Opportunity not found"}, status=HTTPStatus.NOT_FOUND)
                return

            self._send_json(handler, updated)
            return

        self._send_json(handler, {"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def handle_head(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(handler.path)
        if parsed.path in {"/", "/favicon.ico"}:
            self._send_empty(handler)
            return
        self._send_empty(handler, status=HTTPStatus.NOT_FOUND)

    def _build_status_payload(self) -> dict[str, Any]:
        with self.session_factory() as session:
            profile = get_profile_snapshot(session)
        return {
            "sync": self.sync_manager.snapshot(),
            "profile": _serialize_profile(profile),
        }

    def _load_conversations(self, *, limit: int, query: str) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            items = list(list_conversations(session, limit=limit))
            conversation_ids = [item.id for item in items]
            message_counts: dict[str, int] = {}
            opportunity_counts: dict[str, int] = {}

            if conversation_ids:
                message_counts = {
                    str(conversation_id): int(count)
                    for conversation_id, count in session.execute(
                        select(MessageRecord.conversation_id, func.count())
                        .where(MessageRecord.conversation_id.in_(conversation_ids))
                        .group_by(MessageRecord.conversation_id)
                    )
                }
                opportunity_counts = {
                    str(conversation_id): int(count)
                    for conversation_id, count in session.execute(
                        select(OpportunityRecord.conversation_id, func.count())
                        .where(OpportunityRecord.conversation_id.in_(conversation_ids))
                        .group_by(OpportunityRecord.conversation_id)
                    )
                    if conversation_id
                }

        rows = [
            _serialize_conversation(
                item,
                message_count=message_counts.get(item.id, 0),
                opportunity_count=opportunity_counts.get(item.id, 0),
            )
            for item in items
        ]
        if not query:
            return rows
        return [
            row
            for row in rows
            if query in row["client_name"].lower() or query in (row["last_message"] or "").lower()
        ]

    def _load_conversation_detail(self, conversation_id: str) -> dict[str, Any] | None:
        with self.session_factory() as session:
            conversation = get_conversation(session, conversation_id)
            if conversation is None:
                return None

            messages = list(list_messages_for_conversation(session, conversation_id))
            opportunities = list(list_opportunities_for_conversation(session, conversation_id))

        return {
            "conversation": _serialize_conversation(
                conversation,
                message_count=len(messages),
                opportunity_count=len(opportunities),
            ),
            "messages": [_serialize_message(item) for item in messages],
            "opportunities": [_serialize_opportunity(item) for item in opportunities],
        }

    def _load_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            items = list(list_messages_for_conversation(session, conversation_id))
        return [_serialize_message(item) for item in items]

    def _load_opportunities(self, *, limit: int) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            items = list(list_opportunities(session, limit=limit))
        return [_serialize_opportunity(item) for item in items]

    def _load_opportunity(self, opportunity_id: str) -> dict[str, Any] | None:
        with self.session_factory() as session:
            opportunity = get_opportunity(session, opportunity_id)
            if opportunity is None:
                return None

            linked_conversation = (
                get_conversation(session, opportunity.conversation_id)
                if opportunity.conversation_id
                else None
            )

        return {
            "opportunity": _serialize_opportunity(opportunity),
            "conversation": (
                _serialize_conversation(linked_conversation)
                if linked_conversation is not None
                else None
            ),
        }

    def _update_conversation_fields(
        self,
        conversation_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        with self.session_factory() as session:
            updated = update_conversation_crm(
                session,
                conversation_id,
                status=payload.get("status"),
                priority=payload.get("priority"),
                manual_workflow_status=payload.get("manual_workflow_status"),
                manual_next_action=payload.get("manual_next_action"),
                archived=payload.get("archived"),
            )
            if updated is None:
                return None

            message_count = int(
                session.scalar(
                    select(func.count()).where(MessageRecord.conversation_id == conversation_id)
                )
                or 0
            )
            opportunity_count = int(
                session.scalar(
                    select(func.count()).where(OpportunityRecord.conversation_id == conversation_id)
                )
                or 0
            )

            return _serialize_conversation(
                updated,
                message_count=message_count,
                opportunity_count=opportunity_count,
            )

    def _refresh_conversation_ai(self, conversation_id: str) -> dict[str, Any] | None:
        settings = OpenAISettings.from_env()
        if settings is None:
            raise RuntimeError("OPENAI_API_KEY is missing")

        with self.session_factory() as session:
            analyzer = OpenAIConversationAnalyzer(
                settings,
                profile=FreelancerProfile.from_snapshot(get_profile_snapshot(session)),
            )
            conversation = get_conversation(session, conversation_id)
            if conversation is None:
                return None

            messages = list(list_messages_for_conversation(session, conversation_id))
            opportunities = list(list_opportunities_for_conversation(session, conversation_id))
            analysis = analyzer.analyze(
                conversation={
                    "id": conversation.id,
                    "client_name": conversation.client_name,
                    "freelancer_name": analyzer.profile.name,
                    "last_message": conversation.last_message,
                    "status": conversation.status,
                    "priority": conversation.priority,
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
            if (
                conversation.archived_at is None
                and analysis.workflow_status == AIWorkflowStatus.A_REPONDRE
                and conversation.manual_workflow_status == AIWorkflowStatus.ATTENTE_REPONSE.value
            ):
                conversation.manual_workflow_status = None
                conversation.manual_next_action = None
            updated = update_conversation_ai(
                session,
                conversation_id,
                analysis,
                analyzed_at=_utcnow(),
            )
            if updated is None:
                return None

            return {
                "conversation": _serialize_conversation(
                    updated,
                    message_count=len(messages),
                    opportunity_count=len(opportunities),
                ),
                "messages": [_serialize_message(item) for item in messages],
                "opportunities": [_serialize_opportunity(item) for item in opportunities],
            }

    def _update_opportunity_fields(
        self,
        opportunity_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        with self.session_factory() as session:
            updated = update_opportunity_crm(
                session,
                opportunity_id,
                archived=payload.get("archived"),
            )
            if updated is None:
                return None
            return _serialize_opportunity(updated)

    def _refresh_opportunity_ai(self, opportunity_id: str) -> dict[str, Any] | None:
        settings = OpenAISettings.from_env()
        if settings is None:
            raise RuntimeError("OPENAI_API_KEY is missing")

        with self.session_factory() as session:
            analyzer = OpenAIConversationAnalyzer(
                settings,
                profile=FreelancerProfile.from_snapshot(get_profile_snapshot(session)),
            )
            opportunity = get_opportunity(session, opportunity_id)
            if opportunity is None:
                return None

            analysis = analyzer.analyze_opportunity(
                opportunity={
                    "id": opportunity.id,
                    "title": opportunity.title,
                    "budget": opportunity.budget,
                    "description": opportunity.description,
                }
            )
            updated = update_opportunity_ai(
                session,
                opportunity_id,
                analysis,
                analyzed_at=_utcnow(),
            )
            if updated is None:
                return None

            linked_conversation = (
                get_conversation(session, updated.conversation_id)
                if updated.conversation_id
                else None
            )

            return {
                "opportunity": _serialize_opportunity(updated),
                "conversation": (
                    _serialize_conversation(linked_conversation)
                    if linked_conversation is not None
                    else None
                ),
            }

    @staticmethod
    def _read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
        content_length = int(handler.headers.get("Content-Length", "0") or "0")
        if content_length <= 0:
            return {}

        raw = handler.rfile.read(content_length)
        if not raw:
            return {}

        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    @staticmethod
    def _send_html(
        handler: BaseHTTPRequestHandler,
        content: str,
        *,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        body = content.encode("utf-8")
        handler.send_response(status.value)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    @staticmethod
    def _send_file(
        handler: BaseHTTPRequestHandler,
        file_path: Path,
        *,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        body = file_path.read_bytes()
        content_type, _ = mimetypes.guess_type(file_path.name)
        handler.send_response(status.value)
        handler.send_header("Content-Type", content_type or "application/octet-stream")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    @staticmethod
    def _send_json(
        handler: BaseHTTPRequestHandler,
        payload: Any,
        *,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(status.value)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    @staticmethod
    def _send_empty(
        handler: BaseHTTPRequestHandler,
        *,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        handler.send_response(status.value)
        handler.send_header("Content-Length", "0")
        handler.end_headers()


def default_config() -> DashboardConfig:
    """Build the default runtime configuration from the current project layout."""

    project_root = Path(__file__).resolve().parent.parent
    return DashboardConfig(
        project_root=project_root,
        database_path=project_root / ".local" / "malt_crm.sqlite3",
        cookie_path=project_root / ".local" / "cookies.local.json",
    )


def main() -> None:
    """Run the local dashboard server."""

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = default_config()
    load_project_env(config.project_root)
    app = DashboardApp(config)
    app.start()


DASHBOARD_HTML = (ASSETS_DIR / "dashboard.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    main()
