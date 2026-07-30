from __future__ import annotations

import json
import sys

import pytest

from keystone_agents.agents.outreach_composer import (
    build_approved_outreach_drafting_context,
    build_follow_up_schedule_record,
    build_outreach_composer_agent,
    build_outreach_composer_compact_synthesis_agent,
    build_outreach_draft_variant_set,
    check_unsupported_claims,
    compose_outreach_draft_fixture,
    compose_outreach_draft_llm_constrained,
    derive_outreach_example_query,
    derive_outreach_variant_labels,
    load_company_profile,
    load_contact_context,
    load_crm_account_context,
    load_opportunity_record,
    load_outreach_template,
    load_research_brief_profile,
    load_style_profile,
    outreach_goal_for_variant,
    retrieve_outreach_example_guidance,
)
from keystone_agents.interfaces.table_mirror import build_table_mirror_provider
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.run import SDKSynthesisOutcome
from keystone_agents.schemas.company_profile import CompanyProfile
from keystone_agents.schemas.email_style import EmailStyleProfile
from keystone_agents.schemas.outreach import (
    ApprovedOutreachDraftingContext,
    CallPrepArtifact,
    FollowUpScheduleRecord,
    OpportunityRecord,
    OutreachContext,
    OutreachDraft,
    OutreachDraftStatusResult,
    OutreachDraftVariantSet,
    OutreachExampleGuidance,
    OutreachLLMDraftPayload,
)
from keystone_agents.sdk import load_prompt
from keystone_agents.storage.sqlite_store import SQLiteStore
from scripts.export_pipeline_table import sqlite_records_for_object_type


def _fixture_draft() -> OutreachDraft:
    return compose_outreach_draft_fixture(
        company_profile=load_company_profile("sample_company_curebase"),
        opportunity_record=load_opportunity_record("sample_lead_curebase"),
        contact_context=load_contact_context("sample_contact_curebase_approved"),
        crm_context=load_crm_account_context("sample_crm_context_curebase"),
        recent_signal="decentralized clinical trial operations",
        outreach_goal="compare notes on clinical AI evaluation support",
    )


def test_compact_llm_draft_payload_ignores_harmless_extra_fields() -> None:
    payload = OutreachLLMDraftPayload.model_validate(
        {
            "company_name": "NeuroFlow",
            "email_subject": "Re: NeuroFlow",
            "email_body": "Hi [Name],\n\nHappy to compare notes if useful.\n\nSincerely,\nAnup",
            "linkedin_note": "Happy to compare notes if useful.",
            "personalization_rationale": "Used only approved thread-local context.",
            "source_ids_used": ["user_provided:thread_local_request"],
            "reply_recommended": False,
            "recommended_next_step": "Assess the collaboration hypothesis before replying.",
            "additional_information_needed": ["Licensing terms"],
            "collaboration_ideas": ["Dataset-fit assessment"],
            "deferral_reason": "The thread is closed and the idea needs more evidence.",
            "request_coverage": {
                "interpreted_request": "Return an internal decision and paste-ready note.",
                "status": "complete",
                "satisfied_dimensions": ["decision", "paste-ready note"],
                "unmet_dimensions": ["None"],
                "output_form_status": "satisfied",
                "stop_condition_status": "satisfied",
            },
            "recipient": "Review thread",
        }
    )

    assert payload.company_name == "NeuroFlow"
    assert payload.reply_recommended is False
    assert payload.collaboration_ideas == ["Dataset-fit assessment"]
    assert payload.request_coverage.status == "complete"
    assert payload.request_coverage.unmet_dimensions == []
    assert not hasattr(payload, "recipient")


def _approved_llm_context(
    *,
    blocked_facts: list[str] | None = None,
    revision_request: str | None = None,
    style: bool = False,
) -> ApprovedOutreachDraftingContext:
    return build_approved_outreach_drafting_context(
        company_profile=load_company_profile("sample_company_curebase"),
        opportunity_record=load_opportunity_record("sample_lead_curebase"),
        contact_context=load_contact_context("sample_contact_curebase_approved"),
        crm_context=load_crm_account_context("sample_crm_context_curebase"),
        email_style_profile=(
            load_style_profile("sample_email_style_profile_approved") if style else None
        ),
        objective="compare notes on clinical AI evaluation support",
        blocked_facts=blocked_facts,
        revision_request=revision_request,
    )


def test_build_outreach_composer_agent() -> None:
    agent = build_outreach_composer_agent()
    tool_names = {getattr(tool, "name", getattr(tool, "__name__", "")) for tool in agent.tools}

    assert agent.name == "outreach_composer"
    assert agent.output_type is OutreachDraft
    assert "Outreach Composer Agent" in agent.instructions
    assert {
        "load_company_profile",
        "load_research_brief_profile",
        "load_opportunity_record",
        "load_contact_context",
        "load_crm_account_context",
        "load_style_profile",
        "list_outreach_templates",
        "load_outreach_template",
        "load_email_style_profile",
        "retrieve_outreach_examples",
        "check_unsupported_claims",
        "build_approved_outreach_drafting_context",
        "compose_outreach_draft_llm_constrained",
        "build_follow_up_schedule_record",
        "create_approval_request_placeholder",
    } <= tool_names


def test_compact_synthesis_agent_omits_unused_tool_contract_prompt() -> None:
    agent = build_outreach_composer_compact_synthesis_agent(
        request_text="Review a closed Gmail thread and recommend the next step."
    )

    assert agent.tools == []
    assert "Shared Web Search Contract" not in str(agent.instructions)
    assert "Compact synthesis mode" in str(agent.instructions)
    assert "honor any stricter requested word limit" in str(agent.instructions)
    assert "retain each supplied fact that is material" in str(agent.instructions)
    assert "include the requested call to action" in str(agent.instructions)
    assert "<!-- outreach_composer_specialist_contracts/SKILL.md -->" in str(
        agent.instructions
    )
    assert "<!-- action_boundary_enforcement/SKILL.md -->" in str(agent.instructions)
    assert "<!-- tool_result_resilience/SKILL.md -->" not in str(agent.instructions)
    assert "<!-- workflow_lifecycle_tracking/SKILL.md -->" not in str(agent.instructions)


def test_internal_slack_compact_agent_uses_internal_artifact_guardrail() -> None:
    internal_agent = build_outreach_composer_compact_synthesis_agent(
        request_text="Give me an internal Slack recommendation.",
        internal_slack_copy=True,
    )
    external_agent = build_outreach_composer_compact_synthesis_agent(
        request_text="Draft an external outreach reply.",
    )

    assert [guardrail.name for guardrail in internal_agent.output_guardrails] == [
        "keystone_internal_artifact_output_safety"
    ]
    assert [guardrail.name for guardrail in external_agent.output_guardrails] == [
        "keystone_output_safety"
    ]


def test_build_outreach_composer_agent_supports_synthesis_only_mode() -> None:
    agent = build_outreach_composer_agent(include_tools=False)

    assert agent.name == "outreach_composer"
    assert agent.output_type is OutreachDraft
    assert agent.tools == []


def test_prompt_is_loaded_from_markdown() -> None:
    agent = build_outreach_composer_agent()

    assert load_prompt("outreach_composer.md").strip() in agent.instructions
    assert load_prompt("keystone_profile.md").strip() in agent.instructions


def test_research_brief_profile_loads_approved_source_context() -> None:
    profile = load_research_brief_profile("sample_company_brief_only")

    assert profile.name == "BriefOnly Health"
    assert profile.claims
    assert all(claim.source_id == "fixture:brief_only_research_brief" for claim in profile.claims)
    assert not profile.unsupported_claims_flagged


def test_curebase_research_brief_profile_loads_approved_source_context() -> None:
    profile = load_research_brief_profile("sample_company_curebase_research_brief")

    assert profile.name == "Curebase"
    assert profile.claims
    assert all(claim.source_id == "fixture:curebase_research_brief" for claim in profile.claims)
    assert not profile.unsupported_claims_flagged


def test_fixture_email_under_180_words() -> None:
    draft = _fixture_draft()

    assert len(draft.email_body.split()) < 180


def test_fixture_draft_turns_instruction_prompt_into_cta_topic() -> None:
    draft = compose_outreach_draft_fixture(
        company_profile=load_company_profile("sample_company_curebase"),
        opportunity_record=load_opportunity_record("sample_lead_curebase"),
        outreach_goal=(
            "Write a short outreach email to Curebase based only on the attached "
            "or fixture-backed research brief. Focus on Keystone's fit. Do not "
            "invent shared contacts, traction, or product details."
        ),
    )

    assert "Write a short outreach email" not in draft.email_body
    assert "mutual interest" in draft.email_body
    assert "clinical research technology" in draft.email_body


def test_email_style_profile_is_optional_by_default() -> None:
    draft = _fixture_draft()

    assert draft.style_profile_used is False
    assert draft.style_profile_id == ""
    assert draft.template_id == ""
    assert draft.template_version == ""
    assert draft.outreach_context is not None
    assert draft.outreach_context.email_style_profile is None
    assert draft.outreach_context.outreach_template is None


def test_approved_email_style_profile_guides_draft_without_enabling_send() -> None:
    style = load_style_profile("sample_email_style_profile_approved")

    draft = compose_outreach_draft_fixture(
        company_profile=load_company_profile("sample_company_curebase"),
        opportunity_record=load_opportunity_record("sample_lead_curebase"),
        contact_context=load_contact_context("sample_contact_curebase_approved"),
        email_style_profile=style,
    )

    assert draft.style_profile_used is True
    assert draft.style_profile_id == "default"
    assert draft.outreach_context is not None
    assert draft.outreach_context.email_style_profile is not None
    assert draft.email_body.startswith("Hi Dr. Priya Shah,")
    assert "Happy to compare notes" in draft.email_body
    assert draft.email_body.endswith("Sincerely,\nAnup")
    assert draft.approval_required is True
    assert draft.send_enabled is False
    assert draft.sent is False
    assert draft.can_send_email is False


def test_approved_personal_signoff_style_profile_guides_outreach_draft() -> None:
    style = load_style_profile("sample_email_style_profile_anup_approved")

    draft = compose_outreach_draft_fixture(
        company_profile=load_company_profile("sample_company_curebase"),
        opportunity_record=load_opportunity_record("sample_lead_curebase"),
        contact_context=load_contact_context("sample_contact_curebase_approved"),
        email_style_profile=style,
    )

    assert draft.style_profile_used is True
    assert draft.style_profile_id == "anup-default"
    assert draft.email_body.endswith("Sincerely,\nAnup")
    assert draft.approval_required is True
    assert draft.send_enabled is False
    assert draft.sent is False
    assert draft.can_send_email is False


def test_fixture_outreach_with_selected_template_records_template_metadata() -> None:
    template = load_outreach_template("research_workflow_intro")

    draft = compose_outreach_draft_fixture(
        company_profile=load_company_profile("sample_company_curebase"),
        opportunity_record=load_opportunity_record("sample_lead_curebase"),
        contact_context=load_contact_context("sample_contact_curebase_approved"),
        outreach_template=template,
    )

    assert draft.template_id == "research_workflow_intro"
    assert draft.template_version == "v1"
    assert "clinical research workflow outreach" in draft.template_fit_reason
    assert draft.outreach_context is not None
    assert draft.outreach_context.outreach_template == template
    assert "template research_workflow_intro guided structure only" in (
        draft.personalization_rationale
    )
    assert draft.send_enabled is False
    assert draft.sent is False
    assert draft.can_send_email is False


def test_fixture_outreach_with_retrieved_examples_records_guidance_metadata() -> None:
    company = load_company_profile("sample_company_curebase")
    opportunity = load_opportunity_record("sample_lead_curebase")
    template = load_outreach_template("low_pressure_intro")
    query = derive_outreach_example_query(
        company_profile=company,
        opportunity_record=opportunity,
        template_context=template,
        outreach_goal="compare notes on clinical AI evaluation support",
    )
    examples = retrieve_outreach_example_guidance(query, max_examples=2)

    draft = compose_outreach_draft_fixture(
        company_profile=company,
        opportunity_record=opportunity,
        contact_context=load_contact_context("sample_contact_curebase_approved"),
        outreach_template=template,
        example_guidance=examples,
    )

    assert examples
    assert draft.example_guidance_used is True
    assert draft.example_ids_used == [example.example_id for example in examples]
    assert draft.outreach_context is not None
    assert draft.outreach_context.example_guidance == examples
    assert not any(
        fact.source_id.startswith("local:approved_outreach_example") for fact in draft.facts_used
    )
    assert draft.send_enabled is False


def test_retrieved_examples_do_not_introduce_unsupported_claims() -> None:
    unsafe_example = {
        "example_id": "unsafe_prior_results_example",
        "title": "Unsafe prior results pattern",
        "tone_guidance": ["confident"],
        "structure_guidance": ["claim Keystone helped customers reduce enrollment delays"],
        "cta_guidance": "Ask for a meeting.",
        "applicability_notes": (
            "Keystone helped customers reduce enrollment delays and delivered proven results."
        ),
    }

    draft = compose_outreach_draft_fixture(
        company_profile=load_company_profile("sample_company_curebase"),
        opportunity_record=load_opportunity_record("sample_lead_curebase"),
        example_guidance=[unsafe_example],
    )
    rendered = "\n".join(
        [
            draft.email_body,
            draft.linkedin_note,
            draft.personalization_rationale,
            " ".join(draft.example_ids_used),
        ]
    )

    assert "reduce enrollment delays" not in rendered
    assert "proven results" not in rendered
    assert draft.example_ids_used == []
    assert draft.example_guidance_used is False
    assert draft.unsupported_claims_flagged == []


def test_example_guidance_cannot_enable_send() -> None:
    try:
        OutreachExampleGuidance(
            example_id="unsafe_send_example",
            title="Unsafe send example",
            structure_guidance=["Use a concise note."],
            send_enabled=True,
        )
    except ValueError as exc:
        assert "cannot include raw email bodies" in str(exc)
    else:
        raise AssertionError("example guidance must reject send_enabled=true")


def test_llm_constrained_draft_accepts_source_backed_payload() -> None:
    context = _approved_llm_context()
    payload = {
        "company_name": "Curebase",
        "contact_name": "Dr. Priya Shah",
        "contact_title": "Clinical Operations Lead",
        "email_subject": "Curebase clinical AI evaluation discussion",
        "email_body": (
            "Hello Dr. Priya Shah,\n\n"
            "I saw Curebase's decentralized clinical trial operations and thought "
            "Keystone's clinical AI evaluation and research operations focus could be "
            "relevant.\n\n"
            "Would a brief introductory conversation be useful?"
        ),
        "linkedin_note": (
            "Hello Dr. Priya Shah, open to compare notes on clinical AI evaluation "
            "and research operations?"
        ),
        "personalization_rationale": (
            "Uses approved Curebase opportunity, contact, CRM, and Keystone context."
        ),
        "source_ids_used": context.allowed_source_ids,
    }

    draft = compose_outreach_draft_llm_constrained(
        approved_context=context,
        llm_draft_payload=payload,
    )

    assert draft.drafting_mode == "llm_constrained"
    assert draft.approval_required is True
    assert draft.approval_scope == "external_use"
    assert draft.external_use_approval_state == "pending"
    assert draft.send_enabled is False
    assert draft.sent is False
    assert draft.can_send_email is False
    assert set(draft.source_ids_used) <= set(context.allowed_source_ids)
    assert draft.unsupported_claims_flagged == []


def test_llm_constrained_draft_does_not_treat_style_profile_as_factual_source() -> None:
    context = _approved_llm_context(style=True)
    assert context.email_style_profile is not None
    payload = {
        "email_subject": "Curebase clinical operations follow-up",
        "email_body": (
            "Hello Dr. Priya Shah,\n\n"
            "I saw Curebase's decentralized clinical trial operations and thought "
            "Keystone's evaluation focus could be relevant.\n\n"
            "Would a brief conversation be useful?"
        ),
        "linkedin_note": "Open to compare notes?",
        "personalization_rationale": "Used approved facts and drafting style.",
        "source_ids_used": [
            *context.allowed_source_ids,
            context.email_style_profile.source_id,
        ],
    }

    draft = compose_outreach_draft_llm_constrained(
        approved_context=context,
        llm_draft_payload=payload,
    )

    assert context.email_style_profile.source_id not in draft.source_ids_used
    assert set(draft.source_ids_used) <= set(context.allowed_source_ids)


def test_llm_constrained_draft_still_rejects_unknown_factual_source() -> None:
    context = _approved_llm_context(style=True)
    payload = {
        "email_subject": "Curebase clinical operations follow-up",
        "email_body": (
            "Hello Dr. Priya Shah,\n\n"
            "I saw Curebase's decentralized clinical trial operations.\n\n"
            "Would a brief conversation be useful?"
        ),
        "linkedin_note": "Open to compare notes?",
        "personalization_rationale": "Used approved facts.",
        "source_ids_used": [*context.allowed_source_ids, "unknown:factual-source"],
    }

    try:
        compose_outreach_draft_llm_constrained(
            approved_context=context,
            llm_draft_payload=payload,
        )
    except ValueError as exc:
        assert "unapproved source_ids" in str(exc)
    else:
        raise AssertionError("Unknown factual sources must remain rejected")


def test_llm_constrained_draft_rejects_blocked_fact() -> None:
    context = _approved_llm_context(blocked_facts=["major hospital contract"])
    payload = {
        "email_subject": "Curebase hospital contract discussion",
        "email_body": (
            "Hello Dr. Priya Shah,\n\n"
            "I saw Curebase won a major hospital contract and wanted to compare notes."
        ),
        "linkedin_note": "Hello, open to compare notes?",
        "personalization_rationale": "Uses a blocked fact.",
        "source_ids_used": context.allowed_source_ids,
    }

    try:
        compose_outreach_draft_llm_constrained(
            approved_context=context,
            llm_draft_payload=payload,
        )
    except ValueError as exc:
        assert "blocked facts" in str(exc)
    else:
        raise AssertionError("LLM drafts must reject blocked facts")


def test_llm_constrained_draft_preserves_explicit_contact_persona_metadata() -> None:
    context = _approved_llm_context()
    payload = {
        "company_name": "Curebase",
        "contact_title": "VP of Clinical Operations",
        "email_subject": "Curebase clinical trial operations discussion",
        "email_body": (
            "Hello,\n\n"
            "I saw Curebase's decentralized clinical trial operations and thought "
            "Keystone's clinical AI evaluation and research operations focus could be "
            "relevant.\n\n"
            "Would a brief exploratory conversation be useful?"
        ),
        "linkedin_note": "Hello, open to compare notes on clinical trial operations?",
        "personalization_rationale": (
            "Uses approved Curebase context and the explicit recipient persona supplied "
            "by the operator."
        ),
        "source_ids_used": context.allowed_source_ids,
    }

    draft = compose_outreach_draft_llm_constrained(
        approved_context=context,
        llm_draft_payload=payload,
    )

    assert draft.contact_title == "VP of Clinical Operations"
    assert draft.contact_name == "Dr. Priya Shah"
    assert draft.send_enabled is False


def test_llm_constrained_draft_does_not_greet_title_as_name() -> None:
    context = _approved_llm_context()
    payload = {
        "company_name": "Curebase",
        "contact_title": "VP of Clinical Operations",
        "email_subject": "Curebase clinical trial operations discussion",
        "email_body": (
            "Hi VP of Clinical Operations,\n\n"
            "I saw Curebase's decentralized clinical trial operations and thought "
            "Keystone's clinical AI evaluation focus could be relevant.\n\n"
            "Would a brief exploratory conversation be useful?"
        ),
        "linkedin_note": "Hello, open to compare notes?",
        "personalization_rationale": "Uses approved Curebase context.",
        "source_ids_used": context.allowed_source_ids,
    }

    draft = compose_outreach_draft_llm_constrained(
        approved_context=context,
        llm_draft_payload=payload,
    )

    assert draft.email_body.startswith("Hi,\n\n")
    assert not draft.email_body.startswith("Hi VP of Clinical Operations")
    assert draft.contact_title == "VP of Clinical Operations"


def test_llm_constrained_draft_requires_approved_context() -> None:
    thin_profile = CompanyProfile(
        name="Thin Context Co",
        description="Unapproved description that should not be enough for outreach.",
    )

    try:
        build_approved_outreach_drafting_context(company_profile=thin_profile)
    except ValueError as exc:
        assert "approved source-backed company or opportunity context" in str(exc)
    else:
        raise AssertionError("LLM drafting context must require approved facts")


def test_llm_constrained_draft_uses_approved_style_profile() -> None:
    context = _approved_llm_context(style=True)
    payload = {
        "email_subject": "Curebase research workflow discussion",
        "email_body": (
            "Hello Dr. Priya Shah,\n\n"
            "I saw Curebase's decentralized clinical trial operations. Happy to compare "
            "notes on clinical AI evaluation support if useful.\n\n"
            "Warmly,\nKeystone"
        ),
        "linkedin_note": "Hello Dr. Priya Shah, happy to compare notes if useful.",
        "personalization_rationale": "Follows the approved style profile and source context.",
        "source_ids_used": context.allowed_source_ids,
    }

    draft = compose_outreach_draft_llm_constrained(
        approved_context=context,
        llm_draft_payload=payload,
    )

    assert draft.style_profile_used is True
    assert draft.style_profile_id == "default"
    assert "Happy to compare notes" in draft.email_body
    assert "Warmly," in draft.email_body
    assert draft.send_enabled is False


def test_llm_constrained_draft_records_revision_request() -> None:
    context = _approved_llm_context(revision_request="Make the note shorter and direct.")
    payload = {
        "email_subject": "Curebase research workflow discussion",
        "email_body": (
            "Hello Dr. Priya Shah,\n\n"
            "I saw Curebase's decentralized clinical trial operations. Would a brief "
            "conversation on clinical AI evaluation support be useful?"
        ),
        "linkedin_note": "Hello Dr. Priya Shah, open to compare notes?",
        "personalization_rationale": "Revision keeps the ask shorter and source-backed.",
        "source_ids_used": context.allowed_source_ids,
    }

    draft = compose_outreach_draft_llm_constrained(
        approved_context=context,
        llm_draft_payload=payload,
    )

    assert draft.revision_request == "Make the note shorter and direct."
    assert len(draft.email_body.split()) < 60
    assert draft.source_ids_used


def test_selected_draft_revision_preserves_identity_cta_recipient_and_word_limit() -> None:
    cta = "Would a brief conversation be useful?"
    context = build_approved_outreach_drafting_context(
        company_profile=load_company_profile("sample_company_curebase"),
        opportunity_record=load_opportunity_record("sample_lead_curebase"),
        contact_context=load_contact_context("sample_contact_curebase_approved"),
        objective="Revise the selected approved draft to 80 words and keep the CTA.",
        revision_request="Use at most 80 words and preserve the same CTA.",
        selected_draft={
            "draft_id": "draft-approved-1",
            "recipient": "Dr. Priya Shah",
            "email_subject": "Curebase research workflow discussion",
            "email_body": f"Hello Dr. Priya Shah,\n\nPrior copy. {cta}",
            "cta_text": cta,
        },
        revision_max_words=80,
        preserve_selected_cta=True,
    )
    payload = {
        "email_subject": "Curebase research workflow discussion",
        "email_body": (
            "Hello Dr. Priya Shah,\n\n"
            "I saw Curebase's decentralized clinical trial operations. "
            f"{cta}"
        ),
        "linkedin_note": "",
        "personalization_rationale": "Kept the approved source-backed context.",
        "source_ids_used": context.allowed_source_ids,
    }

    draft = compose_outreach_draft_llm_constrained(
        approved_context=context,
        llm_draft_payload=payload,
    )

    assert draft.revised_from_draft_id == "draft-approved-1"
    assert draft.recipient == "Dr. Priya Shah"
    assert cta in draft.email_body
    assert len(draft.email_body.split()) <= 80
    assert draft.send_enabled is False


def test_selected_draft_revision_rejects_constraint_drift() -> None:
    cta = "Would a brief conversation be useful?"
    context = build_approved_outreach_drafting_context(
        company_profile=load_company_profile("sample_company_curebase"),
        opportunity_record=load_opportunity_record("sample_lead_curebase"),
        contact_context=load_contact_context("sample_contact_curebase_approved"),
        objective="Revise the selected approved draft.",
        selected_draft={
            "draft_id": "draft-approved-1",
            "recipient": "Dr. Priya Shah",
            "email_subject": "Curebase research workflow discussion",
            "email_body": f"Hello Dr. Priya Shah,\n\nPrior copy. {cta}",
            "cta_text": cta,
        },
        revision_max_words=12,
        preserve_selected_cta=True,
    )
    base_payload = {
        "email_subject": "Curebase research workflow discussion",
        "email_body": "Hello Dr. Priya Shah, this revision changes the call to action.",
        "linkedin_note": "",
        "personalization_rationale": "Uses approved context.",
        "source_ids_used": context.allowed_source_ids,
    }

    with pytest.raises(ValueError, match="preserve the selected CTA"):
        compose_outreach_draft_llm_constrained(
            approved_context=context,
            llm_draft_payload=base_payload,
        )

    recipient_drift = dict(base_payload)
    recipient_drift["recipient"] = "Different Recipient"
    recipient_drift["email_body"] = f"Hello. {cta}"
    with pytest.raises(ValueError, match="preserve the selected recipient"):
        compose_outreach_draft_llm_constrained(
            approved_context=context,
            llm_draft_payload=recipient_drift,
        )

    over_limit = dict(base_payload)
    over_limit["email_body"] = " ".join(["approved"] * 13) + f" {cta}"
    with pytest.raises(ValueError, match="exceeds the selected revision word limit"):
        compose_outreach_draft_llm_constrained(
            approved_context=context,
            llm_draft_payload=over_limit,
        )


def test_diverse_approved_founder_note_is_source_backed_reviewable_and_never_sendable() -> None:
    context = _approved_llm_context(style=True)
    payload = {
        "email_subject": "Curebase research workflow discussion",
        "email_body": (
            "Hello Dr. Priya Shah,\n\n"
            "I saw Curebase's decentralized clinical trial operations. Happy to compare "
            "notes on clinical AI evaluation support if useful.\n\n"
            "Warmly,\nKeystone"
        ),
        "linkedin_note": (
            "Hello Dr. Priya Shah, happy to compare notes on clinical AI evaluation "
            "support if useful."
        ),
        "personalization_rationale": (
            "Used the approved company evidence, recipient persona, and warm style profile."
        ),
        "source_ids_used": context.allowed_source_ids,
    }

    draft = compose_outreach_draft_llm_constrained(
        approved_context=context,
        llm_draft_payload=OutreachLLMDraftPayload.model_validate(payload),
    )

    assert draft.recipient == "Dr. Priya Shah"
    assert draft.source_ids_used
    assert draft.facts_used
    assert draft.style_profile_used is True
    assert draft.approved_context_used is True
    assert draft.approval_required is True
    assert draft.send_enabled is False
    assert draft.sent is False
    assert draft.can_send_email is False


def test_outreach_variant_labels_parse_from_goal() -> None:
    labels = derive_outreach_variant_labels(
        objective="Write three versions: formal, warm-professional, and very concise.",
        max_variants=3,
    )

    assert labels == ["formal", "warm-professional", "very concise"]


def test_outreach_variant_goal_keeps_base_prompt_and_adds_tone_request() -> None:
    goal = outreach_goal_for_variant(
        objective="Write a short outreach email to Curebase based only on the brief.",
        variant_label="warm-professional",
    )

    assert "Write a short outreach email to Curebase" in goal
    assert "Variant tone request: warm-professional." in goal
    assert "Vary only wording, structure, and tone." in goal


def test_outreach_variant_set_wraps_validated_drafts() -> None:
    context = _approved_llm_context()
    labels = ["formal", "warm-professional", "very concise"]
    drafts = [
        compose_outreach_draft_llm_constrained(
            approved_context=context.model_copy(
                update={
                    "objective": outreach_goal_for_variant(
                        objective=context.objective,
                        variant_label=label,
                    )
                }
            ),
            llm_draft_payload={
                "email_subject": f"Curebase discussion ({label})",
                "email_body": (
                    f"Hello Dr. Priya Shah,\n\n"
                    f"This is the {label} version grounded in approved Curebase context."
                ),
                "linkedin_note": f"Hello Dr. Priya Shah, {label} note.",
                "personalization_rationale": f"Uses approved context with a {label} tone.",
                "source_ids_used": context.allowed_source_ids,
            },
        )
        for label in labels
    ]

    variant_set = build_outreach_draft_variant_set(
        approved_context=context,
        variant_labels=labels,
        drafts=drafts,
    )

    assert isinstance(variant_set, OutreachDraftVariantSet)
    assert [item.variant_label for item in variant_set.variants] == labels
    assert all(item.draft.approval_required is True for item in variant_set.variants)
    assert all(item.draft.send_enabled is False for item in variant_set.variants)
    assert variant_set.send_enabled is False


def test_llm_constrained_draft_rejects_em_dash() -> None:
    context = _approved_llm_context()
    payload = {
        "email_subject": "Curebase research workflow discussion",
        "email_body": "Hello Dr. Priya Shah, Curebase works in trials \u2014 open to talk?",
        "linkedin_note": "Hello, open to compare notes?",
        "personalization_rationale": "Uses approved context.",
        "source_ids_used": context.allowed_source_ids,
    }

    try:
        compose_outreach_draft_llm_constrained(
            approved_context=context,
            llm_draft_payload=payload,
        )
    except ValueError as exc:
        assert "em dashes" in str(exc)
    else:
        raise AssertionError("LLM drafts must reject em dashes")


def test_llm_constrained_path_keeps_deterministic_fallback() -> None:
    context = _approved_llm_context()

    draft = compose_outreach_draft_llm_constrained(approved_context=context)

    assert draft.drafting_mode == "deterministic_fixture"
    assert draft.approved_context_used is True
    assert draft.send_enabled is False
    assert "Curebase" in draft.email_body


def test_unapproved_email_style_profile_is_flagged_and_not_used() -> None:
    style = EmailStyleProfile(
        profile_id="pending-style",
        approval_state="pending",
        greeting_patterns=["Yo {name},"],
        signoffs=["Later,"],
        preferred_phrases=["unapproved phrase"],
    )

    draft = compose_outreach_draft_fixture(
        company_profile=load_company_profile("sample_company_curebase"),
        opportunity_record=load_opportunity_record("sample_lead_curebase"),
        contact_context=load_contact_context("sample_contact_curebase_approved"),
        email_style_profile=style,
    )

    assert draft.style_profile_used is False
    assert "Yo Dr. Priya Shah" not in draft.email_body
    assert "unapproved phrase" not in draft.email_body
    assert any("email style profile" in claim for claim in draft.unsupported_claims_flagged)


def test_email_style_profile_rejects_raw_body_storage_and_long_samples() -> None:
    try:
        EmailStyleProfile(
            profile_id="unsafe-style",
            approval_state="approved_for_drafting",
            raw_sent_email_bodies_included=True,
        )
    except ValueError as exc:
        assert "raw bodies" in str(exc)
    else:
        raise AssertionError("style profiles must reject raw sent-email body storage")

    try:
        EmailStyleProfile(
            profile_id="long-sample",
            approval_state="approved_for_drafting",
            approved_sample_snippets=["word " * 80],
        )
    except ValueError as exc:
        assert "short excerpts" in str(exc)
    else:
        raise AssertionError("style profiles must reject long sample snippets")


def test_fixture_linkedin_note_under_300_characters() -> None:
    draft = _fixture_draft()

    assert len(draft.linkedin_note) < 300


def test_fixture_draft_has_no_em_dash() -> None:
    draft = _fixture_draft()

    assert "\u2014" not in draft.email_subject
    assert "\u2014" not in draft.email_body
    assert "\u2014" not in draft.linkedin_note


def test_fixture_draft_requires_pending_approval() -> None:
    draft = _fixture_draft()

    assert draft.approval_required is True
    assert draft.approval_state == "pending"
    assert draft.approval_scope == "external_use"
    assert draft.external_use_approval_state == "pending"
    assert draft.external_use_allowed is False
    assert "external use" in draft.approval_rationale


def test_personalization_rationale_not_empty() -> None:
    draft = _fixture_draft()

    assert draft.personalization_rationale.strip()


def test_facts_used_reference_approved_claim_sources() -> None:
    draft = _fixture_draft()

    assert draft.facts_used
    assert all(fact.claim_text for fact in draft.facts_used)
    assert all(fact.source_id for fact in draft.facts_used)
    assert any(fact.source_id.startswith("fixture:") for fact in draft.facts_used)
    assert not any(fact.source_id.startswith("unbacked:") for fact in draft.facts_used)
    assert any(fact.source_id == "fixture:contact_curebase_priya" for fact in draft.facts_used)
    assert any(fact.source_id == "fixture:crm_context_curebase" for fact in draft.facts_used)


def test_outreach_context_records_approved_inputs_and_blocked_facts() -> None:
    draft = _fixture_draft()

    context = draft.outreach_context

    assert isinstance(context, OutreachContext)
    assert context.company_profile is not None
    assert context.company_profile.name == "Curebase"
    assert context.opportunity_record is not None
    assert context.opportunity_record.company_name == "Curebase"
    assert context.contact_context is not None
    assert context.contact_context.contact_name == "Dr. Priya Shah"
    assert context.crm_context is not None
    assert context.crm_context.company_name == "Curebase"
    assert context.allowed_keystone_positioning
    assert context.facts_used == draft.facts_used
    assert context.blocked_facts == draft.blocked_facts
    assert context.approval_state == "pending"
    assert context.approved_context_used is True


def test_approved_contact_context_personalizes_draft() -> None:
    draft = compose_outreach_draft_fixture(
        company_profile=load_company_profile("sample_company_curebase"),
        opportunity_record=load_opportunity_record("sample_lead_curebase"),
        contact_context=load_contact_context("sample_contact_curebase_approved"),
    )

    assert draft.contact_name == "Dr. Priya Shah"
    assert draft.contact_title == "Clinical Operations Lead"
    assert "Dr. Priya Shah" in draft.email_body
    assert "Clinical Operations Lead" in draft.email_body
    assert draft.approval_required is True


def test_unapproved_contact_context_is_flagged_and_not_used() -> None:
    draft = compose_outreach_draft_fixture(
        company_profile=load_company_profile("sample_company_curebase"),
        opportunity_record=load_opportunity_record("sample_lead_curebase"),
        contact_context=load_contact_context("sample_contact_curebase_unapproved"),
    )

    assert draft.contact_name is None
    assert "Unreviewed Prospect" not in draft.email_body
    assert "Unverified Decision Maker" not in draft.email_body
    assert any("contact context" in claim for claim in draft.unsupported_claims_flagged)
    assert draft.approved_context_used is True
    assert draft.source_ids_used
    assert set(draft.source_ids_used) <= {fact.source_id for fact in draft.facts_used}
    assert "source" in draft.personalization_rationale.lower()


def test_explicit_contact_fields_are_used_when_unapproved_contact_record_is_ignored() -> None:
    draft = compose_outreach_draft_fixture(
        company_profile=load_company_profile("sample_company_curebase"),
        opportunity_record=load_opportunity_record("sample_lead_curebase"),
        contact_name="Dr. Explicit Reviewer",
        contact_title="VP Clinical Operations",
        contact_context=load_contact_context("sample_contact_curebase_unapproved"),
    )

    assert draft.contact_name == "Dr. Explicit Reviewer"
    assert draft.contact_title == "VP Clinical Operations"
    assert "Dr. Explicit Reviewer" in draft.email_body
    assert "VP Clinical Operations" in draft.email_body
    assert "Unreviewed Prospect" not in draft.email_body
    assert "Unverified Decision Maker" not in draft.email_body
    assert any("contact context" in claim for claim in draft.unsupported_claims_flagged)
    assert any(fact.source_id == "user:contact_context" for fact in draft.facts_used)


def test_unapproved_context_is_rejected() -> None:
    thin_profile = CompanyProfile(
        name="Thin Context Co",
        description="Unapproved description that should not be enough for outreach.",
    )

    try:
        compose_outreach_draft_fixture(company_profile=thin_profile)
    except ValueError as exc:
        assert "approved source-backed company or opportunity context" in str(exc)
    else:
        raise AssertionError("unapproved outreach context must not create a draft")


def test_unapproved_opportunity_context_is_explained_and_not_used() -> None:
    opportunity = OpportunityRecord(
        company_name="Curebase",
        title="Unapproved case study angle",
        notes="Won a major hospital contract.",
        approved_for_outreach=False,
    )

    draft = compose_outreach_draft_fixture(
        company_profile=load_company_profile("sample_company_curebase"),
        opportunity_record=opportunity,
    )

    assert "major hospital contract" not in draft.email_body
    assert any("unbacked opportunity context" in item for item in draft.unsupported_claims_flagged)
    assert draft.unsupported_claim_explanations


def test_unsupported_prior_experience_claim_is_flagged() -> None:
    result = check_unsupported_claims(
        "Keystone has helped clinical trial companies reduce enrollment delays.",
        allowed_claims=[],
    )

    assert result["has_unsupported_claims"] is True
    assert result["unsupported_claims"]
    assert result["unsupported_claim_explanations"]


def test_unbacked_recent_signal_is_flagged_not_silently_used() -> None:
    draft = compose_outreach_draft_fixture(
        company_profile=load_company_profile("sample_company_curebase"),
        opportunity_record=load_opportunity_record("sample_lead_curebase"),
        recent_signal="won a major hospital contract",
    )

    assert any("unbacked recent signal" in claim for claim in draft.unsupported_claims_flagged)
    assert draft.unsupported_claim_explanations
    assert "major hospital contract" not in draft.email_body


def test_outreach_draft_does_not_enable_send_fields() -> None:
    draft = _fixture_draft()

    assert draft.send_enabled is False
    assert draft.sent is False
    assert draft.can_send_email is False
    assert draft.external_use_allowed is False


def test_call_prep_is_optional_by_default() -> None:
    draft = _fixture_draft()

    assert draft.call_prep is None


def test_follow_up_schedule_is_optional_data_only() -> None:
    draft = compose_outreach_draft_fixture(
        company_profile=load_company_profile("sample_company_curebase"),
        opportunity_record=load_opportunity_record("sample_lead_curebase"),
        include_follow_up_schedule=True,
        follow_up_date="2026-05-01",
    )

    assert len(draft.follow_up_schedules) == 1
    schedule = draft.follow_up_schedules[0]
    assert isinstance(schedule, FollowUpScheduleRecord)
    assert schedule.company_name == "Curebase"
    assert schedule.proposed_date == "2026-05-01"
    assert schedule.status == "recommended"
    assert schedule.approval_required is True
    assert schedule.send_enabled is False
    assert schedule.sent is False
    assert schedule.gmail_scheduled is False
    assert schedule.background_job_created is False


def test_follow_up_schedule_rejects_send_or_background_flags() -> None:
    for field in ("send_enabled", "sent", "gmail_scheduled", "background_job_created"):
        try:
            FollowUpScheduleRecord(
                company="Curebase",
                proposed_date="2026-05-01",
                rationale="Recommend manual review after the approved draft.",
                **{field: True},
            )
        except ValueError as exc:
            assert "data-only" in str(exc)
        else:
            raise AssertionError(f"follow-up schedule must reject {field}=true")


def test_follow_up_schedule_tool_builds_data_only_record() -> None:
    draft = _fixture_draft()
    schedule = build_follow_up_schedule_record(
        draft,
        proposed_date="2026-05-01",
        related_draft_id="draft-1",
    )

    assert schedule.company == "Curebase"
    assert schedule.related_draft_id == "draft-1"
    assert schedule.proposed_date == "2026-05-01"
    assert schedule.approval_required is True
    assert schedule.send_enabled is False


def test_call_prep_uses_only_source_backed_internal_context() -> None:
    draft = compose_outreach_draft_fixture(
        company_profile=load_company_profile("sample_company_curebase"),
        opportunity_record=load_opportunity_record("sample_lead_curebase"),
        contact_context=load_contact_context("sample_contact_curebase_approved"),
        include_call_prep=True,
    )

    assert draft.call_prep is not None
    assert draft.call_prep.draft_only_internal is True
    assert draft.call_prep.approval_required is True
    assert draft.call_prep.discovery_questions
    assert draft.call_prep.meeting_objectives
    assert draft.call_prep.known_facts
    assert draft.call_prep.unknowns
    assert draft.call_prep.risks
    assert draft.call_prep.suggested_next_step
    assert set(draft.call_prep.source_ids_used) <= set(draft.source_ids_used)
    assert all(fact.approved and fact.confidence > 0 for fact in draft.call_prep.known_facts)
    assert not any(
        fact.source_id.startswith(("unbacked:", "user:")) for fact in draft.call_prep.known_facts
    )


def test_fixture_bounds_long_workflow_objective_before_building_email() -> None:
    long_objective = " ".join(
        ["Draft a careful follow-up that preserves every workflow constraint"] * 60
    )

    draft = compose_outreach_draft_fixture(
        company_profile=load_company_profile("sample_company_curebase"),
        opportunity_record=load_opportunity_record("sample_lead_curebase"),
        outreach_goal=long_objective,
    )

    assert len(draft.outreach_goal.split()) <= 32
    assert len(draft.email_body.split()) <= 180
    assert draft.approval_required is True
    assert draft.send_enabled is False


def test_call_prep_excludes_unbacked_personalization() -> None:
    draft = compose_outreach_draft_fixture(
        company_profile=load_company_profile("sample_company_curebase"),
        opportunity_record=load_opportunity_record("sample_lead_curebase"),
        recent_signal="won a major hospital contract",
        include_call_prep=True,
    )

    assert draft.call_prep is not None
    rendered = " ".join(
        [
            *draft.call_prep.discovery_questions,
            *draft.call_prep.meeting_objectives,
            *(fact.claim_text for fact in draft.call_prep.known_facts),
            *draft.call_prep.unknowns,
            *draft.call_prep.risks,
            draft.call_prep.suggested_next_step,
        ]
    )
    assert "major hospital contract" not in rendered
    assert any("recent signal" in claim for claim in draft.unsupported_claims_flagged)


def test_call_prep_rejects_professional_advice() -> None:
    for unsafe in (
        "medical advice",
        "legal advice",
        "tax advice",
        "regulatory advice",
    ):
        try:
            CallPrepArtifact(
                discovery_questions=[f"Provide {unsafe}."],
                suggested_next_step="Review internally before any follow-up.",
            )
        except ValueError as exc:
            assert "professional advice" in str(exc)
        else:
            raise AssertionError(f"call prep must reject {unsafe}")


def test_cli_optionally_includes_call_prep(monkeypatch, capsys) -> None:
    import scripts.run_outreach_draft as cli

    monkeypatch.setattr(
        sys,
        "argv",
        ["run_outreach_draft.py", "--include-call-prep", "--json"],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    call_prep = payload["draft"]["call_prep"]

    assert payload["draft_created"] is True
    assert call_prep["draft_only_internal"] is True
    assert call_prep["approval_required"] is True
    assert call_prep["discovery_questions"]
    assert call_prep["known_facts"]


def test_outreach_status_result_stays_approval_gated_and_no_send() -> None:
    result = OutreachDraftStatusResult(
        status="clarification_required",
        reason="Need a more specific outreach objective.",
        clarification_request="State the outreach angle and what Keystone should discuss.",
        missing_requirements=["specific outreach objective"],
        recommended_next_action="Add a specific outreach goal and rerun drafting.",
        approval_scope="external_use",
    )

    assert result.approval_required is True
    assert result.send_enabled is False
    assert result.draft_created is False
    assert result.approval_scope == "external_use"


def test_cli_generic_goal_returns_clarification_result(monkeypatch, capsys) -> None:
    import scripts.run_outreach_draft as cli

    monkeypatch.setattr(
        sys,
        "argv",
        ["run_outreach_draft.py", "--goal", "reach out", "--json"],
    )

    assert cli.main() == 0
    payload = OutreachDraftStatusResult.model_validate(json.loads(capsys.readouterr().out))

    assert payload.status == "clarification_required"
    assert "specific outreach objective" in payload.missing_requirements
    assert payload.send_enabled is False
    assert payload.draft_created is False


def test_cli_research_brief_fixture_requires_sdk_synthesis(monkeypatch) -> None:
    import scripts.run_outreach_draft as cli

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_outreach_draft.py",
            "--research-brief-fixture",
            "sample_company_brief_only",
            "--json",
        ],
    )

    try:
        cli.main()
    except SystemExit as exc:
        assert "--research-brief-fixture requires --run-sdk or --live-sdk" in str(exc)
    else:
        raise AssertionError("brief-only OC-1 drafting must use SDK synthesis")


def test_cli_sdk_missing_approved_context_returns_blocked_result(
    monkeypatch,
    capsys,
) -> None:
    import scripts.run_outreach_draft as cli

    def fake_run_sdk_synthesis(args):
        raise ValueError("approved source-backed company or opportunity context is required")

    monkeypatch.setattr(cli, "_run_sdk_synthesis", fake_run_sdk_synthesis)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_outreach_draft.py", "--run-sdk", "--json"],
    )

    assert cli.main() == 0
    payload = OutreachDraftStatusResult.model_validate(json.loads(capsys.readouterr().out))

    assert payload.status == "blocked"
    assert "approved source-backed context" in payload.reason.lower()
    assert payload.send_enabled is False


def test_cli_sdk_failure_returns_structured_no_side_effect_payload(
    monkeypatch,
    capsys,
) -> None:
    import scripts.run_outreach_draft as cli

    class FakeSDKFailure(RuntimeError):
        pass

    failure = FakeSDKFailure("guardrail rejected output")
    failure.keystone_sdk_run_failure = {
        "schema": "keystone.sdk_run_failure.v1",
        "run_mode": "live_sdk",
        "failure_kind": "output_guardrail_tripwire_triggered",
        "usage": {"available": False, "requests": 1},
        "cost": {"available": False},
        "request_cache": {"failed_model_attempts": 1},
        "guardrail": {
            "risk_flags": ["unsupported_claim"],
            "reasons": ["unsupported outreach claim: proven results"],
        },
    }

    def fake_run_sdk_synthesis(args):
        raise failure

    monkeypatch.setattr(cli, "_run_sdk_synthesis", fake_run_sdk_synthesis)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_outreach_draft.py", "--live-sdk", "--json"],
    )

    assert cli.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["usage"]["requests"] == 1
    assert payload["sdk_failure"]["guardrail"]["risk_flags"] == ["unsupported_claim"]
    assert payload["draft_created"] is False
    assert payload["send_enabled"] is False


def test_cli_run_sdk_can_return_multiple_validated_variants(monkeypatch, capsys) -> None:
    import scripts.run_outreach_draft as cli

    calls: list[tuple[str | None, str | None, bool]] = []
    company_profile = load_research_brief_profile("sample_company_curebase_research_brief")

    def fake_run_single_sdk_synthesis(
        args,
        *,
        run_config,
        live: bool,
        founder_fit_profile,
        objective_override: str | None = None,
        variant_label: str | None = None,
    ) -> SDKSynthesisOutcome:
        calls.append((objective_override, variant_label, live))
        draft = OutreachDraft(
            company_name="Curebase",
            email_subject=f"Curebase note ({variant_label})",
            email_body=(
                f"Hello Dr. Priya Shah,\n\n"
                f"This is the {variant_label} version grounded in the approved research brief."
            ),
            linkedin_note=f"Hello Dr. Priya Shah, {variant_label} note.",
            personalization_rationale=f"Uses approved brief context with a {variant_label} tone.",
            approval_required=True,
            approval_state="pending",
            approval_scope="external_use",
            source_ids_used=["fixture:curebase_research_brief", "keystone_profile"],
        )
        return SDKSynthesisOutcome(
            agent_name="outreach_composer",
            raw_context={
                "company_profile": company_profile,
                "research_brief_only": True,
                "context_policy": "Attached approved research brief only.",
            },
            typed_input=None,
            result=TypedAgentRunResult(
                agent_name="outreach_composer",
                output=draft,
                raw_result={"local": True},
                live=live,
            ),
            model_provider="gemini",
            model_name="gemini-2.5-flash",
            model_run_mode="sdk-local",
            usage={
                "available": True,
                "requests": 1,
                "input_tokens": 100,
                "output_tokens": 40,
                "total_tokens": 140,
                "cached_input_tokens": 0,
                "reasoning_output_tokens": 0,
            },
            cost={
                "amount_usd": 0.001,
                "estimated_usd": 0.001,
                "currency": "USD",
                "source": "local_pricing_table",
                "billable_tokens": {
                    "input_tokens": 100,
                    "cached_input_tokens": 0,
                    "output_tokens": 40,
                },
                "components_usd": {"input": 0.0005, "cached_input": 0.0, "output": 0.0005},
            },
            request_cache={
                "request_layout": "static_agent_prefix_then_dynamic_typed_input",
                "static_prefix_sha256": "a" * 64,
                "instructions_sha256": "b" * 64,
                "tool_names_sha256": "c" * 64,
                "output_schema_sha256": "d" * 64,
                "tool_count": 8,
                "dynamic_prompt_chars": 1000,
                "session_attached": False,
            },
        )

    monkeypatch.setattr(cli, "SDK_RUN_CONFIG_FACTORY", lambda: object())
    monkeypatch.setattr(cli, "_run_single_sdk_synthesis", fake_run_single_sdk_synthesis)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_outreach_draft.py",
            "--research-brief-fixture",
            "sample_company_curebase_research_brief",
            "--goal",
            "Write three versions: formal, warm-professional, and very concise.",
            "--max-variants",
            "3",
            "--run-sdk",
            "--json",
        ],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["mode"] == "sdk-synthesis"
    assert payload["output_type"] == "OutreachDraftVariantSet"
    assert payload["variant_labels"] == ["formal", "warm-professional", "very concise"]
    assert len(payload["output"]["variants"]) == 3
    assert payload["output"]["variants"][0]["draft"]["approval_required"] is True
    assert payload["output"]["variants"][0]["draft"]["send_enabled"] is False
    assert payload["usage"]["cache_hit_rate"] == 0.0
    assert payload["request_cache"]["run_count"] == 3
    assert payload["request_cache"]["static_prefix_stable"] is True
    assert payload["request_cache"]["dynamic_prompt_chars"] == 3000
    assert len(calls) == 3
    assert all(live is False for _, _, live in calls)


def test_cli_live_sdk_uses_single_pass_variant_synthesis(monkeypatch, capsys) -> None:
    import scripts.run_outreach_draft as cli

    monkeypatch.setenv("KEYSTONE_OPENAI_API_KEY", "test-only-openai-key")
    calls: list[tuple[bool, list[str]]] = []
    company_profile = load_research_brief_profile("sample_company_curebase_research_brief")
    approved_context = build_approved_outreach_drafting_context(
        company_profile=company_profile,
        objective="Write three versions: formal, warm-professional, and very concise.",
        max_variants=3,
    )
    variant_labels = ["formal", "warm-professional", "very concise"]
    variant_set = build_outreach_draft_variant_set(
        approved_context=approved_context,
        variant_labels=variant_labels,
        drafts=[
            OutreachDraft(
                company_name="Curebase",
                email_subject=f"Curebase note ({label})",
                email_body=f"Hello, this is the {label} version using approved context.",
                linkedin_note=f"Hello, {label} note.",
                personalization_rationale=f"Uses approved context with {label} tone.",
                source_ids_used=["fixture:curebase_research_brief", "keystone_profile"],
            )
            for label in variant_labels
        ],
    )

    def fake_run_compact_variant_set_sdk_synthesis(
        args,
        *,
        run_config,
        live: bool,
        founder_fit_profile,
        variant_labels: list[str],
    ) -> SDKSynthesisOutcome:
        calls.append((live, variant_labels))
        return SDKSynthesisOutcome(
            agent_name="outreach_composer",
            raw_context={"company_profile": company_profile},
            typed_input=None,
            result=TypedAgentRunResult(
                agent_name="outreach_composer",
                output=variant_set,
                raw_result={"local": True},
                live=live,
            ),
            model_provider="openai",
            model_name="gpt-5.4-mini",
            model_run_mode="live_sdk",
            usage={
                "available": True,
                "requests": 1,
                "input_tokens": 100,
                "output_tokens": 40,
                "total_tokens": 140,
                "cached_input_tokens": 0,
                "reasoning_output_tokens": 0,
            },
            cost={"source": "local_pricing_table", "amount_usd": 0.001},
            request_cache={
                "request_layout": "static_agent_prefix_then_dynamic_typed_input",
                "static_prefix_sha256": "e" * 64,
                "dynamic_prompt_chars": 1200,
                "session_attached": False,
            },
        )

    monkeypatch.setattr(
        cli,
        "_run_compact_variant_set_sdk_synthesis",
        fake_run_compact_variant_set_sdk_synthesis,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_outreach_draft.py",
            "--research-brief-fixture",
            "sample_company_curebase_research_brief",
            "--goal",
            "Write three versions: formal, warm-professional, and very concise.",
            "--max-variants",
            "3",
            "--live-sdk",
            "--json",
        ],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert calls == [(True, variant_labels)]
    assert payload["usage"]["requests"] == 1
    assert payload["request_cache"]["static_prefix_sha256"] == "e" * 64
    assert payload["audit_notes"][0] == "Generated 3 constrained outreach variants in one SDK call."
    assert len(payload["output"]["variants"]) == 3


def test_cli_optionally_includes_follow_up_schedule(monkeypatch, capsys) -> None:
    import scripts.run_outreach_draft as cli

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_outreach_draft.py",
            "--include-follow-up-schedule",
            "--follow-up-date",
            "2026-05-01",
            "--json",
        ],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    schedules = payload["draft"]["follow_up_schedules"]

    assert payload["draft_created"] is True
    assert schedules[0]["proposed_date"] == "2026-05-01"
    assert schedules[0]["approval_required"] is True
    assert schedules[0]["send_enabled"] is False
    assert schedules[0]["background_job_created"] is False


def test_template_example_save_and_table_export_omit_full_email_bodies(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    import scripts.run_outreach_draft as cli

    database_url = f"sqlite:///{tmp_path / 'outreach-template-example.db'}"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_outreach_draft.py",
            "--template-id",
            "low_pressure_intro",
            "--use-example-rag",
            "--example-query",
            "clinical AI evaluation compare notes",
            "--save",
            "--database-url",
            database_url,
            "--json",
        ],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    draft_body = payload["draft"]["email_body"]
    store = SQLiteStore(database_url)
    runs = store.fetch_all("agent_runs")
    assert len(runs) == 1
    run_json = json.dumps(runs, sort_keys=True)
    assert '"email_body":' not in runs[0]["output_json"]
    assert draft_body not in run_json
    assert "template_id" in runs[0]["output_json"]
    assert "example_ids_used" in runs[0]["output_json"]

    records = sqlite_records_for_object_type(
        "drafts",
        database_url=database_url,
    )
    export = build_table_mirror_provider("dry-run").export_records("drafts", records)
    export_json = json.dumps(export.model_dump(mode="json"), sort_keys=True)

    assert draft_body not in export_json
    assert "[omitted: full body is not included in exports]" in export_json
    assert "Template ID" in export_json
    assert "Example IDs Used" in export_json


def test_external_use_approval_still_does_not_enable_sending() -> None:
    draft = OutreachDraft(
        company_name="Curebase",
        email_subject="Curebase research workflow discussion",
        email_body="Hello, I noticed Curebase's source-backed research workflow context.",
        linkedin_note="Hello, open to a brief exchange on clinical AI workflows?",
        personalization_rationale="Uses approved fixture context.",
        approval_required=True,
        approval_state="approved_for_external_use",
        approval_scope="external_use",
    )

    assert draft.external_use_allowed is True
    assert draft.send_enabled is False
    assert draft.sent is False
    assert draft.can_send_email is False


def test_outreach_draft_schema_rejects_send_enabled() -> None:
    try:
        OutreachDraft(
            company_name="Curebase",
            email_subject="Curebase research workflow discussion",
            email_body="Hello, I noticed Curebase's source-backed research workflow context.",
            linkedin_note="Hello, open to a brief exchange on clinical AI workflows?",
            personalization_rationale="Uses approved fixture context.",
            approval_required=True,
            approval_state="pending",
            approval_scope="send",
            send_enabled=True,
        )
    except ValueError as exc:
        assert "must not enable sending" in str(exc)
    else:
        raise AssertionError("outreach drafts must reject send_enabled=true")
