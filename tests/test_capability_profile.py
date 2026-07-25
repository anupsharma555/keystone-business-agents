from __future__ import annotations

from typing import Any

import pytest

from keystone_agents.agents import outreach_composer as outreach_module
from keystone_agents.agents.outreach_composer import (
    build_outreach_composer_agent,
    compile_outreach_request_capability_profile,
    run_outreach_composer_sdk,
)
from keystone_agents.capability_profile import (
    compile_child_result_promotion_receipt,
    compile_request_capability_profile,
)
from keystone_agents.models import OutreachComposerSDKInput, TypedAgentRunResult
from keystone_agents.schemas.execution_request import ExecutionEntrypoint

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
    assert captured["trace_metadata"]["capability_profile"] == profile


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
    profile = captured["trace_metadata"]["capability_profile"]
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
