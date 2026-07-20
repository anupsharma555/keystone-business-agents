#!/usr/bin/env python3
"""Run one compact, no-tool Opportunity Scout assessment for the ANU-61 pilot."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from keystone_agents.agents.opportunity_scout import build_opportunity_assessment_agent
from keystone_agents.config import load_settings
from keystone_agents.controlled_pilot import controlled_pilot_cases
from keystone_agents.models import OpportunityScoutSDKInput
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.opportunity import OpportunityAssessmentBrief
from scripts.run_opportunity_normalization_validation import SOURCE_PACKET, _load_packet

MODEL = "gpt-5.4-mini"
MAX_REQUESTS = 1
MAX_COST_USD = 0.10


def pilot_ask() -> str:
    return next(
        case.natural_ask
        for case in controlled_pilot_cases()
        if case.case_id == "current_opportunity_assessment"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-packet", type=Path, default=SOURCE_PACKET)
    parser.add_argument("--request-text")
    parser.add_argument("--inline-source-context")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--max-openai-requests", type=int, default=MAX_REQUESTS)
    parser.add_argument("--budget-usd", type=float, default=MAX_COST_USD)
    parser.add_argument("--revalidate-receipt", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/controlled-pilot-opportunity-compact.json"),
    )
    parser.add_argument(
        "--save-output",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Persist the local JSON receipt. Direct Slack calls disable this.",
    )
    return parser


def compact_input(
    packet: dict[str, Any],
    *,
    request_text: str | None = None,
) -> OpportunityScoutSDKInput:
    context = {
        "execution_contract": {
            "attach_tools": False,
            "live_search": False,
            "provider_operations": 0,
            "external_actions": 0,
        },
        "response_contract": {
            "one_opportunity": True,
            "confirmed_facts_cite_retained_source_ids": True,
            "separate_interpretation_from_facts": True,
            "retain_uncertainty": True,
            "review_only": True,
            "avoid_duplicate_source_or_pipeline_layers": True,
        },
        "source_packet": packet,
    }
    return OpportunityScoutSDKInput(
        topic=str(request_text or pilot_ask()).strip(),
        max_results=1,
        context=json.dumps(context, ensure_ascii=True, sort_keys=True),
    )


def inline_source_packet(request_text: str, source_context: str) -> dict[str, Any]:
    """Normalize one bounded operator-supplied opportunity source without retrieval."""

    context = " ".join(str(source_context or "").split()).strip()
    if not context:
        raise ValueError("Inline opportunity source context is required.")
    urls = list(
        dict.fromkeys(
            match.rstrip(".,);]")
            for match in re.findall(r"https://[^\s<>]+", context)
        )
    )
    sources = [
        {
            "source_id": f"operator:inline-opportunity-context:{index}",
            "title": "Operator-provided opportunity context",
            "url": url,
            "source_type": "unknown",
            "facts": [context],
        }
        for index, url in enumerate(urls, start=1)
    ]
    if not sources:
        sources = [
            {
                "source_id": "operator:inline-opportunity-context:1",
                "title": "Operator-provided opportunity context",
                "url": "operator://inline-opportunity-context",
                "source_type": "unknown",
                "facts": [context],
            }
        ]
    return {
        "request_text": str(request_text or "").strip(),
        "source_scope": "operator_provided_only",
        "external_verification_performed": False,
        "sources": sources,
    }


def compact_opportunity_human_summary(brief: OpportunityAssessmentBrief) -> str:
    """Render the bounded assessment as operator-facing judgment, not run metadata."""

    confirmed = " ".join(
        fact.statement.strip()
        for fact in brief.confirmed_facts
        if fact.statement.strip()
    )
    missing = [item.strip() for item in brief.missing_evidence if item.strip()]
    limitations = " ".join(missing)
    gap = missing[0] if missing else "Independent validation evidence is still needed."
    lines = [
        f"What the supplied context establishes: {confirmed}",
        (
            "What it does not establish: "
            + (limitations or "The evidence needed to validate the opportunity is incomplete.")
        ),
        f"Most credible KNI opportunity: {brief.keystone_fit.strip()}",
        f"Assessment: {brief.interpretation.strip()}",
        f"Single most important validation gap: {gap}",
        f"Next safe action: {brief.next_safe_action.strip()}",
    ]
    return "\n\n".join(line for line in lines if line.split(":", 1)[-1].strip())


def validate_payload(
    result: Any,
    *,
    budget_usd: float,
    pilot_contract: bool = True,
) -> dict[str, Any]:
    brief = OpportunityAssessmentBrief.model_validate(result.final_output)
    human_summary = compact_opportunity_human_summary(brief)
    source_urls = {source.url for source in brief.retained_sources}
    combined = " ".join(
        [
            brief.interpretation,
            brief.keystone_fit,
            brief.timing_status,
            brief.geography_status,
            *brief.missing_evidence,
            brief.next_safe_action,
        ]
    ).lower()
    checks: dict[str, bool] = {
        "exact_request_count": int(result.usage.get("requests") or 0) == 1,
        "within_budget": float(result.cost.get("estimated_usd") or 0) <= budget_usd,
        "no_retry": int(result.request_cache.get("rate_limit_retries") or 0) == 0,
        "facts_and_interpretation_separated": bool(brief.confirmed_facts)
        and bool(brief.interpretation.strip()),
        "retained_source_identity_present": bool(source_urls),
        "no_outreach_or_external_action": not brief.outreach_recommended
        and not brief.external_action_performed,
    }
    if pilot_contract:
        checks.update(
            {
                "exact_sources_retained_once": source_urls
                == {
                    "https://hack-for-humanity-summer-26.devpost.com/",
                    "https://hack-for-humanity-summer-26.devpost.com/details/dates",
                }
                and len(brief.retained_sources) == 2,
                "unknown_geography_retained": any(
                    phrase in brief.geography_status.lower()
                    for phrase in (
                        "unknown",
                        "not established",
                        "not provided",
                        "not stated",
                    )
                ),
                "upcoming_timing_retained": any(
                    term in brief.timing_status.lower()
                    for term in ("upcoming", "not yet open", "august 7")
                ),
                "conditional_safe_action": any(
                    term in combined
                    for term in ("conditional", "eligib", "prototype", "portfolio")
                ),
            }
        )
    return {
        "schema": "keystone.opportunity.compact_assessment_evidence.v1",
        "status": "pass" if all(checks.values()) else "partial",
        "case_id": "current_opportunity_assessment",
        "model": MODEL,
        "natural_request": pilot_ask(),
        "checks": checks,
        "usage": result.usage,
        "cost": result.cost,
        "request_cache": result.request_cache,
        "output_type": "OpportunityAssessmentBrief",
        "human_summary": human_summary,
        "slack_display_text": human_summary,
        "display_text": human_summary,
        "safety": {
            "tools_attached": False,
            "live_search": False,
            "provider_reads": 0,
            "provider_writes": 0,
            "external_action_performed": False,
        },
        "output": brief.model_dump(mode="json"),
    }


def main() -> int:
    args = build_parser().parse_args()
    if args.model != MODEL or args.max_openai_requests != MAX_REQUESTS:
        raise SystemExit("Compact opportunity assessment requires gpt-5.4-mini and one request.")
    if args.budget_usd <= 0 or args.budget_usd > MAX_COST_USD:
        raise SystemExit("Compact opportunity assessment requires a budget at or below $0.10.")
    os.environ["KEYSTONE_AGENT_RUN_BUDGET_USD"] = str(args.budget_usd)
    os.environ["KEYSTONE_LIVE_MODEL_MAX_RETRIES"] = "0"
    os.environ["KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES"] = "0"
    inline_mode = bool(args.inline_source_context)
    if inline_mode != bool(args.request_text):
        raise SystemExit(
            "--request-text and --inline-source-context must be supplied together."
        )
    if args.revalidate_receipt is not None:
        saved = json.loads(args.revalidate_receipt.read_text(encoding="utf-8"))
        result = SimpleNamespace(
            final_output=OpportunityAssessmentBrief.model_validate(saved["output"]),
            usage=saved["usage"],
            cost=saved["cost"],
            request_cache=saved["request_cache"],
        )
        payload = validate_payload(result, budget_usd=args.budget_usd)
        payload["revalidated_from"] = str(args.revalidate_receipt)
        payload["validation_revision"] = (
            "Accept explicit bounded geography uncertainty without requiring the literal "
            "word unknown. No model call was made."
        )
        if args.save_output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.output.with_suffix(args.output.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            temporary.replace(args.output)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["status"] == "pass" else 2
    load_settings(force_dotenv=True)
    request_text = str(args.request_text or pilot_ask()).strip()
    packet = (
        inline_source_packet(request_text, str(args.inline_source_context or ""))
        if inline_mode
        else _load_packet(args.source_packet)
    )
    typed_input = compact_input(packet, request_text=request_text)
    result = run_typed_sdk_agent(
        agent=build_opportunity_assessment_agent(model=args.model, request_text=request_text),
        typed_input=typed_input,
        output_type=OpportunityAssessmentBrief,
        live=True,
        workflow_name="Keystone compact supplied-opportunity assessment",
        tracing_disabled=True,
        trace_include_sensitive_data=False,
        max_turns=1,
    )
    payload = validate_payload(
        result,
        budget_usd=args.budget_usd,
        pilot_contract=not inline_mode,
    )
    payload["natural_request"] = request_text
    payload["source_scope"] = (
        "operator_provided_only" if inline_mode else "controlled_pilot_packet"
    )
    if args.save_output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        temporary.replace(args.output)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
