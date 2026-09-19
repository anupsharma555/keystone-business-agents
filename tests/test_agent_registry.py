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
from keystone_agents.capabilities.catalog import instruction_profile_text
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
        "instructions_sha256": "cacc99d31ba51016195aa4ef7b5bc7c42274ffbdaa0520acad4ba47b0cf25792",
        "tool_names_sha256": "7363ff1e9c76e8f477af66a812c9a8116691604c51ed68d1f70b93f6112b93b4",
        "output_schema_sha256": "42ef2b014989eb11bb8b3dcae8b8b3fd937e33272ae562e92eb9023c5dc6cde7"
    },
    "business_research_analyst": {
        "instructions_sha256": "a140965a81e647d50d514e67822917bff6c89a6c1c487f29e94078326fde9f2e",
        "tool_names_sha256": "2f40cad113c0a5e031f969d39dbfde851cd27fa13abd6c8ac5b7d7cd612bdabe",
        "output_schema_sha256": "feaea1322b89b637193b4514a4774c031eb4a3d0ecaef162abe13116185c3fc8"
    },
    "rag_retrieval_specialist": {
        "instructions_sha256": "1393cbbe8819cba28839355899da4271556d780ab3c47218c873c40c144970d9",
        "tool_names_sha256": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
        "output_schema_sha256": "0924032eaa83589b6e8a92c5d3c49fdc927995598184e4c6c6d43a673f8193d6"
    },
    "opportunity_scout": {
        "instructions_sha256": "bb34ee463ec9ae0b81a7c70e9f82f33d20dcbc964352dd9afc85915089b01464",
        "tool_names_sha256": "11af4029345ccdcb3ec2287e1a91e53391d4ad9e44324574984c31e86d8ce399",
        "output_schema_sha256": "6619d4b688ba020b2492f169ca2a85fda8a4606a507dc223440a5a109bcd5668"
    },
    "outreach_composer": {
        "instructions_sha256": "304e3718deab2a2947f3571a21da6599d8fa55d5e7c51dd31c379884422d2319",
        "tool_names_sha256": "5a7b62f096ae42d9499642af1f89b47f850f0ac89ee6feb500bcde203705f6b4",
        "output_schema_sha256": "43ecdc7e630e3b1a5f60de46a961a424eba0f8c4a791cd80b89d566ffe01bf2c"
    },
    "airtable_context_agent": {
        "instructions_sha256": "ce993ccfb26bb6898c1774f1e49f304d8edcdbc1e67c85004c95a757144fcdb1",
        "tool_names_sha256": "def24ef9628ea20812b088d5dec602be8044e2d1f8777bb8f581b582f9f8a5a6",
        "output_schema_sha256": "0cc10736f7d15eda9511e39458c9a8f91ae093f2a3d876c1809569a86d659c62"
    },
    "google_workspace_context_agent": {
        "instructions_sha256": "3a4686d69c6608fb21b9fcc6b9610e2b48ef4b9226059fd55f00dcd45f16ab84",
        "tool_names_sha256": "31f25b0b1bf5b672634cad9cb1434c0c2b4c5f722ba41a0a783e809e1cbecab3",
        "output_schema_sha256": "ab79b01b12622292bb3f6bdee32d6ea50e0bdce0ef73617de1f982d1ac0ee9f2"
    },
    "zotero_context_agent": {
        "instructions_sha256": "6134743f50581f8b2b49ab386f321e55840eb261b6c02ea0e0b9043d5a4d3963",
        "tool_names_sha256": "157efe138d475b9b0eb13961c0e598b47f58037480732babd614b98019ecc526",
        "output_schema_sha256": "3c77001f4b3b9255d7211ecf93cac61c46319283c777030ca276562be497ad6f"
    },
    "rss_context_agent": {
        "instructions_sha256": "46a70062181d7a35a3b0a9587106d898bc9f55e4c40984841e04623a5ea6ec89",
        "tool_names_sha256": "bc30cef46242b8d0a15644b1a4101f798d7b2a954318d627d988c4c37ea44ee2",
        "output_schema_sha256": "f29520fe32adbc184aeac2351584d860891cd307feed3848ae151a93207c4498"
    },
    "preprints_context_agent": {
        "instructions_sha256": "b3489cb9ffc47d3e997165f57e860cab09387edec7c74bb604a7e08242063995",
        "tool_names_sha256": "e7f0e140b410b6c05354ae3b6e33ce424dfce60d09e205c4d9f7d80eb0b56e19",
        "output_schema_sha256": "7491586988c2e8b03a5811ae94699267786483b949548047b5be898787469e0a"
    },
    "orchestrator": {
        "instructions_sha256": "86d67df008a57af5cdc795cc589612dfc6dc6df081ee54f6b99e5aeae9392462",
        "tool_names_sha256": "d57d63506543d4ec46131f24a3f3e64a98fc50bf996a3c59629c5de66ee1f6c2",
        "output_schema_sha256": "b661ecc6298a304709693b32f2cd7ee29bfadd54fbebe589c4bdd708c0993fa0"
    },
    "chief_of_staff": {
        "instructions_sha256": "e33c8a04ced2ff52fcf4c58132f66be97954d111eaa8909b65ee1921720acc04",
        "tool_names_sha256": "8048af129fda950491143e09f38c94257de40e91ae96d9feedaed22f79334860",
        "output_schema_sha256": "2de4f02033461c076c09eb6b4b77a2ab623e6404dbacdfc6a4c7ffe9274590a4"
    }
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
        "instructions_sha256": _sha256(instruction_profile_text(agent)),
        "tool_names_sha256": _sha256(_ordered_tool_names(agent)),
        "output_schema_sha256": _sha256(schema),
    }


def test_registry_has_canonical_agents() -> None:
    assert set(AGENT_REGISTRY) == {
        "gmail_triage",
        "business_research_analyst",
        "rag_retrieval_specialist",
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
        "rag_retrieval_specialist",
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


def test_provider_agent_cards_declare_extraction_and_workspace_live_gates() -> None:
    research = AGENT_REGISTRY["business_research_analyst"]
    workspace = AGENT_REGISTRY["google_workspace_context_agent"]

    assert "live_extraction=true" in research.live_flags_required
    assert "KEYSTONE_ENABLE_WEBSITE_EXTRACTION=true" in research.live_flags_required
    assert any("public HTTP(S) only" in note for note in research.safety_notes)
    assert {
        "KEYSTONE_GOOGLE_WORKSPACE_LIVE_READS=true",
        "GOOGLE_WORKSPACE_WRITES_ENABLED=true",
        "approval_reference",
    } <= set(workspace.live_flags_required)


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
    assert "latest user question" in str(by_layer["local_kni_documents"]["reasoning_contract"])
    assert "organizer" in str(by_layer["local_kni_documents"]["reasoning_contract"])
    assert "registered agent" in str(by_layer["local_kni_documents"]["reasoning_contract"])


def test_registered_builders_match_declared_schema_and_tools_bidirectionally() -> None:
    for spec in REGISTERED_AGENT_SPECS:
        agent = spec.build_agent()
        attached_tools = _tool_names(agent)
        declared_tools = set(spec.tools)
        optional_tools = set(spec.optional_tools)
        assert isinstance(agent, Agent)
        assert agent.output_type is spec.resolve_output_schema()
        assert attached_tools - optional_tools == declared_tools, spec.route_name
        assert attached_tools <= declared_tools | optional_tools, spec.route_name
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

    assert {
        "airtable_get_base_schema",
        "airtable_read_records",
        "airtable_aggregate_records",
    } <= airtable_tool_names
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
        "google_doc_test_lifecycle",
        "google_drive_list_folder",
        "google_drive_search_files",
        "google_drive_get_file_metadata",
        "google_drive_media_ocr_read",
        "google_slide_deck_read",
        "google_slide_deck_write",
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
        "zotero_list_cached_items",
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
    assert (
        disallowed_tool_names(
            "google_workspace_context_agent",
            sorted(workspace_tool_names),
        )
        == []
    )
    assert disallowed_tool_names("zotero_context_agent", sorted(zotero_tool_names)) == []
    signal_lifecycle_tools = {
        "inspect_signal_lifecycle",
        "prepare_signal_lifecycle_checkpoint",
        "advance_signal_lifecycle_checkpoint",
    }
    assert rss_tool_names == {
        "retrieve_rss_announcement_history",
        "read_rss_announcement_evidence",
        *signal_lifecycle_tools,
    }
    assert preprints_tool_names == {
        "retrieve_preprint_announcement_history",
        "read_preprint_announcement_evidence",
        *signal_lifecycle_tools,
    }
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
    assert tool_tier_for_name("google_doc_test_lifecycle") == ToolTier.INTERNAL_WRITE
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
    assert tool_tier_for_name("read_rss_announcement_evidence") == ToolTier.CORE_READ
    assert tool_tier_for_name("retrieve_preprint_announcement_history") == ToolTier.CORE_READ
    assert tool_tier_for_name("read_preprint_announcement_evidence") == ToolTier.CORE_READ
    assert tool_tier_for_name("inspect_signal_lifecycle") == ToolTier.CORE_READ
    assert tool_tier_for_name("prepare_signal_lifecycle_checkpoint") == ToolTier.INTERNAL_WRITE
    assert tool_tier_for_name("advance_signal_lifecycle_checkpoint") == ToolTier.INTERNAL_WRITE

    nested_airtable = AGENT_REGISTRY["airtable_context_agent"].build_agent(
        tool_tier=ToolTier.DIAGNOSTIC
    )
    nested_workspace = AGENT_REGISTRY["google_workspace_context_agent"].build_agent(
        tool_tier=ToolTier.DIAGNOSTIC
    )
    nested_zotero = AGENT_REGISTRY["zotero_context_agent"].build_agent(
        tool_tier=ToolTier.DIAGNOSTIC
    )
    nested_rss = AGENT_REGISTRY["rss_context_agent"].build_agent(tool_tier=ToolTier.DIAGNOSTIC)
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


def test_build_model_settings_preserves_bounded_initial_tool_choice(monkeypatch) -> None:
    monkeypatch.delenv("KEYSTONE_SDK_INCLUDE_USAGE", raising=False)
    monkeypatch.delenv("KEYSTONE_SDK_PROMPT_CACHE_RETENTION", raising=False)

    settings = build_model_settings(tool_choice="query_gmail_message_summaries")

    assert settings.tool_choice == "query_gmail_message_summaries"


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
    research_deep = build_business_research_analyst_research_brief_agent(
        tool_tier="deep_retrieval",
        manual_request_plan={
            "source": "canonical:test",
            "target_agent": "business_research_analyst",
            "intent": "research_brief",
            "requires_live_search": True,
            "ask_shape": {
                "evidence_depth": "deep",
                "permission_state": "read_only",
            },
        },
    )
    scout_web = build_opportunity_scout_agent(
        tool_tier="web_search",
        manual_request_plan={
            "source": "canonical:test",
            "target_agent": "opportunity_scout",
            "intent": "opportunity_search",
            "requires_live_search": True,
            "ask_shape": {
                "evidence_depth": "standard",
                "permission_state": "read_only",
            },
        },
    )
    scout_diagnostic = build_opportunity_scout_agent(
        tool_tier="diagnostic",
        manual_request_plan={
            "source": "canonical",
            "target_agent": "opportunity_scout",
            "intent": "browser_diagnostics",
            "ask_shape": {
                "evidence_depth": "deep",
                "permission_state": "read_only",
            },
        },
    )

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
        ), spec.route_name
        assert _static_prefix_fingerprint(first_agent) == _static_prefix_fingerprint(second_agent)
        assert _ordered_tool_names(first_agent) == _ordered_tool_names(second_agent)
        assert "<!-- repo_runtime_policy.md -->" in str(first_agent.instructions)
        assert "<!-- AGENTS.md -->" not in str(first_agent.instructions)
        assert "<!-- safety_policy.md -->" in str(first_agent.instructions)


@pytest.mark.parametrize(
    ("route", "source_prompt"),
    [
        ("airtable_context_agent", "airtable_context.md"),
        ("google_workspace_context_agent", "google_workspace_context.md"),
        ("zotero_context_agent", "zotero_context.md"),
        ("rss_context_agent", "rss_context.md"),
        ("preprints_context_agent", "preprints_context.md"),
    ],
)
def test_context_agents_have_compact_direct_instruction_profiles(
    route: str,
    source_prompt: str,
) -> None:
    full = AGENT_REGISTRY[route].build_agent(request_text="read one exact source")
    compact = AGENT_REGISTRY[route].build_agent(
        request_text="read one exact source",
        tool_tier="core_read",
        compact_instructions=True,
    )
    full_text = str(full.instructions)
    compact_text = str(compact.instructions)

    assert len(compact_text) < len(full_text) * 0.55
    assert "<!-- memory_policy.md -->" in compact_text
    assert "<!-- writing_style.md -->" in compact_text
    assert "<!-- safety_policy.md -->" in compact_text
    assert f"<!-- {source_prompt} -->" in compact_text
    assert "<!-- tools.md -->" not in compact_text
    assert "<!-- slack-posting-rules.md -->" not in compact_text


@pytest.mark.parametrize(
    "route",
    ["rss_context_agent", "preprints_context_agent"],
)
def test_signal_context_instructions_allow_only_bounded_empty_result_reformulation(
    route: str,
) -> None:
    instructions = str(
        AGENT_REGISTRY[route].build_agent(
            request_text="Review bounded saved history.",
            tool_tier="core_read",
            compact_instructions=True,
        ).instructions
    )

    assert "one changed" in instructions
    assert "same approved history source" in instructions or (
        "same already-authorized source" in instructions
    )
    assert "original operator request" in instructions
    assert "abbreviation expansion" in instructions
    assert "Do not use an empty query" in instructions
    assert "yourself exactly once" not in instructions


def test_direct_zotero_read_catalog_is_request_scoped() -> None:
    article = AGENT_REGISTRY["zotero_context_agent"].build_agent(
        request_text="For the same Zotero article, read its notes and attached PDF.",
        tool_tier="core_read",
        compact_instructions=True,
    )

    assert _tool_names(article) == {
        "zotero_read_api_metadata",
        "zotero_resolve_article_context",
        "zotero_read_item_children",
        "zotero_read_pdf_attachment_text",
    }


def test_direct_google_workspace_read_catalog_is_request_scoped() -> None:
    doc = AGENT_REGISTRY["google_workspace_context_agent"].build_agent(
        request_text="Read one exact Google Doc and summarize it.",
        tool_tier="core_read",
        compact_instructions=True,
    )
    sheet = AGENT_REGISTRY["google_workspace_context_agent"].build_agent(
        request_text="Read one exact Google Sheet table and summarize its rows.",
        tool_tier="core_read",
        compact_instructions=True,
    )

    assert _tool_names(doc) == {
        "google_drive_search_files",
        "google_drive_get_file_metadata",
        "google_doc_read",
    }
    assert _tool_names(sheet) == {
        "google_drive_search_files",
        "google_sheet_list",
        "google_sheet_read_table",
    }


def test_direct_google_workspace_write_catalog_is_target_and_operation_scoped() -> None:
    doc = AGENT_REGISTRY["google_workspace_context_agent"].build_agent(
        request_text="Update one exact Google Doc after reading its current content.",
        tool_tier="internal_write",
        compact_instructions=True,
    )
    sheet = AGENT_REGISTRY["google_workspace_context_agent"].build_agent(
        request_text="Append one approved row to an exact Google Sheet.",
        tool_tier="internal_write",
        compact_instructions=True,
    )

    assert _tool_names(doc) == {
        "google_drive_search_files",
        "google_drive_get_file_metadata",
        "google_doc_read",
        "google_doc_write",
    }
    assert _tool_names(sheet) == {
        "google_drive_search_files",
        "google_sheet_list",
        "google_sheet_read_table",
        "google_sheet_append_rows",
    }


def test_direct_zotero_write_catalog_preserves_native_mutation_boundary() -> None:
    ordinary_note = AGENT_REGISTRY["zotero_context_agent"].build_agent(
        request_text="Add a note to this exact Zotero article.",
        tool_tier="internal_write",
        compact_instructions=True,
    )
    test_lifecycle = AGENT_REGISTRY["zotero_context_agent"].build_agent(
        request_text=("Create, revise, and remove one marked KBA_TEST_NOTE Zotero note lifecycle."),
        tool_tier="internal_write",
        compact_instructions=True,
    )

    assert _tool_names(ordinary_note) == {
        "zotero_read_api_metadata",
        "zotero_resolve_article_context",
        "zotero_read_item_children",
    }
    assert "zotero_write_test_note" not in _tool_names(ordinary_note)
    assert _tool_names(test_lifecycle) == {"zotero_test_note_lifecycle"}


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
        specialist_agent_tool_name(specialist.route_name)
        for specialist in SPECIALIST_AGENT_SPECS
        if specialist.route_name != "rag_retrieval_specialist"
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
    assert handoffs["preprints_context_agent"].output_schema.endswith(".PreprintsContextResult")


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
    chief_status = cards_by_route["chief_of_staff"]["runtime_tool_availability"]["file_search"]
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


def test_agent_cards_declare_the_runtime_catalog_prompt():
    for card in agent_cards():
        assert card["shared_runtime_prompt_files"] == ["runtime_capability_catalog.md"]
        metadata = prompt_metadata_for_files(card["shared_runtime_prompt_files"])
        assert metadata[0]["name"] == "runtime_capability_catalog"
