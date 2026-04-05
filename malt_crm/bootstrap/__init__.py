"""Bootstrap : HTML minimal, sync initiale SQLite + IA optionnelle (logique dans ``dashboard``)."""

from __future__ import annotations

from .sync import is_forbidden_cookie_error, run_initial_sync

__all__ = ["is_forbidden_cookie_error", "run_initial_sync"]
