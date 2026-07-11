"""Bounded public company-contact discovery with first-party corroboration."""

from __future__ import annotations

import re
from collections.abc import Callable
from html.parser import HTMLParser
from typing import Any

import requests
from pydantic import BaseModel, Field

from keystone_agents.guardrails import keystone_tool_guardrail_kwargs
from keystone_agents.sdk import function_tool
from keystone_agents.tools.search_provider import SearchRequest, SearchResult, build_search_provider

_USER_AGENT = "Mozilla/5.0 KeystoneBusinessAgents/0.1 (public contact research)"
_PUBLIC_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_ROLE_PREFIX_RE = re.compile(
    r"^(?:chief\b|ceo\b|president\b|co[- ]founder\b|"
    r"senior\s+vice\s+president\b|vice\s+president\b|vp\b|head\b|director\b)",
    re.I,
)
_ROLE_SIGNALS = (
    ("head of partnerships", 100),
    ("chief commercial", 98),
    ("business development", 95),
    ("commercial growth", 94),
    ("chief growth", 90),
    ("market growth", 86),
    ("partnership", 84),
    ("commercial", 80),
    ("growth", 72),
)


class PublicContactCandidate(BaseModel):
    """One current public contact candidate with visible source support."""

    name: str
    role: str
    company_name: str
    relevance: str
    confidence: float = Field(ge=0.0, le=1.0)
    source_urls: list[str] = Field(default_factory=list)
    profile_url: str = ""
    search_corroborated: bool = False
    contact_path: str = ""
    contact_path_url: str = ""
    verification_status: str = "source_backed"


class PublicContactDiscoveryResult(BaseModel):
    """Structured no-outreach result for a public contact-discovery ask."""

    status: str
    company_name: str
    candidates: list[PublicContactCandidate] = Field(default_factory=list)
    search_provider: str = ""
    search_requests: int = 0
    search_result_count: int = 0
    corroborated_candidate_count: int = 0
    source_urls: list[str] = Field(default_factory=list)
    no_draft: bool = True
    send_enabled: bool = False
    inferred_personal_email: bool = False
    raw_source_content_included: bool = False
    blocker: str = ""


class _VisibleTextParts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if not self._skip_depth and value:
            self.parts.append(value)


def _visible_parts(html: str) -> list[str]:
    parser = _VisibleTextParts()
    parser.feed(html)
    return parser.parts


def _looks_like_person_name(value: str) -> bool:
    tokens = value.replace(",", "").split()
    if not 2 <= len(tokens) <= 5 or len(value) > 80:
        return False
    banned = {"our", "leadership", "team", "frequently", "contact", "about"}
    if any(token.lower() in banned for token in tokens):
        return False
    return all(token[:1].isupper() for token in tokens if token and token[0].isalpha())


def _official_leadership_candidates(html: str) -> list[tuple[str, str]]:
    parts = _visible_parts(html)
    candidates: list[tuple[str, str]] = []
    for index, role in enumerate(parts):
        if index == 0 or not _ROLE_PREFIX_RE.search(role) or len(role) > 100:
            continue
        name = parts[index - 1]
        if _looks_like_person_name(name):
            candidates.append((name, role))
    return list(dict.fromkeys(candidates))


def _role_score(role: str) -> int:
    lowered = role.lower()
    return max((score for marker, score in _ROLE_SIGNALS if marker in lowered), default=20)


def _result_name(result: SearchResult) -> str:
    title = result.title.strip()
    title = re.sub(r"'s\s+email.*$", "", title, flags=re.I)
    return title.split(" - ", 1)[0].strip()


def _result_role(result: SearchResult, *, company_name: str) -> str:
    content = " ".join((result.snippet, result.content or ""))
    escaped = re.escape(company_name)
    patterns = (
        rf"((?:Chief|Senior Vice President|Vice President|VP|Head|Director)"
        rf"[^\n|]{{2,80}}?)\s+(?:-|at)\s+\[?{escaped}\]?",
        r"Position:\s*((?:Chief|Senior Vice President|Vice President|VP|Head|Director)"
        r"[^\n]{2,100}?)(?:\n|Current|Location)",
    )
    for pattern in patterns:
        match = re.search(pattern, content, flags=re.I)
        if match:
            return " ".join(match.group(1).split()).strip(" -")
    return ""


def _fetch_public_html(
    url: str,
    *,
    http_get: Callable[..., Any],
) -> str:
    response = http_get(url, headers={"User-Agent": _USER_AGENT}, timeout=15)
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code >= 400:
        raise RuntimeError(f"Public contact source returned HTTP {status_code}: {url}")
    return str(getattr(response, "text", "") or "")


def discover_public_company_contacts_impl(
    company_name: str,
    official_about_url: str,
    official_contact_url: str,
    *,
    live: bool = False,
    search_provider: str = "exa",
    max_candidates: int = 3,
    http_get: Callable[..., Any] | None = None,
    search_results: list[SearchResult] | None = None,
) -> PublicContactDiscoveryResult:
    """Find source-backed public contacts without drafting or inferring personal details."""

    if not live:
        return PublicContactDiscoveryResult(status="dry-run", company_name=company_name)
    get = http_get or requests.get
    about_html = _fetch_public_html(official_about_url, http_get=get)
    contact_html = _fetch_public_html(official_contact_url, http_get=get)
    official_candidates = _official_leadership_candidates(about_html)
    rows = search_results
    search_requests = 0
    if rows is None:
        provider = build_search_provider(search_provider, live=True)
        rows = provider.search_structured(
            SearchRequest(
                query=(
                    f"{company_name} current partnerships business development "
                    "commercial growth leadership"
                ),
                num_results=5,
                scrape=True,
            )
        )
        search_requests = 1

    result_by_name: dict[str, tuple[str, str]] = {}
    for row in rows:
        name = _result_name(row)
        role = _result_role(row, company_name=company_name)
        if name and role and _looks_like_person_name(name):
            result_by_name.setdefault(name.lower(), (role, row.link))

    public_emails = sorted(set(_PUBLIC_EMAIL_RE.findall(" ".join(_visible_parts(contact_html)))))
    general_email = next(
        (email for email in public_emails if email.lower().startswith(("info@", "hello@"))),
        "",
    )
    candidates: list[PublicContactCandidate] = []
    for name, official_role in official_candidates:
        corroborated_role, profile_url = result_by_name.get(name.lower(), ("", ""))
        role = official_role or corroborated_role
        score = _role_score(role)
        if score < 70:
            continue
        source_urls = [official_about_url]
        if profile_url:
            source_urls.append(profile_url)
        confidence = min(0.98, 0.78 + (0.12 if profile_url else 0.0) + score / 1000)
        candidates.append(
            PublicContactCandidate(
                name=name,
                role=role,
                company_name=company_name,
                relevance=(
                    f"{role} is the strongest current first-party leadership match for "
                    "a commercial, growth, or partnership conversation."
                ),
                confidence=round(confidence, 2),
                source_urls=list(dict.fromkeys(source_urls)),
                profile_url=profile_url,
                search_corroborated=bool(profile_url),
                contact_path=(
                    f"Use the official general-inquiry path ({general_email}); do not infer "
                    "a personal address."
                    if general_email
                    else "Use the official contact or demo path; do not infer a personal address."
                ),
                contact_path_url=official_contact_url,
            )
        )
    candidates.sort(key=lambda item: (-_role_score(item.role), -item.confidence, item.name))
    selected = candidates[: max(1, min(max_candidates, 5))]
    source_urls = list(
        dict.fromkeys(
            [official_about_url, official_contact_url]
            + [url for candidate in selected for url in candidate.source_urls]
        )
    )
    return PublicContactDiscoveryResult(
        status="pass" if selected else "blocked",
        company_name=company_name,
        candidates=selected,
        search_provider=search_provider,
        search_requests=search_requests,
        search_result_count=len(rows),
        corroborated_candidate_count=sum(
            1 for candidate in selected if candidate.search_corroborated
        ),
        source_urls=source_urls,
        blocker="" if selected else "No current first-party commercial contact was verified.",
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def discover_public_company_contacts(
    company_name: str,
    official_about_url: str,
    official_contact_url: str,
    live: bool = False,
    search_provider: str = "exa",
    max_candidates: int = 3,
) -> str:
    """Find current public company contacts with first-party role evidence and no outreach."""

    return discover_public_company_contacts_impl(
        company_name,
        official_about_url,
        official_contact_url,
        live=live,
        search_provider=search_provider,
        max_candidates=max_candidates,
    ).model_dump_json()
