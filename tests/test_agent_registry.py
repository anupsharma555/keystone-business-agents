from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from keystone_agents.agent_registry import (
    AGENT_REGISTRY,
    REGISTERED_AGENT_SPECS,
    SPECIALIST_AGENT_SPECS,
    agent_cards,
    specialist_handoff_specs,
)
from keystone_agents.agent_tool_policy import (
    AgentToolPolicyError,
    ToolTier,
    allowed_tool_names_for_tier,
    disallowed_tool_names,
    source_layer_policy_for_tools,
    tool_policy_for_agent,
    tool_tier_for_name,
    unclassified_tool_names,
)
from keystone_agents.agents.business_research_analyst import (
    build_business_research_analyst_research_brief_agent,
)
from keystone_agents.agents.opportunity_scout import build_opportunity_scout_agent
from keystone_agents.agents.orchestrator import INTENDED_HANDOFFS, build_orchestrator_agent
from keystone_agents.sdk import (
    Agent,
    build_model_settings,
    build_sdk_agent,
    prompt_metadata_for_files,
    skill_metadata_for_files,
)
from keystone_agents.skill_sets import AGENT_SKILL_NAMES
from keystone_agents.specialist_tool_names import specialist_agent_tool_name
from keystone_agents.tools.gmail_tool import get_gmail_message

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_ROOT = PROJECT_ROOT / "src" / "keystone_agents" / "prompts"
SKILLS_ROOT = PROJECT_ROOT / "src" / "keystone_agents" / "skills"

STATIC_PREFIX_FINGERPRINTS = {
    "gmail_triage": {
        "instructions_sha256": "89486fa423c7635bf00d80387ad5b207e2e9da7d5427af2effe938c8946cd4e9",
        "tool_names_sha256": "b97b3dda055d85e98d851db73b3015000dca0c65fb172be0e1f60fa6e2b14e42",
        "output_schema_sha256": "519be6127e040ff904066e7dd8efe671ce88a3c4c527bdcb730a6e5f131d3345",
    },
    "business_research_analyst": {
        "instructions_sha256": "5df1682344565f30f7f93c73ee96af46484d3bab6867affe9d2e0a3e87a01c71",
        "tool_names_sha256": "a9f26c0e0237174742fdc53f1c4e7ce5c8936f492b353f5ef6e3adfb4c34a35e",
        "output_schema_sha256": "b8218a333d85d2f3850203f5ee48b7ec535a6f924a8851c513f1f2b2afeef6e0",
    },
    "opportunity_scout": {
        "instructions_sha256": "f87f46790d41cd896e056d98877b0b0dd188b7374dbe798ee7094a82bacb8567",
        "tool_names_sha256": "ace37ad8d7eef4c988ac31f6848c03a502b9dfb1efe502400c1720e9cc194ce1",
        "output_schema_sha256": "eea0e07dc95ce476794bae691207f241c160ab5e5e22c18036030f137ce907fc",
    },
    "outreach_composer": {
        "instructions_sha256": "0bcf141203edda855b958029c11ddffd0d4dc0c32dc71847eb4ee48bf165bc4f",
        "tool_names_sha256": "6092ed57d03b82170c380457199725a537ab0d16cf199a5393920a1b096815fe",
        "output_schema_sha256": "167da45f0bb07c0a255c4115b52e9510272a22cc1c479abe9e97c88693d44b34",
    },
    "airtable_context_agent": {
        "instructions_sha256": "c111ee36dbcf0dffe496def016046b2d1b2eabc15e911dbec19e48b555583c5a",
        "tool_names_sha256": "ed16e9723a4e18dfc9651d0320d9cd10e9056b06dce5c60a9c5f0044ab36b998",
        "output_schema_sha256": "7d4017a4e6833b52f2c408fe16352594a5fa2b393e740433a25b77b2934c4984",
    },
    "google_workspace_context_agent": {
        "instructions_sha256": "d1955f30061601e2c4a21f138041f62328892f18b49ebc02e7746050e53a2631",
        "tool_names_sha256": "37563d600dab82f4c57154b5a74f90272db02e509bba9f0e117659d0149a8aae",
        "output_schema_sha256": "1b82a4db79f4e9f017351bafb57dbad7399b5c05e6e7ba528d97abbe7f1be506",
    },
    "zotero_context_agent": {
        "instructions_sha256": "e111db03587d6d74f3b12a5ab47d67bab65195c4fdf0bd5fbd10da6be1614a07",
        "tool_names_sha256": "a75e6b6a2d3991af1d02b9d4ffc7af2ba89e240791382ca696117959176455c4",
        "output_schema_sha256": "efa1b731da05cfd915b319d730cf84019d76dd22d0a5f199818546deedac5bb8",
    },
    "rss_context_agent": {
        "instructions_sha256": "b7763763edeb1ca0ac72358383cba19a67e1d363a00d6af2411657fba029d41c",
        "tool_names_sha256": "f31fcf99ce67500ab85ceb6130f6c81c8fdeea9668c82ffd10eaf4862b47a55f",
        "output_schema_sha256": "c81421b84589b67baca97d6ae5bc0a8468adbe9bd3b10d1c13639a3148d68936",
    },
    "preprints_context_agent": {
        "instructions_sha256": "09202172bff8e756ae3549a4455d20ee58a036396284bd28faae6a9df1de9eee",
        "tool_names_sha256": "985bb3e5f395ac4fdab0e2243e450e60eaeab20c34646431415a94dbbb9bef08",
        "output_schema_sha256": "0c360ba87ef900bd5c658029374a445b6c14c22b9067207d8b7ad083c4b1d702",
    },
    "orchestrator": {
        "instructions_sha256": "f8fc7e4111c8fe465b76b08ec81e128c9ab4e9a4ba757ec9c46e5d5994e54cbf",
        "tool_names_sha256": "d1452b1d9a45ccd89c167d726fc4d4cdf612b8834f6616f7e08524efa4ae0817",
        "output_schema_sha256": "98f0e154658a465df86c498fbf9b1a6c84028fbd02eedef97d1549ef5b5887e7",
    },
    "chief_of_staff": {
        "instructions_sha256": "66cd0a1f8e8ef855fb66b16a13372795dfbddea426146e0fbd85d44409727196",
        "tool_names_sha256": "5d10d910810cfa07005226805bbaf378f7420af4d3133d8d88d9db8667f804ff",
        "output_schema_sha256": "c172f11e63550cf751d67a22c72020c661cf35d7a9f6b118274dd7ffdea1e5b9",
    },
}


def _tool_names(agent: Agent) -> set[str]:
    return {getattr(tool, "name", "") for tool in agent.tools}


def _ordered_tool_names(agent: Agent) -> list[str]:
    return [str(getattr(tool, "name", getattr(tool, "__name__", "")) or "") for tool in agent.tools]


def _sha256(value: object) -> str:
    if isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _static_prefix_fingerprint(agent: Agent) -> dict[str, str]:
    schema = agent.output_type.model_json_schema() if agent.output_type is not None else {}
    return {
        "instructions_sha256": _sha256(str(agent.instructions)),
        "tool_names_sha256": _sha256(_ordered_tool_names(agent)),
        "output_schema_sha256": _sha256(schema),
    }


def test_registry_has_canonical_agents() -> None:
    assert set(AGENT_REGISTRY) == {
        "gmail_triage",
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
        "airtable_context_agent",
        "google_workspace_context_agent",
        "zotero_context_agent",
        "rss_context_agent",
        "preprints_context_agent",
        "orchestrator",
        "chief_of_staff",
    }
    assert [spec.route_name for spec in REGISTERED_AGENT_SPECS] == [
        "gmail_triage",
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
        "airtable_context_agent",
        "google_workspace_context_agent",
        "zotero_context_agent",
        "rss_context_agent",
        "preprints_context_agent",
        "orchestrator",
        "chief_of_staff",
    ]


def test_registered_agents_have_builders_schemas_prompts_and_validation() -> None:
    for spec in REGISTERED_AGENT_SPECS:
        assert spec.builder_name.startswith("build_")
        assert issubclass(spec.resolve_output_schema(), BaseModel)
        assert spec.handoff_description
        assert spec.safety_notes
        assert spec.eval_datasets or spec.validation_paths

        for prompt_file in spec.prompt_files:
            assert (PROMPTS_ROOT / prompt_file).exists()
        metadata = prompt_metadata_for_files(spec.prompt_files)
        assert all(item["name"] for item in metadata)
        assert all(item["version"] for item in metadata)

        skill_metadata = skill_metadata_for_files(spec.skills)
        assert spec.skills == AGENT_SKILL_NAMES[spec.route_name]
        assert len(skill_metadata) == len(spec.skills)
        for skill_name, item in zip(spec.skills, skill_metadata, strict=True):
            assert (SKILLS_ROOT / skill_name / "SKILL.md").exists()
            assert item["skill_id"] == skill_name
            assert item["version"]
            assert item["purpose"]
            assert item["safety_notes"]
            assert spec.route_name in item["applies_to"]
            assert item["eval_datasets"] or item["validation_paths"]
            for eval_path in (*item["eval_datasets"], *item["validation_paths"]):
                assert (PROJECT_ROOT / eval_path).exists(), eval_path

        for eval_path in (*spec.eval_datasets, *spec.validation_paths):
            assert (PROJECT_ROOT / eval_path).exists(), eval_path


def test_source_layer_policy_separates_local_hosted_and_public_search() -> None:
    policy = source_layer_policy_for_tools(
        (
            "search_web",
            "file_search",
            "list_kni_document_folder",
            "list_kni_document_sources",
            "search_kni_documents",
            "read_kni_document_file",
        )
    )
    by_layer = {str(item["layer"]): item for item in policy}

    assert set(by_layer) == {
        "local_kni_documents",
        "hosted_file_search",
        "public_web_search",
    }
    assert "local KNI folder evidence" in " ".join(
        str(item) for item in by_layer["hosted_file_search"]["not_for"]
    )
    assert "current public facts" in " ".join(
        str(item) for item in by_layer["public_web_search"]["use_for"]
    )
    assert "latest user question" in str(
        by_layer["local_kni_documents"]["reasoning_contract"]
    )
    assert "organizer" in str(by_layer["local_kni_documents"]["reasoning_contract"])
    assert "registered agent" in str(
        by_layer["local_kni_documents"]["reasoning_contract"]
    )


def test_registered_builders_match_declared_schema_and_tools() -> None:
    for spec in REGISTERED_AGENT_SPECS:
        agent = spec.build_agent()
        assert isinstance(agent, Agent)
        assert agent.output_type is spec.resolve_output_schema()
        assert set(spec.tools) <= _tool_names(agent)
        assert agent.handoff_description
        assert agent.input_guardrails
        assert agent.output_guardrails


def test_context_agents_support_direct_writes_but_nested_tier_is_advisory() -> None:
    for route_name in (
        "airtable_context_agent",
        "google_workspace_context_agent",
        "zotero_context_agent",
        "rss_context_agent",
        "preprints_context_agent",
    ):
        spec = AGENT_REGISTRY[route_name]
        assert spec.eval_datasets == ("promptfoo/tests/slack_agent_expansion_15.yaml",)
        assert spec.handoff_enabled is True

    airtable = AGENT_REGISTRY["airtable_context_agent"].build_agent()
    workspace = AGENT_REGISTRY["google_workspace_context_agent"].build_agent()
    zotero = AGENT_REGISTRY["zotero_context_agent"].build_agent()
    rss = AGENT_REGISTRY["rss_context_agent"].build_agent()
    preprints = AGENT_REGISTRY["preprints_context_agent"].build_agent()
    airtable_tool_names = _tool_names(airtable)
    workspace_tool_names = _tool_names(workspace)
    zotero_tool_names = _tool_names(zotero)
    rss_tool_names = _tool_names(rss)
    preprints_tool_names = _tool_names(preprints)

    assert {"airtable_get_base_schema", "airtable_read_records"} <= airtable_tool_names
    assert "airtable_write_record" in airtable_tool_names
    assert "airtable_upload_attachment" in airtable_tool_names
    assert "airtable_link_attachment" in airtable_tool_names
    assert "airtable_create_expense_from_receipt" in airtable_tool_names
    assert "airtable_delete_test_record" in airtable_tool_names
    assert "airtable_test_record_lifecycle" in airtable_tool_names
    assert workspace_tool_names == {
        "google_doc_read",
        "google_doc_write",
        "google_doc_trash",
        "google_drive_list_folder",
        "google_drive_search_files",
        "google_drive_get_file_metadata",
        "google_slide_deck_read",
        "presentation_search_local",
        "presentation_read_local",
        "presentation_extract_slide_copy_local",
        "presentation_delete_test_artifact_local",
        "google_drive_create_folder",
        "google_drive_rename_folder",
        "google_drive_remove_folder",
        "google_sheet_list",
        "google_sheet_create",
        "google_sheet_read_table",
        "google_sheet_append_rows",
        "google_sheet_update_row",
        "google_sheet_delete_rows",
        "google_sheet_create_tab",
        "google_sheet_update_tab",
        "google_sheet_remove_tab",
        "google_sheet_trash",
    }
    assert {
        "list_local_context_sources",
        "search_local_context",
        "read_local_context_file",
        "zotero_resolve_collection_context",
        "zotero_resolve_article_context",
        "zotero_read_api_metadata",
        "zotero_import_article_with_backend",
        "zotero_write_test_note",
        "zotero_delete_test_note",
        "zotero_test_note_lifecycle",
        "zotero_write_test_collection",
        "zotero_delete_test_collection",
        "zotero_write_test_item",
        "zotero_delete_test_item",
        "google_drive_list_folder",
        "google_drive_search_files",
        "google_drive_get_file_metadata",
        "google_doc_read",
        "google_doc_write",
        "google_drive_create_folder",
        "google_sheet_list",
        "google_sheet_create",
        "google_sheet_read_table",
        "google_sheet_append_rows",
        "google_sheet_update_row",
    } <= zotero_tool_names
    assert disallowed_tool_names("airtable_context_agent", sorted(airtable_tool_names)) == []
    assert disallowed_tool_names(
        "google_workspace_context_agent",
        sorted(workspace_tool_names),
    ) == []
    assert disallowed_tool_names("zotero_context_agent", sorted(zotero_tool_names)) == []
    assert rss_tool_names == {"retrieve_rss_announcement_history"}
    assert preprints_tool_names == {"retrieve_preprint_announcement_history"}
    assert disallowed_tool_names("rss_context_agent", sorted(rss_tool_names)) == []
    assert disallowed_tool_names("preprints_context_agent", sorted(preprints_tool_names)) == []
    assert tool_tier_for_name("airtable_write_record") == ToolTier.INTERNAL_WRITE
    assert tool_tier_for_name("airtable_upload_attachment") == ToolTier.INTERNAL_WRITE
    assert tool_tier_for_name("airtable_create_expense_from_receipt") == ToolTier.INTERNAL_WRITE
    assert tool_tier_for_name("airtable_delete_test_record") == ToolTier.INTERNAL_WRITE
    assert tool_tier_for_name("airtable_test_record_lifecycle") == ToolTier.INTERNAL_WRITE
    assert tool_tier_for_name("send_gmail_test_draft") == ToolTier.INTERNAL_WRITE
    assert tool_tier_for_name("create_gmail_draft_with_attachment") == ToolTier.INTERNAL_WRITE
    assert tool_tier_for_name("create_google_calendar_event") == ToolTier.INTERNAL_WRITE
    assert tool_tier_for_name("update_google_calendar_event") == ToolTier.INTERNAL_WRITE
    assert tool_tier_for_name("delete_google_calendar_event") == ToolTier.INTERNAL_WRITE
    assert tool_tier_for_name("google_doc_write") == ToolTier.INTERNAL_WRITE
    assert tool_tier_for_name("google_doc_trash") == ToolTier.INTERNAL_WRITE
    assert tool_tier_for_name("presentation_extract_slide_copy_local") == ToolTier.INTERNAL_WRITE
    assert tool_tier_for_name("presentation_delete_test_artifact_local") == ToolTier.INTERNAL_WRITE
    assert tool_tier_for_name("zotero_import_article_with_backend") == ToolTier.INTERNAL_WRITE
    assert tool_tier_for_name("zotero_write_test_note") == ToolTier.INTERNAL_WRITE
    assert tool_tier_for_name("zotero_delete_test_note") == ToolTier.INTERNAL_WRITE
    assert tool_tier_for_name("zotero_test_note_lifecycle") == ToolTier.INTERNAL_WRITE
    assert tool_tier_for_name("zotero_write_test_collection") == ToolTier.INTERNAL_WRITE
    assert tool_tier_for_name("zotero_delete_test_collection") == ToolTier.INTERNAL_WRITE
    assert tool_tier_for_name("zotero_write_test_item") == ToolTier.INTERNAL_WRITE
    assert tool_tier_for_name("zotero_delete_test_item") == ToolTier.INTERNAL_WRITE
    assert tool_tier_for_name("retrieve_rss_announcement_history") == ToolTier.CORE_READ
    assert tool_tier_for_name("retrieve_preprint_announcement_history") == ToolTier.CORE_READ

    nested_airtable = AGENT_REGISTRY["airtable_context_agent"].build_agent(
        tool_tier=ToolTier.DIAGNOSTIC
    )
    nested_workspace = AGENT_REGISTRY["google_workspace_context_agent"].build_agent(
        tool_tier=ToolTier.DIAGNOSTIC
    )
    nested_zotero = AGENT_REGISTRY["zotero_context_agent"].build_agent(
        tool_tier=ToolTier.DIAGNOSTIC
    )
    nested_rss = AGENT_REGISTRY["rss_context_agent"].build_agent(
        tool_tier=ToolTier.DIAGNOSTIC
    )
    nested_preprints = AGENT_REGISTRY["preprints_context_agent"].build_agent(
        tool_tier=ToolTier.DIAGNOSTIC
    )
    nested_tool_names = (
        _tool_names(nested_airtable)
        | _tool_names(nested_workspace)
        | _tool_names(nested_zotero)
        | _tool_names(nested_rss)
        | _tool_names(nested_preprints)
    )
    assert "airtable_write_record" not in nested_tool_names
    assert "airtable_upload_attachment" not in nested_tool_names
    assert "airtable_create_expense_from_receipt" not in nested_tool_names
    assert "google_drive_get_file_metadata" in nested_tool_names
    assert "google_doc_write" not in nested_tool_names
    assert "google_doc_trash" not in nested_tool_names
    assert "google_sheet_append_rows" not in nested_tool_names
    assert "zotero_import_article_with_backend" not in nested_tool_names
    assert "retrieve_rss_announcement_history" in nested_tool_names
    assert "retrieve_preprint_announcement_history" in nested_tool_names


def test_build_model_settings_defaults_to_usage_and_prompt_cache(monkeypatch) -> None:
    monkeypatch.delenv("KEYSTONE_SDK_INCLUDE_USAGE", raising=False)
    monkeypatch.delenv("KEYSTONE_SDK_PROMPT_CACHE_RETENTION", raising=False)

    settings = build_model_settings(reasoning_effort="low", max_tokens=500)

    assert getattr(settings, "include_usage", None) is True
    assert getattr(settings, "prompt_cache_retention", None) == "24h"


def test_build_sdk_agent_applies_cache_friendly_defaults(monkeypatch) -> None:
    monkeypatch.delenv("KEYSTONE_SDK_INCLUDE_USAGE", raising=False)
    monkeypatch.delenv("KEYSTONE_SDK_PROMPT_CACHE_RETENTION", raising=False)

    agent = build_sdk_agent(
        name="cache_default_test",
        instructions="Keystone test agent.",
        output_type=None,
        tools=[],
        policy_agent_name=None,
    )

    assert getattr(agent.model_settings, "include_usage", None) is True
    assert getattr(agent.model_settings, "prompt_cache_retention", None) == "24h"


def test_build_sdk_agent_allows_cache_defaults_to_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("KEYSTONE_SDK_INCLUDE_USAGE", "false")
    monkeypatch.setenv("KEYSTONE_SDK_PROMPT_CACHE_RETENTION", "off")

    agent = build_sdk_agent(
        name="cache_disabled_test",
        instructions="Keystone test agent.",
        output_type=None,
        tools=[],
        policy_agent_name=None,
    )

    assert getattr(agent.model_settings, "include_usage", None) is False
    assert getattr(agent.model_settings, "prompt_cache_retention", None) is None


def test_registered_agents_use_cache_friendly_model_settings(monkeypatch) -> None:
    monkeypatch.delenv("KEYSTONE_SDK_INCLUDE_USAGE", raising=False)
    monkeypatch.delenv("KEYSTONE_SDK_PROMPT_CACHE_RETENTION", raising=False)

    for spec in REGISTERED_AGENT_SPECS:
        agent = spec.build_agent()

        assert getattr(agent.model_settings, "include_usage", None) is True
        assert getattr(agent.model_settings, "prompt_cache_retention", None) == "24h"


def test_registered_agent_tools_follow_controlled_tool_policy() -> None:
    for spec in REGISTERED_AGENT_SPECS:
        policy = tool_policy_for_agent(spec.route_name)
        assert policy is not None
        agent = spec.build_agent()
        assert disallowed_tool_names(spec.route_name, sorted(_tool_names(agent))) == []

    outreach_policy = tool_policy_for_agent("outreach_composer")
    assert outreach_policy is not None
    assert "search_web" in outreach_policy.allowed_tool_names
    assert "get_gmail_message" not in outreach_policy.allowed_tool_names


def test_registered_tool_policies_have_complete_tier_classification() -> None:
    for spec in REGISTERED_AGENT_SPECS:
        policy = tool_policy_for_agent(spec.route_name)
        assert policy is not None
        assert unclassified_tool_names(policy.allowed_tool_names) == []


def test_tool_tiers_keep_search_and_write_surfaces_separate() -> None:
    research_core = allowed_tool_names_for_tier(
        "business_research_analyst",
        ToolTier.CORE_READ,
    )
    research_web = allowed_tool_names_for_tier(
        "business_research_analyst",
        ToolTier.WEB_SEARCH,
    )
    research_deep = allowed_tool_names_for_tier(
        "business_research_analyst",
        ToolTier.DEEP_RETRIEVAL,
    )
    scout_diagnostic = allowed_tool_names_for_tier(
        "opportunity_scout",
        ToolTier.DIAGNOSTIC,
    )

    assert "search_web" not in research_core
    assert "search_web" in research_web
    assert "fetch_company_page" not in research_web
    assert "extract_research_claims_from_html" in research_deep
    assert "airtable_write_record" not in research_deep
    assert "airtable_upload_attachment" not in research_deep
    assert "google_sheet_append_rows" not in research_deep
    assert "render_page" in scout_diagnostic
    assert "save_opportunity_memory" not in scout_diagnostic


def test_search_heavy_agent_builders_support_tiered_tool_attachment() -> None:
    research_core = build_business_research_analyst_research_brief_agent(tool_tier="core_read")
    research_deep = build_business_research_analyst_research_brief_agent(tool_tier="deep_retrieval")
    scout_web = build_opportunity_scout_agent(tool_tier="web_search")
    scout_diagnostic = build_opportunity_scout_agent(tool_tier="diagnostic")

    research_core_tools = _tool_names(research_core)
    research_deep_tools = _tool_names(research_deep)
    scout_web_tools = _tool_names(scout_web)
    scout_diagnostic_tools = _tool_names(scout_diagnostic)

    assert "search_web" not in research_core_tools
    assert "search_web" in research_deep_tools
    assert "extract_research_claims_from_html" in research_deep_tools
    assert "airtable_write_record" not in research_deep_tools
    assert "airtable_upload_attachment" not in research_deep_tools
    assert "search_web" in scout_web_tools
    assert "render_page" not in scout_web_tools
    assert "render_page" in scout_diagnostic_tools
    assert "save_opportunity_memory" not in scout_diagnostic_tools


def test_search_capable_agents_load_shared_search_contract_prompt() -> None:
    search_capable = [spec for spec in REGISTERED_AGENT_SPECS if "search_web" in spec.tools]

    assert {spec.route_name for spec in search_capable} == {
        "gmail_triage",
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
        "orchestrator",
        "chief_of_staff",
    }
    for spec in search_capable:
        assert "tools.md" in spec.prompt_files
        instructions = str(spec.build_agent().instructions)
        normalized = " ".join(instructions.split())
        assert "Shared Web Search Contract" in instructions
        assert "should not choose providers directly" in instructions
        assert "Serper remains disabled while credits are unavailable" in normalized
        assert "The quality bar is higher than generic LLM search" in instructions
        assert "produce an enriched but succinct synthesis from that source context" in instructions
        assert "provider diagnostics only in the final metadata section" in instructions


def test_registered_agent_static_prefix_fingerprints_are_stable(monkeypatch) -> None:
    """Guard the cache-sensitive prompt prefix: instructions, tools, and schema."""

    monkeypatch.delenv("KEYSTONE_ORCHESTRATOR_SPECIALIST_TOOLS", raising=False)
    assert set(STATIC_PREFIX_FINGERPRINTS) == {spec.route_name for spec in REGISTERED_AGENT_SPECS}

    for spec in REGISTERED_AGENT_SPECS:
        first_agent = spec.build_agent()
        second_agent = spec.build_agent()

        assert (
            _static_prefix_fingerprint(first_agent) == STATIC_PREFIX_FINGERPRINTS[spec.route_name]
        )
        assert _static_prefix_fingerprint(first_agent) == _static_prefix_fingerprint(second_agent)
        assert _ordered_tool_names(first_agent) == _ordered_tool_names(second_agent)
        assert "<!-- repo_runtime_policy.md -->" in str(first_agent.instructions)
        assert "<!-- AGENTS.md -->" not in str(first_agent.instructions)
        assert "<!-- safety_policy.md -->" in str(first_agent.instructions)


def test_orchestrator_registry_declares_read_only_specialist_tools() -> None:
    spec = AGENT_REGISTRY["orchestrator"]
    policy = tool_policy_for_agent("orchestrator")
    default_agent = build_orchestrator_agent()
    opted_in_agent = build_orchestrator_agent(include_handoffs=False, include_specialist_tools=True)
    default_tool_names = _tool_names(default_agent)
    opted_in_tool_names = _tool_names(opted_in_agent)

    for tool_name in (
        "business_research_analyst_research_brief",
        "opportunity_scout_read_only",
    ):
        assert tool_name in spec.optional_tools
        assert policy is not None and tool_name in policy.allowed_tool_names
        assert tool_name not in default_tool_names
        assert tool_name in opted_in_tool_names


def test_chief_of_staff_registry_declares_all_specialist_tools() -> None:
    spec = AGENT_REGISTRY["chief_of_staff"]
    policy = tool_policy_for_agent("chief_of_staff")
    agent = spec.build_agent(include_specialist_tools=True)
    generated_tool_names = {
        specialist_agent_tool_name(specialist.route_name) for specialist in SPECIALIST_AGENT_SPECS
    }
    tool_names = _tool_names(agent)

    assert generated_tool_names <= set(spec.optional_tools)
    assert generated_tool_names <= tool_names
    assert disallowed_tool_names("chief_of_staff", sorted(generated_tool_names)) == []
    assert policy is not None
    for tool_name in generated_tool_names:
        assert tool_tier_for_name(tool_name) == ToolTier.DEEP_RETRIEVAL


def test_orchestrator_registry_declares_control_plane_bridge_coverage() -> None:
    spec = AGENT_REGISTRY["orchestrator"]

    assert "raw Keystone requests first" in spec.handoff_description
    assert "compact preflight context" in spec.handoff_description
    assert "review specialist outputs" in spec.handoff_description
    assert "tests/test_orchestrator_preflight_context.py" in spec.validation_paths
    assert "tests/test_workflow_runner.py" in spec.validation_paths
    assert "tests/test_slack_action_contract.py" in spec.validation_paths
    assert "tests/test_slack_agent_actions.py" in spec.validation_paths
    assert "Manager-loop reviews feed final response synthesis" in spec.safety_notes


def test_runtime_tool_policy_rejects_disallowed_tools() -> None:
    with pytest.raises(AgentToolPolicyError, match="disallowed tool"):
        build_sdk_agent(
            name="outreach_composer",
            instructions="Keystone test agent.",
            output_type=None,
            tools=[get_gmail_message],
            policy_agent_name="outreach_composer",
        )


def test_runtime_tool_policy_rejects_missing_policy() -> None:
    with pytest.raises(AgentToolPolicyError, match="No AgentToolPolicy"):
        build_sdk_agent(
            name="unregistered_agent",
            instructions="Keystone test agent.",
            output_type=None,
            tools=[],
            policy_agent_name="unregistered_agent",
        )


def test_orchestrator_handoffs_derive_from_specialist_registry() -> None:
    assert INTENDED_HANDOFFS == specialist_handoff_specs()

    agent = build_orchestrator_agent()
    handoff_enabled_specs = [spec for spec in SPECIALIST_AGENT_SPECS if spec.handoff_enabled]
    assert len(agent.handoffs) == len(handoff_enabled_specs)
    assert [handoff.name for handoff in agent.handoffs] == [
        spec.route_name for spec in handoff_enabled_specs
    ]
    assert "airtable_context_agent" in [handoff.name for handoff in agent.handoffs]
    assert "google_workspace_context_agent" in [handoff.name for handoff in agent.handoffs]
    assert "zotero_context_agent" in [handoff.name for handoff in agent.handoffs]
    assert "rss_context_agent" in [handoff.name for handoff in agent.handoffs]
    assert "preprints_context_agent" in [handoff.name for handoff in agent.handoffs]


def test_orchestrator_handoff_specs_expose_typed_contracts() -> None:
    handoffs = {handoff.route: handoff for handoff in specialist_handoff_specs()}

    assert set(handoffs) == {spec.route_name for spec in SPECIALIST_AGENT_SPECS}
    research = handoffs["business_research_analyst"]
    assert research.contract_version == "keystone.handoff_spec.v1"
    assert research.input_schema == "keystone_agents.schemas.context_pack.ResearchContextPack"
    assert research.input_contract_type == "ResearchContextPack"
    assert research.output_schema == "keystone_agents.schemas.research.ResearchBrief"
    assert research.output_contract_type == "ResearchBrief"
    assert research.type_contract.source_output_type == (
        "keystone_agents.schemas.orchestrator.OrchestratorResult"
    )
    assert research.type_contract.target_input_type == research.input_schema
    assert research.type_contract.target_output_type == research.output_schema
    assert research.type_contract.payload_mode == "adapted"
    assert research.type_contract.compatibility_status == "compatible"

    assert handoffs["airtable_context_agent"].input_schema == (
        "keystone_agents.schemas.chief_of_staff.ChiefSpecialistToolInput"
    )
    assert handoffs["google_workspace_context_agent"].output_schema.endswith(
        ".GoogleWorkspaceContextResult"
    )
    assert handoffs["zotero_context_agent"].type_contract.target_output_type.endswith(
        ".ZoteroContextResult"
    )
    assert handoffs["rss_context_agent"].output_schema.endswith(".RssContextResult")
    assert handoffs["preprints_context_agent"].output_schema.endswith(
        ".PreprintsContextResult"
    )


def test_agent_cards_are_json_safe_extension_metadata() -> None:
    cards = agent_cards()

    assert len(cards) == len(REGISTERED_AGENT_SPECS)
    assert cards[0]["route_name"] == "gmail_triage"
    for card in cards:
        assert isinstance(card["prompt_files"], list)
        assert isinstance(card["skills"], list)
        assert isinstance(card["tools"], list)
        assert isinstance(card["optional_tools"], list)
        assert isinstance(card["safety_notes"], list)
        assert card["input_schema"]
        assert "skills.md" not in card["prompt_files"]
        assert tuple(card["skills"]) == AGENT_SKILL_NAMES[card["route_name"]]
        assert "evidence_attribution_and_claim_mapping" in card["skills"]
        assert "capabilities" not in card
        assert "builder" in card
        assert "output_schema" in card
        assert card["tool_policy"] is not None
        assert isinstance(card["tool_policy"]["allowed_tool_names"], list)
        assert isinstance(card["tool_policy"]["tool_tiers"], dict)
        assert "core_read" in card["tool_policy"]["tool_tiers"]
        assert "internal_write" in card["tool_policy"]["tool_tiers"]
        assert isinstance(card["runtime_tool_availability"], dict)


def test_agent_cards_expose_sanitized_file_search_runtime_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from keystone_agents import file_search

    for key in (
        file_search.GLOBAL_VECTOR_STORE_IDS_ENV,
        file_search.GLOBAL_MAX_RESULTS_ENV,
        file_search.GLOBAL_INCLUDE_RESULTS_ENV,
        *file_search.AGENT_VECTOR_STORE_IDS_ENVS.values(),
        *file_search.AGENT_MAX_RESULTS_ENVS.values(),
        *file_search.AGENT_INCLUDE_RESULTS_ENVS.values(),
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(
        "KEYSTONE_CHIEF_OF_STAFF_FILE_SEARCH_VECTOR_STORE_IDS",
        "vs_private_ops",
    )

    cards_by_route = {card["route_name"]: card for card in agent_cards()}
    chief_status = cards_by_route["chief_of_staff"]["runtime_tool_availability"][
        "file_search"
    ]
    gmail_status = cards_by_route["gmail_triage"]["runtime_tool_availability"]

    assert chief_status["configured"] is True
    assert chief_status["vector_store_id_count"] == 1
    assert chief_status["vector_store_source"] == "agent"
    assert "vs_private_ops" not in repr(chief_status)
    assert "file_search" not in gmail_status
    assert gmail_status["search_web"]["status"] == "attached_live_gated"
    assert gmail_status["mcp"]["status"] in {
        "sdk_available_not_configured",
        "sdk_unavailable",
    }
    assert gmail_status["tool_search"]["status"] in {
        "sdk_available_not_configured",
        "sdk_unavailable",
    }


def test_chief_of_staff_agent_card_exposes_local_kni_document_status() -> None:
    cards_by_route = {card["route_name"]: card for card in agent_cards()}
    status = cards_by_route["chief_of_staff"]["runtime_tool_availability"]

    assert "local_kni_documents" in status
    assert status["local_kni_documents"]["local_only"] is True
    assert status["local_kni_documents"]["send_enabled"] is False
