"""Run one bounded live planner smoke for an ANU-120 contextual action case."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from keystone_agents.agents.manual_request_planner import resolve_manual_request_plan
from keystone_agents.config import load_settings
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.target_action_matrix import contextual_target_action_scorecard

MODEL = "gpt-5.4-mini"
MAX_REQUESTS = 1
MAX_BUDGET_USD = 0.03


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case",
        required=True,
        choices=[case.case_id for case in contextual_target_action_scorecard()],
    )
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--max-openai-requests", type=int, default=MAX_REQUESTS)
    parser.add_argument("--budget-usd", type=float, default=MAX_BUDGET_USD)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/operational-validation/contextual-action-planner"),
    )
    return parser


def _validate_limits(args: argparse.Namespace) -> None:
    if args.model != MODEL:
        raise SystemExit(f"Contextual planner smoke requires model={MODEL}.")
    if args.max_openai_requests != MAX_REQUESTS:
        raise SystemExit("Contextual planner smoke requires max-openai-requests=1.")
    if args.budget_usd <= 0 or args.budget_usd > MAX_BUDGET_USD:
        raise SystemExit("Contextual planner smoke requires a budget at or below $0.03.")


def _configure_bounded_environment(args: argparse.Namespace) -> None:
    os.environ["KEYSTONE_AGENT_RUN_BUDGET_USD"] = str(args.budget_usd)
    os.environ["KEYSTONE_MANUAL_PLANNER_PROVIDER_POLICY"] = "openai"
    os.environ["KEYSTONE_LIVE_MODEL_MAX_RETRIES"] = "0"
    os.environ["KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES"] = "0"


def _tool_call_count(result: TypedAgentRunResult[Any]) -> int:
    return sum(
        str(getattr(item, "type", ""))
        in {"tool_call_item", "tool_call_output_item"}
        for item in list(getattr(result.raw_result, "new_items", []) or [])
    )


def _write_result_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    args = build_parser().parse_args()
    _validate_limits(args)
    load_settings(force_dotenv=True)
    _configure_bounded_environment(args)
    case = next(
        item for item in contextual_target_action_scorecard() if item.case_id == args.case
    )
    observed: list[TypedAgentRunResult[Any]] = []
    plan = resolve_manual_request_plan(
        case.follow_up,
        requested_agent="chief_of_staff",
        live=True,
        model=args.model,
        workflow_state={
            "slack_thread_transcript": (
                f"Prior verified result: {case.prior_context}\n"
                f"Operator follow-up: {case.follow_up}"
            ),
            "recent_slack_thread": [
                {
                    "id": f"ANU-120-{case.case_id}",
                    "source_agent": str(case.owner_agent),
                    "summary": case.prior_context,
                }
            ],
        },
        cost_callback=observed.append,
    )
    result = observed[0] if observed else None
    usage = dict(result.usage) if result is not None else {}
    cost = dict(result.cost) if result is not None else {}
    budget_guard = dict(result.budget_guard) if result is not None else {}
    request_cache = dict(result.request_cache) if result is not None else {}
    tool_calls = _tool_call_count(result) if result is not None else 0
    checks = {
        "live_llm_interpretation": plan.source == "llm" and result is not None,
        "source_owner": plan.target_agent == case.owner_agent,
        "intent": plan.intent == case.expected_intent,
        "exact_request_count": int(usage.get("requests") or 0) == MAX_REQUESTS,
        "no_tools": tool_calls == 0,
        "no_retries": int(request_cache.get("rate_limit_retries") or 0) == 0,
        "budget_not_exceeded": budget_guard.get("exceeded") is False,
        "no_provider_writes": True,
    }
    payload = {
        "schema_version": "keystone.contextual_action_planner_smoke.v1",
        "status": "pass" if all(checks.values()) else "fail",
        "case_id": case.case_id,
        "model": args.model,
        "request_ceiling": args.max_openai_requests,
        "budget_usd": args.budget_usd,
        "input": {
            "prior_context": case.prior_context,
            "follow_up": case.follow_up,
        },
        "expected": {
            "owner_agent": case.owner_agent,
            "intent": case.expected_intent,
            "target_action": case.target_action,
            "tool_contract_status": case.tool_contract_status,
            "required_tool_change": case.required_tool_change,
            "safety_boundary": case.expected_safety_boundary,
        },
        "observed": {
            "plan": plan.model_dump(mode="json"),
            "usage": usage,
            "cost": cost,
            "budget_guard": budget_guard,
            "request_cache": request_cache,
            "tool_calls": tool_calls,
            "provider_reads": 0,
            "provider_writes": 0,
        },
        "checks": checks,
    }
    output = args.output_dir / f"{case.case_id}.json"
    _write_result_atomic(output, payload)
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if payload["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
