from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from keystone_agents.agents import business_research_analyst as research_module
from keystone_agents.agents import chief_of_staff as chief_module
from keystone_agents.agents import gmail_triage as gmail_module
from keystone_agents.agents import opportunity_scout as opportunity_module
from keystone_agents.agents import orchestrator as orchestrator_module
from keystone_agents.agents import outreach_composer as outreach_module
from keystone_agents.agents.business_research_analyst import (
    build_business_research_analyst_agent,
    build_business_research_analyst_focused_brief_agent,
)
from keystone_agents.agents.chief_of_staff import build_chief_of_staff_agent
from keystone_agents.agents.gmail_triage import build_gmail_triage_agent
from keystone_agents.agents.opportunity_scout import (
    build_opportunity_scout_agent,
    build_opportunity_scout_synthesis_agent,
)
from keystone_agents.agents.orchestrator import build_orchestrator_agent
from keystone_agents.agents.outreach_composer import build_outreach_composer_agent
from keystone_agents.agents.preprints_context import build_preprints_context_agent
from keystone_agents.agents.rss_context import build_rss_context_agent
from keystone_agents.capabilities.tool_scope import (
    RequestToolScopeReceipt,
    ToolCapabilityFamily,
    scope_tools_for_request,
    tool_scope_receipt_for_agent,
)
from keystone_agents.models import GmailTriageSDKInput, TypedAgentRunResult
from keystone_agents.planning.compatibility import infer_manual_request_plan
from keystone_agents.schemas.chief_of_staff import ChiefOfStaffResult
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan


@dataclass(frozen=True)
class FakeTool:
    name: str


def _tools(*names: str) -> list[FakeTool]:
    return [FakeTool(name=name) for name in names]


def _agent_tool_names(agent: Any) -> set[str]:
    return {str(getattr(tool, "name", "") or "") for tool in agent.tools}


def _legacy_v1_receipt() -> dict[str, object]:
    """Return the exact receipt shape emitted before diagnostic names existed."""

    return {
        "schema_name": "keystone.request_tool_scope.v1",
        "agent_name": "preprints_context_agent",
        "requested_mode": "request_scoped",
        "effective_mode": "request_scoped",
        "source": "canonical_plan",
        "max_tool_tier": "core_read",
        "capability_families": ["core_context"],
        "candidate_tool_count": 3,
        "selected_tool_names": [
            "retrieve_preprint_announcement_history",
            "inspect_signal_lifecycle",
        ],
        "selected_tool_count": 2,
        "omitted_tool_count": 1,
        "reduction_ratio": 0.333333,
        "selection_fingerprint": "a" * 64,
        "notes": ["Legacy v1 receipt without diagnostic identity arrays."],
    }


def test_legacy_v1_receipt_round_trip_preserves_unknown_diagnostic_identities() -> None:
    receipt = RequestToolScopeReceipt.model_validate(_legacy_v1_receipt())

    serialized = receipt.receipt()

    assert serialized["candidate_tool_count"] == 3
    assert serialized["selected_tool_count"] == 2
    assert serialized["omitted_tool_count"] == 1
    assert serialized["candidate_tool_names"] is None
    assert serialized["omitted_tool_names"] is None
    assert serialized["omission_reasons"] is None
    assert RequestToolScopeReceipt.model_validate(serialized) == receipt


@pytest.mark.parametrize(
    ("updates", "error"),
    (
        ({"selected_tool_count": 1}, "selected_tool_count"),
        ({"candidate_tool_count": 4}, "candidate count"),
        (
            {
                "selected_tool_names": [
                    "retrieve_preprint_announcement_history",
                    "retrieve_preprint_announcement_history",
                ]
            },
            "selected tool names must be unique",
        ),
    ),
)
def test_legacy_v1_receipt_rejects_inconsistent_known_evidence(
    updates: dict[str, object],
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        RequestToolScopeReceipt.model_validate(
            {
                **_legacy_v1_receipt(),
                **updates,
            }
        )


def test_enriched_v1_receipt_validates_complete_diagnostic_identity_sets() -> None:
    payload = {
        **_legacy_v1_receipt(),
        "candidate_tool_names": [
            "retrieve_preprint_announcement_history",
            "inspect_signal_lifecycle",
            "read_signal_checkpoint",
        ],
        "omitted_tool_names": ["read_signal_checkpoint"],
        "omission_reasons": ["public_acceptance_profile_allowlist"],
    }

    receipt = RequestToolScopeReceipt.model_validate(payload)

    assert receipt.candidate_tool_names == (
        "retrieve_preprint_announcement_history",
        "inspect_signal_lifecycle",
        "read_signal_checkpoint",
    )
    assert receipt.omitted_tool_names == ("read_signal_checkpoint",)


@pytest.mark.parametrize(
    ("updates", "error"),
    (
        (
            {"candidate_tool_names": ["one", "two", "three"]},
            "must be supplied together",
        ),
        (
            {
                "candidate_tool_names": ["one", "two", "three"],
                "omitted_tool_names": ["three"],
            },
            "must be supplied together",
        ),
        (
            {
                "candidate_tool_names": [
                    "retrieve_preprint_announcement_history",
                    "inspect_signal_lifecycle",
                ],
                "omitted_tool_names": ["read_signal_checkpoint"],
                "omission_reasons": ["request_scope_policy"],
            },
            "candidate_tool_count must equal candidate_tool_names length",
        ),
        (
            {
                "candidate_tool_names": [
                    "retrieve_preprint_announcement_history",
                    "inspect_signal_lifecycle",
                    "read_signal_checkpoint",
                ],
                "omitted_tool_names": ["inspect_signal_lifecycle"],
                "omission_reasons": ["request_scope_policy"],
            },
            "selected and omitted tool names must be disjoint",
        ),
        (
            {
                "candidate_tool_names": [
                    "retrieve_preprint_announcement_history",
                    "inspect_signal_lifecycle",
                    "inspect_signal_lifecycle",
                ],
                "omitted_tool_names": ["read_signal_checkpoint"],
                "omission_reasons": ["request_scope_policy"],
            },
            "candidate tool names must be unique",
        ),
        (
            {
                "candidate_tool_names": [
                    "retrieve_preprint_announcement_history",
                    "inspect_signal_lifecycle",
                    "unexpected_tool",
                ],
                "omitted_tool_names": ["read_signal_checkpoint"],
                "omission_reasons": ["request_scope_policy"],
            },
            "candidate tools must equal selected plus omitted tools",
        ),
    ),
)
def test_enriched_v1_receipt_rejects_partial_or_inconsistent_diagnostics(
    updates: dict[str, object],
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        RequestToolScopeReceipt.model_validate(
            {
                **_legacy_v1_receipt(),
                **updates,
            }
        )


def test_missing_or_compatibility_plan_cannot_enlarge_core_context() -> None:
    candidates = _tools(
        "list_local_context_sources",
        "search_local_context",
        "read_local_context_file",
        "retrieve_memory",
        "check_workflow_duplicate",
        "search_web",
        "fetch_company_page",
        "airtable_write_record",
    )
    compatibility_plan = ManualRequestPlan(
        source="heuristic",
        target_agent="business_research_analyst",
        intent="company_research",
        requires_live_search=True,
        provider_system="airtable",
        provider_operations=["update"],
        ask_shape={"permission_state": "approval_required", "evidence_depth": "deep"},
    )

    attachment = scope_tools_for_request(
        "business_research_analyst",
        candidates,
        manual_request_plan=compatibility_plan,
    )

    assert set(attachment.scope.selected_tool_names) == {
        "list_local_context_sources",
        "search_local_context",
        "read_local_context_file",
        "retrieve_memory",
        "check_workflow_duplicate",
    }
    assert attachment.scope.source == "default_minimum"
    assert attachment.scope.max_tool_tier == "core_read"
    assert "search_web" not in attachment.scope.selected_tool_names
    assert "airtable_write_record" not in attachment.scope.selected_tool_names


def test_builder_required_tools_cannot_bypass_missing_or_compatibility_authority() -> None:
    compatibility_plan = ManualRequestPlan(
        source="heuristic",
        target_agent="business_research_analyst",
        intent="company_research",
        requires_live_search=True,
        ask_shape={"permission_state": "read_only", "evidence_depth": "deep"},
    )
    business_missing = build_business_research_analyst_agent(
        request_text="Research the company.",
        tool_tier="deep_retrieval",
    )
    business_compatibility = build_business_research_analyst_agent(
        request_text="Research the company.",
        manual_request_plan=compatibility_plan,
        tool_tier="deep_retrieval",
    )
    opportunity_missing = build_opportunity_scout_agent(
        request_text="Find opportunities.",
        tool_tier="deep_retrieval",
    )
    gmail_missing = build_gmail_triage_agent(
        request_text="Draft a reply and archive the message.",
        tool_tier="internal_write",
    )
    orchestrator_missing = build_orchestrator_agent(
        request_text="Research and route this request.",
        tool_tier="deep_retrieval",
    )
    chief_compatibility = build_chief_of_staff_agent(
        request_text="Inspect the Airtable records only.",
        manual_request_plan=ManualRequestPlan(
            source="heuristic",
            target_agent="chief_of_staff",
            intent="business_system_write",
            provider_system="airtable",
            provider_operations=["read"],
            ask_shape={"permission_state": "read_only"},
        ),
    )

    for agent in (business_missing, business_compatibility):
        assert {
            "search_web",
            "fetch_company_page",
            "extract_selected_urls_to_source_bundle",
        }.isdisjoint(_agent_tool_names(agent))
    assert {"search_web", "score_opportunity"}.isdisjoint(_agent_tool_names(opportunity_missing))
    assert {
        "create_gmail_draft_reply",
        "modify_gmail_message_state",
    }.isdisjoint(_agent_tool_names(gmail_missing))
    assert {"search_web", "extract_research_claims_from_html"}.isdisjoint(
        _agent_tool_names(orchestrator_missing)
    )
    assert {
        "airtable_write_record",
        "airtable_upload_attachment",
        "airtable_create_expense_from_receipt",
    }.isdisjoint(_agent_tool_names(chief_compatibility))
    assert tool_scope_receipt_for_agent(chief_compatibility)["effective_mode"] == ("request_scoped")


def test_canonical_plans_restore_exact_research_and_draft_capabilities() -> None:
    research_plan = ManualRequestPlan(
        source="canonical:test",
        target_agent="business_research_analyst",
        intent="company_research",
        requires_live_search=True,
        ask_shape={"permission_state": "read_only", "evidence_depth": "deep"},
    )
    gmail_plan = ManualRequestPlan(
        source="canonical:test",
        target_agent="gmail_triage",
        intent="gmail_triage",
        ask_shape={"permission_state": "draft_only", "output_form": "draft"},
    )

    research = build_business_research_analyst_agent(
        request_text="Research the supplied company.",
        manual_request_plan=research_plan,
    )
    gmail = build_gmail_triage_agent(
        request_text="Prepare the approved draft.",
        manual_request_plan=gmail_plan,
    )

    assert {
        "search_web",
        "fetch_company_page",
        "extract_selected_urls_to_source_bundle",
    } <= _agent_tool_names(research)
    assert {
        "create_gmail_draft_reply",
        "create_approval_queue_item",
    } <= _agent_tool_names(gmail)


def test_natural_selected_page_read_attaches_only_bounded_extraction_without_deep_wording(
) -> None:
    request = (
        "Business Research Analyst, read only Kooth's public homepage at "
        "https://www.kooth.com/ and tell me what it offers, who it serves, and "
        "what remains uncertain for Keystone. Include the exact URL; don't search "
        "elsewhere or save anything."
    )
    plan = infer_manual_request_plan(
        request,
        requested_agent="business_research_analyst",
    )

    agent = build_business_research_analyst_agent(
        request_text=request,
        manual_request_plan=plan,
    )

    assert plan.ask_shape.evidence_depth == "unspecified"
    assert _agent_tool_names(agent) == {
        "extract_selected_urls_to_source_bundle", "read_web_source_window"
    }
    receipt = tool_scope_receipt_for_agent(agent)
    assert receipt["max_tool_tier"] == "deep_retrieval"
    assert receipt["capability_families"] == ["deep_retrieval"]


def test_natural_bounded_live_opportunity_attaches_search_extract_and_score_without_deep_wording(
) -> None:
    request = (
        "Opportunity Scout, find current U.S.-based accelerators, grants, or pilot "
        "programs that a small neuroinformatics consultancy could pursue for "
        "behavioral-health measurement work. Compare the plausible choices, return "
        "one best fit with source URLs and eligibility caveats, and don't save "
        "anything or draft outreach."
    )
    plan = infer_manual_request_plan(request, requested_agent="opportunity_scout")

    agent = build_opportunity_scout_agent(
        request_text=request,
        manual_request_plan=plan,
    )

    assert plan.requires_live_search is True
    assert plan.ask_shape.evidence_depth == "unspecified"
    names = _agent_tool_names(agent)
    assert {
        "search_web",
        "extract_selected_urls_to_source_bundle",
        "extract_research_claims_from_html",
        "score_opportunity",
    } <= names
    assert "structure_web_data_for_schema" not in names
    receipt = tool_scope_receipt_for_agent(agent)
    assert receipt["max_tool_tier"] == "deep_retrieval"


@pytest.mark.parametrize(
    ("requested_agent", "request_text"),
    (
        (
            "airtable_context_agent",
            "Don't access Airtable. Search the public web for Northstar Health's "
            "current partnership evidence and cite the sources.",
        ),
        (
            "zotero_context_agent",
            "Don't use Zotero. Search the public web for current evidence about "
            "implementation barriers and cite the sources.",
        ),
    ),
)
def test_explicit_context_agent_to_public_web_delegation_attaches_read_only_research_tools(
    requested_agent: str,
    request_text: str,
) -> None:
    plan = infer_manual_request_plan(
        request_text,
        requested_agent=requested_agent,
    )

    agent = build_business_research_analyst_agent(
        request_text=request_text,
        manual_request_plan=plan,
        tool_tier="deep_retrieval",
    )
    names = _agent_tool_names(agent)

    assert plan.objective == request_text
    assert plan.requested_agent == requested_agent
    assert plan.target_agent == "business_research_analyst"
    assert plan.requires_live_search is True
    assert set(plan.provider_operations) <= {"read", "search", "verify"}
    assert {
        "search_web",
        "extract_selected_urls_to_source_bundle",
        "extract_research_claims_from_html",
    } <= names
    assert {
        "airtable_write_record",
        "google_doc_write",
        "google_sheet_write_rows",
    }.isdisjoint(names)


@pytest.mark.parametrize(
    "plan",
    (
        ManualRequestPlan(
            source="heuristic",
            requested_agent="airtable_context_agent",
            target_agent="business_research_analyst",
            intent="company_research",
            objective=(
                "Don't access Airtable and don't search the public web. "
                "Use only the supplied facts."
            ),
            task_objective="source_research",
            expected_artifact_type="research_brief",
            requires_live_search=True,
            ask_shape={"permission_state": "read_only"},
        ),
        ManualRequestPlan(
            source="heuristic",
            requested_agent="zotero_context_agent",
            target_agent="business_research_analyst",
            intent="company_research",
            objective="Review the supplied evidence without using Zotero.",
            task_objective="source_research",
            expected_artifact_type="research_brief",
            requires_live_search=True,
            ask_shape={"permission_state": "read_only"},
        ),
        ManualRequestPlan(
            source="heuristic",
            requested_agent="airtable_context_agent",
            target_agent="business_research_analyst",
            intent="company_research",
            objective="Search the public web, then update the Airtable record.",
            task_objective="source_research",
            expected_artifact_type="research_brief",
            provider_system="airtable",
            provider_operations=["search", "update"],
            requires_live_search=True,
            side_effect_policy="approval_required",
            ask_shape={"permission_state": "approval_required"},
        ),
    ),
)
def test_cross_agent_compatibility_cannot_grant_web_or_write_tools_without_bounded_authority(
    plan: ManualRequestPlan,
) -> None:
    agent = build_business_research_analyst_agent(
        request_text=plan.objective,
        manual_request_plan=plan,
        tool_tier="deep_retrieval",
    )
    names = _agent_tool_names(agent)

    assert "search_web" not in names
    assert "extract_selected_urls_to_source_bundle" not in names
    assert "airtable_write_record" not in names


def test_canonical_deep_research_plan_adds_retrieval_not_provider_writes() -> None:
    candidates = _tools(
        "list_local_context_sources",
        "retrieve_memory",
        "search_web",
        "fetch_company_page",
        "extract_selected_urls_to_source_bundle",
        "capture_browser_diagnostics",
        "airtable_write_record",
    )
    plan = ManualRequestPlan(
        source="canonical",
        target_agent="business_research_analyst",
        intent="company_research",
        requires_live_search=True,
        ask_shape={"evidence_depth": "deep", "permission_state": "read_only"},
    )

    attachment = scope_tools_for_request(
        "business_research_analyst",
        candidates,
        manual_request_plan=plan,
    )

    names = set(attachment.scope.selected_tool_names)
    assert {
        "list_local_context_sources",
        "retrieve_memory",
        "search_web",
        "fetch_company_page",
        "extract_selected_urls_to_source_bundle",
    } <= names
    assert "capture_browser_diagnostics" not in names
    assert "airtable_write_record" not in names
    assert attachment.scope.max_tool_tier == "deep_retrieval"
    assert ToolCapabilityFamily.PUBLIC_WEB_SEARCH in attachment.scope.capability_families
    assert ToolCapabilityFamily.DEEP_RETRIEVAL in attachment.scope.capability_families


def test_supplied_opportunity_comparison_attaches_only_scoring() -> None:
    plan = ManualRequestPlan(
        source="canonical:test",
        target_agent="opportunity_scout",
        intent="opportunity_search",
        expected_artifact_type="opportunity_record",
        requires_live_search=False,
        constraints=["comparison-format"],
        ask_shape={"permission_state": "read_only"},
    )

    agent = build_opportunity_scout_agent(
        request_text=(
            "Compare and score the two supplied synthetic opportunity records; "
            "do not search or save anything."
        ),
        manual_request_plan=plan,
        tool_tier="deep_retrieval",
    )

    assert _agent_tool_names(agent) == {"score_opportunity"}
    assert "load_existing_opportunity_state" not in _agent_tool_names(agent)


def test_live_opportunity_discovery_retains_search_extraction_and_scoring() -> None:
    plan = ManualRequestPlan(
        source="canonical:test",
        target_agent="opportunity_scout",
        intent="opportunity_search",
        expected_artifact_type="opportunity_record",
        requires_live_search=True,
        ask_shape={"permission_state": "read_only", "evidence_depth": "deep"},
    )

    agent = build_opportunity_scout_agent(
        request_text="Find and score two currently open opportunities from public sources.",
        manual_request_plan=plan,
        tool_tier="deep_retrieval",
    )
    names = _agent_tool_names(agent)

    assert {
        "search_web",
        "extract_research_claims_from_html",
        "score_opportunity",
    } <= names
    assert "load_existing_opportunity_state" not in names


def test_canonical_provider_read_and_write_plans_have_different_surfaces() -> None:
    candidates = _tools(
        "route_request_placeholder",
        "airtable_get_base_schema",
        "airtable_read_records",
        "airtable_aggregate_records",
        "airtable_write_record",
        "airtable_upload_attachment",
    )
    read_plan = ManualRequestPlan(
        source="canonical",
        target_agent="orchestrator",
        intent="context_lookup",
        provider_system="airtable",
        provider_operations=["read"],
        ask_shape={"permission_state": "read_only"},
    )
    write_plan = ManualRequestPlan(
        source="canonical",
        target_agent="orchestrator",
        intent="business_system_write",
        provider_system="airtable",
        provider_operations=["read", "update"],
        ask_shape={"permission_state": "approval_required"},
    )

    read_scope = scope_tools_for_request(
        "orchestrator",
        candidates,
        manual_request_plan=read_plan,
    ).scope
    write_scope = scope_tools_for_request(
        "orchestrator",
        candidates,
        manual_request_plan=write_plan,
    ).scope

    assert set(read_scope.selected_tool_names) == {
        "route_request_placeholder",
        "airtable_get_base_schema",
        "airtable_read_records",
        "airtable_aggregate_records",
    }
    assert "airtable_write_record" not in read_scope.selected_tool_names
    assert "airtable_upload_attachment" not in read_scope.selected_tool_names
    assert "airtable_write_record" in write_scope.selected_tool_names
    assert "airtable_upload_attachment" in write_scope.selected_tool_names
    assert write_scope.max_tool_tier == "internal_write"
    assert ToolCapabilityFamily.PROVIDER_WRITE in write_scope.capability_families


@pytest.mark.parametrize(
    ("agent_name", "builder", "history_tool", "evidence_tool"),
    [
        (
            "rss_context_agent",
            build_rss_context_agent,
            "retrieve_rss_announcement_history",
            "read_rss_announcement_evidence",
        ),
        (
            "preprints_context_agent",
            build_preprints_context_agent,
            "retrieve_preprint_announcement_history",
            "read_preprint_announcement_evidence",
        ),
    ],
)
def test_signal_context_read_plan_excludes_checkpoint_mutation_tools(
    agent_name: str,
    builder: Any,
    history_tool: str,
    evidence_tool: str,
) -> None:
    plan = ManualRequestPlan(
        source="canonical:test",
        target_agent=agent_name,
        intent="context_lookup",
        requires_durable_state=False,
        ask_shape={"permission_state": "read_only"},
    )

    agent = builder(
        request_text="Inspect unseen signal history without advancing the checkpoint.",
        manual_plan=plan,
    )

    assert _agent_tool_names(agent) == {
        history_tool,
        evidence_tool,
        "inspect_signal_lifecycle",
    }
    receipt = tool_scope_receipt_for_agent(agent)
    assert receipt["effective_mode"] == "request_scoped"
    assert receipt["selected_tool_count"] == 3
    assert "durable_internal_state" not in receipt["capability_families"]


@pytest.mark.parametrize(
    ("agent_name", "builder", "history_tool", "evidence_tool"),
    [
        (
            "rss_context_agent",
            build_rss_context_agent,
            "retrieve_rss_announcement_history",
            "read_rss_announcement_evidence",
        ),
        (
            "preprints_context_agent",
            build_preprints_context_agent,
            "retrieve_preprint_announcement_history",
            "read_preprint_announcement_evidence",
        ),
    ],
)
def test_signal_context_durable_plan_can_attach_checkpoint_tools(
    agent_name: str,
    builder: Any,
    history_tool: str,
    evidence_tool: str,
) -> None:
    plan = ManualRequestPlan(
        source="canonical:test",
        target_agent=agent_name,
        intent="context_lookup",
        requires_durable_state=True,
        ask_shape={"permission_state": "approval_required"},
    )

    agent = builder(
        request_text="Prepare and advance the approved signal checkpoint.",
        manual_plan=plan,
    )

    assert _agent_tool_names(agent) == {
        history_tool,
        evidence_tool,
        "inspect_signal_lifecycle",
        "prepare_signal_lifecycle_checkpoint",
        "advance_signal_lifecycle_checkpoint",
    }
    receipt = tool_scope_receipt_for_agent(agent)
    assert receipt["max_tool_tier"] == "internal_write"
    assert "durable_internal_state" in receipt["capability_families"]


@pytest.mark.parametrize(
    ("agent_name", "builder"),
    [
        ("rss_context_agent", build_rss_context_agent),
        ("preprints_context_agent", build_preprints_context_agent),
    ],
)
def test_signal_context_read_only_plan_cannot_attach_checkpoint_tools(
    agent_name: str,
    builder: Any,
) -> None:
    plan = ManualRequestPlan(
        source="canonical:test",
        target_agent=agent_name,
        intent="context_lookup",
        requires_durable_state=True,
        ask_shape={"permission_state": "read_only"},
    )

    agent = builder(
        request_text="Inspect the lifecycle but do not advance it.",
        manual_plan=plan,
    )

    assert {
        "prepare_signal_lifecycle_checkpoint",
        "advance_signal_lifecycle_checkpoint",
    }.isdisjoint(_agent_tool_names(agent))


def test_outreach_runtime_shape_reduces_tools_and_full_mode_remains_explicit() -> None:
    legacy_inspection = build_outreach_composer_agent()
    scoped = build_outreach_composer_agent(
        request_text="Prepare a draft from the approved context.",
        tool_scope_mode="request_scoped",
    )
    explicit_full = build_outreach_composer_agent(
        request_text="Prepare a draft from the approved context.",
        tool_scope_mode="full",
    )

    legacy_receipt = tool_scope_receipt_for_agent(legacy_inspection)
    scoped_receipt = tool_scope_receipt_for_agent(scoped)
    full_receipt = tool_scope_receipt_for_agent(explicit_full)

    assert len(legacy_inspection.tools) == len(explicit_full.tools)
    assert len(scoped.tools) < len(explicit_full.tools) / 2
    assert legacy_receipt["source"] == "legacy_empty_builder_inspection"
    assert scoped_receipt["effective_mode"] == "request_scoped"
    assert scoped_receipt["selected_tool_count"] == len(scoped.tools)
    assert scoped_receipt["candidate_tool_count"] == len(explicit_full.tools)
    assert scoped_receipt["reduction_ratio"] > 0.5
    assert full_receipt["source"] == "explicit_full"


def test_supplied_context_synthesis_is_tool_free_with_full_ceiling_receipt() -> None:
    opportunity = build_opportunity_scout_synthesis_agent(max_results=3)
    research = build_business_research_analyst_focused_brief_agent(
        request_text="Synthesize the supplied verified company profile.",
        attach_tools=False,
    )

    for agent in (opportunity, research):
        receipt = tool_scope_receipt_for_agent(agent)
        assert agent.tools == []
        assert receipt["source"] == "supplied_context_tool_free"
        assert receipt["effective_mode"] == "request_scoped"
        assert receipt["candidate_tool_count"] > 40
        assert receipt["selected_tool_count"] == 0
        assert receipt["omitted_tool_count"] == receipt["candidate_tool_count"]
        assert receipt["reduction_ratio"] == 1.0


def test_canonical_outreach_draft_gets_draft_helpers_without_broad_provider_tools() -> None:
    plan = ManualRequestPlan(
        source="canonical",
        target_agent="outreach_composer",
        intent="outreach_draft",
        ask_shape={"permission_state": "draft_only"},
    )
    agent = build_outreach_composer_agent(
        request_text="Draft from the approved packet.",
        manual_request_plan=plan,
    )
    names = {getattr(tool, "name", "") for tool in agent.tools}
    receipt = tool_scope_receipt_for_agent(agent)

    assert {
        "load_company_profile",
        "load_approved_contact_context",
        "check_unsupported_claims",
        "build_approved_outreach_drafting_context",
        "compose_outreach_draft_llm_constrained",
    } <= names
    assert "airtable_write_record" not in names
    assert "google_doc_write" not in names
    assert "search_web" not in names
    assert receipt["max_tool_tier"] == "internal_write"
    assert "draft_preparation" in receipt["capability_families"]


@pytest.mark.parametrize(
    ("agent_name", "builder", "expected_tools"),
    [
        (
            "orchestrator",
            build_orchestrator_agent,
            {
                "load_orchestrator_workflow_state",
                "inspect_work_item_execution_receipts",
                "load_pending_approval_items",
            },
        ),
        (
            "chief_of_staff",
            build_chief_of_staff_agent,
            {
                "inspect_active_work_item_execution_summary",
            },
        ),
    ],
)
def test_manager_work_item_inspection_gets_only_operational_read_tools(
    agent_name: str,
    builder: Any,
    expected_tools: set[str],
) -> None:
    plan = ManualRequestPlan(
        source="llm",
        requested_agent=agent_name,
        target_agent=agent_name,
        intent="continue_work_item",
        provider_system="unspecified",
        requires_durable_state=True,
        ask_shape={"permission_state": "read_only"},
    )
    agent = builder(
        request_text="Inspect current WorkItem state without resuming or modifying it.",
        manual_request_plan=plan,
    )

    assert _agent_tool_names(agent) == expected_tools
    receipt = tool_scope_receipt_for_agent(agent)
    assert receipt["selected_tool_count"] == len(expected_tools)
    assert receipt["max_tool_tier"] == "core_read"
    assert "durable_internal_state" not in receipt["capability_families"]


def test_plannerless_chief_work_item_ask_gets_exact_read_only_tools() -> None:
    request = (
        "Hey Chief of Staff - before I plan tomorrow, look across the three blocked, "
        "non-archived WorkItems updated most recently. For each, give me its current "
        "owner or route and last verified stage. Then tell me which one has the clearest "
        "safe next step and whether any approval is already waiting. Keep this read-only; "
        "don't continue, rerun, approve, or change anything."
    )
    plan = ManualRequestPlan(
        source="heuristic",
        requested_agent="chief_of_staff",
        target_agent="chief_of_staff",
        intent="continue_work_item",
        provider_system="unspecified",
        requires_durable_state=True,
        ask_shape={"permission_state": "read_only"},
    )

    agent = build_chief_of_staff_agent(
        request_text=request,
        manual_request_plan=plan,
    )

    assert _agent_tool_names(agent) == {
        "inspect_active_work_item_execution_summary",
    }
    receipt = tool_scope_receipt_for_agent(agent)
    assert receipt["candidate_tool_count"] == 1
    assert receipt["selected_tool_count"] == 1
    assert receipt["source"] == "default_minimum"


def test_chief_exact_work_item_inspection_gets_only_receipt_tool() -> None:
    plan = ManualRequestPlan(
        source="heuristic",
        requested_agent="chief_of_staff",
        target_agent="chief_of_staff",
        intent="continue_work_item",
        provider_system="unspecified",
        requires_durable_state=True,
        ask_shape={"permission_state": "read_only"},
    )

    agent = build_chief_of_staff_agent(
        request_text=(
            "Could you inspect wi_506f354a486244ffb079f653759182b9 and tell me "
            "the last verified stage without resuming it?"
        ),
        manual_request_plan=plan,
    )

    assert _agent_tool_names(agent) == {"inspect_work_item_execution_receipts"}


def test_orchestrator_exact_work_item_inspection_gets_only_receipt_tool() -> None:
    plan = ManualRequestPlan(
        source="heuristic",
        requested_agent="orchestrator",
        target_agent="orchestrator",
        intent="continue_work_item",
        provider_system="unspecified",
        requires_durable_state=True,
        ask_shape={"permission_state": "read_only"},
    )

    agent = build_orchestrator_agent(
        request_text=(
            "Inspect wi_506f354a486244ffb079f653759182b9 and show its last "
            "receipt-backed stage without calling a specialist or changing it."
        ),
        manual_request_plan=plan,
        include_handoffs=False,
    )

    assert _agent_tool_names(agent) == {"inspect_work_item_execution_receipts"}


def test_empty_full_scope_is_not_labeled_intentionally_tool_free() -> None:
    scope = scope_tools_for_request(
        "chief_of_staff",
        [],
        mode="full",
    ).scope

    assert scope.selected_tool_count == 0
    assert "not proof" in " ".join(scope.notes).lower()


def test_request_bearing_large_agents_default_to_smaller_auditable_toolboxes() -> None:
    builders = (
        (
            build_business_research_analyst_agent,
            "Research the supplied company and verify the selected sources.",
        ),
        (
            build_opportunity_scout_agent,
            "Score the supplied opportunity and identify the next review step.",
        ),
        (
            build_gmail_triage_agent,
            "Review this message and draft a reply without sending it.",
        ),
        (
            build_orchestrator_agent,
            "Route this company research request to the correct specialist.",
        ),
        (
            build_chief_of_staff_agent,
            "Review current operations context and recommend the next action.",
        ),
    )

    for builder, request_text in builders:
        scoped = builder(request_text=request_text)
        full = builder(request_text=request_text, tool_scope_mode="full")
        receipt = tool_scope_receipt_for_agent(scoped)

        assert receipt["effective_mode"] == "request_scoped"
        assert receipt["selected_tool_count"] == len(scoped.tools)
        assert receipt["candidate_tool_count"] == len(full.tools)
        assert receipt["selected_tool_count"] < receipt["candidate_tool_count"]
        assert receipt["reduction_ratio"] >= 0.25
        assert len(receipt["selection_fingerprint"]) == 64


def test_tool_scope_selection_and_fingerprint_are_stable_for_equivalent_inputs() -> None:
    first = scope_tools_for_request(
        "outreach_composer",
        _tools("load_company_profile", "search_web", "airtable_write_record"),
    ).scope
    second = scope_tools_for_request(
        "outreach_composer",
        _tools("load_company_profile", "search_web", "airtable_write_record"),
    ).scope

    assert first.receipt() == second.receipt()
    assert len(first.selection_fingerprint) == 64


def test_outreach_runtime_records_same_scope_in_trace_and_request_cache(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}
    result = TypedAgentRunResult(
        agent_name="outreach_composer",
        output=object(),
        raw_result={},
        request_cache={},
    )

    def fake_run_typed_sdk_agent(**kwargs: Any) -> TypedAgentRunResult[Any]:
        captured.update(kwargs)
        return result

    monkeypatch.setattr(
        outreach_module,
        "run_typed_sdk_agent",
        fake_run_typed_sdk_agent,
    )

    outreach_module.run_outreach_composer_sdk(
        "Load only the bounded approved context needed for one draft.",
        entrypoint="work_item",
    )

    trace_metadata = captured["trace_metadata"]
    cached_scope = result.request_cache["request_tool_scope"]
    assert trace_metadata["request_tool_scope_mode"] == "request_scoped"
    assert (
        trace_metadata["request_tool_scope_candidate_count"]
        > trace_metadata["request_tool_scope_selected_count"]
    )
    assert trace_metadata["request_tool_scope_selected_count"] == len(captured["agent"].tools)
    assert trace_metadata["request_tool_scope_fingerprint"] == cached_scope["selection_fingerprint"]


def test_large_agent_runtimes_persist_scope_receipts_in_trace_and_request_cache(
    monkeypatch,
) -> None:
    captured: list[dict[str, Any]] = []

    def fake_run_typed_sdk_agent(**kwargs: Any) -> TypedAgentRunResult[Any]:
        captured.append(dict(kwargs))
        return TypedAgentRunResult(
            agent_name=str(getattr(kwargs["agent"], "name", "")),
            output=object(),
            raw_result={},
            request_cache={},
        )

    for module in (
        research_module,
        gmail_module,
        opportunity_module,
        orchestrator_module,
    ):
        monkeypatch.setattr(module, "run_typed_sdk_agent", fake_run_typed_sdk_agent)

    research_result = research_module.run_business_research_analyst_sdk(
        "Research the supplied company.",
        manual_request_plan=ManualRequestPlan(
            source="canonical:test",
            target_agent="business_research_analyst",
            intent="company_research",
            requires_live_search=True,
            ask_shape={"permission_state": "read_only", "evidence_depth": "deep"},
        ),
    )
    gmail_result = gmail_module.run_gmail_triage_sdk(
        "Prepare a draft reply without sending it.",
        manual_request_plan=ManualRequestPlan(
            source="canonical:test",
            target_agent="gmail_triage",
            intent="gmail_triage",
            ask_shape={"permission_state": "draft_only", "output_form": "draft"},
        ),
        provider_selection_required=False,
    )
    opportunity_result = opportunity_module.run_opportunity_scout_sdk(
        "Rank the supplied opportunity.",
        manual_request_plan=ManualRequestPlan(
            source="canonical:test",
            target_agent="opportunity_scout",
            intent="opportunity_search",
            requires_live_search=True,
            ask_shape={"permission_state": "read_only", "evidence_depth": "deep"},
        ),
    )
    orchestrator_result = orchestrator_module.run_orchestrator_sdk(
        "Route the supplied request.",
        manual_request_plan=ManualRequestPlan(
            source="canonical:test",
            target_agent="orchestrator",
            intent="route_request",
            ask_shape={"permission_state": "read_only"},
        ),
    )

    for call, result in zip(
        captured,
        (
            research_result,
            gmail_result,
            opportunity_result,
            orchestrator_result,
        ),
        strict=True,
    ):
        trace_metadata = call["trace_metadata"]
        cached_scope = result.request_cache["request_tool_scope"]
        assert trace_metadata["request_tool_scope_selected_count"] == len(call["agent"].tools)
        assert len(trace_metadata["request_tool_scope_fingerprint"]) == 64
        assert (
            trace_metadata["request_tool_scope_fingerprint"]
            == cached_scope["selection_fingerprint"]
        )
    gmail_input = captured[1]["typed_input"]
    assert isinstance(gmail_input, GmailTriageSDKInput)
    assert gmail_input.subject == ""
    assert gmail_input.body == ""
    assert gmail_input.request == "Prepare a draft reply without sending it."
    assert gmail_input.operator_timezone == "America/New_York"
    evaluated_at = datetime.fromisoformat(
        gmail_input.request_evaluated_at.replace("Z", "+00:00")
    )
    assert evaluated_at.tzinfo == UTC


def test_chief_runtime_persists_scope_receipt_in_trace_and_request_cache(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run_typed_sdk_agent(**kwargs: Any) -> TypedAgentRunResult[ChiefOfStaffResult]:
        captured.update(kwargs)
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary="Reviewed bounded operations context.",
                synthesis="Reviewed bounded operations context.",
            ),
            raw_result={},
            request_cache={},
        )

    monkeypatch.setattr(chief_module, "run_typed_sdk_agent", fake_run_typed_sdk_agent)
    result = chief_module.run_chief_of_staff_sdk(
        "Review current operations context.",
        force_sdk_interpretation=True,
        manual_request_plan=ManualRequestPlan(
            source="canonical:test",
            target_agent="chief_of_staff",
            intent="context_lookup",
            ask_shape={"permission_state": "read_only"},
        ),
    )

    trace_metadata = captured["trace_metadata"]
    cached_scope = result.request_cache["request_tool_scope"]
    assert trace_metadata["request_tool_scope_selected_count"] == len(captured["agent"].tools)
    assert len(trace_metadata["request_tool_scope_fingerprint"]) == 64
    assert trace_metadata["request_tool_scope_fingerprint"] == cached_scope["selection_fingerprint"]


def test_chief_blocks_required_current_state_run_when_tool_scope_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        chief_module,
        "run_typed_sdk_agent",
        lambda **_kwargs: pytest.fail("model must not run with an empty required tool scope"),
    )
    plan = ManualRequestPlan(
        source="canonical:test",
        requested_agent="chief_of_staff",
        target_agent="chief_of_staff",
        intent="route_request",
        provider_system="unspecified",
        provider_operations=["read"],
        ask_shape={"permission_state": "read_only"},
    )

    result = chief_module.run_chief_of_staff_sdk(
        "Inspect current operational state and report what is verified.",
        live=True,
        force_sdk_interpretation=True,
        manual_request_plan=plan,
    )

    assert result.live is False
    assert result.raw_result["blocked"] == "required_tool_scope_empty"
    assert result.request_cache["request_tool_scope"]["selected_tool_count"] == 0
    assert "No model or provider action ran" in result.output.summary
