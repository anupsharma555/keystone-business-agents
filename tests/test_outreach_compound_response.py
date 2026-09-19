"""Compound draft requirements survive parsing, storage and public rendering."""

import json

import pytest

from keystone_agents import workflow_runner as workflow
from keystone_agents.agents.outreach_composer import compose_outreach_draft_llm_constrained
from keystone_agents.planning.compatibility import (
    infer_manual_request_plan,
    merge_manual_request_plan,
)
from keystone_agents.schemas.company_profile import (
    ClaimEvidenceRecord,
    CompanyProfile,
    SourceRecord,
)
from keystone_agents.schemas.outreach import (
    ApprovedOutreachDraftingContext,
    OutreachDraft,
    OutreachLLMDraftPayload,
)
from keystone_agents.schemas.work_item import WorkflowRunRequest
from keystone_agents.storage.sqlite_store import SQLiteStore

REQUEST = (
    "In three short bullets, summarize this email and identify a fact to verify. "
    "Then write a 120–150-word exploratory outreach template with a subject line "
    "here in Slack. Include the Gmail source link alongside the template. Do not send."
)
SOURCE = "https://mail.google.com/mail/?authuser=reader%40example.test#all/thread-selected"
SUMMARY = (
    "- The newsletter describes an evaluation project.\n- It reports a pilot.\n- Verify the scope."
)


def plan():
    base = infer_manual_request_plan(REQUEST, requested_agent="outreach_composer")
    candidate = base.model_copy(deep=True)
    candidate.source = "llm"
    candidate.ask_shape.output_constraints = candidate.ask_shape.output_constraints.model_copy(
        update={
            "word_scope": "draft_body",
            "item_scope": "answer",
            "source_scope": "entire_response",
        }
    )
    return merge_manual_request_plan(base, candidate)


def draft(summary=SUMMARY, words=130):
    fact = ClaimEvidenceRecord(
        claim_text="The newsletter reports an evaluation pilot.",
        source_id="email-selected",
        confidence=0.8,
        claim_type="user_provided",
    )
    context = ApprovedOutreachDraftingContext(
        company_profile=CompanyProfile(
            name="Example",
            sources=[
                SourceRecord(
                    source_id=fact.source_id,
                    title="Selected email",
                    url=SOURCE,
                    source_type="user_provided",
                    confidence=0.8,
                    supported_claims=[fact.claim_text],
                )
            ],
        ),
        allowed_facts=[fact],
        objective="Exploratory internal draft",
    )
    payload = OutreachLLMDraftPayload(
        email_subject="Exploratory discussion",
        email_body=" ".join(["Discuss"] * words),
        supporting_summary=summary,
        personalization_rationale="Grounded in the selected email.",
        source_ids_used=[fact.source_id],
    )
    return compose_outreach_draft_llm_constrained(
        approved_context=context,
        llm_draft_payload=payload.model_dump(mode="json"),
        fallback_to_fixture=False,
    )


def mismatches(value):
    return workflow._outreach_draft_contract_mismatches(
        value,
        request=WorkflowRunRequest(
            request_text=REQUEST,
            manual_request_plan=plan().model_dump(mode="json"),
        ),
    )


def test_compound_response_survives_schema_storage_and_canonical_renderer(tmp_path):
    value = draft()
    assert mismatches(value) == []
    store = SQLiteStore(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    store.save_outreach_draft(value)
    row = store.fetch_all("outreach_drafts")[0]
    saved = OutreachDraft.model_validate(
        {**json.loads(row["draft_json"]), "email_body": row["email_body"]}
    )
    rendered = workflow._format_outreach_draft_work_item_summary(
        saved,
        company_name="Example",
        compact_thread_local=True,
    )
    assert SUMMARY in rendered and SOURCE in rendered and saved.email_body in rendered
    assert saved.email_body == value.email_body
    assert saved.send_enabled is False and saved.sent is False
    assert rendered.index(SUMMARY) < rendered.index("Body:") < rendered.index("Sources:")
    # The canonical renderer measures the same parts, excluding source/safety from body length.
    from keystone_agents.instruction_following import validate_output_constraints

    assert validate_output_constraints(rendered, plan().ask_shape.output_constraints).passed


@pytest.mark.parametrize(
    "summary,words,violation",
    [
        ("", 130, "item count"),
        ("- First\n- Second", 130, "item count"),
        (SUMMARY + "\n- Extra", 130, "item count"),
        (SUMMARY, 119, "word count"),
        (SUMMARY, 151, "word count"),
    ],
)
def test_each_component_is_checked_independently(summary, words, violation):
    assert any(violation in item for item in mismatches(draft(summary, words)))


def test_unselected_source_cannot_satisfy_visible_link_requirement():
    value = draft().model_copy(update={"source_ids_used": []})
    assert "visible source URL missing" in mismatches(value)


def test_bullets_inside_email_do_not_substitute_for_summary():
    value = draft(summary="").model_copy(update={"email_body": SUMMARY + "\n" + "word " * 110})
    assert any("item count" in item for item in mismatches(value))


@pytest.mark.parametrize("restricted_project", [False, True])
def test_thread_local_model_input_uses_applicable_readiness(monkeypatch, restricted_project):
    from keystone_agents.schemas.work_item import (
        WorkItem,
        WorkItemArtifactRef,
        WorkItemFact,
        WorkItemKind,
        WorkItemRoute,
        WorkItemSourceRef,
        WorkItemTarget,
    )

    source = WorkItemSourceRef(
        source_id="selected",
        title="Example brief",
        url="https://example.test/brief",
        supported_claim="Example is evaluating monitoring workflows.",
    )
    item = WorkItem(
        kind=WorkItemKind.GMAIL_THREAD,
        title="Source-based template",
        request_text=REQUEST,
        current_route=WorkItemRoute.OUTREACH_COMPOSER,
        target=WorkItemTarget(
            name="Example",
            object_type="company",
            metadata={
                "project_context": {
                    "project_id": "restricted",
                    "name": "Restricted project",
                    "approved_for_agent_use": False,
                },
            }
            if restricted_project
            else {},
        ),
        sources=[source],
        facts=[
            WorkItemFact(
                key="description",
                value=source.supported_claim,
                source_refs=[source],
                approval_state="approved_for_drafting",
            )
        ],
        artifact_refs=[
            WorkItemArtifactRef(
                artifact_type="research_brief",
                artifact_id="brief",
                selected=True,
                title="Example",
                summary=source.supported_claim,
                approval_state="approved_for_drafting",
            )
        ],
    )
    captured = {}

    def capture(**kwargs):
        raw = kwargs["retrieve"]()
        captured.update(raw)
        captured["model_input"] = kwargs["normalize"](raw).approved_context
        raise RuntimeError("Stop before any model invocation")

    monkeypatch.setattr(workflow, "run_retrieved_sdk_synthesis", capture)
    workflow._compose_thread_local_outreach_draft_for_work_item(
        request=WorkflowRunRequest(request_text=REQUEST, live_sdk=True),
        work_item=item,
    )
    pack = captured["context_pack"]
    research = next(g for g in pack.readiness_gates if g.name == "research_sufficiency")
    assert not research.ready and not research.required
    assert pack.can_synthesize is (not restricted_project)
    assert pack.ready is (not restricted_project)
    assert source.url in captured["model_input"]
    assert captured["thread_local_policy"]["send_enabled"] is False
    assert captured["thread_local_policy"]["external_write_performed"] is False
    assert not any("company profile" in m.lower() for m in pack.missing_requirements)


@pytest.mark.parametrize("matching_thread", [False, True])
def test_email_context_does_not_turn_publisher_into_target_or_reuse_other_thread_url(
    matching_thread,
):
    from keystone_agents.schemas.email_triage import GmailThreadSummaryResult
    from keystone_agents.schemas.work_item import WorkItem, WorkItemKind, WorkItemSourceRef

    url = "https://mail.google.com/mail/?authuser=reader%40example.test#all/" + (
        "selected" if matching_thread else "other"
    )
    item = WorkItem(
        kind=WorkItemKind.GMAIL_THREAD,
        title="Email-based template",
        sources=[
            WorkItemSourceRef(
                source_id="read",
                url=url,
                provider="gmail",
                extraction_status="provider_context_read",
            ),
        ],
    )
    context = workflow._thread_local_outreach_sdk_context(
        request=WorkflowRunRequest(request_text=REQUEST),
        work_item=item,
        fallback=OutreachDraft(
            company_name="Newsletter Publisher", personalization_rationale="Selected source"
        ),
        gmail_thread_context=GmailThreadSummaryResult(
            thread_id="selected", summary="Selected facts"
        ),
    )
    assert context.company_profile.name == "Selected email context"
    summary_source = context.company_profile.sources[0]
    assert summary_source.url == (url if matching_thread else "gmail-thread://selected")


@pytest.mark.parametrize("lose_summary", [False, True])
def test_final_delivery_records_measured_component_validation(lose_summary):
    from keystone_agents.presentation.public_result import (
        attach_execution_public_result,
        build_work_item_result_payload,
    )
    from keystone_agents.schemas.work_item import (
        WorkflowRunResult,
        WorkItem,
        WorkItemArtifactRef,
        WorkItemKind,
        WorkItemRoute,
        WorkItemStatus,
    )

    value = draft()
    rendered = workflow._format_outreach_draft_work_item_summary(
        value,
        company_name="Example",
        compact_thread_local=True,
    )
    if lose_summary:
        rendered = rendered.replace(SUMMARY, "")
    item = WorkItem(
        kind=WorkItemKind.GMAIL_THREAD,
        title="Synthetic",
        request_text=REQUEST,
        status=WorkItemStatus.DONE,
    )
    result = WorkflowRunResult(
        work_item=item,
        route=WorkItemRoute.OUTREACH_COMPOSER,
        status=item.status,
        advanced=True,
        human_summary=rendered,
        manual_request_plan=plan().model_dump(mode="json"),
        artifact_refs=[
            WorkItemArtifactRef(
                artifact_type="outreach_draft",
                artifact_id="synthetic",
                selected=True,
                metadata={"canonical_draft_copy": True},
            )
        ],
    )
    payload = build_work_item_result_payload(result, user_facing_result_verified=True)
    public = attach_execution_public_result(payload)
    assert payload["instruction_following"]["validation"]["passed"] is (not lose_summary)
    assert payload["user_facing_result_verified"] is (not lose_summary)
    assert public.completion_confirmed is (not lose_summary)


def test_unread_embedded_link_is_not_promoted_to_approved_draft_evidence():
    from keystone_agents.schemas.work_item import WorkItem, WorkItemKind, WorkItemSourceRef

    value = draft()
    base = value.outreach_context.company_profile.sources[0]
    unread = WorkItemSourceRef(
        source_id="embedded-link", title="Link mentioned in the email",
        url="https://example.test/article", extraction_status="link_only",
        supported_claim="The article was linked but not read.",
    )
    item = WorkItem(kind=WorkItemKind.GMAIL_THREAD, title="Template", sources=[unread])
    sources = workflow._thread_local_outreach_context_sources(
        item, base_source=base, target_name=""
    )
    assert [source.source_id for source in sources] == [base.source_id]
    assert [fact.source_id for fact in workflow._thread_local_outreach_allowed_facts(sources)] == [
        base.source_id
    ]
    assert item.sources == [unread]  # Discovery context remains available for later research.
    read = unread.model_copy(update={"extraction_status": "provider_context_read"})
    assert workflow._source_record_from_work_item_source(read) is not None


@pytest.mark.parametrize("reply_recommended", [False, True])
def test_template_recipient_does_not_inherit_newsletter_sender(
    tmp_path, monkeypatch, reply_recommended,
):
    from keystone_agents.schemas.email_triage import (
        GmailThreadSummaryMessage,
        GmailThreadSummaryResult,
    )
    from keystone_agents.schemas.work_item import WorkItem, WorkItemKind

    context = GmailThreadSummaryResult(
        thread_id="selected", source_label="gmail_triage_sdk_selected",
        subject="Evaluation pilot", summary="The newsletter describes a pilot.",
        thread_context="A new evaluation pilot was announced.",
        messages=[GmailThreadSummaryMessage(
            message_id="selected", sender_email="publisher@example.test",
            sender_name="Publisher", subject="Evaluation pilot",
        )],
    )
    value = draft()
    monkeypatch.setattr(
        workflow, "_compose_thread_local_outreach_draft_for_work_item",
        lambda **kwargs: (value, "Test synthesis", None, {"reply_recommended": reply_recommended}),
    )
    item = WorkItem(kind=WorkItemKind.GMAIL_THREAD, title="Template", request_text=REQUEST)
    store = SQLiteStore(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    store.save_work_item(item)
    result = workflow._advance_thread_local_outreach_draft(
        item, request=WorkflowRunRequest(request_text=REQUEST), store=store,
        gmail_thread_context=context,
    )
    expected = "publisher@example.test" if reply_recommended else ""
    assert result.artifact_refs[0].metadata["recipient_email"] == expected
    assert result.blockers == []
    assert store.list_approval_items(object_type="outreach_draft") == []


def test_graph_preserves_canonical_private_template_without_invented_reply_recommendation():
    from keystone_agents.langgraph_workflow import _enhance_graph_terminal_summary
    from keystone_agents.schemas.work_item import (
        WorkflowRunResult,
        WorkItem,
        WorkItemArtifactRef,
        WorkItemKind,
        WorkItemRoute,
        WorkItemStatus,
    )

    text = workflow._outreach_response_parts_text(draft())
    item = WorkItem(kind=WorkItemKind.GMAIL_THREAD, title="Template", artifact_refs=[
        WorkItemArtifactRef(
            artifact_type="outreach_draft", artifact_id="template",
            metadata={"canonical_draft_copy": True, "thread_local_slack_draft": True,
                      "model_recommendation": {"reply_recommended": False}},
        )
    ])
    result = WorkflowRunResult(
        work_item=item, route=WorkItemRoute.OUTREACH_COMPOSER,
        status=WorkItemStatus.DONE, human_summary=text, advanced=True,
    )
    enhanced = _enhance_graph_terminal_summary(
        original_request=WorkflowRunRequest(request_text=REQUEST), result=result,
        graph_completion_review={"completed_routes": ["gmail_triage", "outreach_composer"]},
    )
    assert enhanced.human_summary == text
