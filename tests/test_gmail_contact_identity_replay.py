from __future__ import annotations

import json
from types import SimpleNamespace

import keystone_agents.cli as cli
from keystone_agents.gmail_triage.contact_lookup import (
    bind_gmail_contact_lookup_result,
    gmail_contact_lookup_human_summary,
)
from keystone_agents.schemas.email_triage import (
    GmailContactLookupResult,
    GmailResolvedContact,
)
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan


def test_same_thread_contact_aliases_resolve_one_person_and_stable_address() -> None:
    summaries = [
        {
            "id": "msg-approval",
            "threadId": "thread-onboarding",
            "from": "Alex Rivera <alex@messages.vendor.example>",
            "to": "Operator <operator@example.test>",
            "subject": "Startup account approved",
            "snippet": "Your startup account is approved. I can help with onboarding.",
        },
        {
            "id": "msg-followup",
            "threadId": "thread-onboarding",
            "from": "Alex Rivera <alex@vendor.example>",
            "to": "Operator <operator@example.test>",
            "subject": "Re: Startup account approved",
            "snippet": "Following up on the same onboarding steps.",
        },
    ]
    model_result = GmailContactLookupResult(
        found=True,
        answer="Alex Rivera handled the startup onboarding.",
        contacts=[
            GmailResolvedContact(
                message_id="msg-approval",
                thread_id="thread-onboarding",
                contact_name="Alex Rivera",
                contact_email="alex@messages.vendor.example",
                source_field="from",
                relationship="startup onboarding contact",
                evidence_summary="The approval message offers onboarding help.",
            ),
            GmailResolvedContact(
                message_id="msg-followup",
                thread_id="thread-onboarding",
                contact_name="Alex Rivera",
                contact_email="alex@vendor.example",
                source_field="from",
                relationship="startup onboarding contact",
                evidence_summary="The same person follows up in the same thread.",
            ),
        ],
        supporting_message_ids=["msg-approval", "msg-followup"],
        rationale=(
            "Both messages are from Alex Rivera in one onboarding thread; the "
            "shallower vendor domain is the stable direct address."
        ),
        uncertainty=(
            "The approval message used a message-routing subdomain alias for the "
            "same local part and person."
        ),
    )

    bound = bind_gmail_contact_lookup_result(summaries, model_result)

    assert len(bound.contacts) == 1
    assert bound.contacts[0].contact_name == "Alex Rivera"
    assert bound.contacts[0].contact_email == "alex@vendor.example"
    assert bound.supporting_message_ids == ["msg-approval", "msg-followup"]
    assert gmail_contact_lookup_human_summary(bound) == (
        "The best-supported contact is Alex Rivera <alex@vendor.example> "
        "(startup onboarding contact)."
    )


def test_same_thread_contact_alias_order_does_not_change_stable_address() -> None:
    summaries = [
        {
            "id": "msg-direct",
            "threadId": "thread-review",
            "from": "Jordan Lee <jordan@partner.example>",
            "to": "Operator <operator@example.test>",
        },
        {
            "id": "msg-routed",
            "threadId": "thread-review",
            "from": "Jordan Lee <jordan@updates.partner.example>",
            "to": "Operator <operator@example.test>",
        },
    ]
    contacts = [
        GmailResolvedContact(
            message_id="msg-routed",
            thread_id="thread-review",
            contact_name="Jordan Lee",
            contact_email="jordan@updates.partner.example",
            source_field="from",
            relationship="application review contact",
            evidence_summary="A routed alias continues the review thread.",
        ),
        GmailResolvedContact(
            message_id="msg-direct",
            thread_id="thread-review",
            contact_name="Jordan Lee",
            contact_email="jordan@partner.example",
            source_field="from",
            relationship="application review contact",
            evidence_summary="The direct address appears in the same review thread.",
        ),
    ]
    model_result = GmailContactLookupResult(
        found=True,
        answer="Jordan Lee handled the application review.",
        contacts=contacts,
        supporting_message_ids=["msg-routed", "msg-direct"],
        rationale="Both verified messages identify one person in one thread.",
        uncertainty="The routed address may be a delivery alias.",
    )

    bound = bind_gmail_contact_lookup_result(summaries, model_result)

    assert len(bound.contacts) == 1
    assert bound.contacts[0].message_id == "msg-direct"
    assert bound.contacts[0].contact_email == "jordan@partner.example"
    assert bound.supporting_message_ids == ["msg-routed", "msg-direct"]
    assert bound.uncertainty == "The routed address may be a delivery alias."
    assert bound.provider_write is False
    assert bound.send_enabled is False


def test_same_thread_contact_alias_remaps_mismatched_message_id_to_exact_header() -> None:
    summaries = [
        {
            "id": "msg-routed",
            "threadId": "thread-onboarding",
            "from": "Alex Rivera <alex@messages.vendor.example>",
            "to": "Operator <operator@example.test>",
        },
        {
            "id": "msg-direct",
            "threadId": "thread-onboarding",
            "from": "Alex Rivera <alex@vendor.example>",
            "to": "Operator <operator@example.test>",
        },
    ]
    model_result = GmailContactLookupResult(
        found=True,
        answer="Alex Rivera handled the startup onboarding.",
        contacts=[
            GmailResolvedContact(
                message_id="msg-routed",
                thread_id="thread-onboarding",
                contact_name="Alex Rivera",
                contact_email="alex@vendor.example",
                source_field="from",
                relationship="startup onboarding contact",
                evidence_summary=(
                    "The direct address appears in the same provider-returned thread."
                ),
            )
        ],
        supporting_message_ids=["msg-routed"],
        rationale="The selected Gmail thread identifies the onboarding contact.",
    )

    bound = bind_gmail_contact_lookup_result(summaries, model_result)

    assert len(bound.contacts) == 1
    assert bound.contacts[0].message_id == "msg-direct"
    assert bound.contacts[0].contact_email == "alex@vendor.example"
    assert bound.supporting_message_ids == ["msg-direct"]


def test_contact_alias_does_not_remap_across_provider_threads() -> None:
    summaries = [
        {
            "id": "msg-routed",
            "threadId": "thread-onboarding",
            "from": "Alex Rivera <alex@messages.vendor.example>",
            "to": "Operator <operator@example.test>",
        },
        {
            "id": "msg-direct",
            "threadId": "thread-unrelated",
            "from": "Alex Rivera <alex@vendor.example>",
            "to": "Operator <operator@example.test>",
        },
    ]
    model_result = GmailContactLookupResult(
        found=True,
        answer="Alex Rivera handled the startup onboarding.",
        contacts=[
            GmailResolvedContact(
                message_id="msg-routed",
                thread_id="thread-onboarding",
                contact_name="Alex Rivera",
                contact_email="alex@vendor.example",
                source_field="from",
                relationship="startup onboarding contact",
                evidence_summary="The model associated the wrong provider message.",
            )
        ],
        supporting_message_ids=["msg-routed"],
        rationale="The model proposed one contact.",
    )

    try:
        bind_gmail_contact_lookup_result(summaries, model_result)
    except ValueError as exc:
        assert "not present" in str(exc)
    else:
        raise AssertionError("Cross-thread contact evidence must not be rebound.")


def test_same_display_name_without_alias_evidence_remains_separate() -> None:
    summaries = [
        {
            "id": "msg-first",
            "threadId": "thread-shared",
            "from": "Casey Morgan <casey@first.example>",
            "to": "Operator <operator@example.test>",
        },
        {
            "id": "msg-second",
            "threadId": "thread-shared",
            "from": "Casey Morgan <support@second.example>",
            "to": "Operator <operator@example.test>",
        },
    ]
    model_result = GmailContactLookupResult(
        found=True,
        answer="Two contacts share the same display name.",
        contacts=[
            GmailResolvedContact(
                message_id="msg-first",
                thread_id="thread-shared",
                contact_name="Casey Morgan",
                contact_email="casey@first.example",
                source_field="from",
                evidence_summary="The first verified sender.",
            ),
            GmailResolvedContact(
                message_id="msg-second",
                thread_id="thread-shared",
                contact_name="Casey Morgan",
                contact_email="support@second.example",
                source_field="from",
                evidence_summary="The second verified sender.",
            ),
        ],
        supporting_message_ids=["msg-first", "msg-second"],
        rationale="The bounded evidence does not establish one identity.",
        uncertainty="The shared display name may represent different people.",
    )

    bound = bind_gmail_contact_lookup_result(summaries, model_result)

    assert [contact.contact_email for contact in bound.contacts] == [
        "casey@first.example",
        "support@second.example",
    ]
    assert bound.uncertainty == "The shared display name may represent different people."


def test_contact_lookup_child_promotion_keeps_artifact_review_non_terminal(
    monkeypatch,
    capsys,
) -> None:
    request = (
        "Who was the Acme Compute person who helped get my startup account set up? "
        "Give me their email here, and don't change anything."
    )
    plan = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="gmail_triage",
        intent="gmail_triage",
        primary_target="Acme Compute",
        target_type="gmail_message_collection",
        provider_system="gmail",
        provider_operations=["search", "read"],
        provider_read_scope="bounded_collection",
        provider_result_mode="items",
        gmail_mailbox_direction="any",
        gmail_requested_fields=["sender", "subject", "date", "snippet"],
        gmail_query='"Acme Compute"',
        task_objective="contact_discovery",
        expected_artifact_type="contact_candidates",
    )
    child_payload = {
        "status": "completed",
        "human_summary": (
            "The best-supported contact is Alex Rivera <alex@acme.example> "
            "(startup onboarding contact)."
        ),
        "output_type": "GmailContactLookupResult",
        "output": {
            "found": True,
            "answer": "Alex Rivera handled the startup onboarding.",
            "contacts": [
                {
                    "message_id": "msg-direct",
                    "thread_id": "thread-onboarding",
                    "contact_name": "Alex Rivera",
                    "contact_email": "alex@acme.example",
                    "source_field": "from",
                    "relationship": "startup onboarding contact",
                    "evidence_summary": (
                        "Two selected messages identify the same person; the stable "
                        "base-domain address is shown."
                    ),
                }
            ],
            "supporting_message_ids": ["msg-routed", "msg-direct"],
            "rationale": (
                "Both provider messages are in the same onboarding thread and identify "
                "one person."
            ),
            "uncertainty": (
                "One message used a routed subdomain alias for the same local part."
            ),
            "provider_write": False,
            "send_enabled": False,
        },
        "tool_receipts": [
            {
                "provider": "gmail",
                "operation": "search_and_read_contact_evidence",
                "candidate_count": 2,
                "selected_message_ids": ["msg-routed", "msg-direct"],
                "provider_read": True,
                "provider_write": False,
                "verified": True,
            }
        ],
        "user_facing_result_verified": True,
        "public_result": {
            "status": "completed",
            "completion_confirmed": True,
            "provider_write_attempted": False,
            "provider_receipt_verified": True,
        },
        "send_enabled": False,
    }
    monkeypatch.setattr(
        cli,
        "run_isolated_child_process",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(child_payload),
            stderr="",
        ),
    )

    exit_code = cli._run_ask_script_live(
        "gmail_triage",
        request,
        ["python", "synthetic-gmail-child.py"],
        json_output=True,
        manual_plan=plan,
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["specialist_output_review"]["status"] == "fail"
    assert "orchestrator_review" not in payload
    assert payload["child_result_promotion_receipt"]["reader_ready"] is True
    assert payload["child_result_promotion_receipt"]["verification_basis"] == [
        "verified_child_public_result"
    ]
    assert payload["public_result"]["status"] == "completed"
    assert payload["public_result"]["completion_confirmed"] is True
    assert payload["public_result"]["provider_write_attempted"] is False
    assert payload["slack_display_text"] == child_payload["human_summary"]
    assert payload["script_payload"]["tool_receipts"][0]["verified"] is True
