"""Run Gmail triage with env-aware dry-run or live-test defaults."""

from __future__ import annotations

import argparse
import inspect
import json
from dataclasses import replace
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from keystone_agents.agents.gmail_triage import (
    build_gmail_priority_grouping_agent,
    build_gmail_triage_agent,
    email_fixture_to_envelope,
    load_email_fixture,
    run_gmail_triage_fixture,
    triage_gmail_message_envelope,
)
from keystone_agents.cli_orchestrator_review import (
    add_orchestrator_review_arguments,
    build_cli_orchestrator_review,
)
from keystone_agents.cli_sdk import (
    SDKRunConfigFactory,
    add_sdk_run_arguments,
    reject_sdk_side_effect_flags,
    resolve_sdk_execution,
    sdk_agent_description,
    sdk_execution_requested,
    sdk_synthesis_payload,
)
from keystone_agents.config import (
    SLACK_APPROVAL_CREDENTIALS,
    cli_default_dry_run,
    cli_default_live_gmail,
    cli_default_live_sdk,
    require_cli_live_confirmation,
    with_cli_environment,
)
from keystone_agents.feedback import build_operator_feedback_request
from keystone_agents.founder_profile import (
    founder_drafting_context,
    founder_profile_audit_payload,
    load_founder_fit_profile,
)
from keystone_agents.model_provider import get_runtime_agent_model_config
from keystone_agents.models import (
    DEFAULT_GMAIL_PRIORITY_GROUPING_REQUEST,
    GmailPriorityGroupingSDKInput,
    GmailTriageSDKInput,
)
from keystone_agents.reporting import (
    build_gmail_priority_grouping_test_pack_payload,
    render_gmail_priority_grouping_test_pack_report,
    render_gmail_triage_report,
    render_orchestrator_output_review,
    to_json,
)
from keystone_agents.run import run_retrieved_sdk_synthesis
from keystone_agents.schemas.approval import ApprovalScope, ApprovalState
from keystone_agents.schemas.email_triage import (
    EmailTriageResult,
    GmailClarificationResult,
    GmailMessageEnvelope,
    GmailPriorityGroupingResult,
    GmailThreadSummaryMessage,
    GmailThreadSummaryResult,
)
from keystone_agents.sdk import ToolGuardrailViolation
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools.approval_tool import build_approval_queue_item, post_approval_request
from keystone_agents.tools.email_style_tool import (
    load_email_style_profile_fixture,
    load_email_style_profile_from_storage,
)
from keystone_agents.tools.gmail_tool import GmailAPIError, GmailConfigurationError, GmailTool
from keystone_agents.tools.storage_tool import StorageTool

APPROVAL_DECISIONS = [state.value for state in ApprovalState]
APPROVAL_SCOPES = [scope.value for scope in ApprovalScope]
TERMINAL_APPROVAL_DECISIONS = {ApprovalState.REJECTED.value, ApprovalState.EXPIRED.value}
SDK_RUN_CONFIG_FACTORY: SDKRunConfigFactory | None = None
ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY: SDKRunConfigFactory | None = None
GT1_PRIORITY_GROUPING_PROMPT = DEFAULT_GMAIL_PRIORITY_GROUPING_REQUEST


def _approval_context(
    *,
    object_id: str | int,
    summary: str,
    args: argparse.Namespace,
    slack_ts: str | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "approval_scope": args.approval_scope,
        "slack_ts": slack_ts,
        "send_enabled": False,
    }
    if args.ask_feedback:
        metadata["operator_feedback_request"] = build_operator_feedback_request(
            object_type="email_triage",
            object_id=object_id,
            source_agent="gmail_triage",
            review_stage="approval_review",
        ).model_dump(mode="json")
    return {
        "object_type": "gmail_draft",
        "object_id": object_id,
        "summary": summary,
        "decision": args.approval_decision,
        "scope": args.approval_scope,
        "reviewer": args.reviewer,
        "notes": args.approval_notes,
        "source_agent": "gmail_triage",
        "metadata": metadata,
    }


def _apply_labels_with_optional_cleanup(
    gmail: Any,
    *,
    message: dict[str, Any],
    message_id: str,
    labels: list[str],
    cleanup_obsolete: bool,
    dry_run_preview: bool,
) -> dict[str, Any]:
    optional_kwargs = {
        "existing_labels": [
            str(label) for label in message.get("labelIds", []) if str(label).strip()
        ],
        "cleanup_obsolete": cleanup_obsolete,
        "dry_run_preview": dry_run_preview,
    }
    signature = inspect.signature(gmail.apply_labels)
    parameters = signature.parameters
    supports_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
    if supports_kwargs or set(optional_kwargs) <= set(parameters):
        return gmail.apply_labels(message_id=message_id, labels=labels, **optional_kwargs)
    return gmail.apply_labels(message_id=message_id, labels=labels)


def _require_live_draft_approval(
    *,
    database_url: str | None,
    message_id: str,
) -> dict[str, Any]:
    """Require a latest approved-for-send local record before live draft creation."""

    approval = SQLiteStore(database_url).latest_approval(
        object_type="gmail_draft",
        object_id=message_id,
        scope=ApprovalScope.SEND,
    )
    expected = ApprovalState.APPROVED_FOR_SEND.value
    if approval is None:
        raise SystemExit(
            "Live Gmail draft creation requires an approved_for_send approval record "
            f"for gmail_draft '{message_id}' in local SQLite. "
            "Create or approve the record, pass --database-url if needed, and rerun. "
            "No Gmail draft was created."
        )
    decision = str(approval.get("decision") or "")
    scope = str(approval.get("scope") or "")
    if decision != expected or scope != ApprovalScope.SEND.value:
        raise SystemExit(
            "Live Gmail draft creation blocked: latest approval for "
            f"gmail_draft '{message_id}' is {decision or 'unknown'}/"
            f"{scope or 'unknown'}, expected {expected}/{ApprovalScope.SEND.value}. "
            "No Gmail draft was created."
        )
    return {
        "id": approval.get("id"),
        "object_type": approval.get("object_type"),
        "object_id": approval.get("object_id"),
        "decision": decision,
        "scope": scope,
        "reviewer": approval.get("reviewer") or "",
    }


def _structured_gmail_clarification(
    *,
    status: str,
    operation: str,
    clarification_request: str,
    message: str,
    source_label: str,
    query: str = "",
    reason_code: str = "",
    candidate_thread_ids: list[str] | None = None,
    missing_inputs: list[str] | None = None,
    suggested_next_steps: list[str] | None = None,
) -> dict[str, Any]:
    return GmailClarificationResult(
        status=status,
        clarification_request=clarification_request,
        operation=operation,
        reason_code=reason_code,
        message=message,
        source_label=source_label,
        query=query,
        candidate_count=len(candidate_thread_ids or []),
        candidate_thread_ids=candidate_thread_ids or [],
        missing_inputs=missing_inputs or [],
        suggested_next_steps=suggested_next_steps or [],
        send_enabled=False,
        draft_created=False,
        labels_modified=False,
    ).model_dump(mode="json")


def _dedupe_thread_ids(refs: list[dict[str, Any]]) -> list[str]:
    thread_ids: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        thread_id = str(ref.get("threadId") or ref.get("id") or "").strip()
        if not thread_id or thread_id in seen:
            continue
        seen.add(thread_id)
        thread_ids.append(thread_id)
    return thread_ids


def _thread_summary_result_from_payload(
    *,
    thread: dict[str, Any],
    source_label: str,
    query: str,
) -> GmailThreadSummaryResult:
    messages = [
        GmailThreadSummaryMessage(
            message_id=str(item.get("id") or ""),
            received_at=str(item.get("received_at") or ""),
            sender_name=str(item.get("sender_name") or ""),
            sender_email=str(item.get("sender_email") or ""),
            subject=str(item.get("subject") or ""),
            snippet=str(item.get("snippet") or ""),
            prior_labels=[
                str(label) for label in item.get("prior_labels", []) if str(label).strip()
            ],
            summary=str(item.get("thread_summary") or item.get("snippet") or ""),
        )
        for item in thread.get("messages", [])
        if isinstance(item, dict)
    ]
    return GmailThreadSummaryResult(
        thread_id=str(thread.get("thread_id") or ""),
        source_label=source_label,
        query=query,
        subject=str(thread.get("subject") or ""),
        summary=str(thread.get("summary") or ""),
        thread_context=str(thread.get("thread_context") or ""),
        message_count=int(thread.get("message_count") or 0),
        latest_received_at=str(thread.get("latest_received_at") or ""),
        participants=[str(item) for item in thread.get("participants", []) if str(item).strip()],
        action_items=[str(item) for item in thread.get("action_items", []) if str(item).strip()],
        deadlines=[str(item) for item in thread.get("deadlines", []) if str(item).strip()],
        open_questions=[
            str(item) for item in thread.get("open_questions", []) if str(item).strip()
        ],
        triage_limitations=[
            str(item) for item in thread.get("triage_limitations", []) if str(item).strip()
        ],
        messages=messages,
        send_enabled=False,
        draft_created=False,
        labels_modified=False,
    )


def _render_thread_summary_markdown(payload: dict[str, Any]) -> str:
    threads = payload.get("threads", [])
    if not threads:
        return "No Gmail threads found."
    parts: list[str] = []
    for thread in threads:
        parts.append(str(thread.get("summary") or "Thread summary unavailable."))
        action_items = thread.get("action_items") or []
        deadlines = thread.get("deadlines") or []
        open_questions = thread.get("open_questions") or []
        if action_items:
            parts.append("Action items:")
            parts.extend(f"- {item}" for item in action_items)
        if deadlines:
            parts.append("Deadlines:")
            parts.extend(f"- {item}" for item in deadlines)
        if open_questions:
            parts.append("Open questions:")
            parts.extend(f"- {item}" for item in open_questions)
    return "\n".join(parts)


def _render_clarification_markdown(payload: dict[str, Any]) -> str:
    lines = [str(payload.get("message") or "Clarification required.")]
    clarification_request = str(payload.get("clarification_request") or "")
    if clarification_request:
        lines.extend(["", clarification_request])
    missing_inputs = payload.get("missing_inputs") or []
    if missing_inputs:
        lines.append("")
        lines.append("Missing inputs:")
        lines.extend(f"- {item}" for item in missing_inputs)
    suggested_next_steps = payload.get("suggested_next_steps") or []
    if suggested_next_steps:
        lines.append("")
        lines.append("Next steps:")
        lines.extend(f"- {item}" for item in suggested_next_steps)
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    dry_run_default = cli_default_dry_run()
    live_gmail_default = cli_default_live_gmail()
    parser = argparse.ArgumentParser(description="Run the Gmail triage agent.")
    parser.add_argument("--fixture", default=None, help="Path to a local plain-text email fixture.")
    parser.add_argument(
        "--fixtures",
        nargs="+",
        default=None,
        help="Multiple local plain-text email fixtures for priority grouping.",
    )
    parser.add_argument(
        "--subject", default="", help="Subject override or subject for inline input."
    )
    parser.add_argument(
        "--request",
        default="",
        help="Optional operator request to include in SDK synthesis context.",
    )
    parser.add_argument("--sender-name", default="", help="Sender display name.")
    parser.add_argument("--sender-email", default="", help="Sender email address.")
    parser.add_argument(
        "--email-style-profile-fixture",
        default=None,
        help="Optional approved local aggregate email style profile fixture.",
    )
    parser.add_argument(
        "--email-style-profile-id",
        default=None,
        help="Optional approved aggregate email style profile id from local SQLite.",
    )
    parser.add_argument(
        "--founder-fit-profile",
        default=None,
        help=(
            "Optional approved founder-fit JSON profile. Used for business-fit "
            "and reply drafting only when approved_for_drafting=true."
        ),
    )
    parser.add_argument(
        "--live-gmail",
        action="store_true",
        default=live_gmail_default,
        help="Use live Gmail API access.",
    )
    parser.add_argument(
        "--label-filter",
        default=None,
        help="Required Gmail label id/name for live Gmail unless --allow-inbox is passed.",
    )
    parser.add_argument(
        "--gmail-query",
        "--query",
        dest="gmail_query",
        default=None,
        help="Optional Gmail search query to narrow live read-only triage within the label scope.",
    )
    parser.add_argument(
        "--thread-id",
        default=None,
        help="Optional Gmail thread id to summarize directly in read-only mode.",
    )
    parser.add_argument(
        "--thread-summary",
        action="store_true",
        default=False,
        help=(
            "Read and summarize one or more Gmail threads instead of per-message triage. "
            "This mode is always read-only."
        ),
    )
    parser.add_argument(
        "--max-messages", type=int, default=1, help="Maximum Gmail messages to process."
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=3,
        help="Unread Gmail lookback window for priority grouping.",
    )
    parser.add_argument(
        "--priority-grouping",
        action="store_true",
        default=False,
        help="Run LLM SDK batch priority grouping for unread Gmail messages.",
    )
    parser.add_argument(
        "--test-pack-report-dir",
        default=None,
        help=(
            "Write sanitized GT-1 test-pack markdown and JSON report artifacts to this "
            "local directory. Only valid with --priority-grouping SDK synthesis."
        ),
    )
    parser.add_argument(
        "--create-draft",
        action="store_true",
        default=False,
        help="Create Gmail drafts for reply-needed messages.",
    )
    parser.add_argument(
        "--apply-labels",
        action="store_true",
        default=False,
        help="Apply recommended Keystone labels to live Gmail messages.",
    )
    parser.add_argument(
        "--preview-labels",
        action="store_true",
        default=False,
        help="Preview Gmail label add/remove operations without applying labels.",
    )
    parser.add_argument(
        "--cleanup-labels",
        action="store_true",
        default=False,
        help="Remove obsolete Keystone-managed Gmail labels. Preview first with --preview-labels.",
    )
    parser.add_argument(
        "--allow-inbox",
        action="store_true",
        default=live_gmail_default,
        help="Allow live Gmail to read INBOX when no label filter is supplied.",
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=dry_run_default,
        help=(
            "Use deterministic fixture mode. Defaults to KEYSTONE_DRY_RUN or true. "
            "Pass --no-dry-run for live flags."
        ),
    )
    parser.add_argument(
        "--sdk", action="store_true", help="Use explicit SDK mode instead of fixture mode."
    )
    add_sdk_run_arguments(parser)
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    parser.add_argument("--markdown", action="store_true", help="Print markdown output.")
    add_orchestrator_review_arguments(parser)
    parser.add_argument(
        "--save", action="store_true", help="Save triage and audit records to SQLite."
    )
    parser.add_argument("--database-url", default=None, help="SQLite URL for --save.")
    parser.add_argument(
        "--request-approval", action="store_true", help="Post a draft approval request."
    )
    parser.add_argument(
        "--ask-feedback",
        action="store_true",
        help="Attach an optional structured operator feedback request to the review item.",
    )
    parser.add_argument(
        "--approval-decision",
        choices=APPROVAL_DECISIONS,
        default="pending",
        help="Manual approval decision to apply to this fixture run.",
    )
    parser.add_argument(
        "--approval-scope",
        choices=APPROVAL_SCOPES,
        default=ApprovalScope.SEND.value,
        help="Workflow scope the approval decision applies to.",
    )
    parser.add_argument("--reviewer", default="", help="Human reviewer for saved approvals.")
    parser.add_argument("--approval-notes", default="", help="Reviewer notes for saved approvals.")
    parser.add_argument(
        "--live-slack", action="store_true", help="Post approval notification to Slack."
    )
    return parser


def _apply_live_test_defaults(args: argparse.Namespace) -> argparse.Namespace:
    """Promote env-backed live test defaults for SDK-only Gmail paths."""

    if (
        args.priority_grouping
        and not args.sdk
        and not sdk_execution_requested(args)
        and cli_default_live_sdk()
    ):
        args.live_sdk = True
    return args


def _style_profile_context(value: Any | None) -> str:
    if value is None or not getattr(value, "approved_for_drafting", False):
        return ""
    return json.dumps(value.model_dump(mode="json"), ensure_ascii=True, sort_keys=True)


def _load_requested_style_profile(args: argparse.Namespace) -> Any | None:
    if args.email_style_profile_id and args.email_style_profile_fixture:
        raise SystemExit(
            "Use either --email-style-profile-id or --email-style-profile-fixture, not both."
        )
    if args.email_style_profile_id:
        profile = load_email_style_profile_from_storage(
            args.email_style_profile_id,
            database_url=args.database_url,
        )
        if profile is None:
            raise SystemExit(
                "No approved email style profile found in local SQLite for "
                f"'{args.email_style_profile_id}'. Generated profiles must be "
                "approved_for_drafting before use."
            )
        return profile
    if args.email_style_profile_fixture:
        return load_email_style_profile_fixture(args.email_style_profile_fixture)
    return None


def _run_sdk_synthesis(
    args: argparse.Namespace,
    *,
    email_style_profile: Any | None,
    founder_fit_profile: Any | None,
) -> dict[str, Any]:
    reject_sdk_side_effect_flags(
        args,
        {
            "live_slack": "--live-slack",
            "request_approval": "--request-approval",
            "create_draft": "--create-draft",
            "apply_labels": "--apply-labels",
            "preview_labels": "--preview-labels",
            "cleanup_labels": "--cleanup-labels",
        },
    )
    if args.live_gmail:
        require_cli_live_confirmation(
            dry_run=args.dry_run,
            live_flag=True,
            flag_name="--live-gmail",
            live_action="read-only Gmail API calls for SDK synthesis",
        )
    run_config, live = resolve_sdk_execution(
        args,
        run_config_factory=SDK_RUN_CONFIG_FACTORY,
    )
    style_context = _style_profile_context(email_style_profile)
    founder_context = founder_drafting_context(founder_fit_profile)

    def retrieve() -> GmailMessageEnvelope:
        if args.live_gmail:
            label = _priority_grouping_source_label(args)
            gmail = GmailTool(live=True)
            message_refs = gmail.list_recent_messages(
                label=label,
                max_results=args.max_messages,
                query=args.gmail_query,
            )
            if not message_refs:
                raise RuntimeError("No Gmail message matched the live SDK request.")
            if len(message_refs) != 1:
                raise RuntimeError(
                    "Live Gmail SDK synthesis requires exactly one selected message. "
                    "Narrow --gmail-query or use --max-messages 1."
                )
            message = gmail.get_message(str(message_refs[0]["id"]))
            if not message.get("id"):
                message["id"] = str(message_refs[0]["id"])
            return _thread_enriched_live_envelope(gmail, message)
        fixture = load_email_fixture(
            args.fixture,
            subject=args.subject,
            sender_name=args.sender_name,
            sender_email=args.sender_email,
        )
        return email_fixture_to_envelope(fixture)

    def normalize(envelope: GmailMessageEnvelope) -> GmailTriageSDKInput:
        typed_input = GmailTriageSDKInput.from_envelope(envelope)
        if args.request:
            typed_input = replace(typed_input, request=args.request)
        if style_context or founder_context:
            return replace(
                typed_input,
                email_style_profile=style_context,
                founder_fit_context=founder_context,
            )
        return typed_input

    storage = StorageTool(args.database_url) if args.save else None
    outcome = run_retrieved_sdk_synthesis(
        agent=build_gmail_triage_agent(include_tools=False),
        output_type=EmailTriageResult,
        retrieve=retrieve,
        normalize=normalize,
        input_summary=args.request or args.subject or args.fixture or "gmail triage SDK synthesis",
        input_audit_payload={
            "fixture": args.fixture,
            "subject": args.subject,
            "sender_email": args.sender_email,
            "request": args.request,
            "live_gmail": bool(args.live_gmail),
            "gmail_query": args.gmail_query,
            "label_filter": args.label_filter,
            "allow_inbox": bool(args.allow_inbox),
            "email_style_profile_fixture": args.email_style_profile_fixture,
            "email_style_profile_id": args.email_style_profile_id,
            "founder_fit_profile": founder_profile_audit_payload(
                args.founder_fit_profile,
                founder_fit_profile,
            ),
            "sdk_synthesis": True,
        },
        run_config=run_config,
        live=live,
        trace_include_sensitive_data=args.trace_include_sensitive_data,
        save=args.save,
        storage=storage,
        model_label="sdk-live" if live else "sdk-local",
    )
    payload = sdk_synthesis_payload(
        outcome,
        include_provider_cost_window=args.include_provider_cost_window,
        provider_cost_window_seconds=args.provider_cost_window_seconds,
        openai_cost_project_id=args.openai_cost_project_id,
    )
    if args.orchestrator_review:
        payload["orchestrator_review"] = build_cli_orchestrator_review(
            args,
            run_config_factory=ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY,
            agent_name="gmail_triage",
            output=outcome.final_output,
            request_summary=args.request
            or args.subject
            or args.fixture
            or "gmail triage SDK synthesis",
            run_type="live SDK" if live else "local SDK",
        )
    return payload


def _priority_grouping_source_label(args: argparse.Namespace) -> str:
    if args.label_filter:
        return str(args.label_filter)
    if args.allow_inbox:
        return "INBOX"
    return "UNREAD"


def _fixture_envelopes_for_priority_grouping(
    args: argparse.Namespace,
) -> list[GmailMessageEnvelope]:
    fixture_paths = list(args.fixtures or ([] if args.fixture is None else [args.fixture]))
    if not fixture_paths:
        raise SystemExit(
            "--priority-grouping requires --fixtures or --fixture unless --live-gmail is set."
        )

    envelopes: list[GmailMessageEnvelope] = []
    for index, fixture_path in enumerate(fixture_paths, start=1):
        fixture = load_email_fixture(
            fixture_path,
            subject=args.subject,
            sender_name=args.sender_name,
            sender_email=args.sender_email,
        )
        path = Path(str(fixture_path))
        fixture_key = path.stem or f"message-{index}"
        envelope = email_fixture_to_envelope(fixture).model_copy(
            update={
                "message_id": f"fixture-{index}-{fixture_key}",
                "thread_id": f"fixture-thread-{index}-{fixture_key}",
                "triage_limitations": [
                    "Fixture mode uses sanitized local email content; no live Gmail "
                    "reads were made."
                ],
            }
        )
        envelopes.append(envelope)
    return envelopes


def _envelope_from_live_message_payload(message: dict[str, Any]) -> GmailMessageEnvelope:
    sender_name, sender_email = parseaddr(str(message.get("from") or ""))
    return GmailMessageEnvelope.model_validate(
        message.get("envelope")
        or {
            "message_id": str(message.get("id") or ""),
            "thread_id": str(message.get("threadId") or ""),
            "received_at": str(message.get("received_at") or ""),
            "subject": str(message.get("subject") or ""),
            "sender_name": sender_name,
            "sender_email": sender_email,
            "snippet": str(message.get("snippet") or ""),
            "normalized_body": str(
                message.get("normalized_body")
                or message.get("body")
                or message.get("snippet")
                or ""
            ),
            "prior_labels": [
                str(label) for label in message.get("labelIds", []) if str(label).strip()
            ],
            "extracted_links": message.get("extracted_links", []),
            "attachment_metadata": message.get("attachment_metadata", []),
            "thread_summary": str(message.get("thread_summary") or ""),
            "thread_context": str(message.get("thread_context") or ""),
            "suspicious_signals": message.get("suspicious_signals", []),
            "triage_limitations": message.get("triage_limitations", []),
        }
    )


def _thread_enriched_live_envelope(
    gmail: Any,
    message: dict[str, Any],
) -> GmailMessageEnvelope:
    """Prefer full read-only thread context when the Gmail adapter exposes it."""

    envelope = _envelope_from_live_message_payload(message)
    thread_id = envelope.thread_id or str(message.get("threadId") or "")
    get_thread = getattr(gmail, "get_thread", None)
    if not thread_id or not callable(get_thread):
        return envelope

    thread = get_thread(thread_id)
    thread_messages = thread.get("messages") if isinstance(thread, dict) else []
    for thread_message in thread_messages or []:
        if not isinstance(thread_message, dict):
            continue
        thread_message_id = str(thread_message.get("message_id") or thread_message.get("id") or "")
        if thread_message_id != envelope.message_id:
            continue
        return _envelope_from_live_message_payload(thread_message)

    thread_context = str(thread.get("thread_context") or "") if isinstance(thread, dict) else ""
    thread_summary = str(thread.get("summary") or "") if isinstance(thread, dict) else ""
    message_count = int(thread.get("message_count") or 0) if isinstance(thread, dict) else 0
    if not (thread_context or thread_summary or message_count):
        return envelope
    limitations = list(envelope.triage_limitations)
    limitations.append("Full Gmail thread was read in sanitized read-only mode.")
    return envelope.model_copy(
        update={
            "thread_context": thread_context or envelope.thread_context,
            "thread_summary": envelope.thread_summary or thread_summary,
            "thread_message_count": message_count or envelope.thread_message_count,
            "triage_limitations": list(dict.fromkeys(limitations)),
        }
    )


_GMAIL_PRIORITY_ACTION_TERMS = (
    "action required",
    "approval",
    "certificate",
    "coi",
    "contract",
    "deadline",
    "due",
    "follow up",
    "fw:",
    "insurance",
    "introduction",
    "invoice",
    "keystone",
    "login",
    "meeting",
    "onboarding",
    "password",
    "payment",
    "please",
    "quote",
    "re:",
    "reply",
    "request",
    "review",
    "scope",
    "sow",
    "urgent",
)
_GMAIL_PRIORITY_NOISE_TERMS = (
    "ads setting",
    "digest",
    "newsletter",
    "roundup",
    "unsubscribe",
    "weekly",
)
_GMAIL_THREAD_CONTEXT_TERMS = (
    "certificate",
    "coi",
    "contract",
    "follow up",
    "fw:",
    "insurance",
    "introduction",
    "onboarding",
    "quote",
    "re:",
    "scope",
    "sow",
)


def _summary_text(summary: dict[str, Any]) -> str:
    return " ".join(
        str(summary.get(key) or "")
        for key in ("from", "subject", "snippet", "thread_summary", "thread_context")
    ).lower()


def _priority_summary_needs_full_message(summary: dict[str, Any]) -> bool:
    text = _summary_text(summary)
    if any(term in text for term in _GMAIL_PRIORITY_ACTION_TERMS):
        return True
    if any(term in text for term in _GMAIL_PRIORITY_NOISE_TERMS):
        return False
    return bool(str(summary.get("threadId") or "").strip())


def _priority_summary_needs_thread(summary: dict[str, Any]) -> bool:
    text = _summary_text(summary)
    return any(term in text for term in _GMAIL_THREAD_CONTEXT_TERMS)


def _summary_has_classification_context(summary: dict[str, Any]) -> bool:
    return any(str(summary.get(key) or "").strip() for key in ("from", "subject", "snippet"))


def _envelope_from_live_summary(summary: dict[str, Any]) -> GmailMessageEnvelope:
    envelope = _envelope_from_live_message_payload(
        {
            **summary,
            "body": str(summary.get("snippet") or summary.get("thread_summary") or ""),
            "normalized_body": str(summary.get("snippet") or summary.get("thread_summary") or ""),
            "labelIds": summary.get("labelIds") or summary.get("prior_labels") or [],
            "triage_limitations": [
                *[str(item) for item in summary.get("triage_limitations", []) if str(item).strip()],
                "GT-1 staged retrieval classified this item from Gmail search summary only.",
            ],
        }
    )
    return envelope


def _search_live_message_summaries(
    gmail: Any,
    *,
    label: str,
    max_results: int,
    query: str,
) -> list[dict[str, Any]]:
    search_summaries = getattr(gmail, "search_message_summaries", None)
    if callable(search_summaries):
        return list(search_summaries(label=label, max_results=max_results, query=query))
    return list(gmail.list_recent_messages(label=label, max_results=max_results, query=query))


def _batch_read_live_messages(
    gmail: Any,
    message_ids: list[str],
) -> dict[str, dict[str, Any]]:
    if not message_ids:
        return {}
    batch_get = getattr(gmail, "batch_get_messages", None)
    if callable(batch_get):
        messages = batch_get(message_ids, skip_blocked=True)
    else:
        messages = []
        for message_id in message_ids:
            try:
                messages.append(gmail.get_message(message_id))
            except ToolGuardrailViolation:
                continue
    return {
        str(message.get("id") or message.get("message_id") or ""): message
        for message in messages
        if str(message.get("id") or message.get("message_id") or "").strip()
    }


def _live_gmail_envelopes_for_priority_grouping(
    args: argparse.Namespace,
) -> list[GmailMessageEnvelope]:
    gmail = GmailTool(live=True)
    label = _priority_grouping_source_label(args)
    query = str(args.gmail_query or "").strip() or f"newer_than:{args.lookback_days}d"
    summaries = _search_live_message_summaries(
        gmail,
        label=label,
        max_results=args.max_messages,
        query=query,
    )
    full_read_ids = [
        str(summary.get("id") or "").strip()
        for summary in summaries
        if str(summary.get("id") or "").strip() and _priority_summary_needs_full_message(summary)
    ]
    full_messages = _batch_read_live_messages(gmail, full_read_ids)
    envelopes: list[GmailMessageEnvelope] = []
    for summary in summaries:
        message_id = str(summary.get("id") or "").strip()
        if not message_id:
            continue
        message = full_messages.get(message_id)
        if message is None:
            if message_id in full_read_ids and not _summary_has_classification_context(summary):
                continue
            envelopes.append(_envelope_from_live_summary(summary))
            continue
        if not message.get("id"):
            message["id"] = message_id
        if _priority_summary_needs_thread(summary):
            try:
                envelopes.append(_thread_enriched_live_envelope(gmail, message))
                continue
            except ToolGuardrailViolation:
                pass
        envelope = _envelope_from_live_message_payload(message)
        limitations = [
            *envelope.triage_limitations,
            "GT-1 staged retrieval read the full message body without expanding the thread.",
        ]
        envelopes.append(
            envelope.model_copy(update={"triage_limitations": list(dict.fromkeys(limitations))})
        )
    return envelopes


def _priority_grouping_report_source(args: argparse.Namespace) -> str:
    if args.live_gmail:
        query = str(args.gmail_query or "").strip() or f"newer_than:{args.lookback_days}d"
        return (
            f"Read-only staged Gmail retrieval: search summaries for query {query} "
            f"with source label {_priority_grouping_source_label(args)}, batch-read "
            "shortlisted messages, and expand full threads only when needed."
        )
    fixture_paths = list(args.fixtures or ([] if args.fixture is None else [args.fixture]))
    names = ", ".join(Path(str(path)).name for path in fixture_paths)
    return f"Local Gmail fixtures: {names}."


def _priority_grouping_report_model(live: bool) -> str:
    if not live:
        return "injected fake/local SDK run_config"
    config = get_runtime_agent_model_config("gmail_triage").as_log_dict()
    provider = str(config.get("provider") or "unknown")
    model = str(config.get("model") or "unknown")
    return f"{provider}/{model}"


def _write_priority_grouping_test_pack_report(
    *,
    args: argparse.Namespace,
    result: GmailPriorityGroupingResult,
    live: bool,
    usage: dict[str, Any] | None = None,
    cost: dict[str, Any] | None = None,
    gemini_free_tier_usage: dict[str, Any] | None = None,
    orchestrator_review: dict[str, Any] | None = None,
) -> dict[str, str]:
    report_dir_value = str(args.test_pack_report_dir or "").strip()
    if not report_dir_value:
        return {}

    report_dir = Path(report_dir_value)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_payload = build_gmail_priority_grouping_test_pack_payload(
        result,
        run_type="live SDK" if live else "local SDK",
        model=_priority_grouping_report_model(live),
        input_summary=GT1_PRIORITY_GROUPING_PROMPT,
        input_source=_priority_grouping_report_source(args),
        usage=usage,
        cost=cost,
        gemini_free_tier_usage=gemini_free_tier_usage,
        orchestrator_review=orchestrator_review,
    )
    markdown_path = report_dir / "gmail-triage-gt1-priority-grouping.md"
    json_path = report_dir / "gmail-triage-gt1-priority-grouping.json"
    markdown_path.write_text(
        render_gmail_priority_grouping_test_pack_report(report_payload),
        encoding="utf-8",
    )
    json_path.write_text(to_json(report_payload), encoding="utf-8")
    return {
        "markdown_path": str(markdown_path),
        "json_path": str(json_path),
        "status": str(report_payload["status"]),
    }


def _run_priority_grouping_sdk_synthesis(
    args: argparse.Namespace,
    *,
    email_style_profile: Any | None,
    founder_fit_profile: Any | None,
) -> dict[str, Any]:
    reject_sdk_side_effect_flags(
        args,
        {
            "live_slack": "--live-slack",
            "request_approval": "--request-approval",
            "create_draft": "--create-draft",
            "apply_labels": "--apply-labels",
            "preview_labels": "--preview-labels",
            "cleanup_labels": "--cleanup-labels",
        },
    )
    if args.live_gmail:
        require_cli_live_confirmation(
            dry_run=args.dry_run,
            live_flag=True,
            flag_name="--live-gmail",
            live_action="read-only Gmail API calls for priority grouping",
        )
    run_config, live = resolve_sdk_execution(
        args,
        run_config_factory=SDK_RUN_CONFIG_FACTORY,
    )
    style_context = _style_profile_context(email_style_profile)
    founder_context = founder_drafting_context(founder_fit_profile)
    source_label = _priority_grouping_source_label(args)

    def retrieve() -> list[GmailMessageEnvelope]:
        if args.live_gmail:
            return _live_gmail_envelopes_for_priority_grouping(args)
        return _fixture_envelopes_for_priority_grouping(args)

    def normalize(envelopes: list[GmailMessageEnvelope]) -> GmailPriorityGroupingSDKInput:
        return GmailPriorityGroupingSDKInput.from_envelopes(
            envelopes,
            request=GT1_PRIORITY_GROUPING_PROMPT,
            lookback_days=args.lookback_days,
            source_label=source_label,
            email_style_profile=style_context,
            founder_fit_context=founder_context,
        )

    fixture_paths = list(args.fixtures or ([] if args.fixture is None else [args.fixture]))
    storage = StorageTool(args.database_url) if args.save else None
    outcome = run_retrieved_sdk_synthesis(
        agent=build_gmail_priority_grouping_agent(),
        output_type=GmailPriorityGroupingResult,
        retrieve=retrieve,
        normalize=normalize,
        input_summary="gmail GT-1 priority grouping SDK synthesis",
        input_audit_payload={
            "priority_grouping": True,
            "request": GT1_PRIORITY_GROUPING_PROMPT,
            "lookback_days": args.lookback_days,
            "source_label": source_label,
            "max_messages": args.max_messages,
            "live_gmail": bool(args.live_gmail),
            "fixture_count": len(fixture_paths),
            "fixture_names": [Path(str(path)).name for path in fixture_paths],
            "email_style_profile_fixture": args.email_style_profile_fixture,
            "email_style_profile_id": args.email_style_profile_id,
            "founder_fit_profile": founder_profile_audit_payload(
                args.founder_fit_profile,
                founder_fit_profile,
            ),
            "sdk_synthesis": True,
        },
        workflow_name="Keystone Gmail Priority Grouping SDK synthesis",
        trace_metadata={
            "workflow_kind": "gmail_priority_grouping",
            "lookback_days": args.lookback_days,
            "source_label": source_label,
        },
        run_config=run_config,
        live=live,
        trace_include_sensitive_data=args.trace_include_sensitive_data,
        save=args.save,
        storage=storage,
        model_label="sdk-live" if live else "sdk-local",
    )
    payload = sdk_synthesis_payload(
        outcome,
        include_provider_cost_window=args.include_provider_cost_window,
        provider_cost_window_seconds=args.provider_cost_window_seconds,
        openai_cost_project_id=args.openai_cost_project_id,
    )
    review_payload = None
    if args.orchestrator_review:
        review_payload = build_cli_orchestrator_review(
            args,
            run_config_factory=ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY,
            agent_name="gmail_triage",
            output=outcome.final_output,
            request_summary=GT1_PRIORITY_GROUPING_PROMPT,
            run_type="live SDK" if live else "local SDK",
        )
        payload["orchestrator_review"] = review_payload
    payload["priority_grouping"] = True
    report = _write_priority_grouping_test_pack_report(
        args=args,
        result=outcome.final_output,
        live=live,
        usage=outcome.usage,
        cost=outcome.cost,
        gemini_free_tier_usage=payload.get("gemini_free_tier_usage"),
        orchestrator_review=review_payload,
    )
    if report:
        payload["test_pack_report"] = report
    return payload


def _run_live_thread_summary(
    *,
    args: argparse.Namespace,
    gmail: Any,
    label: str,
) -> dict[str, Any]:
    if args.create_draft or args.apply_labels or args.preview_labels or args.cleanup_labels:
        return _structured_gmail_clarification(
            status="blocked",
            operation="thread_summary",
            clarification_request=(
                "Remove draft or label mutation flags and rerun the thread summary "
                "in read-only mode."
            ),
            message=(
                "Gmail thread summary mode is read-only and cannot create drafts or modify labels."
            ),
            source_label=label,
            query=args.gmail_query or "",
            reason_code="thread_summary_read_only",
            missing_inputs=[],
            suggested_next_steps=[
                "Rerun with --thread-summary only, or switch back to per-message "
                "triage for draft work."
            ],
        )

    if args.thread_id:
        thread_ids = [str(args.thread_id)]
    else:
        message_refs = gmail.list_recent_messages(
            label=label,
            max_results=args.max_messages,
            query=args.gmail_query,
        )
        thread_ids = _dedupe_thread_ids(message_refs)

    if not thread_ids:
        return _structured_gmail_clarification(
            status="blocked",
            operation="thread_summary",
            clarification_request=(
                "Broaden the Gmail query or pick a specific thread id before retrying."
            ),
            message="No Gmail thread matched the requested label and query.",
            source_label=label,
            query=args.gmail_query or "",
            reason_code="no_thread_found",
            suggested_next_steps=[
                "Adjust --gmail-query or pass --thread-id for a known thread.",
                "If you need sent mail, pass --label-filter SENT explicitly.",
            ],
        )

    summaries = [
        _thread_summary_result_from_payload(
            thread=gmail.get_thread(thread_id),
            source_label=label,
            query=args.gmail_query or "",
        ).model_dump(mode="json")
        for thread_id in thread_ids
    ]
    return {
        "mode": "live-gmail-thread-summary",
        "label": label,
        "query": args.gmail_query or "",
        "count": len(summaries),
        "threads": summaries,
        "send_enabled": False,
        "draft_created": False,
        "labels_modified": False,
    }


@with_cli_environment()
def main() -> int:
    args = _apply_live_test_defaults(build_parser().parse_args())
    if args.max_messages < 1:
        raise SystemExit("--max-messages must be at least 1.")
    if args.lookback_days < 1:
        raise SystemExit("--lookback-days must be at least 1.")
    if args.test_pack_report_dir and not args.priority_grouping:
        raise SystemExit("--test-pack-report-dir is only supported with --priority-grouping.")
    email_style_profile = _load_requested_style_profile(args)
    founder_fit_profile = load_founder_fit_profile(args.founder_fit_profile)

    if sdk_execution_requested(args):
        try:
            if args.priority_grouping:
                payload = _run_priority_grouping_sdk_synthesis(
                    args,
                    email_style_profile=email_style_profile,
                    founder_fit_profile=founder_fit_profile,
                )
            else:
                payload = _run_sdk_synthesis(
                    args,
                    email_style_profile=email_style_profile,
                    founder_fit_profile=founder_fit_profile,
                )
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        if args.markdown and not args.json:
            lines = [
                "# Gmail SDK Synthesis",
                "",
                f"Agent: {payload['agent_name']}",
                f"Live SDK: {str(payload['live_sdk']).lower()}",
                "SDK run invoked: true",
            ]
            review_markdown = render_orchestrator_output_review(payload.get("orchestrator_review"))
            if review_markdown:
                lines.extend(["", review_markdown])
            print("\n".join(lines))
        else:
            print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        return 0

    try:
        if args.live_gmail:
            require_cli_live_confirmation(
                dry_run=args.dry_run,
                live_flag=True,
                flag_name="--live-gmail",
                live_action="Gmail API calls",
            )
        if args.live_slack:
            require_cli_live_confirmation(
                dry_run=args.dry_run,
                live_flag=True,
                flag_name="--live-slack",
                live_action="posting approval notifications to Slack",
                required_credentials=SLACK_APPROVAL_CREDENTIALS,
            )
        if not args.live_gmail and not args.live_slack and not args.dry_run:
            require_cli_live_confirmation(
                dry_run=False,
                live_flag=False,
                flag_name="--live-gmail or --live-slack",
                live_action="live Gmail triage",
            )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    if args.sdk:
        agent = (
            build_gmail_priority_grouping_agent()
            if args.priority_grouping
            else build_gmail_triage_agent()
        )
        payload = {
            "mode": "sdk-agent",
            "dry_run": True,
            "live_sdk": False,
            "priority_grouping": bool(args.priority_grouping),
            "agent": sdk_agent_description(agent),
            "audit_notes": [
                "--sdk constructs the agent boundary only.",
                "Use the shared SDK synthesis harness with an explicit run_config or live=True "
                "for model execution.",
            ],
        }
        if args.markdown and not args.json:
            print(
                "\n".join(
                    [
                        "# Gmail SDK Agent",
                        "",
                        f"Agent: {payload['agent']['name']}",
                        f"Output type: {payload['agent']['output_type']}",
                        "SDK run invoked: false",
                    ]
                )
            )
        else:
            print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        return 0

    if args.priority_grouping:
        raise SystemExit("--priority-grouping requires --run-sdk or --live-sdk to invoke the LLM.")

    if args.live_gmail:
        if not args.label_filter and not args.allow_inbox:
            raise SystemExit("Use --label-filter for live Gmail, or pass --allow-inbox explicitly.")
        if args.cleanup_labels and not (args.preview_labels or args.apply_labels):
            raise SystemExit("--cleanup-labels requires --preview-labels or --apply-labels.")

        gmail = GmailTool(live=True)
        label = args.label_filter or "INBOX"
        try:
            if args.thread_summary or args.thread_id:
                payload = _run_live_thread_summary(args=args, gmail=gmail, label=label)
            else:
                message_refs = gmail.list_recent_messages(
                    label=label,
                    max_results=args.max_messages,
                    query=args.gmail_query,
                )
                if args.create_draft and not message_refs:
                    payload = _structured_gmail_clarification(
                        status="blocked",
                        operation="draft_reply",
                        clarification_request=(
                            "Adjust the label or Gmail query so one target message can be selected."
                        ),
                        message="No Gmail message matched the current draft request.",
                        source_label=label,
                        query=args.gmail_query or "",
                        reason_code="draft_target_not_found",
                        missing_inputs=["matching gmail message"],
                        suggested_next_steps=[
                            "Broaden --gmail-query, or pick a different label filter.",
                        ],
                    )
                elif args.create_draft and len(message_refs) != 1:
                    payload = _structured_gmail_clarification(
                        status="clarification_required",
                        operation="draft_reply",
                        clarification_request=(
                            "Narrow the request to one Gmail message with --gmail-query or use "
                            "--thread-id with --thread-summary before creating a draft."
                        ),
                        message=(
                            "Live Gmail draft creation requires exactly one selected message."
                        ),
                        source_label=label,
                        query=args.gmail_query or "",
                        reason_code="draft_target_ambiguous",
                        candidate_thread_ids=_dedupe_thread_ids(message_refs),
                        missing_inputs=["specific gmail_query or thread_id"],
                        suggested_next_steps=[
                            "Rerun with --max-messages 1 and a narrower --gmail-query.",
                            "Use --thread-summary --thread-id <id> to review the "
                            "full thread first.",
                        ],
                    )
                else:
                    items: list[dict[str, Any]] = []
                    for ref in message_refs:
                        message = gmail.get_message(str(ref["id"]))
                        if not message.get("id"):
                            message["id"] = str(ref["id"])
                        envelope = _thread_enriched_live_envelope(gmail, message)
                        triage = triage_gmail_message_envelope(
                            envelope,
                            email_style_profile=email_style_profile,
                        )
                        data = triage.model_dump()
                        data["approval_decision"] = (
                            "pending" if triage.draft_reply else "not_required"
                        )
                        data["approval_scope"] = ApprovalScope.SEND.value
                        if args.preview_labels or args.apply_labels:
                            data["labels_api"] = _apply_labels_with_optional_cleanup(
                                gmail,
                                message=message,
                                message_id=triage.message_id,
                                labels=triage.recommended_labels,
                                cleanup_obsolete=args.cleanup_labels,
                                dry_run_preview=args.preview_labels,
                            )
                        else:
                            data["labels_api"] = {
                                "status": "skipped",
                                "reason": (
                                    "Pass --preview-labels to preview label changes or "
                                    "--apply-labels to modify Gmail labels."
                                ),
                                "message_id": triage.message_id,
                                "labels": triage.recommended_labels,
                                "cleanup_obsolete": args.cleanup_labels,
                            }
                        if triage.draft_reply:
                            if args.create_draft:
                                data["draft_approval"] = _require_live_draft_approval(
                                    database_url=args.database_url,
                                    message_id=triage.message_id,
                                )
                                draft_api = gmail.create_draft_reply(
                                    message_id=triage.message_id,
                                    body=triage.draft_reply,
                                )
                                draft_api["sent"] = False
                                draft_api["approval_required"] = True
                                data["draft_api"] = draft_api
                            else:
                                data["draft_api"] = {
                                    "status": "skipped",
                                    "reason": "Pass --create-draft to create a Gmail draft.",
                                    "approval_required": True,
                                }
                        items.append(data)
                    payload = {
                        "mode": "live-gmail",
                        "label": label,
                        "query": args.gmail_query or "",
                        "count": len(items),
                        "messages": items,
                    }
        except (GmailAPIError, GmailConfigurationError) as exc:
            raise SystemExit(str(exc)) from exc

        if args.orchestrator_review:
            payload["orchestrator_review"] = build_cli_orchestrator_review(
                args,
                run_config_factory=ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY,
                agent_name="gmail_triage",
                output=payload,
                request_summary=f"live Gmail triage for {label}",
                run_type="live Gmail",
            )
        if args.markdown:
            if payload.get("mode") == "live-gmail-thread-summary":
                summary = _render_thread_summary_markdown(payload)
            elif payload.get("status") in {"clarification_required", "blocked"}:
                summary = _render_clarification_markdown(payload)
            else:
                messages = payload.get("messages", [])
                summary = (
                    "\n\n".join(item["summary"] for item in messages)
                    if messages
                    else "No messages found."
                )
            review_markdown = render_orchestrator_output_review(payload.get("orchestrator_review"))
            print("\n\n".join(item for item in (summary, review_markdown) if item))
        else:
            print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        return 0

    result = run_gmail_triage_fixture(
        args.fixture,
        subject=args.subject,
        sender_name=args.sender_name,
        sender_email=args.sender_email,
        email_style_profile=email_style_profile,
    )
    if args.approval_decision in TERMINAL_APPROVAL_DECISIONS and result.draft_reply:
        result = result.model_copy(
            update={
                "draft_reply": None,
                "draft_created": False,
                "approval_required": False,
                "recommended_action": (
                    f"Manual approval decision {args.approval_decision}; no draft created."
                ),
            }
        )
    data = result.model_dump()
    data["approval_decision"] = args.approval_decision
    data["approval_scope"] = args.approval_scope
    if args.orchestrator_review:
        data["orchestrator_review"] = build_cli_orchestrator_review(
            args,
            run_config_factory=ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY,
            agent_name="gmail_triage",
            output=result,
            request_summary=args.subject or args.fixture or "gmail triage fixture run",
            run_type="deterministic fixture",
        )
    if args.request_approval and (
        result.draft_reply or args.approval_decision in TERMINAL_APPROVAL_DECISIONS
    ):
        approval_request = post_approval_request(
            result,
            context={
                "object_type": "gmail_draft",
                "object_id": result.message_id,
                "summary": result.summary,
                "decision": args.approval_decision,
                "scope": args.approval_scope,
                "reviewer": args.reviewer,
                "notes": args.approval_notes,
            },
            live=args.live_slack,
        )
        data["approval_request"] = approval_request.model_dump()
        queue_item = build_approval_queue_item(
            result,
            context=_approval_context(
                object_id=result.message_id or "gmail-draft",
                summary=result.summary,
                args=args,
                slack_ts=approval_request.slack_ts,
            ),
        )
        data["approval_queue_item"] = queue_item.model_dump(mode="json")
    if args.save:
        storage = StorageTool(args.database_url)
        data["storage"] = {
            "email": storage.save_email(result),
            "agent_run": storage.save_agent_run(
                agent_name="gmail_triage",
                input_payload={
                    "fixture": args.fixture,
                    "subject": args.subject,
                    "sender_email": args.sender_email,
                    "email_style_profile_fixture": args.email_style_profile_fixture,
                    "email_style_profile_id": args.email_style_profile_id,
                },
                input_summary=args.subject or args.fixture or "gmail triage fixture run",
                output=data,
                model="fixture",
                dry_run=True,
                status="success",
            ),
        }
        if result.approval_required or args.approval_decision in TERMINAL_APPROVAL_DECISIONS:
            data["storage"]["approval"] = storage.save_approval(
                object_type="gmail_draft",
                object_id=result.message_id or data["storage"]["email"]["id"],
                decision=args.approval_decision,
                scope=args.approval_scope,
                reviewer=args.reviewer,
                notes=args.approval_notes,
                risk_flags=result.risk_flags,
                source_agent="gmail_triage",
            )
            queue_item = build_approval_queue_item(
                result,
                context=_approval_context(
                    object_id=result.message_id or data["storage"]["email"]["id"],
                    summary=result.summary,
                    args=args,
                    slack_ts=(
                        data.get("approval_request", {}).get("slack_ts")
                        if isinstance(data.get("approval_request"), dict)
                        else None
                    ),
                ),
            )
            data["storage"]["approval_queue"] = storage.save_approval_item(queue_item)
            data["approval_queue_item"] = queue_item.model_dump(mode="json")
    if args.markdown:
        report = render_gmail_triage_report(result)
        review_markdown = render_orchestrator_output_review(data.get("orchestrator_review"))
        print("\n\n".join(item for item in (report, review_markdown) if item))
    else:
        print(json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
