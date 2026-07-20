from __future__ import annotations

from keystone_agents.opportunity_scout.evaluation import (
    evaluate_opportunity_scout_portfolio,
    evaluate_opportunity_scout_result,
)


def _record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "entity_name": "Clinical AI Certificate",
        "opportunity_kind": "certification_or_professional_development",
        "opportunity_status": "open",
        "detail_verification_status": "page_verified",
        "sources": [{"url": "https://example.test/certificate"}],
    }
    record.update(overrides)
    return record


def test_result_evaluation_passes_current_verified_non_company_opportunity() -> None:
    result = evaluate_opportunity_scout_result(
        {
            "records": [_record()],
            "review_candidates": [],
            "filtered_candidates": [],
            "search_lanes": ["certification_professional_development"],
            "outreach_generated": False,
        }
    )

    assert result["status"] == "pass"
    assert result["expired_ranked"] == []
    assert result["detail_unverified_ranked"] == []
    assert result["no_outreach"] is True


def test_result_evaluation_fails_expired_or_unverified_ranked_items() -> None:
    result = evaluate_opportunity_scout_result(
        {
            "records": [
                _record(
                    opportunity_status="closed_or_expired",
                    detail_verification_status="snippet_only",
                )
            ]
        }
    )

    assert result["status"] == "fail"
    assert result["expired_ranked"] == ["Clinical AI Certificate"]
    assert result["detail_unverified_ranked"] == ["Clinical AI Certificate"]


def test_portfolio_evaluation_accepts_honest_no_result_and_review_scenarios() -> None:
    result = evaluate_opportunity_scout_portfolio(
        [
            ("verified", {"records": [_record()], "search_lanes": ["workshop_training"]}),
            (
                "needs-review",
                {
                    "records": [],
                    "review_candidates": [{"company_name": "Possible Grant"}],
                    "search_lanes": ["grant"],
                },
            ),
            ("none", {"records": [], "review_candidates": [], "search_lanes": ["role"]}),
        ]
    )

    assert result["status"] == "pass"
    assert result["scenario_count"] == 3
    assert result["record_count"] == 1
    assert result["review_candidate_count"] == 1
    assert result["search_lanes"] == ["grant", "role", "workshop_training"]
    assert result["safety_passed"] is True
    assert result["verified_yield_met"] is True


def test_portfolio_evaluation_does_not_call_all_empty_scenarios_useful() -> None:
    result = evaluate_opportunity_scout_portfolio(
        [("empty-a", {"records": []}), ("empty-b", {"records": []})]
    )

    assert result["status"] == "incomplete"
    assert result["safety_passed"] is True
    assert result["verified_yield_met"] is False
    assert result["useful_scenario_count"] == 0


def test_portfolio_evaluation_checks_case_yield_and_lane_coverage() -> None:
    result = evaluate_opportunity_scout_portfolio(
        [
            (
                "broad",
                {
                    "records": [_record()],
                    "search_lanes": ["conference", "grant", "collaboration"],
                },
            )
        ],
        expected_cases={
            "broad": {
                "required_lanes": [
                    "conference_speaking",
                    "grant_fellowship",
                    "industry_collaboration",
                    "networking_community",
                ],
                "minimum_verified_results": 2,
            }
        },
    )

    assert result["status"] == "incomplete"
    assert result["verified_yield_met"] is False
    assert result["requested_lane_coverage_met"] is False
    assert result["scenarios"][0]["missing_required_lanes"] == ["networking_community"]
