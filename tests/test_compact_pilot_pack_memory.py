"""Compact pilot packs preserve task state without automatic legacy-memory queries."""

import pytest

from keystone_agents.runtime.continuity_policy import (
    SlackCompactContinuityPolicy,
    activate_continuity_policy,
)
from keystone_agents.schemas.approval import ApprovalState
from keystone_agents.schemas.context_pack import MemoryContextRef
from keystone_agents.schemas.memory import MemoryItem
from keystone_agents.schemas.work_item import (
    WorkItem,
    WorkItemApprovalGate,
    WorkItemArtifactRef,
    WorkItemFact,
    WorkItemKind,
    WorkItemRoute,
    WorkItemTarget,
)
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.work_items import build_context_pack_for_route, hydrate_context_pack_memory

MEMORY_FIELDS = {
    "relevant_memory_refs", "approved_company_facts", "retrieval_performance_notes",
    "prior_opportunity_refs", "outreach_style_examples", "approval_history_refs",
}


def _work_item():
    return WorkItem(
        kind=WorkItemKind.COMPANY_RESEARCH, title="Synthetic task",
        request_text="Research Synthetic",
        target=WorkItemTarget(
            name="Synthetic", metadata={"manual_constraints": ["Keep scope exact"]},
        ),
        facts=[WorkItemFact(
            key="current-task-fact", value="Current approved task fact.",
            approval_state=ApprovalState.APPROVED_FOR_RESEARCH,
        )],
        artifact_refs=[WorkItemArtifactRef(
            artifact_id="current-artifact", artifact_type="research_brief", selected=True,
            source_agent="business_research_analyst", summary="Current canonical task artifact.",
        )],
        approval_gates=[WorkItemApprovalGate(scope="external_use", state="pending")],
    )


@pytest.mark.parametrize("route", [
    WorkItemRoute.BUSINESS_RESEARCH_ANALYST, WorkItemRoute.OPPORTUNITY_SCOUT,
    WorkItemRoute.OUTREACH_COMPOSER, WorkItemRoute.GMAIL_TRIAGE,
    WorkItemRoute.RAG_RETRIEVAL_SPECIALIST,
])
@pytest.mark.parametrize("with_store", [False, True])
def test_pilot_clears_only_hydration_owned_refs_without_querying(
    tmp_path, monkeypatch, route, with_store,
):
    work_item = _work_item()
    original_work_item = work_item.model_dump(mode="json")
    pack = build_context_pack_for_route(work_item, route)
    memory = MemoryContextRef(
        id=17, memory_type="retrieval_tool_performance", title="AUTOMATIC_METRIC_MEMORY_SENTINEL",
    )
    present = MEMORY_FIELDS.intersection(type(pack).model_fields)
    pack = pack.model_copy(update={field: [memory] for field in present})
    before = pack.model_dump(mode="json")
    store = SQLiteStore(f"sqlite:///{tmp_path / 'pilot.db'}") if with_store else None

    def forbidden(*args, **kwargs):
        raise AssertionError("Eligible compact pilot must not auto-query legacy memory.")

    monkeypatch.setattr(SQLiteStore, "retrieve_memory", forbidden)
    with activate_continuity_policy(SlackCompactContinuityPolicy(eligible=True)):
        result = hydrate_context_pack_memory(pack, work_item, store)
    assert all(getattr(result, field) == [] for field in present)
    assert result.model_dump(mode="json", exclude=present) == {
        key: value for key, value in before.items() if key not in present
    }
    assert work_item.model_dump(mode="json") == original_work_item
    assert result.approved_facts and result.selected_artifacts and result.approval_gates
    assert result.constraints == ["Keep scope exact"]


@pytest.mark.parametrize("policy", [None, SlackCompactContinuityPolicy(eligible=False)])
def test_nonpilot_keeps_legacy_hydration(tmp_path, monkeypatch, policy):
    store = SQLiteStore(f"sqlite:///{tmp_path / 'legacy.db'}")
    for kind in ("company_fact", "retrieval_tool_performance"):
        store.save_memory_item(MemoryItem(
            memory_type=kind, object_key="Synthetic", title=f"Synthetic {kind}",
            summary="Previously approved Synthetic context.",
            approval_state=ApprovalState.APPROVED_FOR_RESEARCH,
        ))
    calls = []
    original = store.retrieve_memory

    def observed(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "retrieve_memory", observed)
    if policy is None:
        result = build_context_pack_for_route(
            _work_item(), WorkItemRoute.BUSINESS_RESEARCH_ANALYST, store=store,
        )
    else:
        with activate_continuity_policy(policy):
            result = build_context_pack_for_route(
                _work_item(), WorkItemRoute.BUSINESS_RESEARCH_ANALYST, store=store,
            )
    assert calls
    assert len(result.approved_company_facts) == 1
    assert len(result.retrieval_performance_notes) == 1
    assert len(result.relevant_memory_refs) == 2
