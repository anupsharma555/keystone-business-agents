from __future__ import annotations

import zipfile

from keystone_agents.founder_profile import (
    build_founder_cv_review_packet,
    founder_drafting_context,
    founder_profile_audit_payload,
    founder_profile_claims,
    founder_search_context,
    load_founder_fit_profile,
)


def test_founder_fit_profile_contexts_use_approval_flags() -> None:
    profile = load_founder_fit_profile("tests/fixtures/founder_fit_profile_approved.json")

    search_context = founder_search_context(profile)
    drafting_context = founder_drafting_context(profile)
    claims = founder_profile_claims(profile)

    assert "founder_fit_test" in search_context
    assert "clinical AI" in search_context
    assert "allowed_outreach_claims" in drafting_context
    assert claims
    assert claims[0].source_id == "founder_fit_profile:founder_fit_test"


def test_default_founder_profile_covers_expanded_opportunity_identity_context() -> None:
    profile = load_founder_fit_profile("documents/founder_fit_profile.json")
    assert profile is not None

    context = founder_search_context(profile)

    assert "strategic_priorities" in context
    assert "opportunity_lanes" in context
    assert "access_preferences" in context
    assert "conferences and speaking calls" in context
    assert "industry-sponsored collaborations and pilots" in context
    assert "Remote, virtual, or online opportunities are preferred for now." in context

    audit = founder_profile_audit_payload("documents/founder_fit_profile.json", profile)
    assert audit["search_context_complete"] is True
    assert set(audit["search_context_fields"]) >= {
        "summary",
        "strategic_priorities",
        "opportunity_lanes",
        "access_preferences",
        "public_links",
    }


def test_founder_cv_review_packet_extracts_docx_metadata(tmp_path) -> None:
    docx_path = tmp_path / "cv.docx"
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>Clinical AI and psychiatry experience.</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    with zipfile.ZipFile(docx_path, "w") as archive:
        archive.writestr("word/document.xml", document_xml)

    packet = build_founder_cv_review_packet(docx_path)

    assert packet["status"] == "review_required"
    assert packet["paragraph_count"] == 1
    assert "extracted_text" not in packet
