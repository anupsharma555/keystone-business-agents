"""Requested domain counts survive defaults, explicit ceilings, and checkpoint JSON."""

from __future__ import annotations

import json

import pytest

from keystone_agents import workflow_runner as workflow
from keystone_agents.entrypoints.cli_impl import build_parser
from keystone_agents.quality_budget import AgentQualityBudget, QualityMode
from keystone_agents.runtime.decision_validation import AgentDecisionValidationError
from keystone_agents.runtime.request_budget import activate_model_request_budget
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkItem,
    WorkItemKind,
    WorkItemRoute,
    WorkItemSourceRef,
    WorkItemTarget,
)


def plan(count):
    return {
        "source": "orchestrator_canonical",
        "target_agent": "opportunity_scout",
        "intent": "opportunity_search",
        "primary_target": "Synthetic Company",
        "target_type": "company",
        "desired_count": count,
        "desired_count_explicit": False,
    }


@pytest.mark.parametrize(
    "count,kwargs,expected,explicit",
    [
        (1, {}, 1, False),
        (5, {}, 5, False),
        (10, {}, 10, False),
        (5, {"max_results": 2}, 2, True),
        (5, {"max_results": 3}, 3, True),
        (1, {"max_results": 8}, 1, True),
        (5, {"max_results": 3, "max_results_explicit": False}, 5, False),
    ],
)
def test_canonical_count_owns_output_and_explicit_caller_limit_caps_it(
    count, kwargs, expected, explicit
):
    request = WorkflowRunRequest(manual_request_plan=plan(count), **kwargs)
    assert request.max_results_explicit is explicit
    assert workflow._effective_max_results(request) == expected
    payload = json.loads(request.model_dump_json())
    assert payload["max_results_explicit"] is explicit
    restored = WorkflowRunRequest.from_checkpoint(payload)
    assert restored.max_results_explicit is explicit
    assert workflow._effective_max_results(restored) == expected


@pytest.mark.parametrize(
    "kwargs,expected", [({}, 3), ({"max_results": 1}, 1), ({"max_results": 20}, 20)]
)
def test_unplanned_request_keeps_legacy_default_or_explicit_limit(kwargs, expected):
    request = WorkflowRunRequest(**kwargs)
    assert workflow._effective_max_results(request) == expected


def test_api_input_presence_and_legacy_checkpoint_default_are_distinct():
    payload = {"max_results": 3, "manual_request_plan": plan(5)}
    api_request = WorkflowRunRequest.model_validate(payload)
    assert api_request.max_results_explicit is True
    assert workflow._effective_max_results(api_request) == 3
    restored_legacy = WorkflowRunRequest.from_checkpoint(payload)
    assert restored_legacy.max_results_explicit is False
    assert workflow._effective_max_results(restored_legacy) == 5
    assert "max_results_explicit" not in payload  # Never rewrite historical input objects.


@pytest.mark.parametrize("command", [["ask", "Review one experiment"], ["work-items", "advance"]])
@pytest.mark.parametrize("limit", [None, 2, 3, 8])
def test_cli_records_argument_presence_even_when_explicit_value_equals_default(command, limit):
    argv = command if limit is None else [*command, "--max-results", str(limit)]
    args = build_parser().parse_args(argv)
    request = WorkflowRunRequest(
        max_results=args.max_results,
        max_results_explicit=args.max_results_explicit,
        manual_request_plan=plan(5),
    )
    assert args.max_results_explicit is (limit is not None)
    assert workflow._effective_max_results(request) == (5 if limit is None else min(5, limit))


def test_domain_result_count_does_not_reduce_company_source_retrieval_breadth():
    request = WorkflowRunRequest(manual_request_plan=plan(1))
    budget = AgentQualityBudget(
        agent_name="business_research_analyst",
        mode=QualityMode.DEEP,
        max_seconds=300,
        retrieval_max_results=8,
    )
    assert workflow._effective_max_results(request) == 1
    assert workflow._quality_budgeted_max_results(request, budget) == 8
    assert (
        workflow._quality_budgeted_max_results(request, budget, preserve_requested_count=True) == 1
    )
    capped = WorkflowRunRequest(max_results=2, manual_request_plan=plan(5))
    assert (
        workflow._quality_budgeted_max_results(capped, budget, preserve_requested_count=True) == 2
    )


@pytest.mark.parametrize("too_many", [False, True])
def test_one_experiment_reaches_actual_opportunity_sdk_and_bounds_accepted_output(
    supplied_opportunity_fake_sdk,
    too_many,
):
    request = WorkflowRunRequest(
        request_text=(
            "Assess whether one internal experiment is justified from the supplied evidence."
        ),
        manual_request_plan=plan(1),
        live_sdk=True,
        live_search=False,
        save=False,
        sdk_session_enabled=False,
    )
    item = WorkItem(
        kind=WorkItemKind.OPPORTUNITY,
        title="One experiment",
        request_text=request.request_text,
        current_route=WorkItemRoute.OPPORTUNITY_SCOUT,
        target=WorkItemTarget(name="Synthetic Company", object_type="company"),
        sources=[
            WorkItemSourceRef(
                source_id="experiment-evidence",
                provider_candidate_id="experiment-evidence",
                title="Supplied experiment evidence",
                url="https://example.test/experiment",
                provider="operator_supplied",
                extraction_status="supplied_material",
                evidence_excerpt="The available evaluation permits a proposed internal comparison.",
            )
        ],
    )
    supplied_opportunity_fake_sdk.count = 2 if too_many else None
    with activate_model_request_budget(2) as budget:
        if too_many:
            with pytest.raises(AgentDecisionValidationError):
                workflow._run_supplied_opportunity_sdk(
                    item,
                    request=request,
                    topic=item.target.name,
                    store=None,
                    sdk_session=None,
                )
            assert budget.consumed == 2  # Existing single decision repair, no added retry.
        else:
            output, _ = workflow._run_supplied_opportunity_sdk(
                item,
                request=request,
                topic=item.target.name,
                store=None,
                sdk_session=None,
            )
            assert len(output.records) == budget.consumed == 1
    assert all(call["typed_input"].max_results == 1 for call in supplied_opportunity_fake_sdk.calls)


def test_native_checkpoint_retains_default_and_explicit_count_provenance(tmp_path):
    pytest.importorskip("langgraph.checkpoint.sqlite")
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.graph import END, START, StateGraph

    from keystone_agents.langgraph_workflow import WorkItemGraphState

    builder = StateGraph(WorkItemGraphState)
    builder.add_node("retain", lambda state: {"request": state["request"]})
    builder.add_edge(START, "retain")
    builder.add_edge("retain", END)
    database = str(tmp_path / "count-checkpoint.sqlite")
    for explicit in (False, True):
        request = WorkflowRunRequest(
            max_results=3,
            max_results_explicit=explicit,
            manual_request_plan=plan(5),
        )
        config = {"configurable": {"thread_id": f"count-{explicit}"}}
        with SqliteSaver.from_conn_string(database) as saver:
            graph = builder.compile(checkpointer=saver)
            graph.invoke({"request": request.model_dump(mode="json")}, config=config)
        with SqliteSaver.from_conn_string(database) as saver:
            graph = builder.compile(checkpointer=saver)
            persisted = graph.get_state(config).values["request"]
            restored = WorkflowRunRequest.from_checkpoint(persisted)
            assert workflow._effective_max_results(restored) == (3 if explicit else 5)
