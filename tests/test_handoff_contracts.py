from __future__ import annotations

from pathlib import Path

from keystone_agents.reporting import render_pipeline_report
from keystone_agents.schemas.approval import ApprovalState
from keystone_agents.schemas.handoff import (
    validate_orchestrator_to_approval_review,
    validate_outreach_composer_to_orchestrator,
)
from keystone_agents.schemas.orchestrator import HandoffSpec, OrchestratorResult
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.workflows import run_keystone_pipeline

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _run_pipeline(*, save: bool = False, database_url: str | None = None):
    return run_keystone_pipeline(
        email_fixture=FIXTURES / "sample_email_consulting.txt",
        company_fixture=FIXTURES / "sample_company_curebase.json",
        approval_state=ApprovalState.APPROVED_FOR_DRAFTING,
        dry_run=True,
        save=save,
        database_url=database_url,
        include_operator_feedback_request=True,
    )


def test_pipeline_exposes_valid_handoff_contracts() -> None:
    result = _run_pipeline()

    contracts = {contract.contract_name: contract for contract in result.handoff_contracts}

    assert set(contracts) == {
        "opportunity_scout_to_business_research_analyst",
        "business_research_analyst_to_outreach_composer",
        "outreach_composer_to_orchestrator",
    }
    assert all(contract.valid for contract in contracts.values())
    assert contracts["opportunity_scout_to_business_research_analyst"].source_ids_required
    assert contracts["business_research_analyst_to_outreach_composer"].missing_evidence_present
    assert contracts["outreach_composer_to_orchestrator"].source_ids_required
    assert set(contracts["outreach_composer_to_orchestrator"].source_ids_required) <= set(
        contracts["outreach_composer_to_orchestrator"].source_ids_present
    )
    assert any("handoff contract" in note for note in result.audit_notes)


def test_handoff_contract_report_is_human_visible() -> None:
    report = render_pipeline_report(_run_pipeline())

    assert "## Handoff Contracts" in report
    assert "## Operator Feedback Requests" in report
    assert "opportunity_scout_to_business_research_analyst" in report
    assert "business_research_analyst_to_outreach_composer" in report
    assert "outreach_composer_to_orchestrator" in report
    assert "Question: Should this artifact be approved, revised, or rejected?" in report
    assert "weak_personalization" in report


def test_outreach_handoff_flags_lost_source_id() -> None:
    result = _run_pipeline()
    assert result.outreach_draft is not None

    broken_draft = result.outreach_draft.model_copy(update={"source_ids_used": []})
    contract = validate_outreach_composer_to_orchestrator(broken_draft)

    assert contract.valid is False
    assert any(issue.field == "source_ids_present" for issue in contract.issues)


def test_outreach_handoff_preserves_unsupported_claim_flags() -> None:
    result = _run_pipeline()
    assert result.outreach_draft is not None

    draft = result.outreach_draft.model_copy(
        update={
            "unsupported_claims_flagged": ["unbacked outreach fact: invented outcome"],
            "unsupported_claim_explanations": [
                "Invented outcome is not approved source-backed context."
            ],
        }
    )
    contract = validate_outreach_composer_to_orchestrator(draft)

    assert contract.valid is True
    assert contract.unsupported_claims_required == ["unbacked outreach fact: invented outcome"]
    assert contract.unsupported_claims_present == contract.unsupported_claims_required


def test_pipeline_save_persists_handoff_metadata_for_approval_review(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'pipeline.db'}"
    _run_pipeline(save=True, database_url=database_url)
    store = SQLiteStore(database_url)

    items = store.get_pending_approvals(status=None)
    outreach_item = next(item for item in items if item.object_type == "outreach_draft")
    contracts = outreach_item.metadata["handoff_contracts"]
    feedback_request = outreach_item.metadata["operator_feedback_request"]

    assert contracts[0]["contract_name"] == "outreach_composer_to_orchestrator"
    assert contracts[0]["valid"] is True
    assert contracts[0]["source_ids"]["present"]
    assert feedback_request["object_type"] == "outreach_draft"
    assert "good_cta" in feedback_request["suggested_tags"]
    assert feedback_request["send_enabled"] is False
    assert outreach_item.metadata["send_enabled"] is False
    assert "We are evaluating external consulting support" not in str(outreach_item.metadata)


def test_orchestrator_handoff_contract_requires_target_for_specialist_route() -> None:
    result = OrchestratorResult(
        route="business_research_analyst",
        target_agent=None,
        rationale="Route ambiguous company question to business research.",
        forbidden_actions=["send_email"],
        audit_notes=["State captured for review."],
    )

    contract = validate_orchestrator_to_approval_review(result)

    assert contract.valid is False
    assert any(issue.field == "target_agent" for issue in contract.issues)


def test_orchestrator_handoff_contract_accepts_explicit_sdk_handoff_metadata() -> None:
    result = OrchestratorResult(
        route="business_research_analyst",
        target_agent="Business Research Analyst",
        rationale="Route company question to source-backed business research.",
        forbidden_actions=["send_email"],
        intended_handoffs=[
            HandoffSpec(
                route="business_research_analyst",
                agent_name="Business Research Analyst",
                builder="build_business_research_analyst_research_brief_agent",
                description="Create source-attributed research briefs.",
            )
        ],
        workflow_state_summary={"pending_approvals": 1},
        audit_notes=["State captured for review."],
    )

    contract = validate_orchestrator_to_approval_review(result)
    metadata = contract.to_approval_metadata()

    assert contract.valid is True
    assert metadata["valid"] is True
    assert metadata["contract_name"] == "orchestrator_to_approval_review"
