from keystone_agents.agent_mentions import parse_agent_mention
from keystone_agents.provider_slack_acceptance import (
    PROVIDER_SLACK_ACCEPTANCE_CASES,
    build_provider_slack_acceptance_report,
)


def test_provider_slack_acceptance_manifest_covers_four_sources_with_natural_prompts() -> None:
    assert {case.provider for case in PROVIDER_SLACK_ACCEPTANCE_CASES} == {
        "google_calendar",
        "airtable",
        "zotero",
        "google_workspace",
    }
    for case in PROVIDER_SLACK_ACCEPTANCE_CASES:
        mention = parse_agent_mention(case.prompt)
        assert mention.route == case.expected_route
        assert mention.input_text
        assert "--" not in case.prompt
        assert case.latency_limit_seconds <= 60


def test_success_cases_require_link_visual_readback_and_cleanup() -> None:
    successes = [
        case
        for case in PROVIDER_SLACK_ACCEPTANCE_CASES
        if case.expected_outcome == "verified_success"
    ]
    assert len(successes) == 4
    for case in successes:
        assert case.expected_link_host
        assert case.visual_surface
        assert case.cleanup_prompt.startswith("@KNI ")
        assert "provider_read_back" in case.expected_operations or (
            "absence_read_back" in case.expected_operations
        )


def test_blocker_case_forbids_link_and_mutation() -> None:
    blockers = [
        case
        for case in PROVIDER_SLACK_ACCEPTANCE_CASES
        if case.expected_outcome == "accurate_blocker"
    ]
    assert len(blockers) == 1
    blocker = blockers[0]
    assert blocker.expected_link_host == ""
    assert blocker.expected_operations == ["block_before_provider_mutation"]
    assert "no provider link or side effect" in blocker.stop_rule


def test_report_preserves_unproven_live_boundary_and_nine_request_ceiling() -> None:
    report = build_provider_slack_acceptance_report()

    assert report["status"] == "pending_live_approval"
    assert report["case_count"] == 5
    assert report["provider_count"] == 4
    assert report["max_openai_requests"] == 9
    assert report["max_total_cost_usd"] == 0.15
    assert report["serial_execution_required"] is True
    assert report["automatic_retries_allowed"] is False
    assert report["live_pass_claimed"] is False
    assert report["visible_app_pass_claimed"] is False
