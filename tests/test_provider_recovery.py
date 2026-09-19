from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from keystone_agents.provider_recovery import (
    ProviderPartialSuccessError,
    ProviderRecoveryError,
    ProviderRecoveryStore,
)
from keystone_agents.receipts.normalization import normalize_tool_output_receipt
from keystone_agents.run import run_typed_sdk_agent


def _verified_create_receipt() -> dict[str, Any]:
    return {
        "status": "success",
        "operation": "create_record",
        "provider": "fixture",
        "record_id": "rec_fixture_001",
        "provider_link": "https://provider.example.test/rec_fixture_001",
        "verification": {"passed": True, "status": "verified_present"},
    }


@pytest.mark.parametrize("failed_stage", ["attachment", "provider_verification", "rendering"])
def test_partial_success_reuses_verified_object_after_downstream_failure(
    tmp_path: Path,
    failed_stage: str,
) -> None:
    checkpoint = tmp_path / f"{failed_stage}.json"
    create_calls = 0

    def create_record() -> dict[str, Any]:
        nonlocal create_calls
        create_calls += 1
        return _verified_create_receipt()

    first = ProviderRecoveryStore(
        checkpoint,
        idempotency_key=f"fixture-{failed_stage}",
    )
    created = first.reuse_or_execute_mutation(
        tool_name="airtable_create_record",
        operation="create_record",
        execute=create_record,
    )

    with pytest.raises(ProviderPartialSuccessError) as raised:
        first.run_downstream_stage(
            failed_stage,
            lambda: (_ for _ in ()).throw(RuntimeError(f"{failed_stage} failed")),
        )

    assert created["record_id"] == "rec_fixture_001"
    assert raised.value.partial_success.status == "partial_success"
    assert raised.value.partial_success.failed_stage == failed_stage
    assert raised.value.partial_success.receipts[0].object_id == "rec_fixture_001"

    resumed = ProviderRecoveryStore(
        checkpoint,
        idempotency_key=f"fixture-{failed_stage}",
    )
    reused = resumed.reuse_or_execute_mutation(
        tool_name="airtable_create_record",
        operation="create_record",
        execute=create_record,
    )
    resumed.run_downstream_stage(failed_stage, lambda: "verified")
    completed = resumed.mark_completed()

    assert reused["record_id"] == "rec_fixture_001"
    assert create_calls == 1
    assert completed.status == "completed"
    assert completed.retry_reused is True
    assert failed_stage in completed.completed_stages


@pytest.mark.parametrize(
    "receipt",
    [
        {
            **_verified_create_receipt(),
            "verification": {"passed": False},
        },
        {
            **_verified_create_receipt(),
            "record_id": "",
        },
        {
            **_verified_create_receipt(),
            "status": "blocked",
        },
    ],
)
def test_checkpoint_rejects_unverified_or_identity_free_mutation(
    tmp_path: Path,
    receipt: dict[str, Any],
) -> None:
    checkpoint = tmp_path / "recovery.json"
    store = ProviderRecoveryStore(checkpoint, idempotency_key="fixture-unverified")

    with pytest.raises(ProviderRecoveryError, match="provider-verified"):
        store.record_receipt({**receipt, "tool_name": "airtable_create_record"})

    assert checkpoint.exists() is False


def test_checkpoint_checksum_and_idempotency_key_are_verified(tmp_path: Path) -> None:
    checkpoint = tmp_path / "recovery.json"
    store = ProviderRecoveryStore(checkpoint, idempotency_key="fixture-checksum")
    store.record_receipt(
        {**_verified_create_receipt(), "tool_name": "airtable_create_record"}
    )
    wrapper = json.loads(checkpoint.read_text(encoding="utf-8"))
    wrapper["payload"]["receipts"][0]["object_id"] = "rec_tampered"
    checkpoint.write_text(json.dumps(wrapper), encoding="utf-8")

    with pytest.raises(ProviderRecoveryError, match="checksum mismatch"):
        ProviderRecoveryStore(checkpoint, idempotency_key="fixture-checksum")


@pytest.mark.parametrize(
    ("tool_name", "operation", "identity_key", "identity_value", "receipt"),
    [
        (
            "zotero_write_test_item",
            "create",
            "item_key",
            "ITEMKBA1",
            {
                "status": "success",
                "operation": "create",
                "item_key": "ITEMKBA1",
                "collection_key": "COLLKBA1",
                "title": "KBA_TEST_ITEM fixture",
                "approval_reference": "approval:item",
                "verification": {"passed": True, "status": "verified"},
            },
        ),
        (
            "zotero_write_test_collection",
            "create",
            "collection_key",
            "COLLKBA1",
            {
                "status": "success",
                "operation": "create",
                "collection_key": "COLLKBA1",
                "name": "KBA_TEST_COLLECTION fixture",
                "approval_reference": "approval:collection",
                "verification": {"passed": True, "status": "verified"},
            },
        ),
        (
            "google_sheet_create",
            "create_sheet",
            "spreadsheet_id",
            "sheetKBA1",
            {
                "status": "success",
                "operation": "create_sheet",
                "spreadsheet_id": "sheetKBA1",
                "title": "KBA_TEST_SHEET fixture",
                "approval_reference": "approval:sheet",
                "verification": {"passed": True, "status": "verified"},
            },
        ),
        (
            "google_drive_create_folder",
            "create_folder",
            "folder_id",
            "folderKBA1",
            {
                "status": "success",
                "folder_id": "folderKBA1",
                "approval_reference": "approval:folder",
                "verification": {"passed": True, "status": "verified"},
            },
        ),
    ],
)
def test_provider_neutral_identity_receipts_checkpoint_reload_and_reuse(
    tmp_path: Path,
    tool_name: str,
    operation: str,
    identity_key: str,
    identity_value: str,
    receipt: dict[str, Any],
) -> None:
    checkpoint = tmp_path / f"{identity_key}.json"
    store = ProviderRecoveryStore(
        checkpoint,
        idempotency_key=f"fixture-{identity_key}",
    )

    stored = store.reuse_or_execute_mutation(
        tool_name=tool_name,
        operation=operation,
        execute=lambda: receipt,
    )
    resumed = ProviderRecoveryStore(
        checkpoint,
        idempotency_key=f"fixture-{identity_key}",
    )
    execute_calls = 0

    def repeat_mutation() -> dict[str, Any]:
        nonlocal execute_calls
        execute_calls += 1
        return receipt

    reused = resumed.reuse_or_execute_mutation(
        tool_name=tool_name,
        operation=operation,
        execute=repeat_mutation,
    )
    journal_receipt = normalize_tool_output_receipt(tool_name, receipt)

    assert stored[identity_key] == identity_value
    assert journal_receipt is not None
    assert journal_receipt[identity_key] == identity_value
    assert resumed.state.receipts[0].object_id == identity_value
    assert resumed.state.receipts[0].payload[identity_key] == identity_value
    assert reused[identity_key] == identity_value
    assert execute_calls == 0
    assert resumed.state.retry_reused is True


@pytest.mark.parametrize(
    ("tool_name", "operation", "wrong_identity"),
    [
        (
            "zotero_write_test_item",
            "create",
            {"collection_key": "COLLKBA1"},
        ),
        (
            "google_sheet_create",
            "create_sheet",
            {"folder_id": "folderKBA1"},
        ),
    ],
)
def test_provider_recovery_rejects_identity_from_another_object_family(
    tmp_path: Path,
    tool_name: str,
    operation: str,
    wrong_identity: dict[str, str],
) -> None:
    store = ProviderRecoveryStore(
        tmp_path / "mismatched-identity.json",
        idempotency_key=f"fixture-{tool_name}",
    )

    with pytest.raises(ProviderRecoveryError, match="identity-bearing"):
        store.reuse_or_execute_mutation(
            tool_name=tool_name,
            operation=operation,
            execute=lambda: {
                "status": "success",
                "operation": operation,
                **wrong_identity,
                "approval_reference": "approval:mismatch",
                "verification": {"passed": True},
            },
        )

    assert store.path.exists() is False


def test_sdk_retry_loads_checkpoint_and_disables_only_completed_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = tmp_path / "sdk-recovery.json"
    create_calls = 0

    def create_tool_output(_context: Any, _tool_input: str) -> dict[str, Any]:
        nonlocal create_calls
        create_calls += 1
        return _verified_create_receipt()

    create_tool = SimpleNamespace(
        name="airtable_create_record",
        is_enabled=True,
        on_invoke_tool=create_tool_output,
    )
    read_tool = SimpleNamespace(
        name="airtable_get_record",
        is_enabled=True,
        on_invoke_tool=lambda _context, _input: {"status": "success"},
    )
    compensate_tool = SimpleNamespace(
        name="airtable_delete_record",
        is_enabled=True,
        on_invoke_tool=lambda _context, _input: {"status": "dry-run"},
    )
    agent = SimpleNamespace(
        name="fixture_agent",
        model="fixture-model",
        tools=[create_tool, read_tool, compensate_tool],
    )
    first_attempt = True

    def fake_run_typed_sdk_sync(
        _agent: Any,
        prompt: Any,
        _output_type: Any,
        **_kwargs: Any,
    ) -> tuple[Any, dict[str, Any]]:
        nonlocal first_attempt
        active_tools = {tool.name: tool for tool in _agent.tools}
        active_create_tool = active_tools["airtable_create_record"]
        active_read_tool = active_tools["airtable_get_record"]
        active_compensate_tool = active_tools["airtable_delete_record"]
        if first_attempt:
            first_attempt = False
            assert active_create_tool.is_enabled is True
            asyncio.run(active_create_tool.on_invoke_tool(None, "{}"))
            raise RuntimeError("attachment read-back failed after provider mutation")
        assert active_create_tool.is_enabled is False
        assert active_read_tool.is_enabled is True
        assert active_compensate_tool.is_enabled is True
        assert "Provider receipts:" in str(prompt)
        return SimpleNamespace(usage=None), {"status": "recovered"}

    monkeypatch.setattr(
        "keystone_agents.run.run_typed_sdk_sync",
        fake_run_typed_sdk_sync,
    )
    run_config = SimpleNamespace(model="fixture-model")
    first_store = ProviderRecoveryStore(
        checkpoint,
        idempotency_key="fixture-sdk-retry",
    )

    with pytest.raises(ProviderPartialSuccessError) as raised:
        run_typed_sdk_agent(
            agent=agent,
            typed_input={"request": "create then attach"},
            output_type=dict,
            run_config=run_config,
            recovery_store=first_store,
        )

    assert raised.value.partial_success.failed_stage == "attachment"
    assert create_calls == 1

    resumed_store = ProviderRecoveryStore(
        checkpoint,
        idempotency_key="fixture-sdk-retry",
    )
    result = run_typed_sdk_agent(
        agent=agent,
        typed_input={"request": "create then attach"},
        output_type=dict,
        run_config=run_config,
        recovery_store=resumed_store,
    )

    assert result.output == {"status": "recovered"}
    assert create_calls == 1
    assert create_tool.is_enabled is True
    assert read_tool.is_enabled is True
    assert compensate_tool.is_enabled is True
