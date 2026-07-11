from __future__ import annotations

import pytest
from pydantic import ValidationError

from keystone_agents.schemas.weekly_ops import WeeklyOpsAssemblyInput
from keystone_agents.weekly_ops_packet import (
    build_weekly_ops_external_synthesis_bundle,
    build_weekly_ops_source_bundle,
)


def _payload() -> WeeklyOpsAssemblyInput:
    return WeeklyOpsAssemblyInput.model_validate(
        {
            "window": {
                "time_min": "2026-07-04T00:00:00-04:00",
                "time_max": "2026-07-11T00:00:00-04:00",
                "timezone": "America/New_York",
                "packet_date": "2026-07-10",
            },
            "slack": [
                {
                    "message_ts": "100.1",
                    "summary": "Calendar packet contract was approved for offline work.",
                    "action_owner": "Operator",
                }
            ],
            "gmail": [
                {
                    "thread_id": "thread-1",
                    "subject": "Review request",
                    "sender_label": "Partner team",
                    "summary": "A response is needed after internal review.",
                    "follow_up": "Confirm the accountable owner.",
                }
            ],
            "completed_runs": [
                {
                    "run_id": "run-1",
                    "agent_name": "chief_of_staff",
                    "completed_at": "2026-07-10T21:58:00-04:00",
                    "outcome_summary": "Calendar update completed and was read back.",
                    "work_item_id": "work-1",
                    "usage_summary": "0 OpenAI requests",
                    "receipt_reference": "calendar-receipt-1",
                    "packet_role": "primary",
                    "relevance_reason": "Completed a requested operator workflow.",
                },
                {
                    "run_id": "run-2",
                    "agent_name": "gmail_triage",
                    "completed_at": "2026-07-09T18:00:00-04:00",
                    "outcome_summary": "Selected thread was summarized without a draft.",
                    "carry_forward": "Retain the no-send boundary.",
                    "packet_role": "supporting",
                    "relevance_reason": "Established a reusable safety boundary.",
                },
                {
                    "run_id": "run-3",
                    "agent_name": "google_workspace_context_agent",
                    "completed_at": "2026-07-08T12:00:00-04:00",
                    "outcome_summary": "Marked test artifact lifecycle passed.",
                    "packet_role": "operational_health_only",
                    "relevance_reason": "Validation evidence, not a human workstream.",
                },
            ],
            "calendar": [
                {
                    "event_id": "event-focus",
                    "title": "Client review",
                    "start": "2026-07-06T10:00:00-04:00",
                },
                {
                    "event_id": "event-recurring-1",
                    "title": "Weekly operations sync",
                    "start": "2026-07-08T09:00:00-04:00",
                    "is_recurring": True,
                    "recurring_event_id": "series-1",
                },
                {
                    "event_id": "event-recurring-2",
                    "title": "Weekly operations sync",
                    "start": "2026-07-10T09:00:00-04:00",
                    "is_recurring": True,
                    "recurring_event_id": "series-1",
                },
            ],
        }
    )


def test_weekly_ops_bundle_prioritizes_focus_and_preserves_write_gates() -> None:
    bundle = build_weekly_ops_source_bundle(_payload())

    assert bundle["schema"] == "keystone.work_item.source_bundle.v1"
    assert bundle["target"]["name"] == "KNI Weekly Operations Packet — 2026-07-10"
    assert bundle["delivery_plan"]["folder_name"] == "KNIOps"
    assert bundle["delivery_plan"]["slack_channel_name"] == "ops-finance"
    assert bundle["delivery_plan"]["workspace_write_approval_required"] is True
    assert bundle["delivery_plan"]["slack_post_approval_required"] is True
    assert bundle["sources"][2]["key_facts"][0]["identity"] == "run-1"
    assert bundle["sources"][2]["key_facts"][0]["work_item_id"] == "work-1"
    assert bundle["sources"][2]["key_facts"][0]["usage"] == "0 OpenAI requests"
    assert all(
        fact["identity"] != "run-3" for fact in bundle["sources"][2]["key_facts"]
    )
    assert bundle["operational_health_signals"][0]["summary"] == (
        "google_workspace_context_agent: Marked test artifact lifecycle passed."
    )
    assert bundle["sources"][3]["key_facts"][0]["summary"] == "Client review"
    assert bundle["sources"][4]["key_facts"][0]["summary"] == (
        "Weekly operations sync: 2 instance(s)"
    )
    assert bundle["synthesis_ready"] is True
    assert bundle["section_order"][-2:] == ["operational_health", "packet_metadata"]
    metadata_contract = bundle["terminal_section_contract"]["packet_metadata"]
    assert "one human-readable footer line" in metadata_contract
    assert "Exclude schemas, routes, node paths" in metadata_contract
    assert bundle["safety"] == {
        "raw_slack_bodies_included": False,
        "raw_gmail_bodies_included": False,
        "calendar_descriptions_included": False,
        "calendar_attendees_included": False,
        "external_writes_enabled": False,
        "send_enabled": False,
    }


def test_weekly_ops_schema_rejects_raw_provider_fields() -> None:
    payload = _payload().model_dump(mode="json")
    payload["gmail"][0]["body"] = "raw private body"
    payload["calendar"][0]["attendees"] = ["private@example.com"]

    with pytest.raises(ValidationError) as exc_info:
        WeeklyOpsAssemblyInput.model_validate(payload)

    message = str(exc_info.value)
    assert "body" in message
    assert "attendees" in message


def test_external_synthesis_bundle_removes_identity_and_personal_fields() -> None:
    payload = _payload()
    payload.calendar[0].title = "Meeting with Example Person"

    bundle = build_weekly_ops_external_synthesis_bundle(
        payload,
        personal_redaction_terms=("Example Person",),
    )
    rendered = str(bundle)

    assert bundle["data_classification"] == (
        "operator-approved non-personal business operations"
    )
    assert "Example Person" not in rendered
    assert "[redacted person]" in rendered
    assert "thread-1" not in rendered
    assert "run-1" not in rendered
    assert "event-focus" not in rendered
    assert "work-1" not in rendered
    assert "calendar-receipt-1" not in rendered


def test_weekly_ops_schema_requires_recurring_series_identity() -> None:
    payload = _payload().model_dump(mode="json")
    payload["calendar"][1]["recurring_event_id"] = ""

    with pytest.raises(ValidationError, match="recurring_event_id"):
        WeeklyOpsAssemblyInput.model_validate(payload)


def test_weekly_ops_schema_rejects_non_run_fields() -> None:
    payload = _payload().model_dump(mode="json")
    payload["completed_runs"][0]["status"] = "needs_approval"

    with pytest.raises(ValidationError, match="status"):
        WeeklyOpsAssemblyInput.model_validate(payload)
