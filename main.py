"""Lance Malt Inbox en local : une seule app (paramètres, 1ʳᵉ sync, dashboard)."""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path

from malt_crm.constants import DEFAULT_HOST, DEFAULT_PORT, public_url_host
from malt_crm.dashboard import DashboardApp, default_config
from malt_crm.env import load_project_env

PROJECT_ROOT = Path(__file__).resolve().parent


def _dashboard_url() -> str:
    return f"http://{public_url_host(bind_host=DEFAULT_HOST)}:{DEFAULT_PORT}"


def _dashboard_is_running() -> bool:
    probe_host = "127.0.0.1" if DEFAULT_HOST == "0.0.0.0" else DEFAULT_HOST
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((probe_host, DEFAULT_PORT)) == 0


def main() -> None:
    """Lance le serveur local : bootstrap et CRM sur le même port."""

    logging.basicConfig(
        level=os.getenv("MALT_CRM_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    load_project_env(PROJECT_ROOT)

    if _dashboard_is_running():
        print(f"Malt Inbox déjà actif sur {_dashboard_url()}")
        return

    print("")
    print("Malt Inbox")
    print(_dashboard_url())
    print("")
    DashboardApp(default_config()).run()


if __name__ == "__main__":
    main()
