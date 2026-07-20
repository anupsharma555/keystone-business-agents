"""Deterministic quality evaluation for Opportunity Scout result payloads."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

DETAIL_SENSITIVE_KINDS = {
    "conference",
    "workshop_or_training",
    "certification_or_professional_development",
    "grant_or_fellowship",
    "contract_or_rfp",
    "networking_or_professional_community",
    "publication_call",
    "accelerator_or_challenge",
}


def evaluate_opportunity_scout_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Score one saved Scout result without provider or model calls."""

    records = [item for item in payload.get("records", []) if isinstance(item, dict)]
    review = [item for item in payload.get("review_candidates", []) if isinstance(item, dict)]
    expired = [
        _record_name(item)
        for item in records
        if str(item.get("opportunity_status") or "") == "closed_or_expired"
    ]
    source_missing = [
        _record_name(item)
        for item in records
        if not any(
            str(source.get("url") or "").startswith(("http://", "https://", "fixture://"))
            for source in item.get("sources", [])
            if isinstance(source, dict)
        )
    ]
    detail_unverified = [
        _record_name(item)
        for item in records
        if str(item.get("opportunity_kind") or "") in DETAIL_SENSITIVE_KINDS
        and str(item.get("detail_verification_status") or "") != "page_verified"
    ]
    kinds = sorted(
        {
            str(item.get("opportunity_kind") or "other")
            for item in records
            if str(item.get("opportunity_kind") or "").strip()
        }
    )
    issues: list[str] = []
    if expired:
        issues.append("expired opportunities reached final ranking")
    if source_missing:
        issues.append("ranked opportunities lack source URLs")
    if detail_unverified:
        issues.append("detail-sensitive opportunities lack page verification")
    if bool(payload.get("outreach_generated")):
        issues.append("Opportunity Scout generated outreach")
    status = "pass"
    if issues:
        status = "fail"
    elif not records and review:
        status = "review_required"
    elif not records:
        status = "no_verified_results"
    return {
        "schema": "keystone.opportunity_scout_result_evaluation.v1",
        "status": status,
        "record_count": len(records),
        "review_candidate_count": len(review),
        "filtered_candidate_count": len(payload.get("filtered_candidates", [])),
        "opportunity_kinds": kinds,
        "search_lanes": list(dict.fromkeys(payload.get("search_lanes", []))),
        "expired_ranked": expired,
        "source_missing_ranked": source_missing,
        "detail_unverified_ranked": detail_unverified,
        "issues": issues,
        "no_outreach": not bool(payload.get("outreach_generated")),
    }


def evaluate_opportunity_scout_portfolio(
    payloads: list[tuple[str, dict[str, Any]]],
    *,
    expected_cases: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Aggregate scenarios and distinguish safe emptiness from useful coverage."""

    scenarios: list[dict[str, Any]] = []
    for name, payload in payloads:
        row = {"scenario": name, **evaluate_opportunity_scout_result(payload)}
        expected = (expected_cases or {}).get(name, {})
        required_lanes = [str(item) for item in expected.get("required_lanes", [])]
        covered_lanes = {_normalized_lane(item) for item in row["search_lanes"]}
        missing_lanes = [
            lane for lane in required_lanes if _normalized_lane(lane) not in covered_lanes
        ]
        minimum_verified_results = int(expected.get("minimum_verified_results", 0))
        row.update(
            {
                "minimum_verified_results": minimum_verified_results,
                "verified_yield_met": row["record_count"] >= minimum_verified_results,
                "required_lanes": required_lanes,
                "missing_required_lanes": missing_lanes,
                "requested_lane_coverage_met": not missing_lanes,
            }
        )
        scenarios.append(row)
    lanes = sorted({lane for row in scenarios for lane in row["search_lanes"]})
    kinds = sorted({kind for row in scenarios for kind in row["opportunity_kinds"]})
    safety_passed = bool(scenarios) and all(row["status"] != "fail" for row in scenarios)
    useful_scenario_count = sum(bool(row["record_count"]) for row in scenarios)
    verified_yield_met = useful_scenario_count > 0 and all(
        row["verified_yield_met"] for row in scenarios
    )
    requested_lane_coverage_met = all(row["requested_lane_coverage_met"] for row in scenarios)
    status = "pass"
    if not safety_passed:
        status = "fail"
    elif not verified_yield_met or not requested_lane_coverage_met:
        status = "incomplete"
    return {
        "schema": "keystone.opportunity_scout_portfolio_evaluation.v1",
        "status": status,
        "safety_passed": safety_passed,
        "verified_yield_met": verified_yield_met,
        "requested_lane_coverage_met": requested_lane_coverage_met,
        "useful_scenario_count": useful_scenario_count,
        "scenario_count": len(scenarios),
        "search_lanes": lanes,
        "opportunity_kinds": kinds,
        "record_count": sum(int(row["record_count"]) for row in scenarios),
        "review_candidate_count": sum(int(row["review_candidate_count"]) for row in scenarios),
        "scenarios": scenarios,
    }


def _normalized_lane(lane: str) -> str:
    aliases = {
        "conference_speaking": "conference",
        "grant_fellowship": "grant",
        "grant_funding": "grant",
        "industry_collaboration": "collaboration",
        "contract_procurement": "contract_rfp",
        "role": "consulting_advisory",
    }
    return aliases.get(str(lane), str(lane))


def _record_name(record: dict[str, Any]) -> str:
    return str(record.get("entity_name") or record.get("company_name") or "unknown")
