"""Offline regressions for second-campaign context-agent execution contracts."""

from __future__ import annotations

from keystone_agents.agent_tool_policy import tool_name_for_policy
from keystone_agents.agents.zotero_context import build_zotero_context_agent
from keystone_agents.capabilities.profile import compile_request_capability_profile
from keystone_agents.entrypoints import cli_impl
from keystone_agents.planning.compatibility import infer_manual_request_plan
from keystone_agents.tools.zotero_context_tools import project_zotero_item_metadata


def test_google_doc_lifecycle_preserves_full_title_and_explicit_body_revision() -> None:
    request = (
        "I approve one exact synthetic KNIOps document lifecycle titled KBA_TEST_DOC "
        "Orchard Handoff: create it with the sentence \"Orchard handoff is ready.\", "
        "read it back, replace the sentence with \"Orchard handoff is verified.\", "
        "confirm the same document changed, then move it to trash and verify it."
    )

    assert cli_impl._google_doc_lifecycle_scope(request, context_text=request) == (
        "KBA_TEST_DOC Orchard Handoff",
        "Orchard handoff is verified.",
        "KNIOps",
    )
    assert cli_impl._google_doc_lifecycle_bodies(request, context_text=request) == (
        "Orchard handoff is ready.",
        "Orchard handoff is verified.",
    )


def test_google_doc_lifecycle_does_not_invent_unspecified_body_copy() -> None:
    request = (
        "Create a KNIOps document titled KBA_TEST_DOC Orchard Handoff with one short "
        "paragraph, replace the paragraph, then trash the test document."
    )

    assert cli_impl._google_doc_lifecycle_bodies(request, context_text=request) == (
        "",
        "",
    )


def test_zotero_latest_article_plan_and_projection_keep_requested_citation_fields() -> None:
    request = (
        "Read Zotero and find the most recently added journal article that has a stored "
        "abstract. Give exactly its title, publication, publication date, DOI or URL, "
        "and a one-sentence abstract gist. Don't include other items or change the library."
    )
    plan = infer_manual_request_plan(
        request,
        requested_agent="zotero_context_agent",
    )

    assert plan.zotero_requested_fields == [
        "title",
        "publication_title",
        "publication_date",
        "doi",
        "url",
        "abstract",
    ]

    projection = project_zotero_item_metadata(
        {
            "key": "ITEM1",
            "data": {
                "itemType": "journalArticle",
                "title": "A bounded article",
                "publicationTitle": "Journal of Bounded Evidence",
                "date": "2026-07-01",
                "DOI": "10.1000/bounded",
                "url": "https://example.org/bounded",
                "abstractNote": "A stored abstract for bounded synthesis.",
            },
        },
        requested_fields=plan.zotero_requested_fields,
    )

    assert projection["fields"] == {
        "title": "A bounded article",
        "publication_title": "Journal of Bounded Evidence",
        "publication_date": "2026-07-01",
        "doi": "10.1000/bounded",
        "url": "https://example.org/bounded",
        "abstract": "A stored abstract for bounded synthesis.",
    }
    assert projection["missing_requested_fields"] == []


def test_zotero_marked_note_lifecycle_compiles_one_exact_capability() -> None:
    request = (
        "I approve one exact standalone Zotero note lifecycle marked KBA_TEST_NOTE: "
        "create it, read it back, revise the wording, confirm the same note and version "
        "changed, then remove it and confirm it is absent."
    )
    plan = infer_manual_request_plan(
        request,
        requested_agent="zotero_context_agent",
    )
    agent = build_zotero_context_agent(
        request_text=request,
        manual_plan=plan,
        tool_tier="internal_write",
    )
    profile = compile_request_capability_profile(
        entrypoint="cli",
        agent=agent,
        execution_shape="direct_specialist",
        prompt_profile="compact",
        max_turns=2,
        provider_operations=plan.provider_operations,
        write_enabled=True,
    )

    assert plan.provider_operations == ["create", "read", "update", "verify", "delete"]
    assert {tool_name_for_policy(tool) for tool in agent.tools} == {
        "zotero_test_note_lifecycle"
    }
    assert profile.tool_names == ("zotero_test_note_lifecycle",)
    assert profile.write_enabled is True
