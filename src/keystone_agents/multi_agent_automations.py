"""Read-only multi-agent automation helpers for scheduled Slack posts."""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.request
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, urlunparse

from pydantic import BaseModel, Field

from keystone_agents.agents.business_research_analyst import (
    run_business_research_analyst_research_brief_sdk,
)
from keystone_agents.agents.chief_of_staff import run_chief_of_staff_sdk
from keystone_agents.agents.opportunity_scout import run_opportunity_scout_sdk
from keystone_agents.agents.orchestrator import review_specialist_output, run_orchestrator_sdk
from keystone_agents.config import load_settings
from keystone_agents.models import OpportunityScoutSDKInput, ResearchSDKInput
from keystone_agents.schemas.announcement_feed import (
    AnnouncementFeedEvidence,
    AnnouncementFeedItem,
)
from keystone_agents.schemas.chief_of_staff import ChiefOfStaffResult
from keystone_agents.schemas.opportunity import OpportunityScoutResult
from keystone_agents.schemas.orchestrator import OrchestratorOutputReview, OrchestratorResult
from keystone_agents.schemas.research import ResearchBrief
from keystone_agents.storage.sqlite_store import SQLiteStore, stable_hash
from keystone_agents.tools.search_provider import (
    SearchProviderConfigurationError,
    SearchProviderError,
    SearchResult,
    build_search_provider,
)
from keystone_agents.tools.website_extraction_tool import (
    WebsiteExtractionError,
    extract_website_content,
)

MAX_MEETING_PREP_ITEMS = 3
MIN_ANNOUNCEMENT_ITEMS = 3
MAX_ANNOUNCEMENT_ITEMS = 5
MAX_GITHUB_REPO_ITEMS = 4
SUMMARY_WORD_TARGET = 150
MIN_ARTICLE_READ_CHARS = 300
GITHUB_PRESELECT_README_WORDS = 80
GITHUB_SELECTED_README_WORDS = 180

MEETING_PREP_TERMS = {
    "appointment",
    "board",
    "brief",
    "call",
    "clinical",
    "collaboration",
    "consult",
    "deadline",
    "demo",
    "funding",
    "grant",
    "interview",
    "intro",
    "meeting",
    "partner",
    "prep",
    "proposal",
    "review",
    "strategy",
}
LOW_PREP_TERMS = {"block", "blocked", "focus", "hold", "lunch", "personal", "travel"}
MEETING_SEARCH_STOPWORDS = {
    "ai",
    "and",
    "brief",
    "calendar",
    "clinical",
    "digest",
    "discussion",
    "funding",
    "grant",
    "grants",
    "internal",
    "kni",
    "linkedin",
    "meeting",
    "opportunities",
    "opportunity",
    "prep",
    "preprints",
    "review",
    "scan",
    "the",
    "trials",
    "watch",
    "weekly",
}
ANNOUNCEMENT_RELEVANCE_TERMS = {
    "ai",
    "artificial intelligence",
    "behavioral health",
    "biomarker",
    "clinical",
    "clinical trial",
    "depression",
    "digital health",
    "fda",
    "healthcare",
    "machine learning",
    "mental health",
    "nimh",
    "psychiatry",
    "psychosis",
    "research",
    "schizophrenia",
}
GITHUB_REPO_DEFAULT_QUERIES = (
    "agents sdk openai slack google workspace automation in:name,description,topics,readme stars:>=50 pushed:>=2025-01-01 archived:false language:Python",
    "business agents workflow automation llm tools in:name,description,topics,readme stars:>=50 pushed:>=2025-01-01 archived:false",
    "data analysis pandas duckdb notebooks dashboards in:name,description,topics,readme stars:>=100 pushed:>=2025-01-01 archived:false",
    "slack bot google drive sheets python automation in:name,description,topics,readme stars:>=50 pushed:>=2025-01-01 archived:false",
)
GITHUB_REPO_RELEVANCE_TERMS = {
    "agent",
    "agents",
    "analysis",
    "analytics",
    "automation",
    "business",
    "dashboard",
    "data",
    "duckdb",
    "google",
    "integration",
    "llm",
    "openai",
    "pandas",
    "python",
    "slack",
    "workflow",
}
GITHUB_AGENT_TERMS = {
    "agent",
    "agents",
    "llm",
    "openai",
    "workflow",
    "automation",
    "orchestration",
    "handoff",
    "eval",
}
GITHUB_WORKSPACE_TERMS = {"slack", "google", "drive", "sheets", "workspace", "integration"}
GITHUB_DATA_TERMS = {"data", "analysis", "analytics", "dashboard", "pandas", "duckdb", "notebook"}
GITHUB_CLINICAL_TERMS = {"psychiatry", "behavioral", "mental health", "clinical", "healthcare"}
GITHUB_LOW_FIT_DOMAIN_TERMS = {"gis", "geospatial", "genome", "genomics", "bioinformatics"}


class SearchEvidence(BaseModel):
    kind: str = "search"
    title: str = ""
    url: str = ""
    snippet: str = ""
    source: str = "search"
    status: str = "success"
    char_count: int = 0


class MeetingPrepItem(BaseModel):
    title: str
    start: str = ""
    end: str = ""
    category: str = ""
    note: str = ""
    why_salient: str
    prep_focus: str
    research_queries: list[str] = Field(default_factory=list)
    evidence: list[SearchEvidence] = Field(default_factory=list)
    search_note: str = ""


class MeetingPrepAutomationResult(BaseModel):
    kind: str = "meeting-prep"
    status: str
    window: str = "Next 7 days"
    selected_count: int = 0
    prep_items: list[MeetingPrepItem] = Field(default_factory=list)
    slack_text: str
    diagnostics: list[str] = Field(default_factory=list)
    live_sdk_requested: bool = False
    live_search_requested: bool = False
    external_writes_enabled: bool = False
    calendar_writes_enabled: bool = False
    gmail_writes_enabled: bool = False
    crm_writes_enabled: bool = False
    slack_post_allowed: bool = True
    orchestrator_review: OrchestratorOutputReview | None = None


class AnnouncementLinkInput(BaseModel):
    title: str
    url: str = ""
    snippet: str = ""
    source: str = ""
    published_at: str = ""
    slack_link: str = ""
    relevance: list[str] = Field(default_factory=list)
    authors: list[str] = Field(default_factory=list)
    doi: str = ""
    arxiv_id: str = ""
    biorxiv_id: str = ""
    medrxiv_id: str = ""
    tags: list[str] = Field(default_factory=list)
    evidence: list[SearchEvidence] = Field(default_factory=list)


class AnnouncementResearchSummary(BaseModel):
    title: str
    url: str = ""
    source: str = ""
    word_count: int = 0
    summary: str
    why_selected: str
    evidence: list[SearchEvidence] = Field(default_factory=list)


class AnnouncementResearchAutomationResult(BaseModel):
    kind: str = "announcements-research"
    status: str
    selected_count: int = 0
    summaries: list[AnnouncementResearchSummary] = Field(default_factory=list)
    slack_text: str
    diagnostics: list[str] = Field(default_factory=list)
    live_sdk_requested: bool = False
    live_search_requested: bool = False
    external_writes_enabled: bool = False
    calendar_writes_enabled: bool = False
    gmail_writes_enabled: bool = False
    crm_writes_enabled: bool = False
    slack_post_allowed: bool = True
    learning_notes: list[str] = Field(default_factory=list)
    future_query_suggestions: list[str] = Field(default_factory=list)
    agent_chain: list[str] = Field(
        default_factory=lambda: [
            "orchestrator",
            "opportunity_scout",
            "business_research_analyst",
        ]
    )
    orchestrator_review: OrchestratorOutputReview | None = None


class GitHubRepositoryOpportunity(BaseModel):
    full_name: str
    url: str
    description: str = ""
    language: str = ""
    stars: int = 0
    forks: int = 0
    open_issues: int = 0
    license: str = ""
    pushed_at: str = ""
    updated_at: str = ""
    topics: list[str] = Field(default_factory=list)
    archived: bool = False
    readme_excerpt: str = ""
    key_paths: list[str] = Field(default_factory=list)
    community_signals: list[str] = Field(default_factory=list)
    detailed_review: str = ""
    relevance_score: int = 0
    implementation_value_score: int = 0
    why_useful: str
    keystone_fit: str = ""
    implementation_use_case: str = ""
    quality_signals: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    suggested_next_step: str
    evidence: list[SearchEvidence] = Field(default_factory=list)


class GitHubRepoOpportunityAutomationResult(BaseModel):
    kind: str = "github-repo-opportunities"
    status: str
    selected_count: int = 0
    repositories: list[GitHubRepositoryOpportunity] = Field(default_factory=list)
    slack_text: str
    diagnostics: list[str] = Field(default_factory=list)
    live_sdk_requested: bool = False
    live_search_requested: bool = False
    external_writes_enabled: bool = False
    github_writes_enabled: bool = False
    calendar_writes_enabled: bool = False
    gmail_writes_enabled: bool = False
    crm_writes_enabled: bool = False
    slack_post_allowed: bool = True
    learning_notes: list[str] = Field(default_factory=list)
    future_query_suggestions: list[str] = Field(default_factory=list)
    agent_chain: list[str] = Field(
        default_factory=lambda: [
            "orchestrator",
            "opportunity_scout",
            "business_research_analyst",
        ]
    )
    orchestrator_review: OrchestratorOutputReview | None = None


def run_meeting_prep_automation(
    payload: dict[str, Any],
    *,
    live_sdk: bool = False,
    live_search: bool = False,
    max_items: int = MAX_MEETING_PREP_ITEMS,
) -> MeetingPrepAutomationResult:
    """Select prep-worthy calendar items and render a Slack-ready brief."""

    window = str(payload.get("window") or payload.get("title") or "Next 7 days")
    events = _calendar_events_from_payload(payload)
    diagnostics = list(_string_list(payload.get("diagnostics")))
    selected = _select_meeting_prep_items(events, max_items=max_items)
    if live_search and selected:
        diagnostics.extend(_attach_search_evidence(selected))
    chief_result: ChiefOfStaffResult | None = None
    if live_sdk and selected:
        chief_result, chief_diagnostics = _run_chief_of_staff_meeting_prep_synthesis(
            window,
            selected,
            payload,
        )
        diagnostics.extend(chief_diagnostics)
    if not selected:
        text = (
            "*Chief of Staff weekly meeting prep*\n"
            f"No high-salience meeting preparation items were found for {window}. "
            "I did not write to Calendar, Gmail, CRM, or any external system."
        )
        result = MeetingPrepAutomationResult(
            status="no_prep_needed",
            window=window,
            slack_text=text,
            diagnostics=diagnostics or ["No prep-worthy calendar items selected."],
            live_sdk_requested=bool(live_sdk),
            live_search_requested=bool(live_search),
        )
        return _attach_automation_orchestrator_review(
            result,
            agent_name="chief_of_staff",
            request_summary=f"Scheduled meeting prep automation for {window}",
        )

    lines = [
        "*Chief of Staff weekly meeting prep*",
        f"Window: {window}",
        "",
    ]
    if chief_result is not None:
        lines.append(chief_result.summary)
        if chief_result.recommended_actions:
            lines.append("Useful preparation:")
            lines.extend(f"- {action}" for action in chief_result.recommended_actions[:5])
        lines.append("")
    for index, item in enumerate(selected, start=1):
        when = f" ({item.start})" if item.start else ""
        lines.append(f"{index}. *{item.title}*{when}")
        lines.append(f"   Prep focus: {item.prep_focus}")
        if item.evidence:
            evidence = item.evidence[0]
            lines.append(f"   Research lead: {evidence.title} - {evidence.url}")
        elif item.research_queries:
            lines.append(f"   Research lead: {item.research_queries[0]}")
    lines.extend(
        [
            "",
            "No calendar writes, Gmail sends, CRM updates, or external publications were performed.",
        ]
    )
    result = MeetingPrepAutomationResult(
        status="ok",
        window=window,
        selected_count=len(selected),
        prep_items=selected,
        slack_text="\n".join(lines),
        diagnostics=diagnostics,
        live_sdk_requested=bool(live_sdk),
        live_search_requested=bool(live_search),
    )
    return _attach_automation_orchestrator_review(
        result,
        agent_name="chief_of_staff",
        request_summary=f"Scheduled meeting prep automation for {window}",
    )


def _run_chief_of_staff_meeting_prep_synthesis(
    window: str,
    selected: list[MeetingPrepItem],
    payload: dict[str, Any],
) -> tuple[ChiefOfStaffResult | None, list[str]]:
    diagnostics = ["Chief of Staff SDK path: enabled for weekly meeting prep."]
    prompt = _meeting_prep_chief_of_staff_prompt(window, selected, payload)
    try:
        sdk_result = run_chief_of_staff_sdk(prompt, live=True)
    except Exception as exc:
        return None, diagnostics + [
            f"Chief of Staff SDK synthesis failed; used deterministic fallback: {type(exc).__name__}: {exc}"
        ]
    diagnostics.append("Chief of Staff SDK synthesis completed.")
    return sdk_result.output, diagnostics


def _meeting_prep_chief_of_staff_prompt(
    window: str,
    selected: list[MeetingPrepItem],
    payload: dict[str, Any],
) -> str:
    lines = [
        "Prepare the internal weekly meeting-prep brief for Keystone.",
        "Use only this bounded calendar context and any supplied research leads.",
        "Do not create or update calendar events, send Gmail, post externally, or write CRM records.",
        f"Window: {window}",
    ]
    prep_items = _string_list(payload.get("prep_items"))
    if prep_items:
        lines.extend(["Calendar digest prep hints:", *[f"- {item}" for item in prep_items[:8]]])
    for index, item in enumerate(selected, start=1):
        lines.extend(
            [
                "",
                f"Meeting {index}: {item.title}",
                f"Start: {item.start or '(unknown)'}",
                f"End: {item.end or '(unknown)'}",
                f"Category: {item.category or '(none)'}",
                f"Calendar note: {item.note or '(none)'}",
                f"Deterministic prep focus: {item.prep_focus}",
            ]
        )
        for evidence in item.evidence[:2]:
            lines.extend(
                [
                    f"Research lead: {evidence.title}",
                    f"Research URL: {evidence.url}",
                    f"Research snippet: {evidence.snippet}",
                ]
            )
    return "\n".join(lines)


def run_announcements_research_synthesis(
    payload: dict[str, Any],
    *,
    live_sdk: bool = False,
    live_search: bool = False,
    min_items: int = MIN_ANNOUNCEMENT_ITEMS,
    max_items: int = MAX_ANNOUNCEMENT_ITEMS,
    database_url: str | None = None,
    automation_run_id: str = "",
) -> AnnouncementResearchAutomationResult:
    """Select salient announcement links and render source-backed summaries."""

    links = _announcement_links_from_payload(payload)
    diagnostics = list(_string_list(payload.get("diagnostics")))
    selected = _select_announcement_links(links, min_items=min_items, max_items=max_items)
    if live_search and selected:
        diagnostics.extend(_attach_announcement_search_evidence(selected))

    sdk_brief: ResearchBrief | None = None
    if live_sdk and selected:
        sdk_brief, sdk_diagnostics = _run_business_research_analyst_announcement_synthesis(selected)
        diagnostics.extend(sdk_diagnostics)

    summaries = (
        _summaries_from_research_brief(sdk_brief, selected)
        if sdk_brief is not None
        else [_summarize_announcement(item) for item in selected]
    )
    if not summaries:
        text = (
            "*Business Research Analyst weekly announcement synthesis*\n"
            "No sufficiently relevant announcement links were found in the supplied seven-day context. "
            "No external writes were performed."
        )
        result = AnnouncementResearchAutomationResult(
            status="empty",
            slack_text=text,
            diagnostics=diagnostics or ["No announcement links were available for synthesis."],
            live_sdk_requested=bool(live_sdk),
            live_search_requested=bool(live_search),
        )
        reviewed = _attach_automation_orchestrator_review(
            result,
            agent_name="business_research_analyst",
            request_summary="Scheduled announcements research synthesis",
        )
        return _persist_announcement_research_records(
            reviewed,
            links=links,
            selected=selected,
            summaries=[],
            database_url=database_url,
            automation_run_id=automation_run_id,
        )

    if len(summaries) < min_items:
        diagnostics.append(
            f"Selected {len(summaries)} item(s), below the target of {min_items}, because fewer relevant links were supplied."
        )
    lines = [
        "*Business Research Analyst weekly announcement synthesis*",
        "",
    ]
    for index, item in enumerate(summaries, start=1):
        title = f"<{item.url}|{item.title}>" if item.url else item.title
        lines.append(f"{index}. *{title}*")
        lines.append(item.summary)
    basis_line = (
        "Source basis: Chief of Staff link selection plus Business Research Analyst SDK synthesis "
        "over bounded article reads and search evidence."
        if sdk_brief is not None
        else "Source basis: supplied #announcements context plus bounded article/search evidence when enabled."
    )
    lines.extend(
        [
            "",
            basis_line,
            "No calendar writes, Gmail sends, CRM updates, or external publications were performed.",
        ]
    )
    result = AnnouncementResearchAutomationResult(
        status="ok",
        selected_count=len(summaries),
        summaries=summaries,
        slack_text="\n".join(lines),
        diagnostics=diagnostics,
        live_sdk_requested=bool(live_sdk),
        live_search_requested=bool(live_search),
    )
    reviewed = _attach_automation_orchestrator_review(
        result,
        agent_name="business_research_analyst",
        request_summary="Scheduled announcements research synthesis",
    )
    return _persist_announcement_research_records(
        reviewed,
        links=links,
        selected=selected,
        summaries=summaries,
        database_url=database_url,
        automation_run_id=automation_run_id,
    )


def run_github_repo_opportunities(
    payload: dict[str, Any],
    *,
    live_sdk: bool = False,
    live_search: bool = False,
    max_items: int = MAX_GITHUB_REPO_ITEMS,
) -> GitHubRepoOpportunityAutomationResult:
    """Find useful open-source GitHub repositories for Keystone operations."""

    diagnostics = list(_string_list(payload.get("diagnostics")))
    target = str(
        payload.get("target")
        or (
            "open-source repositories useful for Keystone company operations, "
            "AI agents development, data analysis, Slack/Google integrations, "
            "and psychiatry or behavioral-health research workflows"
        )
    )
    repos = _github_repos_from_payload(payload)
    preselection_enriched = False
    if live_search and not repos:
        discovered, discovery_diagnostics = _discover_github_repositories(
            payload, max_items=max_items
        )
        repos = discovered
        preselection_enriched = True
        diagnostics.extend(discovery_diagnostics)
    elif not repos:
        diagnostics.append("No repository payload supplied and live search was not enabled.")
    if live_search and repos and not preselection_enriched:
        repos, enrich_diagnostics = _enrich_github_repository_candidates(
            repos,
            max_items=max_items,
        )
        diagnostics.extend(enrich_diagnostics)

    selected = _rank_github_repository_opportunities(repos, max_items=max_items)
    if live_search and selected:
        selected, detail_diagnostics = _deepen_selected_github_repositories(selected)
        diagnostics.extend(detail_diagnostics)
    learning_notes, future_query_suggestions = _github_repo_learning_notes(
        candidates=repos,
        selected=selected,
        max_items=max_items,
    )
    diagnostics.extend(f"Learning note: {note}" for note in learning_notes)
    orchestrator_result: OrchestratorResult | None = None
    sdk_result: OpportunityScoutResult | None = None
    research_brief: ResearchBrief | None = None
    if live_sdk and selected:
        orchestrator_result, orchestrator_diagnostics = _run_orchestrator_github_repo_route(
            target=target,
            selected=selected,
        )
        diagnostics.extend(orchestrator_diagnostics)
        sdk_result, sdk_diagnostics = _run_opportunity_scout_github_repo_synthesis(
            target=target,
            selected=selected,
        )
        diagnostics.extend(sdk_diagnostics)
        research_brief, research_diagnostics = _run_business_research_analyst_github_repo_synthesis(
            target=target,
            selected=selected,
        )
        diagnostics.extend(research_diagnostics)

    if not selected:
        text = (
            "*Opportunity Scout weekly GitHub repository scan*\n"
            "No high-confidence open-source repository opportunities were found. "
            "No GitHub writes, Slack broadcasts, Gmail sends, CRM updates, Calendar writes, "
            "Drive edits, or Airtable writes were performed."
        )
        result = GitHubRepoOpportunityAutomationResult(
            status="empty",
            slack_text=text,
            diagnostics=diagnostics or ["No repository candidates were available."],
            live_sdk_requested=bool(live_sdk),
            live_search_requested=bool(live_search),
            learning_notes=learning_notes,
            future_query_suggestions=future_query_suggestions,
        )
        return _attach_automation_orchestrator_review(
            result,
            agent_name="opportunity_scout",
            request_summary=f"Scheduled GitHub repository opportunity scan for {target}",
        )

    lines = [
        "*Opportunity Scout weekly GitHub repository scan*",
        f"Focus: {target}",
        "Agent chain: Orchestrator -> Opportunity Scout -> Business Research Analyst",
        "",
    ]
    if orchestrator_result is not None:
        route = orchestrator_result.route or "unknown"
        rationale = _trim_words(orchestrator_result.rationale or "", 28)
        route_line = f"Orchestrator route: {route}"
        if rationale:
            route_line += f" - {rationale}"
        lines.append(route_line)
    if sdk_result is not None:
        lines.append(
            "Opportunity Scout SDK synthesis completed over the selected repository evidence."
        )
    if research_brief is not None:
        lines.append(
            "Business Research Analyst detail synthesis completed over the selected repository evidence."
        )
        if research_brief.summary:
            lines.append(_trim_words(research_brief.summary, 45))
        lines.append("")
    for index, repo in enumerate(selected, start=1):
        title = f"<{repo.url}|{repo.full_name}>" if repo.url else repo.full_name
        signals = "; ".join(repo.quality_signals[:4]) or "quality signals require follow-up"
        caveats = "; ".join(repo.caveats[:3]) or "no major caveat found from available metadata"
        lines.extend(
            [
                f"{index}. *{title}*",
                f"   Keystone fit: {repo.keystone_fit or repo.why_useful}",
                f"   Use case: {repo.implementation_use_case or repo.why_useful}",
                *([f"   Reviewed: {repo.detailed_review}"] if repo.detailed_review else []),
                f"   Signals: {signals}",
                f"   Caveats: {caveats}",
                f"   Next: {repo.suggested_next_step}",
            ]
        )
    lines.extend(
        [
            "",
            "Source basis: GitHub repository metadata plus bounded Opportunity Scout synthesis when enabled.",
            *(["Learning signal: " + " ".join(learning_notes[:2])] if learning_notes else []),
            "No GitHub writes, Slack broadcasts, Gmail sends, CRM updates, Calendar writes, Drive edits, or Airtable writes were performed.",
        ]
    )
    result = GitHubRepoOpportunityAutomationResult(
        status="ok",
        selected_count=len(selected),
        repositories=selected,
        slack_text="\n".join(lines),
        diagnostics=diagnostics,
        live_sdk_requested=bool(live_sdk),
        live_search_requested=bool(live_search),
        learning_notes=learning_notes,
        future_query_suggestions=future_query_suggestions,
    )
    return _attach_automation_orchestrator_review(
        result,
        agent_name="opportunity_scout",
        request_summary=f"Scheduled GitHub repository opportunity scan for {target}",
    )


def _attach_automation_orchestrator_review(
    result: Any,
    *,
    agent_name: str,
    request_summary: str,
) -> Any:
    """Attach deterministic Orchestrator review metadata to Slack-facing automation output."""

    payload = result.model_dump(mode="json", exclude={"orchestrator_review"})
    review = review_specialist_output(
        agent_name=agent_name,
        output=payload,
        request_summary=request_summary,
        run_type=f"scheduled_{payload.get('kind', 'automation')}",
    )
    diagnostics = list(getattr(result, "diagnostics", []) or [])
    diagnostics.append(
        f"Orchestrator automation review: {review.status} ({review.overall_score}/100)."
    )
    return result.model_copy(
        update={
            "orchestrator_review": review,
            "diagnostics": diagnostics,
        }
    )


def _github_repos_from_payload(payload: dict[str, Any]) -> list[GitHubRepositoryOpportunity]:
    rows = payload.get("repositories")
    if rows is None:
        rows = payload.get("repos")
    if rows is None:
        rows = payload.get("items")
    if not isinstance(rows, list):
        return []
    repos: list[GitHubRepositoryOpportunity] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        repo = _github_repo_from_mapping(row)
        if repo is not None:
            repos.append(repo)
    return repos


def _discover_github_repositories(
    payload: dict[str, Any],
    *,
    max_items: int,
) -> tuple[list[GitHubRepositoryOpportunity], list[str]]:
    queries = _github_repo_queries(payload)
    diagnostics = [
        f"GitHub repository discovery query count: {len(queries)}.",
        "GitHub writes disabled: no stars, forks, issues, pull requests, or repository mutations.",
    ]
    candidates: list[GitHubRepositoryOpportunity] = []
    api_failed = False
    for query in queries:
        try:
            rows = _github_search_repositories(query, per_page=max(max_items * 3, 10))
        except Exception as exc:
            api_failed = True
            diagnostics.append(
                f"GitHub API search failed for `{query}`: {type(exc).__name__}: {exc}"
            )
            break
        candidates.extend(
            repo for row in rows if (repo := _github_repo_from_mapping(row)) is not None
        )
    if candidates:
        diagnostics.append(
            f"GitHub API repository search returned {len(candidates)} candidate row(s)."
        )
        candidates, enrich_diagnostics = _enrich_github_repository_candidates(
            candidates,
            max_items=max_items,
        )
        diagnostics.extend(enrich_diagnostics)
        return candidates, diagnostics
    provider, provider_error = _live_search_provider()
    if provider_error:
        diagnostics.append(provider_error)
    if provider is None:
        return candidates, diagnostics
    diagnostics.extend(_live_search_diagnostics())
    for query in queries:
        search_query = f"site:github.com {query}"
        try:
            raw_results = provider.search_web(search_query, num_results=max(max_items * 2, 8))
        except Exception as exc:
            diagnostics.append(
                f"Fallback web search failed for `{search_query}`: {type(exc).__name__}: {exc}"
            )
            continue
        for result in raw_results or []:
            candidate = _github_repo_from_search_result(result, search_query)
            if candidate is not None:
                candidates.append(candidate)
    if api_failed:
        diagnostics.append("Used SearXNG/compatible fallback after GitHub API search failed.")
    diagnostics.append(f"Fallback repository search returned {len(candidates)} candidate row(s).")
    candidates, enrich_diagnostics = _enrich_github_repository_candidates(
        candidates,
        max_items=max_items,
    )
    diagnostics.extend(enrich_diagnostics)
    return candidates, diagnostics


def _github_repo_queries(payload: dict[str, Any]) -> list[str]:
    supplied = _string_list(payload.get("queries"))
    if supplied:
        return supplied[:8]
    target = str(payload.get("target") or "").strip()
    if target:
        return [
            (
                f"{_clean_query(target)} in:name,description,topics,readme "
                "stars:>=50 pushed:>=2025-01-01 archived:false"
            ),
            *GITHUB_REPO_DEFAULT_QUERIES,
        ][:8]
    return list(GITHUB_REPO_DEFAULT_QUERIES)


def _github_search_repositories(query: str, *, per_page: int) -> list[dict[str, Any]]:
    params = urlencode(
        {
            "q": query,
            "sort": "stars",
            "order": "desc",
            "per_page": max(1, min(per_page, 50)),
        }
    )
    request = urllib.request.Request(
        f"https://api.github.com/search/repositories?{params}",
        headers=_github_api_headers(),
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        payload = json.loads(response.read().decode("utf-8"))
    rows = payload.get("items", []) if isinstance(payload, dict) else []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _github_api_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
    query = f"?{urlencode(params)}" if params else ""
    request = urllib.request.Request(
        f"https://api.github.com{path}{query}",
        headers=_github_api_headers(),
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.loads(response.read().decode("utf-8"))


def _github_api_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "keystone-business-agents",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _github_repo_from_mapping(row: dict[str, Any]) -> GitHubRepositoryOpportunity | None:
    full_name = str(row.get("full_name") or row.get("name") or "").strip()
    url = str(row.get("html_url") or row.get("url") or "").strip()
    if not full_name and url:
        full_name = _github_full_name_from_url(url)
    if not full_name or not url:
        return None
    license_data = row.get("license")
    license_name = ""
    if isinstance(license_data, dict):
        license_name = str(license_data.get("spdx_id") or license_data.get("name") or "").strip()
    elif license_data:
        license_name = str(license_data).strip()
    if license_name.upper() == "NOASSERTION":
        license_name = ""
    evidence = [
        SearchEvidence(
            kind="github_repository",
            title=full_name,
            url=url,
            snippet=str(row.get("description") or ""),
            source="github",
            status="metadata",
        )
    ]
    repo = GitHubRepositoryOpportunity(
        full_name=full_name,
        url=url,
        description=str(row.get("description") or ""),
        language=str(row.get("language") or ""),
        stars=_int_value(row.get("stargazers_count") or row.get("stars")),
        forks=_int_value(row.get("forks_count") or row.get("forks")),
        open_issues=_int_value(row.get("open_issues_count") or row.get("open_issues")),
        license=license_name,
        pushed_at=str(row.get("pushed_at") or ""),
        updated_at=str(row.get("updated_at") or ""),
        topics=_string_list(row.get("topics")),
        archived=bool(row.get("archived", False)),
        readme_excerpt=str(row.get("readme_excerpt") or ""),
        key_paths=_string_list(row.get("key_paths")),
        community_signals=_string_list(row.get("community_signals")),
        detailed_review=str(row.get("detailed_review") or ""),
        relevance_score=0,
        why_useful="",
        suggested_next_step="",
        evidence=evidence,
    )
    return _score_github_repo(repo)


def _github_repo_from_search_result(
    result: Any,
    query: str,
) -> GitHubRepositoryOpportunity | None:
    title = str(
        getattr(result, "title", "")
        or (result.get("title", "") if isinstance(result, dict) else "")
    ).strip()
    url = str(
        getattr(result, "link", "")
        or getattr(result, "url", "")
        or (result.get("link") or result.get("url") if isinstance(result, dict) else "")
    ).strip()
    snippet = str(
        getattr(result, "snippet", "")
        or (result.get("snippet", "") if isinstance(result, dict) else "")
    ).strip()
    if "github.com/" not in url.lower():
        return None
    full_name = _github_full_name_from_url(url)
    if not full_name:
        return None
    repo = GitHubRepositoryOpportunity(
        full_name=full_name,
        url=f"https://github.com/{full_name}",
        description=snippet,
        readme_excerpt="",
        key_paths=[],
        community_signals=[],
        detailed_review="",
        relevance_score=0,
        why_useful="",
        quality_signals=["Found by GitHub-qualified fallback web search."],
        caveats=[
            "Repository metadata such as stars, forks, license, and pushed date was not available from fallback search."
        ],
        suggested_next_step="Open the repository and verify README, license, activity, and fit before adoption.",
        evidence=[
            SearchEvidence(
                kind="search",
                title=title or full_name,
                url=url,
                snippet=f"Query: {query}. {snippet}".strip(),
                source="search",
                status="fallback",
            )
        ],
    )
    return _score_github_repo(repo)


def _github_full_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        return ""
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        return ""
    if parts[0] in {"topics", "search", "marketplace", "features"}:
        return ""
    return f"{parts[0]}/{parts[1].removesuffix('.git')}"


def _enrich_github_repository_candidates(
    candidates: list[GitHubRepositoryOpportunity],
    *,
    max_items: int,
) -> tuple[list[GitHubRepositoryOpportunity], list[str]]:
    if not candidates:
        return candidates, []
    limit = min(len(candidates), max(max_items * 2, 8))
    preliminary = _rank_github_repository_opportunities(candidates, max_items=limit)
    preliminary_names = {repo.full_name.lower() for repo in preliminary}
    enriched: dict[str, GitHubRepositoryOpportunity] = {}
    diagnostics = [f"GitHub repository enrichment inspected up to {limit} candidate(s)."]
    failures = 0
    for repo in preliminary:
        try:
            enriched[repo.full_name.lower()] = _enrich_github_repository(
                repo,
                readme_word_limit=GITHUB_PRESELECT_README_WORDS,
            )
        except Exception as exc:
            failures += 1
            enriched[repo.full_name.lower()] = repo.model_copy(
                update={
                    "caveats": list(
                        dict.fromkeys(
                            [
                                *repo.caveats,
                                f"README/files/comment enrichment failed: {type(exc).__name__}",
                            ]
                        )
                    )
                }
            )
    if failures:
        diagnostics.append(
            f"GitHub enrichment failed for {failures} candidate(s); metadata-only scoring was retained for them."
        )
    return [
        enriched.get(repo.full_name.lower(), repo)
        if repo.full_name.lower() in preliminary_names
        else repo
        for repo in candidates
    ], diagnostics


def _deepen_selected_github_repositories(
    selected: list[GitHubRepositoryOpportunity],
) -> tuple[list[GitHubRepositoryOpportunity], list[str]]:
    deepened: list[GitHubRepositoryOpportunity] = []
    failures = 0
    for repo in selected:
        try:
            enriched = _enrich_github_repository(
                repo,
                readme_word_limit=GITHUB_SELECTED_README_WORDS,
                detailed=True,
            )
        except Exception as exc:
            failures += 1
            enriched = repo.model_copy(
                update={
                    "caveats": list(
                        dict.fromkeys(
                            [
                                *repo.caveats,
                                f"selected-repo detailed review failed: {type(exc).__name__}",
                            ]
                        )
                    )
                }
            )
        deepened.append(enriched)
    diagnostics = [f"Selected GitHub repository detail review inspected {len(selected)} repo(s)."]
    if failures:
        diagnostics.append(
            f"Selected repository detail review failed for {failures} repo(s); preselection evidence was retained."
        )
    return _rank_github_repository_opportunities(deepened, max_items=len(selected)), diagnostics


def _enrich_github_repository(
    repo: GitHubRepositoryOpportunity,
    *,
    readme_word_limit: int,
    detailed: bool = False,
) -> GitHubRepositoryOpportunity:
    readme_excerpt = _github_repo_readme_excerpt(repo.full_name, word_limit=readme_word_limit)
    key_paths = _github_repo_top_level_paths(repo.full_name)
    community_signals = _github_repo_community_signals(repo.full_name)
    evidence = list(repo.evidence)
    if readme_excerpt:
        evidence.append(
            SearchEvidence(
                kind="github_readme",
                title=f"{repo.full_name} README",
                url=f"{repo.url}#readme",
                snippet=readme_excerpt,
                source="github",
                status="read",
                char_count=len(readme_excerpt),
            )
        )
    if key_paths:
        evidence.append(
            SearchEvidence(
                kind="github_contents",
                title=f"{repo.full_name} top-level files",
                url=repo.url,
                snippet=", ".join(key_paths[:16]),
                source="github",
                status="read",
            )
        )
    if community_signals:
        evidence.append(
            SearchEvidence(
                kind="github_community",
                title=f"{repo.full_name} issue/comment signals",
                url=f"{repo.url}/issues",
                snippet="; ".join(community_signals[:8]),
                source="github",
                status="read",
            )
        )
    enriched = _score_github_repo(
        repo.model_copy(
            update={
                "readme_excerpt": readme_excerpt,
                "key_paths": key_paths,
                "community_signals": community_signals,
                "evidence": evidence,
            }
        )
    )
    if detailed:
        enriched = enriched.model_copy(
            update={"detailed_review": _github_repo_detailed_review(enriched)}
        )
    return enriched


def _github_repo_readme_excerpt(full_name: str, *, word_limit: int) -> str:
    payload = _github_api_json(f"/repos/{full_name}/readme")
    if not isinstance(payload, dict):
        return ""
    content = str(payload.get("content") or "")
    encoding = str(payload.get("encoding") or "")
    if encoding != "base64" or not content:
        return ""
    decoded = base64.b64decode(content, validate=False).decode("utf-8", errors="ignore")
    return _trim_words(" ".join(decoded.split()), word_limit)


def _github_repo_top_level_paths(full_name: str) -> list[str]:
    payload = _github_api_json(f"/repos/{full_name}/contents")
    if not isinstance(payload, list):
        return []
    paths: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        kind = str(item.get("type") or "").strip()
        if not name:
            continue
        suffix = "/" if kind == "dir" else ""
        paths.append(f"{name}{suffix}")
    return paths[:20]


def _github_repo_community_signals(full_name: str) -> list[str]:
    signals: list[str] = []
    issues = _github_api_json(
        f"/repos/{full_name}/issues",
        params={"state": "open", "per_page": 8, "sort": "comments", "direction": "desc"},
    )
    if isinstance(issues, list):
        labels: set[str] = set()
        issue_titles: list[str] = []
        for issue in issues:
            if not isinstance(issue, dict) or "pull_request" in issue:
                continue
            issue_titles.append(str(issue.get("title") or "").strip())
            for label in issue.get("labels", []) if isinstance(issue.get("labels"), list) else []:
                if isinstance(label, dict):
                    label_name = str(label.get("name") or "").strip().lower()
                    if label_name:
                        labels.add(label_name)
        if labels & {"help wanted", "good first issue", "documentation", "bug", "enhancement"}:
            signals.append("open issue labels: " + ", ".join(sorted(labels)[:5]))
        if issue_titles:
            signals.append(
                "active issue examples: " + " | ".join(title for title in issue_titles[:2] if title)
            )
    comments = _github_api_json(
        f"/repos/{full_name}/issues/comments",
        params={"per_page": 5, "sort": "updated", "direction": "desc"},
    )
    if isinstance(comments, list) and comments:
        signals.append(f"recent issue comments visible: {len(comments)}")
    return [signal for signal in signals if signal][:6]


def _github_repo_detailed_review(repo: GitHubRepositoryOpportunity) -> str:
    parts: list[str] = []
    if repo.readme_excerpt:
        parts.append("README reviewed")
    if repo.key_paths:
        high_signal_paths = [
            path
            for path in repo.key_paths
            if path.lower().rstrip("/")
            in {"examples", "docs", "src", "tests", "notebooks", "workflows", "integrations"}
            or path.lower().endswith((".md", ".py", ".ts", ".js", ".ipynb", ".yml", ".yaml"))
        ]
        display_paths = high_signal_paths[:5] or repo.key_paths[:5]
        parts.append("key paths: " + ", ".join(display_paths))
    if repo.community_signals:
        parts.append("issue/comment signals: " + "; ".join(repo.community_signals[:2]))
    return "; ".join(parts)


def _score_github_repo(repo: GitHubRepositoryOpportunity) -> GitHubRepositoryOpportunity:
    text = " ".join(
        [
            repo.full_name,
            repo.description,
            repo.language,
            " ".join(repo.topics),
            repo.readme_excerpt,
            " ".join(repo.key_paths),
            " ".join(repo.community_signals),
        ]
    )
    lowered = text.lower()
    relevance_hits = sorted(
        term for term in GITHUB_REPO_RELEVANCE_TERMS if _github_repo_text_has(lowered, term)
    )
    keystone_fit_score = _github_repo_fit_score(lowered, relevance_hits)
    implementation_value_score = _github_repo_implementation_value_score(repo, lowered)
    score = keystone_fit_score + implementation_value_score
    low_fit_domain = _github_repo_low_fit_domain(lowered)
    if low_fit_domain:
        score -= 35
    if repo.archived:
        score -= 45
    if not repo.license:
        score -= 10
    quality = list(repo.quality_signals)
    quality.append(f"fit/value score: {max(0, min(100, score))}/100")
    if repo.stars:
        quality.append(f"{repo.stars:,} stars")
    if repo.forks:
        quality.append(f"{repo.forks:,} forks")
    if repo.license:
        quality.append(f"license: {repo.license}")
    if repo.pushed_at:
        quality.append(f"last pushed: {repo.pushed_at[:10]}")
    if repo.language:
        quality.append(f"language: {repo.language}")
    if repo.readme_excerpt:
        quality.append("README reviewed")
    if repo.key_paths:
        quality.append(f"{len(repo.key_paths)} top-level paths reviewed")
    if repo.community_signals:
        quality.append("issue/comment signals reviewed")
    if relevance_hits:
        quality.append("matched Keystone terms: " + ", ".join(relevance_hits[:5]))
    caveats = list(repo.caveats)
    if repo.archived:
        caveats.append("archived repository")
    if low_fit_domain:
        caveats.append(
            "domain appears outside Keystone's current agent, operations, data-analysis, or behavioral-health focus"
        )
    if not repo.license:
        caveats.append("license not visible in available metadata")
    if repo.pushed_at and not repo.pushed_at.startswith(("2026", "2025")):
        caveats.append(f"activity may be stale; last pushed {repo.pushed_at[:10]}")
    elif not repo.pushed_at:
        caveats.append("recent activity not available from current metadata")
    why = _github_repo_why_useful(repo, relevance_hits)
    keystone_fit = _github_repo_keystone_fit(repo, relevance_hits)
    implementation_use_case = _github_repo_implementation_use_case(repo, relevance_hits)
    next_step = _github_repo_next_step(repo, relevance_hits)
    return repo.model_copy(
        update={
            "relevance_score": max(0, min(100, score)),
            "implementation_value_score": implementation_value_score,
            "why_useful": why,
            "keystone_fit": keystone_fit,
            "implementation_use_case": implementation_use_case,
            "quality_signals": list(dict.fromkeys(quality)),
            "caveats": list(dict.fromkeys(caveats)),
            "suggested_next_step": next_step,
        }
    )


def _github_repo_fit_score(text: str, relevance_hits: list[str]) -> int:
    score = min(20, len(relevance_hits) * 3)
    if _github_repo_has_any(text, GITHUB_AGENT_TERMS):
        score += 28
    if _github_repo_has_any(text, GITHUB_WORKSPACE_TERMS):
        score += 24
    if _github_repo_has_any(text, GITHUB_DATA_TERMS):
        score += 22
    if _github_repo_has_any(text, GITHUB_CLINICAL_TERMS):
        score += 24
    return min(60, score)


def _github_repo_implementation_value_score(repo: GitHubRepositoryOpportunity, text: str) -> int:
    score = 0
    if repo.language.lower() in {"python", "typescript", "javascript"}:
        score += 8
    if _github_repo_has_any(
        text, {"python", "sdk", "api", "framework", "library", "workflow", "automation"}
    ):
        score += 7
    if repo.license:
        score += 7
    if repo.pushed_at.startswith(("2026", "2025")):
        score += 8
    if repo.stars >= 5000:
        score += 6
    elif repo.stars >= 1000:
        score += 5
    elif repo.stars >= 100:
        score += 3
    if repo.forks >= 500:
        score += 3
    elif repo.forks >= 50:
        score += 2
    if repo.archived:
        score -= 20
    return max(0, min(40, score))


def _github_repo_why_useful(repo: GitHubRepositoryOpportunity, relevance_hits: list[str]) -> str:
    text = " ".join([repo.full_name, repo.description, " ".join(repo.topics)]).lower()
    if _github_repo_has_any(text, GITHUB_CLINICAL_TERMS):
        return "Potentially useful for psychiatry, behavioral-health, or clinical research workflows Keystone tracks."
    if _github_repo_has_any(text, GITHUB_WORKSPACE_TERMS):
        return "Potentially useful for Keystone Slack and Google Workspace agent integrations."
    if _github_repo_has_any(text, GITHUB_AGENT_TERMS):
        return "Potentially useful for improving Keystone business-agent orchestration or tool execution."
    if _github_repo_has_any(text, GITHUB_DATA_TERMS):
        return (
            "Potentially useful for structured data analysis, reporting, or operational dashboards."
        )
    if relevance_hits:
        return "Potentially useful because its public metadata matches Keystone operating themes."
    return "Potentially useful as an open-source repository candidate, pending manual fit review."


def _github_repo_keystone_fit(repo: GitHubRepositoryOpportunity, relevance_hits: list[str]) -> str:
    text = _github_repo_full_text(repo)
    name = repo.full_name.lower()
    description = (
        _trim_words(repo.description, 18) if repo.description else "its public repository metadata"
    )
    if _github_repo_low_fit_domain(text):
        return (
            "Weak Keystone fit from available metadata: the repository appears domain-specific outside the current "
            "agent, operations, data-analysis, and behavioral-health automation focus."
        )
    if "hamilton" in name or _github_repo_has_any(text, {"dataflow", "pipeline"}):
        return (
            f"Relevant because {repo.full_name} appears to support explicit dataflow or pipeline structure. "
            "Keystone could use that pattern to make Slack, Drive, Sheets, and research automation steps more testable "
            "than ad hoc scripts."
        )
    if "dify" in name:
        return (
            "Relevant as a mature open-source LLM application platform to compare against Keystone's own agent runtime. "
            "The useful review is architectural: workflow design, app/tool boundaries, observability, and where Keystone "
            "should stay lightweight instead of adopting a platform."
        )
    if "awesome" in name:
        return (
            f"Relevant mainly as a curated reference list, not an implementation dependency. Use it to discover patterns "
            f"or candidate tools around {description}, then promote only specific repos that fit Keystone."
        )
    if _github_repo_has_any(text, {"slack"}):
        return (
            f"Relevant because {repo.full_name} is close to Keystone's Slack operating surface. Review whether its examples "
            "improve app mentions, scheduled posts, selected-message actions, or runtime diagnostics."
        )
    if _github_repo_has_any(text, {"google", "drive", "sheets", "workspace"}):
        return (
            f"Relevant because {repo.full_name} overlaps Keystone's KNIOps Google Workspace layer. Review patterns for "
            "folder-scoped Drive/Docs/Sheets work, OAuth boundaries, and artifact iteration from Slack."
        )
    if _github_repo_has_any(text, GITHUB_CLINICAL_TERMS):
        return (
            f"Relevant to Keystone's psychiatry and behavioral-health research lane because its metadata points to "
            f"{description}. Review whether it can support clinical evidence tracking, text analysis, or opportunity discovery."
        )
    if _github_repo_has_any(text, GITHUB_AGENT_TERMS):
        return (
            f"Relevant to Keystone's business-agent stack because {repo.full_name} appears to address {description}. "
            "Map its concrete examples to one Keystone concern: tool routing, handoffs, memory, retrieval, evals, or approval gates."
        )
    if _github_repo_has_any(text, GITHUB_DATA_TERMS):
        return (
            f"Relevant to Keystone's structured-data needs because {repo.full_name} appears to address {description}. "
            "The implementation question is whether it improves analysis of Slack, Sheets, Drive, or research outputs."
        )
    if relevance_hits:
        return (
            "Relevant enough for review because its public metadata overlaps Keystone's agent, automation, "
            "operations, or research themes."
        )
    return "Low-confidence fit from available metadata; keep only as a watchlist candidate until the README is reviewed."


def _github_repo_implementation_use_case(
    repo: GitHubRepositoryOpportunity, relevance_hits: list[str]
) -> str:
    text = _github_repo_full_text(repo)
    name = repo.full_name.lower()
    if _github_repo_low_fit_domain(text):
        return "Do not prioritize for implementation unless a later README review finds a direct Keystone operating use case."
    if "hamilton" in name or _github_repo_has_any(text, {"dataflow", "pipeline"}):
        return "Try modeling one weekly automation as a small typed dataflow: input context, retrieval, specialist synthesis, Slack render, diagnostics."
    if "dify" in name:
        return "Review workflow/app architecture only; extract ideas for observability or tool UX, not a direct migration target."
    if "awesome" in name:
        return "Use as a discovery source for the next scan; pick one specific downstream repo before adding anything to implementation backlog."
    if _github_repo_has_any(text, {"slack"}):
        return "Compare its Slack examples against Keystone app mentions, buttons, channel context, and scheduled automation posting."
    if _github_repo_has_any(text, {"google", "drive", "sheets", "workspace"}):
        return "Compare its OAuth and file-scoping patterns against the KNIOps Drive/Docs/Sheets boundary."
    if _github_repo_has_any(text, GITHUB_CLINICAL_TERMS):
        return "Prototype a small research-note workflow: extract one relevant dataset, model, or paper-tracking pattern into KNIOps."
    if _github_repo_has_any(text, GITHUB_WORKSPACE_TERMS):
        return "Compare its integration patterns against Keystone Slack/Drive/Sheets tools before adopting code or abstractions."
    if _github_repo_has_any(text, {"agent", "agents", "llm", "openai"}):
        return "Review examples for reusable agent patterns: tool calling, handoffs, memory, retrieval, evals, and approval gates."
    if _github_repo_has_any(text, {"workflow", "automation", "dataflow", "pipeline"}):
        return "Assess whether its workflow model can make scheduled Keystone automations easier to test, trace, or maintain."
    if _github_repo_has_any(text, GITHUB_DATA_TERMS):
        return "Run a tiny KNIOps sample against exported Slack or Sheets data to see if it improves analysis or dashboards."
    return "Open the README and examples, then decide whether it belongs in research tracking or the implementation backlog."


def _github_repo_next_step(repo: GitHubRepositoryOpportunity, relevance_hits: list[str]) -> str:
    text = _github_repo_full_text(repo)
    name = repo.full_name.lower()
    if _github_repo_low_fit_domain(text):
        return "Deprioritize for now; keep only if a later run finds a direct Keystone use case."
    if not repo.license:
        return "Verify license terms first, then review README/examples before adding it to a KNIOps backlog."
    if "hamilton" in name or _github_repo_has_any(text, {"dataflow", "pipeline"}):
        return "Read one tutorial and decide whether a single Keystone automation should be prototyped as a typed dataflow."
    if "dify" in name:
        return "Skim architecture/docs for workflow and observability ideas; do not treat it as an adoption recommendation yet."
    if "awesome" in name:
        return "Use it as a source list for future candidates, then select a concrete implementation repo before action."
    if _github_repo_has_any(text, {"agent", "agents", "llm", "workflow", "automation"}):
        return "Review the README and one example against Keystone's agent automation patterns; capture fit/no-fit in KNIOps."
    if _github_repo_has_any(text, GITHUB_DATA_TERMS):
        return "Try a small read-only data prototype using exported KNIOps data, then keep or reject based on friction."
    if _github_repo_has_any(text, GITHUB_CLINICAL_TERMS):
        return "Check source quality and clinical relevance before adding it to research tracking."
    return "Review README, examples, license, and maintenance health; if fit is strong, add it to a KNIOps research note or implementation backlog."


def _github_repo_has_any(text: str, terms: set[str]) -> bool:
    return any(_github_repo_text_has(text, term) for term in terms)


def _github_repo_text_has(text: str, term: str) -> bool:
    normalized = term.lower().strip()
    if not normalized:
        return False
    if " " in normalized:
        return normalized in text
    return re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", text) is not None


def _github_repo_low_fit_domain(text: str) -> bool:
    if not _github_repo_has_any(text, GITHUB_LOW_FIT_DOMAIN_TERMS):
        return False
    return not (
        _github_repo_has_any(text, GITHUB_AGENT_TERMS)
        or _github_repo_has_any(text, GITHUB_WORKSPACE_TERMS)
        or _github_repo_has_any(text, GITHUB_CLINICAL_TERMS)
    )


def _github_repo_full_text(repo: GitHubRepositoryOpportunity) -> str:
    return " ".join(
        [
            repo.full_name,
            repo.description,
            repo.language,
            " ".join(repo.topics),
            repo.readme_excerpt,
            " ".join(repo.key_paths),
            " ".join(repo.community_signals),
        ]
    ).lower()


def _rank_github_repository_opportunities(
    repos: list[GitHubRepositoryOpportunity],
    *,
    max_items: int,
) -> list[GitHubRepositoryOpportunity]:
    deduped: dict[str, GitHubRepositoryOpportunity] = {}
    for repo in repos:
        key = repo.full_name.lower()
        if repo.archived and len(repos) > max_items:
            continue
        if key not in deduped or repo.relevance_score > deduped[key].relevance_score:
            deduped[key] = repo
    ranked = sorted(
        deduped.values(),
        key=lambda item: (
            -item.relevance_score,
            -item.implementation_value_score,
            -item.stars,
            -item.forks,
            item.full_name.lower(),
        ),
    )
    strong = [
        repo
        for repo in ranked
        if repo.relevance_score >= 45
        and (repo.license or repo.stars >= 500 or not repo.stars)
        and not any("domain appears outside Keystone" in caveat for caveat in repo.caveats)
    ]
    return (strong or ranked)[: max(1, min(max_items, MAX_GITHUB_REPO_ITEMS))]


def _github_repo_learning_notes(
    *,
    candidates: list[GitHubRepositoryOpportunity],
    selected: list[GitHubRepositoryOpportunity],
    max_items: int,
) -> tuple[list[str], list[str]]:
    notes: list[str] = []
    suggestions: list[str] = []
    if len(selected) < min(max_items, MAX_GITHUB_REPO_ITEMS):
        notes.append(
            f"Only {len(selected)} repository candidate(s) met the current quality threshold; broaden or diversify next run if this repeats."
        )
        suggestions.append(
            "open-source agent workflow data analysis in:name,description,topics,readme stars:>=25 pushed:>=2025-01-01 archived:false"
        )
    if candidates and not any(repo.license for repo in candidates):
        notes.append(
            "Current candidate set lacked visible license metadata; keep license visibility as a selection caveat."
        )
        suggestions.append(
            "agent workflow automation license:mit in:name,description,topics,readme pushed:>=2025-01-01 archived:false"
        )
    stale_count = sum(
        1
        for repo in candidates
        if repo.pushed_at and not repo.pushed_at.startswith(("2026", "2025"))
    )
    if stale_count:
        notes.append(
            f"{stale_count} candidate(s) appeared stale by pushed date; current recency filters should remain in place."
        )
    fallback_count = sum(
        1 for repo in candidates if any(evidence.status == "fallback" for evidence in repo.evidence)
    )
    if fallback_count:
        notes.append(
            f"{fallback_count} candidate(s) came from fallback search without full GitHub metadata; prefer GitHub API metadata when available."
        )
    selected_text = " ".join(
        " ".join(
            [
                repo.description,
                repo.language,
                " ".join(repo.topics),
                repo.readme_excerpt,
                " ".join(repo.key_paths),
                " ".join(repo.community_signals),
            ]
        ).lower()
        for repo in selected
    )
    if selected and not any(
        marker in selected_text for marker in ("psychiatry", "behavioral", "clinical")
    ):
        suggestions.append(
            "psychiatry behavioral health clinical ai open-source in:name,description,topics,readme stars:>=25 pushed:>=2025-01-01 archived:false"
        )
    if selected and not any(marker in selected_text for marker in ("slack", "google")):
        suggestions.append(
            "slack google workspace automation python in:name,description,topics,readme stars:>=25 pushed:>=2025-01-01 archived:false"
        )
    if selected:
        notes.append(
            "Persist selected repository names and caveats in automation logs so later runs can avoid repeating low-fit or stale candidates."
        )
    return list(dict.fromkeys(notes)), list(dict.fromkeys(suggestions))


def _run_opportunity_scout_github_repo_synthesis(
    *,
    target: str,
    selected: list[GitHubRepositoryOpportunity],
) -> tuple[OpportunityScoutResult | None, list[str]]:
    diagnostics = [
        "Opportunity Scout SDK path: enabled for GitHub repository opportunity synthesis."
    ]
    context = _github_repo_opportunity_context(selected)
    try:
        sdk_result = run_opportunity_scout_sdk(
            OpportunityScoutSDKInput(
                topic=(
                    "Opportunity Scout: rank and select open-source GitHub repository opportunities for Keystone. "
                    "Use the enriched README, top-level file/folder, issue/comment, metadata, and caveat evidence. "
                    "Prefer repositories with concrete implementation value for company operations, business agents, "
                    "data analysis, Slack/Google integrations, or psychiatry and behavioral-health workflows."
                ),
                max_results=len(selected),
                context=(
                    f"Target: {target}\n\n"
                    "Agent responsibility: Opportunity Scout evaluates opportunity fit, implementation value, "
                    "maintenance risk, and prioritization. Do not perform long-form research synthesis; that belongs "
                    "to Business Research Analyst after selection.\n\n"
                    f"{context}"
                ),
            ),
            live=True,
            tool_tier="deep_retrieval",
        )
    except Exception as exc:
        return None, diagnostics + [
            f"Opportunity Scout SDK synthesis failed; used deterministic fallback: {type(exc).__name__}: {exc}"
        ]
    diagnostics.append("Opportunity Scout SDK synthesis completed.")
    return sdk_result.output, diagnostics


def _run_orchestrator_github_repo_route(
    *,
    target: str,
    selected: list[GitHubRepositoryOpportunity],
) -> tuple[OrchestratorResult | None, list[str]]:
    diagnostics = ["Orchestrator SDK path: enabled for GitHub repository opportunity routing."]
    prompt = "\n".join(
        [
            "Route this scheduled Keystone automation.",
            "Requested workflow: find and summarize open-source GitHub repositories.",
            f"Target: {target}",
            "Expected agent chain: Orchestrator confirms route and constraints; Opportunity Scout selects and prioritizes repositories from enriched evidence; Business Research Analyst synthesizes selected repository details.",
            "Do not collapse these roles: route with Orchestrator, select with Opportunity Scout, synthesize evidence with Business Research Analyst.",
            "No GitHub writes, stars, forks, issues, pull requests, Gmail sends, Calendar writes, CRM updates, Drive edits, Airtable writes, or external Slack broadcasts are permitted.",
            "Selected repository candidates available before specialist synthesis. They already include bounded README/file/comment evidence where available:",
            *[
                f"- {repo.full_name}: {repo.url} | score={repo.relevance_score} | fit={repo.keystone_fit} | reviewed={repo.detailed_review or repo.readme_excerpt or 'metadata only'}"
                for repo in selected
            ],
        ]
    )
    try:
        sdk_result = run_orchestrator_sdk(prompt, live=True)
    except Exception as exc:
        return None, diagnostics + [
            f"Orchestrator SDK routing failed; continued with specialist chain: {type(exc).__name__}: {exc}"
        ]
    diagnostics.append(
        f"Orchestrator SDK routing completed: route={sdk_result.output.route}, target_agent={sdk_result.output.target_agent or ''}."
    )
    return sdk_result.output, diagnostics


def _run_business_research_analyst_github_repo_synthesis(
    *,
    target: str,
    selected: list[GitHubRepositoryOpportunity],
) -> tuple[ResearchBrief | None, list[str]]:
    diagnostics = [
        "Business Research Analyst SDK path: enabled for GitHub repository detail synthesis."
    ]
    source_context = _github_repo_research_source_context(selected)
    try:
        sdk_result = run_business_research_analyst_research_brief_sdk(
            ResearchSDKInput(
                target_name="Weekly GitHub repository opportunities",
                target_type="github_repository_collection",
                research_goal=(
                    "Business Research Analyst: synthesize the selected open-source GitHub repositories for Keystone "
                    "using the bounded source evidence supplied by Opportunity Scout. For each selected repo, explain "
                    "what the README, key files/folders, and issue/comment signals imply about practical utility, "
                    "maintenance/quality, adoption caveats, and implementation fit for company operations, business-agent "
                    "development, data analysis, Slack/Google integrations, or psychiatry and behavioral-health workflows. "
                    f"Target: {target}"
                ),
                source_context=source_context,
            ),
            live=True,
            tool_tier="deep_retrieval",
        )
    except Exception as exc:
        return None, diagnostics + [
            f"Business Research Analyst SDK synthesis failed; used deterministic fallback: {type(exc).__name__}: {exc}"
        ]
    diagnostics.append("Business Research Analyst SDK synthesis completed.")
    return sdk_result.output, diagnostics


def _github_repo_opportunity_context(selected: list[GitHubRepositoryOpportunity]) -> str:
    lines = [
        "Selected GitHub repository candidates. Treat them as open-source tooling opportunities, not companies.",
        "Opportunity Scout role: decide whether each repository is a true opportunity for Keystone and rank by fit plus implementation value.",
        "Evidence basis: use README excerpts, top-level files/folders, issue/comment signals, source links, and metadata. Do not rely on stars alone.",
        "Do not recommend GitHub writes, stars, forks, issues, pull requests, external outreach, or Slack broadcasts.",
    ]
    for index, repo in enumerate(selected, start=1):
        lines.extend(
            [
                "",
                f"Repository {index}: {repo.full_name}",
                f"URL: {repo.url}",
                f"Description: {repo.description or '(none supplied)'}",
                f"Language: {repo.language or '(unknown)'}",
                f"Stars: {repo.stars}",
                f"Forks: {repo.forks}",
                f"License: {repo.license or '(unknown)'}",
                f"Pushed at: {repo.pushed_at or '(unknown)'}",
                f"Topics: {', '.join(repo.topics) if repo.topics else '(none supplied)'}",
                f"README excerpt: {repo.readme_excerpt or '(not reviewed)'}",
                f"Key paths: {', '.join(repo.key_paths) if repo.key_paths else '(not reviewed)'}",
                f"Community signals: {'; '.join(repo.community_signals) if repo.community_signals else '(not reviewed)'}",
                f"Detailed review: {repo.detailed_review or '(not available)'}",
                f"Deterministic relevance score: {repo.relevance_score}",
                f"Why useful: {repo.why_useful}",
                f"Caveats: {'; '.join(repo.caveats) if repo.caveats else '(none)'}",
            ]
        )
    return "\n".join(lines)


def _github_repo_research_source_context(selected: list[GitHubRepositoryOpportunity]) -> str:
    lines = [
        "Opportunity Scout selected these GitHub repositories for bounded Business Research Analyst review.",
        "Business Research Analyst role: explain what the selected repository evidence means for Keystone. Use the source_id values exactly. Do not recommend repository writes or external outreach.",
        "Evidence basis includes bounded README excerpts, top-level files/folders, issue/comment signals, source links, and metadata.",
    ]
    for index, repo in enumerate(selected, start=1):
        source_id = f"github_repo_{index}"
        lines.extend(
            [
                "",
                f"Source ID: {source_id}",
                f"Repository: {repo.full_name}",
                f"URL: {repo.url}",
                f"Description: {repo.description or '(none supplied)'}",
                f"Language: {repo.language or '(unknown)'}",
                f"Stars: {repo.stars}",
                f"Forks: {repo.forks}",
                f"Open issues: {repo.open_issues}",
                f"License: {repo.license or '(unknown)'}",
                f"Pushed at: {repo.pushed_at or '(unknown)'}",
                f"Updated at: {repo.updated_at or '(unknown)'}",
                f"Topics: {', '.join(repo.topics) if repo.topics else '(none supplied)'}",
                f"README excerpt: {repo.readme_excerpt or '(not reviewed)'}",
                f"Key paths: {', '.join(repo.key_paths) if repo.key_paths else '(not reviewed)'}",
                f"Community signals: {'; '.join(repo.community_signals) if repo.community_signals else '(not reviewed)'}",
                f"Detailed review: {repo.detailed_review or '(not available)'}",
                f"Opportunity Scout rationale: {repo.why_useful}",
                f"Quality signals: {'; '.join(repo.quality_signals) if repo.quality_signals else '(none)'}",
                f"Caveats: {'; '.join(repo.caveats) if repo.caveats else '(none)'}",
            ]
        )
    return "\n".join(lines)


def _int_value(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _calendar_events_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("events")
    if rows is None:
        rows = payload.get("items")
    if rows is None and isinstance(payload.get("digest"), dict):
        rows = payload["digest"].get("items")
    return [dict(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _select_meeting_prep_items(
    events: list[dict[str, Any]], *, max_items: int
) -> list[MeetingPrepItem]:
    ranked: list[tuple[int, str, MeetingPrepItem]] = []
    for row in events:
        text = " ".join(
            str(row.get(key, ""))
            for key in ("title", "summary", "description", "location", "category", "note")
        )
        score = _term_score(text, MEETING_PREP_TERMS) - _term_score(text, LOW_PREP_TERMS)
        title = str(row.get("title") or row.get("summary") or "(untitled event)").strip()
        if score <= 0 and title.lower() not in {
            item.lower() for item in _string_list(row.get("prep_items"))
        }:
            continue
        start = str(row.get("start", ""))
        focus = _meeting_focus(title=title, note=str(row.get("note", "")), score=score)
        queries, search_note = _meeting_queries(title, row)
        item = MeetingPrepItem(
            title=title,
            start=start,
            end=str(row.get("end", "")),
            category=str(row.get("category", "")),
            note=str(row.get("note", "")),
            why_salient="The calendar item appears to involve preparation, coordination, or external context.",
            prep_focus=focus,
            research_queries=queries,
            search_note=search_note,
        )
        ranked.append((score, start, item))
    ranked.sort(key=lambda item: (-item[0], item[1], item[2].title.lower()))
    return [item for _score, _start, item in ranked[: max(max_items, 0)]]


def _announcement_links_from_payload(payload: dict[str, Any]) -> list[AnnouncementLinkInput]:
    rows = payload.get("links")
    if rows is None:
        rows = payload.get("items")
    if rows is None:
        rows = payload.get("categorized_items")
    if not isinstance(rows, list):
        return []
    links: list[AnnouncementLinkInput] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title", "")).strip()
        if not title:
            continue
        links.append(
            AnnouncementLinkInput(
                title=title,
                url=str(row.get("url") or row.get("link") or ""),
                snippet=str(row.get("snippet") or row.get("summary") or row.get("note") or ""),
                source=str(row.get("source") or ""),
                published_at=str(row.get("published_at") or row.get("published") or ""),
                slack_link=str(row.get("slack_link") or row.get("permalink") or ""),
                relevance=_string_list(row.get("relevance")),
                authors=_string_list(row.get("authors")),
                doi=str(row.get("doi") or ""),
                arxiv_id=str(row.get("arxiv_id") or row.get("arxiv") or ""),
                biorxiv_id=str(row.get("biorxiv_id") or row.get("biorxiv") or ""),
                medrxiv_id=str(row.get("medrxiv_id") or row.get("medrxiv") or ""),
                tags=_string_list(row.get("tags")),
            )
        )
    return links


def _select_announcement_links(
    links: list[AnnouncementLinkInput],
    *,
    min_items: int,
    max_items: int,
) -> list[AnnouncementLinkInput]:
    deduped: dict[str, tuple[int, AnnouncementLinkInput]] = {}
    for item in links:
        key = item.url or _normalize_text(item.title)
        score = _announcement_score(item)
        if key not in deduped or score > deduped[key][0]:
            deduped[key] = (score, item)
    ranked = sorted(deduped.values(), key=lambda row: (-row[0], row[1].title.lower()))
    relevant = [item for score, item in ranked if score > 0]
    if len(relevant) >= min_items:
        return relevant[:max_items]
    fallback = [item for _score, item in ranked]
    return fallback[: min(max_items, max(min_items, len(fallback)))]


def _summarize_announcement(item: AnnouncementLinkInput) -> AnnouncementResearchSummary:
    evidence_note = ""
    if item.relevance:
        evidence_note = f" The RSS review tagged it for {', '.join(item.relevance[:3])}."
    article = next((evidence for evidence in item.evidence if evidence.kind == "article"), None)
    search = next((evidence for evidence in item.evidence if evidence.kind == "search"), None)
    evidence_note_text = ""
    if article is not None:
        snippet = (
            _trim_words(article.snippet, 40) if article.snippet else "no readable excerpt returned"
        )
        evidence_note_text = (
            f" Read source: {article.title or item.title} via {article.source} "
            f"({article.status}, {article.char_count} chars). Key extracted passage: {snippet} "
            f"({article.url})."
        )
    elif search is not None:
        snippet = _trim_words(search.snippet, 30) if search.snippet else "no snippet returned"
        evidence_note_text = f" Search evidence only: {search.title} - {snippet} ({search.url})."
    source = item.source or "the supplied announcement context"
    text = (
        f"{item.title} is relevant because it connects to Keystone's psychiatry, healthcare, clinical research, "
        f"or AI operating themes. Source: {source}.{evidence_note} "
        f"{evidence_note_text} "
        f"The supplied context says: {item.snippet or 'no snippet was supplied, so this should be treated as a lead for follow-up review.'} "
        "Useful follow-up is to verify the primary source, extract the concrete business or research implication, "
        "and decide whether it belongs in research tracking, opportunity scouting, or a future outreach draft."
    )
    summary = _trim_words(text, SUMMARY_WORD_TARGET)
    return AnnouncementResearchSummary(
        title=item.title,
        url=item.url,
        source=source,
        word_count=len(summary.split()),
        summary=summary,
        why_selected=_why_announcement_selected(item),
        evidence=list(item.evidence),
    )


def _run_business_research_analyst_announcement_synthesis(
    selected: list[AnnouncementLinkInput],
) -> tuple[ResearchBrief | None, list[str]]:
    diagnostics = [
        "Business Research Analyst SDK path: enabled for announcement research synthesis.",
    ]
    source_context = _announcement_research_source_context(selected)
    if not source_context.strip():
        return None, diagnostics + [
            "Business Research Analyst SDK path skipped: no source context was available."
        ]
    try:
        sdk_result = run_business_research_analyst_research_brief_sdk(
            ResearchSDKInput(
                target_name="Weekly #announcements selected links",
                target_type="article_collection",
                research_goal=(
                    "Research the selected weekly #announcements links for Keystone. "
                    "For each link, summarize what the source says, why it matters for "
                    "psychiatry, healthcare, clinical research, AI, or Keystone operations, "
                    "and the most useful follow-up. Use the provided source_id values exactly."
                ),
                source_context=source_context,
            ),
            live=True,
            tool_tier="deep_retrieval",
        )
    except Exception as exc:
        return None, diagnostics + [
            f"Business Research Analyst SDK synthesis failed; used deterministic fallback: {type(exc).__name__}: {exc}"
        ]
    diagnostics.append("Business Research Analyst SDK synthesis completed.")
    return sdk_result.output, diagnostics


def _announcement_research_source_context(selected: list[AnnouncementLinkInput]) -> str:
    lines = [
        "Chief of Staff selected these #announcements links for Business Research Analyst review.",
        "Use the source_id values exactly when citing facts. Do not treat search snippets as article reads.",
    ]
    for index, item in enumerate(selected, start=1):
        source_id = f"announcement_{index}"
        lines.extend(
            [
                "",
                f"Source ID: {source_id}",
                f"Title: {item.title}",
                f"URL: {item.url or '(not supplied)'}",
                f"Source: {item.source or 'RSS/channel context'}",
                f"RSS tags: {', '.join(item.relevance) if item.relevance else '(none)'}",
                f"RSS/channel snippet: {item.snippet or '(none supplied)'}",
            ]
        )
        for evidence_index, evidence in enumerate(item.evidence, start=1):
            evidence_id = f"{source_id}_{evidence.kind}_{evidence_index}"
            lines.extend(
                [
                    "",
                    f"Source ID: {evidence_id}",
                    f"Parent source ID: {source_id}",
                    f"Evidence kind: {evidence.kind}",
                    f"Title: {evidence.title or item.title}",
                    f"URL: {evidence.url or item.url or '(not supplied)'}",
                    f"Provider: {evidence.source}",
                    f"Status: {evidence.status}",
                    f"Chars read: {evidence.char_count}",
                    f"Extracted text or snippet: {_trim_words(evidence.snippet, 140)}",
                ]
            )
    return "\n".join(lines)


def _summaries_from_research_brief(
    brief: ResearchBrief,
    selected: list[AnnouncementLinkInput],
) -> list[AnnouncementResearchSummary]:
    summaries: list[AnnouncementResearchSummary] = []
    article_by_title = {
        _normalize_text(article.title): article for article in brief.article_summaries
    }
    article_by_source_id = {
        source_id: article
        for article in brief.article_summaries
        for source_id in article.source_ids
    }
    for index, item in enumerate(selected, start=1):
        source_id = f"announcement_{index}"
        article = article_by_source_id.get(source_id) or article_by_title.get(
            _normalize_text(item.title)
        )
        if article is not None:
            parts = [
                article.research_question,
                article.methods_or_design,
                " ".join(article.key_findings[:3]),
                "Limitations: " + "; ".join(article.limitations[:2]) if article.limitations else "",
                article.relevance_to_goal,
            ]
        else:
            facts = [fact.text for fact in brief.facts if source_id in set(fact.source_ids)][:3]
            parts = [*facts, *brief.inferences[:2]]
        source_basis = _announcement_summary_source_basis(item)
        summary_text = _trim_words(
            " ".join(part for part in parts if part).strip()
            or brief.summary
            or _summarize_announcement(item).summary,
            max(SUMMARY_WORD_TARGET - len(source_basis.split()), 60),
        )
        if source_basis:
            summary_text = f"{source_basis} {summary_text}"
        summaries.append(
            AnnouncementResearchSummary(
                title=item.title,
                url=item.url,
                source=item.source or "Business Research Analyst SDK synthesis",
                word_count=len(summary_text.split()),
                summary=summary_text,
                why_selected=_why_announcement_selected(item),
                evidence=list(item.evidence),
            )
        )
    return summaries


def _persist_announcement_research_records(
    result: AnnouncementResearchAutomationResult,
    *,
    links: list[AnnouncementLinkInput],
    selected: list[AnnouncementLinkInput],
    summaries: list[AnnouncementResearchSummary],
    database_url: str | None,
    automation_run_id: str,
) -> AnnouncementResearchAutomationResult:
    if not database_url:
        return result
    selected_keys = {_announcement_input_key(item) for item in selected}
    summaries_by_key = {_announcement_input_key(item): item for item in summaries}
    diagnostics = list(result.diagnostics)
    saved = 0
    try:
        store = SQLiteStore(database_url)
        for item in links:
            summary = summaries_by_key.get(_announcement_input_key(item))
            store.save_announcement_feed_item(
                _announcement_feed_record(
                    item,
                    summary=summary,
                    selected=_announcement_input_key(item) in selected_keys,
                    automation_run_id=automation_run_id,
                )
            )
            saved += 1
    except Exception as exc:
        diagnostics.append(
            f"Announcement feed persistence failed: {type(exc).__name__}: {exc}"
        )
    else:
        diagnostics.append(f"Persisted {saved} announcement feed item(s) to application data.")
    return result.model_copy(update={"diagnostics": diagnostics})


def _announcement_feed_record(
    item: AnnouncementLinkInput,
    *,
    summary: AnnouncementResearchSummary | None,
    selected: bool,
    automation_run_id: str,
) -> AnnouncementFeedItem:
    evidence = [
        AnnouncementFeedEvidence(
            kind=evidence_item.kind,
            title=evidence_item.title,
            url=evidence_item.url,
            snippet=evidence_item.snippet,
            source=evidence_item.source,
            status=evidence_item.status,
            char_count=evidence_item.char_count,
        )
        for evidence_item in (summary.evidence if summary is not None else item.evidence)
    ]
    tags = list(dict.fromkeys([*item.relevance, *item.tags]))
    summary_text = summary.summary if summary is not None else ""
    selection_reason = summary.why_selected if summary is not None else _why_announcement_selected(item)
    return AnnouncementFeedItem(
        title=item.title,
        url=item.url,
        doi=item.doi,
        arxiv_id=item.arxiv_id,
        biorxiv_id=item.biorxiv_id,
        medrxiv_id=item.medrxiv_id,
        source=item.source,
        feed=item.source,
        authors=item.authors,
        published_at=item.published_at,
        tags=tags,
        relevance_status="selected" if selected else "candidate",
        selected=selected,
        selection_reason=selection_reason if selected else "",
        summary=summary_text,
        evidence=evidence,
        content_hash=stable_hash(
            {
                "title": item.title,
                "url": item.url,
                "snippet": item.snippet,
                "summary": summary_text,
                "evidence": [record.model_dump(mode="json") for record in evidence],
            }
        ),
        automation_run_id=automation_run_id,
        slack_link=item.slack_link,
        review_metadata={
            "kind": "announcements-research",
            "selected": selected,
            "source_snippet": item.snippet,
            "source_basis": _announcement_summary_source_basis(item),
        },
    )


def _announcement_input_key(item: AnnouncementLinkInput | AnnouncementResearchSummary) -> str:
    if item.url:
        return item.url.strip().lower().rstrip("/")
    return _normalize_text(item.title)


def _attach_search_evidence(items: list[MeetingPrepItem]) -> list[str]:
    diagnostics: list[str] = _live_search_diagnostics()
    provider, error = _live_search_provider()
    if provider is None:
        return diagnostics + [error]
    for item in items:
        if item.search_note:
            diagnostics.append(f"Search skipped for `{item.title}`: {item.search_note}")
            continue
        for query in item.research_queries[:1]:
            try:
                evidence = _search_evidence(provider.search_web(query, num_results=3))
                matching = [
                    result for result in evidence if _meeting_search_result_matches(query, result)
                ]
                if matching:
                    item.evidence.extend(matching[:1])
                else:
                    diagnostics.append(
                        f"Search returned no specific matching lead for `{item.title}` using `{query}`."
                    )
            except (SearchProviderConfigurationError, SearchProviderError, OSError) as exc:
                diagnostics.append(f"Search failed for `{query}`: {type(exc).__name__}: {exc}")
    return diagnostics


def _attach_announcement_search_evidence(items: list[AnnouncementLinkInput]) -> list[str]:
    diagnostics: list[str] = _live_search_diagnostics()
    provider, error = _live_search_provider()
    if provider is None:
        return diagnostics + [error]
    for item in items:
        try:
            query = f"{item.title} {item.source}".strip()
            item.evidence.extend(_search_evidence(provider.search_web(query, num_results=2)))
        except (SearchProviderConfigurationError, SearchProviderError, OSError) as exc:
            diagnostics.append(f"Search failed for `{item.title}`: {type(exc).__name__}: {exc}")
        diagnostics.extend(_attach_announcement_article_evidence(item))
    return diagnostics


def _attach_announcement_article_evidence(item: AnnouncementLinkInput) -> list[str]:
    diagnostics: list[str] = []
    for url in _announcement_article_urls(item):
        try:
            extraction = extract_website_content(
                url,
                company_name=item.title,
                live=True,
            )
        except (WebsiteExtractionError, OSError) as exc:
            diagnostics.append(
                f"Article read failed for `{item.title}` from {url}: {type(exc).__name__}: {exc}"
            )
            continue
        text = extraction.text_or_markdown.strip()
        if len(text) < MIN_ARTICLE_READ_CHARS:
            diagnostics.append(
                f"Article read returned too little readable text for `{item.title}` from "
                f"{extraction.url}: {len(text)} chars."
            )
            continue
        item.evidence.append(
            SearchEvidence(
                kind="article",
                title=extraction.title or item.title,
                url=extraction.url,
                snippet=_article_excerpt(extraction.claims, text),
                source=extraction.provider,
                status=extraction.status,
                char_count=len(text),
            )
        )
        diagnostics.append(
            f"Read article for `{item.title}`: {extraction.provider} {extraction.status}, "
            f"{len(text)} chars from {extraction.url}"
        )
        return diagnostics
    if not diagnostics:
        diagnostics.append(
            f"Article read skipped for `{item.title}`: no http(s) source URL available."
        )
    return diagnostics


def _announcement_summary_source_basis(item: AnnouncementLinkInput) -> str:
    article = next((evidence for evidence in item.evidence if evidence.kind == "article"), None)
    if article is not None:
        return f"Source read: {article.source} {article.status}, {article.char_count} chars."
    search = next((evidence for evidence in item.evidence if evidence.kind == "search"), None)
    if search is not None:
        return f"Source basis: search-only fallback via {search.source}."
    return "Source basis: RSS/channel context only."


def _announcement_article_urls(item: AnnouncementLinkInput) -> list[str]:
    candidates: list[str] = []
    if _is_http_url(item.url):
        candidates.append(item.url)
    candidates.extend(
        evidence.url
        for evidence in item.evidence
        if evidence.kind == "search" and _is_http_url(evidence.url)
    )
    return list(dict.fromkeys(candidates))[:2]


def _article_excerpt(claims: list[str], text: str) -> str:
    if claims:
        return " ".join(claims[:2])
    return _trim_words(text, 55)


def _live_search_provider() -> tuple[Any | None, str]:
    try:
        provider = build_search_provider(os.environ.get("SEARCH_PROVIDER") or "searxng", live=True)
        return provider, ""
    except SearchProviderConfigurationError as exc:
        return None, f"Live search requested but not configured: {exc}"


def _live_search_diagnostics() -> list[str]:
    settings = load_settings()
    provider_name = os.environ.get("SEARCH_PROVIDER") or settings.search_provider or "searxng"
    diagnostics = [f"Live search provider: {provider_name}"]
    if str(provider_name).strip().lower() in {"searxng", ""}:
        base_url = settings.searxng_base_url or ""
        diagnostics.append(f"SearXNG base URL: {base_url or 'not configured'}")
        if base_url:
            diagnostics.append(_searxng_reachability_line(base_url))
    return diagnostics


def _searxng_reachability_line(base_url: str) -> str:
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        return "SearXNG reachability: not checked; base URL is invalid."
    query = urlencode({"q": "searxng health check", "format": "json"})
    probe_url = urlunparse((parsed.scheme, parsed.netloc, "/search", "", query, ""))
    try:
        with urllib.request.urlopen(probe_url, timeout=3) as response:
            return f"SearXNG reachability: HTTP {int(response.status)} from {probe_url}"
    except HTTPError as exc:
        return f"SearXNG reachability: HTTP {exc.code} from {probe_url}"
    except (URLError, TimeoutError, OSError) as exc:
        return f"SearXNG reachability: failed for {probe_url}: {type(exc).__name__}: {exc}"


def _search_evidence(results: list[SearchResult]) -> list[SearchEvidence]:
    return [
        SearchEvidence(
            title=result.title, url=result.link, snippet=result.snippet, source=result.source
        )
        for result in results
        if result.title and result.link
    ]


def _meeting_focus(*, title: str, note: str, score: int) -> str:
    if "grant" in title.lower() or "proposal" in title.lower():
        return "Clarify the opportunity, deadline, funder priorities, and any needed source-backed materials."
    if "intro" in title.lower() or "partner" in title.lower() or "collaboration" in title.lower():
        return "Review the counterpart, likely agenda, mutual fit, and one concrete next step."
    if "appointment" in title.lower() or "clinical" in title.lower():
        return "Prepare context, logistics, and any questions that should be ready before the appointment."
    if note:
        return f"Use the calendar note as the prep anchor: {note}"
    if score >= 2:
        return "Prepare a concise context brief, agenda hypotheses, and follow-up options."
    return "Confirm whether prep is needed and capture any missing context."


def _meeting_queries(title: str, row: dict[str, Any]) -> tuple[list[str], str]:
    supplied_urls = _urls_from_text(
        " ".join(
            str(row.get(key, "")) for key in ("url", "link", "description", "note", "location")
        )
    )
    if supplied_urls:
        return supplied_urls[:1], ""
    base = _clean_query(title)
    attendees = " ".join(_string_list(row.get("attendees")))
    distinctive_terms = _distinctive_meeting_terms(" ".join([title, attendees]))
    if not distinctive_terms:
        return [], "calendar item is internal or too generic for a reliable external web lead"
    queries = [base]
    if attendees:
        queries.append(_clean_query(f"{title} {attendees}"))
    return [query for query in queries if query], ""


def _distinctive_meeting_terms(text: str) -> set[str]:
    tokens = {token.lower() for token in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", text)}
    return {token for token in tokens if token not in MEETING_SEARCH_STOPWORDS}


def _meeting_search_result_matches(query: str, evidence: SearchEvidence) -> bool:
    distinctive_terms = _distinctive_meeting_terms(query)
    if not distinctive_terms:
        return False
    haystack = " ".join([evidence.title, evidence.url, evidence.snippet]).lower()
    matches = {term for term in distinctive_terms if term in haystack}
    if len(distinctive_terms) == 1:
        return bool(matches)
    return len(matches) >= min(2, len(distinctive_terms))


def _urls_from_text(text: str) -> list[str]:
    urls = re.findall(r"https?://[^\s<>)]+", text or "")
    return [url.rstrip(".,;:") for url in urls if _is_http_url(url.rstrip(".,;:"))]


def _announcement_score(item: AnnouncementLinkInput) -> int:
    text = " ".join([item.title, item.snippet, item.source, " ".join(item.relevance)])
    score = _term_score(text, ANNOUNCEMENT_RELEVANCE_TERMS)
    if item.url:
        score += 1
    return score


def _why_announcement_selected(item: AnnouncementLinkInput) -> str:
    text = " ".join([item.title, item.snippet, " ".join(item.relevance)]).lower()
    matches = [term for term in sorted(ANNOUNCEMENT_RELEVANCE_TERMS) if term in text]
    if matches:
        return f"Matched Keystone-relevant topic(s): {', '.join(matches[:4])}."
    return "Selected as one of the strongest available links in the supplied context."


def _term_score(text: str, terms: set[str]) -> int:
    lowered = text.lower()
    return sum(1 for term in terms if term in lowered)


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list | tuple | set):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _is_http_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _clean_query(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 .:&/-]", " ", value)).strip()


def _trim_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).rstrip(".,;:") + "."


def automation_payload_metadata() -> dict[str, str]:
    """Return stable read-only metadata for downstream logs."""

    return {
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "external_writes_enabled": "false",
        "calendar_writes_enabled": "false",
        "gmail_writes_enabled": "false",
        "crm_writes_enabled": "false",
    }
