from __future__ import annotations

from keystone_agents.business_research_analyst.context import (
    coerce_contact_context,
    resolve_fixture_path,
)
from keystone_agents.gmail_triage.text import normalize_body_for_triage, style_cta
from keystone_agents.orchestrator.routing import (
    looks_like_company,
    looks_like_email,
    payload_text,
)
from keystone_agents.outreach_composer.text import (
    salutation_name,
)
from keystone_agents.outreach_composer.text import (
    style_cta as outreach_style_cta,
)
from keystone_agents.schemas.email_style import EmailStyleProfile


def test_outreach_text_helpers_are_style_aware() -> None:
    profile = EmailStyleProfile(
        profile_id="style-test",
        approval_state="approved_for_drafting",
        preferred_phrases=["compare notes"],
    )

    assert salutation_name("Dr. Anup Sharma") == "Dr. Anup Sharma"
    assert outreach_style_cta("clinical AI evaluation", profile) == (
        "Happy to compare notes on clinical AI evaluation if useful."
    )


def test_gmail_text_helpers_strip_html_and_apply_context_request_cta() -> None:
    profile = EmailStyleProfile(
        profile_id="style-test",
        approval_state="approved_for_drafting",
        cta_style="context_request",
    )

    assert normalize_body_for_triage("<p>Hello</p><blockquote>old thread</blockquote>") == "Hello"
    assert style_cta("Default CTA.", profile) == (
        "Please send any non-sensitive context that would help me review fit."
    )


def test_business_research_analyst_context_helpers_resolve_and_coerce_fixture() -> None:
    path = resolve_fixture_path("sample_contact_curebase_approved")
    contact = coerce_contact_context({"company_name": "Curebase", "name": "Dr. Example"})

    assert path.name == "sample_contact_curebase_approved.json"
    assert contact is not None
    assert contact.contact_name == "Dr. Example"


def test_orchestrator_routing_helpers_extract_structured_intent() -> None:
    payload = {"subject": "Intro", "from": "a@example.com", "body": "hello"}

    assert "subject: Intro" in payload_text(payload)
    assert looks_like_email(payload, payload_text(payload))
    assert looks_like_company("NeuroFlow")
