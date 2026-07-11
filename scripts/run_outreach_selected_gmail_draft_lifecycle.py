"""Validate Outreach over one synthetic Gmail thread and a reversible provider draft."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from keystone_agents.agents.outreach_composer import (
    build_approved_outreach_drafting_context,
    load_style_profile,
    run_outreach_composer_constrained_sdk,
)
from keystone_agents.config import load_settings
from keystone_agents.context_env import context_env_value
from keystone_agents.execution_identity import create_validation_execution_identity
from keystone_agents.gmail_triage.draft_actions import (
    GMAIL_TEST_DRAFT_MARKER,
    GMAIL_TEST_EMAIL_MARKER,
    delete_approved_gmail_test_draft,
    execute_approved_gmail_draft_action,
)
from keystone_agents.models import OutreachComposerSDKInput
from keystone_agents.schemas.company_profile import CompanyProfile, SourceRecord
from keystone_agents.schemas.outreach import OutreachDraft
from keystone_agents.tools.gmail_tool import GmailTool

EXPECTED_MODEL = "gpt-5.4-mini"
EXPECTED_REQUESTS = 1
MAX_BUDGET_USD = 0.05


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=EXPECTED_MODEL)
    parser.add_argument("--max-openai-requests", type=int, default=EXPECTED_REQUESTS)
    parser.add_argument("--budget-usd", type=float, default=MAX_BUDGET_USD)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "artifacts/test-pack/outreach-selected-gmail-draft-lifecycle-live.json"
        ),
    )
    return parser


def _account_and_recipient(provider: Any, selected: dict[str, Any]) -> tuple[str, str]:
    account = str(provider.current_account_email() or "").strip()
    configured_account = (
        os.getenv("KEYSTONE_GMAIL_DRAFT_ACCOUNT", "").strip()
        or context_env_value("KNI_BUSINESS_AGENTS_GMAIL_DRAFT_ACCOUNT").strip()
        or context_env_value("GMAIL_ACCOUNT").strip()
    )
    if configured_account and configured_account.casefold() != account.casefold():
        raise RuntimeError("configured_gmail_account_mismatch")
    selected_recipient = str(selected.get("recipient") or "").strip()
    configured_recipient = os.getenv("KEYSTONE_GMAIL_TEST_SEND_RECIPIENT", "").strip()
    recipient = configured_recipient or selected_recipient
    if configured_recipient and configured_recipient.casefold() != selected_recipient.casefold():
        raise RuntimeError("configured_test_recipient_mismatch")
    if not account or not recipient:
        raise RuntimeError("configured_test_account_or_recipient_missing")
    return account, recipient


def _hash(value: object, *, length: int = 16) -> str:
    return hashlib.sha256(str(value or "").encode()).hexdigest()[:length]


def _select_synthetic_thread(provider: Any) -> dict[str, Any]:
    refs = provider.list_recent_messages(
        label="SENT",
        max_results=10,
        query=f'subject:"{GMAIL_TEST_EMAIL_MARKER}"',
    )
    if len(refs) != 1:
        raise RuntimeError(
            "synthetic_gmail_thread_not_unique"
            if refs
            else "synthetic_gmail_thread_not_found"
        )
    thread_id = str(refs[0].get("threadId") or refs[0].get("thread_id") or "")
    if not thread_id:
        raise RuntimeError("synthetic_gmail_thread_id_missing")
    thread = provider.get_thread(thread_id)
    messages = list(thread.get("messages") or [])
    if not messages:
        raise RuntimeError("synthetic_gmail_thread_empty")
    latest = messages[-1]
    subject = str(thread.get("subject") or latest.get("subject") or "")
    body = str(latest.get("normalized_body") or latest.get("body") or "")
    if GMAIL_TEST_EMAIL_MARKER not in subject or GMAIL_TEST_EMAIL_MARKER not in body:
        raise RuntimeError("synthetic_gmail_thread_marker_missing")
    raw_recipients = latest.get("to") or []
    recipients = (
        [str(item).strip() for item in raw_recipients if str(item).strip()]
        if isinstance(raw_recipients, list)
        else [str(raw_recipients).strip()]
        if str(raw_recipients).strip()
        else []
    )
    if len(recipients) != 1:
        raise RuntimeError("synthetic_gmail_thread_recipient_not_unique")
    return {
        "thread_id": thread_id,
        "message_id": str(latest.get("id") or ""),
        "subject": subject,
        "body": body,
        "message_count": len(messages),
        "recipient": recipients[0],
    }


def _approved_context(selected: dict[str, Any]) -> Any:
    source_id = f"gmail:selected-thread:{_hash(selected['thread_id'])}"
    claims = [
        "The selected Gmail thread is a synthetic Keystone Business Agents validation thread.",
        "The prior validation message requested no send and allowed cleanup after review.",
    ]
    profile = CompanyProfile(
        name="Keystone Business Agents validation",
        description="Synthetic operational-validation correspondence.",
        sources=[
            SourceRecord(
                source_id=source_id,
                title="Selected synthetic Gmail validation thread",
                url="gmail://selected-synthetic-validation-thread",
                source_type="user_provided",
                supported_claims=claims,
                evidence_excerpt="Synthetic validation correspondence only.",
                confidence=1.0,
            )
        ],
    )
    return build_approved_outreach_drafting_context(
        company_profile=profile,
        email_style_profile=load_style_profile("sample_email_style_profile_anup_approved"),
        objective=(
            "Draft one concise follow-up to the selected synthetic Gmail validation thread. "
            "Ask the recipient to confirm receipt, use exactly one question, end exactly "
            "Sincerely, Anup, and do not send."
        ),
        blocked_facts=["Any claim about a real client, relationship, or completed engagement."],
    )


def _typed_input(selected: dict[str, Any], context: Any) -> OutreachComposerSDKInput:
    approved_packet = {
        "selected_thread": {
            "thread_ref": _hash(selected["thread_id"]),
            "message_ref": _hash(selected["message_id"]),
            "message_count": selected["message_count"],
            "subject_marker_verified": True,
            "body_marker_verified": True,
            "synthetic_context": selected["body"][:1200],
        },
        "allowed_facts": [fact.model_dump(mode="json") for fact in context.allowed_facts],
        "blocked_facts": context.blocked_facts,
        "allowed_source_ids": context.allowed_source_ids,
    }
    style = context.email_style_profile.model_dump(mode="json")
    return OutreachComposerSDKInput(
        company_name="Keystone Business Agents validation",
        contact_name="there",
        outreach_goal=context.objective,
        approved_context=json.dumps(approved_packet, sort_keys=True),
        email_style_profile=json.dumps(style, sort_keys=True),
    )


def _run_model(
    typed_input: OutreachComposerSDKInput,
    *,
    context: Any,
    model: str,
    trace_metadata: dict[str, str],
) -> Any:
    return run_outreach_composer_constrained_sdk(
        typed_input,
        approved_drafting_context=context,
        live=True,
        model=model,
        max_turns=1,
        workflow_name="Keystone Outreach selected Gmail thread validation",
        trace_metadata=trace_metadata,
    )


def _draft_checks(draft: OutreachDraft, *, allowed_source_ids: list[str]) -> dict[str, bool]:
    body = draft.email_body.strip()
    lowered = body.lower()
    return {
        "draft_present": bool(body),
        "receipt_confirmation_cta": "confirm" in lowered and body.count("?") == 1,
        "sincerely_anup": body.endswith("Sincerely,\nAnup"),
        "source_ids_bounded": bool(draft.source_ids_used)
        and set(draft.source_ids_used) <= set(allowed_source_ids),
        "approval_required": draft.approval_required is True,
        "send_disabled": not draft.send_enabled and not draft.sent and not draft.can_send_email,
        "unsupported_relationship_absent": not any(
            phrase in lowered
            for phrase in ("our client", "worked together", "our engagement")
        ),
    }


def _safe_provider_receipt(result: dict[str, Any]) -> dict[str, Any]:
    verification = dict(result.get("verification") or {})
    return {
        "status": result.get("status"),
        "operation": result.get("operation"),
        "draft_id_present": bool(result.get("draft_id")),
        "verification": verification,
        "sent": False,
        "send_enabled": False,
    }


def execute_validation(
    *,
    model: str,
    budget_usd: float,
    provider: Any,
    account: str = "",
    recipient: str = "",
    suffix: str,
    model_runner: Callable[..., Any] = _run_model,
) -> dict[str, Any]:
    identity = create_validation_execution_identity(
        scenario="outreach_selected_gmail_thread_provider_draft",
        route="outreach_composer",
    )
    selected = _select_synthetic_thread(provider)
    if not account or not recipient:
        account, recipient = _account_and_recipient(provider, selected)
    context = _approved_context(selected)
    result = model_runner(
        _typed_input(selected, context),
        context=context,
        model=model,
        trace_metadata=identity.trace_metadata(),
    )
    draft = OutreachDraft.model_validate(result.final_output)
    model_checks = _draft_checks(draft, allowed_source_ids=context.allowed_source_ids)
    requests = int(result.usage.get("requests") or 0)
    estimated_usd = float(result.cost.get("estimated_usd") or 0.0)
    retries = int(result.request_cache.get("rate_limit_retries") or 0)
    marker = f"{GMAIL_TEST_DRAFT_MARKER} {suffix}"
    approval = f"operator-approved:selected-thread-outreach:{suffix}"
    draft_id = ""
    create_receipt: dict[str, Any] = {}
    delete_receipt: dict[str, Any] = {}
    failure = ""
    try:
        if not all(model_checks.values()):
            raise RuntimeError("outreach_selected_thread_acceptance_failed")
        if requests != EXPECTED_REQUESTS or retries or estimated_usd > budget_usd:
            raise RuntimeError("outreach_selected_thread_usage_guard_failed")
        create_receipt = execute_approved_gmail_draft_action(
            provider,
            to=recipient,
            subject=f"{marker} selected-thread follow-up",
            body=f"{marker}\n{draft.email_body.strip()}",
            expected_account=account,
            approval_reference=f"{approval}:create",
        )
        draft_id = str(create_receipt.get("draft_id") or "")
        if not draft_id or not create_receipt.get("verification", {}).get("passed"):
            raise RuntimeError("outreach_provider_draft_verification_failed")
    except Exception as exc:
        failure = str(exc)
    finally:
        if draft_id:
            try:
                delete_receipt = delete_approved_gmail_test_draft(
                    provider,
                    draft_id=draft_id,
                    expected_account=account,
                    approval_reference=f"{approval}:delete",
                )
            except Exception as exc:
                failure = f"{failure}; cleanup failed: {exc}".strip("; ")
    checks = {
        **model_checks,
        "one_unique_synthetic_thread": selected["message_count"] >= 1,
        "exact_request_count": requests == EXPECTED_REQUESTS,
        "no_retry": retries == 0,
        "budget_not_exceeded": estimated_usd <= budget_usd,
        "provider_draft_verified": bool(
            create_receipt.get("verification", {}).get("passed")
        ),
        "provider_draft_deleted": bool(
            delete_receipt.get("verification", {}).get("absent")
        ),
    }
    passed = not failure and all(checks.values())
    return {
        "status": "pass" if passed else "partial",
        "scenario": "outreach_selected_gmail_thread_provider_draft",
        "model": model,
        "requests": requests,
        "request_ceiling": EXPECTED_REQUESTS,
        "budget_usd": budget_usd,
        "usage": dict(result.usage or {}),
        "cost": dict(result.cost or {}),
        "request_cache": dict(result.request_cache or {}),
        "execution_identity": identity.receipt(),
        "selected_thread": {
            "thread_hash": _hash(selected["thread_id"]),
            "message_hash": _hash(selected["message_id"]),
            "message_count": selected["message_count"],
            "subject_marker_verified": True,
            "body_marker_verified": True,
        },
        "checks": checks,
        "provider_create": _safe_provider_receipt(create_receipt),
        "provider_cleanup": _safe_provider_receipt(delete_receipt),
        "failure": failure,
        "safety": {
            "synthetic_thread_only": True,
            "recipient_persisted": False,
            "draft_copy_persisted": False,
            "gmail_draft_created": bool(draft_id),
            "gmail_draft_absent_after": bool(
                delete_receipt.get("verification", {}).get("absent")
            ),
            "email_sent": False,
            "slack_posted": False,
            "live_search": False,
        },
    }


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def main() -> int:
    args = build_parser().parse_args()
    if args.model != EXPECTED_MODEL or args.max_openai_requests != EXPECTED_REQUESTS:
        raise SystemExit("Selected-thread Outreach validation requires one gpt-5.4-mini call.")
    if args.budget_usd <= 0 or args.budget_usd > MAX_BUDGET_USD:
        raise SystemExit("Selected-thread Outreach validation budget must be at most $0.05.")
    os.environ["KEYSTONE_AGENT_RUN_BUDGET_USD"] = str(args.budget_usd)
    os.environ["KEYSTONE_LIVE_MODEL_MAX_RETRIES"] = "0"
    os.environ["KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES"] = "0"
    load_settings(force_dotenv=True)
    provider = GmailTool(live=True)
    if args.preflight_only:
        selected = _select_synthetic_thread(provider)
        result = {
            "status": "pass",
            "scenario": "outreach_selected_gmail_thread_preflight",
            "selected_thread": {
                "thread_hash": _hash(selected["thread_id"]),
                "message_hash": _hash(selected["message_id"]),
                "message_count": selected["message_count"],
                "subject_marker_verified": True,
                "body_marker_verified": True,
            },
            "openai_requests": 0,
            "gmail_writes": 0,
            "email_sent": False,
        }
        _write_atomic(args.output, result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    result = execute_validation(
        model=args.model,
        budget_usd=args.budget_usd,
        provider=provider,
        suffix=uuid4().hex[:10],
    )
    _write_atomic(args.output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
