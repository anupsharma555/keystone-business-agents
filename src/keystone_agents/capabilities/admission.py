"""Authority-bound admission for provider tools attached to SDK agents."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from keystone_agents.agent_tool_policy import (
    AIRTABLE_READ_ALLOWED_TOOLS,
    AIRTABLE_TEST_CLEANUP_TOOLS,
    AIRTABLE_TEST_LIFECYCLE_TOOLS,
    CALENDAR_READ_TOOL_NAMES,
    GOOGLE_WORKSPACE_READ_TOOLS,
    ToolTier,
    tool_name_for_policy,
    tool_tier_for_name,
)
from keystone_agents.schemas.execution_request import ExecutionEntrypoint
from keystone_agents.tools.zotero_context_tools import (
    ZOTERO_IMPORT_TOOL_NAMES,
    ZOTERO_READ_CONTEXT_TOOL_NAMES,
)

CapabilityEnforcementMode = Literal["compatibility", "authority_bound"]
_MUTATION_OPERATIONS = frozenset({"create", "update", "delete", "attach"})


class ProviderToolAdmission(BaseModel):
    """Exact provider-operation contract for one attached tool."""

    model_config = ConfigDict(frozen=True)

    tool_name: str
    provider_system: str
    supported_operations: tuple[str, ...]
    admitted_operations: tuple[str, ...]
    operation_selector: str = ""
    send_required: bool = False


class CapabilityAdmissionReceipt(BaseModel):
    """Bounded proof of the capability ceiling applied before one SDK run."""

    model_config = ConfigDict(frozen=True)

    schema_name: str = "keystone.capability_admission.v1"
    enforcement_mode: CapabilityEnforcementMode
    authority_source: str = ""
    entrypoint: ExecutionEntrypoint
    agent_name: str
    attached_tool_names: tuple[str, ...]
    provider_system: str = ""
    authorized_provider_operations: tuple[str, ...] = ()
    provider_tools: tuple[ProviderToolAdmission, ...] = ()
    unknown_tool_names: tuple[str, ...] = ()
    duplicate_tool_names: tuple[str, ...] = ()
    write_tools_attached: bool = False
    write_authorized: bool = False
    send_authorized: bool = False
    violations: tuple[str, ...] = ()
    admitted: bool = True
    effective_profile_fingerprint: str = ""

    def receipt(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class _ProviderToolContract(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider_system: str
    supported_operations: tuple[str, ...]
    operation_selector: str = ""
    send_required: bool = False


def _contracts() -> dict[str, _ProviderToolContract]:
    contracts: dict[str, _ProviderToolContract] = {}

    def add(
        names: Sequence[str] | frozenset[str],
        provider: str,
        operations: tuple[str, ...],
        *,
        selector: str = "",
        send_required: bool = False,
    ) -> None:
        for name in names:
            contracts[name] = _ProviderToolContract(
                provider_system=provider,
                supported_operations=operations,
                operation_selector=selector,
                send_required=send_required,
            )

    add(AIRTABLE_READ_ALLOWED_TOOLS, "airtable", ("read", "search", "verify"))
    add(
        {"airtable_write_record"},
        "airtable",
        ("create", "update"),
        selector="airtable_operation",
    )
    add(
        {"airtable_upload_attachment", "airtable_link_attachment"},
        "airtable",
        ("attach",),
    )
    add(
        {"airtable_create_expense_from_receipt"},
        "airtable",
        ("create", "attach"),
    )
    add(
        {"airtable_reconcile_duplicate_expense"},
        "airtable",
        ("update", "delete", "verify"),
    )
    add(AIRTABLE_TEST_CLEANUP_TOOLS, "airtable", ("delete", "verify"))
    add(
        AIRTABLE_TEST_LIFECYCLE_TOOLS,
        "airtable",
        ("create", "update", "delete", "verify"),
    )

    add(GOOGLE_WORKSPACE_READ_TOOLS, "google_workspace", ("read", "search"))
    add(
        {
            "google_drive_create_folder",
            "google_sheet_create",
            "google_sheet_create_tab",
        },
        "google_workspace",
        ("create",),
    )
    add(
        {
            "google_drive_rename_folder",
            "google_sheet_append_rows",
            "google_sheet_update_row",
            "google_sheet_update_tab",
        },
        "google_workspace",
        ("update",),
    )
    add(
        {
            "google_doc_trash",
            "google_drive_remove_folder",
            "google_sheet_delete_rows",
            "google_sheet_remove_tab",
            "google_sheet_trash",
            "presentation_delete_test_artifact_local",
        },
        "google_workspace",
        ("delete",),
    )
    add(
        {"presentation_extract_slide_copy_local"},
        "google_workspace",
        ("create",),
    )
    add(
        {"google_doc_write"},
        "google_workspace",
        ("create", "update"),
        selector="workspace_document_upsert",
    )
    add(
        {"google_doc_test_lifecycle"},
        "google_workspace",
        ("create", "update", "delete", "verify"),
    )

    add(ZOTERO_READ_CONTEXT_TOOL_NAMES, "zotero", ("read", "search", "verify"))
    add(ZOTERO_IMPORT_TOOL_NAMES, "zotero", ("create", "attach"))
    add(
        {
            "zotero_write_test_note",
            "zotero_write_test_collection",
            "zotero_write_test_item",
        },
        "zotero",
        ("create", "update"),
        selector="zotero_write_operation",
    )
    add(
        {
            "zotero_delete_test_note",
            "zotero_delete_test_collection",
            "zotero_delete_test_item",
        },
        "zotero",
        ("delete", "verify"),
    )
    add(
        {"zotero_test_note_lifecycle"},
        "zotero",
        ("create", "update", "delete", "verify"),
    )

    add({"get_gmail_message"}, "gmail", ("read",))
    add({"apply_gmail_labels"}, "gmail", ("update",))
    add(
        {"modify_gmail_message_state"},
        "gmail",
        ("update", "delete"),
        selector="gmail_state_operation",
    )
    add({"create_gmail_draft_reply"}, "gmail", ("create",))
    add(
        {"create_gmail_draft_with_attachment"},
        "gmail",
        ("create", "update", "attach"),
        selector="gmail_draft_upsert",
    )
    add(
        {"send_gmail_test_draft"},
        "gmail",
        ("update",),
        send_required=True,
    )
    add(
        {"gmail_test_draft_lifecycle"},
        "gmail",
        ("create", "update", "delete", "verify"),
    )

    add(CALENDAR_READ_TOOL_NAMES, "google_calendar", ("read",))
    for operation, name in {
        "create": "create_google_calendar_event",
        "update": "update_google_calendar_event",
        "delete": "delete_google_calendar_event",
    }.items():
        add({name}, "google_calendar", (operation,))

    return contracts


_PROVIDER_TOOL_CONTRACTS = _contracts()


def verify_expected_capability_profile(expected: Any, actual: Any) -> None:
    """Reject every mismatch between a caller expectation and runtime truth."""

    expected_payload = expected.model_dump(mode="json")
    actual_payload = actual.model_dump(mode="json")
    mismatched = sorted(
        key
        for key in set(expected_payload) | set(actual_payload)
        if expected_payload.get(key) != actual_payload.get(key)
    )
    if mismatched:
        mismatch_label = (
            "Capability profile tool surface does not match runtime truth"
            if "tool_names" in mismatched
            else "Capability profile does not match runtime truth"
        )
        raise RuntimeError(
            mismatch_label
            + "; mismatched fields: "
            + ", ".join(mismatched)
        )


def compile_capability_admission(
    *,
    agent: Any,
    entrypoint: ExecutionEntrypoint,
    enforcement_mode: CapabilityEnforcementMode,
    authorized_provider_operations: Sequence[str] = (),
    provider_system: str = "",
    authority_source: str = "",
    send_enabled: bool = False,
    effective_profile_fingerprint: str = "",
) -> CapabilityAdmissionReceipt:
    """Compile provider-neutral admission evidence from actual attached tools."""

    attached = tuple(
        tool_name_for_policy(tool).strip()
        for tool in list(getattr(agent, "tools", []) or [])
        if tool_name_for_policy(tool).strip()
    )
    normalized_operations = tuple(
        dict.fromkeys(
            str(operation or "").strip().lower()
            for operation in authorized_provider_operations
            if str(operation or "").strip()
        )
    )
    duplicates = tuple(
        dict.fromkeys(name for name in attached if attached.count(name) > 1)
    )
    unknown = tuple(
        dict.fromkeys(name for name in attached if tool_tier_for_name(name) is None)
    )
    write_tools_attached = any(
        (tier := tool_tier_for_name(name)) is not None
        and tier >= ToolTier.INTERNAL_WRITE
        for name in attached
    )
    violations: list[str] = []
    provider_tools: list[ProviderToolAdmission] = []
    if enforcement_mode == "authority_bound":
        if duplicates:
            violations.append(
                "duplicate attached tool names: " + ", ".join(duplicates)
            )
        if unknown:
            violations.append(
                "unclassified tools under authority: " + ", ".join(unknown)
            )
        authorized = set(normalized_operations)
        for name in dict.fromkeys(attached):
            tier = tool_tier_for_name(name)
            contract = _PROVIDER_TOOL_CONTRACTS.get(name)
            if contract is None:
                if tier is not None and tier >= ToolTier.INTERNAL_WRITE:
                    violations.append(
                        f"write tool {name} lacks an exact provider-operation contract"
                    )
                continue
            if contract.provider_system != provider_system:
                violations.append(
                    f"provider tool {name} belongs to {contract.provider_system}, "
                    f"not {provider_system or 'unspecified'}"
                )
            safe_prerequisite = bool(
                set(contract.supported_operations) <= {"read", "search", "verify"}
                and _MUTATION_OPERATIONS.intersection(authorized)
            )
            admitted = (
                contract.supported_operations
                if safe_prerequisite
                else tuple(
                    operation
                    for operation in contract.supported_operations
                    if operation in authorized
                )
            )
            if (
                "verify" in contract.supported_operations
                and _MUTATION_OPERATIONS.intersection(admitted)
                and "verify" not in admitted
            ):
                admitted = tuple(
                    operation
                    for operation in contract.supported_operations
                    if operation in {*admitted, "verify"}
                )
            if not admitted:
                violations.append(
                    f"provider tool {name} has no admitted operation"
                )
            elif (
                len(contract.supported_operations) > 1
                and not contract.operation_selector
                and tier is not None
                and tier >= ToolTier.INTERNAL_WRITE
                and not set(contract.supported_operations).issubset(admitted)
            ):
                violations.append(
                    f"multi-operation tool {name} requires all supported operations"
                )
            if contract.send_required and not send_enabled:
                violations.append(f"send tool {name} requires send authority")
            provider_tools.append(
                ProviderToolAdmission(
                    tool_name=name,
                    provider_system=contract.provider_system,
                    supported_operations=contract.supported_operations,
                    admitted_operations=admitted,
                    operation_selector=contract.operation_selector,
                    send_required=contract.send_required,
                )
            )

    return CapabilityAdmissionReceipt(
        enforcement_mode=enforcement_mode,
        authority_source=authority_source,
        entrypoint=entrypoint,
        agent_name=str(getattr(agent, "name", "") or "").strip(),
        attached_tool_names=attached,
        provider_system=str(provider_system or "").strip(),
        authorized_provider_operations=normalized_operations,
        provider_tools=tuple(provider_tools),
        unknown_tool_names=unknown,
        duplicate_tool_names=duplicates,
        write_tools_attached=write_tools_attached,
        write_authorized=bool(_MUTATION_OPERATIONS.intersection(normalized_operations)),
        send_authorized=bool(send_enabled),
        violations=tuple(dict.fromkeys(violations)),
        admitted=not violations,
        effective_profile_fingerprint=effective_profile_fingerprint,
    )


def guard_tool_invocation(
    receipt: CapabilityAdmissionReceipt | Mapping[str, Any] | None,
    *,
    tool_name: str,
    tool_input: str | Mapping[str, Any],
) -> None:
    """Fail before a provider call when its requested operation exceeds authority."""

    if receipt is None:
        return
    admission = (
        receipt
        if isinstance(receipt, CapabilityAdmissionReceipt)
        else CapabilityAdmissionReceipt.model_validate(receipt)
    )
    if admission.enforcement_mode != "authority_bound":
        return
    matching = next(
        (item for item in admission.provider_tools if item.tool_name == tool_name),
        None,
    )
    if matching is None:
        tier = tool_tier_for_name(tool_name)
        if tier is None or tier >= ToolTier.INTERNAL_WRITE:
            raise RuntimeError(
                f"Tool {tool_name} is not admitted by the request capability authority."
            )
        return
    payload = _tool_input_mapping(tool_input)
    required = _required_operations(matching, payload)
    admitted = set(matching.admitted_operations)
    missing = sorted(set(required) - admitted)
    if missing:
        raise RuntimeError(
            f"Tool {tool_name} requested unauthorized operations: {', '.join(missing)}"
        )
    if matching.send_required and not admission.send_authorized:
        raise RuntimeError(f"Tool {tool_name} requires send authority.")


def _tool_input_mapping(value: str | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    try:
        decoded = json.loads(str(value or ""))
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, Mapping) else {}


def _required_operations(
    admission: ProviderToolAdmission,
    payload: Mapping[str, Any],
) -> tuple[str, ...]:
    selector = admission.operation_selector
    if selector == "airtable_operation":
        operation = str(payload.get("operation") or "").strip().lower()
        if operation not in {"create", "update"}:
            raise RuntimeError(
                f"Tool {admission.tool_name} requires an explicit create/update operation."
            )
        return (operation,)
    if selector == "gmail_state_operation":
        operation = str(payload.get("operation") or "").strip().lower()
        if not operation:
            raise RuntimeError(
                f"Tool {admission.tool_name} requires an explicit mailbox operation."
            )
        return ("delete",) if operation == "trash" else ("update",)
    if selector == "gmail_draft_upsert":
        upsert = "update" if str(payload.get("draft_id") or "").strip() else "create"
        return (upsert, "attach")
    if selector == "workspace_document_upsert":
        return (
            "update"
            if str(payload.get("document_id") or "").strip()
            else "create",
        )
    if selector == "zotero_write_operation":
        operation = str(payload.get("operation") or "").strip().lower()
        if operation in {"create", "update"}:
            return (operation,)
        raise RuntimeError(
            f"Tool {admission.tool_name} requires an explicit create/update operation."
        )
    if set(admission.supported_operations) <= {"read", "search", "verify"}:
        return admission.admitted_operations
    return admission.supported_operations


__all__ = [
    "CapabilityAdmissionReceipt",
    "CapabilityEnforcementMode",
    "ProviderToolAdmission",
    "compile_capability_admission",
    "guard_tool_invocation",
    "verify_expected_capability_profile",
]
