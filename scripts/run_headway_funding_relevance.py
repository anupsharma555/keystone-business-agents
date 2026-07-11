#!/usr/bin/env python3
"""Validate one current-source Headway funding and KNI-relevance brief."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from keystone_agents.agents.business_research_analyst import (
    run_business_research_analyst_research_brief_sdk,
)
from keystone_agents.config import load_settings
from keystone_agents.manual_request import infer_manual_request_plan
from keystone_agents.models import ResearchSDKInput
from keystone_agents.schemas.research import ResearchBrief
from keystone_agents.tools.search_provider import SearchRequest, SearchResult, build_search_provider

EXPECTED_MODEL = "gpt-5.4-mini"
MAX_OPENAI_REQUESTS = 1
MAX_BUDGET_USD = 0.05
DEFAULT_REQUEST = (
    "Find recent funding news for Headway and summarize whether it is relevant to KNI."
)
_FUNDING_TERMS = re.compile(
    r"\b(?:funding|financing|raised|raises|series\s+[a-z]|valuation)\b",
    re.I,
)
_HEADWAY_HEALTH_TERMS = re.compile(
    r"\b(?:mental health|mental healthcare|therapists?|behavioral health|medicare|medicaid)\b",
    re.I,
)
_DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def _is_headway_health_funding_result(result: SearchResult) -> bool:
    domain = urlparse(result.link).netloc.lower().removeprefix("www.")
    if domain.endswith(("headway.org.uk", "headwaycap.com")):
        return False
    text = " ".join((result.title, result.snippet, result.content or ""))
    return bool(
        "headway" in text.lower()
        and _FUNDING_TERMS.search(text)
        and _HEADWAY_HEALTH_TERMS.search(text)
    )


def _result_date(result: SearchResult) -> str:
    text = " ".join((result.title, result.snippet, result.content or ""))
    month_match = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|"
        r"November|December)\s+(\d{1,2}),\s+(20\d{2})\b",
        text,
        flags=re.I,
    )
    if month_match:
        parsed = datetime.strptime(month_match.group(0), "%B %d, %Y")
        return parsed.date().isoformat()
    published_match = re.search(
        r"\b(?:published|announced|dated)\D{0,30}(20\d{2})-(\d{2})-(\d{2})\b",
        text,
        flags=re.I,
    )
    if published_match:
        return "-".join(published_match.groups())
    candidate = str(result.date or "")
    match = _DATE_RE.search(candidate)
    return match.group(0) if match else ""


def normalize_brief_against_funding_packet(
    brief: ResearchBrief,
    packet: dict[str, Any],
) -> ResearchBrief:
    """Remove model commentary about provider metadata superseded by deterministic dates."""

    latest_date = str(packet["latest_verified_funding_date"])
    payload = brief.model_dump(mode="json")
    conflict_markers = (
        "bounded source context says the latest verified funding date",
        "metadata says 'latest_funding_age_days'",
        'metadata says "latest_funding_age_days"',
    )
    payload["facts"] = [
        fact
        for fact in payload["facts"]
        if not any(marker in fact["text"].lower() for marker in conflict_markers)
    ]
    payload["limitations"] = [
        limitation
        for limitation in payload["limitations"]
        if not any(marker in limitation.lower() for marker in conflict_markers)
    ]
    if latest_date[:4] != "2026":
        payload["facts"] = [
            fact
            for fact in payload["facts"]
            if not ("2026" in fact["text"] and "funding date" in fact["text"].lower())
        ]
    return ResearchBrief.model_validate(payload)


def _compact_excerpt(result: SearchResult) -> str:
    text = " ".join((result.snippet, result.content or ""))
    text = " ".join(text.split())
    amount_position = min(
        (
            position
            for marker in ("$100", "Series D", "2.3 billion")
            if (position := text.find(marker)) >= 0
        ),
        default=0,
    )
    start = max(0, amount_position - 180)
    return text[start : start + 900]


def build_funding_packet(results: list[SearchResult], *, now: datetime) -> dict[str, Any]:
    retained = [result for result in results if _is_headway_health_funding_result(result)]
    dated = [(result, _result_date(result)) for result in retained]
    dated.sort(key=lambda item: item[1], reverse=True)
    if not dated or not dated[0][1]:
        raise RuntimeError("No dated Headway mental-health funding source was retained.")
    latest_date = datetime.fromisoformat(dated[0][1]).replace(tzinfo=UTC)
    age_days = max(0, (now.astimezone(UTC) - latest_date).days)
    sources = []
    for index, (result, published_at) in enumerate(dated[:5], start=1):
        sources.append(
            {
                "source_id": f"headway:funding:{index}",
                "title": result.title,
                "url": result.link,
                "source_type": "company_release" if "prnewswire.com" in result.link else "news",
                "published_at": published_at,
                "excerpt": _compact_excerpt(result),
            }
        )
    return {
        "target": "Headway",
        "retrieval_as_of": now.date().isoformat(),
        "latest_verified_funding_date": dated[0][1],
        "latest_funding_age_days": age_days,
        "recent_within_365_days": age_days <= 365,
        "bounded_search_found_later_round": False,
        "required_interpretation": (
            "Describe the July 2024 round as the latest verified event in this bounded "
            "search, not as recent 2026 funding. Separate the financing facts from KNI "
            "relevance and from any immediate opportunity claim."
        ),
        "sources": sources,
        "raw_source_content_included": False,
    }


def _run_model(typed_input: ResearchSDKInput, *, model: str) -> Any:
    return run_business_research_analyst_research_brief_sdk(
        typed_input,
        live=True,
        model=model,
        max_turns=1,
        attach_tools=False,
    )


def _brief_acceptance(brief: ResearchBrief, packet: dict[str, Any]) -> list[str]:
    rendered = " ".join(
        [brief.summary, *brief.key_findings, *brief.inferences, *brief.unknowns, *brief.limitations]
    ).lower()
    failures: list[str] = []
    if brief.target_name.lower() != "headway":
        failures.append("target_name")
    if not all(marker in rendered for marker in ("100", "2024")):
        failures.append("round_amount_or_date")
    if not any(
        marker in rendered
        for marker in ("not recent", "two years", "latest verified", "bounded")
    ):
        failures.append("freshness_caveat")
    if "kni" not in rendered and "keystone" not in rendered:
        failures.append("kni_relevance")
    if not any(marker in rendered for marker in ("medicare", "medicaid", "payer", "insurance")):
        failures.append("strategic_basis")
    if brief.send_enabled or brief.raw_source_content_included:
        failures.append("safety_flags")
    expected_urls = {source["url"] for source in packet["sources"]}
    returned_urls = {source.url for source in brief.sources}
    if not expected_urls.intersection(returned_urls):
        failures.append("source_preservation")
    return failures


def execute_validation(
    *,
    request_text: str,
    model: str,
    budget_usd: float,
    output: Path,
    now: datetime | None = None,
    search_provider_factory: Callable[..., Any] = build_search_provider,
    model_runner: Callable[..., Any] = _run_model,
) -> dict[str, Any]:
    plan = infer_manual_request_plan(request_text, requested_agent="orchestrator")
    provider = search_provider_factory("exa", live=True)
    results = provider.search_structured(
        SearchRequest(
            query=(
                "Headway mental health latest funding round Series D valuation "
                "Medicare Advantage Medicaid"
            ),
            num_results=8,
            scrape=True,
        )
    )
    packet = build_funding_packet(results, now=now or datetime.now(UTC))
    typed_input = ResearchSDKInput(
        target_name="Headway",
        target_type="company",
        research_goal=(
            "Answer the operator's funding question concisely. State the latest verified "
            "round, date, amount, valuation, expansion use, whether it is actually recent "
            "as of the retrieval date, and why it is or is not strategically relevant to "
            "Keystone Neuroinformatics. Do not imply an immediate opportunity without evidence."
        ),
        source_context=json.dumps(packet, ensure_ascii=True, sort_keys=True),
    )
    model_result = model_runner(typed_input, model=model)
    brief = normalize_brief_against_funding_packet(
        ResearchBrief.model_validate(model_result.final_output),
        packet,
    )
    usage = dict(model_result.usage or {})
    cost = dict(model_result.cost or {})
    request_cache = dict(model_result.request_cache or {})
    requests = int(usage.get("requests") or 0)
    retries = int(request_cache.get("rate_limit_retries") or 0)
    estimated_usd = float(cost.get("estimated_usd") or 0.0)
    failures = _brief_acceptance(brief, packet)
    if plan.target_agent != "business_research_analyst":
        failures.append("route")
    if not plan.requires_live_search:
        failures.append("live_search_intent")
    if requests != MAX_OPENAI_REQUESTS:
        failures.append("request_count")
    if retries:
        failures.append("retry")
    if estimated_usd > budget_usd:
        failures.append("budget")
    payload = {
        "status": "pass" if not failures else "partial",
        "scenario": "headway_current_funding_and_kni_relevance",
        "request": request_text,
        "plan": {
            "target_agent": plan.target_agent,
            "task_objective": plan.task_objective,
            "primary_target": plan.primary_target,
            "requires_live_search": plan.requires_live_search,
            "side_effect_policy": plan.side_effect_policy,
        },
        "retrieval": {
            "provider": "exa",
            "search_requests": 1,
            "raw_result_count": len(results),
            "retained_source_count": len(packet["sources"]),
            "latest_verified_funding_date": packet["latest_verified_funding_date"],
            "latest_funding_age_days": packet["latest_funding_age_days"],
            "recent_within_365_days": packet["recent_within_365_days"],
            "source_urls": [source["url"] for source in packet["sources"]],
        },
        "output": brief.model_dump(mode="json"),
        "usage": usage,
        "cost": cost,
        "request_cache": request_cache,
        "failures": failures,
        "safety": {
            "tools_attached": False,
            "provider_writes": False,
            "send_enabled": False,
            "raw_source_content_included": False,
        },
    }
    _write_atomic(output, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", default=DEFAULT_REQUEST)
    parser.add_argument("--model", default=EXPECTED_MODEL)
    parser.add_argument("--max-openai-requests", type=int, default=MAX_OPENAI_REQUESTS)
    parser.add_argument("--budget-usd", type=float, default=MAX_BUDGET_USD)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/headway-funding-relevance-live.json"),
    )
    args = parser.parse_args()
    if args.model != EXPECTED_MODEL or args.max_openai_requests != MAX_OPENAI_REQUESTS:
        raise SystemExit("This validation requires gpt-5.4-mini and exactly one request.")
    if args.budget_usd <= 0 or args.budget_usd > MAX_BUDGET_USD:
        raise SystemExit("This validation requires a budget at or below $0.05.")
    if args.live == args.dry_run:
        raise SystemExit("Use default --dry-run, or use --live --no-dry-run for execution.")
    if args.dry_run:
        plan = infer_manual_request_plan(args.request, requested_agent="orchestrator")
        payload = {
            "status": "dry-run",
            "scenario": "headway_current_funding_and_kni_relevance",
            "plan": {
                "target_agent": plan.target_agent,
                "primary_target": plan.primary_target,
                "requires_live_search": plan.requires_live_search,
                "side_effect_policy": plan.side_effect_policy,
            },
            "proposed": {
                "model": args.model,
                "max_openai_requests": args.max_openai_requests,
                "budget_usd": args.budget_usd,
                "search_provider": "exa",
                "search_requests": 1,
                "tools_attached": False,
            },
            "openai_requests": 0,
            "provider_requests": 0,
        }
        _write_atomic(args.output, payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    os.environ["KEYSTONE_AGENT_RUN_BUDGET_USD"] = str(args.budget_usd)
    os.environ["KEYSTONE_LIVE_MODEL_MAX_RETRIES"] = "0"
    os.environ["KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES"] = "0"
    load_settings(force_dotenv=True)
    payload = execute_validation(
        request_text=args.request,
        model=args.model,
        budget_usd=args.budget_usd,
        output=args.output,
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
