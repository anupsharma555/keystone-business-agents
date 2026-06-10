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
    tool_policy_for_agent,
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
from keystone_agents.tools.gmail_tool import get_gmail_message

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_ROOT = PROJECT_ROOT / "src" / "keystone_agents" / "prompts"
SKILLS_ROOT = PROJECT_ROOT / "src" / "keystone_agents" / "skills"

STATIC_PREFIX_FINGERPRINTS = {
    "gmail_triage": {
        "instructions_sha256": "2d51d6da5d6927bbb6a033f9b4524706aae9cd8bdad9899b814c1f16ca4311d9",
        "tool_names_sha256": "7f8aa859104bbb74f20effcc9ea3df828444a7a5e6ee333faaa4308f214a7b1a",
        "output_schema_sha256": "563358d4e138654b23fb9567b06c639a66127dcfd6a3182a497b20a32dc166de",
    },
    "business_research_analyst": {
        "instructions_sha256": "d188c6135295be2acbb85ce872aea619ee188660dffb8b3df5e49c43f1c384f7",
        "tool_names_sha256": "dc99c3bef9ef99b28c0fefeb017783195c895dc2c819670275e860e8b2fc16fa",
        "output_schema_sha256": "b8218a333d85d2f3850203f5ee48b7ec535a6f924a8851c513f1f2b2afeef6e0",
    },
    "opportunity_scout": {
        "instructions_sha256": "856054f0f8ea6e3d592e7861cd84f8db42c413109a4b298bd9c550e8edaf6a95",
        "tool_names_sha256": "cfd3e659bff1d1d5a9d2c9d3ed823f9261df6f5e5197fd50d51d96c069b34e66",
        "output_schema_sha256": "2e91674be427e59361cfb5a9c275a048bf4166fbcd9f88f6e06405a235ab59e8",
    },
    "outreach_composer": {
        "instructions_sha256": "c3c802afb1bd3eacf069e36299b767467bc4aefc9cbf8f49ca90ccf04331f497",
        "tool_names_sha256": "ef26ea7d5fdaaa5e435c8e4d6dee0521dd07a8ddf663dc12bcec3116a9592585",
        "output_schema_sha256": "167da45f0bb07c0a255c4115b52e9510272a22cc1c479abe9e97c88693d44b34",
    },
    "orchestrator": {
        "instructions_sha256": "3a425283ff544b95eca56539ee6d4192f26f5c03d7e00f835dd89e9df98cc042",
        "tool_names_sha256": "1c2bf4210e21e89f1381c3fc9e33b1480130220b362593845c47475e93b2f925",
        "output_schema_sha256": "89d3c6cd618bcd2546fd63fdbd9de221cf4db20af407030347d7f64c0a646e8b",
    },
    "chief_of_staff": {
        "instructions_sha256": "5f6004b0df94b3e342367e3de636dc6a2af0a5881dc7647b94410be9927de6ed",
        "tool_names_sha256": "7eb39fa7ca936b8307f679537f03f155e6a41e71280a9d22b0c9ea46ad881d63",
        "output_schema_sha256": "132332f59ebca8c234b91d6f51380f7e18effd7dc623c77d104774f1003cbe3f",
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
        "orchestrator",
        "chief_of_staff",
    }
    assert [spec.route_name for spec in REGISTERED_AGENT_SPECS] == [
        "gmail_triage",
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
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


def test_registered_builders_match_declared_schema_and_tools() -> None:
    for spec in REGISTERED_AGENT_SPECS:
        agent = spec.build_agent()
        assert isinstance(agent, Agent)
        assert agent.output_type is spec.resolve_output_schema()
        assert set(spec.tools) <= _tool_names(agent)
        assert agent.handoff_description
        assert agent.input_guardrails
        assert agent.output_guardrails


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
    assert len(agent.handoffs) == len(SPECIALIST_AGENT_SPECS)
    assert [handoff.name for handoff in agent.handoffs] == [
        spec.route_name for spec in SPECIALIST_AGENT_SPECS
    ]


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
