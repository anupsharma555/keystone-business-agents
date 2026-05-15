from __future__ import annotations

import json
from pathlib import Path

import pytest

from keystone_agents.agent_registry import AGENT_REGISTRY
from keystone_agents.agent_tool_policy import disallowed_tool_names, tool_policy_for_agent
from keystone_agents.agents.chief_of_staff import (
    build_chief_of_staff_agent,
    plan_chief_of_staff_request,
)
from keystone_agents.schemas.chief_of_staff import ChiefOfStaffResult
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools.chief_of_staff_tool import (
    list_chief_of_staff_context_sources,
    lookup_slack_workflow_capability,
    read_slack_repo_context_file,
    search_official_operations_docs,
    search_slack_repo_context,
    summarize_slack_runtime_config,
)


def _payload(raw: str) -> dict[str, object]:
    return json.loads(raw)


def test_chief_of_staff_builder_matches_schema_and_policy() -> None:
    agent = build_chief_of_staff_agent()
    tool_names = {getattr(tool, "name", "") for tool in agent.tools}

    assert agent.name == "chief_of_staff"
    assert agent.output_type is ChiefOfStaffResult
    assert "list_chief_of_staff_context_sources" in tool_names
    assert "summarize_slack_runtime_config" in tool_names
    assert "search_local_context" in tool_names
    assert disallowed_tool_names("chief_of_staff", sorted(tool_names)) == []

    policy = tool_policy_for_agent("chief_of_staff")
    assert policy is not None
    assert "search_official_operations_docs" in policy.allowed_tool_names


def test_chief_of_staff_registry_card_is_canonical() -> None:
    spec = AGENT_REGISTRY["chief_of_staff"]

    assert spec.builder_name == "build_chief_of_staff_agent"
    assert spec.resolve_output_schema() is ChiefOfStaffResult
    assert "chief_of_staff.md" in spec.prompt_files
    assert "tests/test_chief_of_staff.py" in spec.validation_paths


def test_calendar_meetings_request_routes_read_only_to_meetings_channel() -> None:
    result = plan_chief_of_staff_request(
        "take a look at my calendar and add an update to #meetings"
    )

    assert result.recommended_route.workflow_type == "calendar-read"
    assert result.recommended_route.command_text == "/kni calendar today"
    assert result.recommended_route.target_channel == "meetings"
    assert result.slack_post_allowed is False
    assert result.send_enabled is False
    assert "calendar_create_or_update" in result.blocked_side_effects
    assert result.recommended_route.requires_human_approval_before_post is True


def test_email_onboarding_request_routes_to_gmail_summary_without_send() -> None:
    result = plan_chief_of_staff_request(
        "see my email and send an update to the #onboarding channel"
    )

    assert result.recommended_route.workflow_type == "gmail-summary"
    assert result.recommended_route.command_text == "/kni gmail summarize onboarding"
    assert result.recommended_route.target_channel == "onboarding"
    assert result.send_enabled is False
    assert result.slack_post_allowed is False
    assert "gmail_send" in result.blocked_side_effects
    assert "selected_gmail_context" in result.context_sources_considered
    assert any("Gmail" in source.title or "Slack" in source.title for source in result.sources)


def test_scope_question_returns_scope_plan_not_clarification() -> None:
    result = plan_chief_of_staff_request("what is your scope for this slack?")

    assert result.recommended_route.workflow_type == "slack-runtime-review"
    assert result.recommended_route.command_text.startswith("@KNI chief of staff")
    assert result.recommended_route.target_channel == "current-thread"
    assert result.summary == "Explain Chief of Staff scope for KNI Slack operations."
    assert result.slack_post_allowed is False
    assert any("Plan KNI Slack routing" in action for action in result.recommended_actions)
    assert "keystone_slack_runtime_repo" in result.context_sources_considered


def test_supplied_slack_history_digest_renders_timestamped_answer() -> None:
    result = plan_chief_of_staff_request(
        "\n".join(
            [
                "chief of staff what were the last articles posted in "
                "#grants-and-funding regarding?",
                "",
                "Read-only Slack message-history context supplied by the KNI Slack runtime.",
                "Slack channel history digest:",
                "Channel: #grants-and-funding",
                "Channel id: C0ASKGN9946",
                "Messages reviewed: 3",
                "Recent candidate messages, newest first:",
                (
                    "- ts=1778779000.000100 author=UKNI title=Early psychosis prediction: "
                    "ClinicalTrials.gov watchlist generated for `early psychosis prediction`. "
                    "*Workflow:* `trials-watch` *Status:* `ok` *Run ID:* `run_123`"
                ),
                (
                    "- ts=1778778000.000100 author=UKNI title=Esketamine and bipolar: "
                    "ClinicalTrials.gov watchlist generated for `esketamine bipolar`. "
                    "*Workflow:* `trials-watch` *Status:* `ok`"
                ),
                (
                    "- ts=1778777000.000100 author=UKNI title=Topics:: "
                    "Topics: translational psychiatry | Score: 1 *Matched on:* topic focus "
                    "*Link:* *Summary:* Long trial summary"
                ),
            ]
        )
    )

    assert result.recommended_route.workflow_type == "slack-runtime-review"
    assert result.mode == "deterministic"
    assert "Recent posts:" in result.summary
    assert "Recent posts:\n\n1. early psychosis prediction" in result.summary
    assert "1. early psychosis prediction" in result.summary
    assert "   Posted: 2026-05-14 13:16 EDT" in result.summary
    assert "\n\n2. esketamine bipolar" in result.summary
    assert "   Posted: 2026-05-14 13:00 EDT" in result.summary
    assert "\n\n3. translational psychiatry | Score: 1" in result.summary
    assert "Metadata:" not in result.summary
    assert "run_123" not in result.summary
    assert "Matched on" not in result.summary
    assert "\n\nTheme:" in result.summary
    assert "Theme:" in result.summary
    assert "supplied_slack_message_history_digest" in result.context_sources_considered
    assert result.slack_post_allowed is False


def test_reference_capture_request_saves_operator_memory(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'keystone.db'}"
    url = (
        "https://braininitiative.nih.gov/news-events/blog/"
        "register-now-nih-brain-neuroai-workshop"
    )

    result = plan_chief_of_staff_request(
        (
            "chief of staff keep this for future reference: "
            f"NIH AI conference with virtual attendees: {url}"
        ),
        database_url=database_url,
    )

    assert result.recommended_route.workflow_type == "reference-capture"
    assert result.summary.startswith("Saved reference for future use:")
    assert result.slack_post_allowed is False
    assert result.artifact_refs
    assert result.artifact_refs[0].artifact_type == "operator_reference_memory"
    assert result.artifact_refs[0].url == url

    memories = SQLiteStore(database_url).retrieve_memory(
        "NIH AI conference",
        memory_types=["operator_reference"],
    )
    assert len(memories) == 1
    assert memories[0].title == "NIH AI conference with virtual attendees"
    assert memories[0].content["url"] == url


def test_slack_repo_context_tools_are_read_only_and_secret_filtered(tmp_path: Path) -> None:
    repo = tmp_path / "keystone-slack"
    package = repo / "kni_integrations"
    package.mkdir(parents=True)
    (repo / "AGENTS.md").write_text("Slack is the primary frontend.\n", encoding="utf-8")
    (package / "slack_socket_mode.py").write_text(
        "Socket Mode receives slash commands and sends acknowledgements.\n",
        encoding="utf-8",
    )
    (repo / ".env").write_text("SLACK_BOT_TOKEN=xoxb-secret\n", encoding="utf-8")

    search = _payload(search_slack_repo_context("Socket Mode", repo_path=str(repo)))
    assert search["send_enabled"] is False
    assert search["matches"]
    assert search["matches"][0]["relative_path"] == "kni_integrations/slack_socket_mode.py"

    read = _payload(read_slack_repo_context_file("AGENTS.md", repo_path=str(repo)))
    assert read["repo_write_enabled"] is False
    assert "Slack is the primary frontend" in read["content"]

    with pytest.raises(ValueError):
        read_slack_repo_context_file(".env", repo_path=str(repo))


def test_runtime_summary_and_docs_catalog_are_capped_and_official(tmp_path: Path) -> None:
    repo = tmp_path / "keystone-slack"
    package = repo / "kni_integrations"
    package.mkdir(parents=True)
    (package / "config.py").write_text(
        'gmail_channel=get("SLACK_GMAIL_CHANNEL", "gmail") or "gmail"\n',
        encoding="utf-8",
    )

    summary = _payload(summarize_slack_runtime_config(repo_path=str(repo)))
    assert summary["send_enabled"] is False
    assert summary["default_channels"]["gmail_channel"] == "gmail"

    docs = _payload(search_official_operations_docs("Slack Socket Mode OpenAI Agents SDK"))
    urls = [item["url"] for item in docs["results"]]
    assert any(url.startswith("https://docs.slack.dev/") for url in urls)
    assert any(url.startswith("https://developers.openai.com/") for url in urls)

    context_sources = _payload(list_chief_of_staff_context_sources())
    source_ids = [item["source_id"] for item in context_sources["sources"]]
    assert "selected_gmail_context" in source_ids
    assert "selected_calendar_context" in source_ids
    assert "github_and_local_repos" in source_ids
    assert context_sources["slack_post_allowed"] is False


def test_lookup_slack_workflow_capability_blocks_posts() -> None:
    payload = _payload(lookup_slack_workflow_capability("calendar next week to #meetings"))

    assert payload["send_enabled"] is False
    assert payload["slack_post_allowed"] is False
    assert payload["capability"]["workflow_type"] == "calendar-read"
    assert payload["capability"]["target_channel"] == "meetings"
