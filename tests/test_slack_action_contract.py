from __future__ import annotations

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
    KBA_ACTION_IDS,
    KBA_COS_AUDIT_AUTOMATIONS,
    KBA_INTENT_MORE_RESEARCH,
    KBA_MORE_RESEARCH,
    OPERATOR_FAILURE_SCHEMA,
    RUN_AGENT_MESSAGE_CALLBACK_ID,
    SLACK_AGENT_FEEDBACK_EVENT_SCHEMA,
    SLACK_SELECTED_CONTEXT_SCHEMA,
    BusinessAgentWriteGatePayload,
    OperatorFailurePayload,
    SlackAgentFeedbackEvent,
    business_agent_action_value,
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
    assert contract["schemas"]["business_agent_action"] == BUSINESS_AGENT_ACTION_SCHEMA
    assert contract["schemas"]["agent_feedback_event"] == SLACK_AGENT_FEEDBACK_EVENT_SCHEMA
    assert contract["schemas"]["operator_failure"] == OPERATOR_FAILURE_SCHEMA
    assert contract["schemas"]["selected_message_context"] == SLACK_SELECTED_CONTEXT_SCHEMA
    assert contract["schemas"]["write_gate"] == BUSINESS_AGENT_WRITE_GATE_SCHEMA
    assert KBA_MORE_RESEARCH in contract["action_ids"]
    assert KBA_COS_AUDIT_AUTOMATIONS in contract["action_ids"]
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


def test_action_and_selected_context_models_share_contract_schema_names() -> None:
    value = business_agent_action_value(
        intent=KBA_INTENT_MORE_RESEARCH,
        work_item_id="wi_123",
        artifact_id="company_profile:42",
    )
    parsed = parse_business_agent_action_value(value)
    context = SlackSelectedMessageContext(
        channel_id="C123", selected_message_ts="1715366400.000100"
    )

    assert parsed.schema_name == BUSINESS_AGENT_ACTION_SCHEMA
    assert parsed.intent == KBA_INTENT_MORE_RESEARCH
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
