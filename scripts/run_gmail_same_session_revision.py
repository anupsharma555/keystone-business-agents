"""Run one bounded live-model Gmail continuation over synthetic session state."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from keystone_agents.agents.gmail_triage import build_gmail_triage_agent
from keystone_agents.config import load_settings
from keystone_agents.execution_identity import create_validation_execution_identity
from keystone_agents.models import GmailTriageSDKInput
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.email_triage import EmailTriageResult
from keystone_agents.sdk_sessions import SDKSessionSpec, build_sdk_session

MESSAGE_ID = "synthetic-gmail-continuation-1"
SUBJECT = "Example Health clinical operations advisory request"
ORIGINAL_DRAFT = (
    "Hi Alex,\n\nThanks for reaching out about the clinical operations workflow. "
    "I would be happy to compare notes about whether a short advisory project could "
    "be useful. If helpful, please send any non-sensitive context and a few times "
    "that work.\n\nSincerely,\nAnup"
)
FOLLOWUP_REQUEST = (
    "Make the existing draft shorter and warmer, and keep its existing scheduling CTA "
    "verbatim. Preserve only the supplied facts: "
    "the inquiry concerns a clinical operations workflow and a possible short advisory "
    "project. Do not create a Gmail draft, use tools, send, search, or write externally."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--session-db", required=True)
    parser.add_argument("--model", default="gpt-5.4-mini")
    parser.add_argument("--max-openai-requests", type=int, default=1)
    parser.add_argument("--budget-usd", type=float, default=0.05)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/gmail-same-session-revision-live.json"),
    )
    parser.add_argument("--json", action="store_true")
    return parser


def _prior_result() -> dict[str, Any]:
    return {
        "message_id": MESSAGE_ID,
        "thread_id": "synthetic-gmail-thread-1",
        "received_at": "",
        "subject": SUBJECT,
        "sender_name": "Alex",
        "sender_email": "alex@example.test",
        "category": "consulting_opportunity",
        "confidence": 0.95,
        "priority": "high",
        "summary": "Synthetic inquiry about a clinical operations advisory project.",
        "thread_summary": "Synthetic single-message thread for continuation validation.",
        "thread_context": "Only synthetic supplied material is in scope.",
        "reasoning": "Prepared draft-only guidance from synthetic supplied facts.",
        "needs_reply": True,
        "recommended_labels": ["Keystone/Triage", "Keystone/Consulting Opportunity"],
        "risk_flags": [],
        "suspicious_signals": [],
        "recommended_next_agent": "human_review",
        "triage_limitations": ["Synthetic fixture context only."],
        "prior_labels": [],
        "snippet": "",
        "normalized_body": "",
        "extracted_links": [],
        "attachment_metadata": [],
        "recommended_action": "Review the draft before any external use.",
        "draft_reply": ORIGINAL_DRAFT,
        "draft_created": False,
        "style_profile_used": True,
        "style_profile_id": "sample-email-style-profile-anup-approved",
        "approval_required": True,
        "requires_human_review": True,
    }


def _seed_items() -> list[dict[str, Any]]:
    return [
        {
            "role": "user",
            "content": (
                "Using this synthetic Example Health inquiry, prepare a concise reply for "
                "review. Do not create a provider draft or send it."
            ),
        },
        {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [
                {
                    "type": "output_text",
                    "text": json.dumps(_prior_result(), ensure_ascii=True, sort_keys=True),
                    "annotations": [],
                }
            ],
        },
    ]


def _revision_checks(result: EmailTriageResult) -> dict[str, bool]:
    revised = str(result.draft_reply or "")
    lowered = revised.lower()
    missing_context = " ".join(result.human_work_context.missing_context).lower()
    return {
        "same_message_identity": result.message_id == MESSAGE_ID,
        "same_subject": result.subject == SUBJECT,
        "draft_text_present": bool(revised.strip()),
        "shorter_than_prior": 0 < len(revised) < len(ORIGINAL_DRAFT),
        "warmer_greeting": revised.lower().startswith(("hi alex", "hello alex")),
        "clinical_workflow_fact_preserved": "clinical operations" in lowered,
        "advisory_fact_preserved": "advisory" in lowered,
        "prior_draft_cta_preserved": "a few times that work" in lowered,
        "prior_draft_not_reported_missing": not (
            "existing draft" in missing_context
            and any(marker in missing_context for marker in ("not provided", "not available"))
        ),
        "provider_draft_not_created": result.draft_created is False,
        "approval_preserved": result.approval_required is True,
    }


async def _existing_draft_from_session(session: Any) -> str:
    """Resolve the latest exact-message draft from bounded local SDK history."""

    items = await session.get_items()
    for item in reversed(list(items or [])):
        if not isinstance(item, dict) or item.get("role") != "assistant":
            continue
        for content in reversed(list(item.get("content") or [])):
            if not isinstance(content, dict) or content.get("type") != "output_text":
                continue
            try:
                payload = json.loads(str(content.get("text") or ""))
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or payload.get("message_id") != MESSAGE_ID:
                continue
            draft = str(payload.get("draft_reply") or "").strip()
            if draft:
                return draft
    raise RuntimeError("No exact-message draft artifact was found in the local SDK session.")


def main() -> int:
    args = build_parser().parse_args()
    if args.model != "gpt-5.4-mini":
        raise SystemExit("Gmail continuation validation requires model=gpt-5.4-mini.")
    if args.max_openai_requests != 1:
        raise SystemExit("Gmail continuation validation requires max_openai_requests=1.")
    if args.budget_usd <= 0 or args.budget_usd > 0.05:
        raise SystemExit("Gmail continuation validation requires a budget at or below $0.05.")
    os.environ["KEYSTONE_AGENT_RUN_BUDGET_USD"] = str(args.budget_usd)
    os.environ["KEYSTONE_LIVE_MODEL_MAX_RETRIES"] = "0"
    os.environ["KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES"] = "0"
    load_settings(force_dotenv=True)
    session_path = Path(args.session_db)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session = build_sdk_session(
        SDKSessionSpec(
            enabled=True,
            session_id=args.session_id,
            database_path=str(session_path),
            scope="gmail_revision",
            source="explicit",
            history_limit=12,
        )
    )
    asyncio.run(session.add_items(_seed_items()))
    existing_draft = asyncio.run(_existing_draft_from_session(session))
    if existing_draft != ORIGINAL_DRAFT:
        raise RuntimeError("The resolved session draft does not match the seeded artifact.")

    typed_input = GmailTriageSDKInput(
        subject=SUBJECT,
        body="",
        request=FOLLOWUP_REQUEST,
        sender_name="Alex",
        sender_email="alex@example.test",
        message_id=MESSAGE_ID,
        thread_id="synthetic-gmail-thread-1",
        thread_context=(
            "The selected message identity and an existing draft are present in the "
            "attached local SDK session. Revise that prior draft; do not report it missing."
        ),
        existing_draft=existing_draft,
    )
    execution_identity = create_validation_execution_identity(
        scenario="gmail_same_session_revision",
        route="gmail_triage",
    )
    result = run_typed_sdk_agent(
        agent=build_gmail_triage_agent(
            model=args.model,
            include_tools=False,
            request_text=FOLLOWUP_REQUEST,
        ),
        typed_input=typed_input,
        output_type=EmailTriageResult,
        live=True,
        session=session,
        workflow_name="Keystone Gmail same-session revision validation",
        trace_metadata=execution_identity.trace_metadata(),
    )
    checks = _revision_checks(result.final_output)
    observed_requests = int(result.usage.get("requests") or 0)
    request_ceiling_passed = 0 < observed_requests <= args.max_openai_requests
    payload = {
        "status": "pass" if all(checks.values()) and request_ceiling_passed else "partial",
        "scenario": "gmail_same_session_revision",
        "model": args.model,
        "requests": observed_requests,
        "request_ceiling": args.max_openai_requests,
        "request_ceiling_passed": request_ceiling_passed,
        "budget_usd": args.budget_usd,
        "retries_allowed": 0,
        "execution_identity": execution_identity.receipt(),
        "usage": result.usage,
        "cost": result.cost,
        "budget_guard": result.budget_guard,
        "request_cache": result.request_cache,
        "checks": checks,
        "output": result.final_output.model_dump(mode="json"),
        "safety": {
            "synthetic_input_only": True,
            "tool_count": 0,
            "provider_reads": 0,
            "provider_writes": 0,
            "draft_created": result.final_output.draft_created,
            "send_enabled": False,
            "sent": False,
            "labels_modified": False,
        },
    }
    _write_result_atomic(args.output, payload)
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if payload["status"] == "pass" else 2


def _write_result_atomic(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
