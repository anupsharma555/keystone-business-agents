# ruff: noqa: E501
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from keystone_agents.slack_action_contract import (
    BUSINESS_AGENT_ACTION_SCHEMA,
    BUSINESS_AGENT_SLACK_CONTRACT_CAPABILITIES,
    BUSINESS_AGENT_SLACK_CONTRACT_SCHEMA,
    BUSINESS_AGENT_WRITE_GATE_ACTION_ID,
    BUSINESS_AGENT_WRITE_GATE_INTENT,
    BUSINESS_AGENT_WRITE_GATE_SCHEMA,
    CONTEXT_AGENT_RESULT_RENDERERS,
    KBA_ACTION_IDS,
    KBA_COS_AUDIT_AUTOMATIONS,
    KBA_EVAL_ORCHESTRATOR_JUDGE,
    KBA_EVAL_REVIEW,
    KBA_INTENT_EVAL_ORCHESTRATOR_JUDGE,
    KBA_INTENT_EVAL_REVIEW,
    KBA_INTENT_MORE_RESEARCH,
    KBA_MORE_RESEARCH,
    NAMED_AGENT_RESULT_RENDERERS,
    OPERATOR_FAILURE_SCHEMA,
    RUN_AGENT_MESSAGE_CALLBACK_ID,
    SLACK_AGENT_FEEDBACK_EVENT_SCHEMA,
    SLACK_SELECTED_CONTEXT_SCHEMA,
    BusinessAgentWriteGatePayload,
    OperatorFailurePayload,
    SlackAgentFeedbackEvent,
    business_agent_action_value,
    business_agent_result_display_text,
    business_agent_slack_contract,
    business_agent_write_gate_value,
    parse_business_agent_action_value,
    parse_business_agent_write_gate_value,
    slack_agent_feedback_event,
)
from keystone_agents.slack_actions import SlackSelectedMessageContext


def test_business_agent_action_contract_rejects_unknown_schema_version() -> None:
    value = json.dumps({"schema": "keystone.business_agent_action.v2", "intent": "more_research"})

    with pytest.raises(ValidationError):
        parse_business_agent_action_value(value)


def test_business_agent_write_gate_contract_requires_current_schema_and_request_text() -> None:
    value = business_agent_write_gate_value(
        request_text="chief of staff draft a Slack update",
        source_channel_id="C123",
        source_thread_ts="1715366400.000100",
    )

    parsed = parse_business_agent_write_gate_value(value)

    assert parsed.schema_name == BUSINESS_AGENT_WRITE_GATE_SCHEMA
    assert parsed.intent == BUSINESS_AGENT_WRITE_GATE_INTENT
    assert parsed.request_text == "chief of staff draft a Slack update"
    assert parsed.source_channel_id == "C123"

    with pytest.raises(ValidationError):
        BusinessAgentWriteGatePayload.model_validate(
            {"schema": BUSINESS_AGENT_WRITE_GATE_SCHEMA, "intent": BUSINESS_AGENT_WRITE_GATE_INTENT}
        )

    with pytest.raises(ValidationError):
        parse_business_agent_write_gate_value(
            json.dumps(
                {
                    "schema": "keystone.business_agent_write_request.v2",
                    "intent": BUSINESS_AGENT_WRITE_GATE_INTENT,
                    "request_text": "draft a Slack update",
                }
            )
        )


def test_business_agent_slack_contract_exports_action_and_context_metadata() -> None:
    contract = business_agent_slack_contract()

    assert contract["schema"] == BUSINESS_AGENT_SLACK_CONTRACT_SCHEMA
    assert set(contract["capabilities"]) == set(BUSINESS_AGENT_SLACK_CONTRACT_CAPABILITIES)
    assert "context_agent_result_rendering" in contract["capabilities"]
    assert "named_agent_result_rendering" in contract["capabilities"]
    assert "source_channel_response_routing" in contract["capabilities"]
    assert contract["schemas"]["business_agent_action"] == BUSINESS_AGENT_ACTION_SCHEMA
    assert contract["schemas"]["agent_feedback_event"] == SLACK_AGENT_FEEDBACK_EVENT_SCHEMA
    assert contract["schemas"]["operator_failure"] == OPERATOR_FAILURE_SCHEMA
    assert contract["schemas"]["selected_message_context"] == SLACK_SELECTED_CONTEXT_SCHEMA
    assert contract["schemas"]["write_gate"] == BUSINESS_AGENT_WRITE_GATE_SCHEMA
    assert KBA_MORE_RESEARCH in contract["action_ids"]
    assert KBA_COS_AUDIT_AUTOMATIONS in contract["action_ids"]
    assert KBA_EVAL_REVIEW in contract["action_ids"]
    assert KBA_EVAL_ORCHESTRATOR_JUDGE in contract["action_ids"]
    assert KBA_INTENT_EVAL_REVIEW in contract["intents"]
    assert KBA_INTENT_EVAL_ORCHESTRATOR_JUDGE in contract["intents"]
    assert set(contract["action_ids"]) == KBA_ACTION_IDS
    assert contract["input_action_ids"]["write_gate"] == BUSINESS_AGENT_WRITE_GATE_ACTION_ID
    assert contract["callback_ids"]["run_agent_message"] == RUN_AGENT_MESSAGE_CALLBACK_ID

    action_schema = contract["payload_json_schemas"]["business_agent_action"]
    feedback_schema = contract["payload_json_schemas"]["agent_feedback_event"]
    selected_context_schema = contract["payload_json_schemas"]["selected_message_context"]
    write_gate_schema = contract["payload_json_schemas"]["write_gate"]
    failure_schema = contract["payload_json_schemas"]["operator_failure"]
    assert "intent" in action_schema["required"]
    assert "event_type" in feedback_schema["required"]
    assert "kind" in failure_schema["required"]
    assert failure_schema["properties"]["schema"]["default"] == OPERATOR_FAILURE_SCHEMA
    assert selected_context_schema["properties"]["schema"]["const"] == SLACK_SELECTED_CONTEXT_SCHEMA
    assert "prior_agent_runs" in selected_context_schema["properties"]
    assert "request_text" in write_gate_schema["required"]
    assert contract["result_rendering"]["context_agents"] == [
        dict(item) for item in CONTEXT_AGENT_RESULT_RENDERERS
    ]
    assert contract["result_rendering"]["named_agents"] == [
        dict(item) for item in NAMED_AGENT_RESULT_RENDERERS
    ]
    context_output_types = {
        item["output_type"] for item in contract["result_rendering"]["context_agents"]
    }
    assert {
        "AirtableContextResult",
        "GoogleWorkspaceContextResult",
        "ZoteroContextResult",
        "RssContextResult",
        "PreprintsContextResult",
    } <= context_output_types
    named_output_types = {
        item["output_type"] for item in contract["result_rendering"]["named_agents"]
    }
    assert {
        "CompanyResearchFocusedBrief",
        "ChiefOfStaffResult",
        "OpportunityScoutResult",
        "EmailTriageResult",
        "OutreachDraft",
    } <= named_output_types
    assert contract["result_rendering"]["fallback_text_fields"][0] == "human_summary"
    routing = contract["slack_response_routing"]
    assert routing["source_channel_id_field"] == "source_channel_id"
    assert routing["source_thread_ts_field"] == "source_thread_ts"
    assert "source Slack channel/thread" in routing["policy"]
    assert "do not funnel" in routing["policy"]


def test_generated_slack_contract_artifact_matches_canonical_contract_shape() -> None:
    artifact = Path("contracts/keystone_slack_business_agent_contract.v1.json")
    assert artifact.is_file()
    payload = json.loads(artifact.read_text(encoding="utf-8"))

    assert payload["schema"] == BUSINESS_AGENT_SLACK_CONTRACT_SCHEMA
    assert set(payload["capabilities"]) == set(BUSINESS_AGENT_SLACK_CONTRACT_CAPABILITIES)
    assert set(payload["payload_json_schemas"]) == set(
        business_agent_slack_contract()["payload_json_schemas"]
    )
    assert payload["action_ids"] == sorted(KBA_ACTION_IDS)
    assert payload["schemas"]["agent_feedback_event"] == SLACK_AGENT_FEEDBACK_EVENT_SCHEMA
    assert payload["schemas"]["operator_failure"] == OPERATOR_FAILURE_SCHEMA
    assert payload["schemas"]["selected_message_context"] == SLACK_SELECTED_CONTEXT_SCHEMA
    assert (
        payload["payload_json_schemas"]["agent_feedback_event"]["properties"]["schema"]["default"]
        == SLACK_AGENT_FEEDBACK_EVENT_SCHEMA
    )
    assert (
        payload["payload_json_schemas"]["selected_message_context"]["properties"]["schema"]["const"]
        == SLACK_SELECTED_CONTEXT_SCHEMA
    )
    assert payload["payload_json_schemas"]["write_gate"]["properties"]["schema"]["default"] == (
        BUSINESS_AGENT_WRITE_GATE_SCHEMA
    )
    assert (
        payload["payload_json_schemas"]["operator_failure"]["properties"]["schema"]["default"]
        == OPERATOR_FAILURE_SCHEMA
    )
    assert payload["result_rendering"] == business_agent_slack_contract()["result_rendering"]
    assert payload["slack_response_routing"] == business_agent_slack_contract()[
        "slack_response_routing"
    ]


def test_slack_result_rendering_examples_match_display_helper() -> None:
    artifact = Path("contracts/keystone_slack_result_rendering_examples.v1.json")
    assert artifact.is_file()
    payload = json.loads(artifact.read_text(encoding="utf-8"))

    assert payload["schema"] == "keystone.slack_result_rendering_examples.v1"
    examples = payload["examples"]
    assert {example["name"] for example in examples} == {
        "business_research_named_agent_human_summary",
        "business_research_legacy_display_text_fallback",
        "conversational_named_agent_selected_agent_only",
        "rss_context_output_type_only",
        "preprints_context_route_only",
    }
    for example in examples:
        assert (
            business_agent_result_display_text(example["payload"])
            == example["expected_display_text"]
        )


def test_validate_slack_result_rendering_examples_script(capsys: pytest.CaptureFixture[str]) -> None:
    script_path = Path("scripts/validate_slack_result_rendering_examples.py")
    spec = importlib.util.spec_from_file_location(
        "validate_slack_result_rendering_examples",
        script_path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.validate_examples() == []
    assert module.main([]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "Validated 5 Slack result rendering examples"
    assert captured.err == ""


def test_validate_slack_bridge_contract_script(capsys: pytest.CaptureFixture[str]) -> None:
    script_path = Path("scripts/validate_slack_bridge_contract.py")
    spec = importlib.util.spec_from_file_location(
        "validate_slack_bridge_contract",
        script_path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.validate_bridge_contract() == []
    assert module.main([]) == 0
    captured = capsys.readouterr()
    assert (
        captured.out.strip()
        == "Validated Slack bridge contract, rendering examples, and source-channel routing"
    )
    assert captured.err == ""


@pytest.mark.parametrize(
    ("route", "output_type"),
    [(item["route"], item["output_type"]) for item in CONTEXT_AGENT_RESULT_RENDERERS],
)
def test_context_agent_result_display_text_prefers_human_summary(
    route: str,
    output_type: str,
) -> None:
    summary = (
        "*Answer:*\n"
        "Context results are ready for the operator.\n\n"
        "*Detailed Summary:*\n"
        "The agent returned a sectioned human summary that should be rendered."
    )

    assert (
        business_agent_result_display_text(
            {
                "route": route,
                "output_type": output_type,
                "status": "done",
                "message": "Business Agents WorkItem completed.",
                "output": {"summary": "Generic nested summary."},
                "human_summary": summary,
            }
        )
        == summary
    )


@pytest.mark.parametrize(
    "renderer",
    list(CONTEXT_AGENT_RESULT_RENDERERS) + list(NAMED_AGENT_RESULT_RENDERERS),
)
@pytest.mark.parametrize("identifier_shape", ["route", "output_type", "selected_agent"])
def test_business_agent_result_display_text_accepts_partial_renderer_identity(
    renderer: dict[str, str],
    identifier_shape: str,
) -> None:
    summary = (
        "*Answer:*\n"
        "The bridge has enough identity to render the clean summary.\n\n"
        "*Detailed Summary:*\n"
        "Route-only, selected-agent-only, and output-type-only payloads should "
        "not fall back to metadata."
    )
    payload = {
        "human_summary": summary,
        "message": "Business Agents WorkItem completed.",
        "output": {"summary": "Nested metadata fallback."},
    }
    if identifier_shape == "route":
        payload["route"] = renderer["route"]
    elif identifier_shape == "selected_agent":
        payload["selected_agent"] = renderer["route"]
    else:
        payload["output_type"] = renderer["output_type"]

    assert business_agent_result_display_text(payload) == summary


def test_business_agent_result_display_text_uses_fallback_fields() -> None:
    assert (
        business_agent_result_display_text(
            {
                "slack_display_text": "Top-level display text.",
                "output": {"summary": "Nested summary."},
                "message": "Plain message.",
            }
        )
        == "Top-level display text."
    )
    assert (
        business_agent_result_display_text(
            {
                "display_text": "General display text.",
                "summary": "Top-level summary.",
                "output": {"summary": "Nested summary."},
                "message": "Plain message.",
            }
        )
        == "General display text."
    )
    assert (
        business_agent_result_display_text(
            {
                "summary": "Top-level summary.",
                "output": {"summary": "Nested summary."},
                "message": "Plain message.",
            }
        )
        == "Top-level summary."
    )
    assert (
        business_agent_result_display_text({"output": {"summary": "Nested summary."}})
        == "Nested summary."
    )
    assert business_agent_result_display_text({"message": "Plain message."}) == "Plain message."


@pytest.mark.parametrize(
    ("route", "output_type"),
    [(item["route"], item["output_type"]) for item in NAMED_AGENT_RESULT_RENDERERS],
)
def test_named_agent_result_display_text_prefers_human_summary(
    route: str,
    output_type: str,
) -> None:
    summary = (
        "*Answer:*\n"
        "The named agent returned a reader-facing summary.\n\n"
        "*Detailed Summary:*\n"
        "The Slack bridge should render this reader-facing summary before metadata."
    )

    assert (
        business_agent_result_display_text(
            {
                "selected_agent": route,
                "output_type": output_type,
                "human_summary": summary,
                "output": {
                    "title": "Metadata-shaped title field.",
                    "summary": "Nested fallback summary.",
                },
                "model": {"provider": "openai", "name": "gpt-5.4-mini"},
                "retrieval_diagnostics": {"provider_summary": "searxng+exa"},
            }
        )
        == summary
    )


def test_action_and_selected_context_models_share_contract_schema_names() -> None:
    value = business_agent_action_value(
        intent=KBA_INTENT_MORE_RESEARCH,
        work_item_id="wi_123",
        artifact_id="company_profile:42",
        source_channel_id="CDOCS123",
        source_message_ts="1782000000.000100",
        source_thread_ts="1782000000.000100",
    )
    parsed = parse_business_agent_action_value(value)
    context = SlackSelectedMessageContext(
        channel_id="C123", selected_message_ts="1715366400.000100"
    )

    assert parsed.schema_name == BUSINESS_AGENT_ACTION_SCHEMA
    assert parsed.intent == KBA_INTENT_MORE_RESEARCH
    assert parsed.source_channel_id == "CDOCS123"
    assert parsed.source_message_ts == "1782000000.000100"
    assert parsed.source_thread_ts == "1782000000.000100"
    assert context.model_dump(mode="json", by_alias=True)["schema"] == SLACK_SELECTED_CONTEXT_SCHEMA


def test_slack_agent_feedback_event_has_contract_schema() -> None:
    event = slack_agent_feedback_event(
        "orchestrator_preflight",
        {"selected_agent": "chief_of_staff", "blocked_by_orchestrator": False},
    )

    parsed = SlackAgentFeedbackEvent.model_validate(event)

    assert parsed.schema_name == SLACK_AGENT_FEEDBACK_EVENT_SCHEMA
    assert parsed.event_type == "orchestrator_preflight"
    assert parsed.payload["selected_agent"] == "chief_of_staff"


def test_operator_failure_payload_has_contract_schema() -> None:
    payload = OperatorFailurePayload(
        kind="schema_or_parse_error",
        summary="The Slack bridge run returned data that could not be validated.",
        reason="Invalid JSON",
        next_step="Keep the WorkItem blocked and rerun after fixing the malformed payload.",
        retryable=True,
        safe_to_continue=True,
    ).model_dump(mode="json", by_alias=True)

    assert payload["schema"] == OPERATOR_FAILURE_SCHEMA
    assert payload["kind"] == "schema_or_parse_error"
    assert payload["retryable"] is True
