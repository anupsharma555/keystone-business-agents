"""Natural read-verb admission across the registered context specialists.

These tests are offline only.  They exercise deterministic request planning and
the exact SDK tool surface without running a model or provider.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from keystone_agents.agents.airtable_context import build_airtable_context_agent
from keystone_agents.agents.gmail_triage import build_gmail_triage_agent
from keystone_agents.agents.google_workspace_context import (
    build_google_workspace_context_agent,
)
from keystone_agents.agents.preprints_context import build_preprints_context_agent
from keystone_agents.agents.rss_context import build_rss_context_agent
from keystone_agents.agents.zotero_context import build_zotero_context_agent
from keystone_agents.planning.compatibility import infer_manual_request_plan

Builder = Callable[..., Any]


def _tool_names(agent: Any) -> set[str]:
    return {
        str(getattr(tool, "name", ""))
        for tool in list(getattr(agent, "tools", []) or [])
        if str(getattr(tool, "name", ""))
    }


@pytest.mark.parametrize(
    ("route", "prompt", "builder", "plan_parameter", "required_tools"),
    [
        (
            "airtable_context_agent",
            (
                "Open the latest active partner records in Airtable and tell me "
                "which one needs attention. Please do not change anything."
            ),
            build_airtable_context_agent,
            "manual_plan",
            {"airtable_get_base_schema", "airtable_read_records"},
        ),
        (
            "google_workspace_context_agent",
            (
                "Open the latest operating plan in Google Drive and summarize the "
                "decisions and owners. Please do not change anything."
            ),
            build_google_workspace_context_agent,
            "manual_plan",
            {"google_drive_search_files", "google_drive_get_file_metadata"},
        ),
        (
            "zotero_context_agent",
            (
                "Open the newest paper in Zotero about measurement-based care and "
                "summarize its conclusions. Please do not change anything."
            ),
            build_zotero_context_agent,
            "manual_plan",
            {
                "zotero_read_api_metadata",
                "zotero_read_item_children",
                "zotero_read_pdf_attachment_text",
            },
        ),
        (
            "rss_context_agent",
            (
                "Open the recent RSS announcements and choose the strongest "
                "partnership signal. Please do not checkpoint or change anything."
            ),
            build_rss_context_agent,
            "manual_plan",
            {"retrieve_rss_announcement_history"},
        ),
        (
            "preprints_context_agent",
            (
                "Open the latest preprints and choose the one most relevant to "
                "digital psychiatry. Please do not checkpoint or change anything."
            ),
            build_preprints_context_agent,
            "manual_plan",
            {"retrieve_preprint_announcement_history"},
        ),
        (
            "gmail_triage",
            (
                "Open the four newest G2i emails and choose the current one that "
                "needs a reply. Please do not draft or change anything."
            ),
            build_gmail_triage_agent,
            "manual_request_plan",
            {"inspect_gmail_mailbox_schema", "query_gmail_message_summaries"},
        ),
    ],
)
def test_open_is_admitted_as_a_read_without_granting_a_write(
    route: str,
    prompt: str,
    builder: Builder,
    plan_parameter: str,
    required_tools: set[str],
) -> None:
    plan = infer_manual_request_plan(prompt, requested_agent=route)
    agent = builder(
        request_text=prompt,
        tool_tier="core_read",
        **{plan_parameter: plan},
    )

    assert plan.provider_operations == ["read"]
    assert required_tools <= _tool_names(agent)
    assert {"create", "update", "delete", "attach"}.isdisjoint(plan.provider_operations)


@pytest.mark.parametrize(
    ("read_action", "requested_content"),
    [
        ("View", "tell me what is still unresolved"),
        ("Access", "tell me who owns each action item"),
        ("Pull up", "give me its top priorities"),
        ("Bring up", "tell me whether launch is approved"),
        ("Take a look at", "give me the key dates"),
        ("Go through", "tell me who is responsible for follow-up"),
    ],
)
def test_natural_workspace_read_family_preserves_read_authority(
    read_action: str,
    requested_content: str,
) -> None:
    prompt = (
        f"{read_action} the latest operating plan in Google Drive and "
        f"{requested_content}. Please do not change anything."
    )

    plan = infer_manual_request_plan(
        prompt,
        requested_agent="google_workspace_context_agent",
    )
    agent = build_google_workspace_context_agent(
        request_text=prompt,
        manual_plan=plan,
        tool_tier="core_read",
    )

    assert plan.provider_operations == ["read"]
    assert {
        "google_drive_search_files",
        "google_doc_read",
        "google_sheet_read_table",
        "google_slide_deck_read",
        "google_drive_media_ocr_read",
    } <= _tool_names(agent)


@pytest.mark.parametrize(
    ("route", "provider_boundary"),
    [
        ("airtable_context_agent", "Do not open Airtable or read any records."),
        (
            "google_workspace_context_agent",
            "Do not open Google Drive or read any file contents.",
        ),
        ("zotero_context_agent", "Do not open Zotero or read any library items."),
    ],
)
def test_negated_open_never_grants_provider_read_authority(
    route: str,
    provider_boundary: str,
) -> None:
    prompt = (
        "Using only this supplied sentence, return a one-line internal summary. "
        f"{provider_boundary} Do not create, update, attach, or delete anything."
    )

    plan = infer_manual_request_plan(prompt, requested_agent=route)

    assert plan.provider_operations == []


def test_negated_open_approval_is_not_provider_read_evidence() -> None:
    prompt = (
        "Using only this approved synthetic scenario, write a short organization "
        "introduction here. Do not research, send, save, open an approval, or update "
        "tracking."
    )

    plan = infer_manual_request_plan(prompt, requested_agent="outreach_composer")

    assert plan.provider_operations == []


def test_interrogative_record_selection_admits_bounded_airtable_read() -> None:
    prompt = (
        "Which active partner record in Airtable matches the organization discussed "
        "in yesterday's note? Please do not change anything."
    )

    plan = infer_manual_request_plan(
        prompt,
        requested_agent="airtable_context_agent",
    )
    agent = build_airtable_context_agent(
        request_text=prompt,
        manual_plan=plan,
        tool_tier="core_read",
    )

    assert plan.provider_operations == ["read"]
    assert {
        "airtable_get_base_schema",
        "airtable_read_records",
    } <= _tool_names(agent)


def test_schema_read_survives_separate_record_value_and_write_boundaries() -> None:
    prompt = (
        "Read-only schema question: which Business Expenses fields accept values, "
        "and which are computed? Show field names and types only. Do not read record "
        "values or make any provider changes."
    )

    plan = infer_manual_request_plan(
        prompt,
        requested_agent="airtable_context_agent",
    )
    agent = build_airtable_context_agent(
        request_text=prompt,
        manual_plan=plan,
        tool_tier="internal_write",
    )

    assert plan.provider_operations == ["read"]
    assert _tool_names(agent) == {
        "airtable_get_base_schema",
        "airtable_read_schema_detail",
    }


def test_human_schema_question_admits_schema_without_row_access() -> None:
    prompt = (
        "I'm reviewing our Business Expenses base. Which columns can someone actually "
        "edit, and which are formulas or system-managed? I only need the schema, so "
        "please don't open any rows or change anything."
    )

    plan = infer_manual_request_plan(
        prompt,
        requested_agent="airtable_context_agent",
    )
    agent = build_airtable_context_agent(
        request_text=prompt,
        manual_plan=plan,
        tool_tier="core_read",
    )

    assert plan.provider_operations == ["read"]
    assert _tool_names(agent) == {
        "airtable_get_base_schema",
        "airtable_read_schema_detail",
    }


def test_setup_preamble_does_not_turn_schema_only_request_into_update() -> None:
    prompt = (
        "Before I set up an expense entry, walk me through the Business Expenses "
        "table layout—what fields are manually entered, what is computed, and which "
        "fields Airtable controls. Please don't inspect any records or make changes."
    )

    plan = infer_manual_request_plan(
        prompt,
        requested_agent="airtable_context_agent",
    )
    agent = build_airtable_context_agent(
        request_text=prompt,
        manual_plan=plan,
        tool_tier="core_read",
    )

    assert plan.provider_operations == ["read"]
    assert _tool_names(agent) == {
        "airtable_get_base_schema",
        "airtable_read_schema_detail",
    }


def test_future_add_preamble_does_not_grant_airtable_create_tools() -> None:
    prompt = (
        "Before I add anything, explain the Business Expenses table layout and which "
        "fields are computed. Don't inspect rows or change data."
    )

    plan = infer_manual_request_plan(
        prompt,
        requested_agent="airtable_context_agent",
    )
    agent = build_airtable_context_agent(
        request_text=prompt,
        manual_plan=plan,
        tool_tier="internal_write",
    )

    assert plan.provider_operations == ["read"]
    assert _tool_names(agent) == {
        "airtable_get_base_schema",
        "airtable_read_schema_detail",
    }


@pytest.mark.parametrize(
    "prompt",
    [
        (
            "Can you walk me through the columns and field types in Business "
            "Expenses? Please do not read records, create entries, or edit anything."
        ),
        (
            "I only need the Business Expenses schema and controlled fields. "
            "Avoid opening entries or modifying Airtable."
        ),
    ],
)
def test_schema_request_infers_read_and_admits_only_schema(
    prompt: str,
) -> None:
    plan = infer_manual_request_plan(
        prompt,
        requested_agent="airtable_context_agent",
    )
    agent = build_airtable_context_agent(
        request_text=prompt,
        manual_plan=plan,
        tool_tier="core_read",
    )

    assert plan.provider_operations == ["read"]
    assert _tool_names(agent) == {
        "airtable_get_base_schema",
        "airtable_read_schema_detail",
    }


def test_schema_wording_does_not_override_complete_airtable_access_prohibition() -> None:
    prompt = (
        "Tell me the Airtable schema and fields, but do not read or access Airtable. "
        "Use only this sentence."
    )
    plan = infer_manual_request_plan(
        prompt,
        requested_agent="airtable_context_agent",
    )
    agent = build_airtable_context_agent(
        request_text=prompt,
        manual_plan=plan,
        tool_tier="core_read",
    )

    assert plan.provider_operations == []
    assert _tool_names(agent) == set()


def test_field_wording_does_not_reduce_explicit_airtable_update_to_schema_read() -> None:
    prompt = (
        "In Airtable, update the Description field on this personal expense to "
        "'Software subscription', and verify the same record."
    )
    plan = infer_manual_request_plan(
        prompt,
        requested_agent="airtable_context_agent",
    )

    assert plan.provider_operations == ["update", "verify"]


@pytest.mark.parametrize(
    ("route", "prompt"),
    [
        (
            "zotero_context_agent",
            "In the measurement-based care collection, which recently added article "
            "is most relevant to implementation barriers? Compare the plausible items "
            "and don't create notes or change the library.",
        ),
        (
            "google_workspace_context_agent",
            "Which operating-plan document is the current one? Compare the plausible "
            "files and don't edit or share anything.",
        ),
        (
            "rss_context_agent",
            "Which recent announcement matters most for the launch? Compare the "
            "plausible entries without posting or changing the feed.",
        ),
        (
            "preprints_context_agent",
            "Which recent preprint is most relevant to measurement-based care? Compare "
            "the plausible studies without changing saved state.",
        ),
    ],
)
def test_named_context_agent_owns_natural_selection_questions(
    route: str,
    prompt: str,
) -> None:
    plan = infer_manual_request_plan(prompt, requested_agent=route)

    assert plan.target_agent == route


@pytest.mark.parametrize(
    ("requested_route", "prompt", "expected_route", "expected_tool"),
    [
        (
            "google_workspace_context_agent",
            "Use Airtable instead of Google Drive to compare the active partner "
            "records, and don't change anything.",
            "airtable_context_agent",
            "airtable_read_records",
        ),
        (
            "rss_context_agent",
            "Use the Zotero measurement-based care collection instead of RSS history "
            "to compare the most relevant articles. Don't change the library.",
            "zotero_context_agent",
            "zotero_read_api_metadata",
        ),
        (
            "preprints_context_agent",
            "Use Google Drive instead of the preprint feed to compare the latest "
            "planning files. Don't edit anything.",
            "google_workspace_context_agent",
            "google_drive_search_files",
        ),
    ],
)
def test_named_context_agent_can_delegate_to_explicit_read_only_provider(
    requested_route: str,
    prompt: str,
    expected_route: str,
    expected_tool: str,
) -> None:
    plan = infer_manual_request_plan(prompt, requested_agent=requested_route)
    builders = {
        "airtable_context_agent": build_airtable_context_agent,
        "google_workspace_context_agent": build_google_workspace_context_agent,
        "zotero_context_agent": build_zotero_context_agent,
    }
    agent = builders[expected_route](
        request_text=prompt,
        manual_plan=plan,
        tool_tier="core_read",
    )

    assert plan.target_agent == expected_route
    assert plan.provider_operations == ["read"]
    assert expected_tool in _tool_names(agent)


@pytest.mark.parametrize(
    ("requested_route", "prompt"),
    [
        (
            "airtable_context_agent",
            "Don't access Airtable. Search the public web for Northstar Health's "
            "current partnership evidence and cite the sources.",
        ),
        (
            "zotero_context_agent",
            "Don't use Zotero. Search the public web for current evidence about "
            "implementation barriers and cite the sources.",
        ),
    ],
)
def test_named_context_agent_can_delegate_explicit_public_web_research(
    requested_route: str,
    prompt: str,
) -> None:
    plan = infer_manual_request_plan(prompt, requested_agent=requested_route)

    assert plan.target_agent == "business_research_analyst"
    assert plan.provider_system == "unspecified"
    assert plan.requires_live_search is True


def test_interrogative_selection_does_not_override_provider_access_prohibition() -> None:
    prompt = (
        "Tell me which active partner record matches yesterday's note, but do not "
        "read or access Airtable. Use only the sentence I supplied."
    )

    plan = infer_manual_request_plan(
        prompt,
        requested_agent="airtable_context_agent",
    )

    assert plan.provider_operations == []


@pytest.mark.parametrize(
    "route",
    ["rss_context_agent", "preprints_context_agent"],
)
def test_named_feed_context_negated_live_web_search_keeps_selected_owner(
    route: str,
) -> None:
    prompt = (
        "Use available feed context, not browser automation or live web search. "
        "Return a concise answer and detailed summary."
    )

    plan = infer_manual_request_plan(prompt, requested_agent=route)

    assert plan.target_agent == route
    assert plan.requires_live_search is False
