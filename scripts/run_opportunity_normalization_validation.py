"""Run one bounded no-search Opportunity Scout normalization validation."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from keystone_agents.agents.opportunity_scout import build_opportunity_scout_agent
from keystone_agents.config import load_settings
from keystone_agents.execution_identity import (
    ValidationExecutionIdentity,
    create_validation_execution_identity,
)
from keystone_agents.models import OpportunityScoutSDKInput, TypedAgentRunResult
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.opportunity import OpportunityScoutResult

EXPECTED_MODEL = "gpt-5.4-mini"
EXPECTED_REQUESTS = 1
MAX_BUDGET_USD = 0.10
SOURCE_PACKET = Path(
    "tests/fixtures/opportunity-hack-for-humanity-source-packet.json"
)
EXPECTED_URLS = {
    "https://hack-for-humanity-summer-26.devpost.com/",
    "https://hack-for-humanity-summer-26.devpost.com/details/dates",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=EXPECTED_MODEL)
    parser.add_argument("--max-openai-requests", type=int, default=EXPECTED_REQUESTS)
    parser.add_argument("--budget-usd", type=float, default=MAX_BUDGET_USD)
    parser.add_argument("--source-packet", type=Path, default=SOURCE_PACKET)
    parser.add_argument(
        "--operator-request",
        default="",
        help=(
            "Optional exact natural ask to bind to the supplied packet. The packet's "
            "stored request remains the default."
        ),
    )
    parser.add_argument(
        "--revalidate-receipt",
        type=Path,
        help="Re-run deterministic checks over a saved receipt without a model call.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/opportunity-normalization-live.json"),
    )
    return parser


def _validate_run_limits(args: argparse.Namespace) -> None:
    if args.model != EXPECTED_MODEL:
        raise SystemExit(f"Opportunity validation requires model={EXPECTED_MODEL}.")
    if args.max_openai_requests != EXPECTED_REQUESTS:
        raise SystemExit("Opportunity validation requires max_openai_requests=1.")
    if args.budget_usd <= 0 or args.budget_usd > MAX_BUDGET_USD:
        raise SystemExit("Opportunity validation requires a budget at or below $0.10.")


def _configure_bounded_environment(args: argparse.Namespace) -> None:
    os.environ["KEYSTONE_AGENT_RUN_BUDGET_USD"] = str(args.budget_usd)
    os.environ["KEYSTONE_LIVE_MODEL_MAX_RETRIES"] = "0"
    os.environ["KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES"] = "0"


def _load_packet(path: Path) -> dict[str, Any]:
    packet = json.loads(path.read_text(encoding="utf-8"))
    urls = {str(source.get("url") or "") for source in packet.get("sources", [])}
    if urls != EXPECTED_URLS:
        raise SystemExit("Opportunity source packet does not contain the exact two approved URLs.")
    if packet.get("source_domain") != "hack-for-humanity-summer-26.devpost.com":
        raise SystemExit("Opportunity source packet domain is not the approved Devpost domain.")
    return packet


def _typed_input(
    packet: dict[str, Any], *, operator_request: str | None = None
) -> OpportunityScoutSDKInput:
    instructions = {
        "execution_contract": {
            "attach_tools": False,
            "live_search": False,
            "provider_operations": 0,
            "search_provider": "none",
            "search_queries": [],
        },
        "normalization_contract": {
            "one_source_bundle": True,
            "one_independent_source_domain": True,
            "opportunity_type": "hackathon or challenge opportunity",
            "outreach_generated": False,
        },
        "source_packet": packet,
    }
    return OpportunityScoutSDKInput(
        topic=str(operator_request or packet["operator_request"]),
        max_results=1,
        context=(
            "Use only this verified supplied-source packet. Treat the two URLs as two pages "
            "in one source bundle and one independent source domain. Return search_provider "
            "as none, search_queries empty, raw_search_result_count 0, exactly one record, "
            "and no outreach. Geography is unknown; the submission window is upcoming. "
            "Recommend only a conditional prototype/portfolio review and state missing "
            "eligibility evidence. Do not infer consulting work, revenue, or a cash prize.\n\n"
            + json.dumps(instructions, ensure_ascii=True, sort_keys=True)
        ),
    )


def _tool_call_count(result: TypedAgentRunResult[OpportunityScoutResult]) -> int:
    return sum(
        str(getattr(item, "type", "")) in {"tool_call_item", "tool_call_output_item"}
        for item in list(getattr(result.raw_result, "new_items", []) or [])
    )


def _source_urls(output: OpportunityScoutResult) -> set[str]:
    return {
        source.url
        for record in output.records
        for source in record.sources
    }


def _quality_counts(output: OpportunityScoutResult) -> tuple[int | None, int | None]:
    top = output.source_quality_summary
    bundle = output.source_bundles[0] if len(output.source_bundles) == 1 else None
    bundle_quality = bundle.source_quality_summary if bundle is not None else None
    return (
        top.independent_source_count if top is not None else None,
        bundle_quality.independent_source_count if bundle_quality is not None else None,
    )


def _states_unknown(value: str | None) -> bool:
    return not str(value or "").strip() or "unknown" in str(value).strip().lower()


def _explicit_non_inference_checks(record: Any) -> bool:
    if record is None or record.outside_consulting_likelihood > 10:
        return False
    boundary_text = " ".join(
        [
            *record.disqualification_reasons,
            *record.unsupported_claims_flagged,
            *record.weak_evidence_reasons,
        ]
    ).lower()
    consulting_or_revenue_blocked = any(
        phrase in boundary_text
        for phrase in (
            "not a consulting or revenue",
            "not a consulting",
            "no consulting",
            "do not infer consulting",
        )
    )
    cash_blocked = any(
        phrase in boundary_text
        for phrase in ("not a cash prize", "no established cash prize", "no cash prize")
    )
    return consulting_or_revenue_blocked and cash_blocked


def _build_payload(
    result: TypedAgentRunResult[OpportunityScoutResult],
    *,
    execution_identity: ValidationExecutionIdentity,
    model: str,
    request_ceiling: int,
    budget_usd: float,
) -> dict[str, Any]:
    output = result.final_output
    record = output.records[0] if len(output.records) == 1 else None
    combined_text = " ".join(
        [
            record.why_now_signal if record else "",
            (record.novelty or "") if record else "",
            record.recommended_next_step if record else "",
            record.keystone_fit_reason if record else "",
            *(record.missing_evidence if record else []),
            *(record.analyst_recommendation for _ in [0] if record),
        ]
    ).lower()
    top_independent, bundle_independent = _quality_counts(output)
    checks = {
        "exact_request_count": int(result.usage.get("requests") or 0) == request_ceiling,
        "no_tools": _tool_call_count(result) == 0,
        "one_record": record is not None,
        "correct_opportunity_type": bool(record)
        and record.opportunity_type == "hackathon or challenge opportunity",
        "exact_sources_retained": _source_urls(output) == EXPECTED_URLS,
        "one_record_bundle": bool(record) and len(record.source_bundles) == 1,
        "one_top_level_bundle": len(output.source_bundles) == 1,
        "record_independent_domain_count": bool(record)
        and record.source_quality_summary is not None
        and record.source_quality_summary.independent_source_count == 1,
        "bundle_independent_domain_count": bundle_independent == 1,
        "top_level_independent_domain_count": top_independent == 1,
        "no_search_metadata": output.search_provider.strip().lower() in {"", "none"}
        and output.search_queries == []
        and output.raw_search_result_count == 0,
        "geography_unknown": bool(record)
        and _states_unknown(record.usa_relevance)
        and _states_unknown(record.role_country),
        "upcoming_timing_retained": any(
            term in combined_text for term in ("upcoming", "not yet open", "august 7")
        ),
        "conditional_non_outreach_recommendation": bool(record)
        and any(term in combined_text for term in ("conditional", "prototype", "portfolio"))
        and output.outreach_generated is False
        and record.approved_for_outreach is False,
        "eligibility_gap_retained": "eligib" in combined_text,
        "no_consulting_revenue_or_cash_inference": _explicit_non_inference_checks(record),
        "budget_not_exceeded": result.budget_guard.get("exceeded") is False,
        "no_retry": int(result.request_cache.get("rate_limit_retries") or 0) == 0,
    }
    return {
        "schema_version": "keystone.opportunity.normalization_evidence.v1",
        "status": "pass" if all(checks.values()) else "partial",
        "scenario": "opportunity_same_packet_top_level_normalization",
        "model": model,
        "expected_requests": EXPECTED_REQUESTS,
        "requests": int(result.usage.get("requests") or 0),
        "request_ceiling": request_ceiling,
        "budget_usd": budget_usd,
        "retries_allowed": 0,
        "execution_identity": execution_identity.receipt(),
        "usage": result.usage,
        "cost": result.cost,
        "budget_guard": result.budget_guard,
        "request_cache": result.request_cache,
        "checks": checks,
        "output": output.model_dump(mode="json"),
        "safety": {
            "live_search": False,
            "tool_count": _tool_call_count(result),
            "provider_reads": 0,
            "provider_writes": 0,
            "outreach_generated": output.outreach_generated,
            "external_write_performed": False,
        },
    }


def _write_result_atomic(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _revalidate_saved_receipt(path: Path) -> tuple[
    TypedAgentRunResult[OpportunityScoutResult], ValidationExecutionIdentity, str
]:
    saved = json.loads(path.read_text(encoding="utf-8"))
    result = TypedAgentRunResult(
        agent_name="opportunity_scout",
        output=OpportunityScoutResult.model_validate(saved["output"]),
        raw_result=SimpleNamespace(new_items=[]),
        live=True,
        usage=saved["usage"],
        cost=saved["cost"],
        budget_guard=saved["budget_guard"],
        request_cache=saved["request_cache"],
    )
    identity = ValidationExecutionIdentity(**saved["execution_identity"])
    return result, identity, str(saved.get("status") or "unknown")


def main() -> int:
    args = build_parser().parse_args()
    _validate_run_limits(args)
    _configure_bounded_environment(args)
    if args.revalidate_receipt is not None:
        result, execution_identity, initial_status = _revalidate_saved_receipt(
            args.revalidate_receipt
        )
        payload = _build_payload(
            result,
            execution_identity=execution_identity,
            model=args.model,
            request_ceiling=args.max_openai_requests,
            budget_usd=args.budget_usd,
        )
        payload["revalidated_from"] = str(args.revalidate_receipt)
        payload["initial_status"] = initial_status
        payload["validation_revision"] = (
            "Accept bounded unknown-geography wording and explicit negated "
            "consulting/revenue/cash-prize boundaries. No model call was made."
        )
        _write_result_atomic(args.output, payload)
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        return 0 if payload["status"] == "pass" else 2
    load_settings(force_dotenv=True)
    packet = _load_packet(args.source_packet)
    operator_request = str(args.operator_request or packet["operator_request"]).strip()
    if not operator_request:
        raise SystemExit("Opportunity validation requires a non-empty operator request.")
    execution_identity = create_validation_execution_identity(
        scenario="opportunity_same_packet_top_level_normalization",
        route="opportunity_scout",
    )
    result = run_typed_sdk_agent(
        agent=build_opportunity_scout_agent(
            model=args.model,
            request_text=operator_request,
            attach_tools=False,
        ),
        typed_input=_typed_input(packet, operator_request=operator_request),
        output_type=OpportunityScoutResult,
        live=True,
        workflow_name="Keystone Opportunity supplied-packet normalization validation",
        trace_metadata=execution_identity.trace_metadata(),
        max_turns=args.max_openai_requests,
    )
    payload = _build_payload(
        result,
        execution_identity=execution_identity,
        model=args.model,
        request_ceiling=args.max_openai_requests,
        budget_usd=args.budget_usd,
    )
    _write_result_atomic(args.output, payload)
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if payload["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
