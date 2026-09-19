from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from keystone_agents.agents.preprints_context import build_preprints_context_agent
from keystone_agents.agents.rss_context import build_rss_context_agent
from keystone_agents.presentation.public_result import attach_execution_public_result
from keystone_agents.receipts.mutations import receipt_reports_possible_write
from keystone_agents.run import _tool_execution_summary_from_journals
from keystone_agents.runtime.signal_context import signal_decision_evidence
from keystone_agents.runtime.tool_execution import build_tool_execution_summary
from keystone_agents.schemas.announcement_feed import AnnouncementFeedItem
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools.announcement_context_tools import (
    retrieve_preprint_announcement_history,
    retrieve_preprint_announcement_history_impl,
)


def _history_tool(agent):
    return next(tool for tool in agent.tools if "announcement_history" in tool.name)


@pytest.mark.parametrize(
    "builder",
    [build_preprints_context_agent, build_rss_context_agent],
    ids=("preprints", "rss"),
)
def test_history_tool_schema_exposes_typed_discovery_vs_selected_scope(builder) -> None:
    tool = _history_tool(builder(model="gpt-test"))
    schema = tool.params_json_schema

    assert "selected_only" not in schema["properties"]
    assert schema["properties"]["history_scope"]["enum"] == ["discovery", "selected"]
    assert schema["properties"]["history_scope"]["default"] == "discovery"
    assert "previously selected" in tool.description.lower()


def test_history_tool_maps_typed_scope_without_mutating_source_selection(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'history-scope.db'}"
    store = SQLiteStore(database_url)
    store.save_announcement_feed_item(
        AnnouncementFeedItem(
            title="Synthetic neuromodulation candidate",
            url="https://example.test/preprints/candidate",
            source="Synthetic Preprints",
            feed="preprints",
            tags=["preprint", "neuromodulation"],
            selected=False,
            summary="Synthetic discovery candidate for portable scope testing.",
        )
    )
    store.save_announcement_feed_item(
        AnnouncementFeedItem(
            title="Synthetic neuromodulation selected item",
            url="https://example.test/preprints/selected",
            source="Synthetic Preprints",
            feed="preprints",
            tags=["preprint", "neuromodulation"],
            selected=True,
            selection_reason="Explicitly selected synthetic fixture.",
            summary="Synthetic selected item for portable scope testing.",
        )
    )
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.delenv("DISCOVERY_STORE_PATH", raising=False)
    monkeypatch.delenv("KEYSTONE_CANARY_ACCEPTANCE_PROFILE", raising=False)

    def invoke(scope: str) -> dict[str, object]:
        return json.loads(
            retrieve_preprint_announcement_history(
                query="synthetic neuromodulation",
                history_scope=scope,
                limit=8,
            )
        )

    discovery = invoke("discovery")
    selected = invoke("selected")

    assert discovery["selected_only"] is None
    assert discovery["item_count"] == 2
    assert selected["selected_only"] is True
    assert selected["item_count"] == 1
    assert {item["selected"] for item in discovery["items"]} == {False, True}
    assert selected["items"][0]["selected"] is True
    unselected = retrieve_preprint_announcement_history_impl(
        query="synthetic neuromodulation",
        selected_only=False,
        database_url=database_url,
    )
    assert unselected["item_count"] == 1
    assert unselected["items"][0]["selected"] is False


def test_signal_evidence_retains_each_call_scope_and_call_output_count() -> None:
    tool_name = "retrieve_preprint_announcement_history"
    raw = SimpleNamespace(
        new_items=[
            SimpleNamespace(
                type="tool_call_item",
                call_id="call-1",
                tool_name=tool_name,
                arguments=json.dumps(
                    {"query": "first query", "history_scope": "discovery", "limit": 8}
                ),
            ),
            SimpleNamespace(
                type="tool_call_output_item",
                call_id="call-1",
                output=json.dumps({"status": "success", "items": []}),
            ),
            SimpleNamespace(
                type="tool_call_item",
                call_id="call-2",
                tool_name=tool_name,
                arguments=json.dumps(
                    {"query": "second query", "history_scope": "discovery", "limit": 8}
                ),
            ),
            SimpleNamespace(
                type="tool_call_output_item",
                call_id="call-2",
                output=json.dumps(
                    {
                        "status": "success",
                        "items": [
                            {
                                "feed_item_id": "synthetic:preprint:one",
                                "title": "Synthetic preprint",
                                "url": "https://example.test/preprint/one",
                            }
                        ],
                    }
                ),
            ),
        ]
    )

    evidence = signal_decision_evidence(raw, tool_name=tool_name)

    assert evidence["model_tool_call_count"] == 2
    assert evidence["tool_output_count"] == 2
    assert evidence["tool_result_item_counts"] == [0, 1]
    assert evidence["history_scopes"] == ["discovery", "discovery"]


def test_receipt_summary_separates_invocations_observations_and_distinct_receipts() -> None:
    receipt = {
        "tool_name": "retrieve_preprint_announcement_history",
        "status": "success",
        "item_count": 0,
    }
    summary = _tool_execution_summary_from_journals(
        selected_tool_names=[receipt["tool_name"]],
        invocations=[
            {"status": "started", "tool_name": receipt["tool_name"]},
            {"status": "completed", "tool_name": receipt["tool_name"]},
            {"status": "started", "tool_name": receipt["tool_name"]},
            {"status": "completed", "tool_name": receipt["tool_name"]},
        ],
        tool_receipts=[receipt, dict(receipt)],
        preacquired_tool_receipts=[],
        postcondition=None,
        failed=False,
    )

    assert summary["model_tool_call_count"] == 2
    assert summary["tool_output_count"] == 2
    assert summary["distinct_persisted_receipt_count"] == 1
    assert summary["provider_receipt_count"] == 1
    assert summary["receipt_observation_count"] == 2
    assert summary["provider_request_attempt_count_available"] is False


def test_build_summary_keeps_legacy_unknown_provider_counters_unknown() -> None:
    summary = build_tool_execution_summary(
        mode="llm_selected_function_tools",
        model_called_tool_names=["read_history"],
        model_tool_call_count=2,
        tool_output_count=2,
        distinct_persisted_receipt_count=1,
        receipt_observation_count=3,
    )

    assert summary["provider_request_attempt_count_available"] is False
    assert summary["provider_receipt_count_available"] is True
    assert summary["distinct_persisted_receipt_count"] == 1
    assert summary["receipt_observation_count"] == 3


def test_read_receipt_with_approval_reference_is_not_a_write() -> None:
    receipt = {
        "tool_name": "retrieve_preprint_announcement_history",
        "operation": "read",
        "status": "success",
        "approval_reference": "reviewed-read-scope",
        "provider_write": False,
    }

    assert receipt_reports_possible_write(receipt) is False

    payload = {
        "status": "blocked",
        "human_summary": "No matching read-only history was found.",
        "tool_receipts": [receipt],
        "side_effects": {"external_write_performed": False},
    }
    result = attach_execution_public_result(payload)
    assert result.provider_write_attempted is False
    assert result.provider_receipt_verified is None


@pytest.mark.parametrize(
    ("receipt", "expected"),
    [
        (
            {
                "tool_name": "create_google_calendar_event",
                "operation": "create",
                "status": "success",
                "verification": {"passed": True},
            },
            True,
        ),
        ({"approval_reference": "write-approved", "status": "unknown"}, True),
        (
            {
                "tool_name": "create_google_calendar_event",
                "operation": "create",
                "status": "blocked",
                "dry_run": True,
            },
            False,
        ),
    ],
)
def test_write_classifier_remains_fail_closed_for_mutations(receipt, expected) -> None:
    assert receipt_reports_possible_write(receipt) is expected
