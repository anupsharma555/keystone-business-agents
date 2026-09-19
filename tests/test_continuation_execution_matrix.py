"""Execution-level continuation matrix for retained and refreshed evidence."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agents.models.interface import Model, ModelProvider, ModelResponse
from agents.usage import Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from keystone_agents.agents import orchestrator as orchestrator_module
from keystone_agents.agents.gmail_triage import run_gmail_triage_sdk
from keystone_agents.entrypoints import cli_impl as cli
from keystone_agents.models import GmailTriageSDKInput
from keystone_agents.planning.composition_admission import (
    resolve_provider_free_composition_admission,
)
from keystone_agents.runtime.signal_context import run_signal_context_sdk
from keystone_agents.schemas.announcement_feed import AnnouncementFeedItem
from keystone_agents.schemas.email_triage import EmailTriageResult
from keystone_agents.schemas.execution_request import DirectAgentResponse
from keystone_agents.schemas.manual_request_plan import AskShapePolicy, ManualRequestPlan
from keystone_agents.schemas.orchestrator import OrchestratorResult
from keystone_agents.sdk import build_local_run_config
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools import announcement_context_tools, gmail_query_tools

TEAM_ID = "T-SYNTHETIC"
CHANNEL_ID = "C-SYNTHETIC"
THREAD_TS = "1790000000.000100"
ROOT_REQUEST_TS = "1790000001.000100"
FOLLOWUP_REQUEST_TS = "1790000002.000100"
GMAIL_MESSAGE_ID = "msg-synthetic-continuation"
GMAIL_THREAD_ID = "thread-synthetic-continuation"
GMAIL_LINK = "https://evidence.example/gmail/synthetic-continuation"
PREPRINT_ID = "preprint-synthetic-continuation"
PREPRINT_LINK = "https://evidence.example/preprints/synthetic-continuation"


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
        del model_settings, output_schema, handoffs, tracing, previous_response_id
        del conversation_id, prompt
        self.calls.append(
            {
                "system_instructions": system_instructions,
                "input": input,
                "tool_names": [tool.name for tool in tools],
            }
        )
        return ModelResponse(
            output=self.outputs.pop(0),
            usage=Usage(requests=1, input_tokens=40, output_tokens=20),
            response_id=f"continuation-fake-{len(self.calls)}",
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
        id="continuation-structured-output",
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


def _database_url(tmp_path: Path, source: str, path: str, family: str) -> str:
    return f"sqlite:///{tmp_path / f'{source}-{path}-{family}.db'}"


def _slack_scope() -> dict[str, str]:
    return {
        "team_id": TEAM_ID,
        "channel_id": CHANNEL_ID,
        "channel_name": "synthetic-evidence",
        "thread_ts": THREAD_TS,
        "request_ts": FOLLOWUP_REQUEST_TS,
    }


def _gmail_prior_run(run_id: str = "gmail-prior-run") -> dict[str, str]:
    return {
        "id": run_id,
        "route": "gmail_triage",
        "status": "completed",
        "thread_correlation": "same_thread",
        "object_id": GMAIL_THREAD_ID,
        "title": "Synthetic newsletter evidence",
        "summary": (
            "The selected synthetic newsletter describes an early evaluation pilot. "
            f"Source: {GMAIL_LINK}. Limitation: the result is preliminary and was "
            "not refreshed for this continuation."
        ),
    }


def _gmail_verified_object() -> dict[str, Any]:
    return {
        "provider_system": "gmail",
        "object_type": "gmail_message",
        "object_id": GMAIL_MESSAGE_ID,
        "display_name": "Synthetic newsletter evidence",
        "lifecycle_state": "active",
        "verification_status": "verified",
        "provider_scope": {"thread_id": GMAIL_THREAD_ID},
    }


def _preprint_source() -> dict[str, Any]:
    return {
        "feed_item_id": PREPRINT_ID,
        "title": "Synthetic behavioral-health evaluation preprint",
        "url": PREPRINT_LINK,
        "source": "Synthetic Preprints",
        "published_at": "2026-09-14",
        "summary": "The preprint reports an early evaluation result.",
        "selection_reason": "It matches the bounded evidence-monitoring request.",
        "relevance_to_keystone": "It may inform evaluation planning.",
        "evidence_status": "verified_signal_history",
        "source_basis": "Synthetic provider evidence for offline tests.",
        "evidence_notes": ["Preliminary preprint; no fresh verification."],
    }


def _signal_evidence(source: dict[str, Any]) -> dict[str, Any]:
    candidate = {
        "candidate_id": source["feed_item_id"],
        "title": source["title"],
        "url": source["url"],
        "published_at": source["published_at"],
        "source": source["source"],
        "summary": source["summary"],
        "selection_reason": source["selection_reason"],
        "source_basis": source["source_basis"],
        "evidence_status": source["evidence_status"],
        "evidence_notes": source["evidence_notes"],
    }
    payload: dict[str, Any] = {
        "schema": "keystone.signal_decision_evidence.v1",
        "source": "first_attempt_model_called_history_tool",
        "tool_name": "retrieve_preprint_announcement_history",
        "tool_call_count": 1,
        "model_tool_call_count": 1,
        "blocked_tool_call_count": 0,
        "unresolved_tool_call_count": 0,
        "tool_output_count": 1,
        "candidate_count": 1,
        "candidate_ids": [source["feed_item_id"]],
        "candidates": [candidate],
        "raw_provider_payload_retained": False,
    }
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    payload["evidence_fingerprint"] = sha256(canonical.encode()).hexdigest()
    return payload


def _save_gmail_run(database_url: str) -> str:
    output = {
        "status": "done",
        "public_result": {
            "status": "completed",
            "text": _gmail_prior_run()["summary"],
            "completion_confirmed": True,
        },
        "continuation_objects": [_gmail_verified_object()],
        "slack_run_provenance": {
            "schema": "keystone.slack.run_provenance.v1",
            "context_validated": True,
            "team_id": TEAM_ID,
            "channel_id": CHANNEL_ID,
            "thread_ts": THREAD_TS,
            "request_ts": ROOT_REQUEST_TS,
        },
    }
    return str(
        SQLiteStore(database_url).save_agent_run(
            agent_name="gmail_triage",
            input_summary="Select the synthetic newsletter evidence.",
            output=output,
            model="offline-test-double",
            dry_run=False,
            status="success",
        )
    )


def _save_preprint_run(database_url: str) -> str:
    source = _preprint_source()
    output = {
        "status": "done",
        "route": "preprints_context_agent",
        "output": {
            "retrieved_item_ids": [PREPRINT_ID],
            "articles": [source],
            "decision": {
                "decision_owner": "specialist_agent",
                "decision_stage": "signal_relevance_selection",
                "selected_candidate_ids": [PREPRINT_ID],
                "needs_more_context": False,
                "limitations": ["Preliminary preprint; no fresh verification."],
            },
        },
        "public_result": {
            "status": "completed",
            "text": f"Selected synthetic preprint: {PREPRINT_LINK}",
            "completion_confirmed": True,
        },
        "slack_run_provenance": {
            "schema": "keystone.slack.run_provenance.v1",
            "context_validated": True,
            "team_id": TEAM_ID,
            "channel_id": CHANNEL_ID,
            "thread_ts": THREAD_TS,
            "request_ts": ROOT_REQUEST_TS,
        },
        "internal_decision_ownership": {"validator_outcome": {"status": "accepted"}},
        "_sdk_request_cache": {
            "signal_decision_evidence": _signal_evidence(source),
            "decision_ownership": {"validator_outcome": {"status": "accepted"}},
        },
    }
    return str(
        SQLiteStore(database_url).save_agent_run(
            agent_name="preprints_context_agent",
            input_summary="Select the synthetic preprint.",
            output=output,
            model="offline-test-double",
            dry_run=False,
            status="success",
        )
    )


def _direct_state(source: str) -> dict[str, Any]:
    if source == "gmail":
        return {
            "slack_context": _slack_scope(),
            "slack_thread_root": "Review the synthetic newsletter evidence.",
            "prior_agent_runs": [_gmail_prior_run()],
            "verified_provider_objects": [_gmail_verified_object()],
        }
    source_row = _preprint_source()
    return {
        "slack_context": _slack_scope(),
        "slack_thread_root": "Review the synthetic preprint evidence.",
        "prior_agent_runs": [
            {
                "id": "preprint-prior-run",
                "route": "preprints_context_agent",
                "status": "completed",
                "thread_correlation": "same_thread",
                "summary": f"Selected a preliminary preprint: {PREPRINT_LINK}",
            }
        ],
        "verified_signal_context": {
            "source_run_id": "preprint-prior-run",
            "source_route": "preprints_context_agent",
            "verification_status": "verified",
            "public_result_status": "completed",
            "source_request_ts": ROOT_REQUEST_TS,
            "selected_sources": [
                {
                    "source_id": PREPRINT_ID,
                    "provider_candidate_id": PREPRINT_ID,
                    "title": source_row["title"],
                    "url": PREPRINT_LINK,
                    "source_type": "preprints_historical_context",
                    "supported_claim": source_row["summary"],
                    "provider": "preprints_context_agent",
                    "extraction_status": "verified_signal_history",
                    "retrieved_at": "2026-09-14",
                }
            ],
            "source_dates": {PREPRINT_ID: "2026-09-14"},
            "limitations": ["Preliminary preprint; no fresh verification."],
        },
    }


def _persisted_state(
    tmp_path: Path,
    database_url: str,
    *,
    source: str,
    request: str,
    include_evidence: bool,
) -> dict[str, Any]:
    prior_runs: list[dict[str, Any]] = []
    if include_evidence and source == "gmail":
        run_id = _save_gmail_run(database_url)
        prior_runs = [_gmail_prior_run(run_id)]
    elif include_evidence:
        run_id = _save_preprint_run(database_url)
        prior_runs = [
            {
                "id": run_id,
                "route": "preprints_context_agent",
                "status": "completed",
                "thread_correlation": "same_thread",
                "summary": f"Selected a preliminary preprint: {PREPRINT_LINK}",
            }
        ]
    context_path = tmp_path / f"{source}-{sha256(request.encode()).hexdigest()[:8]}.json"
    context_path.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.history_context.v1",
                **_slack_scope(),
                "thread_root_request": f"Review the synthetic {source} evidence.",
                "prior_agent_runs": prior_runs,
            }
        ),
        encoding="utf-8",
    )
    return cli._orchestrator_workflow_state_from_cli_context(
        context_file_path=str(context_path),
        request_text=request,
        database_url=database_url,
    )


def _workflow_state(
    tmp_path: Path,
    database_url: str,
    *,
    source: str,
    path: str,
    request: str,
    include_evidence: bool = True,
) -> dict[str, Any]:
    if path == "direct":
        return _direct_state(source) if include_evidence else {}
    return _persisted_state(
        tmp_path,
        database_url,
        source=source,
        request=request,
        include_evidence=include_evidence,
    )


def _route(owner: str, *, context_only: bool) -> OrchestratorResult:
    return OrchestratorResult(
        route=owner,
        workflow=[owner],
        routing_mode="llm",
        context_only_response=context_only,
        rationale=(
            "Use the completed selected evidence without another provider read."
            if context_only
            else "The current request requires one bounded provider refresh."
        ),
    )


def _run_preflight(
    monkeypatch: pytest.MonkeyPatch,
    request: str,
    state: dict[str, Any],
    *,
    owner: str,
    context_only: bool,
):
    monkeypatch.setattr(
        orchestrator_module,
        "_route_ambiguous_with_llm",
        lambda *_args, **_kwargs: _route(owner, context_only=context_only),
    )
    return orchestrator_module.run_orchestrator_preflight(
        request,
        requested_agent=owner,
        live_orchestrator=True,
        workflow_state=state,
    )


def _fail_provider_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("provider read/query must not run for retained-context execution")

    monkeypatch.setattr(gmail_query_tools, "query_gmail_message_summaries_impl", unexpected)
    monkeypatch.setattr(gmail_query_tools, "read_gmail_context_impl", unexpected)
    monkeypatch.setattr(gmail_query_tools.gmail_tool, "search_message_summaries", unexpected)
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_message_context_projection",
        unexpected,
    )
    monkeypatch.setattr(
        announcement_context_tools,
        "retrieve_preprint_announcement_history_impl",
        unexpected,
    )


def _record_observation(payload: dict[str, Any]) -> None:
    destination = os.environ.get("DUO072_MATRIX_OUTPUT", "").strip()
    if not destination:
        return
    with Path(destination).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")


def _requests(family: str, source: str) -> tuple[str, str]:
    noun = "newsletter" if source == "gmail" else "preprint"
    link = GMAIL_LINK if source == "gmail" else PREPRINT_LINK
    values = {
        "simple_rewrite": (
            f"Rewrite the retained {noun} result as one concise sentence and keep {link}.",
            f"Retained {noun} evidence remains preliminary: {link}",
        ),
        "compound_format_citation": (
            f"Give two bullets: one finding and one limitation, and retain the citation {link}.",
            f"- Finding: the retained {noun} evidence describes an early evaluation.\n"
            f"- Limitation and citation: preliminary evidence only; {link}",
        ),
        "pronoun_continuation": (
            "What does it imply for KNI, and can you keep its source link?",
            f"It supports monitoring rather than action because it remains preliminary: {link}",
        ),
        "owner_domain_change": (
            "Have Opportunity Scout assess whether that evidence is actionable, keeping its link.",
            f"Opportunity Scout assesses this as monitor-only evidence: {link}",
        ),
    }
    return values[family]


def _fresh_plan(source: str, request: str) -> ManualRequestPlan:
    route = "gmail_triage" if source == "gmail" else "preprints_context_agent"
    return ManualRequestPlan(
        source="test:actual-fresh-continuation",
        requested_agent=route,
        target_agent=route,
        intent="context_lookup" if source == "preprints" else "gmail_triage",
        task_objective="context_lookup" if source == "preprints" else "gmail_triage",
        expected_artifact_type="context_summary"
        if source == "preprints"
        else "gmail_triage_report",
        objective=request,
        provider_system="unspecified" if source == "preprints" else "gmail",
        provider_operations=["read"],
        side_effect_policy="draft_or_read_only",
        ask_shape=AskShapePolicy(
            permission_state="read_only", prior_context_dependency="selected_context"
        ),
    )


def _preflight_for_plan(
    plan: ManualRequestPlan,
    request: str,
    *,
    route: str,
) -> orchestrator_module.OrchestratorPreflight:
    return orchestrator_module.OrchestratorPreflight(
        request_text=request,
        requested_agent=route,
        selected_agent=route,
        manual_request_plan=plan,
        route_result=_route(route, context_only=False),
        composition_admission=resolve_provider_free_composition_admission(
            plan,
            workflow_state=None,
        ),
    )


def _gmail_fresh_output() -> EmailTriageResult:
    return EmailTriageResult(
        message_id=GMAIL_MESSAGE_ID,
        thread_id=GMAIL_THREAD_ID,
        subject="Synthetic newsletter evidence",
        sender_name="Synthetic Publisher",
        sender_email="publisher@example.test",
        received_at="2026-09-16T12:00:00Z",
        category="newsletter",
        confidence=0.9,
        summary=f"Fresh bounded provider context was read: {GMAIL_LINK}",
        reasoning="The exact retained message identity was refreshed once.",
        recommended_action="Monitor the preliminary evidence.",
        recommended_next_agent="human_review",
        extracted_links=[{"url": GMAIL_LINK}],
        decision={
            "decision_owner": "specialist_agent",
            "decision_stage": "gmail_verified_continuation",
            "selected_candidate_id": GMAIL_THREAD_ID,
            "selected_candidate_ids": [GMAIL_THREAD_ID],
            "needs_more_context": False,
            "reasoning": "The exact retained thread was refreshed from bounded provider context.",
            "candidate_assessments": [
                {
                    "candidate_id": GMAIL_THREAD_ID,
                    "disposition": "selected",
                    "rationale": "It is the exact retained conversation.",
                }
            ],
            "limitations": ["Only the exact retained message was refreshed."],
        },
    )


def _preprint_fresh_payload() -> dict[str, Any]:
    source = _preprint_source()
    return {
        "mode": "llm",
        "summary": f"Fresh bounded preprint history selected one item: {PREPRINT_LINK}",
        "query": "synthetic behavioral health evaluation",
        "retrieved_item_ids": [PREPRINT_ID],
        "articles": [
            {
                **source,
                "relevance_status": "selected",
                "selection_reason": "It matches the requested evidence refresh.",
                "relevance_to_keystone": "It may inform evaluation planning.",
            }
        ],
        "decision": {
            "decision_owner": "specialist_agent",
            "decision_stage": "signal_relevance_selection",
            "selected_candidate_ids": [PREPRINT_ID],
            "candidate_assessments": [
                {
                    "candidate_id": PREPRINT_ID,
                    "disposition": "selected",
                    "rationale": "It is the exact matching bounded candidate.",
                }
            ],
            "reasoning": "The single bounded candidate matches the refresh request.",
            "limitations": ["This is preliminary preprint evidence."],
        },
    }


def _run_fresh_gmail(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    request: str,
    state: dict[str, Any],
    database_url: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    plan = _fresh_plan("gmail", request)
    preflight = _preflight_for_plan(plan, request, route="gmail_triage")
    context = cli._direct_specialist_execution_context(
        request,
        workflow_state=state,
        force_thread_context=True,
    )
    read_calls: list[str] = []

    def provider_read(identity: str) -> dict[str, Any]:
        read_calls.append(identity)
        return {
            "id": GMAIL_MESSAGE_ID,
            "threadId": GMAIL_THREAD_ID,
            "received_at": "2026-09-16T12:00:00Z",
            "sender_name": "Synthetic Publisher",
            "sender_email": "publisher@example.test",
            "subject": "Synthetic newsletter evidence",
            "snippet": "A synthetic early evaluation pilot.",
            "prior_labels": ["INBOX"],
            "thread_summary": f"Synthetic provider summary with source {GMAIL_LINK}",
            "thread_context": "Synthetic current provider context; preliminary evidence.",
            "extracted_links": [{"url": GMAIL_LINK}],
            "triage_limitations": ["Only the exact retained message was refreshed."],
        }

    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_message_context_projection",
        provider_read,
    )
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        lambda **_kwargs: pytest.fail("fresh exact-object refresh must not broaden to query"),
    )
    model = FakeModel(
        [
            [
                _tool_call(
                    "read_gmail_context",
                    {"resource_type": "message", "resource_id": GMAIL_MESSAGE_ID},
                    call_id="gmail-refresh-read",
                )
            ],
            [_structured_message(_gmail_fresh_output().model_dump(mode="json"))],
        ]
    )
    sdk_errors: list[str] = []

    def sdk_boundary(_agent: Any, sdk_input: Any, _output_type: Any, **_kwargs: Any):
        try:
            result = run_gmail_triage_sdk(
                GmailTriageSDKInput(
                    subject="",
                    body="",
                    request=str(sdk_input),
                    message_id=GMAIL_MESSAGE_ID,
                    thread_id=GMAIL_THREAD_ID,
                ),
                run_config=build_local_run_config(FakeProvider(model)),
                live=True,
                manual_request_plan=plan,
                provider_selection_required=False,
                provider_context_read_required=True,
                repair_invalid_selection=False,
            )
        except Exception as exc:
            sdk_errors.append(f"{type(exc).__name__}: {exc}")
            raise
        return result.raw_result, result.final_output

    monkeypatch.setattr(cli, "run_typed_sdk_sync", sdk_boundary)
    exit_code = cli._run_ask_context_agent_live(
        "gmail_triage",
        request,
        json_output=True,
        manual_plan=plan,
        orchestrator_preflight=preflight,
        sdk_session_spec=None,
        execution_context=context,
        database_url=database_url,
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0, {"sdk_errors": sdk_errors, "payload": payload}
    assert read_calls == [GMAIL_MESSAGE_ID]
    assert len(model.calls) == 2
    assert request in str(model.calls[0]["input"])
    assert GMAIL_MESSAGE_ID in str(model.calls[0]["input"])
    assert payload["status"] == "blocked"
    assert payload["tool_execution"]["provider_request_attempt_count"] == 0
    return payload, {
        "matrix_status": "gap",
        "provider_calls": read_calls,
        "provider_call_observation": "one successful fake-provider exact-object read",
        "model_input": str(model.calls[0]["input"]),
        "sdk_boundary": "run_typed_sdk_sync -> run_gmail_triage_sdk/FakeProvider",
        "gap_reason": (
            "The generic in-process Gmail context entry observed the exact one-object "
            "refresh, but its outer current-state contract also requires mailbox schema "
            "and query evidence; the supported live Gmail entry uses a subprocess boundary "
            "that is intentionally not replaced in this offline test."
        ),
        "sdk_output": _gmail_fresh_output().summary,
    }


def _run_fresh_preprint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    request: str,
    state: dict[str, Any],
    database_url: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    plan = _fresh_plan("preprints", request)
    preflight = _preflight_for_plan(plan, request, route="preprints_context_agent")
    context = cli._direct_specialist_execution_context(
        request,
        workflow_state=state,
        force_thread_context=True,
    )
    monkeypatch.setenv("DATABASE_URL", database_url)
    SQLiteStore(database_url).save_announcement_feed_item(
        AnnouncementFeedItem(
            canonical_key=PREPRINT_ID,
            title=_preprint_source()["title"],
            url=PREPRINT_LINK,
            source="Synthetic Preprints",
            feed="preprints",
            published_at="2026-09-14",
            summary=_preprint_source()["summary"],
            selected=True,
            selection_reason=_preprint_source()["selection_reason"],
        )
    )
    original_read = announcement_context_tools.retrieve_preprint_announcement_history_impl
    read_calls: list[dict[str, Any]] = []

    def provider_read(**kwargs: Any) -> dict[str, Any]:
        read_calls.append(dict(kwargs))
        return original_read(**kwargs)

    monkeypatch.setattr(
        announcement_context_tools,
        "retrieve_preprint_announcement_history_impl",
        provider_read,
    )
    model = FakeModel(
        [
            [
                _tool_call(
                    "retrieve_preprint_announcement_history",
                    {
                        "query": "synthetic behavioral health evaluation",
                        "history_scope": "discovery",
                        "limit": 8,
                    },
                    call_id="preprint-refresh-read",
                )
            ],
            [_structured_message(_preprint_fresh_payload())],
        ]
    )

    def sdk_boundary(kind: str, sdk_input: str, **kwargs: Any):
        return run_signal_context_sdk(
            kind,
            sdk_input,
            manual_request_plan=kwargs.get("manual_request_plan"),
            live=True,
            run_config=build_local_run_config(FakeProvider(model)),
            max_turns=kwargs.get("max_turns"),
            max_selected=kwargs.get("max_selected", 8),
            agent=kwargs.get("agent"),
        )

    monkeypatch.setattr(cli, "run_signal_context_sdk", sdk_boundary)
    exit_code = cli._run_ask_context_agent_live(
        "preprints_context_agent",
        request,
        json_output=True,
        manual_plan=plan,
        orchestrator_preflight=preflight,
        sdk_session_spec=None,
        execution_context=context,
        database_url=database_url,
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert len(read_calls) == 1
    assert len(model.calls) == 2
    assert request in str(model.calls[0]["input"])
    assert PREPRINT_LINK in payload["public_result"]["text"]
    return payload, {
        "provider_calls": read_calls,
        "model_input": str(model.calls[0]["input"]),
        "sdk_boundary": "run_signal_context_sdk/FakeProvider",
    }


FAMILIES = (
    "simple_rewrite",
    "compound_format_citation",
    "pronoun_continuation",
    "owner_domain_change",
    "fresh_information",
    "missing_conflicting_evidence",
)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("source", ("gmail", "preprints"))
@pytest.mark.parametrize("path", ("direct", "persisted_slack"))
def test_continuation_execution_matrix(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    family: str,
    source: str,
    path: str,
) -> None:
    database_url = _database_url(tmp_path, source, path, family)
    if family == "fresh_information":
        request = (
            "Refresh the exact retained Gmail message from current provider context once."
            if source == "gmail"
            else "Refresh current preprint history once for the same evaluation topic."
        )
        state = _workflow_state(
            tmp_path,
            database_url,
            source=source,
            path=path,
            request=request,
        )
        if source == "gmail":
            payload, evidence = _run_fresh_gmail(
                monkeypatch,
                capsys,
                request=request,
                state=state,
                database_url=database_url,
            )
        else:
            payload, evidence = _run_fresh_preprint(
                monkeypatch,
                capsys,
                request=request,
                state=state,
                database_url=database_url,
            )
        matrix_status = str(evidence.pop("matrix_status", "passed"))
        _record_observation(
            {
                "family": family,
                "source": source,
                "path": path,
                "status": matrix_status,
                "production_functions": [
                    "_direct_specialist_execution_context",
                    "_run_ask_context_agent_live",
                    "_print_ask_live_payload",
                    "SQLiteStore.save_agent_run",
                ],
                "final_output": payload["public_result"]["text"],
                "source_link": GMAIL_LINK if source == "gmail" else PREPRINT_LINK,
                **evidence,
            }
        )
        return

    if family == "missing_conflicting_evidence":
        request = (
            "Rewrite that selected Gmail result without loading anything new."
            if source == "gmail"
            else "Rewrite only https://evidence.example/preprints/not-the-selected-source."
        )
        state = _workflow_state(
            tmp_path,
            database_url,
            source=source,
            path=path,
            request=request,
            include_evidence=source == "preprints",
        )
        _fail_provider_reads(monkeypatch)
        monkeypatch.setattr(
            cli,
            "run_typed_sdk_agent",
            lambda **_kwargs: pytest.fail("blocked evidence must not reach the SDK"),
        )
        preflight = _run_preflight(
            monkeypatch,
            request,
            state,
            owner="chief_of_staff",
            context_only=True,
        )
        expected_reason = (
            "selected_context_missing"
            if source == "gmail"
            else "selected_context_explicit_source_mismatch"
        )
        assert preflight.execution_allowed is False
        assert preflight.composition_admission.reason == expected_reason
        exit_code = cli._print_ask_preflight_blocked(
            request,
            json_output=True,
            orchestrator_preflight=preflight,
        )
        payload = json.loads(capsys.readouterr().out)
        assert exit_code == 0
        assert payload["status"] == "blocked"
        assert payload["block_kind"] == "context_only_response_unavailable"
        assert payload["block_reason"] == preflight.block_reason
        _record_observation(
            {
                "family": family,
                "source": source,
                "path": path,
                "status": "passed",
                "production_functions": [
                    "run_orchestrator_preflight",
                    "resolve_provider_free_composition_admission",
                    "_print_ask_preflight_blocked",
                ],
                "sdk_boundary": "not_invoked",
                "model_input": "not_created",
                "final_output": payload["public_result"]["text"],
                "provider_calls": [],
                "bounded_failure": expected_reason,
            }
        )
        return

    request, answer = _requests(family, source)
    state = _workflow_state(
        tmp_path,
        database_url,
        source=source,
        path=path,
        request=request,
    )
    owner = "opportunity_scout" if family == "owner_domain_change" else "chief_of_staff"
    preflight = _run_preflight(
        monkeypatch,
        request,
        state,
        owner=owner,
        context_only=True,
    )
    assert preflight.execution_allowed is True
    assert preflight.composition_admission.composition_allowed is True
    context = cli._direct_specialist_execution_context(
        request,
        workflow_state=state,
        force_thread_context=True,
        verified_signal_context=preflight.composition_admission.verified_signal_context,
    )
    _fail_provider_reads(monkeypatch)
    sdk_calls: list[dict[str, Any]] = []

    def sdk_boundary(**kwargs: Any) -> SimpleNamespace:
        sdk_calls.append(kwargs)
        assert kwargs["agent"].tools == []
        assert kwargs["agent"].handoffs == []
        assert kwargs["typed_input"].original_request == request
        model_context = kwargs["typed_input"].selected_context
        assert (GMAIL_LINK if source == "gmail" else PREPRINT_LINK) in model_context
        if source == "gmail":
            assert GMAIL_MESSAGE_ID in model_context
            assert "preliminary" in model_context.lower()
        else:
            assert PREPRINT_ID in model_context
            assert "2026-09-14" in model_context
            assert "preliminary" in model_context.lower()
        if family == "owner_domain_change":
            assert kwargs["agent"].name == "opportunity_scout"
        return SimpleNamespace(
            output=DirectAgentResponse(answer=answer),
            usage={"requests": 1},
            cost={"estimated_usd": 0.0},
            budget_guard={"status": "passed"},
            request_cache={},
        )

    monkeypatch.setattr(cli, "run_typed_sdk_agent", sdk_boundary)
    exit_code = cli._run_ask_specialist_live(
        owner,
        request,
        json_output=True,
        manual_plan=preflight.manual_request_plan,
        orchestrator_preflight=preflight,
        execution_context=context,
        database_url=database_url,
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert len(sdk_calls) == 1
    assert payload["status"] == "completed"
    assert payload["public_result"]["text"] == answer
    assert (GMAIL_LINK if source == "gmail" else PREPRINT_LINK) in payload["public_result"]["text"]
    assert payload["tool_execution"]["provider_request_attempt_count"] == 0
    assert payload["tool_execution"]["model_tool_call_count"] == 0
    persisted = json.loads(SQLiteStore(database_url).fetch_all("agent_runs")[-1]["output_json"])
    assert persisted["public_result"]["text"] == answer
    assert persisted["tool_execution"]["provider_request_attempt_count"] == 0
    _record_observation(
        {
            "family": family,
            "source": source,
            "path": path,
            "status": "passed",
            "production_functions": [
                "run_orchestrator_preflight",
                "resolve_provider_free_composition_admission",
                "_direct_specialist_execution_context",
                "_run_ask_specialist_live",
                "_run_direct_supplied_context_response_live",
                "_print_ask_live_payload",
                "SQLiteStore.save_agent_run",
            ],
            "sdk_boundary": "run_typed_sdk_agent",
            "model_input": sdk_calls[0]["typed_input"].model_dump(mode="json"),
            "final_output": payload["public_result"]["text"],
            "provider_calls": [],
            "source_identity": GMAIL_MESSAGE_ID if source == "gmail" else PREPRINT_ID,
            "source_link": GMAIL_LINK if source == "gmail" else PREPRINT_LINK,
            "selected_agent": payload["selected_agent"],
            "tool_names_at_sdk_boundary": [],
        }
    )
