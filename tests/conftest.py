"""Réglages communs aux tests."""

from __future__ import annotations

import webbrowser

import pytest


@pytest.fixture(autouse=True)
def _no_browser_during_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Évite d'ouvrir une fenêtre à chaque TestClient / lifespan."""

    monkeypatch.setattr(webbrowser, "open", lambda *args, **kwargs: True)
