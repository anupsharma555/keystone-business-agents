"""Outreach composer agent builder and deterministic fixture-mode draft generation."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from keystone_agents.capabilities.profile import (
    RequestCapabilityProfile,
    compile_request_capability_profile,
)
from keystone_agents.company_research import research_company_fixture
from keystone_agents.founder_profile import FounderFitProfile, founder_profile_claims
from keystone_agents.guardrails import assess_unsupported_outreach_claims, keystone_guardrails
from keystone_agents.models import OutreachComposerSDKInput, TypedAgentRunResult
from keystone_agents.outreach_composer.text import (
    clean_copy as _clean_copy,
)
from keystone_agents.outreach_composer.text import (
    normalized_text as _normalized_text,
)
from keystone_agents.outreach_composer.text import (
    salutation_name as _salutation_name,
)
from keystone_agents.outreach_composer.text import (
    sentence_fragment as _sentence_fragment,
)
from keystone_agents.outreach_composer.text import (
    shorten_text as _shorten_text,
)
from keystone_agents.outreach_composer.text import (
    style_cta as _style_cta,
)
from keystone_agents.outreach_composer.text import (
    style_greeting as _style_greeting,
)
from keystone_agents.outreach_composer.text import (
    style_signoff as _style_signoff,
)
from keystone_agents.outreach_composer.text import (
    token_set as _token_set,
)
from keystone_agents.outreach_templates import load_outreach_template_context
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.approval import ApprovalScope
from keystone_agents.schemas.company_profile import ClaimEvidenceRecord, CompanyProfile
from keystone_agents.schemas.contact_context import ContactRecord, CRMAccountContext
from keystone_agents.schemas.email_style import EmailStyleProfile
from keystone_agents.schemas.execution_request import ExecutionEntrypoint
from keystone_agents.schemas.outreach import (
    ApprovedOutreachDraftingContext,
    CallPrepArtifact,
    FollowUpScheduleRecord,
    OpportunityRecord,
    OutreachContext,
    OutreachDraft,
    OutreachDraftVariant,
    OutreachDraftVariantSet,
    OutreachExampleGuidance,
    OutreachLLMDraftPayload,
    OutreachLLMVariantSetPayload,
    OutreachTemplateContext,
    SelectedOutreachDraft,
    default_follow_up_date,
)
from keystone_agents.sdk import (
    Agent,
    build_sdk_agent,
    compose_direct_instructions,
    compose_instructions,
)
from keystone_agents.sdk_run_policy import resolve_sdk_turn_policy
from keystone_agents.skill_sets import select_agent_skill_names, skill_request_text
from keystone_agents.tools.approval_tool import create_approval_queue_item
from keystone_agents.tools.email_style_tool import (
    DEFAULT_EMAIL_STYLE_PROFILE,
    load_email_style_profile,
    load_email_style_profile_fixture,
)
from keystone_agents.tools.internal_data_tools import (
    airtable_get_base_schema,
    airtable_read_records,
    airtable_write_record,
    google_workspace_tools,
)
from keystone_agents.tools.local_context_tool import (
    list_local_context_sources,
    read_local_context_file,
    search_local_context,
)
from keystone_agents.tools.memory_tool import (
    check_workflow_duplicate,
    learn_email_style_profile,
    retrieve_memory,
    retrieve_outreach_examples,
    save_outreach_dedup_memory,
)
from keystone_agents.tools.outreach_template_tool import (
    list_outreach_templates as list_outreach_template_tool,
)
from keystone_agents.tools.outreach_template_tool import (
    load_outreach_template as load_outreach_template_tool,
)
from keystone_agents.tools.serper_tool import search_web
from keystone_agents.tools.storage_tool import (
    list_outreach_tracking_records,
    load_approved_contact_context,
    load_approved_crm_context,
    load_approved_outreach_examples,
    save_initial_outreach_tracking_record,
)
from keystone_agents.tools.web_structuring_tool import structure_web_data_for_schema

PROJECT_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures"
DEFAULT_COMPANY_FIXTURE = "sample_company_curebase"
DEFAULT_RESEARCH_BRIEF_FIXTURE = "sample_company_brief_only"
DEFAULT_OPPORTUNITY_FIXTURE = "sample_lead_curebase"
DEFAULT_CONTACT_FIXTURE = "sample_contact_curebase_approved"
DEFAULT_CRM_CONTEXT_FIXTURE = "sample_crm_context_curebase"
DEFAULT_STYLE_PROFILE_FIXTURE = DEFAULT_EMAIL_STYLE_PROFILE
DEFAULT_OUTREACH_VARIANT_LABELS: tuple[str, ...] = (
    "formal",
    "warm-professional",
    "very concise",
)
KEYSTONE_PROFILE_CLAIM = ClaimEvidenceRecord(
    claim_text=(
        "Keystone Neuroinformatics LLC is a physician-scientist-led consulting company focused "
        "on clinical AI evaluation, psychiatry and neuroscience expertise, and research "
        "operations."
    ),
    source_id="keystone_profile",
    confidence=1.0,
    claim_type="keystone_profile",
)
OUTREACH_EXAMPLE_GUIDANCE_LIBRARY: tuple[OutreachExampleGuidance, ...] = (
    OutreachExampleGuidance(
        example_id="example_low_pressure_clinical_ai_intro",
        title="Low-pressure clinical AI intro pattern",
        source_id="local:approved_outreach_example:clinical_ai_intro",
        tone_guidance=["measured", "specific", "not hype-driven"],
        structure_guidance=[
            "open with one approved signal",
            "keep Keystone positioning to one sentence",
            "end with a soft question",
        ],
        pacing_guidance="Use two short paragraphs before the signoff.",
        cta_guidance="Use a compare-notes CTA instead of a calendar demand.",
        follow_up_pattern="Manual follow-up only after review.",
        applicability_notes=(
            "Useful for clinical AI or research operations prospects when source-backed "
            "context is available."
        ),
    ),
    OutreachExampleGuidance(
        example_id="example_research_workflow_context_request",
        title="Research workflow context request pattern",
        source_id="local:approved_outreach_example:workflow_context_request",
        tone_guidance=["practical", "low-pressure", "concise"],
        structure_guidance=[
            "start from the approved workflow signal",
            "avoid customer outcomes or proof claims",
            "ask for non-sensitive context",
        ],
        pacing_guidance="Prefer plain language and short paragraphs.",
        cta_guidance="Ask whether a brief exchange would be useful.",
        follow_up_pattern="If needed, one manual reminder after approval.",
        applicability_notes=(
            "Useful for decentralized trial, evidence, or research operations workflows."
        ),
    ),
)


def _with_tool_name(func: Any) -> Any:
    func.name = func.__name__
    return func


def _resolve_fixture_path(fixture: str | Path, *, default_suffix: str = ".json") -> Path:
    raw_path = Path(fixture)
    candidates = [raw_path]
    if raw_path.suffix == "":
        candidates.append(raw_path.with_suffix(default_suffix))
        candidates.append(FIXTURE_ROOT / f"{raw_path.name}{default_suffix}")
    else:
        candidates.append(FIXTURE_ROOT / raw_path.name)

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    raise FileNotFoundError(f"Fixture not found: {fixture}")


def _read_fixture_json(fixture: str | Path) -> dict[str, Any]:
    return json.loads(_resolve_fixture_path(fixture).read_text(encoding="utf-8"))


def _read_resolved_fixture_json(fixture: str | Path) -> tuple[dict[str, Any], Path]:
    path = _resolve_fixture_path(fixture)
    return json.loads(path.read_text(encoding="utf-8")), path


def _claim_context(allowed_claims: list[Any] | None) -> list[str]:
    texts: list[str] = []
    for claim in allowed_claims or []:
        if isinstance(claim, ClaimEvidenceRecord):
            texts.append(claim.claim_text)
        elif isinstance(claim, dict):
            text = claim.get("claim_text") or claim.get("claim") or claim.get("text")
            if text:
                texts.append(str(text))
        elif str(claim).strip():
            texts.append(str(claim))
    return texts


def _source_backed_claims(claims: list[ClaimEvidenceRecord]) -> list[ClaimEvidenceRecord]:
    return [
        claim
        for claim in claims
        if claim.confidence > 0.0
        and claim.claim_type != "unsupported"
        and claim.approved
        and not claim.source_id.startswith("unbacked:")
    ]


def _call_prep_known_facts(facts: list[ClaimEvidenceRecord]) -> list[ClaimEvidenceRecord]:
    return [
        fact
        for fact in facts
        if fact.confidence > 0.0
        and fact.claim_type != "unsupported"
        and fact.approved
        and not fact.source_id.startswith(("unbacked:", "user:"))
    ]


def _matching_claim(
    value: str,
    claims: list[ClaimEvidenceRecord],
) -> ClaimEvidenceRecord | None:
    text = _normalized_text(value)
    if not text:
        return None
    for claim in claims:
        claim_text = _normalized_text(claim.claim_text)
        if text in claim_text or claim_text in text:
            return claim
    return None


def _first_claim(
    claims: list[ClaimEvidenceRecord],
    claim_types: set[str],
) -> ClaimEvidenceRecord | None:
    for claim in claims:
        if claim.claim_type in claim_types:
            return claim
    return claims[0] if claims else None


def _company_name_claim(
    company_name: str,
    company_claims: list[ClaimEvidenceRecord],
) -> ClaimEvidenceRecord:
    if company_claims:
        source_id = company_claims[0].source_id
        confidence = min(company_claims[0].confidence, 0.8)
    else:
        source_id = "user:company_name"
        confidence = 0.55
    return ClaimEvidenceRecord(
        claim_text=f"Company name: {company_name}",
        source_id=source_id,
        confidence=confidence,
        claim_type="company_identity",
    )


def _contact_claim(title: str) -> ClaimEvidenceRecord:
    return ClaimEvidenceRecord(
        claim_text=f"Contact title: {title}",
        source_id="user:contact_context",
        confidence=0.6,
        claim_type="contact_context",
    )


def _coerce_contact_context(value: ContactRecord | dict[str, Any] | None) -> ContactRecord | None:
    if value is None:
        return None
    if isinstance(value, ContactRecord):
        return value
    return ContactRecord.model_validate(value)


def _coerce_crm_context(
    value: CRMAccountContext | dict[str, Any] | None,
) -> CRMAccountContext | None:
    if value is None:
        return None
    if isinstance(value, CRMAccountContext):
        return value
    return CRMAccountContext.model_validate(value)


def _coerce_style_profile(
    value: EmailStyleProfile | dict[str, Any] | None,
) -> EmailStyleProfile | None:
    if value is None:
        return None
    if isinstance(value, EmailStyleProfile):
        return value
    return EmailStyleProfile.model_validate(value)


def _coerce_template_context(
    value: OutreachTemplateContext | dict[str, Any] | None,
) -> OutreachTemplateContext | None:
    if value is None:
        return None
    if isinstance(value, OutreachTemplateContext):
        return value
    return OutreachTemplateContext.model_validate(value)


def _coerce_example_guidance(
    values: list[OutreachExampleGuidance | dict[str, Any]] | None,
) -> list[OutreachExampleGuidance]:
    examples: list[OutreachExampleGuidance] = []
    for value in values or []:
        example = (
            value
            if isinstance(value, OutreachExampleGuidance)
            else OutreachExampleGuidance.model_validate(value)
        )
        guidance_text = " ".join(
            [
                example.title,
                " ".join(example.tone_guidance),
                " ".join(example.structure_guidance),
                example.pacing_guidance,
                example.cta_guidance,
                example.follow_up_pattern,
                example.applicability_notes,
            ]
        )
        if assess_unsupported_outreach_claims(guidance_text):
            continue
        examples.append(example)
    return list({example.example_id: example for example in examples}.values())


def _unsupported_context_note(kind: str, value: str) -> tuple[str, str]:
    cleaned = _clean_copy(value)
    return (
        f"unbacked {kind}: {cleaned}",
        (
            f"{kind.replace('_', ' ').title()} was not used for personalization because it "
            "did not match approved source-backed company, opportunity, or contact context."
        ),
    )


@_with_tool_name
def load_company_profile(fixture: str = DEFAULT_COMPANY_FIXTURE) -> CompanyProfile:
    """Load an approved company profile from a local JSON fixture."""

    data, path = _read_resolved_fixture_json(fixture)
    return research_company_fixture(
        company_name=str(data["name"]),
        fixture_json=path,
    )


@_with_tool_name
def load_research_brief_profile(
    fixture: str = DEFAULT_RESEARCH_BRIEF_FIXTURE,
) -> CompanyProfile:
    """Load an approved attached research brief as source-backed company context."""

    data, path = _read_resolved_fixture_json(fixture)
    return research_company_fixture(
        company_name=str(data["name"]),
        fixture_json=path,
    )


@_with_tool_name
def load_opportunity_record(fixture: str = DEFAULT_OPPORTUNITY_FIXTURE) -> OpportunityRecord:
    """Load an approved opportunity record from a local JSON fixture."""

    data, path = _read_resolved_fixture_json(fixture)
    source_id = str(data.get("source_id") or f"fixture:{path.stem}")
    claim_text = str(data.get("rationale") or data.get("notes") or data.get("title") or "")
    claims = (
        [
            ClaimEvidenceRecord(
                claim_text=claim_text,
                source_id=source_id,
                confidence=float(data.get("confidence", 0.7)),
                claim_type="opportunity_signal",
            )
        ]
        if claim_text.strip()
        else []
    )
    return OpportunityRecord(
        company_name=data["company_name"],
        title=data.get("title", ""),
        source=data.get("source", "fixture"),
        source_id=source_id,
        notes=data.get("notes", ""),
        score=data.get("score"),
        rationale=data.get("rationale", data.get("notes", "")),
        next_step=data.get("next_step", ""),
        claims=claims,
    )


@_with_tool_name
def load_contact_context(fixture: str = DEFAULT_CONTACT_FIXTURE) -> ContactRecord:
    """Load local contact context from a JSON fixture without live CRM calls."""

    data = _read_fixture_json(fixture)
    return ContactRecord.model_validate(data)


@_with_tool_name
def load_crm_account_context(fixture: str = DEFAULT_CRM_CONTEXT_FIXTURE) -> CRMAccountContext:
    """Load local CRM/account context from a JSON fixture without live CRM calls."""

    data = _read_fixture_json(fixture)
    return CRMAccountContext.model_validate(data)


@_with_tool_name
def load_style_profile(fixture: str = DEFAULT_STYLE_PROFILE_FIXTURE) -> EmailStyleProfile:
    """Load an approved aggregate email style profile from a local JSON fixture."""

    return load_email_style_profile_fixture(fixture)


@_with_tool_name
def load_outreach_template(
    template_id: str = "low_pressure_intro",
) -> OutreachTemplateContext:
    """Load an approved repo-backed outreach template for fixture mode."""

    return load_outreach_template_context(_clean_copy(template_id) or "low_pressure_intro")


def derive_outreach_example_query(
    *,
    company_profile: CompanyProfile,
    opportunity_record: OpportunityRecord | None = None,
    template_context: OutreachTemplateContext | None = None,
    stage: str = "initial_outreach",
    outreach_goal: str | None = None,
) -> str:
    """Build a local retrieval query from approved context and requested draft shape."""

    parts = [
        company_profile.name,
        company_profile.fit_summary or company_profile.description,
        (
            opportunity_record.opportunity_type
            if hasattr(opportunity_record, "opportunity_type")
            else ""
        ),
        opportunity_record.title if opportunity_record is not None else "",
        opportunity_record.rationale if opportunity_record is not None else "",
        template_context.template_id if template_context is not None else "",
        template_context.name if template_context is not None else "",
        template_context.stage if template_context is not None else stage,
        stage,
        outreach_goal or "",
    ]
    return " ".join(part for part in (_clean_copy(str(item)) for item in parts) if part)


@_with_tool_name
def retrieve_outreach_example_guidance(
    query: str,
    max_examples: int = 2,
    database_url: str | None = None,
) -> list[OutreachExampleGuidance]:
    """Retrieve approved local example guidance without raw bodies or live RAG calls."""

    max_examples = max(0, min(max_examples, 5))
    if database_url and max_examples:
        from keystone_agents.storage.sqlite_store import SQLiteStore

        retrieved = SQLiteStore(database_url).retrieve_outreach_examples(
            query,
            limit=max_examples,
            approved_only=True,
        )
        if retrieved.examples:
            return _coerce_example_guidance(
                [
                    {
                        "example_id": document.example_id,
                        "title": f"Approved local example: {document.example_id}",
                        "source_id": (
                            f"local:outreach_example:{document.source_thread_id_hash[:16]}"
                        ),
                        "matched_query": query,
                        "tone_guidance": document.effective_phrases[:5],
                        "structure_guidance": [
                            document.conversation_pattern,
                            *document.lessons_learned[:3],
                        ],
                        "pacing_guidance": "Use the approved example for pacing only.",
                        "cta_guidance": document.cta_pattern,
                        "follow_up_pattern": document.follow_up_pattern,
                        "applicability_notes": (
                            "Retrieved from approved sanitized local examples. "
                            "Use only for tone, structure, CTA, and follow-up pattern."
                        ),
                        "approved": True,
                        "raw_email_body_included": False,
                        "provides_factual_claims": False,
                        "send_enabled": False,
                    }
                    for document in retrieved.examples
                ]
            )
    query_tokens = _token_set(query)
    scored: list[tuple[int, OutreachExampleGuidance]] = []
    for example in OUTREACH_EXAMPLE_GUIDANCE_LIBRARY:
        haystack = " ".join(
            [
                example.title,
                " ".join(example.tone_guidance),
                " ".join(example.structure_guidance),
                example.pacing_guidance,
                example.cta_guidance,
                example.follow_up_pattern,
                example.applicability_notes,
            ]
        )
        score = len(query_tokens & _token_set(haystack))
        if score or not query_tokens:
            scored.append((score, example.model_copy(update={"matched_query": query})))
    scored.sort(key=lambda item: (item[0], item[1].example_id), reverse=True)
    return _coerce_example_guidance([example for _, example in scored[:max_examples]])


@_with_tool_name
def check_unsupported_claims(
    text: str,
    allowed_claims: list[Any] | None = None,
) -> dict[str, Any]:
    """Flag unsupported prior-experience, proof, or outcome claims in draft text."""

    input_context = "\n".join(_claim_context(allowed_claims))
    raw_claims = assess_unsupported_outreach_claims(text, input_context=input_context)
    flagged = [f"unsupported outreach claim: {claim}" for claim in raw_claims]
    explanations = [
        (
            f"Unsupported outreach claim {claim!r} implies prior Keystone experience, "
            "customer proof, an outcome, or another factual assertion that is not present in "
            "the approved source-backed context."
        )
        for claim in raw_claims
    ]

    return {
        "has_unsupported_claims": bool(flagged),
        "unsupported_claims": flagged,
        "unsupported_claim_explanations": explanations,
    }


@_with_tool_name
def create_approval_request_placeholder(draft: dict[str, Any]) -> dict[str, Any]:
    """Create a local approval request placeholder without sending or posting anything."""

    return {
        "approval_required": True,
        "approval_state": "pending",
        "approval_scope": ApprovalScope.EXTERNAL_USE.value,
        "approval_rationale": (
            "Draft requires human approval before any external use; automatic email sending "
            "is disabled."
        ),
        "request_type": "outreach_draft_review",
        "send_enabled": False,
        "company_name": draft.get("company_name", ""),
        "recipient": draft.get("recipient") or draft.get("contact_name"),
    }


@_with_tool_name
def build_call_prep_artifact(
    *,
    company_profile: CompanyProfile,
    opportunity_record: OpportunityRecord | None,
    facts_used: list[ClaimEvidenceRecord],
    blocked_facts: list[str],
) -> CallPrepArtifact:
    """Build draft-only internal call prep from approved source-backed context."""

    known_facts = _call_prep_known_facts(facts_used)
    unknowns = list(company_profile.missing_information)
    if opportunity_record is None:
        unknowns.append("Approved opportunity rationale is missing.")
    if not any(fact.claim_type == "contact_context" for fact in known_facts):
        unknowns.append("Approved contact-specific context is limited or missing.")
    risks = list(company_profile.risks)
    if not risks:
        risks.append("Call prep is internal only and must not be used as outbound copy.")
    if blocked_facts:
        risks.append("Some proposed personalization facts were blocked as unsupported.")
    return CallPrepArtifact(
        meeting_objectives=[
            "Confirm the company context and current priorities before discussing fit.",
            "Learn whether clinical AI, research operations, or evidence support would be useful.",
            "Identify the best next internal reviewer or follow-up path.",
        ],
        discovery_questions=[
            (
                "What problem in clinical AI, research operations, or evidence work "
                "is most active now?"
            ),
            "Which teams would evaluate outside support for this kind of work?",
            "What source-backed context should Keystone review before any follow-up draft?",
            "What would make a short follow-up useful after this conversation?",
        ],
        known_facts=known_facts,
        unknowns=list(dict.fromkeys(unknowns)),
        risks=list(dict.fromkeys(risks)),
        suggested_next_step=(
            "Use this only as internal call prep, then update source-backed research before "
            "drafting any follow-up."
        ),
        draft_only_internal=True,
        approval_required=True,
    )


@_with_tool_name
def build_follow_up_schedule_record(
    draft: OutreachDraft | dict[str, Any],
    proposed_date: str | None = None,
    sequence_number: int = 1,
    related_draft_id: str | None = None,
    rationale: str | None = None,
) -> FollowUpScheduleRecord:
    """Create a data-only follow-up recommendation without scheduling or sending."""

    data = draft.model_dump(mode="json") if isinstance(draft, OutreachDraft) else dict(draft)
    company_name = _clean_copy(str(data.get("company_name") or ""))
    if not company_name:
        raise ValueError("company_name is required for follow-up schedule recommendations")
    contact_name = _clean_copy(str(data.get("contact_name") or data.get("recipient") or ""))
    return FollowUpScheduleRecord(
        company_name=company_name,
        contact_name=contact_name or None,
        related_draft_id=str(
            related_draft_id or data.get("related_draft_id") or data.get("draft_id") or ""
        ),
        proposed_date=proposed_date or default_follow_up_date(),
        sequence_number=sequence_number,
        status="recommended",
        rationale=(
            _clean_copy(rationale)
            or (
                "Data-only follow-up recommendation for human review after the draft and "
                "source-backed context are approved."
            )
        ),
        approval_required=True,
        send_enabled=False,
        sent=False,
        gmail_scheduled=False,
        background_job_created=False,
    )


def _blocked_context_items(
    *,
    company_profile: CompanyProfile,
    opportunity_record: OpportunityRecord | None,
    contact_record: ContactRecord | None,
    crm_record: CRMAccountContext | None,
    style_record: EmailStyleProfile | None,
    explicit_blocked_facts: list[str] | None,
) -> list[str]:
    blocked = [_clean_copy(item) for item in (explicit_blocked_facts or []) if _clean_copy(item)]
    blocked.extend(company_profile.unsupported_claims_flagged)
    if opportunity_record is not None:
        blocked.extend(opportunity_record.unsupported_claims_flagged)
        if not opportunity_record.approved_for_outreach:
            blocked.append(
                f"unapproved opportunity context: "
                f"{_clean_copy(opportunity_record.rationale or opportunity_record.notes)}"
            )
    if contact_record is not None:
        blocked.extend(contact_record.unsupported_claims_flagged)
        if not contact_record.approved_for_personalization:
            blocked.append(f"unapproved contact context: {contact_record.contact_name}")
    if crm_record is not None:
        blocked.extend(crm_record.unsupported_claims_flagged)
        if not crm_record.approved_for_personalization:
            blocked.append(f"unapproved CRM account context: {crm_record.company_name}")
    if style_record is not None and not style_record.approved_for_drafting:
        blocked.append(f"unapproved email style profile: {style_record.profile_id}")
    return list(dict.fromkeys(item for item in blocked if item))


def _approved_context_facts(
    *,
    company_profile: CompanyProfile,
    opportunity_record: OpportunityRecord | None,
    contact_record: ContactRecord | None,
    crm_record: CRMAccountContext | None,
    founder_fit_profile: FounderFitProfile | None = None,
) -> list[ClaimEvidenceRecord]:
    company_name = _clean_copy(company_profile.name)
    company_claims = _source_backed_claims(company_profile.claims)
    opportunity_claims = (
        _source_backed_claims(opportunity_record.claims)
        if opportunity_record is not None and opportunity_record.approved_for_outreach
        else []
    )
    contact_claims = (
        contact_record.source_backed_claims()
        if contact_record is not None and contact_record.approved_for_personalization
        else []
    )
    crm_claims = (
        crm_record.source_backed_claims()
        if crm_record is not None and crm_record.approved_for_personalization
        else []
    )
    founder_claims = founder_profile_claims(founder_fit_profile)
    facts = [
        _company_name_claim(company_name, company_claims),
        KEYSTONE_PROFILE_CLAIM,
        *founder_claims,
        *opportunity_claims,
        *company_claims,
        *contact_claims,
        *crm_claims,
    ]
    return list({fact.source_id + fact.claim_text: fact for fact in facts}.values())


@_with_tool_name
def build_approved_outreach_drafting_context(
    *,
    company_profile: CompanyProfile,
    opportunity_record: OpportunityRecord | None = None,
    contact_context: ContactRecord | dict[str, Any] | None = None,
    crm_context: CRMAccountContext | dict[str, Any] | None = None,
    email_style_profile: EmailStyleProfile | dict[str, Any] | None = None,
    founder_fit_profile: FounderFitProfile | dict[str, Any] | None = None,
    outreach_template: OutreachTemplateContext | dict[str, Any] | None = None,
    example_guidance: list[OutreachExampleGuidance | dict[str, Any]] | None = None,
    objective: str | None = None,
    blocked_facts: list[str] | None = None,
    revision_request: str | None = None,
    selected_draft: SelectedOutreachDraft | dict[str, Any] | None = None,
    revision_max_words: int | None = None,
    preserve_selected_cta: bool = False,
    preserve_selected_recipient: bool = True,
    max_variants: int = 1,
) -> ApprovedOutreachDraftingContext:
    """Build the typed, approved context envelope for constrained LLM drafting."""

    contact_record = _coerce_contact_context(contact_context)
    crm_record = _coerce_crm_context(crm_context)
    style_record = _coerce_style_profile(email_style_profile)
    founder_record = (
        founder_fit_profile
        if isinstance(founder_fit_profile, FounderFitProfile)
        else FounderFitProfile.model_validate(founder_fit_profile)
        if isinstance(founder_fit_profile, dict)
        else None
    )
    template_record = _coerce_template_context(outreach_template)
    example_records = _coerce_example_guidance(example_guidance)
    selected_draft_record = (
        selected_draft
        if isinstance(selected_draft, SelectedOutreachDraft)
        else SelectedOutreachDraft.model_validate(selected_draft)
        if isinstance(selected_draft, dict)
        else None
    )
    approved_style = (
        style_record if style_record is not None and style_record.approved_for_drafting else None
    )
    allowed_facts = _approved_context_facts(
        company_profile=company_profile,
        opportunity_record=opportunity_record,
        contact_record=contact_record,
        crm_record=crm_record,
        founder_fit_profile=founder_record,
    )
    source_backed_business_facts = [
        fact
        for fact in allowed_facts
        if fact.source_id != "keystone_profile" and not fact.source_id.startswith("user:")
    ]
    if not source_backed_business_facts:
        raise ValueError("approved source-backed company or opportunity context is required")

    return ApprovedOutreachDraftingContext(
        company_profile=company_profile,
        opportunity_record=(
            opportunity_record
            if opportunity_record and opportunity_record.approved_for_outreach
            else None
        ),
        contact_context=(
            contact_record
            if contact_record is not None and contact_record.approved_for_personalization
            else None
        ),
        crm_context=(
            crm_record
            if crm_record is not None and crm_record.approved_for_personalization
            else None
        ),
        email_style_profile=approved_style,
        allowed_facts=allowed_facts,
        blocked_facts=_blocked_context_items(
            company_profile=company_profile,
            opportunity_record=opportunity_record,
            contact_record=contact_record,
            crm_record=crm_record,
            style_record=style_record,
            explicit_blocked_facts=blocked_facts,
        ),
        objective=(
            _clean_copy(objective)
            or "Draft concise, source-backed outreach for external-use approval review."
        ),
        revision_request=_clean_copy(revision_request),
        selected_draft=selected_draft_record,
        revision_max_words=revision_max_words,
        preserve_selected_cta=preserve_selected_cta,
        preserve_selected_recipient=preserve_selected_recipient,
        max_variants=max_variants,
        outreach_template=template_record,
        example_guidance=example_records,
        approval_state="pending",
        approved_context_used=True,
    )


def derive_outreach_variant_labels(
    *,
    objective: str | None,
    max_variants: int = 1,
) -> list[str]:
    """Return up to three human-readable variant labels from an outreach objective."""

    if max_variants <= 1:
        return []
    text = _clean_copy(objective)
    lowered = text.lower()
    candidates: list[str] = []
    for marker in ("versions:", "variants:", "version:", "variant:"):
        if marker in lowered:
            start = lowered.index(marker) + len(marker)
            tail = text[start:].strip()
            tail = tail.split(".", 1)[0].strip()
            normalized = tail.replace(" and ", ", ")
            candidates.extend(
                item.strip(" .") for item in normalized.split(",") if item.strip(" .")
            )
            break
    if not candidates:
        candidates = list(DEFAULT_OUTREACH_VARIANT_LABELS[:max_variants])
    cleaned = list(
        dict.fromkeys(_clean_copy(candidate) for candidate in candidates if _clean_copy(candidate))
    )
    if len(cleaned) < max_variants:
        for label in DEFAULT_OUTREACH_VARIANT_LABELS:
            if label not in cleaned:
                cleaned.append(label)
            if len(cleaned) >= max_variants:
                break
    return cleaned[:max_variants]


def outreach_goal_for_variant(
    *,
    objective: str | None,
    variant_label: str,
) -> str:
    """Inject a bounded tone request while keeping facts and safety constant."""

    base = _clean_copy(objective) or "Write a short source-backed outreach email."
    return (
        f"{base}\n\n"
        f"Variant tone request: {variant_label}.\n"
        "Keep the factual grounding, approval state, and no-send boundary unchanged. "
        "Vary only wording, structure, and tone."
    )


def build_outreach_draft_variant_set(
    *,
    approved_context: ApprovedOutreachDraftingContext,
    variant_labels: list[str],
    drafts: list[OutreachDraft],
) -> OutreachDraftVariantSet:
    """Wrap a small set of validated drafts for OC-2 style tone comparisons."""

    variants = [
        OutreachDraftVariant(variant_label=label, draft=draft)
        for label, draft in zip(variant_labels, drafts, strict=False)
    ]
    if not variants:
        raise ValueError("at least one outreach draft variant is required")
    return OutreachDraftVariantSet(
        company_name=approved_context.company_profile.name,
        outreach_goal=approved_context.objective,
        requested_variant_labels=variant_labels,
        variants=variants,
        approval_required=True,
        approval_scope=ApprovalScope.EXTERNAL_USE,
        send_enabled=False,
    )


def _llm_payload_to_dict(llm_draft_payload: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(llm_draft_payload, str):
        return json.loads(llm_draft_payload)
    return dict(llm_draft_payload)


def _facts_for_source_ids(
    source_ids: list[str],
    context: ApprovedOutreachDraftingContext,
) -> list[ClaimEvidenceRecord]:
    allowed_by_source: dict[str, list[ClaimEvidenceRecord]] = {}
    for fact in context.allowed_facts:
        allowed_by_source.setdefault(fact.source_id, []).append(fact)
    unknown_source_ids = [
        source_id for source_id in source_ids if source_id not in allowed_by_source
    ]
    if unknown_source_ids:
        raise ValueError(f"LLM draft used unapproved source_ids: {unknown_source_ids}")
    facts = [fact for source_id in source_ids for fact in allowed_by_source[source_id]]
    if not facts:
        raise ValueError("LLM draft must include source_ids_used from approved context")
    return list({fact.source_id + fact.claim_text: fact for fact in facts}.values())


def _blocked_fact_mentions(
    text: str,
    blocked_facts: list[str],
) -> list[str]:
    lowered = _normalized_text(text)
    mentions: list[str] = []
    for fact in blocked_facts:
        cleaned = fact.split(":", 1)[-1].strip()
        if cleaned and _normalized_text(cleaned) in lowered:
            mentions.append(fact)
    return mentions


def compose_outreach_draft_fixture(
    *,
    company_profile: CompanyProfile,
    opportunity_record: OpportunityRecord | None = None,
    contact_name: str | None = None,
    contact_title: str | None = None,
    contact_context: ContactRecord | dict[str, Any] | None = None,
    crm_context: CRMAccountContext | dict[str, Any] | None = None,
    email_style_profile: EmailStyleProfile | dict[str, Any] | None = None,
    outreach_template: OutreachTemplateContext | dict[str, Any] | None = None,
    example_guidance: list[OutreachExampleGuidance | dict[str, Any]] | None = None,
    recent_signal: str | None = None,
    outreach_goal: str | None = None,
    include_call_prep: bool = False,
    include_follow_up_schedule: bool = False,
    follow_up_date: str | None = None,
    related_draft_id: str | None = None,
) -> OutreachDraft:
    """Create a deterministic fixture-mode outreach draft without model or API calls."""

    company_name = _clean_copy(company_profile.name)
    explicit_contact_name = _clean_copy(contact_name) or None
    explicit_contact_title = _clean_copy(contact_title) or None
    contact_name = explicit_contact_name
    contact_title = explicit_contact_title
    contact_record = _coerce_contact_context(contact_context)
    crm_record = _coerce_crm_context(crm_context)
    style_record = _coerce_style_profile(email_style_profile)
    style_profile = style_record if style_record and style_record.approved_for_drafting else None
    template_record = _coerce_template_context(outreach_template)
    example_records = _coerce_example_guidance(example_guidance)
    contact_context_claims: list[ClaimEvidenceRecord] = []
    crm_context_claims: list[ClaimEvidenceRecord] = []
    unsupported_context: list[str] = []
    unsupported_explanations: list[str] = []

    if contact_record is not None:
        contact_claims = contact_record.source_backed_claims()
        if contact_record.approved_for_personalization and contact_claims:
            contact_name = contact_record.contact_name
            contact_title = contact_record.role_title
            contact_context_claims = contact_claims
        else:
            contact_name = explicit_contact_name
            contact_title = explicit_contact_title
            flag, explanation = _unsupported_context_note(
                "contact context",
                contact_record.contact_name,
            )
            unsupported_context.append(flag)
            unsupported_explanations.append(explanation)
        unsupported_context.extend(contact_record.unsupported_claims_flagged)

    if crm_record is not None:
        crm_claims = crm_record.source_backed_claims()
        if crm_record.approved_for_personalization and crm_claims:
            crm_context_claims = crm_claims
        else:
            flag, explanation = _unsupported_context_note(
                "CRM account context",
                crm_record.company_name,
            )
            unsupported_context.append(flag)
            unsupported_explanations.append(explanation)
        unsupported_context.extend(crm_record.unsupported_claims_flagged)

    if style_record is not None and not style_record.approved_for_drafting:
        flag, explanation = _unsupported_context_note(
            "email style profile",
            style_record.profile_id,
        )
        unsupported_context.append(flag)
        unsupported_explanations.append(explanation)

    salutation_name = _salutation_name(contact_name)
    greeting = _style_greeting(salutation_name, style_profile)
    title = _clean_copy(contact_title)
    role_context = f" given your work as {title}" if title else ""
    goal = (
        _clean_copy(outreach_goal)
        or "compare notes on clinical AI evaluation and research operations"
    )

    company_context = _clean_copy(company_profile.fit_summary or company_profile.description)
    company_claims = _source_backed_claims(company_profile.claims)
    company_context_claim = _first_claim(
        company_claims,
        {"company_fit", "company_signal", "company_description"},
    )
    opportunity_context = ""
    opportunity_claims: list[ClaimEvidenceRecord] = []
    opportunity_context_claim: ClaimEvidenceRecord | None = None
    if opportunity_record is not None:
        opportunity_context = _clean_copy(opportunity_record.rationale or opportunity_record.notes)
        if opportunity_record.approved_for_outreach:
            opportunity_claims = _source_backed_claims(opportunity_record.claims)
            opportunity_context_claim = _first_claim(
                opportunity_claims,
                {"opportunity_signal", "opportunity_rationale", "keystone_fit"},
            )

    approved_claims = [
        *opportunity_claims,
        *company_claims,
        *contact_context_claims,
        *crm_context_claims,
    ]
    if not approved_claims:
        raise ValueError("approved source-backed company or opportunity context is required")
    if opportunity_record is not None and not opportunity_record.approved_for_outreach:
        flag, explanation = _unsupported_context_note(
            "opportunity context",
            opportunity_context or opportunity_record.title or opportunity_record.company_name,
        )
        unsupported_context.append(flag)
        unsupported_explanations.append(explanation)
    recent_signal_clean = _clean_copy(recent_signal)
    recent_signal_claim = _matching_claim(recent_signal_clean, approved_claims)
    if recent_signal_clean and recent_signal_claim is None:
        flag, explanation = _unsupported_context_note("recent signal", recent_signal_clean)
        unsupported_context.append(flag)
        unsupported_explanations.append(explanation)
    if company_context and company_context_claim is None:
        flag, explanation = _unsupported_context_note("company context", company_context)
        unsupported_context.append(flag)
        unsupported_explanations.append(explanation)
    if (
        opportunity_context
        and opportunity_context_claim is None
        and (opportunity_record is None or opportunity_record.approved_for_outreach)
    ):
        flag, explanation = _unsupported_context_note("opportunity context", opportunity_context)
        unsupported_context.append(flag)
        unsupported_explanations.append(explanation)

    signal_claim = recent_signal_claim or opportunity_context_claim or company_context_claim
    signal = signal_claim.claim_text if signal_claim else ""

    if signal:
        opening = _fixture_outreach_opening(company_name, signal)
    else:
        opening = f"I came across {company_name} and wanted to reach out."

    cta = _style_cta(goal, style_profile)
    if style_profile is None and template_record is not None:
        if "compare notes" in template_record.cta_guidance.lower():
            cta = "Happy to compare notes if useful."
        elif "non-sensitive context" in template_record.cta_guidance.lower():
            cta = f"Could you share non-sensitive context on whether {goal} would be useful?"
    if (
        style_profile is None
        and template_record is None
        and any("compare-notes" in example.cta_guidance.lower() for example in example_records)
    ):
        cta = "Happy to compare notes if useful."
    if role_context and style_profile is None:
        cta = cta.replace("I am reaching out", f"I am reaching out{role_context}", 1)
    elif role_context:
        cta = f"Given your work as {title}, I thought this might be relevant. {cta}"
    signoff = _style_signoff(style_profile)
    email_body = f"{greeting}\n\n{opening} {KEYSTONE_PROFILE_CLAIM.claim_text}\n\n{cta}"
    if signoff:
        email_body = f"{email_body}\n\n{signoff}"

    linkedin_signal = _shorten_text(_sentence_fragment(signal), 88)
    linkedin_opening = (
        f"I noticed {company_name}'s work around {linkedin_signal}"
        if linkedin_signal
        else f"I came across {company_name}"
    )
    linkedin_note = (
        f"Hi {salutation_name or 'there'}, {linkedin_opening}. Keystone works across "
        "clinical AI, neuroscience, and research operations. Open to a brief exchange?"
    )
    if len(linkedin_note) > 300:
        linkedin_note = (
            f"Hi {salutation_name or 'there'}, I noticed {company_name}'s clinical research "
            "work. Keystone works across clinical AI and research operations. Open to a "
            "brief exchange?"
        )

    facts_used = [
        _company_name_claim(company_name, company_claims),
        KEYSTONE_PROFILE_CLAIM,
    ]
    if signal_claim is not None:
        facts_used.append(signal_claim)
    if title:
        if contact_context_claims:
            facts_used.extend(contact_context_claims)
        else:
            facts_used.append(_contact_claim(title))
    if crm_context_claims:
        facts_used.extend(crm_context_claims)

    unsupported_check = check_unsupported_claims(
        "\n".join([email_body, linkedin_note]),
        allowed_claims=facts_used,
    )
    unsupported = [
        *unsupported_context,
        *company_profile.unsupported_claims_flagged,
        *(opportunity_record.unsupported_claims_flagged if opportunity_record else []),
        *unsupported_check["unsupported_claims"],
    ]
    blocked_facts = list(dict.fromkeys(unsupported))
    source_ids_used = list(dict.fromkeys(fact.source_id for fact in facts_used if fact.source_id))
    outreach_context = OutreachContext(
        company_profile=company_profile,
        opportunity_record=opportunity_record,
        contact_context=contact_record if contact_record and contact_context_claims else None,
        crm_context=crm_record if crm_record and crm_context_claims else None,
        email_style_profile=style_profile,
        outreach_template=template_record,
        example_guidance=example_records,
        allowed_keystone_positioning=[KEYSTONE_PROFILE_CLAIM],
        facts_used=facts_used,
        blocked_facts=blocked_facts,
        approval_state="pending",
        approved_context_used=True,
    )
    call_prep = (
        build_call_prep_artifact(
            company_profile=company_profile,
            opportunity_record=opportunity_record,
            facts_used=facts_used,
            blocked_facts=blocked_facts,
        )
        if include_call_prep
        else None
    )

    draft = OutreachDraft(
        company_name=company_name,
        recipient=contact_name,
        contact_name=contact_name,
        contact_title=contact_title,
        outreach_goal=goal,
        email_subject=f"{company_name} research workflow discussion",
        email_body=email_body,
        linkedin_note=linkedin_note,
        personalization_rationale=(
            f"Draft references approved source-backed context for {company_name}"
            + (f" using source {signal_claim.source_id}" if signal_claim else "")
            + (
                f" and approved contact source {contact_context_claims[0].source_id}"
                if contact_context_claims
                else ""
            )
            + (
                f" plus approved CRM source {crm_context_claims[0].source_id}"
                if crm_context_claims
                else ""
            )
            + (
                f"; template {template_record.template_id} guided structure only"
                if template_record is not None
                else ""
            )
            + ("; approved examples guided tone and CTA only" if example_records else "")
            + "."
        ),
        facts_used=facts_used,
        blocked_facts=blocked_facts,
        source_ids_used=source_ids_used,
        outreach_context=outreach_context,
        style_profile_used=style_profile is not None,
        style_profile_id=style_profile.profile_id if style_profile is not None else "",
        template_id=template_record.template_id if template_record is not None else "",
        template_version=(template_record.template_version if template_record is not None else ""),
        template_fit_reason=template_record.fit_reason if template_record is not None else "",
        example_ids_used=[example.example_id for example in example_records],
        example_guidance_used=bool(example_records),
        call_prep=call_prep,
        draft_policy="normal",
        approved_context_used=True,
        unsupported_claims_flagged=blocked_facts,
        unsupported_claim_explanations=[
            *unsupported_explanations,
            *unsupported_check["unsupported_claim_explanations"],
        ],
        approval_required=True,
        approval_state="pending",
        approval_scope=ApprovalScope.EXTERNAL_USE.value,
        approval_rationale=(
            "Draft requires human approval before any external use; automatic email sending "
            "is disabled."
        ),
        send_enabled=False,
        sent=False,
        can_send_email=False,
    )
    if include_follow_up_schedule:
        draft = draft.model_copy(
            update={
                "follow_up_schedules": [
                    build_follow_up_schedule_record(
                        draft,
                        proposed_date=follow_up_date,
                        related_draft_id=related_draft_id,
                    )
                ]
            }
        )
    create_approval_request_placeholder(draft.model_dump())
    return draft


def _fixture_outreach_opening(company_name: str, signal: str) -> str:
    signal_clean = _clean_copy(signal)
    review_subject = _fixture_review_subject(signal_clean)
    if review_subject:
        return f"I saw that {company_name} is considering review support for {review_subject}."
    signal_fragment = _sentence_fragment(signal_clean)
    if "exploring review support" in signal_fragment:
        return f"I saw that {signal_fragment}."
    return f"I noticed {company_name}'s work around {signal_fragment}."


def _fixture_review_subject(signal: str) -> str:
    for pattern in (
        r"\bwhether\s+Keystone\s+could\s+(?:help\s+)?review\s+"
        r"(?P<object>.*?)(?:\s+before\b|$)",
        r"\b(?:is|are)\s+(?:considering|exploring|evaluating)\s+(?:a\s+)?review\s+of\s+"
        r"(?P<object>.*?)(?:\s+before\b|$)",
        r"\b(?:is|are)\s+(?:considering|exploring|evaluating)\s+review\s+support\s+for\s+"
        r"(?P<object>.*?)(?:\s+before\b|$)",
    ):
        match = re.search(pattern, signal, flags=re.I)
        if not match:
            continue
        subject = " ".join(match.group("object").split()).strip(" .;,:")
        if subject:
            return subject[:160]
    return ""


@_with_tool_name
def compose_outreach_draft_llm_constrained(
    *,
    approved_context: ApprovedOutreachDraftingContext | dict[str, Any],
    llm_draft_payload: dict[str, Any] | str | None = None,
    fallback_to_fixture: bool = True,
) -> OutreachDraft:
    """Validate a proposed LLM draft against approved context and draft-only policy."""

    context = (
        approved_context
        if isinstance(approved_context, ApprovedOutreachDraftingContext)
        else ApprovedOutreachDraftingContext.model_validate(approved_context)
    )
    if llm_draft_payload is None:
        if not fallback_to_fixture:
            raise RuntimeError(
                "Live LLM outreach drafting is not implemented in this local path. "
                "Provide llm_draft_payload or use deterministic fallback."
            )
        draft = compose_outreach_draft_fixture(
            company_profile=context.company_profile,
            opportunity_record=context.opportunity_record,
            contact_context=context.contact_context,
            crm_context=context.crm_context,
            email_style_profile=context.email_style_profile,
            outreach_template=context.outreach_template,
            example_guidance=context.example_guidance,
            outreach_goal=context.objective,
        )
        return draft.model_copy(
            update={
                "drafting_mode": "deterministic_fixture",
                "revision_request": context.revision_request,
            }
        )

    payload = _llm_payload_to_dict(llm_draft_payload)
    raw_source_ids = payload.get("source_ids_used")
    if not isinstance(raw_source_ids, list):
        raise ValueError("LLM draft must include source_ids_used as a list")
    advisory_source_ids = {
        str(context.email_style_profile.source_id)
        if context.email_style_profile is not None
        else "",
        str(context.outreach_template.template_id)
        if context.outreach_template is not None
        else "",
        *(
            str(example.source_id)
            for example in context.example_guidance
            if example.source_id
        ),
    }
    source_ids_used = list(
        dict.fromkeys(
            str(source_id)
            for source_id in raw_source_ids
            if str(source_id) not in advisory_source_ids
        )
    )
    facts_used = _facts_for_source_ids(source_ids_used, context)
    raw_text_fields = "\n".join(
        str(payload.get(key) or "")
        for key in (
            "email_subject",
            "subject",
            "email_body",
            "body",
            "linkedin_note",
            "personalization_rationale",
            "rationale",
        )
    )
    if "\u2014" in raw_text_fields:
        raise ValueError("LLM draft must not contain em dashes")
    email_subject = _clean_copy(str(payload.get("email_subject") or payload.get("subject") or ""))
    email_body = _clean_copy(str(payload.get("email_body") or payload.get("body") or ""))
    linkedin_note = _clean_copy(str(payload.get("linkedin_note") or ""))
    personalization_rationale = _clean_copy(
        str(payload.get("personalization_rationale") or payload.get("rationale") or "")
    )
    if not email_subject or not email_body:
        raise ValueError("LLM draft must include email_subject and email_body")

    selected_draft = context.selected_draft
    if context.revision_max_words is not None:
        word_count = len(email_body.split())
        if word_count > context.revision_max_words:
            raise ValueError(
                "revised email body exceeds the selected revision word limit "
                f"({word_count} > {context.revision_max_words})"
            )
    if context.preserve_selected_cta and selected_draft is not None:
        revised_copy = "\n".join((email_body, linkedin_note))
        if selected_draft.cta_text not in revised_copy:
            raise ValueError("revised draft must preserve the selected CTA exactly")
    payload_recipient = _clean_copy(str(payload.get("recipient") or ""))
    if (
        context.preserve_selected_recipient
        and selected_draft is not None
        and selected_draft.recipient
        and payload_recipient
        and payload_recipient != selected_draft.recipient
    ):
        raise ValueError("revised draft must preserve the selected recipient")

    payload_contact_name = _clean_copy(str(payload.get("contact_name") or ""))
    payload_contact_title = _clean_copy(str(payload.get("contact_title") or ""))
    if payload_contact_title and not payload_contact_name:
        for greeting_prefix in ("Hi", "Hello", "Dear"):
            title_greeting = f"{greeting_prefix} {payload_contact_title},"
            if email_body.startswith(title_greeting):
                email_body = f"{greeting_prefix}," + email_body[len(title_greeting) :]
                break

    text_for_validation = "\n".join(
        [email_subject, email_body, linkedin_note, personalization_rationale]
    )
    blocked_mentions = _blocked_fact_mentions(text_for_validation, context.blocked_facts)
    if blocked_mentions:
        raise ValueError(f"LLM draft used blocked facts: {blocked_mentions}")
    unsupported_check = check_unsupported_claims(text_for_validation, allowed_claims=facts_used)
    if unsupported_check["has_unsupported_claims"]:
        raise ValueError(
            f"LLM draft contains unsupported claims: {unsupported_check['unsupported_claims']}"
        )

    outreach_context = OutreachContext(
        company_profile=context.company_profile,
        opportunity_record=context.opportunity_record,
        contact_context=context.contact_context,
        crm_context=context.crm_context,
        email_style_profile=context.email_style_profile,
        outreach_template=context.outreach_template,
        example_guidance=context.example_guidance,
        allowed_keystone_positioning=[KEYSTONE_PROFILE_CLAIM],
        facts_used=facts_used,
        blocked_facts=context.blocked_facts,
        approval_state="pending",
        approved_context_used=True,
    )
    draft = OutreachDraft(
        company_name=_clean_copy(str(payload.get("company_name") or context.company_profile.name)),
        recipient=_clean_copy(
            str(
                payload.get("recipient")
                or (selected_draft.recipient if selected_draft is not None else "")
                or payload_contact_name
                or (
                    context.contact_context.contact_name
                    if context.contact_context is not None
                    else ""
                )
                or ""
            )
        )
        or None,
        contact_name=_clean_copy(
            str(
                payload_contact_name
                or (context.contact_context.contact_name if context.contact_context else "")
            )
        )
        or None,
        contact_title=_clean_copy(
            str(
                payload_contact_title
                or (context.contact_context.role_title if context.contact_context else "")
            )
        )
        or None,
        outreach_goal=context.objective,
        email_subject=email_subject,
        email_body=email_body,
        linkedin_note=linkedin_note,
        personalization_rationale=personalization_rationale,
        facts_used=facts_used,
        blocked_facts=context.blocked_facts,
        source_ids_used=source_ids_used,
        outreach_context=outreach_context,
        style_profile_used=context.email_style_profile is not None,
        style_profile_id=(
            context.email_style_profile.profile_id if context.email_style_profile else ""
        ),
        template_id=(context.outreach_template.template_id if context.outreach_template else ""),
        template_version=(
            context.outreach_template.template_version if context.outreach_template else ""
        ),
        template_fit_reason=(
            context.outreach_template.fit_reason if context.outreach_template else ""
        ),
        example_ids_used=[example.example_id for example in context.example_guidance],
        example_guidance_used=bool(context.example_guidance),
        drafting_mode="llm_constrained",
        revision_request=context.revision_request,
        revised_from_draft_id=(selected_draft.draft_id if selected_draft is not None else ""),
        draft_policy="normal",
        approved_context_used=True,
        unsupported_claims_flagged=[],
        unsupported_claim_explanations=[],
        request_coverage=payload.get("request_coverage") or {},
        approval_required=True,
        approval_state="pending",
        approval_scope=ApprovalScope.EXTERNAL_USE.value,
        approval_rationale=(
            "LLM draft was constrained to approved source-backed context and requires "
            "human approval before external use; automatic sending is disabled."
        ),
        send_enabled=False,
        sent=False,
        can_send_email=False,
    )
    if draft.unsupported_claims_flagged:
        raise ValueError(
            f"LLM draft produced unsupported facts: {draft.unsupported_claims_flagged}"
        )
    create_approval_request_placeholder(draft.model_dump())
    return draft


def build_outreach_composer_agent(
    model: str | None = None,
    *,
    include_tools: bool = True,
    request_text: str = "",
    context_flags: Mapping[str, bool] | None = None,
    include_all_skills: bool = False,
    compact_instructions: bool = False,
) -> Agent:
    """Build the outreach composer agent."""

    skill_files = select_agent_skill_names(
        "outreach_composer",
        request_text=request_text,
        context_flags=context_flags,
        include_all=include_all_skills,
        compact=compact_instructions,
    )
    composer = compose_direct_instructions if compact_instructions else compose_instructions
    prompt_files = (
        ("keystone_profile.md", "safety_policy.md", "outreach_composer.md")
        if compact_instructions
        else (
            "keystone_profile.md",
            "safety_policy.md",
            "tools.md",
            "outreach_composer.md",
        )
    )
    instructions = composer(*prompt_files, skill_files=skill_files)
    return build_sdk_agent(
        name="outreach_composer",
        instructions=instructions,
        output_type=OutreachDraft,
        tools=(
            [
                load_company_profile,
                load_research_brief_profile,
                load_opportunity_record,
                load_contact_context,
                load_crm_account_context,
                load_style_profile,
                list_local_context_sources,
                search_local_context,
                read_local_context_file,
                list_outreach_template_tool,
                load_outreach_template_tool,
                retrieve_outreach_example_guidance,
                load_email_style_profile,
                retrieve_memory,
                retrieve_outreach_examples,
                check_workflow_duplicate,
                airtable_get_base_schema,
                airtable_read_records,
                airtable_write_record,
                search_web,
                structure_web_data_for_schema,
                load_approved_contact_context,
                load_approved_crm_context,
                load_approved_outreach_examples,
                check_unsupported_claims,
                build_approved_outreach_drafting_context,
                compose_outreach_draft_llm_constrained,
                build_call_prep_artifact,
                build_follow_up_schedule_record,
                save_outreach_dedup_memory,
                learn_email_style_profile,
                save_initial_outreach_tracking_record,
                list_outreach_tracking_records,
                create_approval_queue_item,
                create_approval_request_placeholder,
                *google_workspace_tools(),
            ]
            if include_tools
            else []
        ),
        guardrails=keystone_guardrails(),
        model=model,
        policy_agent_name="outreach_composer",
        handoff_description=(
            "Use to compose approval-gated draft-only outreach, call prep, and follow-up "
            "recommendations from approved source-backed context."
        ),
    )


def build_outreach_composer_compact_synthesis_agent(
    model: str | None = None,
    *,
    request_text: str = "",
    include_all_skills: bool = False,
    include_tools_policy: bool = False,
    internal_slack_copy: bool = False,
) -> Agent:
    """Build a compact structured-output outreach agent for constrained providers."""

    prompt_files = ["keystone_profile.md", "safety_policy.md"]
    if include_tools_policy:
        prompt_files.append("tools.md")
    prompt_files.append("outreach_composer.md")
    instructions = compose_instructions(
        *prompt_files,
        skill_files=(
            select_agent_skill_names(
                "outreach_composer",
                request_text=request_text,
                include_all=True,
            )
            if include_all_skills
            else [
                name
                for name in select_agent_skill_names(
                    "outreach_composer",
                    request_text=request_text,
                )
                if name
                in {
                    "evidence_attribution_and_claim_mapping",
                    "context_permission_gating",
                    "action_boundary_enforcement",
                    "unsupported_claim_and_gap_handling",
                    "structured_output_quality_review",
                    "writing_style_adaptation",
                    "outreach_composer_specialist_contracts",
                }
            ]
        ),
    )
    instructions = "\n\n".join(
        [
            instructions,
            (
                "Compact synthesis mode: return only company_name, email_subject, "
                "email_body, linkedin_note, personalization_rationale, source_ids_used, "
                "reply_recommended, recommended_next_step, additional_information_needed, "
                "collaboration_ideas, deferral_reason, and request_coverage. Choose "
                "source_ids_used only "
                "from the approved source IDs in the prompt. Do not include send, approval, "
                "context, facts_used, or other workflow fields; Python will validate and "
                "wrap the compact payload into the full OutreachDraft schema."
            ),
            (
                "Inbound thread-state priority: the supplied chronological conversation state "
                "takes precedence over style-profile CTA preferences. If the latest state is a "
                "courtesy close or future-collaboration invitation, set reply_recommended=false "
                "unless the evidence supports a concrete immediate reply. Return a model-judged "
                "next step, useful missing information, provisional collaboration ideas, and a "
                "deferral reason. Any optional future reply must contain zero questions and must "
                "not reopen scheduling, another call, or a generic compare-notes exchange."
                " When reply_recommended=false, do not describe a reply or draft as though one "
                "exists. Write personalization_rationale as the recommendation rationale and "
                "leave email_subject, email_body, and linkedin_note empty."
                " Write any optional reply as normal correspondence; avoid workflow narration "
                "such as 'based on our thread,' 'the selected context,' or 'if it would be "
                "helpful.' State a concrete point plainly."
            ),
            *(
                [
                    (
                        "Internal Slack recommendation mode overrides the external-reply "
                        "instructions above. This is an internal decision artifact, not "
                        "correspondence. Always return a non-empty email_subject and email_body; "
                        "Python uses email_body as the canonical internal Slack copy. Set "
                        "reply_recommended=true only to indicate that the requested internal "
                        "artifact was produced, not that external outreach is recommended. "
                        "Do not use a greeting, signoff, recipient language, or a generic "
                        "compare-notes CTA. Directly state the strongest supported opportunity, "
                        "the most important validation gap, and the next safe action. Include "
                        "a concise 'What the supplied note supports' section when the raw "
                        "request provides bounded facts, and distinguish those facts from "
                        "unverified outcomes, references, implementation evidence, or "
                        "evaluation claims. Include "
                        "the retained source URLs visibly in the body when the operator asks "
                        "for citations. Keep the full email_body under 150 words, including "
                        "headings and source URLs, so it remains concise and passes the "
                        "internal artifact validator. Preserve every distinct requested "
                        "deliverable from the raw request and interpreted output constraints. "
                        "When the operator asks for a separate paste-ready note or other "
                        "named component, give it its own visible reader-facing section "
                        "instead of folding it into the decision brief. Audit those "
                        "deliverables in request_coverage; use an empty unmet_dimensions "
                        "list when none are missing. Keep external actions disabled."
                    )
                ]
                if internal_slack_copy
                else []
            ),
        ]
    )
    return build_sdk_agent(
        name="outreach_composer",
        instructions=instructions,
        output_type=OutreachLLMDraftPayload,
        tools=[],
        guardrails=keystone_guardrails(internal_artifact=internal_slack_copy),
        model=model,
        handoff_description=(
            "Use for compact, approval-gated draft-only outreach synthesis from "
            "approved source-backed context."
        ),
    )


def build_outreach_composer_compact_variant_agent(
    model: str | None = None,
    *,
    request_text: str = "",
    include_all_skills: bool = False,
) -> Agent:
    """Build a compact one-call outreach variant-set agent."""

    instructions = compose_instructions(
        "keystone_profile.md",
        "safety_policy.md",
        "tools.md",
        "outreach_composer.md",
        skill_files=select_agent_skill_names(
            "outreach_composer",
            request_text=request_text,
            include_all=include_all_skills,
        ),
    )
    instructions = "\n\n".join(
        [
            instructions,
            (
                "Compact multi-variant synthesis mode: return exactly the requested "
                "variant labels as variants. Each variant draft must include only "
                "company_name, email_subject, email_body, linkedin_note, "
                "personalization_rationale, and source_ids_used. Use the same "
                "source_ids_used across all variants, chosen only from approved "
                "source IDs in the prompt. Do not include send, approval, context, "
                "facts_used, or workflow fields; Python will validate and wrap each "
                "compact payload into the full OutreachDraft schema."
            ),
        ]
    )
    return build_sdk_agent(
        name="outreach_composer",
        instructions=instructions,
        output_type=OutreachLLMVariantSetPayload,
        tools=[],
        guardrails=keystone_guardrails(),
        model=model,
        handoff_description=(
            "Use for compact, approval-gated draft-only outreach tone variants from "
            "approved source-backed context."
        ),
    )


def run_outreach_composer_sdk(
    typed_input: OutreachComposerSDKInput | str,
    *,
    run_config: Any | None = None,
    live: bool = False,
    model: str | None = None,
    session: Any | None = None,
    context_flags: Mapping[str, bool] | None = None,
    max_turns: int | None = None,
    attach_tools: bool = True,
    compact_instructions: bool = False,
    entrypoint: ExecutionEntrypoint = "direct_sdk",
) -> TypedAgentRunResult[OutreachDraft]:
    """Run Outreach Composer through the typed SDK harness."""

    supplied_context_profile = isinstance(typed_input, OutreachComposerSDKInput)
    include_tools = bool(attach_tools and not supplied_context_profile)
    resolved_compact_instructions = bool(
        compact_instructions or supplied_context_profile
    )
    provider_is_gemini = False
    if live and run_config is None:
        from keystone_agents.model_provider import (
            GEMINI_PROVIDER,
            get_runtime_agent_model_config,
        )

        model_config = get_runtime_agent_model_config("outreach_composer", model_override=model)
        provider_is_gemini = model_config.provider == GEMINI_PROVIDER
        if provider_is_gemini:
            include_tools = False
    if provider_is_gemini:
        raise RuntimeError(
            "run_outreach_composer_sdk does not support compact Gemini conversion; "
            "use the Outreach Composer CLI SDK synthesis path."
        )

    turn_policy = resolve_sdk_turn_policy(
        "outreach_composer",
        request_text=skill_request_text(typed_input),
        explicit_max_turns=max_turns,
    )
    agent = build_outreach_composer_agent(
        model=model,
        include_tools=include_tools,
        request_text=skill_request_text(typed_input),
        context_flags=context_flags,
        compact_instructions=resolved_compact_instructions,
    )
    capability_model_provider, capability_model_name = _outreach_model_identity(
        run_config=run_config,
        agent=agent,
        model=model,
    )
    capability_profile = compile_request_capability_profile(
        entrypoint=entrypoint,
        agent=agent,
        execution_shape=(
            "supplied_context_draft"
            if supplied_context_profile
            else "legacy_context_acquisition"
        ),
        prompt_profile=(
            "outreach_compact"
            if resolved_compact_instructions
            else "outreach_full"
        ),
        max_turns=turn_policy.max_turns,
        retrieval_enabled=bool(include_tools),
        provider_operations=(),
        write_enabled=False,
        send_enabled=False,
        model_provider=capability_model_provider,
        model_name=capability_model_name,
    )
    result = run_typed_sdk_agent(
        agent=agent,
        typed_input=typed_input,
        output_type=OutreachDraft,
        run_config=run_config,
        live=live,
        session=session,
        capability_profile=capability_profile,
        entrypoint=entrypoint,
        execution_shape=capability_profile.execution_shape,
        prompt_profile=capability_profile.prompt_profile,
        max_turns=turn_policy.max_turns,
    )
    # TypedAgentRunResult is frozen, but its audit metadata mapping is
    # intentionally mutable so wrappers can add route-specific receipts. Keep
    # compatibility with lightweight test doubles that return another shape.
    request_cache = getattr(result, "request_cache", None)
    if isinstance(request_cache, dict):
        request_cache.setdefault("capability_profile", capability_profile.receipt())
    return result


def _outreach_model_identity(
    *,
    run_config: Any | None,
    agent: Any,
    model: str | None,
) -> tuple[str, str]:
    if run_config is not None:
        provider = getattr(run_config, "model_provider", None)
        provider_name = (
            str(
                getattr(provider, "provider_name", "")
                or getattr(provider, "name", "")
                or type(provider).__name__
            ).strip()
            if provider is not None
            else "local"
        )
        model_name = str(
            getattr(run_config, "model", "")
            or getattr(agent, "model", "")
            or "sdk-local"
        ).strip()
        return provider_name, model_name

    from keystone_agents.model_provider import get_runtime_agent_model_config

    config = get_runtime_agent_model_config(
        "outreach_composer",
        model_override=model,
    )
    return config.provider, config.model


def compile_outreach_request_capability_profile(
    typed_input: OutreachComposerSDKInput,
    *,
    entrypoint: ExecutionEntrypoint,
    model: str | None = None,
    max_turns: int | None = None,
) -> RequestCapabilityProfile:
    """Compile the provider-free Outreach profile used by every entrypoint."""

    turn_policy = resolve_sdk_turn_policy(
        "outreach_composer",
        request_text=skill_request_text(typed_input),
        explicit_max_turns=max_turns,
    )
    agent = build_outreach_composer_agent(
        model=model,
        include_tools=False,
        request_text=skill_request_text(typed_input),
        compact_instructions=True,
    )
    model_provider, model_name = _outreach_model_identity(
        run_config=None,
        agent=agent,
        model=model,
    )
    return compile_request_capability_profile(
        entrypoint=entrypoint,
        agent=agent,
        execution_shape="supplied_context_draft",
        prompt_profile="outreach_compact",
        max_turns=turn_policy.max_turns,
        retrieval_enabled=False,
        provider_operations=(),
        write_enabled=False,
        send_enabled=False,
        model_provider=model_provider,
        model_name=model_name,
    )


def run_outreach_composer_constrained_sdk(
    typed_input: OutreachComposerSDKInput | str,
    *,
    approved_drafting_context: ApprovedOutreachDraftingContext | dict[str, Any],
    evidence_output_path: str | Path | None = None,
    run_config: Any | None = None,
    live: bool = False,
    model: str | None = None,
    session: Any | None = None,
    max_turns: int | None = None,
    workflow_name: str | None = None,
    trace_metadata: Mapping[str, Any] | None = None,
) -> TypedAgentRunResult[OutreachDraft]:
    """Run compact synthesis and optionally persist a bounded atomic evidence receipt."""

    context = (
        approved_drafting_context
        if isinstance(approved_drafting_context, ApprovedOutreachDraftingContext)
        else ApprovedOutreachDraftingContext.model_validate(approved_drafting_context)
    )
    turn_policy = resolve_sdk_turn_policy(
        "outreach_composer",
        request_text=skill_request_text(typed_input),
        explicit_max_turns=max_turns,
    )
    compact_result = run_typed_sdk_agent(
        agent=build_outreach_composer_compact_synthesis_agent(
            model=model,
            request_text=skill_request_text(typed_input),
        ),
        typed_input=typed_input,
        output_type=OutreachLLMDraftPayload,
        run_config=run_config,
        live=live,
        session=session,
        max_turns=turn_policy.max_turns,
        workflow_name=workflow_name,
        trace_metadata=trace_metadata,
    )
    compact_payload = compact_result.output.model_dump(mode="json")
    if isinstance(typed_input, OutreachComposerSDKInput):
        if typed_input.contact_name and not compact_payload.get("contact_name"):
            compact_payload["contact_name"] = typed_input.contact_name
        if typed_input.contact_title and not compact_payload.get("contact_title"):
            compact_payload["contact_title"] = typed_input.contact_title
    if evidence_output_path is not None:
        _write_constrained_outreach_evidence(
            evidence_output_path,
            {
                "schema_version": "keystone.outreach.constrained_sdk_evidence.v1",
                "status": "compact_received",
                "agent_name": compact_result.agent_name,
                "live": compact_result.live,
                "compact_output": compact_payload,
                "usage": compact_result.usage,
                "cost": compact_result.cost,
                "budget_guard": compact_result.budget_guard,
                "request_cache": compact_result.request_cache,
            },
        )
    draft = compose_outreach_draft_llm_constrained(
        approved_context=context,
        llm_draft_payload=compact_payload,
        fallback_to_fixture=False,
    )
    result = TypedAgentRunResult(
        agent_name=compact_result.agent_name,
        output=draft,
        raw_result=compact_result.raw_result,
        live=compact_result.live,
        usage=compact_result.usage,
        cost=compact_result.cost,
        budget_guard=compact_result.budget_guard,
        request_cache=compact_result.request_cache,
        tool_receipts=compact_result.tool_receipts,
    )
    if evidence_output_path is not None:
        _write_constrained_outreach_evidence(
            evidence_output_path,
            {
                "schema_version": "keystone.outreach.constrained_sdk_evidence.v1",
                "status": "completed",
                "agent_name": result.agent_name,
                "live": result.live,
                "compact_output": compact_payload,
                "output": result.output.model_dump(mode="json"),
                "usage": result.usage,
                "cost": result.cost,
                "budget_guard": result.budget_guard,
                "request_cache": result.request_cache,
            },
        )
    return result


def _write_constrained_outreach_evidence(
    output_path: str | Path,
    payload: dict[str, Any],
) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)
