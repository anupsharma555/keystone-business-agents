from __future__ import annotations

import asyncio
from typing import Any

import pytest

from keystone_agents.agents import outreach_composer as outreach_module
from keystone_agents.agents.chief_of_staff import build_chief_of_staff_agent
from keystone_agents.agents.outreach_composer import (
    build_outreach_composer_agent,
    compile_outreach_request_capability_profile,
    run_outreach_composer_sdk,
)
from keystone_agents.capabilities.admission import (
    compile_capability_admission,
    guard_tool_invocation,
)
from keystone_agents.capability_profile import (
    compile_child_result_promotion_receipt,
    compile_request_capability_profile,
)
from keystone_agents.contracts.completion import build_count_request_coverage
from keystone_agents.models import OutreachComposerSDKInput, TypedAgentRunResult
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.execution_request import ExecutionEntrypoint
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.tool_receipt_journal import instrument_agent_tools

ENTRYPOINTS: tuple[ExecutionEntrypoint, ...] = (
    "cli",
    "slack_root",
    "slack_followup",
    "scheduled",
    "work_item",
    "direct_sdk",
)


def _supplied_input() -> OutreachComposerSDKInput:
    return OutreachComposerSDKInput(
        company_name="Example Health",
        contact_name="Dr. Example",
        approved_context=(
            "Approved source-backed context: Example Health is evaluating one "
            "clinical workflow. Use no facts beyond this packet."
        ),
        email_style_profile="Use short, personal paragraphs.",
        outreach_goal="Ask whether comparing evaluation approaches would be useful.",
    )


def test_equivalent_entrypoints_compile_the_same_zero_tool_outreach_profile() -> None:
    profiles = [
        compile_outreach_request_capability_profile(
            _supplied_input(),
            entrypoint=entrypoint,
        )
        for entrypoint in ENTRYPOINTS
    ]

    assert {profile.entrypoint for profile in profiles} == set(ENTRYPOINTS)
    assert {profile.profile_fingerprint for profile in profiles} == {
        profiles[0].profile_fingerprint
    }
    assert {profile.tool_names for profile in profiles} == {()}
    assert {profile.tool_count for profile in profiles} == {0}
    assert {profile.prompt_profile for profile in profiles} == {"outreach_compact"}
    assert all(profile.prompt_chars > 0 for profile in profiles)
    assert all(profile.retrieval_enabled is False for profile in profiles)
    assert all(profile.write_enabled is False for profile in profiles)
    assert all(profile.send_enabled is False for profile in profiles)


def test_typed_outreach_sdk_uses_compact_zero_tool_profile_and_records_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run_typed_sdk_agent(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return TypedAgentRunResult(
            agent_name="outreach_composer",
            output=object(),
            raw_result={},
            request_cache={"tool_count": len(kwargs["agent"].tools or [])},
        )

    monkeypatch.setattr(
        outreach_module,
        "run_typed_sdk_agent",
        fake_run_typed_sdk_agent,
    )

    result = run_outreach_composer_sdk(
        _supplied_input(),
        entrypoint="slack_root",
        attach_tools=True,
    )

    assert captured["agent"].tools == []
    assert "outreach_composer.md" in str(captured["agent"].instructions)
    assert "tools.md" not in str(captured["agent"].instructions)
    profile = result.request_cache["capability_profile"]
    assert profile["entrypoint"] == "slack_root"
    assert profile["tool_names"] == []
    assert profile["tool_count"] == 0
    assert profile["retrieval_enabled"] is False
    assert profile["provider_operations"] == []
    assert profile["write_enabled"] is False
    assert profile["send_enabled"] is False
    assert profile["profile_fingerprint"]
    assert captured["capability_profile"].receipt() == profile


def test_legacy_free_form_outreach_input_keeps_existing_tool_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run_typed_sdk_agent(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return TypedAgentRunResult(
            agent_name="outreach_composer",
            output=object(),
            raw_result={},
        )

    monkeypatch.setattr(
        outreach_module,
        "run_typed_sdk_agent",
        fake_run_typed_sdk_agent,
    )

    run_outreach_composer_sdk(
        "Load the approved context for Example Health before drafting.",
        attach_tools=True,
    )

    assert len(captured["agent"].tools) > 0
    profile = captured["capability_profile"].receipt()
    assert profile["execution_shape"] == "legacy_context_acquisition"
    assert profile["tool_count"] == len(captured["agent"].tools)
    assert profile["retrieval_enabled"] is True


def test_profile_cannot_claim_provider_mutation_without_write_authority() -> None:
    agent = build_outreach_composer_agent(
        include_tools=False,
        request_text="Draft from the supplied packet only.",
        compact_instructions=True,
    )

    with pytest.raises(
        ValueError,
        match="provider mutations require write_enabled=true",
    ):
        compile_request_capability_profile(
            entrypoint="direct_sdk",
            agent=agent,
            execution_shape="supplied_context_draft",
            prompt_profile="outreach_compact",
            max_turns=1,
            provider_operations=("create",),
            write_enabled=False,
            send_enabled=False,
        )


def test_canonical_read_ceiling_blocks_attached_write_tool_before_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class FakeTool:
        name = "airtable_write_record"

    class FakeAgent:
        name = "chief_of_staff"
        model = "gpt-test"
        instructions = "Use only the admitted tools."
        tools = [FakeTool()]
        output_type = object

    def fake_run_typed_sdk_sync(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return {}, object()

    monkeypatch.setattr(
        "keystone_agents.run.run_typed_sdk_sync",
        fake_run_typed_sdk_sync,
    )

    with pytest.raises(RuntimeError, match="does not admit"):
        run_typed_sdk_agent(
            agent=FakeAgent(),
            typed_input={"request": "read the record only"},
            output_type=object,
            run_config=object(),
            provider_operations=("read", "verify"),
        )

    assert calls == 0


def test_supplied_capability_profile_must_match_runtime_tool_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_agent = build_outreach_composer_agent(
        include_tools=False,
        request_text="Draft from supplied context.",
        compact_instructions=True,
    )
    profile = compile_request_capability_profile(
        entrypoint="direct_sdk",
        agent=profile_agent,
        execution_shape="supplied_context_draft",
        prompt_profile="outreach_compact",
        max_turns=1,
    )
    runtime_agent = build_outreach_composer_agent(
        include_tools=True,
        request_text="Load approved context before drafting.",
    )
    calls = 0

    def fake_run_typed_sdk_sync(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return {}, object()

    monkeypatch.setattr(
        "keystone_agents.run.run_typed_sdk_sync",
        fake_run_typed_sdk_sync,
    )

    with pytest.raises(RuntimeError, match="tool surface does not match"):
        run_typed_sdk_agent(
            agent=runtime_agent,
            typed_input={"request": "load context"},
            output_type=object,
            run_config=object(),
            max_turns=1,
            capability_profile=profile,
        )

    assert calls == 0


def test_same_tool_profile_with_wrong_entrypoint_is_rejected_before_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = build_outreach_composer_agent(
        include_tools=False,
        request_text="Draft from supplied context.",
        compact_instructions=True,
    )
    profile = compile_request_capability_profile(
        entrypoint="slack_root",
        agent=agent,
        execution_shape="typed_sdk_run",
        prompt_profile="repo_runtime",
        max_turns=1,
        model_name="sdk-local",
    )
    calls = 0

    def fake_run(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return {}, object()

    monkeypatch.setattr("keystone_agents.run.run_typed_sdk_sync", fake_run)
    with pytest.raises(RuntimeError, match="entrypoint"):
        run_typed_sdk_agent(
            agent=agent,
            typed_input={"request": "draft"},
            output_type=object,
            run_config=object(),
            max_turns=1,
            entrypoint="work_item",
            capability_profile=profile,
        )
    assert calls == 0


def test_same_tool_profile_with_wrong_runtime_model_is_rejected_before_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = build_outreach_composer_agent(
        include_tools=False,
        request_text="Draft from supplied context.",
        compact_instructions=True,
    )
    profile = compile_request_capability_profile(
        entrypoint="direct_sdk",
        agent=agent,
        execution_shape="typed_sdk_run",
        prompt_profile="repo_runtime",
        max_turns=1,
        model_name="stale-model",
    )
    calls = 0

    def fake_run(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return {}, object()

    monkeypatch.setattr("keystone_agents.run.run_typed_sdk_sync", fake_run)
    with pytest.raises(RuntimeError, match="model_name"):
        run_typed_sdk_agent(
            agent=agent,
            typed_input={"request": "draft"},
            output_type=object,
            run_config=object(),
            max_turns=1,
            capability_profile=profile,
        )
    assert calls == 0


def test_same_tool_profile_with_stale_instructions_is_rejected_before_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_agent = type(
        "Agent",
        (),
        {
            "name": "outreach_composer",
            "model": "sdk-local",
            "instructions": "Use the approved packet.",
            "tools": [],
            "output_type": object,
        },
    )()
    runtime_agent = type(
        "Agent",
        (),
        {
            "name": "outreach_composer",
            "model": "sdk-local",
            "instructions": "Use a different and stale prompt.",
            "tools": [],
            "output_type": object,
        },
    )()
    profile = compile_request_capability_profile(
        entrypoint="direct_sdk",
        agent=expected_agent,
        execution_shape="typed_sdk_run",
        prompt_profile="repo_runtime",
        max_turns=1,
        model_name="sdk-local",
    )
    calls = 0

    def fake_run(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return {}, object()

    monkeypatch.setattr("keystone_agents.run.run_typed_sdk_sync", fake_run)
    with pytest.raises(RuntimeError, match="prompt_sha256"):
        run_typed_sdk_agent(
            agent=runtime_agent,
            typed_input={"request": "draft"},
            output_type=object,
            run_config=object(),
            max_turns=1,
            capability_profile=profile,
        )
    assert calls == 0


def test_same_tool_profile_with_stale_provider_operations_is_rejected_before_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = type(
        "Agent",
        (),
        {
            "name": "chief_of_staff",
            "model": "sdk-local",
            "instructions": "Use the bounded read tools.",
            "tools": [],
            "output_type": object,
        },
    )()
    profile = compile_request_capability_profile(
        entrypoint="direct_sdk",
        agent=agent,
        execution_shape="typed_sdk_run",
        prompt_profile="repo_runtime",
        max_turns=1,
        provider_operations=("read",),
        model_name="sdk-local",
    )
    calls = 0

    def fake_run(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return {}, object()

    monkeypatch.setattr("keystone_agents.run.run_typed_sdk_sync", fake_run)
    with pytest.raises(RuntimeError, match="provider_operations"):
        run_typed_sdk_agent(
            agent=agent,
            typed_input={"request": "search"},
            output_type=object,
            run_config=object(),
            max_turns=1,
            capability_profile=profile,
            provider_operations=("search",),
            provider_system="airtable",
        )
    assert calls == 0


@pytest.mark.parametrize(
    ("tool_name", "provider_system", "operations", "message"),
    [
        ("modify_gmail_message_state", "gmail", ("read",), "no admitted operation"),
        ("get_gmail_message", "airtable", ("read",), "belongs to gmail"),
        ("unknown_provider_tool", "gmail", ("read",), "unclassified tools"),
    ],
)
def test_authority_bound_admission_rejects_invalid_provider_tool_surfaces(
    tool_name: str,
    provider_system: str,
    operations: tuple[str, ...],
    message: str,
) -> None:
    tool = type("Tool", (), {"name": tool_name})()
    agent = type(
        "Agent",
        (),
        {"name": "gmail_triage", "tools": [tool]},
    )()

    receipt = compile_capability_admission(
        agent=agent,
        entrypoint="slack_root",
        enforcement_mode="authority_bound",
        authorized_provider_operations=operations,
        provider_system=provider_system,
        authority_source="canonical",
    )

    assert receipt.admitted is False
    assert any(message in violation for violation in receipt.violations)


def test_authority_bound_gmail_read_is_admitted() -> None:
    tool = type("Tool", (), {"name": "get_gmail_message"})()
    agent = type(
        "Agent",
        (),
        {"name": "gmail_triage", "tools": [tool]},
    )()

    receipt = compile_capability_admission(
        agent=agent,
        entrypoint="work_item",
        enforcement_mode="authority_bound",
        authorized_provider_operations=("read",),
        provider_system="gmail",
        authority_source="canonical",
    )

    assert receipt.admitted is True
    assert receipt.provider_tools[0].admitted_operations == ("read",)
    assert receipt.write_tools_attached is False
    assert receipt.write_authorized is False


def test_call_time_guard_rejects_cross_operation_airtable_invocation() -> None:
    tool = type("Tool", (), {"name": "airtable_write_record"})()
    agent = type(
        "Agent",
        (),
        {"name": "chief_of_staff", "tools": [tool]},
    )()
    receipt = compile_capability_admission(
        agent=agent,
        entrypoint="slack_root",
        enforcement_mode="authority_bound",
        authorized_provider_operations=("update",),
        provider_system="airtable",
        authority_source="canonical",
    )
    assert receipt.admitted is True

    with pytest.raises(RuntimeError, match="unauthorized operations: create"):
        guard_tool_invocation(
            receipt,
            tool_name="airtable_write_record",
            tool_input='{"operation":"create"}',
        )


def test_call_time_guard_requires_multi_operation_discriminator() -> None:
    tool = type("Tool", (), {"name": "airtable_write_record"})()
    agent = type(
        "Agent",
        (),
        {"name": "chief_of_staff", "tools": [tool]},
    )()
    receipt = compile_capability_admission(
        agent=agent,
        entrypoint="direct_sdk",
        enforcement_mode="authority_bound",
        authorized_provider_operations=("create", "update"),
        provider_system="airtable",
        authority_source="canonical",
    )

    with pytest.raises(RuntimeError, match="explicit create/update"):
        guard_tool_invocation(
            receipt,
            tool_name="airtable_write_record",
            tool_input="{}",
        )


def test_compatibility_mode_records_unknown_tool_without_claiming_authority() -> None:
    tool = type("Tool", (), {"name": "legacy_custom_tool"})()
    agent = type(
        "Agent",
        (),
        {"name": "legacy_agent", "tools": [tool]},
    )()

    receipt = compile_capability_admission(
        agent=agent,
        entrypoint="direct_sdk",
        enforcement_mode="compatibility",
    )

    assert receipt.admitted is True
    assert receipt.enforcement_mode == "compatibility"
    assert receipt.unknown_tool_names == ("legacy_custom_tool",)
    assert receipt.authorized_provider_operations == ()


def test_authority_bound_duplicate_tool_names_are_rejected() -> None:
    first = type("Tool", (), {"name": "get_gmail_message"})()
    second = type("Tool", (), {"name": "get_gmail_message"})()
    agent = type(
        "Agent",
        (),
        {"name": "gmail_triage", "tools": [first, second]},
    )()

    receipt = compile_capability_admission(
        agent=agent,
        entrypoint="direct_sdk",
        enforcement_mode="authority_bound",
        authorized_provider_operations=("read",),
        provider_system="gmail",
        authority_source="canonical",
    )

    assert receipt.admitted is False
    assert receipt.duplicate_tool_names == ("get_gmail_message",)


def test_reused_instrumented_tool_uses_current_run_admission() -> None:
    invocations = 0

    async def original(_context: Any, _tool_input: str) -> str:
        nonlocal invocations
        invocations += 1
        return '{"status":"success","operation":"create","record_id":"rec-1"}'

    tool = type(
        "Tool",
        (),
        {
            "name": "airtable_write_record",
            "on_invoke_tool": staticmethod(original),
        },
    )()
    agent = type(
        "Agent",
        (),
        {"name": "chief_of_staff", "tools": [tool]},
    )()
    update_only = compile_capability_admission(
        agent=agent,
        entrypoint="direct_sdk",
        enforcement_mode="authority_bound",
        authorized_provider_operations=("update",),
        provider_system="airtable",
        authority_source="canonical",
    )
    create_only = compile_capability_admission(
        agent=agent,
        entrypoint="direct_sdk",
        enforcement_mode="authority_bound",
        authorized_provider_operations=("create",),
        provider_system="airtable",
        authority_source="canonical",
    )
    instrument_agent_tools(agent, capability_admission=update_only)
    instrument_agent_tools(agent, capability_admission=create_only)

    result = asyncio.run(
        tool.on_invoke_tool(object(), '{"operation":"create","fields_json":"{}"}')
    )

    assert '"status":"success"' in result
    assert invocations == 1


def test_concurrent_instrumented_tool_runs_keep_request_scoped_authority() -> None:
    invocations: list[str] = []

    async def original(_context: Any, tool_input: str) -> str:
        await asyncio.sleep(0)
        invocations.append(tool_input)
        return '{"status":"success","record_id":"rec-1"}'

    tool = type(
        "Tool",
        (),
        {
            "name": "airtable_write_record",
            "on_invoke_tool": staticmethod(original),
        },
    )()
    agent = type("Agent", (), {"name": "chief_of_staff", "tools": [tool]})()
    update_only = compile_capability_admission(
        agent=agent,
        entrypoint="direct_sdk",
        enforcement_mode="authority_bound",
        authorized_provider_operations=("update",),
        provider_system="airtable",
        authority_source="canonical",
    )
    create_only = compile_capability_admission(
        agent=agent,
        entrypoint="direct_sdk",
        enforcement_mode="authority_bound",
        authorized_provider_operations=("create",),
        provider_system="airtable",
        authority_source="canonical",
    )

    async def invoke(
        receipt: Any,
        operation: str,
    ) -> str:
        instrument_agent_tools(agent, capability_admission=receipt)
        await asyncio.sleep(0)
        return await tool.on_invoke_tool(
            object(),
            f'{{"operation":"{operation}","fields_json":"{{}}"}}',
        )

    async def run_both() -> list[str]:
        return list(
            await asyncio.gather(
                invoke(update_only, "update"),
                invoke(create_only, "create"),
            )
        )

    results = asyncio.run(run_both())

    assert len(results) == 2
    assert len(invocations) == 2


@pytest.mark.parametrize(
    ("provider_system", "operations", "request_text", "expected_tools"),
    [
        (
            "google_calendar",
            ("create",),
            "Create the named calendar event.",
            {"read_google_calendar_window", "create_google_calendar_event"},
        ),
        (
            "airtable",
            ("update",),
            "Update the exact Airtable record.",
            {"airtable_get_base_schema", "airtable_read_records", "airtable_write_record"},
        ),
        (
            "google_workspace",
            ("create",),
            "Create the requested internal Workspace artifact.",
            {
                "google_drive_search_files",
                "google_doc_write",
                "presentation_extract_slide_copy_local",
            },
        ),
    ],
)
def test_canonical_chief_mutations_admit_safe_provider_read_prerequisites(
    provider_system: str,
    operations: tuple[str, ...],
    request_text: str,
    expected_tools: set[str],
) -> None:
    plan = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="chief_of_staff",
        intent="business_system_write",
        target_type="business_system_context",
        provider_system=provider_system,
        provider_operations=list(operations),
        objective=request_text,
        primary_target="exact provider object",
    )
    agent = build_chief_of_staff_agent(
        request_text=request_text,
        manual_request_plan=plan,
        include_specialist_tools=False,
    )
    tool_names = {getattr(tool, "name", "") for tool in agent.tools}

    assert expected_tools <= tool_names
    receipt = compile_capability_admission(
        agent=agent,
        entrypoint="slack_root",
        enforcement_mode="authority_bound",
        authorized_provider_operations=operations,
        provider_system=provider_system,
        authority_source="llm",
    )

    assert receipt.admitted is True
    assert receipt.authorized_provider_operations == operations
    assert all(
        provider_tool.admitted_operations
        for provider_tool in receipt.provider_tools
    )


@pytest.mark.parametrize(
    ("tool_name", "provider_system"),
    [
        ("gmail_test_draft_lifecycle", "gmail"),
        ("google_doc_test_lifecycle", "google_workspace"),
        ("zotero_test_note_lifecycle", "zotero"),
    ],
)
def test_true_lifecycle_tools_require_full_authority_without_fake_selector(
    tool_name: str,
    provider_system: str,
) -> None:
    tool = type("Tool", (), {"name": tool_name})()
    agent = type("Agent", (), {"name": "lifecycle_agent", "tools": [tool]})()
    receipt = compile_capability_admission(
        agent=agent,
        entrypoint="direct_sdk",
        enforcement_mode="authority_bound",
        authorized_provider_operations=("create", "update", "delete", "verify"),
        provider_system=provider_system,
        authority_source="canonical",
    )

    assert receipt.admitted is True
    guard_tool_invocation(receipt, tool_name=tool_name, tool_input="{}")


def test_typed_sdk_admission_uses_canonical_semantic_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = type("Tool", (), {"name": "get_gmail_message"})()
    agent = type(
        "Agent",
        (),
        {
            "name": "gmail_triage",
            "model": "gpt-test",
            "instructions": "Read only the selected Gmail message.",
            "tools": [tool],
            "output_type": object,
        },
    )()
    plan = ManualRequestPlan(
        source="llm",
        requested_agent="gmail_triage",
        target_agent="gmail_triage",
        intent="gmail_triage",
        target_type="gmail_thread",
        provider_system="gmail",
        provider_operations=["read"],
        objective="Read the selected Gmail message.",
    )
    monkeypatch.setattr(
        "keystone_agents.run.run_typed_sdk_sync",
        lambda *_args, **_kwargs: ({}, object()),
    )

    result = run_typed_sdk_agent(
        agent=agent,
        typed_input={"request": plan.objective},
        output_type=object,
        run_config=object(),
        capability_authority=plan,
    )

    admission = result.request_cache["capability_admission"]
    assert admission["enforcement_mode"] == "authority_bound"
    assert admission["authority_source"] == "llm"
    assert admission["provider_system"] == "gmail"
    assert admission["authorized_provider_operations"] == ["read"]


def test_typed_sdk_rejects_provider_conflicting_with_canonical_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    agent = type(
        "Agent",
        (),
        {
            "name": "gmail_triage",
            "model": "gpt-test",
            "instructions": "Read only the selected Gmail message.",
            "tools": [],
            "output_type": object,
        },
    )()
    plan = ManualRequestPlan(
        source="llm",
        requested_agent="gmail_triage",
        target_agent="gmail_triage",
        intent="gmail_triage",
        target_type="gmail_thread",
        provider_system="gmail",
        provider_operations=["read"],
        objective="Read the selected Gmail message.",
    )

    def fake_run(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], object]:
        nonlocal calls
        calls += 1
        return {}, object()

    monkeypatch.setattr("keystone_agents.run.run_typed_sdk_sync", fake_run)

    with pytest.raises(RuntimeError, match="provider system conflicts"):
        run_typed_sdk_agent(
            agent=agent,
            typed_input={"request": plan.objective},
            output_type=object,
            run_config=object(),
            capability_authority=plan,
            provider_system="airtable",
        )

    assert calls == 0


def test_verified_child_public_result_compiles_reader_promotion_receipt() -> None:
    receipt = compile_child_result_promotion_receipt(
        {
            "status": "completed",
            "output_type": "GmailMessageCountResult",
            "send_enabled": False,
            "user_facing_result_verified": True,
            "public_result": {
                "status": "completed",
                "completion_confirmed": True,
                "provider_write_attempted": False,
            },
            "request_cache": {
                "capability_profile": {"profile_fingerprint": "profile-123"}
            },
        },
        summary="You received 7 emails today.",
    )

    assert receipt.reader_ready is True
    assert receipt.verification_basis == ("verified_child_public_result",)
    assert receipt.capability_profile_fingerprint == "profile-123"
    assert receipt.provider_write_attempted is False
    assert receipt.send_enabled is False


def test_unverified_provider_write_cannot_compile_reader_promotion() -> None:
    receipt = compile_child_result_promotion_receipt(
        {
            "status": "completed",
            "send_enabled": False,
            "user_facing_result_verified": True,
            "public_result": {
                "status": "completed",
                "completion_confirmed": True,
                "provider_write_attempted": True,
                "provider_receipt_verified": False,
            },
        },
        summary="The provider record was updated.",
    )

    assert receipt.reader_ready is False
    assert receipt.provider_write_attempted is True
    assert receipt.provider_receipt_verified is False


def test_unverified_legacy_summary_cannot_compile_reader_promotion() -> None:
    receipt = compile_child_result_promotion_receipt(
        {
            "status": "completed",
            "output_type": "LegacySpecialistResult",
            "send_enabled": False,
            "output": {"summary": "A plausible but unverified specialist summary."},
        },
        summary="A plausible but unverified specialist summary.",
    )

    assert receipt.reader_ready is False
    assert receipt.verification_basis == ()
    assert receipt.child_public_result_verified is False
    assert receipt.instruction_repair_verified is False
    assert receipt.typed_display_verified is False
    assert receipt.rendered_display_verified is False


@pytest.mark.parametrize("status", ["partial", "canceled", "in_progress", "unknown"])
def test_non_success_child_status_cannot_compile_reader_promotion(status: str) -> None:
    receipt = compile_child_result_promotion_receipt(
        {
            "status": status,
            "output_type": "OpportunityScoutResult",
            "send_enabled": False,
        },
        summary="A readable but incomplete result.",
        typed_display_verified=True,
    )

    assert receipt.reader_ready is False


def test_host_underfill_blocks_child_reader_promotion() -> None:
    coverage = build_count_request_coverage(
        interpreted_request="Return exactly three opportunities.",
        expected_count=3,
        observed_count=1,
        item_label="opportunities",
        next_safe_action="Continue bounded research.",
        count_mode="exact",
    )

    receipt = compile_child_result_promotion_receipt(
        {
            "status": "completed",
            "output_type": "OpportunityScoutResult",
            "send_enabled": False,
        },
        summary="One source-backed opportunity is ready.",
        typed_display_verified=True,
        host_request_coverage=coverage,
    )

    assert receipt.reader_ready is False
