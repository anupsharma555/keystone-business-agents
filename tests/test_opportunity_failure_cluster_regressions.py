"""Offline regressions for the August 2026 Opportunity Slack failure cluster."""

from __future__ import annotations

import json
import subprocess
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

import keystone_agents.tools.serper_tool as serper_tool
from keystone_agents.agents.opportunity_scout import run_opportunity_scout_sdk
from keystone_agents.entrypoints import cli_impl as cli
from keystone_agents.models import OpportunityScoutSDKInput
from keystone_agents.operator_failures import known_exception_to_operator_failure
from keystone_agents.planning.compatibility import infer_manual_request_plan
from keystone_agents.quality_budget import QualityMode, opportunity_scout_quality_budget
from keystone_agents.receipts.normalization import identity_fingerprints
from keystone_agents.retrieval_policy import (
    HybridSearchProvider,
    RetrievalAutonomyHint,
    RetrievalQualityAssessment,
)
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.runtime.decision_validation import (
    AgentDecisionContract,
    SpecialistDecisionEvidence,
    decision_validation_telemetry,
    validate_specialist_decision,
)
from keystone_agents.runtime.execution_deadline import (
    EXECUTION_DEADLINE_SCHEMA,
    ExecutionDeadlineExceeded,
    ExecutionDeadlineLedger,
)
from keystone_agents.runtime.tool_execution import ToolEvidenceGroup, ToolExecutionContract
from keystone_agents.schemas.decision_ownership import AgentDecisionRecord
from keystone_agents.schemas.manual_request_plan import AskShapePolicy, ManualRequestPlan
from keystone_agents.sdk import build_local_run_config, build_sdk_agent, function_tool
from keystone_agents.sdk_run_policy import (
    resolve_sdk_tool_call_limit,
    resolve_sdk_turn_policy,
)
from keystone_agents.tools.search_provider import SearchResult, search_result_candidate_id

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


OPPORTUNITY_FAILURE_PROMPTS = (
    (
        "Opportunity Scout, could you find one currently open U.S. opportunity-a grant, "
        "RFP, pilot partnership, or conference call-that a small behavioral-health AI "
        "research consultancy could realistically pursue in the next 90 days? Choose the "
        "best-supported option, explain why it fits KNI, include the deadline and official "
        "link, and flag one eligibility caveat. Keep this read-only."
    ),
    (
        "Opportunity Scout, I'm looking for one live U.S. non-dilutive funding or pilot "
        "opening that a small behavioral-health data and AI consultancy could plausibly "
        "act on before early November. Compare the strongest current options you can "
        "verify, choose only one, and give me its deadline, why it fits Keystone, one "
        "eligibility concern, and the official URL. Keep this read-only-don't save it, "
        "hand it off, or start outreach."
    ),
    (
        "Opportunity Scout, I need one practical U.S. opportunity KNI could act on this "
        "quarter. Please compare currently open grants, RFPs, pilot calls, and relevant "
        "conference opportunities, then return only the strongest one with why it fits "
        "our behavioral-health AI research work, its verified deadline, the official "
        "source, and the most important eligibility risk. Keep this read-only; don't save, "
        "apply, contact, draft, or post anything."
    ),
    (
        "Opportunity Scout, could you help me find one currently open U.S. accelerator "
        "cohort, grant, or pilot program that a small neuroinformatics consultancy could "
        "realistically apply to before November 1? Weigh timing, eligibility, evidence "
        "quality, and Keystone fit, pick the best one, and include the official link plus "
        "the biggest caveat. Just research it-don't save, draft, contact, or change anything."
    ),
    (
        "Opportunity Scout, I'm looking for one open U.S. opportunity KNI could "
        "realistically pursue before the end of October. Please compare grants, RFPs, "
        "pilot invitations, and conference calls tied to behavioral-health data, clinical "
        "AI evaluation, or measurement-based care. Pick only the strongest option, explain "
        "why it beats the alternatives, give the official deadline and key eligibility "
        "constraint, and link the primary source. Keep this read-only-don't save, apply, "
        "contact, or draft anything."
    ),
    (
        "Opportunity Scout, help me find the most actionable open opportunity KNI could "
        "pursue between now and Halloween. It can be a grant, procurement notice, pilot or "
        "accelerator call, or conference submission, but it needs to be open now and "
        "plausibly fit a small U.S. behavioral-health AI research consultancy. Compare what "
        "you find, then give me one winner with the official deadline, who can apply, why "
        "KNI fits, one important risk, and the primary-source link. Keep this read-only-"
        "don't save, apply, contact, or draft anything."
    ),
    (
        "Opportunity Scout, I'm trying to decide whether there is a realistic near-term "
        "federal route for a small behavioral-health AI consultancy. Please compare the "
        "current open NIH or other U.S. federal small-business opportunities you find, "
        "choose the one KNI could most plausibly pursue in the next 90 days, and give me "
        "the deadline, official source, fit rationale, and one eligibility concern. Keep "
        "this read-only."
    ),
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
                "input": input,
                "tool_names": [tool.name for tool in tools],
                "system_instructions": system_instructions,
            }
        )
        return ModelResponse(
            output=self.outputs.pop(0),
            usage=Usage(requests=1),
            response_id=f"opportunity-failure-cluster-{len(self.calls)}",
        )

    def stream_response(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        raise NotImplementedError


class _FakeProvider(ModelProvider):
    def __init__(self, model: _FakeModel) -> None:
        self.model = model

    def get_model(self, _model_name: str | None) -> Model:
        return self.model


class _SearchProvider:
    def __init__(self, results: list[SearchResult]) -> None:
        self.results = results
        self.queries: list[str] = []

    def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
        self.queries.append(query)
        return self.results[:num_results]


class _DecisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_ids: list[str] = Field(max_length=10)
    selected_candidate_id: str
    summary: str
    decision: AgentDecisionRecord


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
        id="opportunity-failure-cluster-output",
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


def _model_input_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)


def _live_opportunity_plan(request: str) -> ManualRequestPlan:
    return ManualRequestPlan(
        source="failure_cluster_regression",
        requested_agent="opportunity_scout",
        target_agent="opportunity_scout",
        intent="opportunity_search",
        primary_target="current U.S. opportunity",
        target_type="opportunity",
        provider_system="unspecified",
        provider_operations=["search", "read"],
        objective=request,
        task_objective="opportunity_discovery",
        expected_artifact_type="opportunity_record",
        ask_shape=AskShapePolicy(permission_state="read_only"),
        requires_live_search=True,
    )


def _quality(results: list[Any], request_text: str) -> RetrievalQualityAssessment:
    del request_text
    return RetrievalQualityAssessment(
        result_count=len(results),
        unique_domain_count=len(results),
        duplicate_ratio=0.0,
        primary_source_count=len(results),
        official_source_present=bool(results),
        linkedin_source_present=False,
        recent_signal_count=len(results),
        needs_precision_search=False,
        needs_structured_enrichment=False,
        needs_search_review=False,
        reasons=(),
    )


def _opportunity_payload(url: str) -> dict[str, Any]:
    candidate_id = search_result_candidate_id(url)
    return {
        "topic": "current U.S. behavioral-health AI opportunities",
        "dry_run": False,
        "records": [
            {
                "company_name": "Synthetic Federal Program",
                "canonical_entity_key": "synthetic-federal-program",
                "opportunity_type": "grant or collaboration opportunity",
                "priority_score": 76,
                "why_now_signal": "The official call is open with a current deadline.",
                "recommended_next_step": "Verify the small-business eligibility clause.",
                "sources": [
                    {
                        "source_id": "source:synthetic-federal-program",
                        "title": "Synthetic federal opportunity notice",
                        "url": url,
                        "source_type": "government",
                        "supported_signal": "Current official small-business call.",
                    }
                ],
                "source_signals": ["current deadline", "small-business eligibility"],
                "keystone_fit_reason": "The call includes behavioral-health AI evaluation.",
                "outside_consulting_likelihood": 65,
                "handoff_to_business_research_analyst": False,
                "handoff_reason": "The official notice is sufficient for initial screening.",
                "business_research_analyst_handoff_recommendation": "",
                "research_needed": ["Confirm the exact applicant entity requirement."],
                "outreach_draft": None,
                "approval_required_before_outreach": True,
            }
        ],
        "audit_notes": ["Offline failure-cluster regression."],
        "outreach_generated": False,
        "decision": {
            "decision_owner": "specialist_agent",
            "decision_stage": "opportunity_candidate_selection",
            "selected_candidate_ids": [candidate_id],
            "candidate_assessments": [
                {
                    "candidate_id": candidate_id,
                    "disposition": "selected",
                    "rationale": "It is open, official, and the strongest plausible fit.",
                }
            ],
            "reasoning": "Compared current fit, eligibility, deadline, and source quality.",
            "limitations": ["Applicant eligibility still needs final verification."],
            "needs_more_context": False,
        },
    }


def _decision_payload(selected_id: str) -> dict[str, Any]:
    candidate_ids = ["candidate-current", "candidate-expired"]
    assessed_ids = list(dict.fromkeys([*candidate_ids, selected_id]))
    return {
        "candidate_ids": candidate_ids,
        "selected_candidate_id": selected_id,
        "summary": "Selected the strongest supported current opportunity.",
        "decision": {
            "decision_owner": "specialist_agent",
            "decision_stage": "opportunity_candidate_selection",
            "selected_candidate_ids": [selected_id],
            "candidate_assessments": [
                {
                    "candidate_id": candidate_id,
                    "disposition": "selected" if candidate_id == selected_id else "excluded",
                    "rationale": (
                        "Open with a verified deadline and eligibility evidence."
                        if candidate_id == selected_id
                        else "Not the best supported current option."
                    ),
                }
                for candidate_id in assessed_ids
            ],
            "reasoning": "Compared timing, eligibility, evidence quality, and fit.",
            "limitations": ["Offline synthetic evidence only."],
            "needs_more_context": False,
        },
    }


@pytest.mark.parametrize("prompt_text", OPPORTUNITY_FAILURE_PROMPTS)
def test_explicit_opportunity_ownership_never_reroutes_to_business_research(
    prompt_text: str,
) -> None:
    mention = cli.parse_agent_mention(prompt_text, allow_bare_agent_aliases=True)
    plan = infer_manual_request_plan(
        mention.input_text,
        requested_agent=mention.route,
    )

    assert mention.explicit is True
    assert mention.route == "opportunity_scout"
    assert plan.requested_agent == "opportunity_scout"
    assert plan.target_agent == "opportunity_scout"
    assert "business_research_analyst" not in plan.workflow


@pytest.mark.parametrize("prompt_text", OPPORTUNITY_FAILURE_PROMPTS)
def test_comparative_live_opportunity_research_has_at_least_balanced_turns(
    prompt_text: str,
) -> None:
    plan = _live_opportunity_plan(prompt_text)
    budget = opportunity_scout_quality_budget(
        request_text=prompt_text,
        live_search=True,
        manual_request_plan=plan,
    )
    policy = resolve_sdk_turn_policy(
        "opportunity_scout",
        request_text=prompt_text,
        live_search=True,
        manual_request_plan=plan,
    )

    assert budget.mode in {QualityMode.BALANCED, QualityMode.DEEP}
    assert policy.max_turns >= 8
    assert resolve_sdk_tool_call_limit(
        "opportunity_scout",
        request_text=prompt_text,
        live_search=True,
        manual_request_plan=plan,
    ) == budget.max_tool_calls
    assert policy.source in {"quality_budget:balanced", "quality_budget:deep"}


@pytest.mark.parametrize("prompt_text", OPPORTUNITY_FAILURE_PROMPTS)
def test_request_estimate_reserves_initial_tool_correction_and_decision_repair(
    prompt_text: str,
) -> None:
    plan = _live_opportunity_plan(prompt_text)
    estimate = cli._estimate_ask_openai_requests(
        SimpleNamespace(
            agent="opportunity_scout",
            context_file="",
            live_search=True,
            max_manager_steps=3,
        ),
        input_text=prompt_text,
        live_sdk=True,
        live_manual_plan=False,
        requested_route="opportunity_scout",
        manual_plan=plan,
        effective_live_search=True,
        observed_orchestrator_requests=1,
    )
    route_rows = [
        row for row in estimate["stage_rows"] if row["route"] == "opportunity_scout"
    ]

    assert [row["stage"] for row in route_rows] == [
        "opportunity_scout_direct_sdk",
        "conditional_opportunity_scout_tool_correction",
        "conditional_opportunity_scout_decision_repair",
    ]
    assert [row["admission_reserve_requests"] for row in route_rows] == [4, 2, 1]
    assert cli._post_preflight_admission_reserve(estimate) == 7


def test_cumulative_replay_compacts_noise_and_keeps_descriptive_candidates() -> None:
    reads = {"count": 0}

    @function_tool
    def search_opportunity_candidates(query: str) -> str:
        """Return a bounded synthetic provider candidate universe."""

        reads["count"] += 1
        noise = [
            {
                "candidate_id": f"noise-{index}",
                "title": f"Unrelated result {index}",
                "snippet": "Repeated unrelated diagnostic context. " * 100,
                "url": f"https://example.test/noise/{index}",
            }
            for index in range(100)
        ]
        return json.dumps(
            {
                "status": "success",
                "operation": "search",
                "provider_read": True,
                "identity_fingerprints": identity_fingerprints(
                    ["candidate-current", "candidate-expired"]
                ),
                "candidates": [
                    *noise,
                    {
                        "candidate_id": "candidate-current",
                        "title": "Current behavioral-health AI grant",
                        "deadline": "2026-10-30",
                        "eligibility": "Small U.S. research consultancies may apply.",
                        "snippet": "Supports behavioral-health AI evaluation.",
                    },
                    {
                        "candidate_id": "candidate-expired",
                        "title": "Expired behavioral-health conference call",
                        "deadline": "2026-07-01",
                        "eligibility": "Deadline has passed.",
                        "snippet": "No longer actionable.",
                    },
                ],
                "provider_diagnostics": ["Verbose diagnostic. " * 100] * 25,
                "access_token": "must-not-be-replayed",
            },
            sort_keys=True,
        )

    def evidence(output: _DecisionResult) -> SpecialistDecisionEvidence:
        return SpecialistDecisionEvidence.build(
            output.candidate_ids,
            required_selected_ids=[output.selected_candidate_id],
            selection_required=True,
            exact_required_selection=True,
            provider_identities_by_candidate={
                candidate_id: [candidate_id] for candidate_id in output.candidate_ids
            },
        )

    model = _FakeModel(
        [
            [
                _tool_call(
                    "search_opportunity_candidates",
                    {"query": "current behavioral-health AI opportunity"},
                    call_id="failure-cluster-search",
                )
            ],
            [_structured_message(_decision_payload("fabricated-candidate"))],
            [_structured_message(_decision_payload("candidate-current"))],
        ]
    )
    agent = build_sdk_agent(
        name="synthetic_opportunity_specialist",
        instructions="Search, compare, and select one supported opportunity.",
        output_type=_DecisionResult,
        tools=[search_opportunity_candidates],
        enforce_tool_policy=False,
    )
    result = run_typed_sdk_agent(
        agent=agent,
        typed_input=OPPORTUNITY_FAILURE_PROMPTS[0],
        output_type=_DecisionResult,
        run_config=build_local_run_config(_FakeProvider(model)),
        max_turns=4,
        tool_execution_contract=ToolExecutionContract.required(
            ToolEvidenceGroup(
                "provider_candidates",
                ("search_opportunity_candidates",),
            ),
            stage="opportunity_candidate_selection",
        ),
        decision_contract=AgentDecisionContract(
            route="synthetic_opportunity_specialist",
            decision_stage="opportunity_candidate_selection",
            evidence_resolver=evidence,
        ),
    )

    replay = result.request_cache["decision_repair_evidence"]
    repair_input = _model_input_text(model.calls[2]["input"])
    assert reads["count"] == 1
    assert replay["compaction_applied"] is True
    assert replay["provider_calls_during_repair"] == 0
    assert replay["candidate_record_count"] >= 2
    assert "Current behavioral-health AI grant" in repair_input
    assert "Expired behavioral-health conference call" in repair_input
    assert "must-not-be-replayed" not in repair_input
    assert model.calls[2]["tool_names"] == []


def test_tool_correction_then_decision_repair_reuses_opportunity_provider_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = OPPORTUNITY_FAILURE_PROMPTS[-1]
    url = "https://grants.example.test/current-behavioral-health-ai"
    search_provider = _SearchProvider(
        [
            SearchResult(
                title="Current behavioral-health AI grant",
                link=url,
                snippet="Official open call with a current deadline.",
                source="synthetic-search",
            )
        ]
    )
    hybrid = HybridSearchProvider(
        provider_sequence=("synthetic-search",),
        autonomy_hint=RetrievalAutonomyHint(),
        quality_assessor=_quality,
        provider_factory=lambda _name: search_provider,
    )
    monkeypatch.setattr(
        serper_tool,
        "_sdk_live_search_provider",
        lambda *args, **kwargs: hybrid,
    )

    initial_without_score = _opportunity_payload(url)
    invalid_decision = _opportunity_payload(url)
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
    model = _FakeModel(
        [
            [
                _tool_call(
                    "search_web",
                    {"query": "current federal behavioral health AI grants", "num_results": 5},
                    call_id="opportunity-provider-read",
                )
            ],
            [_structured_message(initial_without_score)],
            [
                _tool_call(
                    "score_opportunity",
                    {
                        "company_name": "Synthetic Federal Program",
                        "opportunity_type": "grant or collaboration opportunity",
                        "signals": ["current deadline", "small-business eligibility"],
                    },
                    call_id="opportunity-score",
                )
            ],
            [_structured_message(invalid_decision)],
            [_structured_message(_opportunity_payload(url))],
        ]
    )

    result = run_opportunity_scout_sdk(
        typed_input=OpportunityScoutSDKInput(
            topic=request,
            max_results=1,
            context=f"Current operator request (authoritative):\n{request}",
        ),
        run_config=build_local_run_config(_FakeProvider(model)),
        manual_request_plan=_live_opportunity_plan(request),
        provider_retrieval_required=True,
        compact_instructions=True,
    )

    assert search_provider.queries == ["current federal behavioral health AI grants"]
    assert len(model.calls) == 5
    assert "search_web" not in model.calls[2]["tool_names"]
    assert model.calls[4]["tool_names"] == []
    assert result.request_cache["tool_execution_correction"]["attempted"] is True
    assert result.request_cache["decision_ownership"]["attempt_count"] == 2
    assert result.request_cache["decision_repair_evidence"][
        "provider_calls_during_repair"
    ] == 0
    assert result.request_cache["semantic_attempt_turn_limits"] == {
        "schema": "keystone.semantic_attempt_turn_limits.v1",
        "initial": 8,
        "tool_correction": 2,
        "decision_repair": 1,
    }


@pytest.mark.parametrize("prompt_text", OPPORTUNITY_FAILURE_PROMPTS)
def test_provider_candidates_cannot_be_discarded_as_false_no_context(
    monkeypatch: pytest.MonkeyPatch,
    prompt_text: str,
) -> None:
    request = prompt_text
    url = "https://grants.example.test/current-behavioral-health-ai"
    search_provider = _SearchProvider(
        [
            SearchResult(
                title="Current behavioral-health AI grant",
                link=url,
                snippet="Official open call with a current deadline.",
                source="synthetic-search",
            )
        ]
    )
    hybrid = HybridSearchProvider(
        provider_sequence=("synthetic-search",),
        autonomy_hint=RetrievalAutonomyHint(),
        quality_assessor=_quality,
        provider_factory=lambda _name: search_provider,
    )
    monkeypatch.setattr(
        serper_tool,
        "_sdk_live_search_provider",
        lambda *args, **kwargs: hybrid,
    )

    false_no_context = _opportunity_payload(url)
    false_no_context["records"] = []
    false_no_context["decision"] = {
        "decision_owner": "specialist_agent",
        "decision_stage": "opportunity_candidate_selection",
        "selected_candidate_ids": [],
        "candidate_assessments": [],
        "reasoning": "No usable live opportunity context was available.",
        "limitations": ["Unable to compare a bounded candidate set."],
        "needs_more_context": True,
    }
    model = _FakeModel(
        [
            [
                _tool_call(
                    "search_web",
                    {"query": "current behavioral health AI opportunities", "num_results": 5},
                    call_id="opportunity-provider-read",
                )
            ],
            [
                _tool_call(
                    "score_opportunity",
                    {
                        "company_name": "Synthetic Federal Program",
                        "opportunity_type": "grant or collaboration opportunity",
                        "signals": ["current deadline", "small-business eligibility"],
                    },
                    call_id="opportunity-score",
                )
            ],
            [_structured_message(false_no_context)],
            [_structured_message(_opportunity_payload(url))],
        ]
    )

    result = run_opportunity_scout_sdk(
        typed_input=OpportunityScoutSDKInput(
            topic=request,
            max_results=1,
            context=f"Current operator request (authoritative):\n{request}",
        ),
        run_config=build_local_run_config(_FakeProvider(model)),
        manual_request_plan=_live_opportunity_plan(request),
        provider_retrieval_required=True,
        compact_instructions=True,
    )

    ownership = result.request_cache["decision_ownership"]
    assert search_provider.queries == ["current behavioral health AI opportunities"]
    assert len(model.calls) == 4
    assert model.calls[3]["tool_names"] == []
    assert ownership["attempt_count"] == 2
    assert ownership["attempts"][0]["validator_outcome"]["reason_code"] == (
        "candidate_assessments_incomplete"
    )
    assert ownership["attempts"][1]["validator_outcome"]["status"] == "accepted"
    assert result.request_cache["decision_repair_evidence"][
        "provider_calls_during_repair"
    ] == 0


def test_provider_url_alias_validates_as_its_canonical_candidate() -> None:
    url = "https://grants.example.test/current-behavioral-health-ai"
    candidate_id = search_result_candidate_id(url)
    evidence = SpecialistDecisionEvidence.build(
        (candidate_id,),
        required_selected_ids=(candidate_id,),
        selection_required=True,
        exact_required_selection=True,
        provider_identities_by_candidate={candidate_id: (url,)},
    )
    output = SimpleNamespace(
        decision=AgentDecisionRecord(
            decision_stage="opportunity_candidate_selection",
            selected_candidate_id=url,
            candidate_assessments=[
                {
                    "candidate_id": url,
                    "disposition": "selected",
                    "rationale": "The official URL identifies the verified provider candidate.",
                }
            ],
            reasoning="Selected the official, current, source-backed opportunity.",
        ),
        model_fields_set={"decision"},
    )
    contract = AgentDecisionContract(
        route="opportunity_scout",
        decision_stage="opportunity_candidate_selection",
        evidence_resolver=lambda _output: evidence,
    )

    outcome, _ = validate_specialist_decision(
        output,
        contract,
        verified_candidate_fingerprints=identity_fingerprints((url,)),
    )
    telemetry = decision_validation_telemetry(
        output,
        contract,
        evidence,
        outcome,
        attempt=1,
    )

    assert outcome.status == "accepted"
    assert telemetry["normalized_selected_candidate_ids"] == [candidate_id]
    assert telemetry["candidate_ids"] == [candidate_id]


def test_outer_timeout_stays_truthful_when_inner_stage_is_unproven() -> None:
    exc = subprocess.TimeoutExpired(
        ["run_opportunity_scout.py"],
        timeout=150,
        output="partial child output without a completed stage packet",
        stderr="",
    )
    failure = known_exception_to_operator_failure(
        exc,
        context="Opportunity Scout Agent child process",
    )
    evidence = cli._child_process_timeout_evidence(
        exc,
        timeout_seconds=150.0,
        child_process_ms=150_048.824,
    )

    assert failure.kind == "child_process_deadline_exceeded"
    assert "provider" not in failure.summary.lower()
    assert evidence["active_parent_stage"] == "entry.specialist_child_process"
    assert evidence["inner_stage"] is None
    assert evidence["inner_stage_available"] is False
    assert evidence["classification"] == "outer_deadline_inner_cause_unproven"
    assert evidence["child_counts"] == {
        "model_requests": None,
        "model_tool_calls": None,
        "provider_attempts": None,
        "provider_receipts": None,
        "availability": "unavailable",
    }

    deadline = ExecutionDeadlineLedger.from_timeouts(
        soft_timeout_seconds=0,
        hard_timeout_seconds=5,
        finalization_reserve_seconds=1,
        correlation_id="opportunity-failure-cluster",
        wall_clock_ms=lambda: 1_000_000,
        monotonic_clock_ms=lambda: 500_000,
    )
    with pytest.raises(ExecutionDeadlineExceeded) as rejected:
        deadline.admit(stage="opportunity_scout:decision_repair", boundary="llm")

    deadline_evidence = rejected.value.telemetry()
    assert deadline_evidence["schema"] == EXECUTION_DEADLINE_SCHEMA
    assert deadline_evidence["rejected_before_dispatch"] is True
    assert deadline_evidence["in_flight_preemption_supported"] is False
    assert deadline_evidence["boundary"] == "llm"
