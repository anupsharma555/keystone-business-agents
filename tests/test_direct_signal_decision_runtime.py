from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from keystone_agents.models import TypedAgentRunResult
from keystone_agents.run import sdk_run_failure_metadata
from keystone_agents.runtime.signal_context import (
    SignalAgentDecisionError,
    _combined_signal_request_cache,
    _sanitized_signal_decision_evidence,
    run_signal_context_sdk,
    signal_decision_evidence,
    validate_signal_agent_decision,
)
from keystone_agents.runtime.tool_execution import sdk_tool_execution_records
from keystone_agents.schemas.announcement_feed import (
    AnnouncementFeedEvidence,
    AnnouncementFeedItem,
)
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.schemas.operational_context import (
    PreprintsContextResult,
    RssContextResult,
)
from keystone_agents.sdk import build_local_run_config
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools import announcement_context_tools

try:
    from agents.models.interface import Model, ModelProvider, ModelResponse
    from agents.usage import Usage
    from openai.types.responses import (
        ResponseFunctionToolCall,
        ResponseOutputMessage,
        ResponseOutputText,
    )
except ImportError:
    pytestmark = pytest.mark.skip(reason="OpenAI Agents SDK fake-model hooks unavailable.")


def test_signal_evidence_omits_malformed_saved_content_scope() -> None:
    evidence = _sanitized_signal_decision_evidence(
        {
            "candidate_ids": ["candidate-1"],
            "candidates": [
                {
                    "candidate_id": "candidate-1",
                    "raw_item": {
                        "source_snapshot": {"sha256": "a" * 64},
                        "evidence_index": [{"evidence_id": "evidence:" + "b" * 24}],
                        "saved_content_scope": ["linked_discovery_summary"],
                        "article_full_text_verified": "true",
                    },
                }
            ],
        }
    )
    candidate = evidence["candidates"][0]
    assert candidate["source_snapshot_sha256"] == "a" * 64
    assert "saved_content_scope" not in candidate
    assert "article_full_text_verified" not in candidate


class FakeModel(Model):
    def __init__(self, outputs: list[list[Any]]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, Any]] = []

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[Any],
        model_settings: Any,
        tools: list[Any],
        output_schema: Any,
        handoffs: list[Any],
        tracing: Any,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: Any,
    ) -> ModelResponse:
        self.calls.append(
            {
                "input": input,
                "tool_names": [tool.name for tool in tools],
                "system_instructions": system_instructions,
            }
        )
        return ModelResponse(
            output=self.outputs.pop(0),
            usage=Usage(requests=1),
            response_id=f"direct-signal-fake-{len(self.calls)}",
        )

    def stream_response(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        raise NotImplementedError


class FakeProvider(ModelProvider):
    def __init__(self, model: FakeModel) -> None:
        self.model = model

    def get_model(self, _model_name: str | None) -> Model:
        return self.model


def _tool_call(name: str, arguments: dict[str, Any], *, call_id: str) -> Any:
    return ResponseFunctionToolCall(
        type="function_call",
        name=name,
        call_id=call_id,
        arguments=json.dumps(arguments),
        status="completed",
    )


def _structured_message(payload: dict[str, Any]) -> Any:
    return ResponseOutputMessage(
        id="direct-signal-output",
        type="message",
        role="assistant",
        status="completed",
        content=[
            ResponseOutputText(
                type="output_text",
                text=json.dumps(payload),
                annotations=[],
            )
        ],
    )


def _result_payload(
    candidates: list[dict[str, Any]],
    *,
    selected_id: str,
    decision_stage: str = "signal_relevance_selection",
    reasoning: str = "The selected item best matches the requested monitoring objective.",
    assessed_ids: list[str] | None = None,
) -> dict[str, Any]:
    selected = next(item for item in candidates if item["feed_item_id"] == selected_id)
    assessment_order = assessed_ids if assessed_ids is not None else [
        item["feed_item_id"] for item in candidates
    ]
    return {
        "mode": "llm",
        "summary": f"Selected {selected['title']}.",
        "query": "behavioral health implementation",
        "retrieved_item_ids": [selected_id],
        "articles": [
            {
                **selected,
                "relevance_status": "selected",
                "selection_reason": "Directly matches the monitoring objective.",
                "relevance_to_keystone": "Useful for KNI evidence monitoring.",
            }
        ],
        "decision": {
            "decision_owner": "specialist_agent",
            "decision_stage": decision_stage,
            "selected_candidate_ids": [selected_id],
            "candidate_assessments": [
                {
                    "candidate_id": candidate_id,
                    "disposition": "selected" if candidate_id == selected_id else "excluded",
                    "rationale": (
                        "Best match for the request."
                        if candidate_id == selected_id
                        else "Less relevant to the requested implementation focus."
                    ),
                }
                for candidate_id in assessment_order
            ],
            "reasoning": reasoning,
            "limitations": ["The decision used bounded stored signal history."],
        },
    }


def _saved_preprint_candidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    database_url = f"sqlite:///{tmp_path / 'signal-evidence.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    store = SQLiteStore(database_url)
    for index in range(8):
        store.save_announcement_feed_item(
            AnnouncementFeedItem(
                canonical_key=f"synthetic-preprint-{index}",
                title=f"Psychiatric validation candidate {index}",
                url=f"https://example.test/preprints/{index}",
                source="preprints",
                feed="preprints",
                published_at=f"2026-09-{index + 1:02d}",
                summary=f"Saved candidate {index} summary.",
                evidence=[
                    AnnouncementFeedEvidence(
                        kind="search",
                        title=f"Source {index}",
                        url=f"https://example.test/preprints/{index}",
                        snippet=f"Exact saved psychiatric validation evidence {index}.",
                    )
                ],
            )
        )
    history = announcement_context_tools.retrieve_preprint_announcement_history_impl(
        query="psychiatric validation", limit=8, database_url=database_url,
    )
    assert len(history["items"]) == 8
    candidates = [
        {
            key: item[key]
            for key in ("feed_item_id", "title", "url", "source", "published_at", "summary")
        }
        for item in history["items"]
    ]
    return history["items"], candidates


@pytest.mark.parametrize(
    ("decision", "reason_code"),
    [
        (None, "missing_signal_decision_reasoning"),
        (
            {
                "decision_owner": "orchestrator",
                "decision_stage": "signal_relevance_selection",
                "reasoning": "The candidate appears relevant.",
            },
            "signal_decision_owner_mismatch",
        ),
    ],
)
def test_signal_validator_requires_explicit_owned_decision(
    decision: dict[str, Any] | None,
    reason_code: str,
) -> None:
    result = RssContextResult() if decision is None else RssContextResult(decision=decision)
    evidence = {
        "candidate_ids": ["rss-a"],
        "tool_call_count": 1,
        "tool_output_count": 1,
    }

    outcome = validate_signal_agent_decision(result, evidence, max_selected=8)

    assert outcome.status == "rejected"
    assert outcome.reason_code == reason_code


@pytest.mark.parametrize("result_type", [RssContextResult, PreprintsContextResult])
def test_signal_output_schema_fixes_decision_stage_before_model_validation(
    result_type: type[RssContextResult] | type[PreprintsContextResult],
) -> None:
    decision_schema = result_type.model_json_schema()["$defs"][
        "SignalRelevanceDecisionRecord"
    ]

    assert decision_schema["properties"]["decision_stage"]["const"] == (
        "signal_relevance_selection"
    )
    with pytest.raises(ValidationError, match="signal_relevance_selection"):
        result_type(
            decision={
                "decision_owner": "specialist_agent",
                "decision_stage": "generic_selection",
                "reasoning": "The candidate appears relevant.",
            }
        )


def test_signal_validator_requires_complete_bounded_candidate_assessments() -> None:
    result = RssContextResult(
        retrieved_item_ids=["rss-a"],
        articles=[{"feed_item_id": "rss-a", "title": "Selected signal"}],
        decision={
            "decision_owner": "specialist_agent",
            "decision_stage": "signal_relevance_selection",
            "selected_candidate_ids": ["rss-a"],
            "candidate_assessments": [
                {
                    "candidate_id": "rss-a",
                    "disposition": "selected",
                    "rationale": "Best match.",
                }
            ],
            "reasoning": "RSS A best matches the request.",
        },
    )
    evidence = {
        "candidate_ids": ["rss-a", "rss-b"],
        "tool_call_count": 1,
        "tool_output_count": 1,
    }

    outcome = validate_signal_agent_decision(result, evidence, max_selected=8)

    assert outcome.status == "rejected"
    assert outcome.reason_code == "incomplete_signal_candidate_assessments"


def test_signal_runtime_repairs_once_with_read_only_tool_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = [
        {
            "feed_item_id": "rss-general",
            "title": "General operations update",
            "url": "https://example.com/general",
            "source": "announcements",
            "summary": "A general company operations update.",
            "published_at": "2026-08-01",
        },
        {
            "feed_item_id": "rss-implementation",
            "title": "Behavioral health implementation partnership",
            "url": "https://example.com/implementation",
            "source": "announcements",
            "summary": "A clinic partnership focused on implementation evidence.",
            "published_at": "2026-08-03",
        },
    ]
    monkeypatch.setattr(
        announcement_context_tools,
        "retrieve_rss_announcement_history_impl",
        lambda **_kwargs: {
            "status": "success",
            "kind": "rss",
            "query": "behavioral health implementation",
            "item_count": 2,
            "items": candidates,
            "send_enabled": False,
        },
    )
    invalid = _result_payload(
        candidates,
        selected_id="rss-implementation",
        assessed_ids=["rss-implementation"],
    )
    repaired = _result_payload(
        candidates,
        selected_id="rss-implementation",
    )
    model = FakeModel(
        [
            [
                _tool_call(
                    "retrieve_rss_announcement_history",
                    {
                        "query": "behavioral health implementation",
                        "selected_only": None,
                        "limit": 8,
                        "live": False,
                    },
                    call_id="rss-history-1",
                )
            ],
            [_structured_message(invalid)],
            [_structured_message(repaired)],
        ]
    )

    result = run_signal_context_sdk(
        "rss",
        "Which recent signal is most relevant to behavioral-health implementation?",
        run_config=build_local_run_config(FakeProvider(model)),
    )

    decision = result.request_cache["decision_ownership"]
    assert decision["attempt_count"] == 2
    assert decision["repair_attempted"] is True
    assert decision["attempts"][0]["validator_outcome"]["reason_code"] == (
        "incomplete_signal_candidate_assessments"
    )
    assert decision["validator_outcome"]["status"] == "accepted"
    assert result.request_cache["decision_repairs"] == 1
    assert "Decision validator repair" in str(model.calls[2]["input"])
    assert "Which recent signal is most relevant" in str(model.calls[2]["input"])
    assert "General operations update" in str(model.calls[2]["input"])
    assert "Behavioral health implementation partnership" in str(
        model.calls[2]["input"]
    )
    assert model.calls[2]["tool_names"] == []
    assert result.usage["requests"] == 3
    assert result.usage["attempt_count"] == 2
    assert result.request_cache["signal_decision_evidence"][
        "raw_provider_payload_retained"
    ] is False
    assert result.request_cache["decision_repair_evidence"][
        "provider_calls_during_repair"
    ] == 0
    assert result.request_cache["decision_ownership"]["repair_evidence"] == (
        result.request_cache["decision_repair_evidence"]
    )
    request_scope = result.request_cache["request_tool_scope"]
    assert request_scope["selected_tool_names"] == [
        "retrieve_rss_announcement_history",
        "read_rss_announcement_evidence",
        "inspect_signal_lifecycle",
    ]
    schema_fingerprints = request_scope["tool_schema_fingerprints"]
    assert set(schema_fingerprints) == set(request_scope["selected_tool_names"])
    assert all(len(value) == 64 for value in schema_fingerprints.values())
    assert [item["name"] for item in request_scope["attached_tools"]] == (
        request_scope["selected_tool_names"]
    )
    assert all(
        item["schema_source"] == "params_json_schema_fingerprint"
        for item in request_scope["attached_tools"]
    )
    assert all(
        "raw_item" not in candidate
        for candidate in result.request_cache["signal_decision_evidence"]["candidates"]
    )
    tool_attempts = result.request_cache["tool_execution_attempts"]
    assert tool_attempts[0]["postcondition"]["mode"] == "required"
    assert tool_attempts[0]["postcondition"]["satisfied"] is True
    assert tool_attempts[1]["postcondition"]["mode"] == "forbidden"
    assert tool_attempts[1]["postcondition"]["satisfied"] is True
    combined_calls = [
        item
        for item in result.raw_result.new_items
        if getattr(item, "type", "") == "tool_call_item"
    ]
    assert len(combined_calls) == 1
    assert all(
        "prepare_signal_lifecycle_checkpoint" not in call["tool_names"]
        and "advance_signal_lifecycle_checkpoint" not in call["tool_names"]
        for call in model.calls
    )


def test_signal_single_history_stage_retains_observed_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        announcement_context_tools,
        "retrieve_rss_announcement_history_impl",
        lambda **_kwargs: {
            "status": "success", "kind": "rss", "item_count": 0,
            "items": [], "send_enabled": False,
        },
    )
    output = RssContextResult(
        mode="llm",
        summary="No matching saved announcement items were found.",
        decision={
            "decision_owner": "specialist_agent",
            "decision_stage": "signal_relevance_selection",
            "reasoning": "The bounded history read returned no matching candidates.",
            "needs_more_context": True,
            "limitations": ["No saved announcement matched the request."],
        },
    )
    model = FakeModel(
        [
            [
                _tool_call(
                    "retrieve_rss_announcement_history",
                    {"query": "behavioral health", "limit": 8},
                    call_id="single-history-1",
                )
            ],
            [_structured_message(output.model_dump(mode="json"))],
        ]
    )

    result = run_signal_context_sdk(
        "rss", "Check saved behavioral health announcements.",
        run_config=build_local_run_config(FakeProvider(model)),
    )

    assert len(model.calls) == 2
    assert len(result.request_cache["signal_stage_request_caches"]) == 1
    assert result.request_cache["tool_execution"]["model_tool_call_count"] == 1
    assert result.request_cache["tool_execution"]["tool_output_count"] == 1
    assert result.request_cache["tool_execution"]["provider_receipt_count"] == 1
    assert result.request_cache["tool_execution"]["receipt_observation_count"] == 1


def test_signal_combined_receipts_deduplicate_identical_stage_payloads() -> None:
    receipt = {"tool_name": "retrieve_rss_announcement_history", "status": "success"}
    stage_cache = {
        "tool_execution": {
            "selected_tool_names": ["retrieve_rss_announcement_history"],
            "model_called_tool_names": ["retrieve_rss_announcement_history"],
            "model_tool_call_count": 1,
            "tool_output_count": 1,
            "distinct_persisted_receipt_count": 1,
            "receipt_observation_count": 1,
        }
    }
    stages = [
        TypedAgentRunResult(
            agent_name="rss_context_agent",
            output=RssContextResult(),
            raw_result=SimpleNamespace(new_items=[]),
            live=False,
            request_cache=stage_cache,
            tool_receipts=[receipt],
        )
        for _ in range(2)
    ]

    combined = _combined_signal_request_cache(stages)

    assert combined["tool_execution"]["model_tool_call_count"] == 2
    assert combined["tool_execution"]["tool_output_count"] == 2
    assert combined["tool_execution"]["distinct_persisted_receipt_count"] == 1
    assert combined["tool_execution"]["receipt_observation_count"] == 2


def test_preprints_signal_repair_reuses_first_candidate_universe_without_second_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = [
        {
            "feed_item_id": "preprint-general",
            "title": "General language-model benchmark",
            "url": "https://example.com/preprint-general",
            "source": "medRxiv",
            "summary": "A general-purpose language-model evaluation.",
            "published_at": "2026-08-01",
        },
        {
            "feed_item_id": "preprint-clinical",
            "title": "Prospective psychiatric validation study",
            "url": "https://example.com/preprint-clinical",
            "source": "medRxiv",
            "summary": "A prospective validation study for psychiatric relapse signals.",
            "published_at": "2026-08-03",
        },
    ]
    provider_calls = 0

    def fake_history(**_kwargs: Any) -> dict[str, Any]:
        nonlocal provider_calls
        provider_calls += 1
        return {
            "status": "success",
            "kind": "preprints",
            "query": "psychiatric validation",
            "item_count": 2,
            "items": candidates,
            "send_enabled": False,
        }

    monkeypatch.setattr(
        announcement_context_tools,
        "retrieve_preprint_announcement_history_impl",
        fake_history,
    )
    invalid = _result_payload(
        candidates,
        selected_id="preprint-clinical",
        assessed_ids=["preprint-clinical"],
    )
    repaired = _result_payload(candidates, selected_id="preprint-clinical")
    model = FakeModel(
        [
            [
                _tool_call(
                    "retrieve_preprint_announcement_history",
                    {
                        "query": "psychiatric validation",
                        "selected_only": None,
                        "limit": 8,
                    },
                    call_id="preprints-history-1",
                )
            ],
            [_structured_message(invalid)],
            [_structured_message(repaired)],
        ]
    )

    result = run_signal_context_sdk(
        "preprints",
        "Which recent paper is strongest for psychiatric validation work?",
        run_config=build_local_run_config(FakeProvider(model)),
    )

    assert provider_calls == 1
    assert len(model.calls) == 3
    assert model.calls[2]["tool_names"] == []
    assert "Prospective psychiatric validation study" in str(model.calls[2]["input"])
    assert result.final_output.retrieved_item_ids == ["preprint-clinical"]
    assert result.request_cache["decision_ownership"]["attempts"][1]["events"][0][
        "tool_mode"
    ] == "verified_context_tool_free"


def test_signal_missing_saved_evidence_uses_candidate_bound_read_correction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history, candidates = _saved_preprint_candidates(tmp_path, monkeypatch)
    selected = history[0]
    selected_id = selected["feed_item_id"]
    initial = _result_payload(candidates, selected_id=selected_id)
    corrected = _result_payload(candidates, selected_id=selected_id)
    model = FakeModel(
        [
            [
                _tool_call(
                    "retrieve_preprint_announcement_history",
                    {"query": "psychiatric validation", "history_scope": "discovery", "limit": 8},
                    call_id="history-1",
                )
            ],
            [_structured_message(initial)],
            [
                _tool_call(
                    "read_preprint_announcement_evidence",
                    {
                        "feed_item_id": selected_id,
                        "evidence_id": selected["evidence_index"][0]["evidence_id"],
                        "expected_snapshot_sha256": selected["source_snapshot"]["sha256"],
                    },
                    call_id="exact-read-1",
                )
            ],
            [_structured_message(corrected)],
        ]
    )

    result = run_signal_context_sdk(
        "preprints",
        "Which saved psychiatric validation paper merits review?",
        run_config=build_local_run_config(FakeProvider(model)),
    )

    assert len(model.calls) == 4
    assert model.calls[2]["tool_names"] == ["read_preprint_announcement_evidence"]
    assert "evidence_ids" in str(model.calls[2]["input"])
    assert "exact_read_handles" not in str(model.calls[2]["input"])
    assert selected["source_snapshot"]["sha256"] in str(model.calls[2]["input"])
    assert result.final_output.decision.selected_candidate_ids == [selected_id]
    assert len(result.final_output.decision.candidate_assessments) == 8
    assert result.request_cache["decision_ownership"]["validator_outcome"]["status"] == (
        "accepted"
    )
    assert result.request_cache["signal_decision_evidence"]["candidate_count"] == 8
    assert result.request_cache["signal_decision_evidence"]["history_tool_call_count"] == 1
    assert result.request_cache["signal_decision_evidence"]["evidence_read_call_count"] == 1
    assert result.request_cache["decision_repair_evidence"]["enabled_read_tool_names"] == [
        "read_preprint_announcement_evidence"
    ]
    assert result.request_cache["signal_stage_request_caches"][1]["role"] == (
        "candidate_bound_exact_read_correction"
    )
    assert result.request_cache["signal_stage_request_caches"][1]["tool_execution"][
        "model_called_tool_names"
    ] == ["read_preprint_announcement_evidence"]
    stages = result.request_cache["signal_stage_request_caches"]
    assert result.request_cache["tool_execution"]["model_tool_call_count"] == sum(
        stage["tool_execution"]["model_tool_call_count"] for stage in stages
    ) == 2
    assert result.request_cache["tool_execution"]["tool_output_count"] == sum(
        stage["tool_execution"]["tool_output_count"] for stage in stages
    ) == 2
    assert result.request_cache["tool_execution"]["provider_receipt_count"] == 2
    assert result.request_cache["tool_execution"]["receipt_observation_count"] >= 2
    assert result.request_cache["tool_call_budget"]["total_consumed"] == sum(
        stage["tool_call_budget"]["total_consumed"] for stage in stages
    ) == 2
    assert len(result.request_cache["tool_invocations"]) == sum(
        len(stage["tool_invocations"]) for stage in stages
    )
    assert result.request_cache["decision_repair_evidence"]["exact_evidence_read_count"] == 1
    assert result.request_cache["decision_repair_evidence"]["provider_calls_during_repair"] == 0
    assert [record.tool_name for record in sdk_tool_execution_records(result.raw_result)] == [
        "retrieve_preprint_announcement_history",
        "read_preprint_announcement_evidence",
    ]
    assert all(record.succeeded for record in sdk_tool_execution_records(result.raw_result))
    assert result.usage["requests"] == 4


def test_signal_exact_read_correction_allows_three_serial_reads_then_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history, candidates = _saved_preprint_candidates(tmp_path, monkeypatch)
    selected_items = history[:3]
    selected_ids = [item["feed_item_id"] for item in selected_items]
    decision = _result_payload(candidates, selected_id=selected_ids[0])
    decision["retrieved_item_ids"] = selected_ids
    decision["articles"] = [
        {
            **candidate,
            "relevance_status": "selected",
            "selection_reason": "Directly matches the monitoring objective.",
            "relevance_to_keystone": "Useful for KNI evidence monitoring.",
        }
        for candidate in candidates[:3]
    ]
    decision["decision"]["selected_candidate_ids"] = selected_ids
    for assessment in decision["decision"]["candidate_assessments"]:
        if assessment["candidate_id"] in selected_ids:
            assessment["disposition"] = "selected"
    model = FakeModel(
        [
            [
                _tool_call(
                    "retrieve_preprint_announcement_history",
                    {"query": "psychiatric validation", "history_scope": "discovery", "limit": 8},
                    call_id="history-three-1",
                )
            ],
            [_structured_message(decision)],
            *[
                [
                    _tool_call(
                        "read_preprint_announcement_evidence",
                        {
                            "feed_item_id": item["feed_item_id"],
                            "evidence_id": item["evidence_index"][0]["evidence_id"],
                            "expected_snapshot_sha256": item["source_snapshot"]["sha256"],
                        },
                        call_id=f"exact-three-{index}",
                    )
                ]
                for index, item in enumerate(selected_items, start=1)
            ],
            [_structured_message(decision)],
        ]
    )

    result = run_signal_context_sdk(
        "preprints",
        "Compare the strongest saved psychiatric validation papers.",
        run_config=build_local_run_config(FakeProvider(model)),
    )

    assert len(model.calls) == 6
    assert all(
        call["tool_names"] == ["read_preprint_announcement_evidence"]
        for call in model.calls[2:]
    )
    assert result.final_output.decision.selected_candidate_ids == selected_ids
    assert result.request_cache["decision_ownership"]["validator_outcome"]["status"] == "accepted"
    assert result.request_cache["signal_decision_evidence"]["evidence_read_call_count"] == 3
    assert result.request_cache["decision_repair_evidence"]["exact_evidence_read_count"] == 3
    stages = result.request_cache["signal_stage_request_caches"]
    assert [stage["tool_execution"]["model_tool_call_count"] for stage in stages] == [1, 3]
    assert result.request_cache["tool_execution"]["model_tool_call_count"] == 4
    assert result.request_cache["tool_execution"]["tool_output_count"] == 4
    assert result.request_cache["tool_execution"]["provider_receipt_count"] == sum(
        stage["tool_execution"]["distinct_persisted_receipt_count"]
        for stage in stages
    ) == 2
    assert result.request_cache["tool_execution"]["receipt_observation_count"] >= 4
    assert result.request_cache["tool_call_budget"]["total_consumed"] == 4
    budget = result.request_cache["tool_call_budget"]
    assert budget["consumed"]["read_preprint_announcement_evidence"] == 3
    assert budget["limits"]["read_preprint_announcement_evidence"] == 3
    assert len(result.request_cache["tool_invocations"]) == sum(
        len(stage["tool_invocations"]) for stage in stages
    )
    assert result.usage["requests"] == 6


def _linked_preprint_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    count: int,
) -> tuple[Path, list[dict[str, Any]], list[dict[str, Any]]]:
    discovery_path = tmp_path / "linked-discovery.sqlite"
    database_url = f"sqlite:///{tmp_path / 'empty-announcements.db'}"
    monkeypatch.setenv("DISCOVERY_STORE_PATH", str(discovery_path))
    monkeypatch.setenv("DATABASE_URL", database_url)
    with sqlite3.connect(discovery_path) as connection:
        connection.execute(
            """CREATE TABLE discovery_candidates (
                candidate_id TEXT PRIMARY KEY, candidate_type TEXT NOT NULL,
                source TEXT NOT NULL, source_item_id TEXT NOT NULL,
                title TEXT, summary TEXT, published TEXT NOT NULL,
                url TEXT NOT NULL, topics_json TEXT, status TEXT NOT NULL,
                last_seen_at TEXT NOT NULL, metadata_json TEXT
            )"""
        )
        connection.executemany(
            "INSERT INTO discovery_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    f"preprint:linked:{index}", "preprint", "Example Preprints",
                    f"synthetic-{index}", f"Depression clinical evidence {index}",
                    f"Synthetic depression evidence summary {index}.", "2026-09-01",
                    f"https://example.test/preprints/linked-{index}", "[]", "candidate",
                    "2026-09-01T12:00:00Z", "{}",
                )
                for index in range(count)
            ],
        )
    history_result = announcement_context_tools.retrieve_preprint_announcement_history_impl(
        query="depression", limit=count, database_url=database_url,
    )
    history = history_result["items"]
    assert len(history) == count
    candidates = [
        {
            key: item[key]
            for key in ("feed_item_id", "title", "url", "source", "published_at", "summary")
        }
        for item in history
    ]
    return discovery_path, history, candidates


def test_signal_exact_read_correction_preserves_linked_discovery_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery_path, history, candidates = _linked_preprint_candidates(
        tmp_path, monkeypatch, count=8
    )
    selected = history[0]
    selected_id = selected["feed_item_id"]
    decision = _result_payload(candidates, selected_id=selected_id)
    source_bytes = discovery_path.read_bytes()
    model = FakeModel(
        [
            [
                _tool_call(
                    "retrieve_preprint_announcement_history",
                    {"query": "depression", "history_scope": "discovery", "limit": 8},
                    call_id="linked-history-1",
                )
            ],
            [_structured_message(decision)],
            [
                _tool_call(
                    "read_preprint_announcement_evidence",
                    {
                        "feed_item_id": selected_id,
                        "evidence_id": selected["evidence_index"][0]["evidence_id"],
                        "expected_snapshot_sha256": selected["source_snapshot"]["sha256"],
                    },
                    call_id="linked-exact-1",
                )
            ],
            [_structured_message(decision)],
        ]
    )

    result = run_signal_context_sdk(
        "preprints",
        "Which saved depression preprint merits review?",
        run_config=build_local_run_config(FakeProvider(model)),
    )

    evidence = result.request_cache["signal_decision_evidence"]
    assert evidence["candidate_ids"] == [item["feed_item_id"] for item in history]
    assert len(result.final_output.decision.candidate_assessments) == 8
    assert evidence["evidence_reads"][0]["feed_item_id"] == selected_id
    assert evidence["evidence_reads"][0]["snapshot_sha256"] == (
        selected["source_snapshot"]["sha256"]
    )
    assert evidence["evidence_reads"][0]["status"] == "success"
    assert result.final_output.articles[0].url == selected["url"]
    assert evidence["candidates"][0]["url"] == selected["url"]
    assert evidence["candidates"][0]["saved_content_scope"] == (
        "linked_discovery_summary"
    )
    assert evidence["candidates"][0]["article_full_text_verified"] is False
    assert result.request_cache["decision_ownership"]["validator_outcome"]["status"] == (
        "accepted"
    )
    assert discovery_path.read_bytes() == source_bytes
    assert all(
        "prepare_signal_lifecycle_checkpoint" not in call["tool_names"]
        and "advance_signal_lifecycle_checkpoint" not in call["tool_names"]
        for call in model.calls
    )


def test_signal_wrong_owner_still_gets_bound_exact_read_correction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery_path, history, candidates = _linked_preprint_candidates(
        tmp_path, monkeypatch, count=5
    )
    selected = history[0]
    selected_id = selected["feed_item_id"]
    initial = _result_payload(
        candidates,
        selected_id=selected_id,
        assessed_ids=[item["feed_item_id"] for item in history[:4]],
    )
    initial["decision"]["decision_owner"] = "chief_of_staff"
    corrected = _result_payload(candidates, selected_id=selected_id)
    before = discovery_path.read_bytes()
    model = FakeModel(
        [
            [
                _tool_call(
                    "retrieve_preprint_announcement_history",
                    {"query": "depression", "history_scope": "discovery", "limit": 5},
                    call_id="owner-history-1",
                )
            ],
            [_structured_message(initial)],
            [
                _tool_call(
                    "read_preprint_announcement_evidence",
                    {
                        "feed_item_id": selected_id,
                        "evidence_id": selected["evidence_index"][0]["evidence_id"],
                        "expected_snapshot_sha256": selected["source_snapshot"]["sha256"],
                    },
                    call_id="owner-exact-1",
                )
            ],
            [_structured_message(corrected)],
        ]
    )

    result = run_signal_context_sdk(
        "preprints", "Which saved depression preprint merits review?",
        run_config=build_local_run_config(FakeProvider(model)),
    )

    ownership = result.request_cache["decision_ownership"]
    assert ownership["attempts"][0]["validator_outcome"]["reason_code"] == (
        "signal_decision_owner_mismatch"
    )
    assert ownership["validator_outcome"]["status"] == "accepted"
    assert result.final_output.decision.decision_owner == "specialist_agent"
    assert len(result.final_output.decision.candidate_assessments) == 5
    assert result.final_output.decision.selected_candidate_ids == [selected_id]
    assert model.calls[2]["tool_names"] == ["read_preprint_announcement_evidence"]
    assert "decision_owner=specialist_agent" in str(model.calls[2]["input"])
    assert "decision_stage=signal_relevance_selection" in str(model.calls[2]["input"])
    assert result.request_cache["signal_decision_evidence"]["evidence_reads"][0][
        "snapshot_sha256"
    ] == selected["source_snapshot"]["sha256"]
    assert result.final_output.articles[0].url == selected["url"]
    assert discovery_path.read_bytes() == before
    assert all(
        "prepare_signal_lifecycle_checkpoint" not in call["tool_names"]
        for call in model.calls
    )


@pytest.mark.parametrize("invalid_selection", ["fabricated_id", "missing_handle"])
def test_signal_wrong_owner_does_not_read_unbound_selected_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_selection: str,
) -> None:
    _path, history, candidates = _linked_preprint_candidates(
        tmp_path, monkeypatch, count=5
    )
    selected_id = history[0]["feed_item_id"]
    if invalid_selection == "missing_handle":
        original_history = announcement_context_tools.retrieve_preprint_announcement_history_impl

        def history_without_handle(**kwargs: Any) -> dict[str, Any]:
            payload = original_history(**kwargs)
            payload["items"][0].pop("evidence_index", None)
            return payload

        monkeypatch.setattr(
            announcement_context_tools,
            "retrieve_preprint_announcement_history_impl",
            history_without_handle,
        )
    initial = _result_payload(candidates, selected_id=selected_id)
    initial["decision"]["decision_owner"] = "chief_of_staff"
    corrected = _result_payload(candidates, selected_id=selected_id)
    if invalid_selection == "fabricated_id":
        fabricated_id = "preprint:linked:unknown"
        for payload in (initial, corrected):
            payload["retrieved_item_ids"] = [fabricated_id]
            payload["articles"][0]["feed_item_id"] = fabricated_id
            payload["decision"]["selected_candidate_ids"] = [fabricated_id]
            payload["decision"]["candidate_assessments"][0]["candidate_id"] = fabricated_id
    source_reads = 0
    original_read = announcement_context_tools.read_preprint_announcement_evidence_impl

    def counted_read(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal source_reads
        source_reads += 1
        return original_read(*args, **kwargs)

    monkeypatch.setattr(
        announcement_context_tools,
        "read_preprint_announcement_evidence_impl", counted_read,
    )
    model = FakeModel(
        [
            [
                _tool_call(
                    "retrieve_preprint_announcement_history",
                    {"query": "depression", "history_scope": "discovery", "limit": 5},
                    call_id="unbound-history-1",
                )
            ],
            [_structured_message(initial)],
            [_structured_message(corrected)],
        ]
    )

    with pytest.raises(SignalAgentDecisionError) as exc_info:
        run_signal_context_sdk(
            "preprints", "Which saved depression preprint merits review?",
            run_config=build_local_run_config(FakeProvider(model)),
        )

    cache = sdk_run_failure_metadata(exc_info.value)["request_cache"]
    assert len(model.calls) == 3
    assert model.calls[2]["tool_names"] == []
    assert source_reads == 0
    assert cache["decision_repair_evidence"]["enabled_read_tool_names"] == []
    assert cache["signal_decision_evidence"]["evidence_read_call_count"] == 0


@pytest.mark.parametrize(
    ("invalid_scope", "first_reason"),
    [
        ("too_many_candidates", "signal_candidate_set_out_of_bounds"),
        ("unchanged_second_query", "signal_history_reformulation_invalid"),
        ("conflicting_source", "signal_decision_owner_mismatch"),
    ],
)
def test_signal_wrong_owner_cannot_read_outside_valid_history_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_scope: str,
    first_reason: str,
) -> None:
    count = 21 if invalid_scope == "too_many_candidates" else 5
    _path, history, candidates = _linked_preprint_candidates(
        tmp_path, monkeypatch, count=count
    )
    selected_id = history[0]["feed_item_id"]
    assessed_ids = [item["feed_item_id"] for item in history[:20]]
    initial = _result_payload(
        candidates, selected_id=selected_id, assessed_ids=assessed_ids,
    )
    initial["decision"]["decision_owner"] = "chief_of_staff"
    corrected = _result_payload(
        candidates, selected_id=selected_id, assessed_ids=assessed_ids,
    )
    if invalid_scope == "unchanged_second_query":
        original_history = announcement_context_tools.retrieve_preprint_announcement_history_impl
        history_reads = 0

        def empty_then_history(**kwargs: Any) -> dict[str, Any]:
            nonlocal history_reads
            history_reads += 1
            if history_reads == 1:
                return {
                    "status": "success", "kind": "preprints", "query": "depression",
                    "item_count": 0, "items": [], "send_enabled": False,
                }
            return original_history(**kwargs)

        monkeypatch.setattr(
            announcement_context_tools,
            "retrieve_preprint_announcement_history_impl", empty_then_history,
        )
    source_reads = 0
    original_read = announcement_context_tools.read_preprint_announcement_evidence_impl

    def counted_read(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal source_reads
        source_reads += 1
        return original_read(*args, **kwargs)

    monkeypatch.setattr(
        announcement_context_tools,
        "read_preprint_announcement_evidence_impl", counted_read,
    )
    history_calls = [
        [
            _tool_call(
                "retrieve_preprint_announcement_history",
                {"query": "depression", "history_scope": "discovery", "limit": count},
                call_id=f"invalid-history-{index}",
            )
        ]
        for index in range(2 if invalid_scope == "unchanged_second_query" else 1)
    ]
    model = FakeModel(
        [*history_calls, [_structured_message(initial)], [_structured_message(corrected)]]
    )
    request = (
        "Use only source:OtherFeed to find a depression preprint."
        if invalid_scope == "conflicting_source"
        else "Which saved depression preprint merits review?"
    )

    with pytest.raises(SignalAgentDecisionError) as exc_info:
        run_signal_context_sdk(
            "preprints", request, run_config=build_local_run_config(FakeProvider(model)),
        )

    cache = sdk_run_failure_metadata(exc_info.value)["request_cache"]
    assert cache["decision_ownership"]["attempts"][0]["validator_outcome"][
        "reason_code"
    ] == first_reason
    assert model.calls[-1]["tool_names"] == []
    assert source_reads == 0
    assert cache["signal_decision_evidence"]["evidence_read_call_count"] == 0


@pytest.mark.parametrize(
    "bad_argument", ["feed_item_id", "evidence_id", "expected_snapshot_sha256"]
)
def test_signal_exact_read_correction_blocks_unknown_identity_before_source_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_argument: str,
) -> None:
    history, candidates = _saved_preprint_candidates(tmp_path, monkeypatch)
    selected = history[0]
    selected_id = selected["feed_item_id"]
    arguments = {
        "feed_item_id": selected_id,
        "evidence_id": selected["evidence_index"][0]["evidence_id"],
        "expected_snapshot_sha256": selected["source_snapshot"]["sha256"],
    }
    arguments[bad_argument] = (
        "synthetic-preprint-unknown"
        if bad_argument == "feed_item_id"
        else "evidence:" + "0" * 24
        if bad_argument == "evidence_id"
        else "0" * 64
    )
    provider_reads = 0
    original = announcement_context_tools.read_preprint_announcement_evidence_impl

    def counted_read(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal provider_reads
        provider_reads += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        announcement_context_tools,
        "read_preprint_announcement_evidence_impl",
        counted_read,
    )
    decision = _result_payload(candidates, selected_id=selected_id)
    model = FakeModel(
        [
            [
                _tool_call(
                    "retrieve_preprint_announcement_history",
                    {"query": "psychiatric validation", "history_scope": "discovery", "limit": 8},
                    call_id="history-bad-1",
                )
            ],
            [_structured_message(decision)],
            [
                _tool_call(
                    "read_preprint_announcement_evidence",
                    arguments,
                    call_id="bad-exact-read-1",
                )
            ],
            [_structured_message(decision)],
        ]
    )

    with pytest.raises(SignalAgentDecisionError) as exc_info:
        run_signal_context_sdk(
            "preprints",
            "Which saved psychiatric validation paper merits review?",
            run_config=build_local_run_config(FakeProvider(model)),
        )

    metadata = sdk_run_failure_metadata(exc_info.value)
    assert provider_reads == 0
    assert metadata["request_cache"]["decision_ownership"]["validator_outcome"][
        "reason_code"
    ] == "selected_signal_saved_evidence_not_read"
    assert metadata["request_cache"]["signal_decision_evidence"]["evidence_reads"][0][
        "status"
    ] == "source_scope_mismatch"
    assert metadata["usage"]["requests"] == 4
    assert model.calls[2]["tool_names"] == ["read_preprint_announcement_evidence"]


def test_signal_exact_read_correction_does_not_accept_partial_saved_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history, candidates = _saved_preprint_candidates(tmp_path, monkeypatch)
    selected = history[0]
    selected_id = selected["feed_item_id"]
    monkeypatch.setattr(
        announcement_context_tools,
        "read_preprint_announcement_evidence_impl",
        lambda *_args, **_kwargs: {
            "status": "partial",
            "read_mode": "evidence_content",
            "feed_item_id": selected_id,
            "evidence_id": selected["evidence_index"][0]["evidence_id"],
            "text": "",
            "content_status": "unavailable_saved_record",
            "content_complete": False,
            "send_enabled": False,
        },
    )
    decision = _result_payload(candidates, selected_id=selected_id)
    model = FakeModel(
        [
            [
                _tool_call(
                    "retrieve_preprint_announcement_history",
                    {"query": "psychiatric validation", "history_scope": "discovery", "limit": 8},
                    call_id="history-partial-1",
                )
            ],
            [_structured_message(decision)],
            [
                _tool_call(
                    "read_preprint_announcement_evidence",
                    {
                        "feed_item_id": selected_id,
                        "evidence_id": selected["evidence_index"][0]["evidence_id"],
                        "expected_snapshot_sha256": selected["source_snapshot"]["sha256"],
                    },
                    call_id="partial-exact-read-1",
                )
            ],
            [_structured_message(decision)],
        ]
    )

    with pytest.raises(SignalAgentDecisionError) as exc_info:
        run_signal_context_sdk(
            "preprints",
            "Which saved psychiatric validation paper merits review?",
            run_config=build_local_run_config(FakeProvider(model)),
        )

    metadata = sdk_run_failure_metadata(exc_info.value)
    assert metadata["request_cache"]["decision_ownership"]["validator_outcome"][
        "reason_code"
    ] == "selected_signal_saved_evidence_not_read"
    assert metadata["request_cache"]["signal_decision_evidence"]["evidence_reads"][0][
        "status"
    ] == "partial"


def test_signal_exact_read_correction_requires_original_history_handles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history, candidates = _saved_preprint_candidates(tmp_path, monkeypatch)
    original = announcement_context_tools.retrieve_preprint_announcement_history_impl

    def missing_handles(**kwargs: Any) -> dict[str, Any]:
        result = original(**kwargs)
        for item in result["items"]:
            item.pop("evidence_index", None)
            item.pop("source_snapshot", None)
        return result

    monkeypatch.setattr(
        announcement_context_tools,
        "retrieve_preprint_announcement_history_impl",
        missing_handles,
    )
    decision = _result_payload(candidates, selected_id=history[0]["feed_item_id"])
    model = FakeModel(
        [
            [
                _tool_call(
                    "retrieve_preprint_announcement_history",
                    {"query": "psychiatric validation", "history_scope": "discovery", "limit": 8},
                    call_id="history-no-handles",
                )
            ],
            [_structured_message(decision)],
        ]
    )

    with pytest.raises(SignalAgentDecisionError) as exc_info:
        run_signal_context_sdk(
            "preprints",
            "Which saved psychiatric validation paper merits review?",
            run_config=build_local_run_config(FakeProvider(model)),
        )

    metadata = sdk_run_failure_metadata(exc_info.value)
    assert len(model.calls) == 2
    assert metadata["attempt_count"] == 1
    assert metadata["usage"]["requests"] == 2
    assert metadata["request_cache"]["decision_ownership"]["validator_outcome"][
        "reason_code"
    ] == "selected_signal_saved_evidence_not_read"


def test_signal_exact_read_correction_still_requires_all_candidate_assessments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history, candidates = _saved_preprint_candidates(tmp_path, monkeypatch)
    selected = history[0]
    selected_id = selected["feed_item_id"]
    initial = _result_payload(candidates, selected_id=selected_id)
    incomplete = _result_payload(
        candidates, selected_id=selected_id, assessed_ids=[selected_id],
    )
    model = FakeModel(
        [
            [
                _tool_call(
                    "retrieve_preprint_announcement_history",
                    {"query": "psychiatric validation", "history_scope": "discovery", "limit": 8},
                    call_id="history-incomplete-1",
                )
            ],
            [_structured_message(initial)],
            [
                _tool_call(
                    "read_preprint_announcement_evidence",
                    {
                        "feed_item_id": selected_id,
                        "evidence_id": selected["evidence_index"][0]["evidence_id"],
                        "expected_snapshot_sha256": selected["source_snapshot"]["sha256"],
                    },
                    call_id="read-incomplete-1",
                )
            ],
            [_structured_message(incomplete)],
        ]
    )

    with pytest.raises(SignalAgentDecisionError) as exc_info:
        run_signal_context_sdk(
            "preprints",
            "Which saved psychiatric validation paper merits review?",
            run_config=build_local_run_config(FakeProvider(model)),
        )

    metadata = sdk_run_failure_metadata(exc_info.value)
    assert metadata["request_cache"]["signal_decision_evidence"]["evidence_read_call_count"] == 1
    assert metadata["request_cache"]["decision_ownership"]["validator_outcome"][
        "reason_code"
    ] == "incomplete_signal_candidate_assessments"
    assert metadata["usage"]["requests"] == 4


def test_signal_runtime_allows_one_narrower_query_after_empty_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = [
        {
            "feed_item_id": "rss-bounded",
            "title": "Bounded implementation signal",
            "url": "https://example.com/rss-bounded",
            "source": "announcements",
            "summary": "A bounded behavioral-health implementation signal.",
            "published_at": "2026-08-03",
        }
    ]
    provider_calls = 0

    observed_queries: list[str] = []

    def fake_history(**kwargs: Any) -> dict[str, Any]:
        nonlocal provider_calls
        provider_calls += 1
        query = str(kwargs.get("query") or "")
        observed_queries.append(query)
        return {
            "status": "success",
            "kind": "rss",
            "item_count": 1 if query == "behavioral health implementation" else 0,
            "items": candidates if query == "behavioral health implementation" else [],
            "send_enabled": False,
        }

    monkeypatch.setattr(
        announcement_context_tools,
        "retrieve_rss_announcement_history_impl",
        fake_history,
    )
    broad_arguments = {
        "query": "recent saved behavioral health implementation review for Keystone",
        "selected_only": None,
        "limit": 8,
        "live": False,
    }
    focused_arguments = {
        "query": "behavioral health implementation",
        "selected_only": None,
        "limit": 8,
        "live": False,
    }
    model = FakeModel(
        [
            [
                _tool_call(
                    "retrieve_rss_announcement_history",
                    broad_arguments,
                    call_id="rss-history-1",
                )
            ],
            [
                _tool_call(
                    "retrieve_rss_announcement_history",
                    focused_arguments,
                    call_id="rss-history-2",
                )
            ],
            [_structured_message(_result_payload(candidates, selected_id="rss-bounded"))],
        ]
    )

    result = run_signal_context_sdk(
        "rss",
        "Which recent signal best supports implementation work?",
        run_config=build_local_run_config(FakeProvider(model)),
    )

    evidence = result.request_cache["signal_decision_evidence"]
    assert provider_calls == 2
    assert observed_queries == [
        "recent saved behavioral health implementation review for Keystone",
        "behavioral health implementation",
    ]
    assert evidence["tool_call_count"] == 2
    assert evidence["model_tool_call_count"] == 2
    assert evidence["blocked_tool_call_count"] == 0
    assert evidence["tool_result_item_counts"] == [0, 1]
    assert evidence["bounded_reformulation"] == {
        "used": True,
        "valid": True,
        "reason": "changed_query_after_empty_result",
    }
    assert result.request_cache["tool_call_budget"]["total_consumed"] == 2
    assert result.request_cache["tool_call_budget"]["total_blocked"] == 0
    assert result.request_cache["decision_ownership"]["validator_outcome"][
        "status"
    ] == "accepted"
    assert "Bounded implementation signal" in str(model.calls[2]["input"])


def test_signal_runtime_allows_synonym_reformulation_and_drops_model_bad_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = {
        "feed_item_id": "preprint-tms",
        "title": "Transcranial magnetic stimulation response study",
        "url": "https://example.test/preprints/tms",
        "source": "Example Institute",
        "summary": "A bounded synthetic neuromodulation result.",
        "published_at": "2026-08-03",
    }
    observed_queries: list[str] = []

    def fake_history(**kwargs: Any) -> dict[str, Any]:
        query = str(kwargs.get("query") or "")
        observed_queries.append(query)
        items = [candidate] if query == "transcranial magnetic stimulation" else []
        return {
            "status": "success",
            "kind": "preprints",
            "item_count": len(items),
            "items": items,
            "send_enabled": False,
        }

    monkeypatch.setattr(
        announcement_context_tools,
        "retrieve_preprint_announcement_history_impl",
        fake_history,
    )
    model = FakeModel(
        [
            [
                _tool_call(
                    "retrieve_preprint_announcement_history",
                    {
                        "query": "source:WrongFeed rTMS",
                        "selected_only": None,
                        "limit": 8,
                    },
                    call_id="preprints-tms-1",
                )
            ],
            [
                _tool_call(
                    "retrieve_preprint_announcement_history",
                    {
                        "query": "transcranial magnetic stimulation",
                        "selected_only": None,
                        "limit": 8,
                    },
                    call_id="preprints-tms-2",
                )
            ],
            [_structured_message(_result_payload([candidate], selected_id="preprint-tms"))],
        ]
    )

    result = run_signal_context_sdk(
        "preprints",
        "Which saved neuromodulation paper merits review?",
        run_config=build_local_run_config(FakeProvider(model)),
    )

    assert observed_queries == [
        "source:WrongFeed rTMS",
        "transcranial magnetic stimulation",
    ]
    assert result.final_output.retrieved_item_ids == ["preprint-tms"]
    assert result.request_cache["signal_decision_evidence"][
        "bounded_reformulation"
    ] == {
        "used": True,
        "valid": True,
        "reason": "changed_query_after_empty_result",
    }


def test_signal_runtime_blocks_a_third_history_read_before_provider_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls = 0

    def fake_history(**_kwargs: Any) -> dict[str, Any]:
        nonlocal provider_calls
        provider_calls += 1
        return {
            "status": "success",
            "kind": "rss",
            "item_count": 0,
            "items": [],
            "send_enabled": False,
        }

    monkeypatch.setattr(
        announcement_context_tools,
        "retrieve_rss_announcement_history_impl",
        fake_history,
    )
    empty_result = {
        "mode": "llm",
        "summary": "No matching bounded history was found.",
        "query": "behavioral health",
        "blockers": ["No matching bounded history was found."],
        "decision": {
            "decision_owner": "specialist_agent",
            "decision_stage": "signal_relevance_selection",
            "candidate_assessments": [],
            "reasoning": "Two bounded local queries returned no candidates.",
            "limitations": ["No source-backed candidate was available."],
            "needs_more_context": True,
        },
    }
    model = FakeModel(
        [
            [
                _tool_call(
                    "retrieve_rss_announcement_history",
                    {
                        "query": "recent saved behavioral health review",
                        "selected_only": None,
                        "limit": 8,
                        "live": False,
                    },
                    call_id="rss-history-1",
                )
            ],
            [
                _tool_call(
                    "retrieve_rss_announcement_history",
                    {
                        "query": "behavioral health",
                        "selected_only": None,
                        "limit": 8,
                        "live": False,
                    },
                    call_id="rss-history-2",
                )
            ],
            [
                _tool_call(
                    "retrieve_rss_announcement_history",
                    {
                        "query": "behavioral",
                        "selected_only": None,
                        "limit": 8,
                        "live": False,
                    },
                    call_id="rss-history-3",
                )
            ],
            [_structured_message(empty_result)],
        ]
    )

    result = run_signal_context_sdk(
        "rss",
        "Find a bounded behavioral-health signal without external expansion.",
        run_config=build_local_run_config(FakeProvider(model)),
    )

    evidence = result.request_cache["signal_decision_evidence"]
    assert provider_calls == 2
    assert evidence["tool_call_count"] == 2
    assert evidence["model_tool_call_count"] == 3
    assert evidence["blocked_tool_call_count"] == 1
    assert evidence["tool_result_item_counts"] == [0, 0]
    assert evidence["bounded_reformulation"]["valid"] is True
    assert result.request_cache["tool_call_budget"]["total_consumed"] == 2
    assert result.request_cache["tool_call_budget"]["total_blocked"] == 1
    assert result.final_output.decision.needs_more_context is True


def test_signal_validator_rejects_second_read_after_nonempty_result() -> None:
    result = RssContextResult(
        retrieved_item_ids=["rss-a"],
        articles=[{"feed_item_id": "rss-a", "title": "Selected signal"}],
        decision={
            "decision_owner": "specialist_agent",
            "decision_stage": "signal_relevance_selection",
            "selected_candidate_ids": ["rss-a"],
            "candidate_assessments": [
                {
                    "candidate_id": "rss-a",
                    "disposition": "selected",
                    "rationale": "Best bounded match.",
                }
            ],
            "reasoning": "RSS A best matches the request.",
        },
    )
    evidence = {
        "candidate_ids": ["rss-a"],
        "tool_call_count": 2,
        "tool_output_count": 2,
        "unresolved_tool_call_count": 0,
        "bounded_reformulation": {
            "used": True,
            "valid": False,
            "reason": "first_result_was_not_empty",
        },
    }

    outcome = validate_signal_agent_decision(result, evidence, max_selected=8)

    assert outcome.status == "rejected"
    assert outcome.reason_code == "signal_history_reformulation_invalid"


@pytest.mark.parametrize(
    "queries",
    [
        ("behavioral health",),
        ("source:ExampleFeed behavioral health review", "behavioral health"),
    ],
)
def test_signal_runtime_accepts_broad_reads_when_selected_evidence_matches_constraint(
    monkeypatch: pytest.MonkeyPatch,
    queries: tuple[str, ...],
) -> None:
    candidate = {
        "feed_item_id": "rss-a",
        "title": "Bounded behavioral-health signal",
        "url": "https://example.com/rss-a",
        "source": "ExampleFeed",
        "summary": "A bounded implementation signal.",
        "published_at": "2026-08-03",
    }

    def fake_history(**kwargs: Any) -> dict[str, Any]:
        items = [candidate] if str(kwargs.get("query") or "") == "behavioral health" else []
        return {
            "status": "success",
            "kind": "rss",
            "item_count": len(items),
            "items": items,
            "send_enabled": False,
        }

    monkeypatch.setattr(
        announcement_context_tools,
        "retrieve_rss_announcement_history_impl",
        fake_history,
    )
    outputs: list[list[Any]] = [
        [
            _tool_call(
                "retrieve_rss_announcement_history",
                {
                    "query": query,
                    "selected_only": None,
                    "limit": 8,
                    "live": False,
                },
                call_id=f"rss-scoped-{index}",
            )
        ]
        for index, query in enumerate(queries, start=1)
    ]
    outputs.append(
        [_structured_message(_result_payload([candidate], selected_id="rss-a"))]
    )
    model = FakeModel(outputs)

    result = run_signal_context_sdk(
        "rss",
        "Use only source:ExampleFeed for the saved feed lookup.",
        run_config=build_local_run_config(FakeProvider(model)),
    )

    evidence = result.request_cache["signal_decision_evidence"]
    assert evidence["bounded_reformulation"] == (
        {
            "used": False,
            "valid": True,
            "reason": "not_used",
        }
        if len(queries) == 1
        else {
            "used": True,
            "valid": True,
            "reason": "changed_query_after_empty_result",
        }
    )
    assert evidence["authoritative_constraints"] == [
        {"kind": "source", "relation": "exact", "value": "examplefeed"}
    ]
    assert result.request_cache["decision_ownership"]["validator_outcome"][
        "status"
    ] == "accepted"


def test_signal_evidence_records_constraint_without_requiring_query_spelling() -> None:
    candidate = {
        "feed_item_id": "rss-a",
        "title": "Bounded signal",
        "url": "https://example.test/rss-a",
    }
    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(
                type="tool_call_item",
                call_id="rss-authority-1",
                tool_name="retrieve_rss_announcement_history",
                raw_item=SimpleNamespace(
                    type="function_call",
                    call_id="rss-authority-1",
                    name="retrieve_rss_announcement_history",
                    arguments=json.dumps({"query": "behavioral health"}),
                ),
            ),
            SimpleNamespace(
                type="tool_call_output_item",
                call_id="rss-authority-1",
                output=json.dumps({"status": "success", "items": [candidate]}),
            ),
        ]
    )

    evidence = signal_decision_evidence(
        raw_result,
        tool_name="retrieve_rss_announcement_history",
        authoritative_request="Use only source:ExampleFeed.",
    )

    assert evidence["bounded_reformulation"] == {
        "used": False,
        "valid": True,
        "reason": "not_used",
    }
    assert evidence["authoritative_constraints"] == [
        {"kind": "source", "relation": "exact", "value": "examplefeed"}
    ]


@pytest.mark.parametrize(
    "queries",
    [
        ("depression",),
        ("rTMS", "transcranial magnetic stimulation"),
    ],
)
def test_signal_runtime_rejects_wrong_explicit_source_after_bounded_reads(
    monkeypatch: pytest.MonkeyPatch,
    queries: tuple[str, ...],
) -> None:
    candidate = {
        "feed_item_id": "synthetic-preprint-a",
        "title": "Depression stimulation study",
        "url": "https://doi.org/10.1234/example.42",
        "source": "Example Institute",
        "summary": "A preliminary synthetic study.",
        "published_at": "2026-08-03",
        "publication_ids": ["10.1234/example.42"],
    }
    provider_calls = 0

    def fake_history(**kwargs: Any) -> dict[str, Any]:
        nonlocal provider_calls
        provider_calls += 1
        query = str(kwargs.get("query") or "")
        items = [candidate] if query == queries[-1] else []
        return {
            "status": "success",
            "kind": "preprints",
            "item_count": len(items),
            "items": items,
            "send_enabled": False,
        }

    monkeypatch.setattr(
        announcement_context_tools,
        "retrieve_preprint_announcement_history_impl",
        fake_history,
    )
    outputs: list[list[Any]] = [
        [
            _tool_call(
                "retrieve_preprint_announcement_history",
                {"query": query, "selected_only": None, "limit": 8},
                call_id=f"preprint-wrong-source-{index}",
            )
        ]
        for index, query in enumerate(queries, start=1)
    ]
    selected = _result_payload(
        [candidate],
        selected_id="synthetic-preprint-a",
    )
    outputs.extend(
        [
            [_structured_message(selected)],
            [_structured_message(selected)],
        ]
    )

    with pytest.raises(SignalAgentDecisionError) as exc_info:
        run_signal_context_sdk(
            "preprints",
            "Use only source:OtherFeed to find a depression paper.",
            run_config=build_local_run_config(FakeProvider(FakeModel(outputs))),
        )

    metadata = sdk_run_failure_metadata(exc_info.value)
    assert provider_calls == len(queries)
    assert metadata["request_cache"]["signal_decision_evidence"][
        "bounded_reformulation"
    ]["valid"] is True
    assert metadata["request_cache"]["decision_ownership"]["validator_outcome"][
        "reason_code"
    ] == "selected_signal_constraint_violation"


def test_signal_runtime_accepts_date_constraint_after_broad_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = {
        "feed_item_id": "synthetic-preprint-a",
        "title": "Depression stimulation study",
        "url": "https://doi.org/10.1234/example.42",
        "source": "Example Institute",
        "summary": "A preliminary synthetic study.",
        "published_at": "2026-08-03",
        "publication_ids": ["10.1234/example.42"],
    }

    def fake_history(**kwargs: Any) -> dict[str, Any]:
        query = str(kwargs.get("query") or "")
        items = [candidate] if query == "transcranial magnetic stimulation" else []
        return {
            "status": "success",
            "kind": "preprints",
            "item_count": len(items),
            "items": items,
            "send_enabled": False,
        }

    monkeypatch.setattr(
        announcement_context_tools,
        "retrieve_preprint_announcement_history_impl",
        fake_history,
    )
    model = FakeModel(
        [
            [
                _tool_call(
                    "retrieve_preprint_announcement_history",
                    {"query": "rTMS", "selected_only": None, "limit": 8},
                    call_id="preprint-date-1",
                )
            ],
            [
                _tool_call(
                    "retrieve_preprint_announcement_history",
                    {
                        "query": "transcranial magnetic stimulation",
                        "selected_only": None,
                        "limit": 8,
                    },
                    call_id="preprint-date-2",
                )
            ],
            [
                _structured_message(
                    _result_payload(
                        [candidate],
                        selected_id="synthetic-preprint-a",
                    )
                )
            ],
        ]
    )

    result = run_signal_context_sdk(
        "preprints",
        "Which saved depression paper after 2026-08-01 merits review?",
        run_config=build_local_run_config(FakeProvider(model)),
    )

    evidence = result.request_cache["signal_decision_evidence"]
    assert evidence["bounded_reformulation"]["valid"] is True
    assert evidence["authoritative_constraints"] == [
        {"kind": "date", "relation": "after", "value": "2026-08-01"}
    ]
    assert result.final_output.retrieved_item_ids == ["synthetic-preprint-a"]


@pytest.mark.parametrize(
    ("request_text", "queries"),
    [
        (
            "Review https://doi.org/10.1234/example.42",
            ("rTMS", "doi:10.1234/example.42"),
        ),
        (
            "Review candidate:synthetic-preprint-a",
            ("id:synthetic-preprint-a",),
        ),
    ],
)
def test_signal_runtime_accepts_equivalent_exact_identity_forms(
    monkeypatch: pytest.MonkeyPatch,
    request_text: str,
    queries: tuple[str, ...],
) -> None:
    candidate = {
        "feed_item_id": "synthetic-preprint-a",
        "title": "Depression stimulation study",
        "url": "https://doi.org/10.1234/example.42",
        "source": "Example Institute",
        "summary": "A preliminary synthetic study.",
        "published_at": "2026-08-03",
        "publication_ids": ["10.1234/example.42"],
    }

    def fake_history(**kwargs: Any) -> dict[str, Any]:
        query = str(kwargs.get("query") or "")
        items = [candidate] if query == queries[-1] else []
        return {
            "status": "success",
            "kind": "preprints",
            "item_count": len(items),
            "items": items,
            "send_enabled": False,
        }

    monkeypatch.setattr(
        announcement_context_tools,
        "retrieve_preprint_announcement_history_impl",
        fake_history,
    )
    outputs: list[list[Any]] = [
        [
            _tool_call(
                "retrieve_preprint_announcement_history",
                {"query": query, "selected_only": None, "limit": 8},
                call_id=f"preprint-equivalent-{index}",
            )
        ]
        for index, query in enumerate(queries, start=1)
    ]
    outputs.append(
        [
            _structured_message(
                _result_payload(
                    [candidate],
                    selected_id="synthetic-preprint-a",
                )
            )
        ]
    )

    result = run_signal_context_sdk(
        "preprints",
        request_text,
        run_config=build_local_run_config(FakeProvider(FakeModel(outputs))),
    )

    assert result.final_output.retrieved_item_ids == ["synthetic-preprint-a"]
    assert result.request_cache["decision_ownership"]["validator_outcome"][
        "status"
    ] == "accepted"


@pytest.mark.parametrize(
    "request_text",
    [
        "Review doi:10.1234/different.99",
        "Review id:synthetic-preprint-other",
    ],
)
def test_signal_runtime_rejects_genuinely_different_exact_identity(
    monkeypatch: pytest.MonkeyPatch,
    request_text: str,
) -> None:
    candidate = {
        "feed_item_id": "synthetic-preprint-a",
        "title": "Depression stimulation study",
        "url": "https://doi.org/10.1234/example.42",
        "source": "Example Institute",
        "summary": "A preliminary synthetic study.",
        "published_at": "2026-08-03",
        "publication_ids": ["10.1234/example.42"],
    }

    def fake_history(**_kwargs: Any) -> dict[str, Any]:
        return {
            "status": "success",
            "kind": "preprints",
            "item_count": 1,
            "items": [candidate],
            "send_enabled": False,
        }

    monkeypatch.setattr(
        announcement_context_tools,
        "retrieve_preprint_announcement_history_impl",
        fake_history,
    )
    selected = _result_payload([candidate], selected_id="synthetic-preprint-a")
    model = FakeModel(
        [
            [
                _tool_call(
                    "retrieve_preprint_announcement_history",
                    {"query": "depression", "selected_only": None, "limit": 8},
                    call_id="preprint-different-id-1",
                )
            ],
            [_structured_message(selected)],
            [_structured_message(selected)],
        ]
    )

    with pytest.raises(SignalAgentDecisionError) as exc_info:
        run_signal_context_sdk(
            "preprints",
            request_text,
            run_config=build_local_run_config(FakeProvider(model)),
        )

    metadata = sdk_run_failure_metadata(exc_info.value)
    assert metadata["request_cache"]["decision_ownership"]["validator_outcome"][
        "reason_code"
    ] == "selected_signal_constraint_violation"


def test_signal_runtime_repairs_missing_constraint_evidence_with_honest_decline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = {
        "feed_item_id": "synthetic-preprint-a",
        "title": "Depression stimulation study",
        "url": "https://example.test/preprint-a",
        "summary": "A preliminary synthetic study.",
        "published_at": "2026-08-03",
    }

    def fake_history(**_kwargs: Any) -> dict[str, Any]:
        return {
            "status": "success",
            "kind": "preprints",
            "item_count": 1,
            "items": [candidate],
            "send_enabled": False,
        }

    monkeypatch.setattr(
        announcement_context_tools,
        "retrieve_preprint_announcement_history_impl",
        fake_history,
    )
    selected = _result_payload([candidate], selected_id="synthetic-preprint-a")
    declined = {
        "mode": "llm",
        "summary": "The returned candidate lacks the requested source evidence.",
        "query": "depression",
        "blockers": ["The exact source constraint cannot be verified."],
        "decision": {
            "decision_owner": "specialist_agent",
            "decision_stage": "signal_relevance_selection",
            "selected_candidate_ids": [],
            "candidate_assessments": [
                {
                    "candidate_id": "synthetic-preprint-a",
                    "disposition": "excluded",
                    "rationale": "The candidate has no source field to verify.",
                }
            ],
            "reasoning": "The exact source constraint is not supported by the evidence.",
            "limitations": ["Candidate source evidence is missing."],
            "needs_more_context": True,
        },
    }
    model = FakeModel(
        [
            [
                _tool_call(
                    "retrieve_preprint_announcement_history",
                    {"query": "depression", "selected_only": None, "limit": 8},
                    call_id="preprint-missing-source-1",
                )
            ],
            [_structured_message(selected)],
            [_structured_message(declined)],
        ]
    )

    result = run_signal_context_sdk(
        "preprints",
        "Use only source:ExampleFeed for the saved paper lookup.",
        run_config=build_local_run_config(FakeProvider(model)),
    )

    telemetry = result.request_cache["decision_ownership"]
    assert result.final_output.decision.needs_more_context is True
    assert telemetry["attempts"][0]["validator_outcome"][
        "reason_code"
    ] == "selected_signal_constraint_evidence_missing"
    assert telemetry["validator_outcome"]["reason_code"] == (
        "agent_requested_more_signal_context"
    )


def test_signal_runtime_preserves_both_attempts_when_tool_free_repair_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = [
        {
            "feed_item_id": "rss-one",
            "title": "Implementation evidence update",
            "url": "https://example.com/rss-one",
            "source": "announcements",
            "summary": "A bounded implementation-evidence signal.",
            "published_at": "2026-08-03",
        }
    ]
    provider_calls = 0

    def fake_history(**_kwargs: Any) -> dict[str, Any]:
        nonlocal provider_calls
        provider_calls += 1
        return {
            "status": "success",
            "kind": "rss",
            "item_count": 1,
            "items": candidates,
            "send_enabled": False,
        }

    monkeypatch.setattr(
        announcement_context_tools,
        "retrieve_rss_announcement_history_impl",
        fake_history,
    )
    invalid = _result_payload(
        candidates,
        selected_id="rss-one",
        assessed_ids=[],
    )
    model = FakeModel(
        [
            [
                _tool_call(
                    "retrieve_rss_announcement_history",
                    {
                        "query": "implementation evidence",
                        "selected_only": None,
                        "limit": 8,
                        "live": False,
                    },
                    call_id="rss-history-failure",
                )
            ],
            [_structured_message(invalid)],
            [_structured_message(invalid)],
        ]
    )

    with pytest.raises(SignalAgentDecisionError) as exc_info:
        run_signal_context_sdk(
            "rss",
            "Which signal best supports our implementation evidence work?",
            run_config=build_local_run_config(FakeProvider(model)),
        )

    metadata = sdk_run_failure_metadata(exc_info.value)
    assert provider_calls == 1
    assert len(model.calls) == 3
    assert model.calls[2]["tool_names"] == []
    assert metadata["usage"]["requests"] == 3
    assert metadata["usage"]["attempt_count"] == 2
    assert metadata["request_cache"]["decision_ownership"]["attempt_count"] == 2
    assert metadata["request_cache"]["decision_ownership"]["validator_outcome"][
        "status"
    ] == "rejected"
    assert metadata["execution_telemetry"]["signal_recovery"]["attempt_count"] == 2
    assert metadata["request_cache"]["signal_decision_evidence"][
        "raw_provider_payload_retained"
    ] is False


@pytest.mark.parametrize(
    ("route", "kind", "result_type", "tool_name", "item_id", "noun"),
    [
        (
            "rss_context_agent",
            "rss",
            RssContextResult,
            "retrieve_rss_announcement_history",
            "rss-implementation",
            "RSS update",
        ),
        (
            "preprints_context_agent",
            "preprints",
            PreprintsContextResult,
            "retrieve_preprint_announcement_history",
            "preprint-implementation",
            "preprint",
        ),
    ],
)
def test_direct_cli_signal_path_delegates_to_strong_runtime(
    route: str,
    kind: str,
    result_type: type[RssContextResult] | type[PreprintsContextResult],
    tool_name: str,
    item_id: str,
    noun: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from keystone_agents.entrypoints import cli_impl

    request = f"Which recent {noun} best fits our behavioral-health implementation work?"
    output = result_type(
        mode="llm",
        summary="Selected the implementation update: https://example.com/implementation",
        query="behavioral health implementation",
        retrieved_item_ids=[item_id],
        articles=[
            {
                "feed_item_id": item_id,
                "title": "Behavioral health implementation partnership",
                "url": "https://example.com/implementation",
                "source": "announcements",
                "relevance_status": "selected",
                "selection_reason": "Direct implementation fit.",
            }
        ],
        decision={
            "decision_owner": "specialist_agent",
            "decision_stage": "signal_relevance_selection",
            "selected_candidate_ids": [item_id],
            "candidate_assessments": [
                {
                    "candidate_id": item_id,
                    "disposition": "selected",
                    "rationale": "Direct implementation fit.",
                }
            ],
            "reasoning": "This is the closest match to the requested work.",
        },
    )
    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(
                type="tool_call_item",
                call_id="signal-direct-1",
                tool_name=tool_name,
            ),
            SimpleNamespace(
                type="tool_call_output_item",
                call_id="signal-direct-1",
                output=json.dumps(
                    {
                        "status": "success",
                        "kind": kind,
                        "item_count": 1,
                        "items": [
                            {
                                "feed_item_id": item_id,
                                "title": "Behavioral health implementation partnership",
                                "url": "https://example.com/implementation",
                            }
                        ],
                        "send_enabled": False,
                    }
                ),
            ),
        ]
    )
    decision_ownership = {
        "schema": "keystone.agent_decision_run.v1",
        "route": route,
        "decision_owner": "specialist_agent",
        "decision_stage": "signal_relevance_selection",
        "attempt_count": 1,
        "repair_attempted": False,
        "validator_outcome": {"status": "accepted"},
        "events": [],
    }
    calls: list[dict[str, Any]] = []
    persisted_outputs: list[dict[str, Any]] = []

    def fake_signal_run(kind: str, request_text: str, **kwargs: Any) -> Any:
        calls.append({"kind": kind, "request_text": request_text, **kwargs})
        return TypedAgentRunResult(
            agent_name=route,
            output=output,
            raw_result=raw_result,
            live=True,
            usage={
                "available": True,
                "requests": 2,
                "provider_request_count_confirmed": True,
            },
            cost={"available": True, "estimated_usd": 0.001},
            budget_guard={"status": "within_budget"},
            request_cache={
                "static_prefix_sha256": "a" * 64,
                "instructions_sha256": "b" * 64,
                "output_schema_sha256": "c" * 64,
                "tool_names_sha256": "d" * 64,
                "dynamic_prompt_sha256": "e" * 64,
                "dynamic_prompt_chars": len(request_text),
                "request_tool_scope": {
                    "selected_tool_names": [tool_name],
                    "tool_schema_fingerprints": {tool_name: "f" * 64},
                },
                "trace_summary": {
                    "event_id": 7,
                    "trace_id": "signal-trace-7",
                    "model_tool_calls": [
                        {
                            "call_order": 1,
                            "name": tool_name,
                            "status": "completed",
                            "output_observed": True,
                            "call_id_fingerprint": "1" * 64,
                        }
                    ],
                },
                "tool_invocations": [
                    {
                        "tool_name": tool_name,
                        "invocation_index": 1,
                        "status": "started",
                    },
                    {
                        "tool_name": tool_name,
                        "invocation_index": 1,
                        "status": "completed",
                    },
                ],
                "tool_execution": {
                    "mode": "llm_selected_function_tools",
                    "selected_tool_names": [tool_name],
                    "model_tool_call_count": 1,
                    "model_called_tool_names": [tool_name],
                    "workflow_tool_call_count": 0,
                    "workflow_called_tool_names": [],
                    "workflow_called_helper_names": [],
                    "preacquired_context_tool_names": [],
                    "provider_request_attempt_count": 1,
                    "provider_request_attempt_count_available": True,
                    "provider_request_success_count": 1,
                    "provider_request_success_count_available": True,
                    "provider_receipt_count": 0,
                    "provider_receipt_count_available": True,
                },
                "signal_decision_evidence": {
                    "source": "first_attempt_model_called_history_tool",
                    "tool_name": tool_name,
                    "candidate_count": 1,
                    "candidate_ids": [item_id],
                    "candidates": [
                        {
                            "candidate_id": item_id,
                            "title": "Behavioral health implementation partnership",
                        }
                    ],
                    "evidence_fingerprint": "2" * 64,
                    "raw_provider_payload_retained": False,
                },
                "decision_ownership": decision_ownership,
            },
            execution_telemetry={
                "schema": "keystone.execution_telemetry_summary.v1",
                "status": "completed",
                "span_count": 2,
                "failed_span_count": 0,
                "turn_count": 2,
                "attempt_count": 1,
                "total_duration_ms": 25.0,
                "first_feedback_ms": 10.0,
                "final_response_ms": 25.0,
                "stage_duration_ms": {"sdk.model": 20.0},
            },
        )

    monkeypatch.setattr(cli_impl, "run_signal_context_sdk", fake_signal_run)
    monkeypatch.setattr(
        cli_impl,
        "run_typed_sdk_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("direct signals must not use the generic SDK path")
        ),
    )
    def fake_save_agent_run(*_args: Any, **kwargs: Any) -> int:
        persisted_outputs.append(dict(kwargs["output"]))
        return 91

    monkeypatch.setattr(cli_impl.SQLiteStore, "save_agent_run", fake_save_agent_run)
    plan = ManualRequestPlan(
        source="canonical:test",
        target_agent=route,
        intent="context_lookup",
        requires_durable_state=False,
        ask_shape={"permission_state": "read_only"},
    )

    exit_code = cli_impl._run_ask_context_agent_live(
        route,
        request,
        json_output=True,
        manual_plan=plan,
        database_url="sqlite:///:memory:",
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert calls and calls[0]["kind"] == kind
    assert calls[0]["agent"].name == route
    assert payload["decision_ownership"]["validator_outcome"]["status"] == "accepted"
    assert payload["tool_execution"]["model_called_tool_names"] == [
        tool_name
    ]
    assert payload["side_effects"]["external_write_performed"] is False
    assert payload["model_execution"]["decision_repairs"] == 0
    assert "_sdk_request_cache" not in payload
    assert len(persisted_outputs) == 1
    persisted = persisted_outputs[0]
    assert persisted["_sdk_model"]["name"]
    assert persisted["_sdk_usage"]["requests"] == 2
    assert persisted["_sdk_budget_guard"]["status"] == "within_budget"
    assert persisted["_sdk_request_cache"]["dynamic_prompt_sha256"] == "e" * 64
    assert persisted["_sdk_request_cache"]["signal_decision_evidence"][
        "candidate_ids"
    ] == [item_id]
    assert persisted["_execution_telemetry"]["total_duration_ms"] == 25.0


@pytest.mark.parametrize(
    "route",
    ["rss_context_agent", "preprints_context_agent"],
)
def test_direct_signal_runtime_admission_does_not_depend_on_signal_keywords(
    route: str,
) -> None:
    from keystone_agents.entrypoints import cli_impl

    assert cli_impl._direct_signal_decision_runtime_required(
        route,
        input_text="What should I pay attention to here this week?",
    )


@pytest.mark.parametrize(
    ("route", "kind", "result_type", "tool_name", "item_id"),
    [
        (
            "rss_context_agent",
            "rss",
            RssContextResult,
            "retrieve_rss_announcement_history",
            "rss-scripted-root",
        ),
        (
            "preprints_context_agent",
            "preprints",
            PreprintsContextResult,
            "retrieve_preprint_announcement_history",
            "preprint-scripted-root",
        ),
    ],
)
def test_actual_scripted_signal_sdk_persists_and_rehydrates_into_tool_free_followup(
    route: str,
    kind: str,
    result_type: type[RssContextResult] | type[PreprintsContextResult],
    tool_name: str,
    item_id: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    from keystone_agents.agents import orchestrator as orchestrator_module
    from keystone_agents.entrypoints import cli_impl
    from keystone_agents.schemas.execution_request import DirectAgentResponse
    from keystone_agents.schemas.orchestrator import OrchestratorResult
    from keystone_agents.storage.sqlite_store import SQLiteStore

    root_request = "Select the strongest bounded behavioral-health signal."
    source = {
        "feed_item_id": item_id,
        "title": "Behavioral health implementation partnership",
        "url": f"https://example.test/{kind}/implementation",
        "source": "synthetic-provider-history",
        "published_at": "2026-09-15",
        "summary": "A public program is seeking implementation-evidence support.",
        "source_basis": (
            f"feed={kind}; selected=true; evidence_status=historical_summary_only"
        ),
        "evidence_status": "historical_summary_only",
        "evidence_notes": ["Only stored provider summary text was available."],
    }
    sparse_unselected = {
        "feed_item_id": f"{item_id}-unselected",
        "title": "Sparse unselected provider candidate",
        "url": f"https://example.test/{kind}/sparse-unselected",
        "source": "synthetic-provider-history",
        "published_at": "",
        "summary": "",
        "source_basis": f"feed={kind}; selected=false; evidence_status=metadata_only",
        "evidence_status": "metadata_only",
        "evidence_notes": [],
    }
    retrieval = {
        "status": "success",
        "kind": kind,
        "query": "behavioral health implementation",
        "item_count": 2,
        "items": [source, sparse_unselected],
        "send_enabled": False,
    }
    implementation_name = (
        "retrieve_preprint_announcement_history_impl"
        if kind == "preprints"
        else "retrieve_rss_announcement_history_impl"
    )
    monkeypatch.setattr(
        announcement_context_tools,
        implementation_name,
        lambda **_kwargs: retrieval,
    )
    root_model = FakeModel(
        [
            [
                _tool_call(
                    tool_name,
                    {
                        "query": "behavioral health implementation",
                        "selected_only": None,
                        "limit": 8,
                        "live": False,
                    },
                    call_id=f"{kind}-scripted-history",
                )
            ],
            [
                _structured_message(
                    _result_payload(
                        [source, sparse_unselected],
                        selected_id=item_id,
                    )
                )
            ],
        ]
    )
    run_config = build_local_run_config(FakeProvider(root_model))

    def scripted_signal_runtime(
        signal_kind: str,
        request_text: str,
        **kwargs: Any,
    ) -> TypedAgentRunResult[Any]:
        kwargs.pop("live", None)
        return run_signal_context_sdk(
            signal_kind,
            request_text,
            run_config=run_config,
            live=False,
            **kwargs,
        )

    monkeypatch.setattr(cli_impl, "run_signal_context_sdk", scripted_signal_runtime)
    database_url = f"sqlite:///{tmp_path / f'{kind}-scripted.db'}"
    root_plan = ManualRequestPlan(
        source="canonical:test",
        target_agent=route,
        intent="context_lookup",
        requires_durable_state=False,
        ask_shape={"permission_state": "read_only"},
    )
    root_result = cli_impl._run_ask_context_agent_live(
        route,
        root_request,
        json_output=True,
        manual_plan=root_plan,
        database_url=database_url,
        execution_context={
            "slack_scope": {
                "team_id": "T-SCRIPTED",
                "channel_id": "C-SCRIPTED",
                "thread_ts": "1789001000.000100",
                "request_ts": "1789001001.000100",
            }
        },
    )
    root_payload = json.loads(capsys.readouterr().out)
    assert root_result == 0
    assert len(root_model.calls) == 2
    assert root_payload["completion_confirmed"] is True
    assert root_payload["side_effects"]["external_write_performed"] is False

    persisted = next(
        row
        for row in SQLiteStore(database_url).fetch_all("agent_runs")
        if row["agent_name"] == route
    )
    persisted_payload = json.loads(persisted["output_json"])
    evidence = persisted_payload["_sdk_request_cache"]["signal_decision_evidence"]
    assert evidence["schema"] == "keystone.signal_decision_evidence.v1"
    assert evidence["candidates"][0]["url"] == source["url"]
    assert evidence["candidates"][0]["summary"] == source["summary"]
    assert evidence["candidate_ids"] == [item_id, sparse_unselected["feed_item_id"]]
    assert evidence["candidates"][1]["published_at"] == ""
    assert evidence["candidates"][1]["summary"] == ""

    followup = (
        "Does that justify action, or is the evidence still too limited to do "
        "anything beyond monitoring?"
    )
    context_path = tmp_path / f"{kind}-followup.json"
    context_path.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.history_context.v1",
                "team_id": "T-SCRIPTED",
                "channel_id": "C-SCRIPTED",
                "thread_ts": "1789001000.000100",
                "request_ts": "1789001002.000100",
                "thread_root_request": root_request,
            }
        ),
        encoding="utf-8",
    )
    state = cli_impl._orchestrator_workflow_state_from_cli_context(
        context_file_path=str(context_path),
        request_text=followup,
        database_url=database_url,
    )
    monkeypatch.setattr(
        orchestrator_module,
        "_route_ambiguous_with_llm",
        lambda *_args, **_kwargs: OrchestratorResult(
            route="opportunity_scout",
            workflow=["opportunity_scout"],
            routing_mode="llm",
            context_only_response=True,
            rationale="Assess only the retained source without another read.",
        ),
    )
    preflight = orchestrator_module.run_orchestrator_preflight(
        followup,
        requested_agent="opportunity_scout",
        live_orchestrator=True,
        workflow_state=state,
    )
    context = preflight.composition_admission.verified_signal_context
    assert preflight.execution_allowed is True
    assert context is not None
    assert context.selected_sources[0].url == source["url"]
    assert context.selected_sources[0].supported_claim == source["summary"]
    assert any("Only stored provider summary" in value for value in context.limitations)

    execution_context = cli_impl._direct_specialist_execution_context(
        followup,
        workflow_state=state,
        force_thread_context=True,
        verified_signal_context=context,
    )
    answer = (
        "The evidence is still too limited for action, so monitor it: "
        f"<{source['url']}|source>"
    )

    def fake_followup(**kwargs: Any) -> SimpleNamespace:
        assert kwargs["agent"].tools == []
        assert kwargs["agent"].handoffs == []
        assert kwargs["typed_input"].original_request == followup
        selected_context = kwargs["typed_input"].selected_context
        assert source["url"] in selected_context
        assert source["summary"] in selected_context
        assert "model_generated_unverified" in selected_context
        return SimpleNamespace(
            output=DirectAgentResponse(answer=answer),
            usage={"requests": 1},
            cost={"estimated_usd": 0.001},
            budget_guard={"status": "passed"},
            request_cache={},
        )

    monkeypatch.setattr(cli_impl, "run_typed_sdk_agent", fake_followup)
    followup_result = cli_impl._run_direct_supplied_context_response_live(
        "opportunity_scout",
        followup,
        json_output=True,
        manual_plan=preflight.manual_request_plan,
        orchestrator_preflight=preflight,
        sdk_session_spec=None,
        database_url=database_url,
        execution_context=execution_context,
    )
    followup_payload = json.loads(capsys.readouterr().out)
    assert followup_result == 0
    assert followup_payload["status"] == "completed"
    assert followup_payload["public_result"]["text"] == answer
    assert followup_payload["retained_signal_validation"]["passed"] is True
    assert followup_payload["tool_admission"]["tool_count"] == 0
    assert followup_payload["tool_execution"]["provider_request_attempt_count"] == 0
    assert followup_payload["side_effects"]["external_write_performed"] is False
