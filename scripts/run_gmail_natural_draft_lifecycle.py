"""Run a bounded natural-language Gmail draft create/update/cleanup lifecycle."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from keystone_agents.agents.gmail_triage import build_gmail_triage_agent
from keystone_agents.config import load_settings
from keystone_agents.context_env import context_env_value
from keystone_agents.execution_identity import create_validation_execution_identity
from keystone_agents.gmail_triage.draft_actions import (
    GMAIL_TEST_DRAFT_MARKER,
    delete_approved_gmail_test_draft,
    execute_approved_gmail_draft_action,
)
from keystone_agents.models import GmailTriageSDKInput
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.email_triage import EmailTriageResult
from keystone_agents.sdk_sessions import SDKSessionSpec, build_sdk_session
from keystone_agents.tools.gmail_tool import GmailTool

EXPECTED_MODEL = "gpt-5.4-mini"
EXPECTED_REQUESTS = 2
MAX_BUDGET_USD = 0.10


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=EXPECTED_MODEL)
    parser.add_argument("--max-openai-requests", type=int, default=EXPECTED_REQUESTS)
    parser.add_argument("--budget-usd", type=float, default=MAX_BUDGET_USD)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--session-db", required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/gmail-natural-draft-lifecycle-live.json"),
    )
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if args.model != EXPECTED_MODEL:
        raise SystemExit(f"Gmail natural draft lifecycle requires model={EXPECTED_MODEL}.")
    if args.max_openai_requests != EXPECTED_REQUESTS:
        raise SystemExit("Gmail natural draft lifecycle requires max_openai_requests=2.")
    if args.budget_usd <= 0 or args.budget_usd > MAX_BUDGET_USD:
        raise SystemExit("Gmail natural draft lifecycle requires a budget at or below $0.10.")


def _account_and_recipient() -> tuple[str, str]:
    account = (
        os.getenv("KEYSTONE_GMAIL_DRAFT_ACCOUNT", "").strip()
        or context_env_value("KNI_BUSINESS_AGENTS_GMAIL_DRAFT_ACCOUNT").strip()
        or context_env_value("GMAIL_ACCOUNT").strip()
    )
    recipient = os.getenv("KEYSTONE_GMAIL_TEST_SEND_RECIPIENT", "").strip()
    if not account or not recipient:
        raise SystemExit(
            "Gmail natural draft lifecycle requires configured draft account and exact "
            "test recipient environment values."
        )
    return account, recipient


def _typed_input(*, message_id: str, existing_draft: str = "") -> GmailTriageSDKInput:
    modifying = bool(existing_draft)
    request = (
        "Make the existing draft shorter and warmer while preserving the clinical "
        "operations and short advisory facts. Keep Sincerely, Anup. Do not send."
        if modifying
        else "Create a concise Gmail draft reply for review using the supplied clinical "
        "operations and short advisory facts. End Sincerely, Anup. Do not send."
    )
    return GmailTriageSDKInput(
        subject="KBA marked clinical operations advisory validation",
        body=(
            "Synthetic supplied context: a colleague asked to compare notes about a "
            "clinical operations workflow and a possible short advisory project."
        ),
        request=request,
        sender_name="Alex",
        sender_email="alex@example.test",
        message_id=message_id,
        thread_id=f"{message_id}-thread",
        existing_draft=existing_draft,
    )


def _run_model(
    typed_input: GmailTriageSDKInput,
    *,
    model: str,
    session: Any,
    trace_metadata: dict[str, str],
) -> Any:
    return run_typed_sdk_agent(
        agent=build_gmail_triage_agent(
            model=model,
            include_tools=False,
            request_text=typed_input.request,
        ),
        typed_input=typed_input,
        output_type=EmailTriageResult,
        live=True,
        session=session,
        workflow_name="Keystone Gmail natural draft lifecycle validation",
        trace_metadata=trace_metadata,
        max_turns=1,
    )


def _safe_provider_receipt(result: dict[str, Any]) -> dict[str, Any]:
    verification = result.get("verification") if isinstance(result, dict) else {}
    return {
        "status": result.get("status"),
        "operation": result.get("operation"),
        "draft_id_present": bool(result.get("draft_id")),
        "approval_reference": result.get("approval_reference"),
        "verification": verification,
        "sent": False,
        "send_enabled": False,
    }


def _sum_usage(results: list[Any]) -> dict[str, Any]:
    keys = (
        "requests",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    return {
        "available": all(bool(result.usage.get("available")) for result in results),
        **{
            key: sum(int(result.usage.get(key) or 0) for result in results)
            for key in keys
        },
    }


def _sum_estimated_cost(results: list[Any]) -> dict[str, Any]:
    amount = sum(float(result.cost.get("estimated_usd") or 0.0) for result in results)
    return {"currency": "USD", "estimated_usd": amount, "source": "summed_run_receipts"}


def _model_draft_checks(
    output: EmailTriageResult,
    *,
    message_id: str,
    prior_draft: str = "",
) -> dict[str, bool]:
    draft = str(output.draft_reply or "").strip()
    lowered = draft.lower()
    return {
        "same_message_identity": output.message_id == message_id,
        "draft_text_present": bool(draft),
        "clinical_operations_fact": "clinical operations" in lowered,
        "short_advisory_fact": "advisory" in lowered,
        "sincerely_anup_signoff": draft.endswith("Sincerely,\nAnup"),
        "approval_required": output.approval_required is True,
        "provider_draft_not_claimed": output.draft_created is False,
        "shorter_than_prior_when_updating": not prior_draft
        or (0 < len(draft) < len(prior_draft)),
    }


def execute_lifecycle(
    *,
    model: str,
    budget_usd: float,
    session: Any,
    provider: Any,
    account: str,
    recipient: str,
    suffix: str,
) -> dict[str, Any]:
    identity = create_validation_execution_identity(
        scenario="gmail_natural_draft_create_update_cleanup",
        route="gmail_triage",
    )
    message_id = f"synthetic-natural-draft-{suffix}"
    marker = f"{GMAIL_TEST_DRAFT_MARKER} {suffix}"
    approval = f"operator-command:gmail-draft:{suffix}"
    draft_id = ""
    create_receipt: dict[str, Any] = {}
    update_receipt: dict[str, Any] = {}
    delete_receipt: dict[str, Any] = {}
    model_results: list[Any] = []
    model_checks: dict[str, dict[str, bool]] = {}
    failure = ""
    try:
        create_model = _run_model(
            _typed_input(message_id=message_id),
            model=model,
            session=session,
            trace_metadata=identity.trace_metadata(),
        )
        model_results.append(create_model)
        initial_draft = str(create_model.final_output.draft_reply or "").strip()
        model_checks["create"] = _model_draft_checks(
            create_model.final_output,
            message_id=message_id,
        )
        if not all(model_checks["create"].values()):
            raise RuntimeError("Gmail model initial draft failed bounded acceptance checks.")
        create_receipt = execute_approved_gmail_draft_action(
            provider,
            to=recipient,
            subject=f"{marker} natural draft validation",
            body=f"{marker}\n{initial_draft}",
            expected_account=account,
            approval_reference=f"{approval}:create",
        )
        draft_id = str(create_receipt.get("draft_id") or "")
        if not create_receipt.get("verification", {}).get("passed") or not draft_id:
            raise RuntimeError("Natural Gmail draft create failed provider verification.")

        update_model = _run_model(
            _typed_input(message_id=message_id, existing_draft=initial_draft),
            model=model,
            session=session,
            trace_metadata=identity.trace_metadata(),
        )
        model_results.append(update_model)
        revised_draft = str(update_model.final_output.draft_reply or "").strip()
        model_checks["update"] = _model_draft_checks(
            update_model.final_output,
            message_id=message_id,
            prior_draft=initial_draft,
        )
        if not all(model_checks["update"].values()):
            raise RuntimeError("Gmail model revision failed bounded acceptance checks.")
        update_receipt = execute_approved_gmail_draft_action(
            provider,
            to=recipient,
            subject=f"{marker} natural draft validation updated",
            body=f"{marker}\n{revised_draft}",
            expected_account=account,
            approval_reference=f"{approval}:update",
            draft_id=draft_id,
        )
        if not update_receipt.get("verification", {}).get("passed"):
            raise RuntimeError("Natural Gmail draft update failed provider verification.")
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

    usage = _sum_usage(model_results)
    cost = _sum_estimated_cost(model_results)
    retries = sum(
        int(result.request_cache.get("rate_limit_retries") or 0)
        for result in model_results
    )
    same_draft_identity = bool(
        draft_id
        and str(create_receipt.get("draft_id") or "") == draft_id
        and str(update_receipt.get("draft_id") or "") == draft_id
    )
    passed = bool(
        not failure
        and usage["requests"] == EXPECTED_REQUESTS
        and cost["estimated_usd"] <= budget_usd
        and retries == 0
        and model_checks
        and all(all(checks.values()) for checks in model_checks.values())
        and same_draft_identity
        and create_receipt.get("verification", {}).get("passed")
        and update_receipt.get("verification", {}).get("passed")
        and delete_receipt.get("verification", {}).get("passed")
    )
    return {
        "status": "pass" if passed else "partial",
        "failure": failure,
        "scenario": "gmail_natural_draft_create_update_cleanup",
        "model": model,
        "request_ceiling": EXPECTED_REQUESTS,
        "budget_usd": budget_usd,
        "execution_identity": identity.receipt(),
        "usage": usage,
        "cost": cost,
        "rate_limit_retries": retries,
        "model_checks": model_checks,
        "provider": {
            "create": _safe_provider_receipt(create_receipt),
            "update": _safe_provider_receipt(update_receipt),
            "delete": _safe_provider_receipt(delete_receipt),
        },
        "safety": {
            "synthetic_input_only": True,
            "same_draft_identity": same_draft_identity,
            "email_sent": False,
            "send_enabled": False,
            "draft_absent_after": bool(
                delete_receipt.get("verification", {}).get("absent")
            ),
        },
    }


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def main() -> int:
    args = build_parser().parse_args()
    _validate_args(args)
    os.environ["KEYSTONE_AGENT_RUN_BUDGET_USD"] = str(args.budget_usd)
    os.environ["KEYSTONE_LIVE_MODEL_MAX_RETRIES"] = "0"
    os.environ["KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES"] = "0"
    load_settings(force_dotenv=True)
    account, recipient = _account_and_recipient()
    session_path = Path(args.session_db)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session = build_sdk_session(
        SDKSessionSpec(
            enabled=True,
            session_id=args.session_id,
            database_path=str(session_path),
            scope="gmail_natural_draft",
            source="explicit",
            history_limit=12,
        )
    )
    payload = execute_lifecycle(
        model=args.model,
        budget_usd=args.budget_usd,
        session=session,
        provider=GmailTool(live=True),
        account=account,
        recipient=recipient,
        suffix=uuid4().hex[:10],
    )
    _write_atomic(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
