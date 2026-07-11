"""Run one bounded Gmail draft comparison using an approved aggregate SENT profile."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from keystone_agents.agents.gmail_triage import build_gmail_triage_agent
from keystone_agents.config import load_settings
from keystone_agents.execution_identity import (
    ValidationExecutionIdentity,
    create_validation_execution_identity,
)
from keystone_agents.models import GmailTriageSDKInput, TypedAgentRunResult
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.email_triage import EmailTriageResult
from keystone_agents.tools.email_style_tool import load_email_style_profile_from_storage

EXPECTED_MODEL = "gpt-5.4-mini"
EXPECTED_REQUESTS = 1
MAX_BUDGET_USD = 0.05
PROFILE_ID = "sent-style-live-20260710-v3"
REQUEST = (
    "Draft a concise reply in my approved aggregate email style. Thank Alex for the "
    "clinical operations note, say I would be glad to compare notes on a short advisory "
    "project, ask for non-sensitive context, and do not create a Gmail draft or send."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=EXPECTED_MODEL)
    parser.add_argument("--max-openai-requests", type=int, default=EXPECTED_REQUESTS)
    parser.add_argument("--budget-usd", type=float, default=MAX_BUDGET_USD)
    parser.add_argument("--profile-id", default=PROFILE_ID)
    parser.add_argument("--database-url", required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/gmail-sent-style-comparison-live.json"),
    )
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if args.model != EXPECTED_MODEL:
        raise SystemExit(f"Gmail style comparison requires model={EXPECTED_MODEL}.")
    if args.max_openai_requests != EXPECTED_REQUESTS:
        raise SystemExit("Gmail style comparison requires max_openai_requests=1.")
    if args.budget_usd <= 0 or args.budget_usd > MAX_BUDGET_USD:
        raise SystemExit("Gmail style comparison requires a budget at or below $0.05.")
    if args.profile_id != PROFILE_ID:
        raise SystemExit(f"Gmail style comparison requires profile_id={PROFILE_ID}.")


def _typed_input(profile: Any) -> GmailTriageSDKInput:
    return GmailTriageSDKInput(
        subject="Example Health clinical operations note",
        body=(
            "Synthetic supplied context: Alex shared a clinical operations workflow note "
            "and asked whether a short advisory conversation may be useful."
        ),
        request=REQUEST,
        sender_name="Alex",
        sender_email="alex@example.test",
        message_id="synthetic-style-comparison-1",
        thread_id="synthetic-style-comparison-thread-1",
        email_style_profile=json.dumps(profile.model_dump(mode="json"), sort_keys=True),
    )


def _build_payload(
    result: TypedAgentRunResult[EmailTriageResult],
    *,
    execution_identity: ValidationExecutionIdentity,
    model: str,
    budget_usd: float,
) -> dict[str, Any]:
    output = result.final_output
    draft = str(output.draft_reply or "").strip()
    lowered = draft.lower()
    checks = {
        "exact_request_count": int(result.usage.get("requests") or 0) == 1,
        "draft_present": bool(draft),
        "approved_profile_used": output.style_profile_used is True
        and output.style_profile_id == PROFILE_ID,
        "personal_greeting": lowered.startswith(("hi alex", "hello alex")),
        "clinical_operations_fact": "clinical operations" in lowered,
        "short_advisory_fact": "advisory" in lowered,
        "non_sensitive_context_cta": "non-sensitive context" in lowered,
        "sincerely_anup_signoff": draft.endswith("Sincerely,\nAnup"),
        "concise": 25 <= len(draft.split()) <= 110,
        "approval_preserved": output.approval_required is True,
        "provider_draft_not_created": output.draft_created is False,
        "no_retry": int(result.request_cache.get("rate_limit_retries") or 0) == 0,
        "budget_not_exceeded": result.budget_guard.get("exceeded") is False,
    }
    return {
        "status": "pass" if all(checks.values()) else "partial",
        "scenario": "gmail_approved_sent_style_comparison",
        "model": model,
        "requests": int(result.usage.get("requests") or 0),
        "request_ceiling": 1,
        "budget_usd": budget_usd,
        "execution_identity": execution_identity.receipt(),
        "usage": result.usage,
        "cost": result.cost,
        "budget_guard": result.budget_guard,
        "request_cache": result.request_cache,
        "checks": checks,
        "output": output.model_dump(mode="json"),
        "safety": {
            "synthetic_input_only": True,
            "tool_count": 0,
            "provider_reads": 0,
            "provider_writes": 0,
            "draft_created": output.draft_created,
            "send_enabled": False,
            "sent": False,
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
    profile = load_email_style_profile_from_storage(
        args.profile_id, database_url=args.database_url
    )
    if profile is None:
        raise SystemExit("The exact aggregate SENT style profile is not approved for drafting.")
    execution_identity = create_validation_execution_identity(
        scenario="gmail_approved_sent_style_comparison",
        route="gmail_triage",
    )
    result = run_typed_sdk_agent(
        agent=build_gmail_triage_agent(
            model=args.model,
            include_tools=False,
            request_text=REQUEST,
        ),
        typed_input=_typed_input(profile),
        output_type=EmailTriageResult,
        live=True,
        workflow_name="Keystone Gmail approved SENT style comparison",
        trace_metadata=execution_identity.trace_metadata(),
        max_turns=1,
    )
    payload = _build_payload(
        result,
        execution_identity=execution_identity,
        model=args.model,
        budget_usd=args.budget_usd,
    )
    _write_atomic(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
