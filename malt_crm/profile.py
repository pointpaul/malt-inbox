"""Freelancer profile refresh via Malt profile page."""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from typing import Any

import requests
from curl_cffi import requests as curl_requests

from .models import MaltProfileSnapshot

PROFILE_URL = "https://www.malt.fr/profile/?origin=dashboard_update_profile"


class MaltProfileError(RuntimeError):
    """Raised when the Malt profile cannot be fetched or parsed."""


def _extract_first(pattern: str, content: str) -> str | None:
    match = re.search(pattern, content, re.S | re.I)
    if not match:
        return None
    return html.unescape(match.group(1)).strip() or None


def _extract_ldjson_blocks(content: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for raw in re.findall(r'<script type="application/ld\+json">(.*?)</script>', content, re.S | re.I):
        try:
            parsed = json.loads(html.unescape(raw))
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            blocks.append(parsed)
    return blocks


def _extract_profile_summary(content: str) -> str | None:
    patterns = [
        r'profileSummary":\d+},\["Ref",\d+],\["Ref",\d+],"((?:[^"\\]|\\.)*)"',
        r'profileSummary":\d+},\["Ref",\d+],"((?:[^"\\]|\\.)*)"',
    ]
    match = None
    for pattern in patterns:
        match = re.search(pattern, content, re.S)
        if match:
            break
    if not match:
        return None
    raw = (
        match.group(1)
        .replace("\\n", "\n")
        .replace("\\/", "/")
        .replace('\\"', '"')
    )
    return html.unescape(raw).strip() or None


def _extract_missions(content: str, limit: int = 6) -> list[str]:
    pattern = re.compile(
        r'data-testid="profile-experience-item-company"[^>]*title="([^"]+)"'
        r'.*?data-testid="profile-experience-item-job"[^>]*>([^<]+)'
        r'.*?data-testid="profile-experience-item-description"[^>]*><div>(.*?)</div>',
        re.S,
    )
    missions: list[str] = []
    for company, role, description in pattern.findall(content):
        clean_description = re.sub(r"<[^>]+>", " ", description)
        clean_description = html.unescape(re.sub(r"\s+", " ", clean_description)).strip()
        mission = f"{html.unescape(role).strip()} chez {html.unescape(company).strip()}"
        if clean_description:
            mission = f"{mission}: {clean_description}"
        missions.append(mission)
        if len(missions) >= limit:
            break
    return missions


def _profile_from_html(content: str, *, final_url: str) -> MaltProfileSnapshot:
    blocks = _extract_ldjson_blocks(content)
    profile_block = next((block for block in blocks if block.get("@type") == "ProfilePage"), None)
    product_block = next((block for block in blocks if block.get("@type") == "Product"), None)

    person = profile_block.get("mainEntity", {}) if isinstance(profile_block, dict) else {}
    if not isinstance(person, dict):
        person = {}

    full_name = str(person.get("name") or _extract_first(r"<title>([^,<]+)", content) or "").strip()
    if not full_name:
        raise MaltProfileError("Could not parse freelancer name from Malt profile")

    headline = (
        str(person.get("jobTitle") or profile_block.get("name") if isinstance(profile_block, dict) else "")
        .strip()
        or _extract_first(r"<title>[^,]+,\s*(.*?)</title>", content)
    )
    summary = _extract_profile_summary(content) or _extract_first(r'<meta name="description" content="(.*?)"', content)
    skills = [str(item).strip() for item in person.get("skills", []) if str(item).strip()] if isinstance(person.get("skills"), list) else []
    missions = _extract_missions(content)

    daily_rate = None
    if isinstance(product_block, dict):
        offers = product_block.get("offers")
        if isinstance(offers, dict):
            price = offers.get("price")
            try:
                daily_rate = float(price) if price is not None else None
            except (TypeError, ValueError):
                daily_rate = None

    return MaltProfileSnapshot(
        full_name=full_name,
        headline=headline,
        summary=summary,
        skills=skills[:20],
        missions=missions,
        profile_url=final_url,
        image_url=str(person.get("image") or profile_block.get("image") if isinstance(profile_block, dict) else "") or None,
        daily_rate=daily_rate,
        raw_html_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        fetched_at=datetime.now(tz=timezone.utc),
    )


class MaltProfileFetcher:
    """Fetch and parse the current freelancer Malt profile."""

    def __init__(self, cookies: requests.cookies.RequestsCookieJar) -> None:
        self.cookies = cookies

    def _iter_cookie_values(self) -> list[tuple[str, str, str, str]]:
        """Return cookies as (name, value, domain, path) tuples."""
        normalized: list[tuple[str, str, str, str]] = []
        for cookie in self.cookies:
            if hasattr(cookie, "name") and hasattr(cookie, "value"):
                normalized.append(
                    (
                        str(cookie.name),
                        str(cookie.value),
                        str(getattr(cookie, "domain", "") or "www.malt.fr"),
                        str(getattr(cookie, "path", "") or "/"),
                    )
                )
                continue
            key = str(cookie)
            value = str(self.cookies.get(key, ""))
            if not value:
                continue
            normalized.append((key, value, "www.malt.fr", "/"))
        return normalized

    def fetch(self) -> MaltProfileSnapshot:
        session = curl_requests.Session(impersonate="chrome131")
        for name, value, domain, path in self._iter_cookie_values():
            session.cookies.set(
                name,
                value,
                domain=domain,
                path=path,
            )
        response = session.get(PROFILE_URL, timeout=30, allow_redirects=True)
        response.raise_for_status()
        if "Just a moment" in response.text[:500]:
            raise MaltProfileError("Cloudflare challenge still blocking the profile page")
        return _profile_from_html(response.text, final_url=str(response.url))
