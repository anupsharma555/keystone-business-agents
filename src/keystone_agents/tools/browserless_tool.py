"""Safe Browserless placeholder and SDK tool wrappers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from keystone_agents.company_research import research_company_fixture
from keystone_agents.config import load_settings
from keystone_agents.guardrails import (
    enforce_tool_input_guardrails,
    enforce_tool_output_guardrails,
    keystone_tool_guardrail_kwargs,
)
from keystone_agents.sdk import function_tool
from keystone_agents.source_enrichment import extract_claim_candidates, extract_clean_text


@dataclass(frozen=True)
class ScrapeResult:
    """Rendered page scrape result from Browserless or a deterministic dry-run mock."""

    url: str
    status: str
    title: str | None = None
    text_or_markdown: str = ""
    html: str | None = None
    source: str = "browserless:dry-run"


@dataclass(frozen=True)
class BrowserlessTool:
    live: bool = False

    def render(self, url: str) -> dict[str, Any]:
        result = fetch_rendered_page(url=url, live=self.live)
        return asdict(result)


def _require_browserless_key() -> str:
    api_key = load_settings().browserless_api_key
    if not api_key:
        raise RuntimeError(
            "BROWSERLESS_API_KEY is required for live Browserless rendering. "
            "Dry-run mode does not require credentials."
        )
    return api_key


def fetch_rendered_page(url: str, live: bool = False) -> ScrapeResult:
    """Fetch a rendered page through Browserless or return a deterministic dry-run mock."""

    enforce_tool_input_guardrails("browserless_fetch_rendered_page", {"url": url, "live": live})
    if not live:
        return enforce_tool_output_guardrails(
            "browserless_fetch_rendered_page",
            ScrapeResult(
                url=url,
                status="dry-run",
                title="Dry-run rendered page",
                text_or_markdown=(
                    f"Dry-run Browserless rendering for {url}. No network request was made."
                ),
                html="<html><head><title>Dry-run rendered page</title></head><body></body></html>",
                source="browserless:dry-run",
            ),
        )

    _require_browserless_key()
    raise NotImplementedError("Live Browserless rendering is not implemented yet.")


@function_tool(**keystone_tool_guardrail_kwargs())
def fetch_company_page(
    company_url: str,
    fixture_json: str | None = None,
    dry_run: bool = True,
) -> str:
    """Fetch a company page from fixture data. Live Browserless is not implemented."""

    if not dry_run:
        raise RuntimeError(
            "Live Browserless fetching requires an explicit integration implementation."
        )

    claims: list[str] = []
    title = company_url
    clean_text = ""
    if fixture_json and fixture_json.strip().startswith("{"):
        fixture = json.loads(fixture_json)
        title = fixture.get("name") or title
        for source in fixture.get("sources") or []:
            if isinstance(source, dict) and source.get("source_type") == "website":
                claims.extend(source.get("supported_claims") or source.get("claims") or [])
                clean_text = clean_text or extract_clean_text(
                    source.get("html") or source.get("text_or_markdown") or ""
                )
        if not claims:
            clean_text = extract_clean_text(
                fixture.get("html") or fixture.get("text_or_markdown") or ""
            )
            claims.extend(
                extract_claim_candidates(clean_text, company_name=str(title))
                or [fixture.get("description", ""), fixture.get("fit", "")]
            )

    return json.dumps(
        {
            "mode": "dry_run",
            "url": company_url,
            "title": title,
            "clean_text": clean_text,
            "claims": [claim for claim in claims if claim],
        },
        sort_keys=True,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def extract_company_signals(
    company_name: str,
    company_url: str | None = None,
    lead_name: str | None = None,
    linkedin_url: str | None = None,
    fixture_json: str | None = None,
    dry_run: bool = True,
) -> str:
    """Extract Keystone fit signals from fixture-backed source records."""

    if not dry_run:
        raise RuntimeError(
            "Live signal extraction requires source records from explicit live tools."
        )

    profile = research_company_fixture(
        company_name=company_name,
        company_url=company_url,
        lead_name=lead_name,
        linkedin_url=linkedin_url,
        fixture_json=fixture_json,
    )
    return profile.model_dump_json()
