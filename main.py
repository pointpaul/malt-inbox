"""Point d'entrée minimal du dashboard local."""

from __future__ import annotations

import json
import os
import socket
import webbrowser
from pathlib import Path

from malt_crm.ai import OpenAISettings
from malt_crm.api import MaltAPIClient
from malt_crm.dashboard import DashboardApp, default_config
from malt_crm.db import create_session_factory
from malt_crm.env import load_project_env
from malt_crm.sync import MaltSyncService, SyncReport

PROJECT_ROOT = Path(__file__).resolve().parent
LOCAL_DIR = PROJECT_ROOT / ".local"
ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
COOKIE_STORE = LOCAL_DIR / "cookies.local.json"


def _dashboard_url() -> str:
    return f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"


def _dashboard_is_running() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((DEFAULT_HOST, DEFAULT_PORT)) == 0


def _load_cookie_store() -> dict[str, str]:
    if not COOKIE_STORE.exists():
        return {}
    try:
        payload = json.loads(COOKIE_STORE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): str(value) for key, value in payload.items()}


def _ensure_cookie_store() -> None:
    LOCAL_DIR.mkdir(exist_ok=True)
    cookies = _load_cookie_store()
    if cookies.get("remember-me", "").strip():
        return

    print("")
    print("Connexion Malt")
    print("Entre le cookie `remember-me` depuis ton navigateur Malt.")
    cookie_value = input("remember-me: ").strip()
    if not cookie_value:
        raise SystemExit("Cookie `remember-me` manquant.")

    COOKIE_STORE.write_text(
        json.dumps({"remember-me": cookie_value}, indent=2),
        encoding="utf-8",
    )


def _ensure_openai_key() -> None:
    if OpenAISettings.from_env() is not None:
        return

    print("")
    print("Fonctionnalités IA optionnelles")
    print("Ajoute une clé OpenAI pour les résumés et réponses suggérées.")
    print("Tu peux laisser vide pour continuer sans IA.")
    api_key = input("OPENAI_API_KEY (optionnel): ").strip()
    if not api_key:
        print("Aucune clé OpenAI configurée. Le dashboard fonctionnera sans IA.")
        return

    existing_lines: list[str] = []
    if ENV_PATH.exists():
        existing_lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
        existing_lines = [
            line for line in existing_lines
            if not line.strip().startswith("OPENAI_API_KEY=")
        ]
    existing_lines.append(f"OPENAI_API_KEY={api_key}")
    ENV_PATH.write_text("\n".join(existing_lines).strip() + "\n", encoding="utf-8")
    os.environ["OPENAI_API_KEY"] = api_key
    print("Clé OpenAI enregistrée dans .env.")


def _render_progress(completed: int, total: int) -> None:
    if total <= 0:
        print("  Aucune conversation à analyser.")
        return

    width = 28
    ratio = completed / total
    filled = min(width, int(width * ratio))
    bar = "#" * filled + "-" * (width - filled)
    end = "\n" if completed >= total else ""
    print(f"\r  [{bar}] {completed}/{total}", end=end, flush=True)


def _run_initial_sync() -> tuple[SyncReport, int]:
    print("")
    print("Synchronisation initiale")

    client = MaltAPIClient.from_cookies(cookies_json_path=COOKIE_STORE)
    session_factory = create_session_factory(PROJECT_ROOT / ".local" / "malt_crm.sqlite3")
    service = MaltSyncService(client, session_factory)
    cookie_count = len([value for value in _load_cookie_store().values() if value.strip()])

    print("- Profil Malt...")
    profile_report = service.sync_profile()
    if profile_report.profile_refreshes:
        print("  Profil récupéré.")
    else:
        print("  Profil indisponible ou non rafraîchi.")

    print("- Conversations et opportunités...")
    conversation_report = service.sync_conversations()
    print(
        f"  {conversation_report.conversations} conversations, "
        f"{conversation_report.opportunities} opportunités."
    )

    print("- Messages...")
    message_report = service.sync_messages()
    print(f"  {message_report.messages} messages.")

    report = profile_report.merge(conversation_report).merge(message_report)

    if OpenAISettings.from_env() is None:
        print("- IA désactivée (aucune clé OpenAI configurée).")
    else:
        print("- Analyse IA...")
        try:
            ai_report = service.sync_ai(
                max_workers=20,
                progress_callback=_render_progress,
            )
        except Exception as exc:
            print(f"  Analyse IA indisponible: {exc}")
        else:
            report.merge(ai_report)
            print(f"  {ai_report.ai_analyses} éléments analysés par l'IA.")
    return report, cookie_count


def main() -> None:
    """Lance le dashboard local avec un cookie Malt stocké localement."""

    load_project_env(PROJECT_ROOT)

    if _dashboard_is_running():
        webbrowser.open(_dashboard_url())
        return

    _ensure_cookie_store()
    _ensure_openai_key()
    report, cookie_count = _run_initial_sync()

    config = default_config()
    app = DashboardApp(config)
    app.sync_manager.record_completed_sync(report=report, cookie_count=cookie_count)
    print("")
    print(f"Dashboard prêt sur {_dashboard_url()}")
    app.start(skip_initial_sync=True)


if __name__ == "__main__":
    main()
