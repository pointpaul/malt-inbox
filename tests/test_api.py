from __future__ import annotations

import json

from curl_cffi import requests as curl_requests

from malt_crm.api import (
    DEFAULT_SESSION_HEADERS,
    MaltAPIClient,
    MaltAPIError,
    load_cookies_from_dict,
    load_cookies_from_json,
)


def test_load_cookies_from_dict_sets_cookie_values() -> None:
    jar = load_cookies_from_dict({"remember-me": "token", "XSRF-TOKEN": "csrf"})

    values = {cookie.name: cookie.value for cookie in jar}
    assert values == {"remember-me": "token", "XSRF-TOKEN": "csrf"}


def test_load_cookies_from_json_supports_mapping(tmp_path) -> None:
    cookies_path = tmp_path / "cookies.json"
    cookies_path.write_text(json.dumps({"remember-me": "abc123"}), encoding="utf-8")

    jar = load_cookies_from_json(cookies_path)

    assert jar.get("remember-me") == "abc123"


def test_client_from_cookies_uses_default_headers() -> None:
    client = MaltAPIClient.from_cookies(cookies={"remember-me": "abc123"})

    for header, value in DEFAULT_SESSION_HEADERS.items():
        assert client.session.headers[header] == value
    assert client.session.cookies.get("remember-me") == "abc123"


def test_request_json_wraps_transport_errors() -> None:
    class FailingSession:
        def __init__(self) -> None:
            self.headers = {}

        def request(self, **kwargs):  # noqa: ANN003
            raise curl_requests.RequestsError("network down")

    client = MaltAPIClient(session=FailingSession())  # type: ignore[arg-type]

    try:
        client._request_json("GET", "/messenger/api/conversation/conversations-or-client-project-offers")
    except MaltAPIError as exc:
        assert "failed before response" in str(exc)
    else:
        raise AssertionError("Expected MaltAPIError to be raised")
