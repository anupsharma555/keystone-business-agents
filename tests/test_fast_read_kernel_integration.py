from __future__ import annotations

import json
from typing import Any

import pytest

import keystone_agents.run as run_module
import keystone_agents.tools.gmail_tool as gmail_module
import keystone_agents.tools.internal_data_tools as workspace_module
from keystone_agents.model_provider import ModelConfig
from keystone_agents.provider_read import (
    ProviderReadPlan,
    activate_provider_read_context,
    current_provider_read_context,
)
from keystone_agents.receipts.journal import (
    reset_tool_receipt_journal,
    tool_receipt_journal,
)
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.chief_of_staff import ChiefOfStaffResult


def _plan(provider: str) -> ProviderReadPlan:
    return ProviderReadPlan(
        provider=provider,
        operation="read",
        resource="agent_request",
    )


def test_gmail_wrappers_reuse_one_request_local_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[Any] = []

    class _FakeGmailTool:
        def __init__(self, *, live: bool) -> None:
            assert live is True
            instances.append(self)

        def list_recent_messages(self, **_kwargs):
            return [{"id": "one"}]

        def get_message(self, *, message_id: str):
            return {"id": message_id}

    monkeypatch.setattr(gmail_module, "GmailTool", _FakeGmailTool)
    reset_tool_receipt_journal()

    with activate_provider_read_context(_plan("gmail")) as context:
        assert gmail_module.list_recent_messages() == [{"id": "one"}]
        assert gmail_module.get_message("one") == {"id": "one"}
        assert context is not None
        assert context.attempt_count == 2

    assert len(instances) == 1
    read_receipts = [
        receipt
        for receipt in tool_receipt_journal()
        if receipt.get("provider_read") is True
    ]
    assert len(read_receipts) == 2
    assert {receipt["provider"] for receipt in read_receipts} == {"gmail"}
    assert all(receipt["provider_write"] is False for receipt in read_receipts)
    assert '"one"' not in json.dumps(read_receipts)


def test_gmail_wrappers_keep_legacy_client_lifecycle_without_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[Any] = []

    class _FakeGmailTool:
        def __init__(self, *, live: bool) -> None:
            assert live is True
            instances.append(self)

        def list_recent_messages(self, **_kwargs):
            return []

        def get_message(self, *, message_id: str):
            return {"id": message_id}

    monkeypatch.setattr(gmail_module, "GmailTool", _FakeGmailTool)

    gmail_module.list_recent_messages()
    gmail_module.get_message("one")

    assert len(instances) == 2


def test_workspace_service_bundle_is_reused_only_in_matching_request_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_build() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"drive": object()}

    monkeypatch.setattr(
        workspace_module,
        "_build_google_workspace_services",
        fake_build,
    )

    with activate_provider_read_context(_plan("google_workspace")):
        first = workspace_module._google_workspace_services()
        second = workspace_module._google_workspace_services()

    assert first is second
    assert calls == 1
    workspace_module._google_workspace_services()
    assert calls == 2


def test_workspace_drive_read_consumes_budget_and_emits_safe_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Request:
        def execute(self) -> dict[str, Any]:
            return {
                "files": [
                    {
                        "id": "private-file-id",
                        "name": "README.doc",
                        "mimeType": "application/vnd.google-apps.document",
                    }
                ]
            }

    class _Files:
        def list(self, **_kwargs: Any) -> _Request:
            return _Request()

    class _Drive:
        def files(self) -> _Files:
            return _Files()

    monkeypatch.setattr(
        workspace_module,
        "_google_workspace_services",
        lambda: {"drive": _Drive()},
    )
    monkeypatch.setattr(
        workspace_module,
        "_assert_configured_google_account",
        lambda _service: None,
    )
    monkeypatch.setattr(
        workspace_module,
        "_find_drive_folder_path",
        lambda _service, _folder_path: "private-folder-id",
    )
    reset_tool_receipt_journal()

    with activate_provider_read_context(_plan("google_workspace")) as context:
        result = workspace_module.google_drive_list_folder_impl("", live=True)
        workspace_module.record_provider_read_result(
            "google_drive_list_folder",
            result,
        )
        assert context is not None
        assert context.attempt_count == 1

    receipt = tool_receipt_journal()[-1]
    assert receipt["provider"] == "google_workspace"
    assert receipt["provider_read"] is True
    assert receipt["provider_write"] is False
    assert receipt["item_count"] == 1
    assert receipt["completeness"] == "complete"
    assert "private-file-id" not in str(receipt)
    assert "README.doc" not in str(receipt)


def test_direct_provider_agent_activates_fast_read_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_provider = ""

    class _Agent:
        name = "gmail_triage"
        model = "gpt-test"
        instructions = "Return the typed result."
        tools: list[Any] = []
        model_settings = None
        output_type = ChiefOfStaffResult

    def fake_run(*_args, **_kwargs):
        nonlocal observed_provider
        context = current_provider_read_context()
        observed_provider = context.plan.provider if context is not None else ""
        return (
            {"fake": True},
            ChiefOfStaffResult(mode="llm", summary="Done.", audit_notes=[]),
        )

    monkeypatch.setattr(run_module, "run_typed_sdk_sync", fake_run)
    monkeypatch.setattr(
        run_module,
        "enforce_agent_run_budget",
        lambda **_kwargs: {"enforced": False},
    )

    run_typed_sdk_agent(
        agent=_Agent(),
        typed_input={"request": "read one message"},
        output_type=ChiefOfStaffResult,
        live=True,
        config=ModelConfig(provider="openai", model="gpt-test", api_key="test-key"),
    )

    assert observed_provider == "gmail"
    assert current_provider_read_context() is None
