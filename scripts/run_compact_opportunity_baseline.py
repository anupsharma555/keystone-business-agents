#!/usr/bin/env python3
"""Run a matched no-tool generic baseline for the compact Opportunity assessment."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from keystone_agents.config import load_settings
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.opportunity import OpportunityAssessmentBrief
from keystone_agents.sdk import build_model_settings, build_sdk_agent
from scripts.run_compact_opportunity_assessment import (
    MAX_COST_USD,
    MODEL,
    SOURCE_PACKET,
    compact_input,
    pilot_ask,
    validate_payload,
)
from scripts.run_opportunity_normalization_validation import _load_packet


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-packet", type=Path, default=SOURCE_PACKET)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--max-openai-requests", type=int, default=1)
    parser.add_argument("--budget-usd", type=float, default=MAX_COST_USD)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/controlled-pilot-opportunity-baseline.json"),
    )
    return parser


def build_baseline_agent(*, model: str = MODEL):
    """Build the generic structured baseline without KBA prompts, skills, or tools."""

    return build_sdk_agent(
        name="codex_chatgpt_baseline",
        instructions=(
            "Answer the user's supplied-source opportunity question accurately and concisely. "
            "Separate confirmed facts from interpretation, cite only retained source IDs, "
            "state uncertainty, recommend one safe next action, and perform no external action."
        ),
        output_type=OpportunityAssessmentBrief,
        tools=[],
        model=model,
        model_settings=build_model_settings(
            reasoning_effort="low",
            verbosity="low",
        ),
        handoff_description="Generic matched baseline for one supplied opportunity packet.",
    )


def main() -> int:
    args = build_parser().parse_args()
    if args.model != MODEL or args.max_openai_requests != 1:
        raise SystemExit("Matched Opportunity baseline requires gpt-5.4-mini and one request.")
    if args.budget_usd <= 0 or args.budget_usd > MAX_COST_USD:
        raise SystemExit("Matched Opportunity baseline requires a budget at or below $0.10.")
    os.environ["KEYSTONE_AGENT_RUN_BUDGET_USD"] = str(args.budget_usd)
    os.environ["KEYSTONE_LIVE_MODEL_MAX_RETRIES"] = "0"
    os.environ["KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES"] = "0"
    load_settings(force_dotenv=True)
    result = run_typed_sdk_agent(
        agent=build_baseline_agent(model=args.model),
        typed_input=compact_input(_load_packet(args.source_packet)),
        output_type=OpportunityAssessmentBrief,
        live=True,
        workflow_name="Matched generic compact opportunity baseline",
        tracing_disabled=True,
        trace_include_sensitive_data=False,
        max_turns=1,
    )
    payload = validate_payload(result, budget_usd=args.budget_usd)
    payload.update(
        {
            "schema": "keystone.opportunity.compact_baseline_evidence.v1",
            "system": "codex_chatgpt_baseline",
            "natural_request": pilot_ask(),
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(args.output)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
