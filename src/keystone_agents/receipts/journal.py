"""Preserve bounded provider tool receipts across SDK retries.

An SDK turn can complete a provider mutation and then fail while validating the
model's structured final answer.  The model retry must not erase proof of the
already-completed tool call or repeat the same mutation.
"""

from __future__ import annotations

import inspect
import json
import os
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from hashlib import sha256
from pathlib import Path
from typing import Any

from keystone_agents.receipts.mutations import (
    mutation_tool_names,
    operation_is_mutation,
    receipt_reports_possible_write,
)
from keystone_agents.receipts.normalization import normalize_tool_output_receipt
from keystone_agents.runtime.decision_validation import is_provider_read_tool_name
from keystone_agents.runtime.tool_call_budget import ToolCallBudgetLedger
from keystone_agents.runtime.tool_execution import tool_result_succeeded

_TOOL_RECEIPT_JOURNAL: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "keystone_tool_receipt_journal",
    default=None,
)
_TOOL_RECEIPT_SINK: ContextVar[Callable[[Mapping[str, Any]], Any] | None] = ContextVar(
    "keystone_tool_receipt_sink",
    default=None,
)
_TOOL_INVOCATION_JOURNAL: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "keystone_tool_invocation_journal",
    default=None,
)
_CURRENT_OPERATION: ContextVar[tuple[Any, str, Any] | None] = ContextVar(
    "kba_current_provider_operation",
    default=None,
)

@dataclass
class _ProviderPreviewEvidence:
    attested: bool = False


_CURRENT_PROVIDER_PREVIEW: ContextVar[_ProviderPreviewEvidence | None] = ContextVar(
    "kba_current_provider_preview", default=None,
)

# Only nonsecret target selectors belong here. Never fingerprint API keys,
# access/refresh tokens, credential contents, or arbitrary environment values.
_OWNER_SCOPE_FIELDS = (
    "api_base_url",
    "expected_account",
    "account",
    "account_email",
    "base_id",
    "calendar_id",
    "library_id",
    "library_type",
    "token_file",
    "credentials_file",
)


def _provider_configuration_scope(tool_name: str) -> dict[str, str]:
    from keystone_agents.context_env import context_env_path, context_env_value

    keys: tuple[str, ...] = ()
    paths: dict[str, str] = {}
    direct_keys: tuple[str, ...] = ()
    if "airtable" in tool_name:
        keys = tuple(
            f"{prefix}_{field}"
            for prefix in ("AIRTABLE", "AIRTABLE_FINANCE_TAX_TRACKER", "AIRTABLE_KNI_OPS")
            for field in ("BASE_ID", "BASE_NAME", "DEFAULT_TABLE")
        )
    elif "zotero" in tool_name:
        keys = ("ZOTERO_LIBRARY_ID", "ZOTERO_LIBRARY_TYPE")
    elif "gmail" in tool_name or "google_calendar" in tool_name:
        keys = ("GMAIL_ACCOUNT", "KNI_BUSINESS_AGENTS_GMAIL_DRAFT_ACCOUNT")
        direct_keys = ("KEYSTONE_GMAIL_DRAFT_ACCOUNT",)
        paths = {"GOOGLE_TOKEN_FILE": "token.json", "GOOGLE_CREDENTIALS_FILE": "credentials.json"}
        if "google_calendar" in tool_name:
            direct_keys += ("GOOGLE_CALENDAR_ID", "GOOGLE_CALENDAR_TIMEZONE")
    elif tool_name.startswith("google_"):
        keys = (
            "GOOGLE_DRIVE_ACCOUNT",
            "GOOGLE_WORKSPACE_REQUIRE_ACCOUNT_MATCH",
            "KEYSTONE_GOOGLE_DOCS_FOLDER",
            "GOOGLE_DRIVE_KNI_OPS_FOLDER",
        )
        paths = {
            "GOOGLE_WORKSPACE_OAUTH_TOKEN_PATH": ".local/google-workspace-oauth-token.json",
            "GOOGLE_TOKEN_FILE": "",
        }
    scope = {key: context_env_value(key).strip() for key in keys}
    scope.update({key: os.getenv(key, "").strip() for key in direct_keys})
    for key, default in paths.items():
        # Resolve the credential file location, not its contents or authenticated account.
        configured = context_env_value(key).strip()
        scope[key] = str(context_env_path(key, default).resolve()) if configured or default else ""
    return scope


def _provider_owner_scope(owner: Any) -> dict[str, str]:
    scope: dict[str, str] = {}
    if owner is None:
        return scope
    for field in _OWNER_SCOPE_FIELDS:
        # Inspect declared data without invoking arbitrary properties or provider reads.
        value = inspect.getattr_static(owner, field, None)
        if isinstance(value, (str, int, Path)) and not isinstance(value, bool):
            scope[field] = str(value).strip()
            if field in {"token_file", "credentials_file"} and scope[field]:
                scope[field] = str(Path(scope[field]).expanduser().resolve())
    return scope


def _bind_provider_scope(
    execution: Any, tool_name: str, *, owner: Any = None, bind_owner: bool = False
) -> None:
    execution.store.bind_provider_scope(
        execution.execution_id,
        tool_name,
        _provider_configuration_scope(tool_name),
    )
    if bind_owner:
        execution.store.bind_provider_scope(
            execution.execution_id,
            tool_name,
            _provider_owner_scope(owner),
            scope_kind="owner",
        )


def record_provider_observation(receipt: Mapping[str, Any]) -> None:
    """Persist a returned object before a composite tool attempts verification."""
    operation = _CURRENT_OPERATION.get()
    if operation is None:
        return
    execution, tool_name, arguments = operation
    execution.store.observe_operation(execution.execution_id, tool_name, arguments, receipt)


def _attest_provider_preview(tool_name: str, result: Any) -> None:
    """Attest a decorated local helper's explicit preview, never a generic failure."""
    if _CURRENT_OPERATION.get() is None:
        return
    receipt = normalize_tool_output_receipt(tool_name, result)
    if not receipt or str(receipt.get("status") or "").lower() not in {
        "dry-run", "dry_run", "preview",
    }:
        return
    verification = receipt.get("verification")
    if (
        receipt.get("provider_write") is True
        or receipt.get("provider_mutated") is True
        or isinstance(verification, Mapping) and verification.get("passed") is True
    ):
        return
    evidence = _CURRENT_PROVIDER_PREVIEW.get()
    if evidence is not None:
        # SDK synchronous tools run in a copied thread context. Mutate this
        # invocation-owned cell so the outer callback can see its attestation.
        evidence.attested = True


def _durable_output_receipt(tool_name: str, result: Any) -> dict[str, Any] | None:
    receipt = normalize_tool_output_receipt(tool_name, result)
    evidence = _CURRENT_PROVIDER_PREVIEW.get()
    if receipt and evidence is not None and evidence.attested:
        verification = receipt.get("verification")
        if (
            str(receipt.get("status") or "").lower() in {"dry-run", "dry_run", "preview"}
            and receipt.get("provider_write") is not True
            and receipt.get("provider_mutated") is not True
            and not (isinstance(verification, Mapping) and verification.get("passed") is True)
        ):
            return {**receipt, "dry_run": True, "provider_write": False}
    return receipt


def durable_provider_tool(tool_name: str):
    """Journal synchronous mutation helpers also used outside SDK FunctionTool dispatch."""

    def decorate(function):
        @wraps(function)
        def invoke(*args, **kwargs):
            from keystone_agents.runtime.durable_execution import current_execution

            execution = current_execution()
            if execution is None:
                return function(*args, **kwargs)
            bound = inspect.signature(function).bind(*args, **kwargs)
            bound.apply_defaults()
            arguments = dict(bound.arguments)
            owner = next(
                (
                    arguments[key]
                    for key in ("self", "tool", "client")
                    if arguments.get(key) is not None
                ),
                None,
            )
            live = arguments.get("live", getattr(owner, "live", None))
            if live is False or arguments.get("dry_run") is True:
                result = function(*args, **kwargs)
                _attest_provider_preview(tool_name, result)
                return result
            _bind_provider_scope(execution, tool_name, owner=owner, bind_owner=True)
            if _CURRENT_OPERATION.get() is not None:
                result = function(*args, **kwargs)
                _attest_provider_preview(tool_name, result)
                return result
            for key in ("self", "client", "session", "store", "connection", "transport", "tool"):
                arguments.pop(key, None)
            saved = execution.store.before_operation(execution.execution_id, tool_name, arguments)
            if saved is not None:
                original = execution.store.stage_result(
                    execution.execution_id,
                    f"tool_result:{tool_name}",
                    arguments,
                )
                return original["tool_output"] if original else saved
            token = _CURRENT_OPERATION.set((execution, tool_name, arguments))
            preview_token = _CURRENT_PROVIDER_PREVIEW.set(_ProviderPreviewEvidence())
            try:
                result = function(*args, **kwargs)
                _attest_provider_preview(tool_name, result)
                receipt = _durable_output_receipt(tool_name, result)
                execution.store.observe_operation(
                    execution.execution_id,
                    tool_name,
                    arguments,
                    receipt,
                )
                if tool_result_succeeded(result):
                    execution.store.save_stage(
                        execution.execution_id,
                        f"tool_result:{tool_name}",
                        arguments,
                        {"tool_output": result},
                    )
                return result
            finally:
                _CURRENT_PROVIDER_PREVIEW.reset(preview_token)
                _CURRENT_OPERATION.reset(token)

        return invoke

    return decorate


def reset_tool_receipt_journal(
    initial_receipts: list[dict[str, Any]] | None = None,
    *,
    receipt_sink: Callable[[Mapping[str, Any]], Any] | None = None,
) -> None:
    """Start one isolated receipt journal for the current SDK execution."""

    _TOOL_RECEIPT_JOURNAL.set([dict(item) for item in (initial_receipts or [])])
    _TOOL_RECEIPT_SINK.set(receipt_sink)
    _TOOL_INVOCATION_JOURNAL.set([])


def tool_receipt_journal() -> list[dict[str, Any]]:
    """Return a detached snapshot of receipts captured in the current execution."""

    return [dict(item) for item in (_TOOL_RECEIPT_JOURNAL.get() or [])]


def tool_invocation_journal() -> list[dict[str, Any]]:
    """Return call-boundary evidence without arguments or provider content."""

    return [dict(item) for item in (_TOOL_INVOCATION_JOURNAL.get() or [])]


def tool_payload_fingerprint(value: Any) -> str:
    """Bind replay to exact JSON arguments/results without retaining their contents."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    return sha256(json.dumps(
        value, ensure_ascii=True, sort_keys=True, default=str,
    ).encode("utf-8")).hexdigest()


def _record_tool_invocation(
    tool_name: str, *, status: str, error_type: str = "", arguments_sha256: str = "",
) -> int:
    journal = _TOOL_INVOCATION_JOURNAL.get()
    if journal is None:
        return 0
    invocation_index = 1 + sum(1 for item in journal if str(item.get("status") or "") == "started")
    journal.append(
        {
            "tool_name": str(tool_name or "").strip(),
            "invocation_index": invocation_index,
            "status": str(status or "").strip(),
            **({"arguments_sha256": arguments_sha256} if arguments_sha256 else {}),
            **({"error_type": error_type[:160]} if error_type else {}),
        }
    )
    return invocation_index


def _record_tool_invocation_terminal(
    tool_name: str,
    invocation_index: int,
    *,
    status: str,
    error_type: str = "",
    output_sha256: str = "",
) -> None:
    journal = _TOOL_INVOCATION_JOURNAL.get()
    if journal is None:
        return
    journal.append(
        {
            "tool_name": str(tool_name or "").strip(),
            "invocation_index": int(invocation_index),
            "status": str(status or "").strip(),
            **({"output_sha256": output_sha256} if output_sha256 else {}),
            **({"error_type": error_type[:160]} if error_type else {}),
        }
    )


def record_tool_output(tool_name: str, output: Any) -> None:
    """Record one bounded structured provider result without message bodies."""

    receipt = normalize_tool_output_receipt(tool_name, output)
    if receipt is None:
        return
    journal = _TOOL_RECEIPT_JOURNAL.get()
    if journal is None:
        return
    journal.append(receipt)
    receipt_sink = _TOOL_RECEIPT_SINK.get()
    if receipt_sink is not None and receipt_reports_possible_write(receipt):
        receipt_sink(receipt)


def instrument_agent_tools(
    agent: Any,
    *,
    tool_call_budget: ToolCallBudgetLedger | None = None,
) -> None:
    """Wrap SDK function tools so outputs survive final-output validation errors."""

    for tool in list(getattr(agent, "tools", []) or []):
        current_callback = getattr(tool, "on_invoke_tool", None)
        original = getattr(
            tool,
            "_keystone_receipt_journal_original",
            current_callback,
        )
        name = str(getattr(tool, "name", "") or "").strip()
        if not name or not callable(original):
            continue
        if not bool(getattr(tool, "_keystone_receipt_journal_wrapped", False)):
            tool._keystone_receipt_journal_original = original

        async def invoke_and_record(
            context: Any,
            tool_input: str,
            *,
            _original: Any = original,
            _name: str = name,
        ) -> Any:
            from keystone_agents.runtime.durable_execution import current_execution, operation_input
            from keystone_agents.storage.sqlite_store import stable_hash

            bounded_read = is_provider_read_tool_name(_name) and not operation_is_mutation(_name)
            invocation_index = _record_tool_invocation(
                _name, status="started",
                arguments_sha256=tool_payload_fingerprint(tool_input) if bounded_read else "",
            )
            if tool_call_budget is not None:
                admission = tool_call_budget.admit(
                    _name,
                    mutation=operation_is_mutation(_name),
                )
                if not admission.admitted:
                    _record_tool_invocation_terminal(
                        _name,
                        invocation_index,
                        status="blocked",
                        error_type="ToolCallBudgetExhausted",
                    )
                    return json.dumps(
                        admission.blocked_output(),
                        ensure_ascii=True,
                        sort_keys=True,
                    )
            execution = current_execution()
            durable_mutation = execution is not None and operation_is_mutation(_name)
            read_input = None
            if execution is not None and not durable_mutation:
                read_input = {
                    "arguments": operation_input(tool_input),
                    "effect_revision": stable_hash(
                        execution.store.operations(execution.execution_id)
                    ),
                }
                cached = execution.store.stage_result(
                    execution.execution_id,
                    f"tool_read:{_name}",
                    read_input,
                )
                if cached is not None:
                    output = cached["tool_output"]
                    record_tool_output(_name, output)
                    _record_tool_invocation_terminal(
                        _name,
                        invocation_index,
                        status="reused_verified_read",
                        output_sha256=tool_payload_fingerprint(output) if bounded_read else "",
                    )
                    return output
            if durable_mutation:
                _bind_provider_scope(execution, _name)
                saved = execution.store.before_operation(execution.execution_id, _name, tool_input)
                if saved is not None:
                    original_output = execution.store.stage_result(
                        execution.execution_id,
                        f"tool_result:{_name}",
                        operation_input(tool_input),
                    )
                    record_tool_output(_name, saved)
                    _record_tool_invocation_terminal(
                        _name,
                        invocation_index,
                        status="reused_verified",
                    )
                    return original_output["tool_output"] if original_output else json.dumps(saved)
            operation_token = _CURRENT_OPERATION.set(
                (execution, _name, tool_input) if durable_mutation else None,
            )
            preview_token = _CURRENT_PROVIDER_PREVIEW.set(_ProviderPreviewEvidence())
            try:
                result = _original(context, tool_input)
                if inspect.isawaitable(result):
                    result = await result
                if durable_mutation:
                    receipt = _durable_output_receipt(_name, result)
                    execution.store.observe_operation(
                        execution.execution_id,
                        _name,
                        tool_input,
                        receipt,
                    )
                    if tool_result_succeeded(result):
                        execution.store.save_stage(
                            execution.execution_id,
                            f"tool_result:{_name}",
                            operation_input(tool_input),
                            {"tool_output": result},
                        )
                elif (
                    execution is not None
                    and read_input is not None
                    and tool_result_succeeded(result)
                ):
                    execution.store.save_stage(
                        execution.execution_id,
                        f"tool_read:{_name}",
                        read_input,
                        {"tool_output": result},
                    )
                record_tool_output(_name, result)
            except Exception as exc:
                _record_tool_invocation_terminal(
                    _name,
                    invocation_index,
                    status="failed",
                    error_type=type(exc).__name__,
                )
                raise
            finally:
                _CURRENT_PROVIDER_PREVIEW.reset(preview_token)
                _CURRENT_OPERATION.reset(operation_token)
            _record_tool_invocation_terminal(
                _name,
                invocation_index,
                status=("completed" if tool_result_succeeded(result) else "returned_unsuccessful"),
                output_sha256=(
                    tool_payload_fingerprint(result)
                    if bounded_read and tool_result_succeeded(result) else ""
                ),
            )
            return result

        tool.on_invoke_tool = invoke_and_record
        tool._keystone_receipt_journal_wrapped = True


def retry_receipt_context(receipts: list[dict[str, Any]]) -> str:
    """Build a compact recovery instruction from already-observed provider truth."""

    compact = json.dumps(receipts[-12:], ensure_ascii=True, sort_keys=True, default=str)
    return (
        "\n\nSDK structured-output recovery context:\n"
        "The prior attempt already produced the provider tool receipts below. "
        "Treat them as authoritative. Do not repeat any mutation represented by these "
        "receipts. You may use a read-only tool if another provider check is needed. "
        "Return the requested typed final answer from the original request and these "
        f"receipts.\nProvider receipts: {compact[:12000]}"
    )


__all__ = [
    "instrument_agent_tools",
    "mutation_tool_names",
    "record_tool_output",
    "record_provider_observation",
    "durable_provider_tool",
    "receipt_reports_possible_write",
    "reset_tool_receipt_journal",
    "retry_receipt_context",
    "tool_invocation_journal",
    "tool_receipt_journal",
]
