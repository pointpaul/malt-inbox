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


def test_main_runs_dashboard_when_port_free(monkeypatch) -> None:
    launcher = _load_launcher_module()

    run_calls: list[int] = []

    class DummyApp:
        def __init__(self, config):  # noqa: ANN001
            pass

        def run(self) -> None:
            run_calls.append(1)

    monkeypatch.setattr(launcher, "_dashboard_is_running", lambda: False)
    monkeypatch.setattr(launcher, "default_config", lambda: object())
    monkeypatch.setattr(launcher, "DashboardApp", DummyApp)
    monkeypatch.setattr(launcher, "load_project_env", lambda project_root: None)

    launcher.main()

    assert run_calls == [1]


def test_main_skips_when_dashboard_running(monkeypatch) -> None:
    launcher = _load_launcher_module()

    run_calls: list[int] = []

    class DummyApp:
        def __init__(self, config):  # noqa: ANN001
            pass

        def run(self) -> None:
            run_calls.append(1)

    monkeypatch.setattr(launcher, "_dashboard_is_running", lambda: True)
    monkeypatch.setattr(launcher, "DashboardApp", DummyApp)
    monkeypatch.setattr(launcher, "load_project_env", lambda project_root: None)

    launcher.main()

    assert run_calls == []


def test_is_forbidden_cookie_error_detects_403() -> None:
    from malt_crm.bootstrap import is_forbidden_cookie_error

    assert is_forbidden_cookie_error(RuntimeError("failed with status 403")) is True
    assert is_forbidden_cookie_error(RuntimeError("failed with status 500")) is False
