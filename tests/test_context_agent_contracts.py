from __future__ import annotations

from keystone_agents.agent_registry import AGENT_REGISTRY
from keystone_agents.agent_tool_policy import tool_policy_for_agent
from keystone_agents.context_agent_contracts import (
    ContextGraphRole,
    ContextMutationLevel,
    context_agent_contracts,
    get_context_agent_contract,
)

EXPECTED_CONTEXT_AGENT_ISSUES = {
    "airtable_context_agent": "ANU-198",
    "google_workspace_context_agent": "ANU-199",
    "zotero_context_agent": "ANU-200",
    "rss_context_agent": "ANU-201",
    "preprints_context_agent": "ANU-202",
}


def test_context_agent_contract_catalog_covers_linear_issue_slice() -> None:
    contracts = context_agent_contracts()

    assert {contract.agent_name: contract.issue_id for contract in contracts} == (
        EXPECTED_CONTEXT_AGENT_ISSUES
    )
    assert all(contract.agent_name in AGENT_REGISTRY for contract in contracts)
    assert all(tool_policy_for_agent(contract.agent_name) is not None for contract in contracts)


def test_context_agent_contracts_define_rw_modify_boundaries() -> None:
    for contract in context_agent_contracts():
        assert contract.read_boundary
        assert contract.write_boundary
        assert contract.modify_boundary
        assert contract.blocked_actions
        assert contract.approval_requirements
        assert contract.validation_requirements


def test_feed_context_agents_stay_read_only_context_nodes() -> None:
    for agent_name in ("rss_context_agent", "preprints_context_agent"):
        contract = get_context_agent_contract(agent_name)

        assert contract is not None
        assert contract.current_role == ContextGraphRole.TOOL_CONTEXT_NODE
        assert contract.mutation_level == ContextMutationLevel.READ_ONLY
        assert any("mutation" in action for action in contract.blocked_actions)
        assert any("Business Research" in edge for edge in contract.graph_candidate_edges)
        assert "ResearchContextPack" in contract.handoff_contract.context_packs


def test_airtable_workspace_zotero_write_paths_are_guarded() -> None:
    airtable = get_context_agent_contract("airtable_context_agent")
    workspace = get_context_agent_contract("google_workspace_context_agent")
    zotero = get_context_agent_contract("zotero_context_agent")

    assert airtable is not None
    assert workspace is not None
    assert zotero is not None
    assert airtable.mutation_level == ContextMutationLevel.APPROVED_INTERNAL_WRITE
    assert workspace.mutation_level == ContextMutationLevel.APPROVED_INTERNAL_WRITE
    assert zotero.mutation_level == ContextMutationLevel.GUARDED_EXTERNAL_IMPORTER
    assert any("approval reference" in item for item in airtable.approval_requirements)
    assert any("approval reference" in item for item in workspace.approval_requirements)
    assert any("dry-run importer proof" in item for item in zotero.approval_requirements)
    assert any("direct Zotero library mutation" in item for item in zotero.blocked_actions)


def test_contracts_define_downstream_evidence_for_research_and_opportunities() -> None:
    for contract in context_agent_contracts():
        handoff = contract.handoff_contract

        assert handoff.required_source_fields
        assert handoff.uncertainty_fields
        assert handoff.evidence_to_business_research
        assert handoff.evidence_to_opportunity_scout
        assert "ResearchContextPack" in handoff.context_packs
        assert contract.graph_candidate_edges
        assert contract.subgraph_promotion_requirements
