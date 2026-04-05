from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from malt_crm.dashboard import DashboardApp, DashboardConfig


def test_dashboard_index_returns_html(tmp_path: Path) -> None:
    cfg = DashboardConfig(
        project_root=tmp_path,
        database_path=tmp_path / "malt_crm.sqlite3",
        env_path=tmp_path / ".env",
        host="127.0.0.1",
        port=18765,
    )
    app = DashboardApp(cfg)
    app._initial_sync_done = True
    client = TestClient(app.build_fastapi_app())
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert len(response.text) > 100
