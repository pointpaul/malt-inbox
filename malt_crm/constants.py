"""Port, clés d’environnement et hôte affiché dans les URLs (logs / redirect).

Les `MALT_CRM_HOST` / `MALT_CRM_PUBLIC_HOST` sont lues au chargement du module.
"""

from __future__ import annotations

import os

DEFAULT_HOST = os.getenv("MALT_CRM_HOST", "127.0.0.1")
DEFAULT_PORT = 8765
REMEMBER_ME_ENV_KEY = "MALT_REMEMBER_ME"


def public_url_host(*, bind_host: str) -> str:
    """Calcule l’hôte à montrer à l’utilisateur (logs, redirection fin de sync).

    Priorité : ``MALT_CRM_PUBLIC_HOST`` si défini ; sinon ``127.0.0.1`` si le bind
    est ``0.0.0.0`` (Docker) ; sinon l’adresse de bind.
    """
    override = os.getenv("MALT_CRM_PUBLIC_HOST", "")
    if override:
        return override
    if bind_host == "0.0.0.0":
        return "127.0.0.1"
    return bind_host
