from __future__ import annotations

import pytest

from keystone_agents.multi_agent_workflow_templates import (
    WorkflowRunMode,
    WorkflowToolTier,
    WorkflowTriggerType,
    build_workflow_dry_run_plan,
    get_workflow_template,
    select_workflow_templates_for_backend_context,
    workflow_template_catalog,
)


def test_catalog_defines_realistic_first_workflow_templates() -> None:
    templates = workflow_template_catalog()

    assert 5 <= len(templates) <= 8
    ids = {template.id for template in templates}
    assert "gmail_thread_research_opportunity_outreach" in ids
    assert "workspace_research_packet_refresh" in ids
    assert "rss_zotero_preprint_research_digest" in ids
    assert "weekly_opportunity_to_outreach_review" in ids
    assert "key_email_response_queue" in ids
    assert "contact_opportunity_documentation_refresh" in ids
    assert "scheduled_review_retry_loop" in ids


def test_scheduled_templates_have_acceptance_criteria_fields() -> None:
    scheduled = [
        template
        for template in workflow_template_catalog()
        if WorkflowRunMode.SCHEDULED in template.run_modes
    ]

    assert scheduled
    for template in scheduled:
        assert template.trigger_type
        assert template.cadence
        assert template.handoff_contract.context_packs
        assert template.tool_tier in WorkflowToolTier
        assert template.approval_gate
        assert template.output_destinations
        assert template.budget_stop_condition
        assert template.validation_path


def test_templates_preserve_source_approval_and_side_effect_boundaries() -> None:
    for template in workflow_template_catalog():
        contract = template.handoff_contract

        assert contract.raw_request_required is True
        assert contract.work_item_state_required is True
        assert contract.source_refs_required is True
        assert contract.approval_state_required is True
        assert "source" in template.source_attribution_contract.lower()
        assert "approval" in template.approval_gate.lower()
        assert any(
            blocked_action in template.side_effect_boundary.lower()
            for blocked_action in ("no ", "without")
        )


def test_backend_selection_uses_context_signals_not_prompt_flags() -> None:
    selected = select_workflow_templates_for_backend_context(
        trigger_type=WorkflowTriggerType.FEED_DELTA,
        available_context=["rss_feed_delta", "preprint_context_available"],
        requested_capabilities=["research"],
    )

    assert [template.id for template in selected] == [
        "rss_zotero_preprint_research_digest"
    ]
    assert all(
        "--" not in signal
        for template in selected
        for signal in template.backend_selection_signals
    )


def test_gmail_workflow_keeps_draft_creation_approval_gated() -> None:
    template = get_workflow_template("gmail_thread_research_opportunity_outreach")

    assert template is not None
    assert template.tool_tier == WorkflowToolTier.APPROVED_DRAFT_CREATION
    assert "GmailContextPack" in template.handoff_contract.context_packs
    assert "ResearchContextPack" in template.handoff_contract.context_packs
    assert "OpportunityContextPack" in template.handoff_contract.context_packs
    assert "OutreachContextPack" in template.handoff_contract.context_packs
    assert "sending is never allowed" in template.approval_gate
    assert "No email send" in template.side_effect_boundary


def test_first_combined_workflow_dry_run_preserves_typed_handoffs_without_authority() -> None:
    plan = build_workflow_dry_run_plan(
        "gmail_thread_research_opportunity_outreach",
        run_mode=WorkflowRunMode.COMBINED,
        project_id="project-kni-synthetic-001",
        source_refs=["fixture:gmail-thread:synthetic-partner"],
    )

    assert plan.status == "dry_run_ready"
    assert plan.schedule_enabled is False
    assert plan.context_packs == [
        "GmailContextPack",
        "ResearchContextPack",
        "OpportunityContextPack",
        "OutreachContextPack",
    ]
    assert plan.max_openai_requests == 0
    assert plan.max_provider_writes == 0
    assert plan.send_allowed is False
    assert plan.external_post_allowed is False


def test_scheduled_email_queue_dry_run_is_disabled_and_fixture_only() -> None:
    plan = build_workflow_dry_run_plan(
        "key_email_response_queue",
        run_mode=WorkflowRunMode.SCHEDULED,
        project_id="project-kni-synthetic-001",
        source_refs=["fixture:gmail-thread:synthetic-partner"],
    )

    assert plan.trigger_type == WorkflowTriggerType.SCHEDULE
    assert plan.cadence == "daily weekday review"
    assert plan.schedule_enabled is False
    assert plan.effective_tool_tier == WorkflowToolTier.FIXTURE_ONLY
    assert plan.output_destinations == [
        "local JSON dry-run artifact",
        "WorkItem dry-run note",
    ]
    assert plan.max_provider_reads == 0
    assert plan.max_provider_writes == 0


def test_dry_run_rejects_unknown_mode_or_missing_identity() -> None:
    with pytest.raises(ValueError, match="does not support scheduled"):
        build_workflow_dry_run_plan(
            "gmail_thread_research_opportunity_outreach",
            run_mode=WorkflowRunMode.SCHEDULED,
            project_id="project",
            source_refs=["fixture:source"],
        )
    with pytest.raises(ValueError, match="project identity and source refs"):
        build_workflow_dry_run_plan(
            "key_email_response_queue",
            run_mode=WorkflowRunMode.SCHEDULED,
            project_id="",
            source_refs=[],
        )
