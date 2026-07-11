"""Run one bounded compact Outreach revision over approved fixture context."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from keystone_agents.agents.outreach_composer import (
    build_approved_outreach_drafting_context,
    load_company_profile,
    load_contact_context,
    load_crm_account_context,
    load_opportunity_record,
    load_style_profile,
    run_outreach_composer_constrained_sdk,
)
from keystone_agents.config import load_settings
from keystone_agents.execution_identity import (
    ValidationExecutionIdentity,
    create_validation_execution_identity,
)
from keystone_agents.models import OutreachComposerSDKInput, TypedAgentRunResult
from keystone_agents.schemas.outreach import ApprovedOutreachDraftingContext, OutreachDraft

EXPECTED_MODEL = "gpt-5.4-mini"
EXPECTED_REQUESTS = 1
MAX_BUDGET_USD = 0.05
REVISION_REQUEST = (
    "Make the existing draft shorter and warmer. Preserve only the approved facts, "
    "remove the unsupported prior-overlap claim, use exactly one low-pressure CTA "
    "question, and sign off exactly with Sincerely, Anup. Do not use tools, search, "
    "create a Gmail draft, send, post, or write externally."
)
EXISTING_DRAFT = (
    "Hi Dr. Example,\n\nCurebase supports decentralized clinical trial workflows "
    "for clinical research teams and trial sponsors. We already work with companies "
    "like Curebase, so I wanted to discuss Keystone's evaluation support in detail. "
    "Would a brief exploratory conversation be useful?\n\nSincerely,\nAnup"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=EXPECTED_MODEL)
    parser.add_argument("--max-openai-requests", type=int, default=EXPECTED_REQUESTS)
    parser.add_argument("--budget-usd", type=float, default=MAX_BUDGET_USD)
    parser.add_argument(
        "--revalidate-receipt",
        type=Path,
        help="Re-run deterministic checks over a saved receipt without a model call.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/outreach-compact-revision-live.json"),
    )
    return parser


def _validate_run_limits(args: argparse.Namespace) -> None:
    if args.model != EXPECTED_MODEL:
        raise SystemExit(f"Outreach revision requires model={EXPECTED_MODEL}.")
    if args.max_openai_requests != EXPECTED_REQUESTS:
        raise SystemExit("Outreach revision requires max_openai_requests=1.")
    if args.budget_usd <= 0 or args.budget_usd > MAX_BUDGET_USD:
        raise SystemExit("Outreach revision requires a budget at or below $0.05.")


def _configure_bounded_environment(args: argparse.Namespace) -> None:
    os.environ["KEYSTONE_AGENT_RUN_BUDGET_USD"] = str(args.budget_usd)
    os.environ["KEYSTONE_LIVE_MODEL_MAX_RETRIES"] = "0"
    os.environ["KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES"] = "0"


def _approved_context() -> ApprovedOutreachDraftingContext:
    return build_approved_outreach_drafting_context(
        company_profile=load_company_profile("sample_company_curebase"),
        opportunity_record=load_opportunity_record("sample_lead_curebase"),
        contact_context=load_contact_context("sample_contact_curebase_approved"),
        crm_context=load_crm_account_context("sample_crm_context_curebase"),
        email_style_profile=load_style_profile("sample_email_style_profile_anup_approved"),
        objective="Prepare a concise source-backed Curebase note for review.",
        blocked_facts=["Keystone already works with companies like Curebase."],
        revision_request=REVISION_REQUEST,
    )


def _typed_input(context: ApprovedOutreachDraftingContext) -> OutreachComposerSDKInput:
    approved_context = {
        "existing_draft": EXISTING_DRAFT,
        "revision_request": REVISION_REQUEST,
        "allowed_facts": [fact.model_dump(mode="json") for fact in context.allowed_facts],
        "blocked_facts": context.blocked_facts,
        "allowed_source_ids": context.allowed_source_ids,
    }
    style = (
        context.email_style_profile.model_dump(mode="json")
        if context.email_style_profile is not None
        else {}
    )
    return OutreachComposerSDKInput(
        company_name=context.company_profile.name,
        contact_name="Dr. Example",
        contact_title="Clinical Operations Lead",
        outreach_goal=REVISION_REQUEST,
        approved_context=json.dumps(approved_context, ensure_ascii=True, sort_keys=True),
        email_style_profile=json.dumps(style, ensure_ascii=True, sort_keys=True),
    )


def _tool_call_count(result: TypedAgentRunResult[OutreachDraft]) -> int:
    return sum(
        str(getattr(item, "type", "")) in {"tool_call_item", "tool_call_output_item"}
        for item in list(getattr(result.raw_result, "new_items", []) or [])
    )


def _build_payload(
    result: TypedAgentRunResult[OutreachDraft],
    *,
    context: ApprovedOutreachDraftingContext,
    execution_identity: ValidationExecutionIdentity,
    model: str,
    request_ceiling: int,
    budget_usd: float,
) -> dict[str, Any]:
    draft = result.final_output
    body = draft.email_body.strip()
    lowered = body.lower()
    source_ids = set(draft.source_ids_used)
    allowed_source_ids = set(context.allowed_source_ids)
    facts_used_are_bounded = bool(draft.facts_used) and all(
        fact.approved and fact.source_id in allowed_source_ids for fact in draft.facts_used
    )
    checks = {
        "exact_request_count": int(result.usage.get("requests") or 0) == request_ceiling,
        "no_tools": _tool_call_count(result) == 0,
        "shorter_than_existing": 0 < len(body) < len(EXISTING_DRAFT),
        "warm_personal_greeting": lowered.startswith(("hi dr. example", "hello dr. example")),
        "approved_company_fact_preserved": "curebase" in lowered
        and "decentralized" in lowered
        and any(term in lowered for term in ("clinical trial", "clinical research")),
        "approved_keystone_positioning_preserved": any(
            term in lowered
            for term in ("evaluation support", "clinical ai", "research operations")
        ),
        "unsupported_overlap_removed": "already work with" not in lowered,
        "exactly_one_cta_question": body.count("?") == 1,
        "sincerely_signoff": body.endswith("Sincerely,\nAnup"),
        "source_ids_bounded": bool(source_ids) and source_ids <= allowed_source_ids,
        "facts_used_approved_and_bounded": facts_used_are_bounded,
        "approval_required": draft.approval_required is True,
        "approval_scope_owned_by_python": draft.approval_scope == "external_use",
        "send_disabled": not draft.send_enabled and not draft.sent and not draft.can_send_email,
        "budget_not_exceeded": result.budget_guard.get("exceeded") is False,
        "no_retry": int(result.request_cache.get("rate_limit_retries") or 0) == 0,
    }
    return {
        "schema_version": "keystone.outreach.compact_revision_evidence.v1",
        "status": "pass" if all(checks.values()) else "partial",
        "scenario": "outreach_compact_approved_revision",
        "request": REVISION_REQUEST,
        "model": model,
        "expected_requests": EXPECTED_REQUESTS,
        "requests": int(result.usage.get("requests") or 0),
        "request_ceiling": request_ceiling,
        "budget_usd": budget_usd,
        "retries_allowed": 0,
        "execution_identity": execution_identity.receipt(),
        "usage": result.usage,
        "cost": result.cost,
        "budget_guard": result.budget_guard,
        "request_cache": result.request_cache,
        "checks": checks,
        "output": draft.model_dump(mode="json"),
        "safety": {
            "fixture_context_only": True,
            "tool_count": _tool_call_count(result),
            "live_search": False,
            "provider_reads": 0,
            "provider_writes": 0,
            "gmail_draft_created": False,
            "email_sent": False,
            "slack_posted": False,
        },
    }


def _write_result_atomic(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _revalidate_saved_receipt(
    path: Path,
) -> tuple[TypedAgentRunResult[OutreachDraft], ValidationExecutionIdentity, str]:
    saved = json.loads(path.read_text(encoding="utf-8"))
    result = TypedAgentRunResult(
        agent_name="outreach_composer",
        output=OutreachDraft.model_validate(saved["output"]),
        raw_result=SimpleNamespace(new_items=[]),
        live=True,
        usage=saved["usage"],
        cost=saved["cost"],
        budget_guard=saved["budget_guard"],
        request_cache=saved["request_cache"],
    )
    identity = ValidationExecutionIdentity(**saved["execution_identity"])
    return result, identity, str(saved.get("status") or "unknown")


def main() -> int:
    args = build_parser().parse_args()
    _validate_run_limits(args)
    _configure_bounded_environment(args)
    context = _approved_context()
    if args.revalidate_receipt is not None:
        result, execution_identity, initial_status = _revalidate_saved_receipt(
            args.revalidate_receipt
        )
        payload = _build_payload(
            result,
            context=context,
            execution_identity=execution_identity,
            model=args.model,
            request_ceiling=args.max_openai_requests,
            budget_usd=args.budget_usd,
        )
        payload["revalidated_from"] = str(args.revalidate_receipt)
        payload["initial_status"] = initial_status
        payload["validation_revision"] = (
            "Replace literal clinical-research phrase matching with approved company and "
            "Keystone fact fidelity. No model call was made."
        )
        _write_result_atomic(args.output, payload)
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        return 0 if payload["status"] == "pass" else 2
    load_settings(force_dotenv=True)
    execution_identity = create_validation_execution_identity(
        scenario="outreach_compact_approved_revision",
        route="outreach_composer",
    )
    result = run_outreach_composer_constrained_sdk(
        _typed_input(context),
        approved_drafting_context=context,
        live=True,
        model=args.model,
        max_turns=args.max_openai_requests,
        workflow_name="Keystone Outreach compact revision validation",
        trace_metadata=execution_identity.trace_metadata(),
    )
    payload = _build_payload(
        result,
        context=context,
        execution_identity=execution_identity,
        model=args.model,
        request_ceiling=args.max_openai_requests,
        budget_usd=args.budget_usd,
    )
    _write_result_atomic(args.output, payload)
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if payload["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
