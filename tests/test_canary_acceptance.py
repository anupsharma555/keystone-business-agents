"""Offline scope, dispatch, budget and provider-boundary proofs for the opt-in canary."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from contextvars import Context
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from agents import Agent, function_tool
from openai import AsyncOpenAI
from openai.types.responses import ResponseReasoningItem
from pydantic import BaseModel, ValidationError

from keystone_agents import sdk
from keystone_agents.agents.preprints_context import build_preprints_context_agent
from keystone_agents.canary_acceptance import (
    CLAIM_ENV,
    DIGEST_ENV,
    GMAIL_READ_ONLY_ENV,
    MODEL,
    PROFILE_ENV,
    TURN_REQUEST_LIMIT_ENV,
    AcceptanceProfile,
    CanaryPolicyError,
    ScopeLedger,
    _hash,
    admit_agent,
    child_environment,
    gmail_read_guard,
    guarded_response_kwargs,
    observe_canary_response_reasoning,
    public_preprint_snapshot_guard,
)
from keystone_agents.canary_acceptance_entrypoint import prepare_root_arguments, run
from keystone_agents.capabilities.tool_scope import tool_scope_receipt_for_agent
from keystone_agents.direct_response import build_direct_supplied_response_agent
from keystone_agents.instruction_following import (
    InstructionFollowingRepairInput,
    InstructionFollowingRepairOutput,
    build_instruction_following_repair_agent,
)
from keystone_agents.model_provider import ModelConfig
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.runtime.request_budget import activate_model_request_budget
from keystone_agents.runtime.signal_context import run_signal_context_sdk
from keystone_agents.schemas.announcement_feed import AnnouncementFeedItem
from keystone_agents.schemas.execution_request import (
    DirectAgentResponse,
    DirectAgentResponseInput,
)
from keystone_agents.schemas.manual_request_plan import AskShapePolicy, ManualRequestPlan
from keystone_agents.schemas.operational_context import PreprintsContextResult
from keystone_agents.slack_actions import _verified_signal_context_from_agent_run
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools.announcement_context_tools import (
    retrieve_preprint_announcement_history_impl,
)
from keystone_agents.tools.gmail_tool import GmailTool


def setup_profile(tmp_path, monkeypatch, **changes):
    state = tmp_path / "state"
    context_dir = state / "slack-context"
    context_dir.mkdir(parents=True)
    token = state / "gmail-copy.json"
    token.write_text('{"refresh_token":"synthetic-copy"}')
    credentials = tmp_path / "client.json"
    credentials.write_text("{}")
    keys = tmp_path / "keys.env"
    keys.write_text(
        "KEYSTONE_OPENAI_API_KEY=synthetic-project-key\nOPENAI_API_KEY=unrelated-key\nEXA_API_KEY=unrelated-provider\n"
    )
    context = {
        "schema": "keystone.slack.history_context.v1",
        "source": "slack_app_mention_history",
        "channel_id": "C_SYNTHETIC",
        "team_id": "T_SYNTHETIC",
        "request_ts": "123.000001",
        "thread_ts": "123.000001",
        "request_text": "Chief of staff review the selected newsletter; draft here only.",
        "thread_fetch_status": "ok",
        "thread_messages": [],
        "read_context": "Approved context.",
    }
    path = context_dir / "root.json"
    path.write_text(json.dumps(context))
    values = dict(
        enabled=True,
        repo_root=Path.cwd(),
        state_dir=state,
        request_sha256=_hash(context["request_text"]),
        context_sha256=_hash(json.dumps(context, ensure_ascii=True, sort_keys=True)),
        channel_id=context["channel_id"],
        team_id=context["team_id"],
        gmail_token_copy=token,
        gmail_credentials_file=credentials,
        key_env_file=keys,
        approval_reference="synthetic-review",
        observed_balance_usd="100",
        approved_reserve_usd="10",
        balance_observed_at=datetime.now(UTC),
    )
    values.update(changes)
    profile = AcceptanceProfile(**values)
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(profile.model_dump_json())
    monkeypatch.setenv(PROFILE_ENV, str(profile_path))
    monkeypatch.setenv(DIGEST_ENV, profile.digest)
    monkeypatch.setenv(GMAIL_READ_ONLY_ENV, "true")
    return profile, profile_path, path, context


def setup_public_preprint_profile(tmp_path, monkeypatch, **changes):
    state = tmp_path / "state"
    context_dir = state / "slack-context"
    source_dir = state / "public-sources"
    context_dir.mkdir(parents=True)
    source_dir.mkdir()
    source = source_dir / "preprints.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute(
            """
            CREATE TABLE discovery_candidates (
                candidate_id TEXT PRIMARY KEY,
                candidate_type TEXT NOT NULL,
                source TEXT NOT NULL,
                source_item_id TEXT NOT NULL,
                title TEXT,
                summary TEXT,
                published TEXT NOT NULL,
                url TEXT NOT NULL,
                topics_json TEXT,
                status TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                metadata_json TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO discovery_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    f"synthetic-preprint-{index}",
                    "preprint",
                    "Synthetic Archive",
                    f"synthetic-{index}",
                    title,
                    summary,
                    f"2026-09-{10 + index:02d}",
                    f"https://example.test/preprints/{index}",
                    json.dumps(topics),
                    "candidate",
                    f"2026-09-{10 + index:02d}T12:00:00Z",
                    json.dumps({"stable_id": f"synthetic-{index}"}),
                )
                for index, (title, summary, topics) in enumerate(
                    [
                        (
                            "Clinical AI evaluation methods",
                            "A synthetic evaluation methods paper.",
                            ["clinical AI", "evaluation"],
                        ),
                        (
                            "Workflow measurement study",
                            "A synthetic clinical implementation paper.",
                            ["measurement", "workflow"],
                        ),
                        (
                            "Behavioral data quality review",
                            "A synthetic data-quality paper.",
                            ["behavioral health", "data quality"],
                        ),
                    ],
                    start=1,
                )
            ],
        )
    keys = tmp_path / "keys.env"
    keys.write_text("KEYSTONE_OPENAI_API_KEY=synthetic-project-key\n")
    followup = (
        "From the selected preprint, identify the strongest realistic opportunity, "
        "if any. Use only that context; do not search or call providers."
    )
    context = {
        "schema": "keystone.slack.history_context.v1",
        "source": "slack_app_mention_history",
        "channel_id": "C_PUBLIC_SYNTHETIC",
        "team_id": "T_PUBLIC_SYNTHETIC",
        "user_id": "U_PUBLIC_SYNTHETIC",
        "request_ts": "223.000001",
        "thread_ts": "223.000001",
        "request_text": (
            "Review the saved public preprints on clinical AI and select the most "
            "relevant one for evidence monitoring."
        ),
        "thread_fetch_status": "ok",
        "thread_messages": [],
        "read_context": "Synthetic public-preprint acceptance context.",
    }
    context_path = context_dir / "root.json"
    context_path.write_text(json.dumps(context))
    values = dict(
        scenario="local_public_preprints",
        enabled=True,
        repo_root=Path.cwd(),
        state_dir=state,
        request_sha256=_hash(context["request_text"]),
        context_sha256=_hash(json.dumps(context, ensure_ascii=True, sort_keys=True)),
        followup_request_sha256=_hash(followup),
        channel_id=context["channel_id"],
        team_id=context["team_id"],
        author_id=context["user_id"],
        key_env_file=keys,
        public_preprint_snapshot=source,
        public_preprint_snapshot_sha256=_hash(source.read_bytes()),
        approval_reference="synthetic-public-review",
        max_requests=7,
        root_max_requests=5,
        followup_max_requests=2,
        model_profile="mini",
        max_structured_output_retries=0,
        max_input_bytes=130_000,
        max_body_bytes=140_000,
        max_output_tokens=3_000,
        observed_balance_usd="100",
        approved_reserve_usd="10",
        balance_observed_at=datetime.now(UTC),
    )
    values.update(changes)
    profile = AcceptanceProfile(**values)
    profile_path = tmp_path / "public-profile.json"
    profile_path.write_text(profile.model_dump_json())
    monkeypatch.setenv(PROFILE_ENV, str(profile_path))
    monkeypatch.setenv(DIGEST_ENV, profile.digest)
    return profile, profile_path, context_path, context, followup, source


def _minimal_public_root(context: dict[str, object], request_ts: str) -> dict[str, object]:
    return {
        "schema": "keystone.slack.history_context.v1",
        "source": "slack_app_mention_history",
        "team_id": context["team_id"],
        "user_id": context["user_id"],
        "channel_id": context["channel_id"],
        "thread_ts": request_ts,
        "request_ts": request_ts,
        "request_text": context["request_text"],
        "read_context": "",
        "thread_root_request": "",
        "thread_messages": [],
        "thread_fetch_status": "not_requested",
        "warnings": [],
    }


def activate(profile, context, monkeypatch):
    claim = ScopeLedger(profile).claim(context)
    monkeypatch.setenv(CLAIM_ENV, claim)
    return claim


def kwargs():
    return {"model": MODEL, "input": "Synthetic text.", "instructions": "Be concise."}


def client():
    return SimpleNamespace(max_retries=0, base_url="https://api.openai.com/v1/")


def dispatch_count(profile):
    with sqlite3.connect(profile.state_dir / "acceptance-scope.sqlite3") as connection:
        return connection.execute("SELECT requests FROM scope WHERE slot=1").fetchone()[0]


def dispatch_records(profile):
    with sqlite3.connect(profile.state_dir / "acceptance-scope.sqlite3") as connection:
        return [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT metadata FROM dispatches ORDER BY ordinal"
            )
    ]


def _function_schema(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "name": name,
        "description": "Synthetic bounded function.",
        "parameters": {"type": "object", "properties": {}},
    }


def test_public_preprint_profile_manifest_environment_and_source_guard(
    tmp_path,
    monkeypatch,
):
    profile, path, _, context, _, source = setup_public_preprint_profile(
        tmp_path, monkeypatch
    )
    profile.check_paths()
    claim = activate(profile, context, monkeypatch)
    env = child_environment(profile, claim, path, turn_request_limit=5)
    manifest = profile.manifest()

    assert manifest["scenario"] == "local_public_preprints"
    assert manifest["root_context_binding"] == "exact_digest"
    assert manifest["per_turn_request_limits"] == {"root": 5, "followup": 2}
    assert manifest["max_orchestrator_requests"] == 5
    assert set(manifest["agent_models"]) == {
        "orchestrator",
        "chief_of_staff",
        "preprints_context_agent",
        "opportunity_scout",
        "instruction_following_repair",
    }
    assert manifest["allowed_function_tools"]["preprints_context_agent"] == [
        "retrieve_preprint_announcement_history"
    ]
    assert all(
        tools == []
        for agent, tools in manifest["allowed_function_tools"].items()
        if agent != "preprints_context_agent"
    )
    assert manifest["gmail_operations"] == []
    assert env["DISCOVERY_STORE_PATH"] == str(source)
    assert env["KEYSTONE_ENABLE_LIVE_RESEARCH"] == "false"
    assert env["KEYSTONE_RSS_CONTEXT_LIVE_SLACK_READ_ENABLED"] == "false"
    assert env[TURN_REQUEST_LIMIT_ENV] == "5"
    for key in (
        GMAIL_READ_ONLY_ENV,
        "GOOGLE_TOKEN_FILE",
        "GOOGLE_CREDENTIALS_FILE",
        "GOOGLE_WORKSPACE_OAUTH_TOKEN_PATH",
        "SEARCH_PROVIDER",
        "SEARXNG_BASE_URL",
        "KEYSTONE_ENABLE_WEBSITE_EXTRACTION",
        "KEYSTONE_WEBSITE_EXTRACTOR",
    ):
        assert key not in env
    for agent in ("GMAIL_TRIAGE", "RSS_CONTEXT_AGENT", "WEB_QUERY_PLANNER"):
        assert f"KEYSTONE_{agent}_MODEL" not in env
    assert public_preprint_snapshot_guard(source) == profile.public_preprint_snapshot_sha256


def test_public_scenario_fields_do_not_change_legacy_profile_digest_or_manifest(
    tmp_path,
    monkeypatch,
):
    profile, _, _, _ = setup_profile(tmp_path, monkeypatch)
    dumped = profile.model_dump(mode="json")
    assert "scenario" not in dumped
    assert "public_preprint_snapshot" not in dumped
    assert "public_preprint_snapshot_sha256" not in dumped
    assert profile.digest == _hash(
        profile.model_dump_json(
            exclude={
                "scenario",
                "public_preprint_snapshot",
                "public_preprint_snapshot_sha256",
                "followup_request_sha256",
                "root_max_requests",
                "followup_max_requests",
            }
        )
    )
    assert "scenario" not in profile.manifest()
    assert profile.manifest()["gmail_operations"] == ["GET"]
    restored = AcceptanceProfile.model_validate_json(profile.model_dump_json())
    assert restored.digest == profile.digest
    assert restored.manifest() == profile.manifest()


def test_public_preprint_agent_and_tool_admission_is_scenario_scoped(
    tmp_path,
    monkeypatch,
):
    profile, _, _, context, _, _ = setup_public_preprint_profile(tmp_path, monkeypatch)
    activate(profile, context, monkeypatch)

    admit_agent("orchestrator")
    guarded_response_kwargs(client(), (), kwargs())
    admit_agent("preprints_context_agent")
    guarded_response_kwargs(
        client(),
        (),
        {
            **kwargs(),
            "tools": [_function_schema("retrieve_preprint_announcement_history")],
        },
    )
    admit_agent("preprints_context_agent")
    with pytest.raises(CanaryPolicyError, match="function_tool_not_allowed"):
        guarded_response_kwargs(
            client(),
            (),
            {**kwargs(), "tools": [_function_schema("inspect_signal_lifecycle")]},
        )
    admit_agent("opportunity_scout")
    with pytest.raises(CanaryPolicyError, match="function_tool_not_allowed"):
        guarded_response_kwargs(
            client(),
            (),
            {**kwargs(), "tools": [_function_schema("search_web")]},
        )
    with pytest.raises(CanaryPolicyError, match="agent_not_allowed"):
        admit_agent("gmail_triage")
    with pytest.raises(CanaryPolicyError, match="gmail_mutation_denied"):
        gmail_read_guard("GET", tmp_path / "gmail.json", "https://gmail.googleapis.com")
    assert dispatch_count(profile) == 2


def test_public_preprint_read_is_profile_bound_read_only_and_audited(
    tmp_path,
    monkeypatch,
):
    profile, path, _, context, _, source = setup_public_preprint_profile(
        tmp_path, monkeypatch
    )
    claim = activate(profile, context, monkeypatch)
    env = child_environment(profile, claim, path, turn_request_limit=5)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    source_before = source.read_bytes()

    result = retrieve_preprint_announcement_history_impl(
        "clinical AI",
        database_url=f"sqlite:///{tmp_path / 'canonical.sqlite'}",
    )

    assert result["status"] == "success" and result["item_count"] == 2
    assert result["items"][0]["feed_item_id"] == "synthetic-preprint-1"
    assert [item["feed_item_id"] for item in result["items"]] == [
        "synthetic-preprint-1",
        "synthetic-preprint-2",
    ]
    assert source.read_bytes() == source_before
    assert {
        item["key"]: item["value"] for item in result["diagnostics"]
    }["canary_public_preprint_snapshot"] == profile.public_preprint_snapshot_sha256

    other = source.with_name("other.sqlite")
    other.write_bytes(source_before)
    monkeypatch.setenv("DISCOVERY_STORE_PATH", str(other))
    rebound = retrieve_preprint_announcement_history_impl(
        "clinical AI",
        database_url=f"sqlite:///{tmp_path / 'canonical.sqlite'}",
    )
    assert rebound["items"][0]["feed_item_id"] == "synthetic-preprint-1"
    assert {
        item["key"]: item["value"] for item in rebound["diagnostics"]
    }["canary_public_preprint_snapshot"] == profile.public_preprint_snapshot_sha256
    assert source.read_bytes() == source_before
    assert other.read_bytes() == source_before


def test_public_preprint_wrapper_bypasses_populated_canonical_store(
    tmp_path,
    monkeypatch,
):
    profile, path, _, context, _, source = setup_public_preprint_profile(
        tmp_path, monkeypatch
    )
    claim = activate(profile, context, monkeypatch)
    env = child_environment(profile, claim, path, turn_request_limit=5)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    canonical_url = f"sqlite:///{tmp_path / 'canonical.sqlite'}"
    canonical = SQLiteStore(canonical_url)
    canonical.save_announcement_feed_item(
        AnnouncementFeedItem(
            title="Unreviewed canonical record",
            url="https://unreviewed.example/private",
            source="preprints",
            feed="preprints",
            summary="Synthetic content outside the approved snapshot.",
        )
    )
    canonical_path = tmp_path / "canonical.sqlite"
    canonical_before = canonical_path.read_bytes()
    source_before = source.read_bytes()

    result = retrieve_preprint_announcement_history_impl(
        "unreviewed",
        database_url=canonical_url,
    )

    assert result["items"] == []
    assert result["diagnostics"][0]["value"] == "bypassed_by_public_snapshot"
    assert any(
        item["key"] == "canary_public_preprint_snapshot"
        for item in result["diagnostics"]
    )
    assert canonical_path.read_bytes() == canonical_before
    assert source.read_bytes() == source_before


def test_public_preprint_followup_denies_history_tool_and_actual_read(
    tmp_path,
    monkeypatch,
):
    profile, path, _, root, followup_text, source = setup_public_preprint_profile(
        tmp_path, monkeypatch
    )
    claim = activate(profile, root, monkeypatch)
    env = child_environment(profile, claim, path, turn_request_limit=5)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    ScopeLedger(profile).finish(claim, 0)
    followup = _followup_context(root, followup_text)
    followup["request_ts"] = "223.000002"
    ScopeLedger(profile).claim_followup(followup)
    monkeypatch.setenv(TURN_REQUEST_LIMIT_ENV, "2")
    source_before = source.read_bytes()

    admit_agent("preprints_context_agent")
    with pytest.raises(CanaryPolicyError, match="provider_not_allowed_for_turn"):
        guarded_response_kwargs(
            client(),
            (),
            {
                **kwargs(),
                "tools": [_function_schema("retrieve_preprint_announcement_history")],
            },
        )
    with pytest.raises(CanaryPolicyError, match="provider_not_allowed_for_turn"):
        retrieve_preprint_announcement_history_impl(
            "clinical AI",
            database_url=f"sqlite:///{tmp_path / 'canonical.sqlite'}",
        )
    admit_agent("opportunity_scout")
    guarded_response_kwargs(client(), (), kwargs())
    assert dispatch_count(profile) == 1
    assert source.read_bytes() == source_before


def test_public_preprint_single_candidate_snapshot_is_valid_and_readable(
    tmp_path,
    monkeypatch,
):
    profile, profile_path, _, context, _, source = setup_public_preprint_profile(
        tmp_path, monkeypatch
    )
    with sqlite3.connect(source) as connection:
        connection.execute(
            "DELETE FROM discovery_candidates WHERE candidate_id != ?",
            ("synthetic-preprint-1",),
        )
    profile = profile.model_copy(
        update={"public_preprint_snapshot_sha256": _hash(source.read_bytes())}
    )
    profile_path.write_text(profile.model_dump_json())
    monkeypatch.setenv(DIGEST_ENV, profile.digest)
    profile.check_paths()
    claim = activate(profile, context, monkeypatch)
    for key, value in child_environment(
        profile, claim, profile_path, turn_request_limit=5
    ).items():
        monkeypatch.setenv(key, value)

    result = retrieve_preprint_announcement_history_impl(
        "clinical AI",
        database_url=f"sqlite:///{tmp_path / 'canonical.sqlite'}",
    )

    assert [item["feed_item_id"] for item in result["items"]] == [
        "synthetic-preprint-1"
    ]


@pytest.mark.parametrize(
    "variant",
    ["missing", "outside", "symlink", "hardlink", "hash", "empty", "foreign"],
)
def test_public_preprint_snapshot_scope_and_identity_controls(
    tmp_path,
    monkeypatch,
    variant,
):
    profile, _, _, _, _, source = setup_public_preprint_profile(tmp_path, monkeypatch)
    if variant == "missing":
        candidate = source.with_name("missing.sqlite")
        changed = profile.model_copy(update={"public_preprint_snapshot": candidate})
    elif variant == "outside":
        candidate = tmp_path / "outside.sqlite"
        candidate.write_bytes(source.read_bytes())
        changed = profile.model_copy(update={"public_preprint_snapshot": candidate})
    elif variant == "symlink":
        candidate = source.with_name("linked.sqlite")
        candidate.symlink_to(source)
        changed = profile.model_copy(update={"public_preprint_snapshot": candidate})
    elif variant == "hardlink":
        candidate = source.with_name("hardlinked.sqlite")
        candidate.hardlink_to(source)
        changed = profile.model_copy(update={"public_preprint_snapshot": candidate})
    elif variant == "hash":
        changed = profile.model_copy(
            update={"public_preprint_snapshot_sha256": "0" * 64}
        )
    else:
        with sqlite3.connect(source) as connection:
            if variant == "empty":
                connection.execute("DELETE FROM discovery_candidates")
            else:
                connection.execute(
                    "UPDATE discovery_candidates SET candidate_type = 'web_result' "
                    "WHERE candidate_id = ?",
                    ("synthetic-preprint-1",),
                )
        changed = profile.model_copy(
            update={"public_preprint_snapshot_sha256": _hash(source.read_bytes())}
        )
    with pytest.raises(CanaryPolicyError):
        changed.check_paths()


def test_public_preprint_source_replacement_and_disabled_profile_fail_before_read(
    tmp_path,
    monkeypatch,
):
    profile, profile_path, _, context, _, source = setup_public_preprint_profile(
        tmp_path, monkeypatch
    )
    activate(profile, context, monkeypatch)
    source.write_bytes(source.read_bytes() + b"replacement")
    with pytest.raises(CanaryPolicyError, match="hash_mismatch"):
        public_preprint_snapshot_guard(source)

    disabled = profile.model_copy(update={"enabled": False})
    profile_path.write_text(disabled.model_dump_json())
    monkeypatch.setenv(DIGEST_ENV, disabled.digest)
    with pytest.raises(CanaryPolicyError, match="not_allowed"):
        public_preprint_snapshot_guard(source)


def test_public_preprint_profile_accepts_lower_explicit_request_allocation(
    tmp_path,
    monkeypatch,
):
    profile, path, _, context, _, source = setup_public_preprint_profile(
        tmp_path,
        monkeypatch,
        max_requests=6,
        root_max_requests=4,
        followup_max_requests=2,
    )
    profile.check_paths()
    claim = activate(profile, context, monkeypatch)
    env = child_environment(profile, claim, path, turn_request_limit=4)
    manifest = profile.manifest()

    assert profile.required_reserve_usd == Decimal("1.881")
    assert manifest["per_turn_request_limits"] == {"root": 4, "followup": 2}
    assert manifest["max_requests"] == 6
    assert manifest["max_orchestrator_requests"] == 4
    assert manifest["required_reserve_usd"] == "1.881"
    assert env["DISCOVERY_STORE_PATH"] == str(source)
    assert env["KEYSTONE_MODEL_REQUEST_BUDGET_LIMIT"] == "6"
    assert env[TURN_REQUEST_LIMIT_ENV] == "4"


def test_public_preprint_launcher_rejects_live_search_and_preserves_reviewed_root_cap(
    tmp_path,
    monkeypatch,
):
    profile, _, context_path, context, _, _ = setup_public_preprint_profile(
        tmp_path, monkeypatch
    )
    argv = [
        "-m",
        "keystone_agents.cli",
        "ask",
        "--context-file",
        str(context_path),
        "--max-openai-requests",
        "12",
        "--live-sdk",
        "--json",
        context["request_text"],
    ]
    rewritten, _ = prepare_root_arguments(profile, argv)
    assert rewritten[rewritten.index("--max-openai-requests") + 1] == "5"
    assert "--live-search" not in rewritten
    with pytest.raises(CanaryPolicyError, match="live_search_not_allowed"):
        prepare_root_arguments(profile, [*argv[:-1], "--live-search", argv[-1]])


def test_public_preprint_profile_rejects_capacity_or_credential_broadening(
    tmp_path,
    monkeypatch,
):
    profile, _, _, _, _, _ = setup_public_preprint_profile(tmp_path, monkeypatch)
    invalid_updates = [
        {"max_requests": 8},
        {"root_max_requests": 6},
        {"followup_max_requests": 3},
        {"root_max_requests": None},
        {"followup_max_requests": None},
        {"max_requests": 7, "root_max_requests": 4, "followup_max_requests": 2},
        {"max_requests": 6, "root_max_requests": 5, "followup_max_requests": 2},
        {"model_profile": "terra_luna"},
        {"max_structured_output_retries": 1},
        {"max_output_tokens": 3_001},
        {"max_input_bytes": 130_001},
        {"max_body_bytes": 140_001},
        {"author_id": ""},
        {"gmail_token_copy": tmp_path / "gmail.json"},
    ]
    for update in invalid_updates:
        with pytest.raises(ValidationError):
            AcceptanceProfile.model_validate({**profile.model_dump(), **update})


@pytest.mark.parametrize(
    ("max_requests", "root_max_requests", "followup_max_requests"),
    [(7, 5, 2), (6, 4, 2)],
)
def test_public_preprint_turn_and_aggregate_caps_are_independent(
    tmp_path,
    monkeypatch,
    max_requests,
    root_max_requests,
    followup_max_requests,
):
    profile, _, _, root, followup_text, _ = setup_public_preprint_profile(
        tmp_path,
        monkeypatch,
        max_requests=max_requests,
        root_max_requests=root_max_requests,
        followup_max_requests=followup_max_requests,
    )
    claim = activate(profile, root, monkeypatch)
    for _ in range(root_max_requests):
        admit_agent("preprints_context_agent")
        guarded_response_kwargs(client(), (), kwargs())
    admit_agent("preprints_context_agent")
    with pytest.raises(CanaryPolicyError, match="turn_request_budget_exhausted"):
        guarded_response_kwargs(client(), (), kwargs())
    ScopeLedger(profile).finish(claim, 0)

    followup = _followup_context(root, followup_text)
    followup["request_ts"] = "223.000002"
    ScopeLedger(profile).claim_followup(followup)
    for _ in range(followup_max_requests):
        admit_agent("opportunity_scout")
        guarded_response_kwargs(client(), (), kwargs())
    admit_agent("opportunity_scout")
    with pytest.raises(CanaryPolicyError, match="request_budget_exhausted"):
        guarded_response_kwargs(client(), (), kwargs())
    receipts = dispatch_records(profile)
    assert len(receipts) == max_requests
    assert [row["turn_max_requests"] for row in receipts] == (
        [root_max_requests] * root_max_requests
        + [followup_max_requests] * followup_max_requests
    )
    assert [row["turn_ordinal"] for row in receipts] == (
        [1] * root_max_requests + [2] * followup_max_requests
    )
    assert [row["aggregate_remaining_after_reservation"] for row in receipts] == list(
        range(max_requests - 1, -1, -1)
    )


def test_public_preprint_third_followup_stops_with_unused_aggregate_capacity(
    tmp_path,
    monkeypatch,
):
    profile, _, _, root, followup_text, _ = setup_public_preprint_profile(
        tmp_path,
        monkeypatch,
        max_requests=6,
        root_max_requests=4,
        followup_max_requests=2,
    )
    claim = activate(profile, root, monkeypatch)
    for _ in range(3):
        admit_agent("preprints_context_agent")
        guarded_response_kwargs(client(), (), kwargs())
    ScopeLedger(profile).finish(claim, 0)
    followup = _followup_context(root, followup_text)
    followup["request_ts"] = "223.000002"
    ScopeLedger(profile).claim_followup(followup)
    for _ in range(2):
        admit_agent("opportunity_scout")
        guarded_response_kwargs(client(), (), kwargs())
    admit_agent("opportunity_scout")
    with pytest.raises(CanaryPolicyError, match="turn_request_budget_exhausted"):
        guarded_response_kwargs(client(), (), kwargs())

    assert dispatch_count(profile) == 5
    assert ScopeLedger(profile).thread_status()["remaining_model_requests"] == 1


def test_public_preprint_actual_core_read_builder_uses_reviewed_tool_scope(
    tmp_path,
    monkeypatch,
):
    profile, _, _, context, _, _ = setup_public_preprint_profile(tmp_path, monkeypatch)
    activate(profile, context, monkeypatch)
    agent = build_preprints_context_agent(
        request_text=context["request_text"],
        tool_tier="core_read",
        compact_instructions=True,
    )
    tool_names = [tool.name for tool in agent.tools]
    assert tool_names == ["retrieve_preprint_announcement_history"]
    scope = tool_scope_receipt_for_agent(agent)
    assert scope["candidate_tool_names"] == [
        "retrieve_preprint_announcement_history",
        "read_preprint_announcement_evidence",
        "inspect_signal_lifecycle",
    ]
    assert scope["selected_tool_names"] == tool_names
    assert scope["omitted_tool_names"] == [
        "read_preprint_announcement_evidence",
        "inspect_signal_lifecycle",
    ]
    assert scope["omission_reasons"] == [
        "public_acceptance_profile_allowlist"
    ]
    assert scope["source"].endswith("+public_acceptance_profile")
    assert len(agent.instructions.encode("utf-8")) == 34_486
    admit_agent("preprints_context_agent")
    guarded_response_kwargs(
        client(),
        (),
        {
            **kwargs(),
            "input": context["request_text"],
            "instructions": agent.instructions,
            "tools": [_function_schema(name) for name in tool_names],
        },
    )
    assert dispatch_count(profile) == 1

    monkeypatch.delenv(PROFILE_ENV)
    monkeypatch.delenv(DIGEST_ENV)
    ordinary = build_preprints_context_agent(
        request_text=context["request_text"],
        tool_tier="core_read",
        compact_instructions=True,
    )
    assert [tool.name for tool in ordinary.tools] == [
        "retrieve_preprint_announcement_history",
        "read_preprint_announcement_evidence",
        "inspect_signal_lifecycle",
    ]


def test_public_preprint_exact_scope_runs_scripted_sdk_persistence_and_followup(
    tmp_path,
    monkeypatch,
):
    profile, profile_path, _, root, followup_text, source = (
        setup_public_preprint_profile(tmp_path, monkeypatch)
    )
    claim = activate(profile, root, monkeypatch)
    env = child_environment(profile, claim, profile_path, turn_request_limit=5)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    source_before = source.read_bytes()
    captured: list[dict[str, object]] = []
    clients = []
    selected_id = "synthetic-preprint-1"
    selected_url = "https://example.test/preprints/1"
    candidates = [
        {
            "feed_item_id": f"synthetic-preprint-{index}",
            "title": title,
            "url": f"https://example.test/preprints/{index}",
            "source": "Synthetic Archive",
            "feed": "preprints",
            "published_at": f"2026-09-{10 + index:02d}",
            "tags": topics,
            "selected": False,
            "relevance_status": "candidate",
            "selection_reason": (
                "Persisted preprint candidate in linked discovery history."
            ),
            "summary": summary,
            "detailed_summary_seed": summary,
            "source_basis": (
                "feed=preprints; selected=false; "
                "evidence_status=discovery_candidate; "
                f"published_at=2026-09-{10 + index:02d}"
            ),
            "evidence_status": "discovery_candidate",
            "publication_ids": [f"synthetic-{index}"],
            "evidence_notes": [],
            "slack_link": "",
            "source_ids": [
                f"synthetic-preprint-{index}",
                f"synthetic-{index}",
                f"https://example.test/preprints/{index}",
            ],
        }
        for index, (title, summary, topics) in enumerate(
            [
                (
                    "Clinical AI evaluation methods",
                    "A synthetic evaluation methods paper.",
                    ["clinical AI", "evaluation"],
                ),
                (
                    "Workflow measurement study",
                    "A synthetic clinical implementation paper.",
                    ["measurement", "workflow"],
                ),
                (
                    "Behavioral data quality review",
                    "A synthetic data-quality paper.",
                    ["behavioral health", "data quality"],
                ),
            ],
            start=1,
        )
    ]
    selected = candidates[0]
    preprint_output = {
        "mode": "llm",
        "summary": f"Selected {selected['title']}.",
        "query": "",
        "retrieved_item_ids": [selected_id],
        "articles": [
            {
                **{
                    key: value
                    for key, value in selected.items()
                    if key not in {"detailed_summary_seed", "source_ids"}
                },
                "selected": True,
                "relevance_status": "selected",
                "selection_reason": "Best match for the synthetic monitoring objective.",
                "relevance_to_keystone": "Useful for synthetic evidence monitoring.",
            }
        ],
        "decision": {
            "decision_owner": "specialist_agent",
            "decision_stage": "signal_relevance_selection",
            "selected_candidate_ids": [selected_id],
            "candidate_assessments": [
                {
                    "candidate_id": candidate["feed_item_id"],
                    "disposition": (
                        "selected"
                        if candidate["feed_item_id"] == selected_id
                        else "excluded"
                    ),
                    "rationale": "Compared within the bounded synthetic universe.",
                }
                for candidate in candidates
            ],
            "reasoning": "The selected candidate best fits the synthetic request.",
            "limitations": ["Synthetic offline evidence only."],
        },
    }
    PreprintsContextResult.model_validate(preprint_output)

    def response(request):
        body = json.loads(request.content)
        captured.append(body)
        ordinal = len(captured)
        if ordinal == 2:
            output = [
                {
                    "type": "function_call",
                    "id": "function-preprints",
                    "call_id": "preprints-history-1",
                    "name": "retrieve_preprint_announcement_history",
                    "arguments": json.dumps(
                        {"query": "", "selected_only": None, "limit": 8}
                    ),
                    "status": "completed",
                }
            ]
        else:
            payload = {
                1: {"answer": "preprints_context_agent"},
                3: preprint_output,
                4: {"answer": "opportunity_scout"},
                5: {
                    "answer": (
                        "Monitor this synthetic signal; evidence is preliminary. "
                        f"Source: {selected_url}"
                    )
                },
            }[ordinal]
            output = [
                {
                    "type": "message",
                    "id": f"message-{ordinal}",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(payload),
                            "annotations": [],
                        }
                    ],
                }
            ]
        return httpx.Response(
            200,
            json={
                "id": f"response-{ordinal}",
                "object": "response",
                "created_at": 0,
                "model": MODEL,
                "status": "completed",
                "output": output,
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            },
        )

    def create_client(**options):
        client_instance = AsyncOpenAI(
            api_key="synthetic-key",
            base_url="https://api.openai.com/v1",
            max_retries=options["max_retries"],
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(response)),
        )
        clients.append(client_instance)
        return client_instance

    monkeypatch.setattr(sdk, "AsyncOpenAI", create_client)
    run_config = sdk.build_live_run_config(
        ModelConfig(provider="openai", model=MODEL, api_key="synthetic-key"),
        tracing_disabled=True,
    )

    class Owner(BaseModel):
        answer: str

    orchestrator = Agent(
        name="orchestrator",
        instructions="Return the selected synthetic owner.",
        output_type=Owner,
        tools=[],
    )
    preprints = build_preprints_context_agent(
        request_text=root["request_text"],
        tool_tier="core_read",
        compact_instructions=True,
    )
    preprints.tools = [
        tool
        for tool in preprints.tools
        if tool.name == "retrieve_preprint_announcement_history"
    ]
    plan = ManualRequestPlan(
        source="canonical:synthetic_public_preprint_followup",
        requested_agent="opportunity_scout",
        target_agent="opportunity_scout",
        intent="route_request",
        task_objective="route_or_continue",
        expected_artifact_type="none",
        objective=followup_text,
        side_effect_policy="draft_or_read_only",
        ask_shape=AskShapePolicy(
            prior_context_dependency="selected_context",
            permission_state="read_only",
            audience_scope="internal",
        ),
    )

    with activate_model_request_budget(7):
        root_owner = run_typed_sdk_agent(
            agent=orchestrator,
            typed_input=root["request_text"],
            output_type=Owner,
            live=True,
            run_config=run_config,
            max_turns=1,
        )
        assert root_owner.output.answer == "preprints_context_agent"
        signal = run_signal_context_sdk(
            "preprints",
            root["request_text"],
            live=True,
            run_config=run_config,
            agent=preprints,
            max_turns=3,
        )
        store = SQLiteStore(profile.database_url)
        run_id = store.save_agent_run(
            agent_name="preprints_context_agent",
            input_summary=root["request_text"],
            output={
                "status": "done",
                "route": "preprints_context_agent",
                "output": signal.output.model_dump(mode="json"),
                "public_result": {
                    "status": "completed",
                    "text": signal.output.summary,
                    "completion_confirmed": True,
                },
                "slack_run_provenance": {
                    "schema": "keystone.slack.run_provenance.v1",
                    "context_validated": True,
                    "team_id": root["team_id"],
                    "channel_id": root["channel_id"],
                    "thread_ts": root["thread_ts"],
                    "request_ts": root["request_ts"],
                },
                "internal_decision_ownership": signal.request_cache[
                    "decision_ownership"
                ],
                "_sdk_request_cache": signal.request_cache,
            },
            model="synthetic-fake-transport",
            dry_run=False,
            status="success",
        )
        row = store.get_agent_run(run_id)
        verified = _verified_signal_context_from_agent_run(row or {})
        assert verified is not None
        assert verified.selected_sources[0].source_id == selected_id

        ScopeLedger(profile).finish(claim, 0)
        followup = _followup_context(root, followup_text)
        followup["request_ts"] = "223.000002"
        ScopeLedger(profile).claim_followup(followup)
        monkeypatch.setenv(TURN_REQUEST_LIMIT_ENV, "2")
        followup_owner = run_typed_sdk_agent(
            agent=orchestrator,
            typed_input=followup_text,
            output_type=Owner,
            live=True,
            run_config=run_config,
            max_turns=1,
        )
        assert followup_owner.output.answer == "opportunity_scout"
        opportunity = build_direct_supplied_response_agent(
            "opportunity_scout",
            request_text=followup_text,
            manual_request_plan=plan,
        )
        assert opportunity.tools == [] and opportunity.handoffs == []
        answer = run_typed_sdk_agent(
            agent=opportunity,
            typed_input=DirectAgentResponseInput(
                requested_agent="opportunity_scout",
                original_request=followup_text,
                selected_context=verified.model_dump_json(),
            ),
            output_type=DirectAgentResponse,
            live=True,
            run_config=run_config,
            max_turns=1,
        )

    assert selected_url in answer.output.answer
    assert len(captured) == dispatch_count(profile) == 5
    assert [row["turn_ordinal"] for row in dispatch_records(profile)] == [1, 1, 1, 2, 2]
    assert [row["turn_max_requests"] for row in dispatch_records(profile)] == [5, 5, 5, 2, 2]
    assert [tool["name"] for tool in captured[1]["tools"]] == [
        "retrieve_preprint_announcement_history"
    ]
    assert captured[3]["tools"] == [] and captured[4]["tools"] == []
    assert source.read_bytes() == source_before
    assert all(client_instance.max_retries == 0 for client_instance in clients)


def test_public_preprint_real_cli_path_runs_fake_transport_and_persisted_followup(
    tmp_path,
    monkeypatch,
):
    from agents import _debug

    from keystone_agents.cli import main as production_cli_main
    from keystone_agents.entrypoints import cli_impl
    from keystone_agents.schemas.decision_ownership import DecisionCandidateAssessment
    from keystone_agents.schemas.orchestrator import (
        OrchestratorPlanningResult,
        OrchestratorRouteDecision,
    )

    profile, profile_path, context_path, root, followup_text, source = (
        setup_public_preprint_profile(
            tmp_path,
            monkeypatch,
            max_requests=6,
            root_max_requests=4,
            followup_max_requests=2,
        )
    )
    representative_context = (
        "Synthetic abstract context describing study design, evaluation setting, "
        "measured outcomes, implementation limitations, publication status, and "
        "potential evidence-monitoring relevance without claiming clinical validity. "
    ) * 8
    with sqlite3.connect(source) as connection:
        connection.execute(
            "UPDATE discovery_candidates SET summary = summary || ?",
            (representative_context,),
        )
        rows = connection.execute(
            "SELECT candidate_id, source, source_item_id, title, summary, published, "
            "url, topics_json FROM discovery_candidates ORDER BY candidate_id"
        ).fetchall()
    profile = profile.model_copy(
        update={"public_preprint_snapshot_sha256": _hash(source.read_bytes())}
    )
    profile_path.write_text(profile.model_dump_json())
    monkeypatch.setenv(DIGEST_ENV, profile.digest)
    source_before = source.read_bytes()
    selected_row = rows[1]
    selected_id = str(selected_row[0])
    selected_url = str(selected_row[6])

    candidates = [
        {
            "feed_item_id": str(row[0]),
            "title": str(row[3]),
            "url": str(row[6]),
            "source": str(row[1]),
            "feed": "preprints",
            "published_at": str(row[5]),
            "tags": json.loads(str(row[7]) or "[]"),
            "selected": False,
            "relevance_status": "candidate",
            "selection_reason": "Persisted reviewed public-preprint candidate.",
            "summary": str(row[4]),
            "detailed_summary_seed": str(row[4]),
            "source_basis": (
                "feed=preprints; selected=false; "
                "evidence_status=discovery_candidate; "
                f"published_at={row[5]}"
            ),
            "evidence_status": "discovery_candidate",
            "publication_ids": [str(row[2])],
            "evidence_notes": ["Synthetic offline candidate; no clinical validation."],
            "slack_link": "",
            "source_ids": [str(row[0]), str(row[2]), str(row[6])],
        }
        for row in rows
    ]
    selected = candidates[1]
    preprint_output = {
        "mode": "llm",
        "summary": (
            f"Selected {selected['title']} for bounded evidence monitoring. "
            f"The evidence is synthetic and preliminary. Source: {selected_url}"
        ),
        "query": "",
        "retrieved_item_ids": [selected_id],
        "articles": [
            {
                **{
                    key: value
                    for key, value in selected.items()
                    if key not in {"detailed_summary_seed", "source_ids"}
                },
                "selected": True,
                "relevance_status": "selected",
                "selection_reason": (
                    "Best fit after comparing every candidate in the reviewed snapshot."
                ),
                "relevance_to_keystone": (
                    "Supports synthetic evaluation-method monitoring without a clinical claim."
                ),
            }
        ],
        "decision": {
            "decision_owner": "specialist_agent",
            "decision_stage": "signal_relevance_selection",
            "selected_candidate_ids": [selected_id],
            "candidate_assessments": [
                {
                    "candidate_id": candidate["feed_item_id"],
                    "disposition": (
                        "selected"
                        if candidate["feed_item_id"] == selected_id
                        else "excluded"
                    ),
                    "rationale": (
                        "Compared against the monitoring objective within the complete "
                        "bounded synthetic universe."
                    ),
                }
                for candidate in candidates
            ],
            "reasoning": (
                "The selected study offers the clearest synthetic evaluation-method "
                "signal while the alternatives emphasize workflow or data quality."
            ),
            "limitations": [
                "Synthetic offline evidence only; publication status is preliminary."
            ],
        },
    }
    PreprintsContextResult.model_validate(preprint_output)

    def orchestrator_output(route: str, *, context_only: bool = False) -> dict[str, object]:
        return OrchestratorPlanningResult(
            route=route,
            workflow=[route],
            context_only_response=context_only,
            routing_mode="llm",
            rationale=(
                "Use the verified prior selection without another provider read."
                if context_only
                else "Preprints Context owns the bounded saved-source comparison."
            ),
            decision=OrchestratorRouteDecision(
                decision_owner="orchestrator",
                decision_stage="orchestrator_route_selection",
                selected_candidate_id=route,
                selected_candidate_ids=[route],
                candidate_assessments=[
                    DecisionCandidateAssessment(
                        candidate_id=route,
                        disposition="selected",
                        rationale="The route matches the current bounded task.",
                    )
                ],
                reasoning="Selected the one specialist that owns the requested stage.",
                limitations=["Synthetic fake-transport routing proof only."],
            ),
        ).model_dump(mode="json", exclude={"retrieval_diagnostics"})

    captured: list[dict[str, object]] = []
    clients: list[AsyncOpenAI] = []

    def response(request):
        body = json.loads(request.content)
        captured.append(body)
        ordinal = len(captured)
        if ordinal == 1:
            payload = orchestrator_output("preprints_context_agent")
            output = [{
                "type": "message",
                "id": "root-route-message",
                "role": "assistant",
                "status": "completed",
                "content": [{
                    "type": "output_text",
                    "text": json.dumps(payload),
                    "annotations": [],
                }],
            }]
        elif ordinal == 2:
            output = [{
                "type": "function_call",
                "id": "function-preprints-production-empty",
                "call_id": "preprints-history-production-1",
                    "name": "retrieve_preprint_announcement_history",
                    "arguments": json.dumps(
                        {
                            "query": "rTMS",
                            "selected_only": None,
                        "limit": 8,
                    }
                ),
                "status": "completed",
            }]
        elif ordinal == 3:
            output = [{
                "type": "function_call",
                "id": "function-preprints-production-recovery",
                "call_id": "preprints-history-production-2",
                    "name": "retrieve_preprint_announcement_history",
                    "arguments": json.dumps(
                        {
                            "query": (
                                "synthetic recent saved preprint review Keystone"
                            ),
                            "selected_only": None,
                        "limit": 8,
                    }
                ),
                "status": "completed",
            }]
        elif ordinal == 4:
            output = [{
                "type": "message",
                "id": "preprints-selection-message",
                "role": "assistant",
                "status": "completed",
                "content": [{
                    "type": "output_text",
                    "text": json.dumps(preprint_output),
                    "annotations": [],
                }],
            }]
        elif ordinal == 5:
            payload = orchestrator_output("opportunity_scout", context_only=True)
            output = [{
                "type": "message",
                "id": "followup-route-message",
                "role": "assistant",
                "status": "completed",
                "content": [{
                    "type": "output_text",
                    "text": json.dumps(payload),
                    "annotations": [],
                }],
            }]
        elif ordinal == 6:
            output = [{
                "type": "message",
                "id": "followup-answer-message",
                "role": "assistant",
                "status": "completed",
                "content": [{
                    "type": "output_text",
                    "text": json.dumps({
                        "answer": (
                            "The strongest realistic opportunity is a bounded internal "
                            "evaluation-method monitoring brief; the evidence remains "
                            f"preliminary. Source: {selected_url}"
                        )
                    }),
                    "annotations": [],
                }],
            }]
        else:  # pragma: no cover - unexpected model request is the test failure
            raise AssertionError(f"Unexpected fake OpenAI request {ordinal}")
        return httpx.Response(
            200,
            json={
                "id": f"production-response-{ordinal}",
                "object": "response",
                "created_at": 0,
                "model": MODEL,
                "status": "completed",
                "output": output,
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 30,
                    "total_tokens": 130,
                },
            },
        )

    def create_client(**options):
        client_instance = AsyncOpenAI(
            api_key="synthetic-key",
            base_url="https://api.openai.com/v1",
            max_retries=options["max_retries"],
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(response)),
        )
        clients.append(client_instance)
        return client_instance

    child_payloads: list[dict[str, object]] = []
    child_stderr: list[str] = []

    def execute(argv, **options):
        stdout_buffer, stderr_buffer = StringIO(), StringIO()
        with (
            patch.dict(os.environ, options["env"], clear=True),
            redirect_stdout(stdout_buffer),
            redirect_stderr(stderr_buffer),
        ):
            code = Context().run(production_cli_main, argv[3:])
        child_stderr.append(stderr_buffer.getvalue())
        child_payloads.append(json.loads(stdout_buffer.getvalue()))
        return SimpleNamespace(returncode=code)

    monkeypatch.setattr(sdk, "AsyncOpenAI", create_client)
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)

    root_argv = [
        "-m",
        "keystone_agents.cli",
        "ask",
        "--context-file",
        str(context_path),
        "--max-openai-requests",
        "4",
        "--max-manager-steps",
        "5",
        "--max-results",
        "1",
        "--live-sdk",
        "--json",
        root["request_text"],
    ]
    root_code = run(profile_path, root_argv, executor=execute)
    assert root_code == 0, json.dumps(
        {
            "payloads": child_payloads,
            "stderr": child_stderr,
            "captured_requests": len(captured),
            "dispatches": dispatch_records(profile),
        },
        indent=2,
        default=str,
    )
    root_payload = child_payloads[0]
    assert root_payload["route"] == "preprints_context_agent"
    assert root_payload["selected_agent"] == "preprints_context_agent"
    assert root_payload["status"] == "done"
    assert selected_url in str(root_payload["human_summary"])
    assert root_payload["request_cache"]["request_tool_scope"][
        "selected_tool_names"
    ] == ["retrieve_preprint_announcement_history"]
    resolved_plan = ManualRequestPlan.model_validate(root_payload["manual_request_plan"])
    resolved_estimate = cli_impl._estimate_ask_openai_requests(
        SimpleNamespace(
            context_file=str(context_path),
            agent=None,
            max_manager_steps=5,
            live_search=False,
        ),
        input_text=root["request_text"],
        live_sdk=True,
        live_manual_plan=False,
        requested_route="preprints_context_agent",
        manual_plan=resolved_plan,
        effective_live_search=False,
        observed_orchestrator_requests=1,
    )
    assert resolved_estimate["min"] == 2
    assert resolved_estimate["max"] == 10
    assert resolved_estimate["shared_runtime_ceiling_eligible"] is True
    assert cli_impl._post_preflight_admission_reserve(resolved_estimate) == 1
    assert cli_impl._ask_request_estimate_exceeds_ceiling(
        resolved_estimate,
        requested_limit=4,
        openai_requests_made=1,
    ) is False
    root_run_id = root_payload["agent_run_id"]
    root_row = SQLiteStore(profile.database_url).get_agent_run(root_run_id)
    stored_root = json.loads(str((root_row or {}).get("output_json") or "{}"))
    verified = _verified_signal_context_from_agent_run(root_row or {})
    assert verified is not None, json.dumps(
        {
            "row": {
                key: (root_row or {}).get(key)
                for key in ("id", "agent_name", "status", "dry_run")
            },
            "public_result": stored_root.get("public_result"),
            "output": stored_root.get("output"),
            "slack_run_provenance": stored_root.get("slack_run_provenance"),
            "decision": stored_root.get("internal_decision_ownership"),
            "evidence": stored_root.get("_sdk_request_cache", {}).get(
                "signal_decision_evidence"
            ),
        },
        indent=2,
        default=str,
    )
    assert [item.source_id for item in verified.selected_sources] == [selected_id]
    assert [item.url for item in verified.selected_sources] == [selected_url]
    root_decision = stored_root["internal_decision_ownership"]
    assert root_decision["decision_stage"] == "signal_relevance_selection"
    assert root_decision["attempt_count"] == 1
    assert root_decision["repair_attempted"] is False
    root_signal = stored_root["_sdk_request_cache"]["signal_decision_evidence"]
    assert root_signal["tool_call_count"] == 2
    assert root_signal["model_tool_call_count"] == 2
    assert root_signal["bounded_reformulation"] == {
        "used": True,
        "valid": True,
        "reason": "changed_query_after_empty_result",
    }

    followup = _followup_context(root, followup_text)
    followup["request_ts"] = "223.000002"
    followup["request_text"] = (
        "business agents continue this prior Slack thread.\n"
        f"Current user request (authoritative): {followup_text}\n"
        "Previous result title: Preprints Context Agent completed\n"
        f"Previous result: {root_payload['human_summary']}\n"
        f"User follow-up: {followup_text}\n"
        "Continue the same agent task, treating the current user request as authoritative."
    )
    followup_path = context_path.with_name("followup.json")
    followup_path.write_text(json.dumps(followup))
    followup_argv = [
        "-m",
        "keystone_agents.cli",
        "ask",
        "--context-file",
        str(followup_path),
        "--max-openai-requests",
        "2",
        "--max-manager-steps",
        "5",
        "--max-results",
        "1",
        "--live-sdk",
        "--json",
        followup["request_text"],
    ]
    assert run(profile_path, followup_argv, executor=execute) == 0
    followup_payload = child_payloads[1]
    assert followup_payload["selected_agent"] == "opportunity_scout"
    assert "entrypoint" in followup_payload, json.dumps(
        {
            "block_kind": followup_payload.get("block_kind"),
            "block_reason": followup_payload.get("block_reason"),
            "composition_admission": followup_payload.get(
                "orchestrator_preflight", {}
            ).get("composition_admission"),
            "manual_plan": followup_payload.get("manual_request_plan"),
        },
        indent=2,
        default=str,
    )
    assert followup_payload["entrypoint"] == "direct_supplied_context_response"
    assert followup_payload["completion_confirmed"] is True
    assert selected_url in str(followup_payload["human_summary"])
    assert followup_payload["tool_admission"] == {
        "admitted": False,
        "tool_count": 0,
        "reason": "complete_provider_free_supplied_context",
    }
    assert followup_payload["retained_signal_validation"]["passed"] is True
    assert followup_payload["retained_signal_validation"]["provider_reads"] == 0
    assert followup_payload["retained_signal_validation"]["provider_writes"] == 0

    receipts = dispatch_records(profile)
    assert len(captured) == len(receipts) == 6
    assert [receipt["agent"] for receipt in receipts] == [
        "orchestrator",
        "preprints_context_agent",
        "preprints_context_agent",
        "preprints_context_agent",
        "orchestrator",
        "opportunity_scout",
    ]
    assert [receipt["turn_ordinal"] for receipt in receipts] == [1, 1, 1, 1, 2, 2]
    assert [receipt["turn_max_requests"] for receipt in receipts] == [4, 4, 4, 4, 2, 2]
    assert [receipt["aggregate_remaining_after_reservation"] for receipt in receipts] == [
        5,
        4,
        3,
        2,
        1,
        0,
    ]
    assert [receipt["turn_remaining_after_reservation"] for receipt in receipts] == [
        3,
        2,
        1,
        0,
        1,
        0,
    ]
    assert all(receipt["model"] == MODEL for receipt in receipts)
    assert all(
        receipt["serialized_input_bytes"] <= profile.max_input_bytes
        and receipt["serialized_body_bytes"] <= profile.max_body_bytes
        and receipt["enforced_max_output_tokens"] == profile.max_output_tokens
        for receipt in receipts
    )
    assert max(receipt["serialized_input_bytes"] for receipt in receipts) < 130_000
    assert max(receipt["serialized_body_bytes"] for receipt in receipts) < 140_000
    assert captured[0].get("tools") == []
    assert [tool["name"] for tool in captured[1]["tools"]] == [
        "retrieve_preprint_announcement_history"
    ]
    assert [tool["name"] for tool in captured[2]["tools"]] == [
        "retrieve_preprint_announcement_history"
    ]
    assert [tool["name"] for tool in captured[3]["tools"]] == [
        "retrieve_preprint_announcement_history"
    ]
    assert captured[4].get("tools") == []
    assert captured[5].get("tools") == []
    assert "synthetic recent saved preprint review Keystone" in json.dumps(
        captured[3], sort_keys=True
    )
    assert selected_id in json.dumps(captured[3], sort_keys=True)
    assert selected_url in json.dumps(captured[4], sort_keys=True)
    assert selected_url in json.dumps(captured[5], sort_keys=True)
    assert followup_text in json.dumps(captured[5], sort_keys=True)
    assert source.read_bytes() == source_before
    assert all(client_instance.max_retries == 0 for client_instance in clients)
    assert all(not value.strip() for value in child_stderr)


def test_pinned_dotenv_loader_respects_disabled_guard_for_forced_load(
    tmp_path,
    monkeypatch,
) -> None:
    from keystone_agents.config import load_settings

    sentinel_key = "KEYSTONE_OPENAI_API_KEY"
    sentinel_value = "synthetic-dotenv-sentinel"
    env_file = tmp_path / ".env"
    env_file.write_text(f"{sentinel_key}={sentinel_value}\n")
    monkeypatch.delenv(sentinel_key, raising=False)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")

    disabled = load_settings(env_file=env_file, force_dotenv=True)

    assert disabled.openai_api_key is None
    assert sentinel_key not in os.environ

    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "0")
    enabled = load_settings(env_file=env_file, force_dotenv=True)

    assert enabled.openai_api_key == sentinel_value


@pytest.mark.parametrize("change", ["channel", "team", "request", "context", "thread"])
def test_wrong_root_context_or_channel_is_rejected(tmp_path, monkeypatch, change):
    profile, _, _, context = setup_profile(tmp_path, monkeypatch)
    key = {
        "channel": "channel_id",
        "team": "team_id",
        "request": "request_text",
        "context": "read_context",
        "thread": "thread_ts",
    }[change]
    context[key] = "different"
    with pytest.raises(CanaryPolicyError):
        ScopeLedger(profile).claim(context)
    assert not (profile.state_dir / "acceptance-scope.sqlite3").exists()


def test_duplicate_root_and_changed_profile_never_reexecute(tmp_path, monkeypatch):
    profile, _, _, context = setup_profile(tmp_path, monkeypatch)
    activate(profile, context, monkeypatch)
    with pytest.raises(CanaryPolicyError, match="duplicate"):
        ScopeLedger(profile).claim(context)
    changed_context = {**context, "request_ts": "124.000001", "thread_ts": "124.000001"}
    changed = profile.model_copy(
        update={"context_sha256": _hash(json.dumps(changed_context, sort_keys=True))}
    )
    with pytest.raises(CanaryPolicyError, match="another_root"):
        ScopeLedger(changed).claim(changed_context)
    assert dispatch_count(profile) == 0


def test_preposted_profile_binds_first_server_root_without_future_timestamp(tmp_path, monkeypatch):
    profile, _, _, context = setup_profile(tmp_path, monkeypatch, context_sha256="")
    assert "request_ts" not in profile.model_dump() and "thread_ts" not in profile.model_dump()
    claim = activate(profile, context, monkeypatch)
    changed = {**context, "request_ts": "456.000002", "thread_ts": "456.000002"}
    with pytest.raises(CanaryPolicyError, match="another_root"):
        ScopeLedger(profile).claim(changed)
    with pytest.raises(CanaryPolicyError, match="bound_context_changed"):
        ScopeLedger(profile).claim({**context, "read_context": "Changed after first claim"})
    with sqlite3.connect(profile.state_dir / "acceptance-scope.sqlite3") as connection:
        stored = connection.execute("SELECT claim,context FROM scope").fetchone()
    assert stored[0] == claim and json.loads(stored[1]) == context


def test_preposted_profile_rejects_existing_thread_and_optional_wrong_author(tmp_path, monkeypatch):
    profile, _, _, context = setup_profile(tmp_path, monkeypatch, context_sha256="")
    with pytest.raises(CanaryPolicyError, match="existing_thread"):
        ScopeLedger(profile).claim(
            {
                **context,
                "thread_messages": [
                    {"ts": "122.000001", "role": "user", "text": "Earlier conversation"},
                ],
            }
        )
    restricted = profile.model_copy(update={"author_id": "U_APPROVED"})
    with pytest.raises(CanaryPolicyError, match="scope"):
        ScopeLedger(restricted).claim({**context, "user_id": "U_OTHER"})


def test_public_unbound_profile_atomically_binds_one_minimal_server_root(
    tmp_path,
    monkeypatch,
) -> None:
    profile, _, _, context, _, _ = setup_public_preprint_profile(
        tmp_path,
        monkeypatch,
        context_sha256="",
    )
    root = _minimal_public_root(context, "1900000000.000001")

    claim = ScopeLedger(profile).claim(root)

    assert profile.manifest()["root_context_binding"] == (
        "atomic_first_minimal_slack_root"
    )
    with sqlite3.connect(profile.state_dir / "acceptance-scope.sqlite3") as connection:
        stored = connection.execute(
            "SELECT profile,claim,context,requests FROM scope WHERE slot=1"
        ).fetchone()
        accepted = connection.execute(
            "SELECT ordinal,request_ts,context FROM accepted_turns"
        ).fetchall()
    assert stored == (profile.digest, claim, json.dumps(root, sort_keys=True), 0)
    assert accepted == [(1, root["request_ts"], json.dumps(root, sort_keys=True))]
    assert dispatch_count(profile) == 0

    with pytest.raises(CanaryPolicyError, match="exact_duplicate"):
        ScopeLedger(profile).claim(root)
    with pytest.raises(CanaryPolicyError, match="another_root"):
        ScopeLedger(profile).claim(
            _minimal_public_root(context, "1900000001.000001")
        )
    changed_profile = profile.model_copy(update={"approval_reference": "other-review"})
    with pytest.raises(CanaryPolicyError, match="another_root"):
        ScopeLedger(changed_profile).claim(root)


def test_two_fresh_public_profiles_bind_equivalent_roots_with_different_slack_timestamps(
    tmp_path,
    monkeypatch,
) -> None:
    first, _, _, first_context, _, _ = setup_public_preprint_profile(
        tmp_path / "first",
        monkeypatch,
        context_sha256="",
    )
    second, _, _, second_context, _, _ = setup_public_preprint_profile(
        tmp_path / "second",
        monkeypatch,
        context_sha256="",
    )
    first_root = _minimal_public_root(first_context, "1900000000.000001")
    second_root = _minimal_public_root(second_context, "1900000001.000001")

    assert ScopeLedger(first).claim(first_root)
    assert ScopeLedger(second).claim(second_root)

    with sqlite3.connect(first.state_dir / "acceptance-scope.sqlite3") as connection:
        first_stored = json.loads(connection.execute("SELECT context FROM scope").fetchone()[0])
    with sqlite3.connect(second.state_dir / "acceptance-scope.sqlite3") as connection:
        second_stored = json.loads(
            connection.execute("SELECT context FROM scope").fetchone()[0]
        )
    assert first_stored == first_root
    assert second_stored == second_root
    assert first_stored["request_ts"] != second_stored["request_ts"]


@pytest.mark.parametrize(
    ("change", "error"),
    (
        ({"user_id": "U_FOREIGN"}, "scope"),
        ({"team_id": "T_FOREIGN"}, "scope"),
        ({"channel_id": "C_FOREIGN"}, "scope"),
        ({"request_text": "Different request"}, "request_mismatch"),
        ({"thread_ts": "1899999999.000001"}, "not_minimal"),
        (
            {
                "request_ts": "not-a-slack-timestamp",
                "thread_ts": "not-a-slack-timestamp",
            },
            "not_minimal",
        ),
        ({"read_context": "Prior private context"}, "not_minimal"),
        ({"thread_root_request": "Earlier root"}, "not_minimal"),
        ({"thread_messages": [{"ts": "1", "role": "user", "text": "Earlier"}]}, "not_minimal"),
        ({"thread_fetch_status": "ok"}, "not_minimal"),
        ({"warnings": ["hidden warning"]}, "not_minimal"),
        ({"unexpected": "hidden"}, "not_minimal"),
        ({"eval": {"case_id": "hidden"}}, "not_minimal"),
    ),
)
def test_public_unbound_root_rejects_unapproved_identity_history_or_hidden_context(
    tmp_path,
    monkeypatch,
    change: dict[str, object],
    error: str,
) -> None:
    profile, _, _, context, _, _ = setup_public_preprint_profile(
        tmp_path,
        monkeypatch,
        context_sha256="",
    )
    root = {**_minimal_public_root(context, "1900000000.000001"), **change}

    with pytest.raises(CanaryPolicyError, match=error):
        ScopeLedger(profile).claim(root)

    assert not (profile.state_dir / "acceptance-scope.sqlite3").exists()


def test_public_unbound_root_revalidates_hash_bound_source_before_claim(
    tmp_path,
    monkeypatch,
) -> None:
    profile, _, _, context, _, _ = setup_public_preprint_profile(
        tmp_path,
        monkeypatch,
        context_sha256="",
    )
    changed = profile.model_copy(update={"public_preprint_snapshot_sha256": "0" * 64})

    with pytest.raises(CanaryPolicyError, match="snapshot_hash_mismatch"):
        ScopeLedger(changed).claim(
            _minimal_public_root(context, "1900000000.000001")
        )

    assert not (profile.state_dir / "acceptance-scope.sqlite3").exists()


def test_failed_exact_execution_resumes_with_same_persistent_counter(tmp_path, monkeypatch):
    profile, _, _, context = setup_profile(tmp_path, monkeypatch, max_requests=2)
    claim = activate(profile, context, monkeypatch)
    monkeypatch.setattr(
        "keystone_agents.runtime.durable_execution.current_execution",
        lambda: SimpleNamespace(execution_id="execution-synthetic"),
    )
    admit_agent("chief_of_staff")
    guarded_response_kwargs(client(), (), kwargs())
    ledger = ScopeLedger(profile)
    with pytest.raises(CanaryPolicyError):
        ledger.resume("execution-synthetic")  # Still running, no race with an active child.
    ledger.finish(claim, 1)
    with pytest.raises(CanaryPolicyError):
        ledger.resume("different-execution")
    assert ledger.resume("execution-synthetic") == claim
    admit_agent("outreach_composer")
    guarded_response_kwargs(client(), (), kwargs())
    ledger.finish(claim, 1)
    with pytest.raises(CanaryPolicyError, match="budget"):
        ledger.resume("execution-synthetic")
    assert dispatch_count(profile) == 2


def test_atomic_counter_and_single_use_model_leases(tmp_path, monkeypatch):
    profile, _, _, context = setup_profile(tmp_path, monkeypatch, max_requests=3)
    activate(profile, context, monkeypatch)

    def attempt(_):
        admit_agent("gmail_triage")
        try:
            guarded_response_kwargs(client(), (), kwargs())
            return True
        except CanaryPolicyError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(10))) == 3
    assert dispatch_count(profile) == 3


@pytest.mark.parametrize(
    "override",
    [
        {"extra_body": {"model": "other", "max_output_tokens": 100000}},
        {"extra_headers": {"Authorization": "Bearer secret"}},
        {"extra_headers": {"OpenAI-Project": "different-project"}},
        {"extra_query": {"api-key": "secret"}},
        {"model": "other"},
        {"stream": True},
        {"stream": "true"},
        {"stream": 0},
        {"previous_response_id": "response-other"},
        {"conversation": "conversation-other"},
        {"prompt": {"id": "stored-prompt"}},
        {"service_tier": "priority"},
        {"input": [{"type": "input_image", "image_url": "https://example.test/image"}]},
        {"input": [{"type": "input_file", "file_id": "file-other"}]},
        {"input": [{"type": "input_audio", "data": "audio"}]},
        {"input": [{"type": "item_reference", "id": "item-other"}]},
        {"input": [{"id": "item-other"}]},
        {"input": [{"id": "item-other", "type": None}]},
        {"input": [{"type": "reasoning", "encrypted_content": "opaque"}]},
        {"tools": [{"type": "web_search"}]},
        {"tools": [{"type": "file_search"}]},
        {"tools": [{"type": "code_interpreter"}]},
        {"tools": [{"type": "image_generation"}]},
    ],
)
def test_effective_request_overrides_and_unpriced_inputs_are_blocked(
    tmp_path, monkeypatch, override
):
    profile, _, _, context = setup_profile(tmp_path, monkeypatch)
    activate(profile, context, monkeypatch)
    admit_agent("chief_of_staff")
    with pytest.raises(CanaryPolicyError):
        guarded_response_kwargs(client(), (), {**kwargs(), **override})
    assert dispatch_count(profile) == 0


@pytest.mark.parametrize(
    "item",
    [
        {"type": "summary_text", "text": "Synthetic"},
        {"type": "reasoning_text", "text": "Synthetic"},
        {"type": "message", "content": [{"type": "summary_text", "text": "Synthetic"}]},
        {"type": "reasoning", "summary": [{"type": "reasoning_text", "text": "Synthetic"}]},
        {"type": "reasoning", "content": [{"type": "summary_text", "text": "Synthetic"}]},
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": {}}]},
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Synthetic"}],
         "encrypted_content": "private-synthetic-value"},
        {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "Synthetic",
                                             "file_id": "private-synthetic-value"}]},
    ],
)
def test_reasoning_text_is_allowed_only_in_its_sdk_position(tmp_path, monkeypatch, item):
    profile, _, _, context = setup_profile(tmp_path, monkeypatch)
    activate(profile, context, monkeypatch)
    admit_agent("chief_of_staff")
    with pytest.raises(CanaryPolicyError, match="canary_non_text_input_not_allowed") as exc:
        guarded_response_kwargs(client(), (), {**kwargs(), "input": [item]})
    assert dispatch_count(profile) == 0
    assert exc.value.measurements
    assert all(type(value) is int for value in exc.value.measurements.values())
    assert "private-synthetic-value" not in str(exc.value.measurements)


@pytest.mark.parametrize(
    "change", ["observer", "ciphertext", "id", "summary", "execution", "nested"],
)
def test_encrypted_reasoning_is_bound_to_observed_item_and_execution(tmp_path, monkeypatch, change):
    profile, _, _, context = setup_profile(tmp_path, monkeypatch)
    activate(profile, context, monkeypatch)
    observer = SimpleNamespace()
    admit_agent("chief_of_staff", observer=observer)
    guarded_response_kwargs(client(), (), kwargs(), observer=observer)
    item = ResponseReasoningItem(
        id="rs_synthetic", type="reasoning", summary=[], encrypted_content="synthetic-ciphertext",
    )
    observe_canary_response_reasoning(
        SimpleNamespace(output=[item]), observer=observer,
        dispatch_receipt=observer._keystone_canary_dispatches[-1],
    )
    assert "synthetic-ciphertext" not in repr(observer._keystone_canary_reasoning)
    replay = item.model_dump(exclude_none=True)
    if change == "observer":
        observer = SimpleNamespace()
    elif change == "ciphertext":
        replay["encrypted_content"] = "foreign-ciphertext"
    elif change == "id":
        replay["id"] = "rs_foreign"
    elif change == "summary":
        replay["summary"] = [{"type": "summary_text", "text": "Altered summary"}]
    elif change == "execution":
        monkeypatch.setattr(
            "keystone_agents.runtime.durable_execution.current_execution",
            lambda: SimpleNamespace(execution_id="different-execution"),
        )
    elif change == "nested":
        replay = {"type": "message", "role": "user", "content": [replay]}
    admit_agent("chief_of_staff", observer=observer)
    with pytest.raises(CanaryPolicyError, match="canary_non_text_input_not_allowed"):
        guarded_response_kwargs(client(), (), {**kwargs(), "input": [replay]}, observer=observer)
    assert dispatch_count(profile) == 1


def test_observed_encrypted_reasoning_still_counts_toward_request_byte_cap(tmp_path, monkeypatch):
    profile, _, _, context = setup_profile(
        tmp_path, monkeypatch, max_input_bytes=500, max_body_bytes=800,
    )
    activate(profile, context, monkeypatch)
    observer = SimpleNamespace()
    admit_agent("chief_of_staff", observer=observer)
    guarded_response_kwargs(client(), (), kwargs(), observer=observer)
    item = ResponseReasoningItem(
        id="rs_synthetic", type="reasoning", summary=[], encrypted_content="opaque" * 400,
    )
    observe_canary_response_reasoning(
        SimpleNamespace(output=[item]), observer=observer,
        dispatch_receipt=observer._keystone_canary_dispatches[-1],
    )
    admit_agent("chief_of_staff", observer=observer)
    with pytest.raises(CanaryPolicyError, match="canary_serialized_request_bound_exceeded"):
        guarded_response_kwargs(
            client(), (), {**kwargs(), "input": [item.model_dump(exclude_none=True)]},
            observer=observer,
        )
    assert dispatch_count(profile) == 1


@pytest.mark.parametrize("change", ["input", "schema", "retries", "base_url"])
def test_limits_apply_before_dispatch(tmp_path, monkeypatch, change):
    profile, _, _, context = setup_profile(
        tmp_path, monkeypatch, max_input_bytes=200, max_body_bytes=400
    )
    activate(profile, context, monkeypatch)
    admit_agent("chief_of_staff")
    body = kwargs()
    target = client()
    if change == "input":
        body["input"] = "x" * 300
    elif change == "schema":
        body["text"] = {"schema": "x" * 500}
    elif change == "retries":
        target.max_retries = 2
    else:
        target.base_url = "https://example.test/v1"
    with pytest.raises(CanaryPolicyError) as failure:
        guarded_response_kwargs(target, (), body)
    if change in {"input", "schema"}:
        measures = failure.value.measurements
        assert measures["input_bytes"] > 200 or measures["body_bytes"] > 400
        assert measures["max_input_bytes"] == 200 and measures["max_body_bytes"] == 400
        assert all(type(value) is int for value in measures.values())
    assert dispatch_count(profile) == 0


def test_model_cap_clamps_and_cannot_reuse_stale_agent_admission(tmp_path, monkeypatch):
    profile, _, _, context = setup_profile(tmp_path, monkeypatch)
    activate(profile, context, monkeypatch)
    admit_agent("chief_of_staff")
    result = guarded_response_kwargs(client(), (), {**kwargs(), "max_output_tokens": 99999})
    assert result["max_output_tokens"] == 12000 and result["store"] is False
    with pytest.raises(CanaryPolicyError, match="lease"):
        guarded_response_kwargs(client(), (), kwargs())
    with pytest.raises(CanaryPolicyError, match="agent"):
        admit_agent("google_workspace_context_agent")
    assert dispatch_count(profile) == 1


def test_instruction_repair_agent_is_tool_free_and_canary_admitted_with_fake_model(
    tmp_path,
    monkeypatch,
):
    profile, profile_path, _, context = setup_profile(
        tmp_path,
        monkeypatch,
        max_requests=1,
        root_max_requests=1,
    )
    claim = activate(profile, context, monkeypatch)
    env = child_environment(profile, claim, profile_path, turn_request_limit=1)
    monkeypatch.setenv(
        "KEYSTONE_SDK_STRUCTURED_OUTPUT_MAX_RETRIES",
        env["KEYSTONE_SDK_STRUCTURED_OUTPUT_MAX_RETRIES"],
    )
    monkeypatch.setenv("KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES", "0")
    captured = []
    clients = []

    def response(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "response-repair",
                "object": "response",
                "created_at": 0,
                "model": MODEL,
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": "message-repair",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "response_text": "A concise repaired response is ready.",
                                        "reasoning_summary": "Satisfied the bounded contract.",
                                    }
                                ),
                                "annotations": [],
                            }
                        ],
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            },
        )

    def create_client(**options):
        client_instance = AsyncOpenAI(
            api_key="synthetic-key",
            base_url="https://api.openai.com/v1",
            max_retries=options["max_retries"],
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(response)),
        )
        clients.append(client_instance)
        return client_instance

    monkeypatch.setattr(sdk, "AsyncOpenAI", create_client)
    agent = build_instruction_following_repair_agent(model=MODEL)
    assert agent.name == "instruction_following_repair"
    assert agent.model == MODEL
    assert agent.tools == []
    assert agent.handoffs == []
    typed_input = InstructionFollowingRepairInput(
        original_request="Return exactly six words.",
        interpreted_constraints={"word_count_mode": "exact", "word_count": 6},
        candidate_response="This response has the wrong length.",
        bounded_evidence="Synthetic bounded evidence only.",
    )
    config = sdk.build_live_run_config(
        ModelConfig(provider="openai", model=MODEL, api_key="synthetic-key"),
        tracing_disabled=True,
    )

    with activate_model_request_budget(1):
        result = run_typed_sdk_agent(
            agent=agent,
            typed_input=typed_input,
            output_type=InstructionFollowingRepairOutput,
            live=True,
            run_config=config,
            inherit_env_session=False,
            tracing_disabled=True,
        )

    assert result.output.response_text == "A concise repaired response is ready."
    assert len(captured) == dispatch_count(profile) == 1
    assert clients[0].max_retries == 0
    assert captured[0]["model"] == MODEL
    assert captured[0]["tools"] == []
    assert captured[0]["max_output_tokens"] == 1200
    assert captured[0]["store"] is False
    assert captured[0]["service_tier"] == "default"
    receipt = dispatch_records(profile)[0]
    assert receipt["agent"] == "instruction_following_repair"
    assert receipt["turn_ordinal"] == 1
    assert receipt["turn_max_requests"] == 1
    assert receipt["aggregate_max_requests"] == 1
    assert receipt["max_structured_output_retries"] == 0
    assert receipt["serialized_input_bytes"] <= profile.max_input_bytes
    assert receipt["serialized_body_bytes"] <= profile.max_body_bytes

    admit_agent("instruction_following_repair")
    with pytest.raises(CanaryPolicyError, match="request_budget_exhausted"):
        guarded_response_kwargs(client(), (), kwargs())
    assert dispatch_count(profile) == 1


def test_instruction_repair_canary_rejects_tools_provider_actions_and_other_profiles(
    tmp_path,
    monkeypatch,
):
    profile, profile_path, _, context = setup_profile(tmp_path, monkeypatch)
    claim = activate(profile, context, monkeypatch)
    env = child_environment(profile, claim, profile_path)
    assert env["KEYSTONE_INSTRUCTION_FOLLOWING_REPAIR_MODEL"] == MODEL
    assert profile.manifest()["tool_free_agents"] == ["instruction_following_repair"]

    admit_agent("instruction_following_repair")
    with pytest.raises(CanaryPolicyError, match="repair_tools"):
        guarded_response_kwargs(
            client(),
            (),
            {
                **kwargs(),
                "tools": [
                    {
                        "type": "function",
                        "name": "unexpected_provider_action",
                        "description": "Must remain unavailable.",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            },
        )
    with pytest.raises(CanaryPolicyError, match="repair_provider_action"):
        gmail_read_guard(
            "GET",
            profile.gmail_token_copy,
            "https://gmail.googleapis.com/gmail/v1/users/me",
        )
    with pytest.raises(CanaryPolicyError, match="agent_not_allowed"):
        admit_agent("unknown_instruction_repair")
    assert dispatch_count(profile) == 0

    other, _, _, other_context = setup_profile(
        tmp_path / "mixed",
        monkeypatch,
        model_profile="terra_luna",
    )
    activate(other, other_context, monkeypatch)
    with pytest.raises(CanaryPolicyError, match="agent_not_allowed"):
        admit_agent("instruction_following_repair")
    assert dispatch_count(other) == 0


def test_preflight_reserve_expansion_and_floor(tmp_path, monkeypatch):
    profile, _, _, _ = setup_profile(tmp_path, monkeypatch)
    assert profile.required_reserve_usd == Decimal("2.832")
    for changes in (
        {"max_requests": 12},
        {"observed_balance_usd": "7.00"},
        {"approved_reserve_usd": "1.00"},
    ):
        with pytest.raises(ValidationError):
            AcceptanceProfile.model_validate({**profile.model_dump(), **changes})
    extended = AcceptanceProfile.model_validate(
        {**profile.model_dump(), "max_requests": 12, "expanded_budget_reviewed": True}
    )
    assert extended.required_reserve_usd == Decimal("4.248")


def test_wrong_state_and_original_token_paths_are_denied(tmp_path, monkeypatch):
    profile, _, _, context = setup_profile(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        profile.model_copy(update={"state_dir": Path.cwd() / "operator-state"}).check_paths()
    original = tmp_path / "original-token.json"
    original.write_text("original")
    with pytest.raises(CanaryPolicyError, match="isolated"):
        profile.model_copy(update={"gmail_token_copy": original}).check_paths()
    linked = profile.state_dir / "linked-token.json"
    linked.symlink_to(original)
    with pytest.raises(CanaryPolicyError):
        profile.model_copy(update={"gmail_token_copy": linked}).check_paths()
    assert original.read_text() == "original"


def test_launcher_rewrites_only_reviewed_scope_and_keeps_original_state(tmp_path, monkeypatch):
    profile, profile_path, context_path, context = setup_profile(tmp_path, monkeypatch)
    original = (
        profile.key_env_file.read_bytes(),
        profile.gmail_token_copy.read_bytes(),
        context_path.read_bytes(),
    )
    args = [
        "-m",
        "keystone_agents.cli",
        "ask",
        "--database-url",
        "sqlite:///operator.db",
        "--context-file",
        str(context_path),
        "--max-openai-requests",
        "12",
        "--max-manager-steps",
        "9",
        "--max-results",
        "5",
        "--live-sdk",
        "--json",
        context["request_text"],
    ]
    captured = {}

    def execute(argv, **options):
        captured.update(argv=argv, **options)
        return SimpleNamespace(returncode=0)

    assert run(profile_path, args, executor=execute) == 0
    argv, env = captured["argv"], captured["env"]
    assert argv[0] == str(Path.cwd() / ".venv/bin/python")
    assert argv[argv.index("--database-url") + 1] == profile.database_url
    assert argv[argv.index("--max-openai-requests") + 1] == "8"
    assert argv[argv.index("--max-manager-steps") + 1] == "5"
    assert argv[argv.index("--max-results") + 1] == "1"
    assert env["KEYSTONE_OPENAI_API_KEY"] == "synthetic-project-key"
    assert "OPENAI_API_KEY" not in env and "EXA_API_KEY" not in env
    assert env["GOOGLE_TOKEN_FILE"] == str(profile.gmail_token_copy)
    assert "disabled-workspace-token" in env["GOOGLE_WORKSPACE_OAUTH_TOKEN_PATH"]
    assert env["KEYSTONE_ENABLE_LIVE_GMAIL"] == "true"
    assert env["KEYSTONE_GMAIL_ALLOW_MAILBOX_WRITES"] == "false"
    assert env["KEYSTONE_GMAIL_ALLOW_TEST_SENDS"] == "false"
    assert original == (
        profile.key_env_file.read_bytes(),
        profile.gmail_token_copy.read_bytes(),
        context_path.read_bytes(),
    )
    record = (profile.state_dir / "loaded-runtime.json").read_text()
    assert "synthetic-project-key" not in record and "runtime_fingerprint" in record
    with pytest.raises(CanaryPolicyError, match="duplicate"):
        run(profile_path, args, executor=lambda *_a, **_k: pytest.fail("Duplicate execution"))
    for malformed in (
        [*args, "--allow-writes"],
        ["-c", "print(1)"],
        [*args[:-1], "Different request"],
    ):
        with pytest.raises(CanaryPolicyError):
            prepare_root_arguments(profile, malformed)


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
def test_gmail_mutation_rejected_before_auth_or_provider(tmp_path, monkeypatch, method):
    profile, _, _, context = setup_profile(tmp_path, monkeypatch)
    activate(profile, context, monkeypatch)
    gmail = GmailTool(live=True, token_file=profile.gmail_token_copy)
    monkeypatch.setattr(gmail, "_access_token", lambda: pytest.fail("Mutation attempted auth"))
    with pytest.raises(CanaryPolicyError, match="mutation"):
        gmail._request(method, "drafts", operation="synthetic write")


def test_gmail_get_uses_only_reviewed_token_copy(tmp_path, monkeypatch):
    profile, _, _, context = setup_profile(tmp_path, monkeypatch)
    activate(profile, context, monkeypatch)
    calls = []
    session = SimpleNamespace(
        request=lambda *a, **k: (
            calls.append((a, k))
            or SimpleNamespace(status_code=200, json=lambda: {"id": "synthetic-message"})
        )
    )
    gmail = GmailTool(live=True, token_file=profile.gmail_token_copy, session=session)
    monkeypatch.setattr(gmail, "_access_token", lambda: "synthetic-access")
    assert (
        gmail._request("GET", "messages/synthetic-message", operation="read")["id"]
        == "synthetic-message"
    )
    assert len(calls) == 1
    gmail.token_file = tmp_path / "original-token.json"
    with pytest.raises(CanaryPolicyError, match="token_scope"):
        gmail._request("GET", "messages/synthetic-message", operation="read")
    assert len(calls) == 1


@pytest.mark.parametrize("model_profile", ["mini", "terra_luna"])
@pytest.mark.parametrize("reasoning_type", [None, "summary_text", "reasoning_text", "encrypted"])
def test_real_sdk_outbound_request_is_capped_and_chat_is_disabled(
    tmp_path, monkeypatch, model_profile, reasoning_type
):
    profile, _, _, context = setup_profile(tmp_path, monkeypatch, model_profile=model_profile)
    selected_model = profile.model_for_agent("chief_of_staff")
    activate(profile, context, monkeypatch)
    monkeypatch.setenv("KEYSTONE_LIVE_MODEL_MAX_RETRIES", "9")
    captured = []
    clients = []

    def response(request):
        captured.append((json.loads(request.content), dict(request.headers)))
        if len(captured) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "response-tool",
                    "object": "response",
                    "created_at": 0,
                    "model": selected_model,
                    "status": "completed",
                    "output": [
                        *([ResponseReasoningItem(
                            id="rs_synthetic",
                            type="reasoning",
                            summary=([{"type": "summary_text", "text": "Synthetic summary"}]
                                     if reasoning_type == "summary_text" else []),
                            content=([{"type": "reasoning_text", "text": "Synthetic text"}]
                                     if reasoning_type == "reasoning_text" else None),
                            encrypted_content=("synthetic-ciphertext"
                                               if reasoning_type == "encrypted" else None),
                        ).model_dump(exclude_none=True)] if reasoning_type else []),
                        {
                            "type": "function_call",
                            "id": "function-synthetic",
                            "call_id": "read-synthetic",
                            "name": "read_fixture",
                            "arguments": "{}",
                            "status": "completed",
                        }
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "response-synthetic",
                "object": "response",
                "created_at": 0,
                "model": selected_model,
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": "message-synthetic",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": '{"answer":"ok"}', "annotations": []}
                        ],
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            },
        )

    def create_client(**options):
        client = AsyncOpenAI(
            api_key="synthetic-key",
            base_url="https://api.openai.com/v1",
            max_retries=options["max_retries"],
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(response)),
        )
        clients.append(client)
        return client

    monkeypatch.setattr(sdk, "AsyncOpenAI", create_client)
    config = sdk.build_live_run_config(
        ModelConfig(provider="openai", model=selected_model, api_key="synthetic-key"),
        tracing_disabled=True,
    )

    class Answer(BaseModel):
        answer: str

    @function_tool
    def read_fixture() -> str:
        return "Bounded synthetic evidence."

    agent = Agent(
        name="chief_of_staff",
        instructions="Return a short answer.",
        output_type=Answer,
        tools=[read_fixture],
    )
    with activate_model_request_budget(8):
        result = run_typed_sdk_agent(
            agent=agent,
            typed_input="Synthetic request",
            output_type=Answer,
            live=True,
            run_config=config,
        )
    assert result.output.answer == "ok"
    assert len(captured) == 2
    if reasoning_type:
        reasoning = next(item for item in captured[1][0]["input"]
                         if item.get("type") == "reasoning")
        if reasoning_type == "encrypted":
            assert reasoning["encrypted_content"] == "synthetic-ciphertext"
        else:
            content = reasoning["summary" if reasoning_type == "summary_text" else "content"]
            assert content[0]["type"] == reasoning_type
    for body, headers in captured:
        assert body["model"] == selected_model
        if model_profile == "terra_luna":
            assert body["prompt_cache_options"] == {"mode": "explicit"}
        assert body["max_output_tokens"] == 12000 and body["store"] is False
        assert body["service_tier"] == "default" and headers["user-agent"].startswith(
            "Agents/Python"
        )
        assert not body.get("previous_response_id")
    assert clients[0].max_retries == 0 and dispatch_count(profile) == 2
    with pytest.raises(CanaryPolicyError, match="chat"):
        asyncio.run(clients[0].chat.completions.create(model=selected_model, messages=[]))
    assert len(captured) == 2


@pytest.mark.parametrize("value", [-1, 2, True, False, 1.0, "1", None])
def test_structured_retry_profile_rejects_non_reviewable_values(tmp_path, monkeypatch, value):
    profile, _, _, _ = setup_profile(tmp_path, monkeypatch)
    with pytest.raises(ValidationError):
        AcceptanceProfile.model_validate({
            **profile.model_dump(), "max_structured_output_retries": value,
        })


def test_structured_retry_profile_defaults_off_and_binds_reviewed_opt_in(tmp_path, monkeypatch):
    profile, path, _, _ = setup_profile(tmp_path, monkeypatch)
    assert profile.max_structured_output_retries == 0
    assert child_environment(profile, "synthetic-claim", path)[
        "KEYSTONE_SDK_STRUCTURED_OUTPUT_MAX_RETRIES"
    ] == "0"
    reviewed = AcceptanceProfile.model_validate({
        **profile.model_dump(), "max_structured_output_retries": 1,
    })
    assert reviewed.digest != profile.digest
    assert reviewed.manifest()["max_structured_output_retries"] == 1
    restored = AcceptanceProfile.model_validate_json(reviewed.model_dump_json())
    assert restored == reviewed
    env = child_environment(restored, "synthetic-claim", path)
    assert env["KEYSTONE_SDK_STRUCTURED_OUTPUT_MAX_RETRIES"] == "1"
    assert env["KEYSTONE_MODEL_REQUEST_BUDGET_LIMIT"] == "8"
    assert env["KEYSTONE_LIVE_MODEL_MAX_RETRIES"] == "0"
    assert env["KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES"] == "0"
    assert reviewed.required_reserve_usd == profile.required_reserve_usd == Decimal("2.832")


@pytest.mark.parametrize("retry_limit,profile_cap,shared_cap,second_valid,expected", [
    (0, 8, 8, True, "schema_failed"),
    (1, 8, 8, True, "accepted"),
    (1, 8, 8, False, "schema_failed"),
    (1, 8, 1, True, "shared_cap"),
    (1, 1, 8, True, "persistent_cap"),
])
def test_real_sdk_structured_correction_obeys_profile_and_shared_caps(
    tmp_path, monkeypatch, retry_limit, profile_cap, shared_cap, second_valid, expected
):
    from agents import _debug
    from agents.exceptions import ModelBehaviorError

    from keystone_agents.agents.orchestrator import build_orchestrator_agent
    from keystone_agents.run import sdk_run_failure_metadata
    from keystone_agents.runtime.request_budget import ModelRequestBudgetExhausted
    from keystone_agents.schemas.decision_ownership import AgentDecisionRecord
    from keystone_agents.schemas.orchestrator import OrchestratorResult, OrchestratorRouteDecision

    profile, path, _, context = setup_profile(
        tmp_path, monkeypatch, max_requests=profile_cap,
        max_structured_output_retries=retry_limit,
    )
    claim = activate(profile, context, monkeypatch)
    env = child_environment(profile, claim, path)
    monkeypatch.setenv(
        "KEYSTONE_SDK_STRUCTURED_OUTPUT_MAX_RETRIES",
        env["KEYSTONE_SDK_STRUCTURED_OUTPUT_MAX_RETRIES"],
    )
    monkeypatch.setenv("KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES", "0")
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    private_failed_value = "synthetic_unretained_failed_output_value"
    valid = OrchestratorResult(
        route="chief_of_staff", target_agent="chief_of_staff", routing_mode="llm",
        decision=OrchestratorRouteDecision(
            decision_owner="orchestrator", decision_stage="orchestrator_route_selection",
            selected_candidate_id="chief_of_staff", selected_candidate_ids=["chief_of_staff"],
            candidate_assessments=[{
                "candidate_id": "chief_of_staff", "disposition": "selected",
                "rationale": "The manager owns the bounded review.",
            }],
        ),
        provider_context_decisions=[AgentDecisionRecord(
            decision_owner="orchestrator", decision_stage="provider_context_selection",
            selected_candidate_id="record-a", selected_candidate_ids=["record-a"],
            candidate_assessments=[
                {"candidate_id": "record-a", "disposition": "selected",
                 "rationale": "The supplied source matches the request."},
                {"candidate_id": "record-b", "disposition": "excluded",
                 "rationale": "The other supplied source is unrelated."},
            ],
        )],
    ).model_dump(mode="json", exclude={"retrieval_diagnostics"})
    invalid = json.loads(json.dumps(valid))
    invalid["provider_context_decisions"][0]["selected_candidate_ids"].append("record-b")
    invalid["audit_notes"] = [private_failed_value]
    captured = []
    clients = []

    def response(request):
        body = json.loads(request.content)
        captured.append(body)
        payload = valid if len(captured) == 2 and second_valid else invalid
        return httpx.Response(200, json={
            "id": f"synthetic-response-{len(captured)}", "object": "response",
            "created_at": 0, "model": MODEL, "status": "completed",
            "output": [{
                "type": "message", "id": f"synthetic-message-{len(captured)}",
                "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": json.dumps(payload),
                             "annotations": []}],
            }],
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        })

    def create_client(**options):
        client = AsyncOpenAI(
            api_key="synthetic-key", base_url="https://api.openai.com/v1",
            max_retries=options["max_retries"],
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(response)),
        )
        clients.append(client)
        return client

    monkeypatch.setattr(sdk, "AsyncOpenAI", create_client)
    agent = build_orchestrator_agent(
        model=MODEL, include_tools=False, include_handoffs=False,
        request_text=context["request_text"],
    )
    typed_input = {
        "raw_request": context["request_text"],
        "evidence": [{"candidate_id": identity, "excerpt": "Synthetic original evidence."}
                     for identity in ("record-a", "record-b")],
        "permissions": {"provider_writes": False},
    }
    with activate_model_request_budget(shared_cap) as budget:
        def invoke():
            return run_typed_sdk_agent(
                agent=agent, typed_input=typed_input, output_type=OrchestratorResult,
                live=True, config=ModelConfig(provider="openai", model=MODEL,
                                             api_key="synthetic-key"),
                inherit_env_session=False, tracing_disabled=True,
            )

        if expected == "accepted":
            result = invoke()
            assert result.output.provider_context_decisions[0].selected_candidate_ids == [
                "record-a"
            ]
            assert result.request_cache["structured_output_retries"] == 1
            assert result.usage["requests"] == 2 and result.usage["input_tokens"] == 20
            metadata = result.request_cache
        else:
            error_type = {
                "schema_failed": ModelBehaviorError,
                "shared_cap": ModelRequestBudgetExhausted,
                "persistent_cap": CanaryPolicyError,
            }[expected]
            with pytest.raises(error_type) as caught:
                invoke()
            metadata = sdk_run_failure_metadata(caught.value)["request_cache"]
        assert budget.consumed <= shared_cap
        if expected != "persistent_cap":
            assert budget.consumed == len(captured)

    expected_calls = 2 if retry_limit == 1 and min(profile_cap, shared_cap) > 1 else 1
    assert len(captured) == dispatch_count(profile) == expected_calls
    assert all(client.max_retries == 0 for client in clients)
    for body in captured:
        assert body["max_output_tokens"] == 12000
        assert body["model"] == MODEL and body["store"] is False
        assert body["service_tier"] == "default" and body["tools"] == []
    if len(captured) == 2:
        repair_text = json.dumps(captured[1]["input"])
        assert "decision_selected_set_mismatch" in repair_text
        assert "provider_context_decisions" in repair_text
        assert (
            "set union of non-empty selected_candidate_id and selected_candidate_ids"
        ) in repair_text
        assert "Synthetic original evidence." in repair_text
        assert context["request_text"] in repair_text
        assert private_failed_value not in repair_text
        assert captured[0]["text"] == captured[1]["text"]
    detail = metadata["validation_diagnostics"]["failures"][0]["errors"][0]
    assert detail["location"] == ["provider_context_decisions", 0]
    assert detail["rule_code"] == "decision_selected_set_mismatch"
    assert private_failed_value not in json.dumps(metadata)


def test_mixed_model_profile_pins_roles_reserves_and_persists_planner_limit(tmp_path, monkeypatch):
    from keystone_agents.model_provider import CANARY_LUNA_MODEL, CANARY_TERRA_MODEL

    profile, path, _, context = setup_profile(tmp_path, monkeypatch, model_profile="terra_luna")
    claim = activate(profile, context, monkeypatch)
    env = child_environment(profile, claim, path)
    assert env["KEYSTONE_ORCHESTRATOR_MODEL"] == CANARY_TERRA_MODEL
    assert env["KEYSTONE_CHIEF_OF_STAFF_MODEL"] == CANARY_LUNA_MODEL
    assert env["KEYSTONE_GMAIL_TRIAGE_MODEL"] == CANARY_LUNA_MODEL
    assert profile.required_reserve_usd == Decimal("3.0872")
    assert profile.manifest()["max_orchestrator_requests"] == 1
    admit_agent("orchestrator")
    with pytest.raises(CanaryPolicyError, match="endpoint"):
        guarded_response_kwargs(client(), (), {**kwargs(), "model": CANARY_LUNA_MODEL})
    admit_agent("orchestrator")
    body = guarded_response_kwargs(client(), (), {**kwargs(), "model": CANARY_TERRA_MODEL})
    assert body["prompt_cache_options"] == {"mode": "explicit"}
    # New ledger handle cannot reset the observed planner allocation.
    assert ScopeLedger(profile).profile.digest == profile.digest
    admit_agent("orchestrator")
    with pytest.raises(CanaryPolicyError, match="comparison_limit"):
        guarded_response_kwargs(client(), (), {**kwargs(), "model": CANARY_TERRA_MODEL})
    for _ in range(7):
        admit_agent("chief_of_staff")
        guarded_response_kwargs(client(), (), {**kwargs(), "model": CANARY_LUNA_MODEL})
    admit_agent("chief_of_staff")
    with pytest.raises(CanaryPolicyError, match="budget_exhausted"):
        guarded_response_kwargs(client(), (), {**kwargs(), "model": CANARY_LUNA_MODEL})
    assert dispatch_count(profile) == 8


def _followup_context(root, text="Shorten the recommendation."):
    wrapper = (
        "business agents continue this prior Slack thread.\n"
        f"Current user request (authoritative): {text}\n"
        "Previous result: A bounded synthetic recommendation.\n"
        f"User follow-up: {text}\n"
        "Continue the same agent task, treating the current user request as authoritative."
    )
    return {**root, "request_ts": "123.000002", "request_text": wrapper,
            "thread_messages": [{"ts": root["request_ts"], "role": "user",
                                 "text": root["request_text"]}]}


def _seed_completed_public_root(profile, root, monkeypatch, *, requests=4):
    from keystone_agents.runtime.durable_execution import (
        ExecutionStore,
        execution_database_path,
    )

    claim = activate(profile, root, monkeypatch)
    execution_id = "ex_synthetic_renewal_root"
    store = ExecutionStore(execution_database_path(profile.database_url))
    store.begin(
        {"request": "synthetic renewal root"},
        execution_id=execution_id,
        budget_limit=profile.max_requests,
        source_fingerprint="1" * 64,
    )
    monkeypatch.setattr(
        "keystone_agents.runtime.durable_execution.current_execution",
        lambda: SimpleNamespace(execution_id=execution_id),
    )
    owners = [
        "orchestrator",
        "preprints_context_agent",
        "preprints_context_agent",
        "instruction_following_repair",
    ]
    for owner in owners[:requests]:
        admit_agent(owner)
        guarded_response_kwargs(client(), (), kwargs())
    with store.connection() as connection:
        connection.execute(
            "UPDATE executions SET status='completed',consumed=? WHERE id=?",
            (requests, execution_id),
        )
    (profile.state_dir / "loaded-runtime.json").write_text(
        json.dumps({
            "runtime_fingerprint": {
                "source_sha256": "2" * 64,
            }
        })
    )
    ScopeLedger(profile).finish(claim, 0)
    return claim, execution_id


def _renewal_case(tmp_path, monkeypatch):
    import keystone_agents.canary_acceptance as policy

    profile, profile_path, _, root, _, _ = setup_public_preprint_profile(
        tmp_path,
        monkeypatch,
        max_requests=6,
        root_max_requests=4,
        followup_max_requests=2,
    )
    _seed_completed_public_root(profile, root, monkeypatch)
    renewed_at = profile.balance_observed_at + timedelta(minutes=31)

    class RenewalClock(datetime):
        @classmethod
        def now(cls, zone=None):
            return renewed_at.astimezone(zone) if zone else renewed_at.replace(tzinfo=None)

    monkeypatch.setattr(policy, "datetime", RenewalClock)
    replacement = AcceptanceProfile.model_validate(
        {
            **profile.model_dump(),
            "followup_request_sha256": _hash("Define iTBS from the article"),
            "approval_reference": "synthetic-renewal-review",
            "observed_balance_usd": "100",
            "approved_reserve_usd": "10",
            "balance_observed_at": renewed_at,
        }
    )
    from keystone_agents.runtime.provenance import build_runtime_fingerprint

    review = {
        "schema": "keystone.canary_followup_renewal_review.v1",
        "status": "reconciled",
        "usage_reconciled": True,
        "observed_balance_usd": "100",
        "approved_reserve_usd": "10",
        "balance_observed_at": renewed_at.isoformat(),
        "open_hold_usd": "0",
        "approval_reference": replacement.approval_reference,
        "evidence_reference": "synthetic-admin-usage-evidence",
        "rationale": "Replace the pending reviewed follow-up after budget reconciliation.",
        "original_runtime_source_sha256": "2" * 64,
        "replacement_runtime_source_sha256": build_runtime_fingerprint(
            repo_root=profile.repo_root
        )["source_sha256"],
    }
    return profile, profile_path, replacement, review, root


def _renew(profile, replacement, review):
    return ScopeLedger(profile).renew_awaiting_followup(
        replacement,
        expected_old_profile_digest=profile.digest,
        review=review,
    )


def test_reviewed_followup_renewal_preserves_root_dispatches_and_budget(
    tmp_path, monkeypatch,
):
    profile, _, replacement, review, root = _renewal_case(tmp_path, monkeypatch)
    original_dispatches = dispatch_records(profile)

    receipt = _renew(profile, replacement, review)

    state = ScopeLedger(replacement).thread_status()
    assert state["status"] == "awaiting_followup"
    assert state["accepted_turns"] == 1
    assert state["model_requests"] == 4
    assert state["remaining_model_requests"] == 2
    assert dispatch_records(replacement) == original_dispatches
    assert receipt["old_profile_sha256"] == profile.digest
    assert receipt["new_profile_sha256"] == replacement.digest
    assert receipt["old_followup_request_sha256"] == profile.followup_request_sha256
    assert receipt["new_followup_request_sha256"] == _hash(
        "Define iTBS from the article"
    )
    assert receipt["root_request_ts"] == root["request_ts"]
    assert receipt["rationale"] == review["rationale"]
    with ScopeLedger(replacement).connection() as connection:
        scope = connection.execute("SELECT * FROM scope WHERE slot=1").fetchone()
        accepted = connection.execute("SELECT * FROM accepted_turns").fetchall()
        renewals = connection.execute("SELECT record FROM renewals").fetchall()
    assert scope["profile"] == replacement.digest
    assert scope["requests"] == 4
    assert len(accepted) == len(renewals) == 1
    assert json.loads(renewals[0][0]) == receipt


def test_followup_renewal_is_compare_and_set_and_cannot_repeat(
    tmp_path, monkeypatch,
):
    profile, _, replacement, review, _ = _renewal_case(tmp_path, monkeypatch)
    _renew(profile, replacement, review)
    with pytest.raises(CanaryPolicyError, match="old_digest_mismatch"):
        _renew(profile, replacement, review)
    with ScopeLedger(replacement).connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM renewals").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM accepted_turns").fetchone()[0] == 1


def test_followup_renewal_can_refresh_budget_without_changing_approved_ask(
    tmp_path, monkeypatch,
):
    profile, _, replacement, review, _ = _renewal_case(tmp_path, monkeypatch)
    same_request = replacement.model_copy(
        update={"followup_request_sha256": profile.followup_request_sha256}
    )

    receipt = _renew(profile, same_request, review)

    assert receipt["old_followup_request_sha256"] == profile.followup_request_sha256
    assert receipt["new_followup_request_sha256"] == profile.followup_request_sha256
    assert ScopeLedger(same_request).thread_status()["model_requests"] == 4


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_sha256", "4" * 64),
        ("author_id", "U_DIFFERENT"),
        ("state_dir", Path("/private/tmp/kba-renewal-different-state")),
        ("public_preprint_snapshot_sha256", "5" * 64),
        ("model_profile", "terra_mini"),
        ("scenario", "gmail_read_only"),
        ("max_requests", 7),
        ("max_output_tokens", 2_999),
    ],
)
def test_followup_renewal_rejects_root_author_path_source_model_tools_or_caps(
    tmp_path, monkeypatch, field, value,
):
    profile, _, replacement, review, _ = _renewal_case(tmp_path, monkeypatch)
    changed = replacement.model_copy(update={field: value})
    with pytest.raises(CanaryPolicyError):
        _renew(profile, changed, review)
    assert ScopeLedger(profile).thread_status()["model_requests"] == 4


def test_followup_renewal_rejects_wrong_expected_old_digest(tmp_path, monkeypatch):
    profile, _, replacement, review, _ = _renewal_case(tmp_path, monkeypatch)
    with pytest.raises(CanaryPolicyError, match="old_digest_mismatch"):
        ScopeLedger(profile).renew_awaiting_followup(
            replacement,
            expected_old_profile_digest="6" * 64,
            review=review,
        )


@pytest.mark.parametrize("state", ["running", "completed", "expired"])
def test_followup_renewal_rejects_nonidle_scope_state(
    tmp_path, monkeypatch, state,
):
    profile, _, replacement, review, _ = _renewal_case(tmp_path, monkeypatch)
    with ScopeLedger(profile).connection() as connection:
        connection.execute("UPDATE scope SET status=? WHERE slot=1", (state,))
    with pytest.raises(CanaryPolicyError, match="state_not_idle"):
        _renew(profile, replacement, review)


def test_followup_renewal_rejects_exhausted_or_missing_root(tmp_path, monkeypatch):
    profile, _, replacement, review, _ = _renewal_case(tmp_path, monkeypatch)
    with ScopeLedger(profile).connection() as connection:
        connection.execute(
            "UPDATE scope SET requests=? WHERE slot=1", (profile.max_requests,)
        )
    with pytest.raises(CanaryPolicyError, match="budget_exhausted"):
        _renew(profile, replacement, review)

    other_path = tmp_path / "missing"
    missing, _, _, _, _, _ = setup_public_preprint_profile(
        other_path,
        monkeypatch,
        max_requests=6,
        root_max_requests=4,
        followup_max_requests=2,
    )
    renewed_missing = AcceptanceProfile.model_validate(
        {
            **missing.model_dump(),
            "followup_request_sha256": replacement.followup_request_sha256,
            "approval_reference": replacement.approval_reference,
            "balance_observed_at": replacement.balance_observed_at,
        }
    )
    from keystone_agents.runtime.durable_execution import ExecutionStore, execution_database_path

    ExecutionStore(execution_database_path(missing.database_url))
    (missing.state_dir / "loaded-runtime.json").write_text(
        json.dumps({"runtime_fingerprint": {"source_sha256": "2" * 64}})
    )
    missing_review = {
        **review,
        "balance_observed_at": renewed_missing.balance_observed_at.isoformat(),
        "approval_reference": renewed_missing.approval_reference,
    }
    with pytest.raises(CanaryPolicyError, match="root_missing"):
        _renew(missing, renewed_missing, missing_review)


def test_followup_renewal_rejects_stale_insufficient_or_unknown_budget(
    tmp_path, monkeypatch,
):
    profile, _, replacement, review, _ = _renewal_case(tmp_path, monkeypatch)
    stale = replacement.model_copy(
        update={"balance_observed_at": profile.balance_observed_at}
    )
    with pytest.raises(CanaryPolicyError, match="observation_stale"):
        _renew(profile, stale, review)

    insufficient = replacement.model_copy(
        update={"observed_balance_usd": Decimal("5")}
    )
    with pytest.raises(CanaryPolicyError, match="profile_invalid"):
        _renew(profile, insufficient, review)

    unknown = {**review, "usage_reconciled": False}
    with pytest.raises(CanaryPolicyError, match="evidence_unknown"):
        _renew(profile, replacement, unknown)

    wrong_source = {**review, "replacement_runtime_source_sha256": "7" * 64}
    with pytest.raises(CanaryPolicyError, match="source_mismatch"):
        _renew(profile, replacement, wrong_source)


def test_followup_renewal_rejects_active_execution_and_unused_lease(
    tmp_path, monkeypatch,
):
    profile, _, replacement, review, _ = _renewal_case(tmp_path, monkeypatch)
    from keystone_agents.runtime.durable_execution import execution_database_path

    with sqlite3.connect(execution_database_path(profile.database_url)) as connection:
        connection.execute("UPDATE executions SET status='running'")
    with pytest.raises(CanaryPolicyError, match="execution_active"):
        _renew(profile, replacement, review)

    with sqlite3.connect(execution_database_path(profile.database_url)) as connection:
        connection.execute("UPDATE executions SET status='completed'")
    with ScopeLedger(profile).connection() as connection:
        connection.execute("INSERT INTO leases VALUES('unused-renewal-lease',0)")
    with pytest.raises(CanaryPolicyError, match="unused_lease"):
        _renew(profile, replacement, review)


def test_followup_renewal_transaction_failure_leaves_old_binding_valid(
    tmp_path, monkeypatch,
):
    profile, _, replacement, review, _ = _renewal_case(tmp_path, monkeypatch)
    with ScopeLedger(profile).connection() as connection:
        connection.execute(
            "CREATE TRIGGER fail_renewal BEFORE UPDATE OF profile ON scope "
            "BEGIN SELECT RAISE(ABORT, 'synthetic renewal failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic renewal failure"):
        _renew(profile, replacement, review)
    with ScopeLedger(profile).connection() as connection:
        scope = connection.execute("SELECT profile,requests FROM scope").fetchone()
        renewal_count = connection.execute("SELECT COUNT(*) FROM renewals").fetchone()[0]
        turn_count = connection.execute("SELECT COUNT(*) FROM accepted_turns").fetchone()[0]
    assert tuple(scope) == (profile.digest, 4)
    assert renewal_count == 0
    assert turn_count == 1


def test_followup_renewal_cli_uses_public_boundary_without_execution(
    tmp_path, monkeypatch, capsys,
):
    profile, profile_path, replacement, review, _ = _renewal_case(tmp_path, monkeypatch)
    replacement_path = tmp_path / "replacement.json"
    review_path = tmp_path / "review.json"
    replacement_path.write_text(replacement.model_dump_json())
    review_path.write_text(json.dumps(review))

    assert run(
        profile_path,
        ["--canary-renew-followup", str(replacement_path), str(review_path)],
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["old_profile_sha256"] == profile.digest
    assert receipt["new_profile_sha256"] == replacement.digest
    assert ScopeLedger(replacement).thread_status()["accepted_turns"] == 1


def test_two_turn_launcher_keeps_state_and_aggregate_budget(tmp_path, monkeypatch):
    from keystone_agents.schemas.work_item import WorkItem, WorkItemKind, WorkItemTarget
    from keystone_agents.storage.sqlite_store import SQLiteStore

    profile, path, context_path, root = setup_profile(
        tmp_path, monkeypatch, followup_request_sha256=_hash("Shorten the recommendation."),
        max_requests=3,
    )
    ledger = ScopeLedger(profile)
    calls = []
    item = WorkItem(kind=WorkItemKind.GMAIL_THREAD, title="Synthetic state", target=WorkItemTarget(
        name="Synthetic", metadata={"slack_context": {
            "channel_id": root["channel_id"], "thread_ts": root["thread_ts"],
        }},
    ))

    def execute(argv, **options):
        calls.append((argv, options))
        assert argv[argv.index("--database-url") + 1] == profile.database_url
        assert options["env"]["DATABASE_URL"] == profile.database_url
        assert options["env"]["KEYSTONE_SDK_SESSION_DB"] == str(
            profile.state_dir / "sdk-sessions.sqlite3"
        )
        store = SQLiteStore(profile.database_url)
        if len(calls) == 1:
            store.save_work_item(item)
        else:
            assert store.get_work_item(item.id).target.name == "Synthetic"
            assert argv[argv.index("--linked-work-item-id") + 1] == item.id
            assert argv[argv.index("--max-openai-requests") + 1] == "1"
        with monkeypatch.context() as scope:
            scope.setenv(CLAIM_ENV, options["env"][CLAIM_ENV])
            scope.setattr("keystone_agents.runtime.durable_execution.current_execution",
                          lambda: SimpleNamespace(execution_id=f"turn-{len(calls)}"))
            for _ in range(2 if len(calls) == 1 else 1):
                admit_agent("chief_of_staff")
                guarded_response_kwargs(client(), (), kwargs())
        return SimpleNamespace(returncode=0)

    root_args = ["-m", "keystone_agents.cli", "ask", "--database-url", "sqlite:///operator.db",
                 "--context-file", str(context_path), "--live-sdk", "--json", root["request_text"]]
    assert run(path, root_args, executor=execute) == 0
    assert ledger.thread_status()["status"] == "awaiting_followup"
    assert ledger.thread_status()["keep_isolated_binding"] is True
    assert ledger.thread_status()["safe_to_restore_after_child_exit"] is False
    assert ledger.thread_status()["remaining_model_requests"] == 1
    followup = _followup_context(root)
    followup_path = context_path.with_name("followup.json")
    followup_path.write_text(json.dumps(followup))
    followup_args = ["-m", "keystone_agents.cli", "ask", "--context-file", str(followup_path),
                     "--linked-work-item-id", item.id,
                     "--live-sdk", "--json", followup["request_text"]]
    assert run(path, followup_args, executor=execute) == 0
    state = ledger.thread_status()
    assert state["status"] == "completed" and state["accepted_turns"] == 2
    assert state["model_requests"] == 3 and state["remaining_model_requests"] == 0
    assert state["safe_to_restore_after_child_exit"] is True
    assert state["post_restoration_thread_routing_supported"] is False
    accepted_paths = [Path(argv[argv.index("--context-file") + 1]) for argv, _ in calls]
    assert len(set(accepted_paths)) == 2
    assert json.loads(accepted_paths[0].read_text()) == root
    assert json.loads(accepted_paths[1].read_text()) == followup
    with ledger.connection() as connection:
        dispatches = [json.loads(r[0])
                      for r in connection.execute("SELECT metadata FROM dispatches")]
    assert [d["execution_id"] for d in dispatches] == ["turn-1", "turn-1", "turn-2"]
    with pytest.raises(CanaryPolicyError, match="not_awaiting"):
        run(path, followup_args, executor=execute)
    assert len(calls) == 2


@pytest.mark.parametrize("field,value", [
    ("channel_id", "C_FOREIGN"), ("team_id", "T_FOREIGN"),
    ("thread_ts", "122.000001"), ("request_ts", "123.000000"),
    ("request_ts", "not-a-timestamp"), ("request_text", "An unapproved second task"),
    ("request_ts", "Infinity"),
    ("thread_messages", [{}] * 13),
])
def test_followup_requires_exact_approved_turn_and_bound_thread(
    tmp_path, monkeypatch, field, value,
):
    profile, _, _, root = setup_profile(
        tmp_path,
        monkeypatch,
        max_requests=6,
        root_max_requests=4,
        followup_max_requests=2,
        followup_request_sha256=_hash("Shorten the recommendation."),
    )
    claim = activate(profile, root, monkeypatch)
    ledger = ScopeLedger(profile)
    ledger.finish(claim, 0)
    with pytest.raises(CanaryPolicyError):
        ledger.claim_followup({**_followup_context(root), field: value})
    assert ledger.thread_status()["accepted_turns"] == 1
    assert ledger.thread_status()["model_requests"] == 0


def test_direct_root_without_workitem_allows_exact_natural_revision_in_same_state(
    tmp_path, monkeypatch,
):
    from keystone_agents.storage.sqlite_store import SQLiteStore

    followup_text = "Make that one sentence, keeping the Gmail link."
    profile, path, context_path, root = setup_profile(
        tmp_path, monkeypatch, followup_request_sha256=_hash(followup_text), max_requests=7,
        max_input_bytes=140000, max_body_bytes=150000, max_output_tokens=3000,
    )
    calls = []

    def execute(argv, **options):
        calls.append(argv)
        assert "--linked-work-item-id" not in argv
        store = SQLiteStore(options["env"]["DATABASE_URL"])
        if len(calls) == 1:
            store.save_agent_run(
                agent_name="gmail_triage", input_payload={}, input_summary="Synthetic root",
                output={"summary": "Two source-backed sentences.", "thread_id": "synthetic-thread"},
                dry_run=True,
            )
        else:
            assert store.fetch_all("agent_runs")[0]["agent_name"] == "gmail_triage"
            assert store.fetch_all("work_items") == []
            assert "User follow-up: " + followup_text in argv[-1]
            assert argv[argv.index("--max-openai-requests") + 1] == "3"
        with monkeypatch.context() as scope:
            scope.setenv(CLAIM_ENV, options["env"][CLAIM_ENV])
            for _ in range(4 if len(calls) == 1 else 2):
                admit_agent("gmail_triage")
                guarded_response_kwargs(client(), (), kwargs())
        return SimpleNamespace(returncode=0)

    argv = ["-m", "keystone_agents.cli", "ask", "--context-file", str(context_path),
            "--live-sdk", "--json", root["request_text"]]
    assert run(path, argv, executor=execute) == 0
    context = _followup_context(root, followup_text)
    context_path = context_path.with_name("direct-followup.json")
    context_path.write_text(json.dumps(context))
    argv = ["-m", "keystone_agents.cli", "ask", "--context-file", str(context_path),
            "--live-sdk", "--json", context["request_text"]]
    assert run(path, argv, executor=execute) == 0
    assert ScopeLedger(profile).thread_status()["accepted_turns"] == 2
    assert dispatch_count(profile) == 6


def test_optional_per_turn_limits_preserve_legacy_digests_and_reserve(
    tmp_path,
    monkeypatch,
):
    base, _, _, _ = setup_profile(tmp_path, monkeypatch)
    assert base.digest == _hash(
        base.model_dump_json(
            exclude={
                "followup_request_sha256",
                "root_max_requests",
                "followup_max_requests",
            }
        )
    )
    legacy_two_turn = AcceptanceProfile.model_validate(
        {
            **base.model_dump(),
            "followup_request_sha256": _hash("Shorten the recommendation."),
        }
    )
    assert legacy_two_turn.digest == _hash(
        legacy_two_turn.model_dump_json(
            exclude={"root_max_requests", "followup_max_requests"}
        )
    )

    reviewed = AcceptanceProfile.model_validate(
        {
            **legacy_two_turn.model_dump(),
            "max_requests": 6,
            "root_max_requests": 4,
            "followup_max_requests": 2,
        }
    )
    assert reviewed.required_reserve_usd == Decimal("2.124")
    assert reviewed.manifest()["per_turn_request_limits"] == {
        "root": 4,
        "followup": 2,
    }
    assert reviewed.digest != legacy_two_turn.digest


def test_reviewed_five_root_two_followup_profile_has_seven_request_reserve(
    tmp_path,
    monkeypatch,
) -> None:
    followup_text = "Shorten the recommendation without reading Gmail again."
    profile, _, _, _ = setup_profile(
        tmp_path,
        monkeypatch,
        max_requests=7,
        root_max_requests=5,
        followup_max_requests=2,
        followup_request_sha256=_hash(followup_text),
        max_output_tokens=3000,
    )

    assert profile.max_requests_for_turn(1) == 5
    assert profile.max_requests_for_turn(2) == 2
    assert profile.required_reserve_usd == Decimal("2.1945")
    assert profile.manifest()["per_turn_request_limits"] == {
        "root": 5,
        "followup": 2,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"root_max_requests": 0},
        {"root_max_requests": True},
        {"followup_max_requests": 0},
        {"root_max_requests": 7, "followup_max_requests": 2},
        {"root_max_requests": 4, "followup_max_requests": 3},
        {"root_max_requests": 4},
        {"followup_max_requests": 2},
    ],
)
def test_two_turn_limits_reject_invalid_partial_or_overbroad_policy(
    tmp_path,
    monkeypatch,
    changes,
):
    base, _, _, _ = setup_profile(tmp_path, monkeypatch)
    values = {
        **base.model_dump(),
        "max_requests": 6,
        "followup_request_sha256": _hash("Shorten the recommendation."),
        **changes,
    }
    with pytest.raises(ValidationError):
        AcceptanceProfile.model_validate(values)


def test_one_root_profile_rejects_followup_limit_but_allows_bounded_root_limit(
    tmp_path,
    monkeypatch,
):
    base, _, _, _ = setup_profile(tmp_path, monkeypatch)
    reviewed = AcceptanceProfile.model_validate(
        {**base.model_dump(), "root_max_requests": 4}
    )
    assert reviewed.manifest()["per_turn_request_limits"] == {"root": 4}
    with pytest.raises(ValidationError):
        AcceptanceProfile.model_validate(
            {**base.model_dump(), "followup_max_requests": 2}
        )


@pytest.mark.parametrize("root_dispatches", [1, 2, 3, 4])
def test_variable_root_usage_leaves_independently_capped_followup(
    tmp_path,
    monkeypatch,
    root_dispatches,
):
    followup_text = "Shorten the recommendation."
    profile, _, context_path, root = setup_profile(
        tmp_path,
        monkeypatch,
        max_requests=6,
        root_max_requests=4,
        followup_max_requests=2,
        followup_request_sha256=_hash(followup_text),
    )
    incoming = [
        "-m",
        "keystone_agents.cli",
        "ask",
        "--context-file",
        str(context_path),
        "--max-openai-requests",
        "12",
        "--live-sdk",
        "--json",
        root["request_text"],
    ]
    rewritten, _ = prepare_root_arguments(profile, incoming)
    assert rewritten[rewritten.index("--max-openai-requests") + 1] == "4"

    claim = activate(profile, root, monkeypatch)
    observer = SimpleNamespace()
    for _ in range(root_dispatches):
        admit_agent("gmail_triage", observer=observer)
        guarded_response_kwargs(client(), (), kwargs(), observer=observer)
    ScopeLedger(profile).finish(claim, 0)

    followup = _followup_context(root, followup_text)
    followup_path = context_path.with_name("followup.json")
    followup_path.write_text(json.dumps(followup))
    incoming[incoming.index("--context-file") + 1] = str(followup_path)
    incoming[-1] = followup["request_text"]
    rewritten, approved_context = prepare_root_arguments(profile, incoming)
    assert rewritten[rewritten.index("--max-openai-requests") + 1] == "2"
    assert ScopeLedger(profile).claim_followup(approved_context) == claim

    for _ in range(2):
        admit_agent("gmail_triage", observer=observer)
        guarded_response_kwargs(client(), (), kwargs(), observer=observer)
    receipts = observer._keystone_canary_dispatches
    assert [row["turn_ordinal"] for row in receipts] == [
        *([1] * root_dispatches),
        2,
        2,
    ]
    assert [row["turn_request_ordinal"] for row in receipts[-2:]] == [1, 2]
    assert all(row["turn_max_requests"] == 2 for row in receipts[-2:])
    assert dispatch_count(profile) == root_dispatches + 2


def test_per_turn_dispatch_guard_blocks_root_and_followup_before_transport(
    tmp_path,
    monkeypatch,
):
    followup_text = "Shorten the recommendation."
    profile, _, _, root = setup_profile(
        tmp_path,
        monkeypatch,
        max_requests=6,
        root_max_requests=4,
        followup_max_requests=2,
        followup_request_sha256=_hash(followup_text),
    )
    claim = activate(profile, root, monkeypatch)
    for _ in range(4):
        admit_agent("gmail_triage")
        guarded_response_kwargs(client(), (), kwargs())
    admit_agent("gmail_triage")
    with pytest.raises(CanaryPolicyError, match="turn_request_budget_exhausted"):
        guarded_response_kwargs(client(), (), kwargs())
    assert dispatch_count(profile) == 4

    ScopeLedger(profile).finish(claim, 0)
    ScopeLedger(profile).claim_followup(_followup_context(root, followup_text))
    for _ in range(2):
        admit_agent("gmail_triage")
        guarded_response_kwargs(client(), (), kwargs())
    admit_agent("gmail_triage")
    with pytest.raises(CanaryPolicyError, match="request_budget_exhausted"):
        guarded_response_kwargs(client(), (), kwargs())
    assert dispatch_count(profile) == 6


def test_unused_root_allowance_does_not_expand_followup_dispatch_limit(
    tmp_path,
    monkeypatch,
):
    followup_text = "Shorten the recommendation."
    profile, _, _, root = setup_profile(
        tmp_path,
        monkeypatch,
        max_requests=6,
        root_max_requests=4,
        followup_max_requests=2,
        followup_request_sha256=_hash(followup_text),
    )
    claim = activate(profile, root, monkeypatch)
    admit_agent("gmail_triage")
    guarded_response_kwargs(client(), (), kwargs())
    ScopeLedger(profile).finish(claim, 0)
    ScopeLedger(profile).claim_followup(_followup_context(root, followup_text))
    for _ in range(2):
        admit_agent("gmail_triage")
        guarded_response_kwargs(client(), (), kwargs())
    admit_agent("gmail_triage")
    with pytest.raises(CanaryPolicyError, match="turn_request_budget_exhausted"):
        guarded_response_kwargs(client(), (), kwargs())
    assert dispatch_count(profile) == 3
    assert ScopeLedger(profile).thread_status()["remaining_model_requests"] == 3


def test_incoming_cli_limit_can_lower_but_not_raise_reviewed_turn_limits(
    tmp_path,
    monkeypatch,
):
    followup_text = "Shorten the recommendation."
    profile, _, context_path, root = setup_profile(
        tmp_path,
        monkeypatch,
        max_requests=6,
        root_max_requests=4,
        followup_max_requests=2,
        followup_request_sha256=_hash(followup_text),
    )

    def limit_for(context, path, requested):
        argv = [
            "-m",
            "keystone_agents.cli",
            "ask",
            "--context-file",
            str(path),
            "--max-openai-requests",
            str(requested),
            "--live-sdk",
            "--json",
            context["request_text"],
        ]
        rewritten, _ = prepare_root_arguments(profile, argv)
        return int(rewritten[rewritten.index("--max-openai-requests") + 1])

    assert limit_for(root, context_path, 12) == 4
    assert limit_for(root, context_path, 3) == 3
    for invalid in (0, -1, "invalid"):
        with pytest.raises(CanaryPolicyError, match="cli_limit_invalid"):
            limit_for(root, context_path, invalid)

    claim = activate(profile, root, monkeypatch)
    admit_agent("gmail_triage")
    guarded_response_kwargs(client(), (), kwargs())
    ScopeLedger(profile).finish(claim, 0)
    followup = _followup_context(root, followup_text)
    followup_path = context_path.with_name("followup.json")
    followup_path.write_text(json.dumps(followup))
    assert limit_for(followup, followup_path, 12) == 2
    assert limit_for(followup, followup_path, 1) == 1


def test_launcher_binds_lower_cli_limit_into_actual_dispatch_guard(
    tmp_path,
    monkeypatch,
):
    followup_text = "Shorten the recommendation."
    profile, profile_path, context_path, root = setup_profile(
        tmp_path,
        monkeypatch,
        max_requests=6,
        root_max_requests=4,
        followup_max_requests=2,
        followup_request_sha256=_hash(followup_text),
    )
    transport_calls = 0

    def execute(argv, **options):
        nonlocal transport_calls
        assert argv[argv.index("--max-openai-requests") + 1] == "2"
        assert options["env"][TURN_REQUEST_LIMIT_ENV] == "2"
        with monkeypatch.context() as scope:
            for key, value in options["env"].items():
                scope.setenv(key, value)
            for _ in range(2):
                admit_agent("gmail_triage")
                guarded_response_kwargs(client(), (), kwargs())
                transport_calls += 1
            admit_agent("gmail_triage")
            with pytest.raises(CanaryPolicyError, match="turn_request_budget_exhausted"):
                guarded_response_kwargs(client(), (), kwargs())
        return SimpleNamespace(returncode=0)

    argv = [
        "-m",
        "keystone_agents.cli",
        "ask",
        "--context-file",
        str(context_path),
        "--max-openai-requests",
        "2",
        "--live-sdk",
        "--json",
        root["request_text"],
    ]
    assert run(profile_path, argv, executor=execute) == 0
    assert transport_calls == dispatch_count(profile) == 2


def test_direct_child_cannot_raise_reviewed_turn_limit_with_environment(
    tmp_path,
    monkeypatch,
):
    profile, _, _, root = setup_profile(
        tmp_path,
        monkeypatch,
        max_requests=6,
        root_max_requests=4,
    )
    activate(profile, root, monkeypatch)
    monkeypatch.setenv(TURN_REQUEST_LIMIT_ENV, "5")
    admit_agent("chief_of_staff")
    with pytest.raises(CanaryPolicyError, match="turn_request_limit_invalid"):
        guarded_response_kwargs(client(), (), kwargs())
    assert dispatch_count(profile) == 0


def test_failed_or_unknown_attempts_cannot_reset_reviewed_turn_limit(
    tmp_path,
    monkeypatch,
):
    profile, _, _, root = setup_profile(
        tmp_path,
        monkeypatch,
        max_requests=3,
        root_max_requests=1,
    )
    claim = activate(profile, root, monkeypatch)
    monkeypatch.setattr(
        "keystone_agents.runtime.durable_execution.current_execution",
        lambda: SimpleNamespace(execution_id="execution-synthetic"),
    )
    admit_agent("chief_of_staff")
    guarded_response_kwargs(client(), (), kwargs())
    ScopeLedger(profile).finish(claim, 1)
    assert ScopeLedger(profile).resume("execution-synthetic") == claim
    admit_agent("chief_of_staff")
    with pytest.raises(CanaryPolicyError, match="turn_request_budget_exhausted"):
        guarded_response_kwargs(client(), (), kwargs())
    assert dispatch_count(profile) == 1


def test_lower_cli_limit_persists_across_failed_execution_resume(
    tmp_path,
    monkeypatch,
):
    profile, _, _, root = setup_profile(
        tmp_path,
        monkeypatch,
        max_requests=6,
        root_max_requests=4,
    )
    claim = activate(profile, root, monkeypatch)
    monkeypatch.setattr(
        "keystone_agents.runtime.durable_execution.current_execution",
        lambda: SimpleNamespace(execution_id="execution-synthetic"),
    )
    monkeypatch.setenv(TURN_REQUEST_LIMIT_ENV, "2")
    for _ in range(2):
        admit_agent("chief_of_staff")
        guarded_response_kwargs(client(), (), kwargs())
    ScopeLedger(profile).finish(claim, 1)
    assert ScopeLedger(profile).resume("execution-synthetic") == claim
    monkeypatch.delenv(TURN_REQUEST_LIMIT_ENV)
    admit_agent("chief_of_staff")
    with pytest.raises(CanaryPolicyError, match="turn_request_budget_exhausted"):
        guarded_response_kwargs(client(), (), kwargs())
    assert dispatch_count(profile) == 2


def test_unknown_reserved_attempt_remains_counted_without_finish(
    tmp_path,
    monkeypatch,
):
    profile, _, _, root = setup_profile(
        tmp_path,
        monkeypatch,
        max_requests=3,
        root_max_requests=1,
    )
    activate(profile, root, monkeypatch)
    admit_agent("chief_of_staff")
    guarded_response_kwargs(client(), (), kwargs())

    # Simulate process loss after reservation and before a terminal result. A new
    # ledger handle reads the durable dispatch instead of reopening the turn.
    assert ScopeLedger(profile).thread_status()["status"] == "running"
    admit_agent("chief_of_staff")
    with pytest.raises(CanaryPolicyError, match="turn_request_budget_exhausted"):
        guarded_response_kwargs(client(), (), kwargs())
    assert dispatch_count(profile) == 1


def test_concurrent_dispatches_cannot_exceed_reviewed_root_limit(
    tmp_path,
    monkeypatch,
):
    profile, _, _, root = setup_profile(
        tmp_path,
        monkeypatch,
        max_requests=6,
        root_max_requests=2,
    )
    activate(profile, root, monkeypatch)

    def attempt(_):
        admit_agent("gmail_triage")
        try:
            guarded_response_kwargs(client(), (), kwargs())
            return True
        except CanaryPolicyError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(8))) == 2
    assert dispatch_count(profile) == 2


def test_changed_turn_limits_cannot_reuse_an_existing_profile_ledger(
    tmp_path,
    monkeypatch,
):
    profile, _, _, root = setup_profile(
        tmp_path,
        monkeypatch,
        max_requests=6,
        root_max_requests=4,
        followup_max_requests=2,
        followup_request_sha256=_hash("Shorten the recommendation."),
    )
    activate(profile, root, monkeypatch)
    changed = profile.model_copy(update={"followup_max_requests": 1})
    assert changed.digest != profile.digest
    with pytest.raises(CanaryPolicyError, match="profile_changed"):
        ScopeLedger(changed).thread_status()


def test_followup_admission_uses_anchored_current_turn_when_context_envelope_is_truncated(
    tmp_path, monkeypatch,
):
    profile, _, context_path, root = setup_profile(
        tmp_path, monkeypatch, followup_request_sha256=_hash("Shorten the recommendation."),
    )
    claim = activate(profile, root, monkeypatch)
    ScopeLedger(profile).finish(claim, 0)
    full = _followup_context(root)
    full["request_text"] = full["request_text"].replace(
        "A bounded synthetic recommendation.", "Synthetic prior evidence. " * 100,
    )
    context = {**full, "request_text": full["request_text"][:1797] + "..."}
    assert "User follow-up:" not in context["request_text"]
    context_path.write_text(json.dumps(context))
    argv = ["-m", "keystone_agents.cli", "ask", "--context-file", str(context_path),
            "--live-sdk", "--json", full["request_text"]]
    rewritten, approved_context = prepare_root_arguments(profile, argv)
    assert rewritten[-1] == full["request_text"]
    assert ScopeLedger(profile).claim_followup(approved_context) == claim


def test_followup_conflicting_authoritative_header_does_not_hide_behind_approved_tail(
    tmp_path, monkeypatch,
):
    profile, _, context_path, root = setup_profile(
        tmp_path, monkeypatch, followup_request_sha256=_hash("Shorten the recommendation."),
    )
    claim = activate(profile, root, monkeypatch)
    ScopeLedger(profile).finish(claim, 0)
    context = _followup_context(root)
    context["request_text"] = context["request_text"].replace(
        "Current user request (authoritative): Shorten the recommendation.",
        "Current user request (authoritative): Perform a different task.",
    )
    context_path.write_text(json.dumps(context))
    argv = ["-m", "keystone_agents.cli", "ask", "--context-file", str(context_path),
            "--live-sdk", "--json", context["request_text"]]
    with pytest.raises(CanaryPolicyError):
        prepare_root_arguments(profile, argv)
    assert ScopeLedger(profile).thread_status()["accepted_turns"] == 1


def test_followup_rejects_overlap_and_exhausted_shared_budget(tmp_path, monkeypatch):
    profile, _, _, root = setup_profile(
        tmp_path, monkeypatch, followup_request_sha256=_hash("Shorten the recommendation."),
        max_requests=1,
    )
    claim = activate(profile, root, monkeypatch)
    ledger = ScopeLedger(profile)
    with pytest.raises(CanaryPolicyError, match="not_awaiting"):
        ledger.claim_followup(_followup_context(root))
    with pytest.raises(CanaryPolicyError, match="cannot_expire"):
        ledger.expire_thread()
    admit_agent("chief_of_staff")
    guarded_response_kwargs(client(), (), kwargs())
    ledger.finish(claim, 0)
    with pytest.raises(CanaryPolicyError, match="budget_exhausted"):
        ledger.claim_followup(_followup_context(root))
    assert ledger.thread_status()["accepted_turns"] == 1
    assert dispatch_count(profile) == 1


def test_closed_thread_returns_truthful_local_harness_result(tmp_path, monkeypatch, capsys):
    import keystone_agents.canary_acceptance_entrypoint as entrypoint

    profile, _, context_path, root = setup_profile(
        tmp_path, monkeypatch, followup_request_sha256=_hash("Shorten the recommendation."),
    )
    claim = activate(profile, root, monkeypatch)
    ledger = ScopeLedger(profile)
    ledger.finish(claim, 0)
    ledger.expire_thread()
    followup = _followup_context(root)
    context_path.write_text(json.dumps(followup))
    args = ["-m", "keystone_agents.cli", "ask", "--context-file", str(context_path),
            "--live-sdk", "--json", followup["request_text"]]
    assert entrypoint.main(args) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason_code"] == "canary_thread_not_awaiting_followup"
    assert "closed" in payload["message"] and "No new model request" in payload["message"]
    assert dispatch_count(profile) == 0


def test_one_root_profiles_keep_legacy_hash_and_deny_followups(tmp_path, monkeypatch):
    profile, _, _, root = setup_profile(tmp_path, monkeypatch)
    assert profile.digest == _hash(profile.model_dump_json(exclude={"followup_request_sha256"}))
    claim = activate(profile, root, monkeypatch)
    ledger = ScopeLedger(profile)
    ledger.finish(claim, 0)
    assert ledger.thread_status()["status"] == "completed"
    with pytest.raises(CanaryPolicyError, match="not_approved"):
        ledger.claim_followup(_followup_context(root))


def test_only_one_concurrent_followup_can_claim_the_thread(tmp_path, monkeypatch):
    profile, _, _, root = setup_profile(
        tmp_path,
        monkeypatch,
        max_requests=6,
        root_max_requests=4,
        followup_max_requests=2,
        followup_request_sha256=_hash("Shorten the recommendation."),
    )
    claim = activate(profile, root, monkeypatch)
    ScopeLedger(profile).finish(claim, 0)

    def attempt():
        try:
            return ScopeLedger(profile).claim_followup(_followup_context(root))
        except CanaryPolicyError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: attempt(), range(2)))
    assert outcomes.count(claim) == outcomes.count("rejected") == 1
    assert ScopeLedger(profile).thread_status()["accepted_turns"] == 2


def test_completed_followup_cannot_replay_or_reopen_turn_budget(tmp_path, monkeypatch):
    followup_text = "Shorten the recommendation."
    profile, _, _, root = setup_profile(
        tmp_path,
        monkeypatch,
        max_requests=6,
        root_max_requests=4,
        followup_max_requests=2,
        followup_request_sha256=_hash(followup_text),
    )
    claim = activate(profile, root, monkeypatch)
    admit_agent("gmail_triage")
    guarded_response_kwargs(client(), (), kwargs())
    ledger = ScopeLedger(profile)
    ledger.finish(claim, 0)
    followup = _followup_context(root, followup_text)
    assert ledger.claim_followup(followup) == claim
    admit_agent("gmail_triage")
    guarded_response_kwargs(client(), (), kwargs())
    ledger.finish(claim, 0)
    with pytest.raises(CanaryPolicyError, match="not_awaiting"):
        ledger.claim_followup(followup)
    assert dispatch_count(profile) == 2


def test_private_two_turn_preparer_stays_disabled_and_preserves_reserve(
    tmp_path, monkeypatch, require_local_evidence,
):
    import runpy

    script = require_local_evidence(".keystone/v2/thread-continuity/prepare.py")
    prepare = runpy.run_path(str(script))["prepare"]
    base, base_path, _, _ = setup_profile(tmp_path, monkeypatch)
    followup = tmp_path / "followup.txt"
    followup.write_text("Shorten the recommendation.")
    keys_before = base.key_env_file.read_bytes()
    token_before = base.gmail_token_copy.read_bytes()
    packet = prepare(base_path, followup, base.state_dir, tmp_path / "prepared")
    prepared = AcceptanceProfile.model_validate_json(Path(packet["profile"]).read_text())
    overlay = json.loads(Path(packet["overlay"]).read_text())
    assert prepared.enabled is False
    assert prepared.followup_request_sha256 == _hash(followup.read_text())
    assert prepared.max_requests == base.max_requests
    assert prepared.required_reserve_usd == base.required_reserve_usd
    assert prepared.approved_reserve_usd == base.approved_reserve_usd
    assert prepared.balance_floor_usd == base.balance_floor_usd
    assert overlay["KNI_BUSINESS_AGENTS_DATABASE_URL"] == prepared.database_url
    assert overlay["KNI_BUSINESS_AGENTS_HISTORY_CONTEXT_ENABLED"] == "true"
    assert "OPENAI_API_KEY" not in json.dumps(overlay)
    assert base.key_env_file.read_bytes() == keys_before
    assert base.gmail_token_copy.read_bytes() == token_before
    assert not (base.state_dir / "acceptance-scope.sqlite3").exists()
    with pytest.raises(ValueError, match="artifacts exist"):
        prepare(base_path, followup, base.state_dir, tmp_path / "prepared")


def test_followup_window_expires_without_model_or_operator_state_access(
    tmp_path, monkeypatch, capsys,
):
    import keystone_agents.canary_acceptance as policy
    import keystone_agents.canary_acceptance_entrypoint as entrypoint

    profile, _, context_path, root = setup_profile(
        tmp_path, monkeypatch, followup_request_sha256=_hash("Shorten the recommendation."),
    )
    claim = activate(profile, root, monkeypatch)
    ledger = ScopeLedger(profile)
    ledger.finish(claim, 0)

    class ExpiredClock:
        @staticmethod
        def now(zone):
            from datetime import timedelta
            return profile.balance_observed_at.astimezone(zone) + timedelta(minutes=31)

    monkeypatch.setattr(policy, "datetime", ExpiredClock)
    followup = _followup_context(root)
    context_path.write_text(json.dumps(followup))
    args = ["-m", "keystone_agents.cli", "ask", "--context-file", str(context_path),
            "--live-sdk", "--json", followup["request_text"]]
    assert entrypoint.main(args) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason_code"] == "canary_balance_observation_stale"
    assert "expired" in payload["message"] and "context is retained" in payload["message"]
    assert ledger.thread_status()["approval_window_expired"] is True
    assert ledger.thread_status()["accepted_turns"] == 1
    assert dispatch_count(profile) == 0


@pytest.mark.parametrize("agent", ["chief_of_staff", "gmail_triage", "orchestrator"])
def test_mixed_model_comparison_rejects_explicit_cache_writes_before_dispatch(
    tmp_path, monkeypatch, agent
):
    profile, _, _, context = setup_profile(tmp_path, monkeypatch, model_profile="terra_luna")
    activate(profile, context, monkeypatch)
    admit_agent(agent)
    with pytest.raises(CanaryPolicyError, match="cache_write"):
        guarded_response_kwargs(client(), (), {
            **kwargs(), "model": profile.model_for_agent(agent),
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": "Synthetic", "prompt_cache_breakpoint": True}
            ]}],
        })
    assert dispatch_count(profile) == 0
