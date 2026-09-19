"""Joined fake-model/provider proofs for specialist-owned retrieval decisions."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

import keystone_agents.tools.serper_tool as serper_tool
from keystone_agents.agents.business_research_analyst import (
    run_business_research_analyst_sdk,
)
from keystone_agents.agents.opportunity_scout import run_opportunity_scout_sdk
from keystone_agents.models import BusinessResearchSDKInput, OpportunityScoutSDKInput
from keystone_agents.retrieval_policy import (
    HybridSearchProvider,
    RetrievalAutonomyHint,
    RetrievalQualityAssessment,
)
from keystone_agents.schemas.manual_request_plan import AskShapePolicy, ManualRequestPlan
from keystone_agents.sdk import build_local_run_config
from keystone_agents.tools.search_provider import (
    SearchProviderError,
    SearchResult,
    search_result_candidate_id,
)

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


def _tool_call(
    name: str,
    arguments: dict[str, Any],
    *,
    call_id: str,
) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        type="function_call",
        name=name,
        call_id=call_id,
        arguments=json.dumps(arguments),
        status="completed",
    )


def _structured_message(payload: dict[str, Any]) -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id="fake-message-output",
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


class _FakeModel(Model):
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
            usage=Usage(requests=1),
            response_id=f"fake-response-{len(self.calls)}",
        )

    def stream_response(
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
    ) -> AsyncIterator[Any]:
        raise NotImplementedError


class _FakeProvider(ModelProvider):
    def __init__(self, model: _FakeModel) -> None:
        self.model = model

    def get_model(self, model_name: str | None) -> Model:
        del model_name
        return self.model


class _FailingSearchProvider:
    def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
        del query, num_results
        raise SearchProviderError("synthetic primary provider outage")


class _SuccessfulSearchProvider:
    def __init__(self, results: list[SearchResult]) -> None:
        self.results = results
        self.queries: list[str] = []

    def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
        self.queries.append(query)
        return self.results[:num_results]


def _quality(results: list[Any], request_text: str) -> RetrievalQualityAssessment:
    del request_text
    return RetrievalQualityAssessment(
        result_count=len(results),
        unique_domain_count=len(results),
        duplicate_ratio=0.0,
        primary_source_count=1 if results else 0,
        official_source_present=bool(results),
        linkedin_source_present=False,
        recent_signal_count=len(results),
        needs_precision_search=False,
        needs_structured_enrichment=False,
        needs_search_review=False,
        reasons=(),
    )


def _live_read_plan(agent: str, objective: str) -> ManualRequestPlan:
    return ManualRequestPlan(
        source="test",
        requested_agent=agent,
        target_agent=agent,
        intent=(
            "company_research"
            if agent == "business_research_analyst"
            else "opportunity_search"
        ),
        primary_target=objective,
        target_type="company" if agent == "business_research_analyst" else "opportunity",
        provider_system="unspecified",
        provider_operations=["search", "read"],
        objective=objective,
        task_objective=(
            "entity_research"
            if agent == "business_research_analyst"
            else "opportunity_discovery"
        ),
        expected_artifact_type=(
            "research_brief"
            if agent == "business_research_analyst"
            else "opportunity_record"
        ),
        ask_shape=AskShapePolicy(permission_state="read_only"),
        requires_live_search=True,
    )


def _company_payload(*, url: str, source_id: str) -> dict[str, Any]:
    provider_candidate_id = search_result_candidate_id(url)
    return {
        "name": "Northstar Behavioral",
        "website": url,
        "description": "A behavioral-health measurement company.",
        "fit_summary": "Its evidence work may be relevant to Keystone.",
        "consulting_fit_score": 82,
        "confidence_score": 0.74,
        "sources": [
            {
                "source_id": source_id,
                "provider_candidate_id": provider_candidate_id,
                "title": "Northstar evidence update",
                "url": url,
                "source_type": "news",
                "supported_claims": ["Northstar published a measurement update."],
                "confidence": 0.8,
            }
        ],
        "decision": {
            "decision_owner": "specialist_agent",
            "decision_stage": "research_source_selection",
            "selected_candidate_ids": [provider_candidate_id],
            "candidate_assessments": [
                {
                    "candidate_id": provider_candidate_id,
                    "disposition": "selected",
                    "rationale": "This source supports the returned evidence signal.",
                }
            ],
            "reasoning": "Selected the current source after reviewing search results.",
            "limitations": ["Only one current evidence source was retained."],
            "needs_more_context": False,
        },
    }


def _opportunity_payload(*, url: str) -> dict[str, Any]:
    candidate_id = search_result_candidate_id(url)
    return {
        "topic": "behavioral-health evidence partnerships",
        "dry_run": False,
        "records": [
            {
                "company_name": "Northstar Behavioral",
                "canonical_entity_key": "northstar-behavioral",
                "opportunity_type": "digital mental health",
                "priority_score": 69,
                "why_now_signal": "A current payer measurement partnership was announced.",
                "recommended_next_step": "Validate the buyer context before outreach.",
                "sources": [
                    {
                        "source_id": "source:northstar-partnership",
                        "title": "Northstar partnership announcement",
                        "url": url,
                        "source_type": "news",
                        "supported_signal": "Current payer measurement partnership.",
                    }
                ],
                "source_signals": ["payer partnership", "measurement evidence"],
                "keystone_fit_reason": "The evidence work overlaps Keystone capabilities.",
                "outside_consulting_likelihood": 80,
                "handoff_to_business_research_analyst": True,
                "handoff_reason": "The announcement does not establish the buyer workflow.",
                "business_research_analyst_handoff_recommendation": (
                    "Validate the buyer, evidence scope, and current implementation context."
                ),
                "research_needed": [
                    "Confirm the payer buyer and implementation scope from primary sources."
                ],
                "outreach_draft": None,
                "approval_required_before_outreach": True,
            }
        ],
        "audit_notes": ["Agent-owned fake-provider test."],
        "outreach_generated": False,
        "decision": {
            "decision_owner": "specialist_agent",
            "decision_stage": "opportunity_candidate_selection",
            "selected_candidate_ids": [candidate_id],
            "candidate_assessments": [
                {
                    "candidate_id": candidate_id,
                    "disposition": "selected",
                    "rationale": "It is current, relevant, and source-backed.",
                }
            ],
            "reasoning": "Selected the current candidate after search and scoring.",
            "limitations": ["Buyer workflow still needs validation."],
            "needs_more_context": False,
        },
    }


def test_research_model_owns_search_and_repairs_invalid_source_after_provider_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_request = (
        "Can you look into Northstar Behavioral's latest measurement work and tell me "
        "whether it seems worth a conversation?"
    )
    valid_url = "https://northstar.example/evidence-update"
    secondary_url = "https://coverage.example/northstar-overview"
    fallback = _SuccessfulSearchProvider(
        [
            SearchResult(
                title="Northstar evidence update",
                link=valid_url,
                snippet="Northstar described a current behavioral-health measurement update.",
                source="agents-web-search",
            ),
            SearchResult(
                title="Northstar overview",
                link=secondary_url,
                snippet="Independent overview of Northstar Behavioral.",
                source="agents-web-search",
            ),
        ]
    )
    hybrid = HybridSearchProvider(
        provider_sequence=("searxng", "agents-web-search"),
        autonomy_hint=RetrievalAutonomyHint(),
        quality_assessor=_quality,
        provider_factory=lambda name: (
            _FailingSearchProvider() if name == "searxng" else fallback
        ),
    )
    monkeypatch.setattr(serper_tool, "_sdk_live_search_provider", lambda *args, **kwargs: hybrid)
    invalid = _company_payload(
        url=secondary_url,
        source_id=secondary_url,
    )
    invalid["sources"].append(
        {
            "source_id": valid_url,
            "provider_candidate_id": search_result_candidate_id(valid_url),
            "title": "Northstar evidence update",
            "url": valid_url,
            "source_type": "news",
            "supported_claims": ["Northstar published a measurement update."],
            "confidence": 0.8,
        }
    )
    invalid["claims"] = [
        {
            "claim_text": "Northstar published a measurement update.",
            "source_id": valid_url,
            "confidence": 0.7,
            "claim_type": "company_signal",
        }
    ]
    invalid["decision"]["candidate_assessments"].append(
        {
            "candidate_id": valid_url,
            "disposition": "excluded",
            "rationale": "The first decision incorrectly excluded this verified source.",
        }
    )
    valid = _company_payload(url=valid_url, source_id=valid_url)
    valid["sources"][0].pop("provider_candidate_id")
    valid["decision"]["selected_candidate_ids"] = [valid_url]
    valid["decision"]["candidate_assessments"][0]["candidate_id"] = valid_url
    valid["decision"]["candidate_assessments"].append(
        {
            "candidate_id": search_result_candidate_id(secondary_url),
            "disposition": "excluded",
            "rationale": "The overview is less current and less specific than the update.",
        }
    )
    valid["decision"]["candidate_assessments"].append(
        {
            "candidate_id": secondary_url,
            "disposition": "excluded",
            "rationale": "The overview is less direct than the current evidence update.",
        }
    )
    model = _FakeModel(
        [
            [
                _tool_call(
                    "search_web",
                    {
                        "query": "Northstar Behavioral measurement outcomes partnerships 2026",
                        "num_results": 4,
                    },
                    call_id="research-search",
                )
            ],
            [_structured_message(invalid)],
            [_structured_message(valid)],
        ]
    )
    result = run_business_research_analyst_sdk(
        BusinessResearchSDKInput(
            company_name="Northstar Behavioral",
            context=f"Current operator request (authoritative):\n{raw_request}",
        ),
        run_config=build_local_run_config(_FakeProvider(model)),
        manual_request_plan=_live_read_plan("business_research_analyst", raw_request),
        provider_retrieval_required=True,
    )

    assert raw_request in json.dumps(model.calls[0]["input"], default=str)
    assert fallback.queries == [
        "Northstar Behavioral measurement outcomes partnerships 2026"
    ]
    diagnostics = result.final_output.retrieval_diagnostics
    assert diagnostics["search_providers_attempted"] == [
        "searxng",
        "agents-web-search",
    ]
    assert diagnostics["provider_error_fallback_used"] is True
    assert result.request_cache["decision_ownership"]["repair_attempted"] is True
    assert result.request_cache["decision_ownership"]["validator_outcome"]["status"] == (
        "accepted"
    )
    assert [receipt["tool_name"] for receipt in result.tool_receipts] == ["search_web"]
    assert len(model.calls) == 3
    assert "search_web" not in model.calls[2]["tool_names"]
    assert result.final_output.sources[0].url == valid_url
    assert result.final_output.sources[0].provider_candidate_id == ""
    assert result.request_cache["decision_ownership"]["attempts"][1][
        "normalized_selected_candidate_ids"
    ] == [search_result_candidate_id(valid_url)]


def test_opportunity_model_searches_scores_ranks_and_chooses_research_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_request = (
        "Find one current behavioral-health measurement partnership that looks "
        "promising, and tell me what still needs checking."
    )
    selected_url = "https://northstar.example/payer-partnership"
    search_provider = _SuccessfulSearchProvider(
        [
            SearchResult(
                title="Northstar payer partnership",
                link=selected_url,
                snippet="Northstar announced a current payer measurement partnership.",
                source="searxng",
            ),
            SearchResult(
                title="Old generic digital-health directory",
                link="https://directory.example/old-list",
                snippet="A general directory without a current opportunity signal.",
                source="searxng",
            ),
        ]
    )
    hybrid = HybridSearchProvider(
        provider_sequence=("searxng",),
        autonomy_hint=RetrievalAutonomyHint(),
        quality_assessor=_quality,
        provider_factory=lambda name: search_provider,
    )
    monkeypatch.setattr(serper_tool, "_sdk_live_search_provider", lambda *args, **kwargs: hybrid)
    opportunity_payload = _opportunity_payload(url=selected_url)
    opportunity_payload["decision"]["candidate_assessments"].append(
        {
            "candidate_id": "https://directory.example/old-list",
            "disposition": "excluded",
            "rationale": "The directory is generic and does not show a current opportunity.",
        }
    )
    model = _FakeModel(
        [
            [
                _tool_call(
                    "search_web",
                    {
                        "query": "current payer behavioral health measurement partnership 2026",
                        "num_results": 4,
                    },
                    call_id="opportunity-search",
                )
            ],
            [
                _tool_call(
                    "score_opportunity",
                    {
                        "company_name": "Northstar Behavioral",
                        "opportunity_type": "digital mental health",
                        "signals": ["payer partnership", "measurement evidence"],
                    },
                    call_id="opportunity-score",
                )
            ],
            [_structured_message(opportunity_payload)],
        ]
    )
    result = run_opportunity_scout_sdk(
        OpportunityScoutSDKInput(
            topic=raw_request,
            max_results=1,
            context=f"Current operator request (authoritative):\n{raw_request}",
        ),
        run_config=build_local_run_config(_FakeProvider(model)),
        manual_request_plan=_live_read_plan("opportunity_scout", raw_request),
        provider_retrieval_required=True,
    )

    assert raw_request in json.dumps(model.calls[0]["input"], default=str)
    assert search_provider.queries == [
        "current payer behavioral health measurement partnership 2026"
    ]
    assert [receipt["tool_name"] for receipt in result.tool_receipts] == ["search_web"]
    assert result.request_cache["tool_execution"]["model_called_tool_names"] == [
        "search_web",
        "score_opportunity",
    ]
    assert result.request_cache["tool_execution_postcondition"]["satisfied"] is True
    assert result.request_cache["decision_ownership"]["validator_outcome"]["status"] == (
        "accepted"
    )
    normalization = result.request_cache["decision_normalizations"][0]
    assert normalization["normalizer"] == "score_opportunity"
    assert normalization["semantic_selection_changed"] is False
    record = result.final_output.records[0]
    assert record.canonical_entity_key == "northstar-behavioral"
    assert record.handoff_to_business_research_analyst is True
    assert "buyer" in record.handoff_reason.lower()
    assert record.research_needed
    assert result.final_output.outreach_generated is False


def test_opportunity_repair_uses_frozen_tool_candidate_universe_and_url_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_request = (
        "Compare the open opportunities you find, choose the strongest current fit, "
        "and explain why the alternatives lose."
    )
    initial_url = "https://grants.example/current-behavioral-health"
    selected_url = "https://nih.example/current-digital-health-topic"
    excluded_url = "https://conference.example/expired-call"
    results = [
        SearchResult(
            title="Current behavioral-health grant",
            link=initial_url,
            snippet="Open grant with a current deadline.",
            source="searxng",
        ),
        SearchResult(
            title="Current digital-health topic",
            link=selected_url,
            snippet="Open NIH topic covering digital health and behavioral research.",
            source="agents-web-search",
        ),
        SearchResult(
            title="Expired conference call",
            link=excluded_url,
            snippet="The abstract deadline has passed.",
            source="searxng",
        ),
    ]
    search_provider = _SuccessfulSearchProvider(results)
    hybrid = HybridSearchProvider(
        provider_sequence=("searxng",),
        autonomy_hint=RetrievalAutonomyHint(),
        quality_assessor=_quality,
        provider_factory=lambda _name: search_provider,
    )
    monkeypatch.setattr(
        serper_tool,
        "_sdk_live_search_provider",
        lambda *args, **kwargs: hybrid,
    )

    initial_id = search_result_candidate_id(initial_url)
    selected_id = search_result_candidate_id(selected_url)
    excluded_id = search_result_candidate_id(excluded_url)
    initial = _opportunity_payload(url=initial_url)
    initial["review_candidates"] = [
        {
            "company_name": "NIH digital health topic",
            "source_url": selected_url,
            "reasons": ["Strong topical fit; compare before final selection."],
        }
    ]
    initial["filtered_candidates"] = [
        {
            "company_name": "Expired conference call",
            "source_url": excluded_url,
            "reasons": ["Deadline passed."],
        }
    ]
    initial["decision"] = {
        "decision_owner": "specialist_agent",
        "decision_stage": "opportunity_candidate_selection",
        "selected_candidate_ids": [initial_id],
        "candidate_assessments": [
            {
                "candidate_id": initial_id,
                "disposition": "selected",
                "rationale": "Initially appeared most actionable.",
            },
            {
                "candidate_id": selected_id,
                "disposition": "plausible",
                "rationale": "Stronger fit may justify changing the selection.",
            },
            {
                "candidate_id": excluded_id,
                "disposition": "excluded",
                "rationale": "Deadline passed.",
            },
        ],
        "reasoning": "Compared all three provider candidates.",
        "limitations": ["Official eligibility still needs confirmation."],
        "needs_more_context": False,
    }

    repaired = _opportunity_payload(url=selected_url)
    repaired["review_candidates"] = [
        {
            "company_name": "Current behavioral-health grant",
            "source_url": initial_url,
            "reasons": ["Weaker fit than the selected topic."],
        }
    ]
    repaired["filtered_candidates"] = [
        {
            "company_name": "Expired conference call",
            "source_url": excluded_url,
            "reasons": ["Deadline passed."],
        }
    ]
    repaired["decision"] = {
        "decision_owner": "specialist_agent",
        "decision_stage": "opportunity_candidate_selection",
        "selected_candidate_ids": [selected_id],
        "candidate_assessments": [
            {
                "candidate_id": selected_id,
                "disposition": "selected",
                "rationale": "Best current evidence and KNI fit.",
            },
            {
                "candidate_id": selected_url,
                "disposition": "excluded",
                "rationale": (
                    "This URL is evidence for the selected canonical ID, "
                    "not a second candidate."
                ),
            },
            {
                "candidate_id": initial_url,
                "disposition": "excluded",
                "rationale": "Current but a weaker fit.",
            },
            {
                "candidate_id": excluded_url,
                "disposition": "excluded",
                "rationale": "Deadline passed.",
            },
        ],
        "reasoning": "Reconsidered the same three provider candidates and changed the winner.",
        "limitations": ["Official eligibility still needs confirmation."],
        "needs_more_context": False,
    }

    model = _FakeModel(
        [
            [
                _tool_call(
                    "search_web",
                    {"query": "current behavioral health AI opportunities", "num_results": 5},
                    call_id="universe-search",
                )
            ],
            [
                _tool_call(
                    "score_opportunity",
                    {
                        "company_name": "Northstar Behavioral",
                        "opportunity_type": "digital mental health",
                        "signals": ["payer partnership", "measurement evidence"],
                    },
                    call_id="universe-score",
                )
            ],
            [_structured_message(initial)],
            [_structured_message(repaired)],
        ]
    )

    result = run_opportunity_scout_sdk(
        OpportunityScoutSDKInput(
            topic=raw_request,
            max_results=1,
            context=f"Current operator request (authoritative):\n{raw_request}",
        ),
        run_config=build_local_run_config(_FakeProvider(model)),
        manual_request_plan=_live_read_plan("opportunity_scout", raw_request),
        provider_retrieval_required=True,
    )

    assert search_provider.queries == ["current behavioral health AI opportunities"]
    assert len(model.calls) == 4
    assert model.calls[3]["tool_names"] == []
    repair_input = json.dumps(model.calls[3]["input"], default=str)
    assert all(
        candidate_id in repair_input
        for candidate_id in (initial_id, selected_id, excluded_id)
    )
    assert "source URLs are evidence aliases" in repair_input
    ownership = result.request_cache["decision_ownership"]
    assert ownership["validator_outcome"]["status"] == "accepted"
    assert ownership["attempts"][0]["candidate_universe_source"] == "model_tool_outputs"
    assert ownership["attempts"][0]["candidate_universe_fingerprint"] == ownership[
        "attempts"
    ][1]["candidate_universe_fingerprint"]
    assert ownership["attempts"][1]["normalized_selected_candidate_ids"] == [selected_id]
    assert set(ownership["candidate_ids"]) == {initial_id, selected_id, excluded_id}
    assert result.request_cache["decision_repair_evidence"][
        "provider_calls_during_repair"
    ] == 0
    assert result.final_output.decision.selected_candidate_ids == [selected_id]


def test_opportunity_repair_preserves_fifteen_tool_candidates_without_losing_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls = [f"https://opportunity-{index}.example/open-call" for index in range(15)]
    results = [
        SearchResult(
            title=f"Open opportunity {index}",
            link=url,
            snippet=(f"Candidate {index} has a current deadline and eligibility details. " * 90),
            source="searxng",
        )
        for index, url in enumerate(urls)
    ]
    search_provider = _SuccessfulSearchProvider(results)
    hybrid = HybridSearchProvider(
        provider_sequence=("searxng",),
        autonomy_hint=RetrievalAutonomyHint(),
        quality_assessor=_quality,
        provider_factory=lambda _name: search_provider,
    )
    monkeypatch.setattr(
        serper_tool,
        "_sdk_live_search_provider",
        lambda *args, **kwargs: hybrid,
    )

    selected_url = urls[7]
    selected_id = search_result_candidate_id(selected_url)
    candidate_ids = [search_result_candidate_id(url) for url in urls]

    invalid = _opportunity_payload(url=selected_url)
    invalid["review_candidates"] = [
        {
            "company_name": f"Alternative {index}",
            "source_url": url,
            "reasons": ["Compare deadline, eligibility, and fit."],
        }
        for index, url in enumerate(urls)
        if url != selected_url
    ]
    invalid["decision"] = {
        "decision_owner": "specialist_agent",
        "decision_stage": "opportunity_candidate_selection",
        "selected_candidate_ids": ["fabricated-candidate"],
        "candidate_assessments": [
            {
                "candidate_id": "fabricated-candidate",
                "disposition": "selected",
                "rationale": "This identity was not returned by the provider.",
            },
            *[
                {
                    "candidate_id": candidate_id,
                    "disposition": "excluded",
                    "rationale": "Not selected in the invalid first decision.",
                }
                for candidate_id in candidate_ids
            ],
        ],
        "reasoning": "The first decision used an unsupported identity.",
        "limitations": ["Eligibility still needs primary-source confirmation."],
        "needs_more_context": False,
    }

    repaired = _opportunity_payload(url=selected_url)
    repaired["review_candidates"] = [
        {
            "company_name": f"Alternative {index}",
            "source_url": url,
            "reasons": ["Lower fit than the selected opportunity."],
        }
        for index, url in enumerate(urls)
        if url != selected_url
    ]
    repaired["decision"] = {
        "decision_owner": "specialist_agent",
        "decision_stage": "opportunity_candidate_selection",
        "selected_candidate_ids": [selected_id],
        "candidate_assessments": [
            {
                "candidate_id": candidate_id,
                "disposition": "selected" if candidate_id == selected_id else "excluded",
                "rationale": (
                    "Best supported current fit."
                    if candidate_id == selected_id
                    else "Lower fit after comparing the bounded evidence."
                ),
            }
            for candidate_id in candidate_ids
        ],
        "reasoning": "Compared the full frozen provider candidate universe.",
        "limitations": ["Eligibility still needs primary-source confirmation."],
        "needs_more_context": False,
    }

    model = _FakeModel(
        [
            [
                _tool_call(
                    "search_web",
                    {"query": "current behavioral health opportunity", "num_results": 20},
                    call_id="large-universe-search",
                )
            ],
            [
                _tool_call(
                    "score_opportunity",
                    {
                        "company_name": "Northstar Behavioral",
                        "opportunity_type": "digital mental health",
                        "signals": ["payer partnership", "measurement evidence"],
                    },
                    call_id="large-universe-score",
                )
            ],
            [_structured_message(invalid)],
            [_structured_message(repaired)],
        ]
    )

    result = run_opportunity_scout_sdk(
        OpportunityScoutSDKInput(
            topic="Find and compare one current behavioral-health opportunity.",
            max_results=1,
            context="Read-only natural-language opportunity comparison.",
        ),
        run_config=build_local_run_config(_FakeProvider(model)),
        manual_request_plan=_live_read_plan(
            "opportunity_scout",
            "Find and compare one current behavioral-health opportunity.",
        ),
        provider_retrieval_required=True,
    )

    ownership = result.request_cache["decision_ownership"]
    replay = result.request_cache["decision_repair_evidence"]
    assert ownership["validator_outcome"]["status"] == "accepted"
    assert ownership["candidate_ids"] == candidate_ids
    assert ownership["attempts"][0]["candidate_universe_fingerprint"] == ownership[
        "attempts"
    ][1]["candidate_universe_fingerprint"]
    assert ownership["attempts"][1]["normalized_selected_candidate_ids"] == [selected_id]
    assert replay["status"] == "ready"
    assert replay["replayed_output_count"] == 1
    assert replay["candidate_record_count"] == 15
    assert replay["provider_calls_during_repair"] == 0
    assert model.calls[3]["tool_names"] == []


def test_opportunity_tool_correction_and_decision_repair_fit_bounded_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_request = (
        "Find one current accelerator or pilot that a small neuroinformatics "
        "consultancy could apply to, and explain the biggest caveat."
    )
    selected_url = "https://northstar.example/current-pilot"
    search_provider = _SuccessfulSearchProvider(
        [
            SearchResult(
                title="Northstar current pilot",
                link=selected_url,
                snippet="A current pilot with an official small-business eligibility page.",
                source="searxng",
            )
        ]
    )
    hybrid = HybridSearchProvider(
        provider_sequence=("searxng",),
        autonomy_hint=RetrievalAutonomyHint(),
        quality_assessor=_quality,
        provider_factory=lambda _name: search_provider,
    )
    monkeypatch.setattr(serper_tool, "_sdk_live_search_provider", lambda *args, **kwargs: hybrid)

    initial_without_score = _opportunity_payload(url=selected_url)
    invalid_decision = _opportunity_payload(url=selected_url)
    invalid_decision["decision"] = {
        "decision_owner": "specialist_agent",
        "decision_stage": "opportunity_candidate_selection",
        "selected_candidate_ids": ["fabricated-candidate"],
        "candidate_assessments": [
            {
                "candidate_id": "fabricated-candidate",
                "disposition": "selected",
                "rationale": "This identity was not returned by the provider.",
            }
        ],
        "reasoning": "Selected an unsupported identity.",
        "limitations": [],
        "needs_more_context": False,
    }
    valid_decision = _opportunity_payload(url=selected_url)
    model = _FakeModel(
        [
            [
                _tool_call(
                    "search_web",
                    {
                        "query": "current neuroinformatics small business pilot",
                        "num_results": 5,
                    },
                    call_id="bounded-search",
                )
            ],
            [_structured_message(initial_without_score)],
            [
                _tool_call(
                    "score_opportunity",
                    {
                        "company_name": "Northstar Behavioral",
                        "opportunity_type": "digital mental health",
                        "signals": ["payer partnership", "measurement evidence"],
                    },
                    call_id="bounded-score",
                )
            ],
            [_structured_message(invalid_decision)],
            [_structured_message(valid_decision)],
        ]
    )

    result = run_opportunity_scout_sdk(
        OpportunityScoutSDKInput(
            topic=raw_request,
            max_results=1,
            context=f"Current operator request (authoritative):\n{raw_request}",
        ),
        run_config=build_local_run_config(_FakeProvider(model)),
        manual_request_plan=_live_read_plan("opportunity_scout", raw_request),
        provider_retrieval_required=True,
        compact_instructions=True,
    )

    assert len(model.calls) == 5
    assert search_provider.queries == ["current neuroinformatics small business pilot"]
    assert "search_web" not in model.calls[2]["tool_names"]
    assert "search_web" not in model.calls[4]["tool_names"]
    assert "score_opportunity" not in model.calls[4]["tool_names"]
    assert result.request_cache["semantic_attempt_turn_limits"] == {
        "schema": "keystone.semantic_attempt_turn_limits.v1",
        "initial": 8,
        "tool_correction": 2,
        "decision_repair": 1,
    }
    assert result.request_cache["tool_execution_correction"]["attempted"] is True
    assert result.request_cache["decision_ownership"]["attempt_count"] == 2
    assert result.request_cache["decision_ownership"]["validator_outcome"]["status"] == (
        "accepted"
    )
    assert result.request_cache["decision_repair_evidence"]["provider_calls_during_repair"] == 0
