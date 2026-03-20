from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_launcher_module():
    launcher_path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("launcher_main", launcher_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_load_cookie_store_returns_empty_mapping_for_invalid_json(tmp_path, monkeypatch) -> None:
    launcher = _load_launcher_module()
    cookie_store = tmp_path / "cookies.local.json"
    cookie_store.write_text("{invalid", encoding="utf-8")
    monkeypatch.setattr(launcher, "COOKIE_STORE", cookie_store)

    assert launcher._load_cookie_store() == {}


def test_load_cookie_store_returns_cookie_mapping(tmp_path, monkeypatch) -> None:
    launcher = _load_launcher_module()
    cookie_store = tmp_path / "cookies.local.json"
    cookie_store.write_text('{"remember-me": "token"}', encoding="utf-8")
    monkeypatch.setattr(launcher, "COOKIE_STORE", cookie_store)

    assert launcher._load_cookie_store() == {"remember-me": "token"}
