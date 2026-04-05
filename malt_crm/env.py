"""Helpers to read and update local .env values."""

from __future__ import annotations

import os
from pathlib import Path


def load_project_env(project_root: Path) -> None:
    """Load simple KEY=VALUE pairs from a local .env file if present."""

    env_path = project_root / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip("'").strip('"')


def upsert_env_value(env_path: Path, key: str, value: str | None) -> None:
    """Insert or replace a KEY=VALUE entry in a .env file and process env."""
    existing_lines: list[str] = []
    if env_path.exists():
        existing_lines = env_path.read_text(encoding="utf-8").splitlines()
        existing_lines = [line for line in existing_lines if not line.strip().startswith(f"{key}=")]

    if value:
        existing_lines.append(f"{key}={value}")

    content = "\n".join(existing_lines).strip()
    if content:
        env_path.write_text(content + "\n", encoding="utf-8")
    elif env_path.exists():
        env_path.unlink()

    if value:
        os.environ[key] = value
    else:
        os.environ.pop(key, None)
