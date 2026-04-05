"""Dashboard FastAPI : API JSON, fichiers statiques, sync Malt en arrière-plan."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import webbrowser
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

import uvicorn
from fastapi import Body, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse as StarletteJSONResponse

from .ai import (
    FOLLOW_UP_DELAY_DAYS,
    FreelancerProfile,
    OpenAIConversationAnalyzer,
    OpenAISettings,
)
from .api import MaltAPIClient, MaltAPIError
from .bootstrap.html import PROGRESS_PAGE_BYTES, render_settings_html
from .bootstrap.sync import is_forbidden_cookie_error, run_initial_sync
from .constants import DEFAULT_HOST, DEFAULT_PORT, REMEMBER_ME_ENV_KEY, public_url_host
from .db import (
    ConversationRecord,
    MessageRecord,
    OpportunityRecord,
    TimelineEventRecord,
    append_timeline_event,
    create_session_factory,
    get_conversation,
    get_opportunity,
    get_profile_snapshot,
    list_conversations,
    list_messages_for_conversation,
    list_opportunities,
    list_opportunities_for_conversation,
    list_timeline_for_conversation,
    max_budget_by_conversation_ids,
    update_conversation_ai,
    update_conversation_crm,
    update_opportunity_ai,
    update_opportunity_crm,
)
from .dirs import malt_local_dir
from .env import load_project_env, upsert_env_value
from .models import AIWorkflowStatus
from .scoring import conversation_smart_tier, conversation_strength, opportunity_strength
from .sync import MaltSyncService, SyncReport

LOGGER = logging.getLogger(__name__)

DEFAULT_SYNC_INTERVAL_SECONDS = 30 * 60
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
_BOOTSTRAP_STAGE_PERCENT = {"sync": 10, "profile": 25, "conversations": 50, "messages": 70, "ai": 90, "done": 100}


class UnicodeJSONResponse(StarletteJSONResponse):
    """JSON UTF-8 lisible ; `Cache-Control: no-store` comme l’ancien serveur."""

    def __init__(
        self,
        content: Any = None,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        merged = {**(headers or {})}
        merged.setdefault("Cache-Control", "no-store")
        super().__init__(content, status_code=status_code, headers=merged, **kwargs)

    def render(self, content: Any) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")


@dataclass(frozen=True)
class DashboardConfig:
    """Runtime configuration for the local dashboard."""

    project_root: Path
    database_path: Path
    env_path: Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    sync_interval_seconds: int = DEFAULT_SYNC_INTERVAL_SECONDS


@dataclass
class SyncStatus:
    """In-memory status for the sync worker."""

    running: bool = False
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    next_run_at: datetime | None = None
    last_error: str | None = None
    last_report: SyncReport | None = None
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


def _serialize_timeline_event(record: TimelineEventRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "kind": record.kind,
        "title": record.title,
        "detail": record.detail,
        "created_at": record.created_at.isoformat(),
    }


def _serialize_conversation(
    record: ConversationRecord,
    *,
    message_count: int = 0,
    opportunity_count: int = 0,
    max_linked_budget: float | None = None,
) -> dict[str, Any]:
    follow_up_due = _follow_up_due(record)
    reply_draft = record.ai_reply_draft
    if follow_up_due and not reply_draft:
        reply_draft = _follow_up_reply(record)
    eff_wf = _effective_workflow_status(record)
    budget = float(max_linked_budget or 0) or None
    tier = conversation_smart_tier(
        effective_workflow=eff_wf,
        ai_urgency=record.ai_urgency,
        ai_category=record.ai_category,
        ai_needs_reply=record.ai_needs_reply,
        priority=record.priority,
        follow_up_due=follow_up_due,
        max_linked_budget=budget,
    )
    strength = conversation_strength(
        effective_workflow=eff_wf,
        ai_urgency=record.ai_urgency,
        ai_category=record.ai_category,
        ai_needs_reply=record.ai_needs_reply,
        ai_confidence=record.ai_confidence,
        max_linked_budget=budget,
        message_count=message_count,
    )
    return {
        "id": record.id,
        "client_name": record.client_name,
        "last_message": record.last_message,
        "updated_at": record.updated_at.isoformat(),
        "status": record.status,
        "priority": record.priority,
        "message_count": message_count,
        "opportunity_count": opportunity_count,
        "workflow_status": eff_wf,
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
        "reminder_due_at": (
            record.reminder_due_at.isoformat() if record.reminder_due_at else None
        ),
        "ai_last_analyzed_at": (
            record.ai_last_analyzed_at.isoformat()
            if record.ai_last_analyzed_at
            else None
        ),
        "smart_tier": tier,
        "strength": strength,
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
    strength = opportunity_strength(record)
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
        "strength": strength,
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


def load_stored_cookies() -> int:
    """Count remember-me cookies configured via environment."""
    return 1 if os.getenv(REMEMBER_ME_ENV_KEY, "").strip() else 0


def _load_cookie_value() -> str:
    return os.getenv(REMEMBER_ME_ENV_KEY, "").strip()


def _build_api_client(config: DashboardConfig) -> MaltAPIClient:
    """Create a Malt API client from remember-me environment value."""
    remember_me = _load_cookie_value()
    return MaltAPIClient.from_cookies(cookies={"remember-me": remember_me})


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
            cookie_count = load_stored_cookies()
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
    """Une seule app FastAPI : bootstrap (settings + 1ʳᵉ sync) puis CRM."""

    def __init__(self, config: DashboardConfig) -> None:
        self.config = config
        self.session_factory = create_session_factory(config.database_path)
        self.sync_manager = SyncManager(config)
        self._initial_sync_done = False
        self._sync_loop_started = False
        self._bootstrap_lock = Lock()
        self._bootstrap_sync_running = False
        pub = public_url_host(bind_host=config.host)
        self._bootstrap_state: dict[str, Any] = {
            "phase": "settings",
            "stage": "sync",
            "detail": "En attente de configuration.",
            "status": "idle",
            "done": False,
            "percent": 10,
            "redirect_url": f"http://{pub}:{config.port}",
            "error_message": None,
        }
        if _load_cookie_value():
            self._bootstrap_maybe_start_initial_sync()

    def _bootstrap_settings_html(self, error_message: str | None) -> str:
        existing_cookie = _load_cookie_value()
        existing_openai_key = os.getenv("OPENAI_API_KEY", "").strip()
        remember_placeholder = (
            "déjà configuré (tu peux le remplacer)"
            if existing_cookie
            else "colle la valeur remember-me"
        )
        openai_placeholder = (
            "déjà configurée (tu peux la remplacer)"
            if existing_openai_key
            else "sk-..."
        )
        return render_settings_html(
            error_message=error_message,
            remember_placeholder=remember_placeholder,
            openai_placeholder=openai_placeholder,
        )

    def _bootstrap_set_sync_error(self, message: str) -> None:
        with self._bootstrap_lock:
            self._bootstrap_state["phase"] = "settings"
            self._bootstrap_state["status"] = "error"
            self._bootstrap_state["detail"] = message
            self._bootstrap_state["error_message"] = message
            self._bootstrap_state["done"] = False

    def _bootstrap_progress_notify(self, *, stage: str, detail: str) -> None:
        with self._bootstrap_lock:
            self._bootstrap_state["stage"] = stage
            self._bootstrap_state["detail"] = detail
            self._bootstrap_state["percent"] = _BOOTSTRAP_STAGE_PERCENT.get(stage, 10)
            self._bootstrap_state["phase"] = "progress"

    def _bootstrap_maybe_start_initial_sync(self) -> None:
        with self._bootstrap_lock:
            if self._bootstrap_sync_running or self._initial_sync_done:
                return
            self._bootstrap_sync_running = True

        def _runner() -> None:
            try:
                with self._bootstrap_lock:
                    self._bootstrap_state["phase"] = "progress"
                    self._bootstrap_state["status"] = "running"
                    self._bootstrap_state["detail"] = "Initialisation…"
                    self._bootstrap_state["error_message"] = None
                    self._bootstrap_state["done"] = False
                report, cookie_count = run_initial_sync(
                    self.config.project_root,
                    progress_callback=self._bootstrap_progress_notify,
                    remember_me_env_key=REMEMBER_ME_ENV_KEY,
                )
            except MaltAPIError as exc:
                message = (
                    "Cookie remember-me invalide ou expiré (403)."
                    if is_forbidden_cookie_error(exc)
                    else str(exc)
                )
                self._bootstrap_set_sync_error(message)
            except Exception as exc:
                self._bootstrap_set_sync_error(str(exc))
            else:
                with self._bootstrap_lock:
                    self._bootstrap_state["phase"] = "progress"
                    self._bootstrap_state["stage"] = "done"
                    self._bootstrap_state["status"] = "success"
                    self._bootstrap_state["detail"] = "Synchronisation terminée. Démarrage du CRM…"
                    self._bootstrap_state["done"] = True
                    self._bootstrap_state["percent"] = 100
                self._complete_initial_sync(report, cookie_count)
            finally:
                with self._bootstrap_lock:
                    self._bootstrap_sync_running = False

        Thread(target=_runner, daemon=True).start()

    def _complete_initial_sync(self, report: SyncReport, cookie_count: int) -> None:
        self.sync_manager.record_completed_sync(report=report, cookie_count=cookie_count)
        with self._bootstrap_lock:
            self._initial_sync_done = True
            should_start = not self._sync_loop_started
            if should_start:
                self._sync_loop_started = True
        if should_start:
            self.sync_manager.start(skip_initial_sync=True)

    def build_fastapi_app(self) -> FastAPI:
        d = self

        @asynccontextmanager
        async def lifespan(_app: FastAPI):
            url = f"http://{public_url_host(bind_host=d.config.host)}:{d.config.port}/"

            async def _open_when_ready() -> None:
                await asyncio.sleep(0.7)
                try:
                    ok = webbrowser.open(url)
                except Exception as exc:
                    LOGGER.warning("Ouverture du navigateur impossible : %s", exc)
                    ok = False
                if not ok:
                    LOGGER.info("Ouvre cette URL dans le navigateur : %s", url)

            asyncio.create_task(_open_when_ready())
            yield

        app = FastAPI(
            title="Malt Inbox",
            default_response_class=UnicodeJSONResponse,
            lifespan=lifespan,
        )
        self._register_routes(app)
        return app

    def run(self) -> None:
        """Lance uvicorn (bloquant) : bootstrap et CRM partagent la même app."""
        app = self.build_fastapi_app()
        url = f"http://{public_url_host(bind_host=self.config.host)}:{self.config.port}"
        LOGGER.info("Malt Inbox — %s", url)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=self.config.host,
                port=self.config.port,
                log_level="info",
            )
        )
        try:
            server.run()
        finally:
            self.sync_manager.stop()

    def _register_routes(self, app: FastAPI) -> None:
        d = self

        app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR.resolve())), name="assets")

        @app.exception_handler(RequestValidationError)
        def _validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
            return JSONResponse(
                {"error": "Paramètres de requête invalides."},
                status_code=422,
            )

        @app.exception_handler(StarletteHTTPException)
        def _http_exception_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
            if exc.status_code == 404:
                return JSONResponse({"error": "Not found"}, status_code=404)
            detail: Any = exc.detail
            if isinstance(detail, str):
                return JSONResponse({"error": detail}, status_code=exc.status_code)
            return JSONResponse(detail, status_code=exc.status_code)

        @app.get("/favicon.ico")
        def _favicon() -> Response:
            return Response(status_code=204)

        @app.api_route("/", methods=["GET", "HEAD"], response_model=None)
        def _root() -> Response | HTMLResponse:
            if d._initial_sync_done:
                return HTMLResponse(DASHBOARD_HTML)
            with d._bootstrap_lock:
                phase = d._bootstrap_state.get("phase", "settings")
                err = d._bootstrap_state.get("error_message")
            if phase == "progress":
                return Response(content=PROGRESS_PAGE_BYTES, media_type="text/html; charset=utf-8")
            return HTMLResponse(d._bootstrap_settings_html(str(err) if err else None))

        @app.api_route("/settings", methods=["GET", "HEAD"], response_model=None)
        def _settings() -> Response | HTMLResponse:
            if d._initial_sync_done:
                return HTMLResponse(SETTINGS_HTML)
            with d._bootstrap_lock:
                err = d._bootstrap_state.get("error_message")
            return HTMLResponse(d._bootstrap_settings_html(str(err) if err else None))

        @app.get("/progress", response_model=None)
        def _progress_page() -> Response | RedirectResponse:
            if d._initial_sync_done:
                return RedirectResponse("/", status_code=302)
            return Response(content=PROGRESS_PAGE_BYTES, media_type="text/html; charset=utf-8")

        @app.get("/api/status")
        def _api_status() -> dict[str, Any]:
            return d._build_status_payload()

        @app.get("/api/progress")
        def _api_progress() -> dict[str, Any]:
            if d._initial_sync_done:
                return {
                    "stage": "done",
                    "detail": "Synchronisation terminée.",
                    "status": "success",
                    "done": True,
                    "percent": 100,
                    "redirect_url": "/",
                }
            with d._bootstrap_lock:
                return dict(d._bootstrap_state)

        @app.get("/api/settings")
        def _api_settings_get() -> dict[str, Any]:
            return d._build_settings_payload()

        @app.post("/api/settings", response_model=None)
        def _api_settings_post(
            payload: dict[str, Any] = Body(...),
        ) -> JSONResponse | dict[str, bool]:
            if not d._initial_sync_done:
                remember_me = str(payload.get("remember_me", "")).strip()
                openai_key = str(payload.get("openai_api_key", "")).strip()
                if not remember_me:
                    return JSONResponse(
                        {"error": "Le cookie remember-me est obligatoire."},
                        status_code=400,
                    )
                d._save_settings(
                    remember_me=remember_me,
                    openai_api_key=openai_key if openai_key else None,
                )
                with d._bootstrap_lock:
                    d._bootstrap_state["phase"] = "progress"
                    d._bootstrap_state["status"] = "running"
                    d._bootstrap_state["error_message"] = None
                d._bootstrap_maybe_start_initial_sync()
                return JSONResponse({"ok": True, "redirect": "/progress"})
            try:
                remember_me = str(payload.get("remember_me", "")).strip()
                openai_api_key = str(payload.get("openai_api_key", "")).strip()
                if not remember_me:
                    raise ValueError("Le cookie remember-me est obligatoire.")
                d._save_settings(
                    remember_me=remember_me,
                    openai_api_key=openai_api_key if openai_api_key else None,
                )
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            return {"ok": True}

        @app.get("/api/conversations")
        def _api_conversations(
            limit: int = Query(100, ge=1, le=500),
            q: str = "",
        ) -> list[dict[str, Any]]:
            return d._load_conversations(limit=limit, query=q.strip().lower())

        @app.post("/api/conversations/{conversation_id}/ai-refresh", response_model=None)
        def _api_conv_ai_refresh(conversation_id: str) -> JSONResponse | dict[str, Any]:
            try:
                out = d._refresh_conversation_ai(conversation_id)
            except RuntimeError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            if out is None:
                return JSONResponse({"error": "Conversation not found"}, status_code=404)
            return out

        @app.post("/api/conversations/{conversation_id}/crm", response_model=None)
        def _api_conv_crm(
            conversation_id: str,
            payload: dict[str, Any] = Body(...),
        ) -> JSONResponse | dict[str, Any]:
            try:
                updated = d._update_conversation_fields(conversation_id, payload)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            if updated is None:
                return JSONResponse({"error": "Conversation not found"}, status_code=404)
            return updated

        @app.post("/api/conversations/{conversation_id}/actions", response_model=None)
        def _api_conv_actions(
            conversation_id: str,
            payload: dict[str, Any] = Body(...),
        ) -> JSONResponse | dict[str, Any]:
            action = str(payload.get("action", "")).strip()
            try:
                updated = d._conversation_quick_action(conversation_id, action)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            if updated is None:
                return JSONResponse({"error": "Conversation not found"}, status_code=404)
            return updated

        @app.get("/api/conversations/{conversation_id}", response_model=None)
        def _api_conversation_detail(conversation_id: str) -> JSONResponse | dict[str, Any]:
            payload = d._load_conversation_detail(conversation_id)
            if payload is None:
                return JSONResponse({"error": "Conversation not found"}, status_code=404)
            return payload

        @app.get("/api/opportunities")
        def _api_opportunities() -> list[dict[str, Any]]:
            return d._load_opportunities(limit=100)

        @app.post("/api/opportunities/{opportunity_id}/ai-draft", response_model=None)
        def _api_opp_ai_draft(opportunity_id: str) -> JSONResponse | dict[str, Any]:
            try:
                out = d._refresh_opportunity_ai(opportunity_id)
            except RuntimeError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            if out is None:
                return JSONResponse({"error": "Opportunity not found"}, status_code=404)
            return out

        @app.post("/api/opportunities/{opportunity_id}/crm", response_model=None)
        def _api_opp_crm(
            opportunity_id: str,
            payload: dict[str, Any] = Body(...),
        ) -> JSONResponse | dict[str, Any]:
            try:
                updated = d._update_opportunity_fields(opportunity_id, payload)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            if updated is None:
                return JSONResponse({"error": "Opportunity not found"}, status_code=404)
            return updated

        @app.get("/api/opportunities/{opportunity_id}", response_model=None)
        def _api_opportunity_detail(opportunity_id: str) -> JSONResponse | dict[str, Any]:
            payload = d._load_opportunity(opportunity_id)
            if payload is None:
                return JSONResponse({"error": "Opportunity not found"}, status_code=404)
            return payload

        @app.get("/api/messages/{conversation_id}")
        def _api_messages(conversation_id: str) -> list[dict[str, Any]]:
            return d._load_messages(conversation_id)

        @app.post("/api/sync", response_model=None)
        def _api_sync() -> JSONResponse:
            started = d.sync_manager.trigger_sync()
            code = 202 if started else 409
            return JSONResponse({"started": started}, status_code=code)

    def _build_status_payload(self) -> dict[str, Any]:
        with self.session_factory() as session:
            profile = get_profile_snapshot(session)
        return {
            "sync": self.sync_manager.snapshot(),
            "profile": _serialize_profile(profile),
        }

    def _build_settings_payload(self) -> dict[str, Any]:
        return {
            "remember_me": _load_cookie_value(),
            "has_openai_api_key": bool(os.getenv("OPENAI_API_KEY", "").strip()),
        }

    def _save_settings(self, *, remember_me: str, openai_api_key: str | None = None) -> None:
        upsert_env_value(self.config.env_path, REMEMBER_ME_ENV_KEY, remember_me)
        if openai_api_key is not None:
            upsert_env_value(self.config.env_path, "OPENAI_API_KEY", openai_api_key or None)

    def _load_conversations(self, *, limit: int, query: str) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            items = list(list_conversations(session, limit=limit))
            conversation_ids = [item.id for item in items]
            message_counts: dict[str, int] = {}
            opportunity_counts: dict[str, int] = {}
            budget_by_conv: dict[str, float] = {}

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
                budget_by_conv = max_budget_by_conversation_ids(session, conversation_ids)

        rows = [
            _serialize_conversation(
                item,
                message_count=message_counts.get(item.id, 0),
                opportunity_count=opportunity_counts.get(item.id, 0),
                max_linked_budget=budget_by_conv.get(item.id),
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
            budget_map = max_budget_by_conversation_ids(session, [conversation_id])
            timeline = list_timeline_for_conversation(session, conversation_id)

            conv_payload = _serialize_conversation(
                conversation,
                message_count=len(messages),
                opportunity_count=len(opportunities),
                max_linked_budget=budget_map.get(conversation_id),
            )

        return {
            "conversation": conv_payload,
            "messages": [_serialize_message(item) for item in messages],
            "opportunities": [_serialize_opportunity(item) for item in opportunities],
            "timeline": [_serialize_timeline_event(ev) for ev in timeline],
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
            conv_payload = None
            if linked_conversation is not None:
                cid = linked_conversation.id
                mb = max_budget_by_conversation_ids(session, [cid]).get(cid)
                mc = int(
                    session.scalar(
                        select(func.count()).where(MessageRecord.conversation_id == cid)
                    )
                    or 0
                )
                oc = int(
                    session.scalar(
                        select(func.count()).where(OpportunityRecord.conversation_id == cid)
                    )
                    or 0
                )
                conv_payload = _serialize_conversation(
                    linked_conversation,
                    message_count=mc,
                    opportunity_count=oc,
                    max_linked_budget=mb,
                )

        return {
            "opportunity": _serialize_opportunity(opportunity),
            "conversation": conv_payload,
        }

    def _update_conversation_fields(
        self,
        conversation_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        old_wf: str | None = None
        had_archived: bool | None = None
        with self.session_factory() as session:
            cur = get_conversation(session, conversation_id)
            if cur is None:
                return None
            old_wf = _effective_workflow_status(cur)
            had_archived = cur.archived_at is not None

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

        new_wf = _effective_workflow_status(updated)
        if payload.get("manual_workflow_status") is not None and old_wf != new_wf:
            with self.session_factory() as s:
                append_timeline_event(
                    s,
                    conversation_id=conversation_id,
                    opportunity_id=None,
                    kind="status_change",
                    title=f"Statut CRM : {old_wf} → {new_wf}",
                )
                s.commit()

        if payload.get("archived") is not None:
            now_arch = updated.archived_at is not None
            if had_archived is not None and now_arch != had_archived:
                title = "Conversation archivée" if now_arch else "Conversation désarchivée"
                with self.session_factory() as s:
                    append_timeline_event(
                        s,
                        conversation_id=conversation_id,
                        opportunity_id=None,
                        kind="archive_toggle",
                        title=title,
                    )
                    s.commit()

        with self.session_factory() as session:
            fresh = get_conversation(session, conversation_id)
            if fresh is None:
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
            mb = max_budget_by_conversation_ids(session, [conversation_id]).get(conversation_id)
            return _serialize_conversation(
                fresh,
                message_count=message_count,
                opportunity_count=opportunity_count,
                max_linked_budget=mb,
            )

    def _conversation_quick_action(
        self,
        conversation_id: str,
        action: str,
    ) -> dict[str, Any] | None:
        if action == "message_sent":
            with self.session_factory() as s:
                append_timeline_event(
                    s,
                    conversation_id=conversation_id,
                    opportunity_id=None,
                    kind="message_sent",
                    title="Message envoyé ✓",
                    detail="Tu as confirmé l’envoi côté Malt.",
                )
                s.commit()
            with self.session_factory() as s:
                update_conversation_crm(
                    s,
                    conversation_id,
                    manual_workflow_status=AIWorkflowStatus.ATTENTE_REPONSE,
                    manual_next_action="En attente de réponse client.",
                    reminder_due_at=None,
                )
        elif action == "snooze_3d":
            due = _utcnow() + timedelta(days=3)
            label = due.strftime("%d/%m/%Y %H:%M")
            with self.session_factory() as s:
                append_timeline_event(
                    s,
                    conversation_id=conversation_id,
                    opportunity_id=None,
                    kind="snooze",
                    title="Rappel dans 3 jours",
                    detail=f"Relance prévue vers {label} (UTC).",
                )
                s.commit()
            with self.session_factory() as s:
                update_conversation_crm(
                    s,
                    conversation_id,
                    manual_workflow_status=AIWorkflowStatus.ATTENTE_REPONSE,
                    manual_next_action=f"Relancer le client (rappel {label} UTC)",
                    reminder_due_at=due,
                )
        else:
            raise ValueError("Action inconnue.")

        with self.session_factory() as session:
            fresh = get_conversation(session, conversation_id)
            if fresh is None:
                return None
            mc = int(
                session.scalar(
                    select(func.count()).where(MessageRecord.conversation_id == conversation_id)
                )
                or 0
            )
            oc = int(
                session.scalar(
                    select(func.count()).where(OpportunityRecord.conversation_id == conversation_id)
                )
                or 0
            )
            mb = max_budget_by_conversation_ids(session, [conversation_id]).get(conversation_id)
            return _serialize_conversation(
                fresh,
                message_count=mc,
                opportunity_count=oc,
                max_linked_budget=mb,
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

            mb = max_budget_by_conversation_ids(session, [conversation_id]).get(conversation_id)
            timeline = list_timeline_for_conversation(session, conversation_id)
            return {
                "conversation": _serialize_conversation(
                    updated,
                    message_count=len(messages),
                    opportunity_count=len(opportunities),
                    max_linked_budget=mb,
                ),
                "messages": [_serialize_message(item) for item in messages],
                "opportunities": [_serialize_opportunity(item) for item in opportunities],
                "timeline": [_serialize_timeline_event(ev) for ev in timeline],
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
            conv_out = None
            if linked_conversation is not None:
                cid = linked_conversation.id
                mb = max_budget_by_conversation_ids(session, [cid]).get(cid)
                mc = int(
                    session.scalar(
                        select(func.count()).where(MessageRecord.conversation_id == cid)
                    )
                    or 0
                )
                oc = int(
                    session.scalar(
                        select(func.count()).where(OpportunityRecord.conversation_id == cid)
                    )
                    or 0
                )
                conv_out = _serialize_conversation(
                    linked_conversation,
                    message_count=mc,
                    opportunity_count=oc,
                    max_linked_budget=mb,
                )

            return {
                "opportunity": _serialize_opportunity(updated),
                "conversation": conv_out,
            }


def default_config() -> DashboardConfig:
    """Build the default runtime configuration from the current project layout."""

    project_root = Path(__file__).resolve().parent.parent
    return DashboardConfig(
        project_root=project_root,
        database_path=malt_local_dir(project_root) / "malt_crm.sqlite3",
        env_path=project_root / ".env",
    )


def main() -> None:
    """Run the local dashboard server."""

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = default_config()
    load_project_env(config.project_root)
    DashboardApp(config).run()


DASHBOARD_HTML = (ASSETS_DIR / "dashboard.html").read_text(encoding="utf-8")
SETTINGS_HTML = (ASSETS_DIR / "settings.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    main()
