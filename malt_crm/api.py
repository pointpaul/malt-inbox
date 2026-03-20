"""Requests-based Malt API client built around Malt session cookies."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Optional

import requests

from .models import Conversation, Message, Opportunity

DEFAULT_BASE_URL = "https://www.malt.fr"
DEFAULT_MALT_DOMAIN = ".malt.fr"
DEFAULT_MESSAGES_REFERER = f"{DEFAULT_BASE_URL}/messages"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_SESSION_HEADERS = {
    "Accept": "application/json",
    "Referer": DEFAULT_MESSAGES_REFERER,
    "User-Agent": DEFAULT_USER_AGENT,
    "X-Requested-With": "XMLHttpRequest",
}

class MaltAPIError(RuntimeError):
    """Raised when the Malt API request fails."""


def _set_cookie(
    jar: requests.cookies.RequestsCookieJar,
    name: str,
    value: str,
    *,
    domain: str = DEFAULT_MALT_DOMAIN,
    path: str = "/",
    secure: bool = True,
) -> None:
    if not name:
        return
    jar.set(name, value, domain=domain, path=path, secure=secure)


def _add_cookie_objects(
    jar: requests.cookies.RequestsCookieJar,
    cookies: Iterable[Mapping[str, Any]],
    *,
    default_domain: str = DEFAULT_MALT_DOMAIN,
) -> None:
    for cookie in cookies:
        _set_cookie(
            jar,
            str(cookie.get("name", "")),
            str(cookie.get("value", "")),
            domain=str(cookie.get("domain") or default_domain),
            path=str(cookie.get("path") or "/"),
            secure=bool(cookie.get("secure", True)),
        )


def load_cookies_from_dict(
    cookies: Mapping[str, str],
    *,
    domain: str = DEFAULT_MALT_DOMAIN,
) -> requests.cookies.RequestsCookieJar:
    """Create a cookie jar from a name/value mapping."""

    jar: requests.cookies.RequestsCookieJar = requests.cookies.RequestsCookieJar()
    for name, value in cookies.items():
        _set_cookie(jar, str(name), str(value), domain=domain)
    return jar


def load_cookies_from_json(
    json_path: Path | str,
    *,
    domain: str = DEFAULT_MALT_DOMAIN,
) -> requests.cookies.RequestsCookieJar:
    """Load cookies from a JSON file."""

    path = Path(json_path).expanduser()
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))

    if isinstance(payload, Mapping):
        return load_cookies_from_dict(
            {str(key): str(value) for key, value in payload.items()},
            domain=domain,
        )

    if not isinstance(payload, list):
        raise ValueError("Cookie JSON must be a mapping or a list of cookie objects")

    jar: requests.cookies.RequestsCookieJar = requests.cookies.RequestsCookieJar()
    _add_cookie_objects(jar, payload, default_domain=domain)
    return jar


def _merge_cookie_jar(
    target: requests.cookies.RequestsCookieJar,
    source: requests.cookies.RequestsCookieJar,
) -> None:
    for cookie in source:
        target.set_cookie(cookie)


class MaltAPIClient:
    """HTTP client for Malt messenger endpoints."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        session: Optional[requests.Session] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout
        if headers:
            self.session.headers.update(dict(headers))
        self.session.headers.setdefault("Accept", "application/json")

    @property
    def has_cookies(self) -> bool:
        """Return whether the current session carries any cookies."""

        return any(True for _ in self.session.cookies)

    @classmethod
    def from_cookies(
        cls,
        *,
        cookies: Optional[Mapping[str, str]] = None,
        cookies_json_path: Optional[Path | str] = None,
        chrome_domain: str = DEFAULT_MALT_DOMAIN,
        timeout: float = 30.0,
        base_url: str = DEFAULT_BASE_URL,
        headers: Optional[Mapping[str, str]] = None,
    ) -> "MaltAPIClient":
        """Build a client from session cookies only."""

        session = requests.Session()
        session.headers.update(DEFAULT_SESSION_HEADERS)
        if headers:
            session.headers.update(dict(headers))

        if cookies_json_path:
            _merge_cookie_jar(
                session.cookies,
                load_cookies_from_json(cookies_json_path, domain=chrome_domain),
            )

        if cookies:
            _merge_cookie_jar(
                session.cookies,
                load_cookies_from_dict(cookies, domain=chrome_domain),
            )

        return cls(
            base_url=base_url,
            session=session,
            timeout=timeout,
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[MutableMapping[str, Any]] = None,
        json_body: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        response = self.session.request(
            method=method,
            url=url,
            params=params,
            json=json_body,
            timeout=self.timeout,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            body_preview = response.text[:500]
            raise MaltAPIError(
                f"{method} {url} failed with status {response.status_code}: {body_preview}"
            ) from exc

        try:
            return response.json()
        except ValueError as exc:
            raise MaltAPIError(f"{method} {url} returned non-JSON data") from exc

    def _paginate(
        self,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        if page_size <= 0:
            raise ValueError("page_size must be > 0")

        items: list[dict[str, Any]] = []
        page = 0
        base_params = dict(params or {})

        while True:
            page_params = dict(base_params)
            page_params["page"] = page
            page_params["pageSize"] = page_size

            payload = self._request_json("GET", path, params=page_params)
            content = payload.get("content", [])
            if not isinstance(content, list):
                raise MaltAPIError(f"Unexpected page payload for {path}: missing list content")

            items.extend(item for item in content if isinstance(item, dict))

            if payload.get("last", True) or not content:
                break
            page += 1

        return items

    def _get_combined_inbox_items(
        self,
        *,
        status: str = "ACTIVE",
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        return self._paginate(
            "/messenger/api/conversation/conversations-or-client-project-offers",
            params={"status": status, "type": ""},
            page_size=page_size,
        )

    def get_conversations(
        self,
        *,
        status: str = "ACTIVE",
        page_size: int = 100,
    ) -> list[Conversation]:
        """Return normalized conversations from the combined inbox feed."""

        items = self._get_combined_inbox_items(status=status, page_size=page_size)
        conversations: list[Conversation] = []
        for item in items:
            if item.get("type") != "MESSAGES" and "lastMessage" not in item:
                continue
            conversations.append(Conversation.from_api(item))
        return conversations

    def get_messages(
        self,
        conversation_id: str,
        *,
        page_size: int = 100,
        sort_field: Optional[str] = None,
        sort_order: Optional[str] = None,
        wait_for_indexation: Optional[bool] = None,
    ) -> list[Message]:
        """Return normalized messages for one conversation."""

        params: dict[str, Any] = {}
        if sort_field:
            params["sortField"] = sort_field
        if sort_order:
            params["sortOrder"] = sort_order
        if wait_for_indexation is not None:
            params["waitForIndexation"] = str(wait_for_indexation).lower()

        items = self._paginate(
            f"/messenger/api/conversation/conversations/{conversation_id}/messages",
            params=params,
            page_size=page_size,
        )
        return [Message.from_api(item) for item in items]

    def get_opportunities(
        self,
        *,
        status: str = "ACTIVE",
        page_size: int = 100,
    ) -> list[Opportunity]:
        """Return project opportunities filtered from the combined inbox feed."""

        items = self._get_combined_inbox_items(status=status, page_size=page_size)
        opportunities: list[Opportunity] = []
        for item in items:
            if item.get("type") == "CLIENT_PROJECT" or "clientProjectId" in item:
                opportunities.append(Opportunity.from_api(item))
        return opportunities
