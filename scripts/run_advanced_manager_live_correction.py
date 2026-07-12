#!/usr/bin/env python3
"""Run one bounded two-turn live manager correction proof with no tools or providers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from keystone_agents.config import load_settings
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.manager_validation import AdvancedManagerRouteDecision
from keystone_agents.sdk import (
    build_model_settings,
    build_sdk_agent,
    build_sqlite_session,
    compose_instructions,
)

MODEL = "gpt-5.4-mini"
INITIAL_REQUEST = (
    "Find current NeuroFlow partnership or opportunity signals and prepare outreach "
    "next steps for review, but do not send anything."
)
CORRECTION_REQUEST = (
    "Correction: do not scout opportunities. Research NeuroFlow only and return a "
    "read-only company brief."
)


def build_manager_validation_agent(*, model: str = MODEL):
    instructions = compose_instructions(
        "safety_policy.md",
        "advanced_manager_route_compact.md",
        skill_files=("action_boundary_enforcement",),
        shared_prompt_files=("memory_policy.md", "writing_style.md"),
    )
    return build_sdk_agent(
        name="orchestrator",
        instructions=instructions,
        output_type=AdvancedManagerRouteDecision,
        tools=[],
        model=model,
        model_settings=build_model_settings(reasoning_effort="low", verbosity="low"),
        policy_agent_name="orchestrator",
        handoff_description="Select current manager ownership after an operator correction.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--max-openai-requests", type=int, default=2)
    parser.add_argument("--max-total-cost-usd", type=float, default=0.10)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/advanced-manager-live-correction.json"),
    )
    return parser


def _run_turn(agent, session, request: str):
    return run_typed_sdk_agent(
        agent=agent,
        typed_input={
            "current_operator_request": request,
            "contract": (
                "Use the latest instruction, return route metadata only, and perform no "
                "tool, specialist, provider, send, write, or post action."
            ),
        },
        output_type=AdvancedManagerRouteDecision,
        live=True,
        workflow_name="Keystone advanced manager correction validation",
        tracing_disabled=True,
        trace_include_sensitive_data=False,
        session=session,
        max_turns=1,
    )


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def main() -> int:
    args = build_parser().parse_args()
    if args.model != MODEL or args.max_openai_requests != 2:
        raise SystemExit("Manager correction validation requires gpt-5.4-mini and two requests.")
    if args.max_total_cost_usd <= 0 or args.max_total_cost_usd > 0.10:
        raise SystemExit("Manager correction validation requires a total budget at or below $0.10.")
    os.environ["KEYSTONE_AGENT_RUN_BUDGET_USD"] = str(args.max_total_cost_usd)
    os.environ["KEYSTONE_LIVE_MODEL_MAX_RETRIES"] = "0"
    os.environ["KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES"] = "0"
    load_settings(force_dotenv=True)
    agent = build_manager_validation_agent(model=args.model)
    with TemporaryDirectory(prefix="kba-manager-session-") as directory:
        session = build_sqlite_session(
            "advanced-manager-correction-validation",
            Path(directory) / "session.sqlite3",
            session_history_limit=8,
        )
        first = _run_turn(agent, session, INITIAL_REQUEST)
        second = _run_turn(agent, session, CORRECTION_REQUEST)
    first_output = AdvancedManagerRouteDecision.model_validate(first.final_output)
    second_output = AdvancedManagerRouteDecision.model_validate(second.final_output)
    requests = int(first.usage.get("requests") or 0) + int(second.usage.get("requests") or 0)
    total_cost = float(first.cost.get("estimated_usd") or 0) + float(
        second.cost.get("estimated_usd") or 0
    )
    checks = {
        "exact_request_count": requests == 2,
        "first_turn_is_cross_agent_manager_owned": first_output.selected_owner
        == "chief_of_staff",
        "first_turn_blocks_side_effects": not first_output.side_effects_allowed,
        "correction_routes_to_business_research": second_output.selected_owner
        == "business_research_analyst",
        "correction_rejects_stale_scouting": "opportunity_scout"
        in second_output.rejected_owners,
        "correction_rejects_stale_outreach": "outreach_composer"
        in second_output.rejected_owners,
        "newest_instruction_applied": second_output.latest_instruction_applied,
        "prior_direction_considered": second_output.prior_direction_considered,
        "read_only_boundary": second_output.operation_boundary == "read_only",
        "no_side_effects": not second_output.side_effects_allowed,
        "session_attached_both_turns": bool(first.request_cache.get("session_attached"))
        and bool(second.request_cache.get("session_attached")),
        "within_total_budget": total_cost <= args.max_total_cost_usd,
        "no_retry": int(first.request_cache.get("rate_limit_retries") or 0) == 0
        and int(second.request_cache.get("rate_limit_retries") or 0) == 0,
    }
    payload = {
        "schema": "keystone.advanced_manager_live_correction.v1",
        "status": "pass" if all(checks.values()) else "partial",
        "model": args.model,
        "request_hashes": [_hash(INITIAL_REQUEST), _hash(CORRECTION_REQUEST)],
        "checks": checks,
        "requests": requests,
        "estimated_cost_usd": round(total_cost, 8),
        "turns": [
            {
                "output": first_output.model_dump(mode="json"),
                "usage": first.usage,
                "cost": first.cost,
                "request_cache": first.request_cache,
            },
            {
                "output": second_output.model_dump(mode="json"),
                "usage": second.usage,
                "cost": second.cost,
                "request_cache": second.request_cache,
            },
        ],
        "safety": {
            "tools_attached": False,
            "provider_reads": 0,
            "provider_writes": 0,
            "send_enabled": False,
            "post_enabled": False,
            "session_persisted_after_run": False,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(args.output)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
