from keystone_agents.agent_mentions import parse_agent_mention
from keystone_agents.provider_slack_acceptance import (
    PROVIDER_SLACK_ACCEPTANCE_CASES,
    build_provider_slack_acceptance_report,
)


def test_provider_slack_acceptance_manifest_completes_ten_ask_cross_provider_set() -> None:
    assert {case.provider for case in PROVIDER_SLACK_ACCEPTANCE_CASES} == {
        "airtable",
        "gmail",
        "zotero",
        "google_workspace",
    }
    assert len(PROVIDER_SLACK_ACCEPTANCE_CASES) == 5
    for case in PROVIDER_SLACK_ACCEPTANCE_CASES:
        mention = parse_agent_mention(case.prompt)
        assert mention.route == case.expected_route
        assert mention.input_text
        assert "--" not in case.prompt
        assert case.latency_limit_seconds <= 60
        assert case.expected_openai_requests <= case.max_openai_requests <= 2


def test_write_lifecycles_are_self_cleaning_exact_object_tests() -> None:
    writes = [
        case
        for case in PROVIDER_SLACK_ACCEPTANCE_CASES
        if case.provider in {"google_workspace", "gmail", "airtable"}
    ]
    assert len(writes) == 3
    for case in writes:
        assert case.cleanup_mode == "self_contained"
        assert case.cleanup_prompt == ""
        assert case.cleanup_verification
        assert case.must_pass_for_merge is True
        assert "same" in case.prompt.lower()
        assert "residue" in case.prompt.lower()
        assert case.provider_link_required is False


def test_zotero_followup_changes_agent_and_reuses_same_thread() -> None:
    ordered_read, followup = [
        case
        for case in PROVIDER_SLACK_ACCEPTANCE_CASES
        if case.provider == "zotero"
    ]

    assert ordered_read.action_id == "zotero_ordered_read"
    assert ordered_read.expected_route == "zotero_context_agent"
    assert ordered_read.cleanup_mode == "read_only"
    assert followup.reply_to_action_id == ordered_read.action_id
    assert followup.thread_group == ordered_read.thread_group
    assert followup.expected_route == "business_research_analyst"
    assert followup.must_pass_for_merge is True
    assert "same_thread_exact_item_rehydration" in followup.expected_operations


def test_report_preserves_unproven_live_boundary_and_full_request_ceiling() -> None:
    report = build_provider_slack_acceptance_report()

    assert report["schema"] == "keystone.provider_slack_acceptance.v2"
    assert report["status"] == "pending_live_approval"
    assert report["case_count"] == 5
    assert report["preceding_calendar_case_count"] == 5
    assert report["combined_case_count"] == 10
    assert report["provider_count"] == 4
    assert report["expected_openai_requests"] == 5
    assert report["max_openai_requests"] == 10
    assert report["max_total_cost_usd"] == 0.15
    assert report["serial_execution_required"] is True
    assert report["automatic_retries_allowed"] is False
    assert report["minimum_pass_count"] == 4
    assert report["must_pass_action_ids"] == [
        "workspace_lifecycle",
        "gmail_lifecycle",
        "airtable_lifecycle",
        "zotero_agent_switch_followup",
    ]
    assert report["live_pass_claimed"] is False
    assert report["visible_app_pass_claimed"] is False
