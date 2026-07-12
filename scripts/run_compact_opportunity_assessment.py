#!/usr/bin/env python3
"""Run one compact, no-tool Opportunity Scout assessment for the ANU-61 pilot."""

from __future__ import annotations

import argparse
import json
import os
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
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--max-openai-requests", type=int, default=MAX_REQUESTS)
    parser.add_argument("--budget-usd", type=float, default=MAX_COST_USD)
    parser.add_argument("--revalidate-receipt", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/controlled-pilot-opportunity-compact.json"),
    )
    return parser


def compact_input(packet: dict[str, Any]) -> OpportunityScoutSDKInput:
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
        topic=pilot_ask(),
        max_results=1,
        context=json.dumps(context, ensure_ascii=True, sort_keys=True),
    )


def validate_payload(result: Any, *, budget_usd: float) -> dict[str, Any]:
    brief = OpportunityAssessmentBrief.model_validate(result.final_output)
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
    checks = {
        "exact_request_count": int(result.usage.get("requests") or 0) == 1,
        "within_budget": float(result.cost.get("estimated_usd") or 0) <= budget_usd,
        "no_retry": int(result.request_cache.get("rate_limit_retries") or 0) == 0,
        "exact_sources_retained_once": source_urls
        == {
            "https://hack-for-humanity-summer-26.devpost.com/",
            "https://hack-for-humanity-summer-26.devpost.com/details/dates",
        }
        and len(brief.retained_sources) == 2,
        "facts_and_interpretation_separated": bool(brief.confirmed_facts)
        and bool(brief.interpretation.strip()),
        "unknown_geography_retained": any(
            phrase in brief.geography_status.lower()
            for phrase in ("unknown", "not established", "not provided", "not stated")
        ),
        "upcoming_timing_retained": any(
            term in brief.timing_status.lower()
            for term in ("upcoming", "not yet open", "august 7")
        ),
        "conditional_safe_action": any(
            term in combined for term in ("conditional", "eligib", "prototype", "portfolio")
        ),
        "no_outreach_or_external_action": not brief.outreach_recommended
        and not brief.external_action_performed,
    }
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
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        temporary.replace(args.output)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["status"] == "pass" else 2
    load_settings(force_dotenv=True)
    typed_input = compact_input(_load_packet(args.source_packet))
    result = run_typed_sdk_agent(
        agent=build_opportunity_assessment_agent(model=args.model, request_text=pilot_ask()),
        typed_input=typed_input,
        output_type=OpportunityAssessmentBrief,
        live=True,
        workflow_name="Keystone compact supplied-opportunity assessment",
        tracing_disabled=True,
        trace_include_sensitive_data=False,
        max_turns=1,
    )
    payload = validate_payload(result, budget_usd=args.budget_usd)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(args.output)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
