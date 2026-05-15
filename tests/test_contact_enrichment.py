from __future__ import annotations

from types import SimpleNamespace

from keystone_agents.contact_enrichment import build_contact_enrichment_artifact


def test_contact_enrichment_ranks_named_email_over_org_form() -> None:
    artifact = build_contact_enrichment_artifact(
        company_name="Example Health",
        sources=[
            SimpleNamespace(
                source_id="source:contact",
                title="Example Health partnerships contact",
                url="https://example.com/contact",
                supported_claims=["Email Jane Doe, partnerships lead, at jane@example.com."],
            ),
            SimpleNamespace(
                source_id="source:form",
                title="Example Health partner with us",
                url="https://example.com/partners",
                supported_signal="Contact form for collaborations.",
            ),
        ],
    )

    assert artifact.best_contact_path is not None
    assert artifact.best_contact_path.path_type == "email"
    assert artifact.best_contact_path.value == "jane@example.com"
    assert artifact.candidates[0].email == "jane@example.com"
    assert artifact.alternate_contact_paths


def test_contact_enrichment_falls_back_to_org_path_without_guessing_email() -> None:
    artifact = build_contact_enrichment_artifact(
        company_name="Conference Example",
        sources=[
            SimpleNamespace(
                source_id="source:portal",
                title="Conference Example call for speakers",
                url="https://conference.example.org/speakers/submit",
                supported_signal="Speaker and abstract submission portal.",
                supported_claims=[],
            )
        ],
    )

    assert artifact.best_contact_path is not None
    assert artifact.best_contact_path.path_type == "conference_portal"
    assert artifact.best_contact_path.url == "https://conference.example.org/speakers/submit"
    assert artifact.candidates == []
    assert "No source-backed contact email found." in artifact.missing
