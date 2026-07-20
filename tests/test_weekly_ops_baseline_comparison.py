from __future__ import annotations

from keystone_agents.schemas.chief_of_staff import ChiefOfStaffResult
from keystone_agents.schemas.weekly_ops import WeeklyOpsAssemblyInput
from keystone_agents.weekly_ops_runner import weekly_ops_operator_request
from scripts.run_weekly_ops_baseline_comparison import (
    REQUIRED_ASSERTION_MARKERS,
    REQUIRED_HEADINGS,
    build_observation,
    validate_kba_evidence,
    validate_weekly_packet,
)


def _packet() -> ChiefOfStaffResult:
    synthesis = "\n".join(
        [
            *REQUIRED_HEADINGS,
            *REQUIRED_ASSERTION_MARKERS,
            " ".join(["bounded operational evidence"] * 130),
        ]
    )
    return ChiefOfStaffResult(
        intent="portfolio_review",
        summary="A bounded weekly operations packet.",
        synthesis=synthesis,
        recommended_actions=["Review follow-up items.", "Carry forward pending review."],
        sources=[
            {
                "title": "Supplied privacy-minimized weekly context",
                "source_type": "user_provided_context",
                "note": "No raw identifiers.",
            }
        ],
    )


def test_weekly_request_helper_is_stable_and_preserves_no_side_effect_boundary() -> None:
    payload = WeeklyOpsAssemblyInput.model_validate(
        {
            "window": {
                "time_min": "2026-07-04T00:00:00Z",
                "time_max": "2026-07-11T00:00:00Z",
                "packet_date": "2026-07-10",
            }
        }
    )

    request = weekly_ops_operator_request(payload)

    assert "2026-07-04 through 2026-07-11" in request
    assert "privacy-minimized assertions" in request
    assert "Do not search, call tools, create a Google Doc, post to Slack" in request


def test_weekly_packet_quality_checks_require_sections_counts_actions_and_safety() -> None:
    checks = validate_weekly_packet(_packet())

    assert all(checks.values())


def test_kba_preflight_requires_successful_private_safe_one_request_receipt() -> None:
    payload = {
        "status": "success",
        "packet": _packet().model_dump(mode="json"),
        "request_count_bound": 1,
        "context_mode": "privacy_minimized_assertions",
        "raw_private_context_transmitted": False,
        "provider_writes": False,
        "send_enabled": False,
    }

    assert all(validate_kba_evidence(payload).values())
    payload["provider_writes"] = True
    assert validate_kba_evidence(payload)["receipt_no_provider_write"] is False


def test_matched_observations_bind_the_same_request_hash_without_invented_advantage() -> None:
    checks = validate_weekly_packet(_packet())
    request = "Prepare the exact weekly packet."
    kba = build_observation(
        system="kba",
        natural_request=request,
        checks=checks,
        usage={"requests": 1},
        cost={"estimated_usd": 0.04},
        evidence_ref="artifact:kba.json",
        latency_ms=None,
    )
    baseline = build_observation(
        system="codex_chatgpt_baseline",
        natural_request=request,
        checks=checks,
        usage={"requests": 1},
        cost={"estimated_usd": 0.01},
        evidence_ref="artifact:baseline.json",
        latency_ms=100,
    )

    assert kba.natural_request_sha256 == baseline.natural_request_sha256
    assert kba.useful_result is True
    assert baseline.useful_result is True
    assert kba.estimated_cost_usd > baseline.estimated_cost_usd
