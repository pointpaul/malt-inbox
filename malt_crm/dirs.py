"""Répertoires de données locales (SQLite sous ``project_root/.local``)."""

from __future__ import annotations

from pathlib import Path


def malt_local_dir(project_root: Path) -> Path:
    """Retourne ``project_root/.local``, créé si nécessaire."""

    d = (project_root / ".local").resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d
