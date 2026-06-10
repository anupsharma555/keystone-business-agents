from __future__ import annotations

import json
from pathlib import Path

from keystone_agents.env_contract import (
    KEYSTONE_ENV_CONTRACT_SCHEMA,
    KEYSTONE_ENV_CONTRACT_VERSION,
    allowed_env_var_names,
    keystone_env_contract,
)


def test_env_contract_lists_child_runtime_keys_with_safety_metadata() -> None:
    contract = keystone_env_contract()
    env_vars = contract["env_vars"]
    by_name = {item["name"]: item for item in env_vars}

    assert contract["schema"] == KEYSTONE_ENV_CONTRACT_SCHEMA
    assert contract["version"] == KEYSTONE_ENV_CONTRACT_VERSION
    assert len(by_name) == len(env_vars)
    assert by_name["KEYSTONE_OPENAI_API_KEY"]["secret"] is True
    assert by_name["KEYSTONE_OPENAI_API_KEY"]["display_safety"] == "secret"
    assert by_name["SEARXNG_BASE_URL"]["category"] == "search"
    assert by_name["KEYSTONE_ORCHESTRATOR_MODEL"]["category"] == "model"
    assert "read environment values" in contract["notes"][0]


def test_env_contract_exposes_slack_child_env_scrub_keys_and_defaults() -> None:
    contract = keystone_env_contract()
    slack_env = contract["slack_child_env"]

    assert set(slack_env["scrub_parent_env_vars"]) == allowed_env_var_names()
    assert slack_env["default_overlays"] == {
        "SEARXNG_BASE_URL": "http://127.0.0.1:18080",
        "KEYSTONE_SEARXNG_TRANSIENT": "true",
        "KEYSTONE_TAVILY_SEARCH_FALLBACK": "false",
        "KEYSTONE_TAVILY_SEARCH_MAX_CALLS_PER_RUN": "2",
        "KEYSTONE_EXA_SEARCH_FALLBACK": "true",
        "KEYSTONE_EXA_SEARCH_MAX_CALLS_PER_RUN": "2",
    }


def test_generated_env_contract_artifact_matches_canonical_contract() -> None:
    artifact = Path("contracts/keystone_business_agent_env_contract.v1.json")
    payload = json.loads(artifact.read_text(encoding="utf-8"))

    assert payload == keystone_env_contract()
