"""Synchronisation initiale lancée avant le dashboard principal."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from malt_crm.ai import OpenAISettings
from malt_crm.api import MaltAPIClient
from malt_crm.constants import REMEMBER_ME_ENV_KEY
from malt_crm.db import create_session_factory
from malt_crm.dirs import malt_local_dir
from malt_crm.sync import MaltSyncService, SyncReport


def is_forbidden_cookie_error(exc: Exception) -> bool:
    return "status 403" in str(exc).lower()


def render_cli_ai_progress(completed: int, total: int) -> None:
    if total <= 0:
        print("  Aucune conversation à analyser.")
        return

    width = 28
    ratio = completed / total
    filled = min(width, int(width * ratio))
    bar = "#" * filled + "-" * (width - filled)
    end = "\n" if completed >= total else ""
    print(f"\r  [{bar}] {completed}/{total}", end=end, flush=True)


def run_initial_sync(
    project_root: Path,
    progress_callback: Callable[..., None] | None = None,
    *,
    remember_me_env_key: str = REMEMBER_ME_ENV_KEY,
) -> tuple[SyncReport, int]:
    def _notify(stage: str, detail: str) -> None:
        if progress_callback is None:
            return
        progress_callback(stage=stage, detail=detail)

    print("")
    print("Synchronisation initiale")
    _notify("sync", "Initialisation…")

    remember_me = os.getenv(remember_me_env_key, "").strip()
    if not remember_me:
        raise RuntimeError("Cookie remember-me manquant.")
    client = MaltAPIClient.from_cookies(cookies={"remember-me": remember_me})
    session_factory = create_session_factory(malt_local_dir(project_root) / "malt_crm.sqlite3")
    service = MaltSyncService(client, session_factory)
    cookie_count = 1

    print("- Profil Malt...")
    _notify("profile", "Récupération du profil Malt…")
    profile_report = service.sync_profile()
    if profile_report.profile_refreshes:
        print("  Profil récupéré.")
        _notify("profile", "Profil récupéré.")
    else:
        print("  Profil indisponible ou non rafraîchi.")
        _notify("profile", "Profil indisponible ou non rafraîchi.")

    print("- Conversations et opportunités...")
    _notify("conversations", "Synchronisation des conversations et opportunités…")
    conversation_report = service.sync_conversations()
    print(
        f"  {conversation_report.conversations} conversations, "
        f"{conversation_report.opportunities} opportunités."
    )
    _notify(
        "conversations",
        f"{conversation_report.conversations} conversations, {conversation_report.opportunities} opportunités.",
    )

    print("- Messages...")
    _notify("messages", "Synchronisation des messages…")
    message_report = service.sync_messages()
    print(f"  {message_report.messages} messages.")
    _notify("messages", f"{message_report.messages} messages synchronisés.")

    report = profile_report.merge(conversation_report).merge(message_report)

    if OpenAISettings.from_env() is None:
        print("- IA désactivée (aucune clé OpenAI configurée).")
        _notify("ai", "IA désactivée (aucune clé OpenAI configurée).")
    else:
        print("- Analyse IA...")
        _notify("ai", "Analyse IA en cours…")
        try:
            ai_report = service.sync_ai(
                max_workers=20,
                progress_callback=render_cli_ai_progress,
            )
        except Exception as exc:
            print(f"  Analyse IA indisponible: {exc}")
            _notify("ai", f"Analyse IA indisponible: {exc}")
        else:
            report.merge(ai_report)
            print(f"  {ai_report.ai_analyses} éléments analysés par l'IA.")
            _notify("ai", f"{ai_report.ai_analyses} éléments analysés par l'IA.")
    _notify("done", "Synchronisation terminée.")
    return report, cookie_count
