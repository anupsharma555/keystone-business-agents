#!/usr/bin/env python3
"""Run one matched generic baseline for the saved KBA weekly operations packet."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from keystone_agents.config import load_settings
from keystone_agents.differentiation_matrix import (
    DifferentiationObservation,
    compare_differentiation_observations,
)
from keystone_agents.privacy_minimized_synthesis import packet_for_model
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.chief_of_staff import ChiefOfStaffResult
from keystone_agents.schemas.weekly_ops import WeeklyOpsAssemblyInput
from keystone_agents.sdk import build_model_settings, build_sdk_agent
from keystone_agents.weekly_ops_packet import build_weekly_ops_privacy_minimized_packet
from keystone_agents.weekly_ops_runner import weekly_ops_operator_request

MODEL = "gpt-5.4-mini"
MAX_COST_USD = 0.05
WORKFLOW_ID = "weekly_project_brief"
REQUIRED_HEADINGS: tuple[str, ...] = (
    "executive focus areas",
    "workstreams and decisions",
    "completed runs and outcomes",
    "carry forward",
    "one-time calendar focus",
    "recurring calendar cadence",
    "next actions",
    "source basis",
    "operational health",
    "packet metadata",
)
REQUIRED_ASSERTION_MARKERS: tuple[str, ...] = (
    "completed_run",
    "gmail_follow_up",
    "one_time_calendar",
    "recurring_calendar",
    "slack_workstream",
    "8",
    "7",
    "14",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("artifacts/test-pack/weekly-chief-packet-2026-07-10-assembly-input.json"),
    )
    parser.add_argument(
        "--kba-evidence",
        type=Path,
        default=Path("artifacts/test-pack/weekly-chief-packet-live.json"),
    )
    parser.add_argument("--live-sdk", action="store_true")
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Validate the matched comparison inputs without making an API request.",
    )
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--max-openai-requests", type=int, default=1)
    parser.add_argument("--max-cost-usd", type=float, default=MAX_COST_USD)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/weekly-ops-baseline-comparison.json"),
    )
    parser.add_argument(
        "--preflight-output",
        type=Path,
        default=Path("artifacts/test-pack/weekly-ops-baseline-preflight.json"),
    )
    return parser


def build_baseline_agent(*, model: str = MODEL):
    return build_sdk_agent(
        name="codex_chatgpt_baseline",
        instructions=(
            "Prepare the requested internal weekly operations packet using only the supplied "
            "privacy-minimized assertions. Preserve source-family, workstream, status, "
            "action-state, owner-role, and count labels. Follow the requested heading order. "
            "Do not infer private identities or outcomes. Return review-only structured output "
            "with no tools, search, send, post, schedule, publish, or provider write."
        ),
        output_type=ChiefOfStaffResult,
        tools=[],
        model=model,
        model_settings=build_model_settings(
            reasoning_effort="low",
            verbosity="low",
            max_tokens=2500,
        ),
        enforce_tool_policy=False,
        handoff_description="Generic matched baseline for one bounded weekly packet.",
    )


def validate_weekly_packet(value: ChiefOfStaffResult | dict[str, Any]) -> dict[str, bool]:
    packet = (
        value if isinstance(value, ChiefOfStaffResult) else ChiefOfStaffResult.model_validate(value)
    )
    synthesis = packet.synthesis.casefold()
    return {
        "required_sections": all(heading in synthesis for heading in REQUIRED_HEADINGS),
        "assertion_markers_preserved": all(
            marker.casefold() in synthesis for marker in REQUIRED_ASSERTION_MARKERS
        ),
        "useful_summary": bool(packet.summary.strip() and len(packet.synthesis.split()) >= 120),
        "actionable_next_steps": len(packet.recommended_actions) >= 2,
        "source_basis_visible": bool(packet.sources and "source basis" in synthesis),
        "review_only": packet.approval_required and packet.human_review_required,
        "no_send": not packet.send_enabled,
        "no_slack_post": not packet.slack_post_allowed,
        "no_provider_write": not packet.write_requests,
    }


def validate_kba_evidence(payload: dict[str, Any]) -> dict[str, bool]:
    packet_checks = validate_weekly_packet(dict(payload.get("packet") or {}))
    return {
        **packet_checks,
        "successful_receipt": payload.get("status") == "success",
        "one_request_bound": int(payload.get("request_count_bound") or 0) == 1,
        "privacy_minimized_context": payload.get("context_mode")
        == "privacy_minimized_assertions",
        "no_raw_private_context": payload.get("raw_private_context_transmitted") is False,
        "receipt_no_provider_write": payload.get("provider_writes") is False,
        "receipt_no_send": payload.get("send_enabled") is False,
    }


def build_observation(
    *,
    system: Literal["kba", "codex_chatgpt_baseline"],
    natural_request: str,
    checks: dict[str, bool],
    usage: dict[str, Any],
    cost: dict[str, Any],
    evidence_ref: str,
    latency_ms: int | None,
) -> DifferentiationObservation:
    useful = all(checks.values())
    return DifferentiationObservation(
        system=system,
        workflow_id=WORKFLOW_ID,
        natural_request_sha256=hashlib.sha256(natural_request.encode()).hexdigest(),
        useful_result=useful,
        route_correct=useful,
        sources_visible=bool(checks.get("source_basis_visible")),
        followup_continuity=False,
        context_reentry_fields=0,
        manual_provider_ids=0,
        approval_round_trips=0,
        unintended_writes=0 if checks.get("no_provider_write") else 1,
        duplicate_artifacts=0,
        developer_intervention=False,
        latency_ms=latency_ms,
        estimated_cost_usd=_estimated_cost(cost),
        evidence_refs=(evidence_ref, f"usage:requests={int(usage.get('requests') or 0)}"),
    )


def _estimated_cost(cost: dict[str, Any]) -> float | None:
    value = cost.get("estimated_usd")
    if value is None:
        value = cost.get("amount_usd")
    return float(value) if value is not None else None


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object in {path}.")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def main() -> int:
    args = build_parser().parse_args()
    if not args.live_sdk and not args.preflight_only:
        raise SystemExit("Matched weekly baseline requires explicit --live-sdk.")
    if args.live_sdk and args.preflight_only:
        raise SystemExit("Choose either --live-sdk or --preflight-only, not both.")
    if args.model != MODEL or args.max_openai_requests != 1:
        raise SystemExit("Matched weekly baseline requires gpt-5.4-mini and one request.")
    if args.max_cost_usd <= 0 or args.max_cost_usd > MAX_COST_USD:
        raise SystemExit("Matched weekly baseline requires a cost ceiling at or below $0.05.")

    assembly = WeeklyOpsAssemblyInput.model_validate(_load_json(args.input))
    natural_request = weekly_ops_operator_request(assembly)
    kba_payload = _load_json(args.kba_evidence)
    kba_checks = validate_kba_evidence(kba_payload)
    if not all(kba_checks.values()):
        failed = [name for name, passed in kba_checks.items() if not passed]
        raise SystemExit("Saved KBA evidence failed preflight: " + ", ".join(failed))

    model_context = packet_for_model(build_weekly_ops_privacy_minimized_packet(assembly))
    if args.preflight_only:
        preflight = {
            "schema": "keystone.weekly_ops_baseline_preflight.v1",
            "status": "ready",
            "openai_api_requests": 0,
            "model": args.model,
            "max_openai_requests": args.max_openai_requests,
            "max_cost_usd": args.max_cost_usd,
            "natural_request_sha256": hashlib.sha256(
                natural_request.encode()
            ).hexdigest(),
            "kba_checks": kba_checks,
            "model_context_schema": model_context.get("schema"),
            "assertion_count": len(model_context.get("assertions") or []),
            "safety": {
                "tools_attached": False,
                "provider_reads": 0,
                "provider_writes": 0,
                "send_enabled": False,
                "post_enabled": False,
            },
        }
        _write_json(args.preflight_output, preflight)
        print(json.dumps(preflight, indent=2, sort_keys=True))
        return 0

    os.environ["KEYSTONE_AGENT_RUN_BUDGET_USD"] = str(args.max_cost_usd)
    os.environ["KEYSTONE_LIVE_MODEL_MAX_RETRIES"] = "0"
    os.environ["KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES"] = "0"
    load_settings(force_dotenv=True)
    started = time.monotonic()
    result = run_typed_sdk_agent(
        agent=build_baseline_agent(model=args.model),
        typed_input={
            "request": natural_request,
            "privacy_minimized_context": model_context,
        },
        output_type=ChiefOfStaffResult,
        live=True,
        workflow_name="Matched generic weekly operations baseline",
        tracing_disabled=True,
        trace_include_sensitive_data=False,
        max_turns=1,
    )
    latency_ms = round((time.monotonic() - started) * 1000)
    baseline_checks = validate_weekly_packet(result.output)
    baseline_checks.update(
        {
            "exact_request_count": int(result.usage.get("requests") or 0) == 1,
            "within_budget": float(result.cost.get("estimated_usd") or 0)
            <= args.max_cost_usd,
            "no_tools_attached": not result.request_cache.get("tool_count"),
            "no_retry": int(result.request_cache.get("rate_limit_retries") or 0) == 0,
        }
    )

    kba_observation = build_observation(
        system="kba",
        natural_request=natural_request,
        checks=kba_checks,
        usage=dict(kba_payload.get("usage") or {}),
        cost=dict(kba_payload.get("cost") or {}),
        evidence_ref=f"artifact:{args.kba_evidence}",
        latency_ms=None,
    )
    baseline_observation = build_observation(
        system="codex_chatgpt_baseline",
        natural_request=natural_request,
        checks=baseline_checks,
        usage=dict(result.usage or {}),
        cost=dict(result.cost or {}),
        evidence_ref=f"artifact:{args.output}",
        latency_ms=latency_ms,
    )
    comparison = compare_differentiation_observations(
        kba_observation, baseline_observation
    )
    payload = {
        "schema": "keystone.weekly_ops_baseline_comparison.v1",
        "status": "pass" if all(baseline_checks.values()) else "partial",
        "workflow_id": WORKFLOW_ID,
        "model": args.model,
        "natural_request": natural_request,
        "natural_request_sha256": kba_observation.natural_request_sha256,
        "kba_checks": kba_checks,
        "baseline_checks": baseline_checks,
        "baseline_output": result.output.model_dump(mode="json"),
        "baseline_usage": result.usage,
        "baseline_cost": result.cost,
        "baseline_request_cache": result.request_cache,
        "baseline_latency_ms": latency_ms,
        "kba_observation": asdict(kba_observation),
        "baseline_observation": asdict(baseline_observation),
        "comparison": asdict(comparison),
        "safety": {
            "tools_attached": False,
            "live_search": False,
            "provider_reads": 0,
            "provider_writes": 0,
            "send_enabled": False,
            "post_enabled": False,
        },
    }
    _write_json(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
