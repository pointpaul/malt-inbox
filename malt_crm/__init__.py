"""Malt Inbox — code applicatif du CRM local (package Python `malt_crm`).

Le nom de distribution installable est `malt-crm-inbox` (voir `pyproject.toml`).
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("malt-crm-inbox")
except PackageNotFoundError:
    __version__ = "0.0.0"

__all__ = ["__version__"]

