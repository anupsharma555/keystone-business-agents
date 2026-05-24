"""Create an outreach draft with env-aware dry-run or live-test defaults."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

try:
    from rich.console import Console
except ImportError:  # pragma: no cover - keeps fixture CLI usable before dependency install.

    class Console:  # type: ignore[no-redef]
        def print(self, value: object, **_: object) -> None:
            print(value)


from keystone_agents.agents.outreach_composer import (
    DEFAULT_COMPANY_FIXTURE,
    DEFAULT_CONTACT_FIXTURE,
    DEFAULT_CRM_CONTEXT_FIXTURE,
    DEFAULT_OPPORTUNITY_FIXTURE,
    DEFAULT_RESEARCH_BRIEF_FIXTURE,
    DEFAULT_STYLE_PROFILE_FIXTURE,
    build_approved_outreach_drafting_context,
    build_outreach_composer_agent,
    build_outreach_composer_compact_synthesis_agent,
    build_outreach_composer_compact_variant_agent,
    build_follow_up_schedule_record,
    build_outreach_draft_variant_set,
    compose_outreach_draft_fixture,
    compose_outreach_draft_llm_constrained,
    derive_outreach_example_query,
    derive_outreach_variant_labels,
    load_company_profile,
    load_contact_context,
    load_crm_account_context,
    load_opportunity_record,
    load_research_brief_profile,
    load_style_profile,
    outreach_goal_for_variant,
    retrieve_outreach_example_guidance,
)
from keystone_agents.cli_orchestrator_review import (
    add_orchestrator_review_arguments,
    build_cli_orchestrator_review,
)
from keystone_agents.cli_sdk import (
    SDKRunConfigFactory,
    add_sdk_run_arguments,
    jsonable,
    reject_sdk_side_effect_flags,
    resolve_sdk_execution,
    sdk_agent_description,
    sdk_execution_requested,
    sdk_synthesis_payload,
)
from keystone_agents.config import (
    SLACK_APPROVAL_CREDENTIALS,
    cli_default_dry_run,
    cli_default_live_sdk,
    require_cli_live_confirmation,
    with_cli_environment,
)
from keystone_agents.costing import (
    fetch_provider_cost_window,
    gemini_free_tier_usage_context,
    provider_cost_window_unqueried,
)
from keystone_agents.feedback import build_operator_feedback_request
from keystone_agents.founder_profile import (
    founder_drafting_context,
    founder_profile_audit_payload,
    load_founder_fit_profile,
)
from keystone_agents.model_provider import GEMINI_PROVIDER, get_runtime_agent_model_config
from keystone_agents.models import OutreachComposerSDKInput, RunMode, TypedAgentRunResult
from keystone_agents.outreach_templates import (
    OutreachTemplateNotFoundError,
    load_outreach_template_context,
)
from keystone_agents.reporting import (
    build_outreach_oc1_test_pack_payload,
    render_orchestrator_output_review,
    render_outreach_draft_report,
    render_outreach_oc1_test_pack_report,
    to_json,
)
from keystone_agents.run import SDKSynthesisOutcome, run_retrieved_sdk_synthesis
from keystone_agents.schemas.approval import (
    ApprovalScope,
    ApprovalState,
    state_allows_drafting,
    state_allows_external_use,
)
from keystone_agents.schemas.outreach import (
    OutreachDraft,
    OutreachDraftStatusResult,
    OutreachDraftVariantSet,
    OutreachLLMDraftPayload,
    OutreachLLMVariantSetPayload,
)
from keystone_agents.tools.approval_tool import build_approval_queue_item, post_approval_request
from keystone_agents.tools.email_style_tool import load_email_style_profile_from_storage
from keystone_agents.tools.storage_tool import StorageTool
from keystone_agents.writing_style import KEYSTONE_WRITING_STYLE_GUIDANCE

APPROVAL_DECISIONS = [state.value for state in ApprovalState]
APPROVAL_SCOPES = [scope.value for scope in ApprovalScope]
TERMINAL_APPROVAL_DECISIONS = {ApprovalState.REJECTED.value, ApprovalState.EXPIRED.value}
SDK_RUN_CONFIG_FACTORY: SDKRunConfigFactory | None = None
ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY: SDKRunConfigFactory | None = None
OC1_GROUNDED_OUTREACH_PROMPT = (
    "Write a short outreach email to Curebase based only on the attached or "
    "fixture-backed research brief. Focus on Keystone's fit. Do not invent shared "
    "contacts, traction, or product details."
)


def _include_outreach_tools_for_sdk_run(*, live: bool) -> bool:
    if not live:
        return True
    model_config = get_runtime_agent_model_config("outreach_composer")
    return model_config.provider != GEMINI_PROVIDER


def _uses_compact_gemini_synthesis(*, live: bool) -> bool:
    return live and not _include_outreach_tools_for_sdk_run(live=live)


def _test_pack_report_run_type(payload: dict[str, Any]) -> str:
    if payload.get("mode") == "sdk-synthesis":
        return "live SDK" if payload.get("live_sdk") else "local SDK"
    return "deterministic fixture"


def _test_pack_report_model(payload: dict[str, Any]) -> str:
    if payload.get("mode") != "sdk-synthesis":
        return "deterministic fixture/no LLM"
    model = payload.get("model") if isinstance(payload.get("model"), dict) else {}
    provider = str(model.get("provider") or "unknown")
    name = str(model.get("name") or "unknown")
    run_mode = str(model.get("run_mode") or "sdk")
    return f"{provider}/{name} ({run_mode})"


def _test_pack_input_source(args: argparse.Namespace) -> str:
    if args.research_brief_fixture:
        return f"Research brief fixture: {args.research_brief_fixture}."
    return f"Company fixture: {args.fixture}; opportunity fixture: {args.opportunity_fixture}."


def _test_pack_report_slug(payload: dict[str, Any]) -> str:
    if payload.get("mode") == "sdk-synthesis":
        return (
            "outreach-composer-oc1-grounded-outreach-live-sdk"
            if payload.get("live_sdk")
            else "outreach-composer-oc1-grounded-outreach-local-sdk"
        )
    return "outreach-composer-oc1-grounded-outreach-dry-run"


def _write_oc1_test_pack_report(
    *,
    args: argparse.Namespace,
    payload: dict[str, Any],
) -> dict[str, str]:
    report_dir_value = str(args.test_pack_report_dir or "").strip()
    if not report_dir_value:
        return {}

    draft_payload = (
        payload.get("output") if payload.get("mode") == "sdk-synthesis" else payload.get("draft")
    )
    report_dir = Path(report_dir_value)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_payload = build_outreach_oc1_test_pack_payload(
        draft_payload if isinstance(draft_payload, dict) else None,
        run_type=_test_pack_report_run_type(payload),
        model=_test_pack_report_model(payload),
        input_summary=args.goal or OC1_GROUNDED_OUTREACH_PROMPT,
        input_source=_test_pack_input_source(args),
        command_args=list(sys.argv),
        usage=payload.get("usage") if isinstance(payload.get("usage"), dict) else None,
        cost=payload.get("cost") if isinstance(payload.get("cost"), dict) else None,
        gemini_free_tier_usage=(
            payload.get("gemini_free_tier_usage")
            if isinstance(payload.get("gemini_free_tier_usage"), dict)
            else None
        ),
        orchestrator_review=(
            payload.get("orchestrator_review")
            if isinstance(payload.get("orchestrator_review"), dict)
            else None
        ),
    )
    slug = _test_pack_report_slug(payload)
    markdown_path = report_dir / f"{slug}.md"
    json_path = report_dir / f"{slug}.json"
    markdown_path.write_text(
        render_outreach_oc1_test_pack_report(report_payload),
        encoding="utf-8",
    )
    json_path.write_text(to_json(report_payload), encoding="utf-8")
    return {
        "markdown_path": str(markdown_path),
        "json_path": str(json_path),
        "status": str(report_payload["status"]),
    }


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
            object_type="outreach_draft",
            object_id=object_id,
            source_agent="outreach_composer",
            review_stage="approval_review",
        ).model_dump(mode="json")
    return {
        "object_type": "outreach_draft",
        "object_id": object_id,
        "summary": summary,
        "decision": args.approval_decision,
        "scope": args.approval_scope,
        "reviewer": args.reviewer,
        "notes": args.approval_notes,
        "source_agent": "outreach_composer",
        "metadata": metadata,
    }


def _goal_requires_clarification(goal: str | None) -> bool:
    text = " ".join(str(goal or "").lower().split())
    if not text:
        return False
    generic_goals = {
        "write outreach",
        "write an outreach email",
        "write an email",
        "draft outreach",
        "draft an email",
        "reach out",
        "contact them",
        "follow up",
        "send a note",
    }
    return text in generic_goals


def _structured_outreach_status(
    *,
    status: str,
    reason: str,
    clarification_request: str = "",
    missing_requirements: list[str] | None = None,
    recommended_next_action: str = "",
) -> dict[str, Any]:
    return OutreachDraftStatusResult(
        status=status,
        reason=reason,
        clarification_request=clarification_request,
        missing_requirements=missing_requirements or [],
        recommended_next_action=recommended_next_action,
        approval_required=True,
        approval_scope=ApprovalScope.EXTERNAL_USE.value,
        send_enabled=False,
        draft_created=False,
    ).model_dump(mode="json")


def build_parser() -> argparse.ArgumentParser:
    dry_run_default = cli_default_dry_run()
    parser = argparse.ArgumentParser(description="Create an outreach draft in fixture mode.")
    parser.add_argument(
        "--mode", choices=[RunMode.DRY_RUN.value, RunMode.LIVE.value], default="dry-run"
    )
    parser.add_argument(
        "--fixture",
        default=DEFAULT_COMPANY_FIXTURE,
        help="Company fixture name or JSON path.",
    )
    parser.add_argument(
        "--research-brief-fixture",
        default=None,
        help=(
            "Approved local attached research brief fixture for SDK synthesis. "
            f"Example: {DEFAULT_RESEARCH_BRIEF_FIXTURE}."
        ),
    )
    parser.add_argument(
        "--opportunity-fixture",
        default=DEFAULT_OPPORTUNITY_FIXTURE,
        help="Optional opportunity fixture name or JSON path.",
    )
    parser.add_argument("--contact-name", default=None)
    parser.add_argument("--contact-title", default=None)
    parser.add_argument(
        "--contact-fixture",
        default=None,
        help=f"Optional approved local contact fixture, e.g. {DEFAULT_CONTACT_FIXTURE}.",
    )
    parser.add_argument(
        "--crm-context-fixture",
        default=None,
        help=f"Optional approved local CRM/account fixture, e.g. {DEFAULT_CRM_CONTEXT_FIXTURE}.",
    )
    parser.add_argument(
        "--email-style-profile-fixture",
        default=None,
        help=(
            "Optional approved local aggregate style fixture, "
            f"e.g. {DEFAULT_STYLE_PROFILE_FIXTURE}."
        ),
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
            "Optional approved founder-fit JSON profile. Used for reply/outreach "
            "drafting only when approved_for_drafting=true."
        ),
    )
    parser.add_argument("--goal", default=None)
    parser.add_argument("--recent-signal", default=None)
    parser.add_argument(
        "--template-id",
        default=None,
        help="Optional approved local outreach template id.",
    )
    parser.add_argument(
        "--use-example-rag",
        action="store_true",
        help="Retrieve approved local example guidance for tone and structure only.",
    )
    parser.add_argument(
        "--example-query",
        default=None,
        help="Optional local example retrieval query. Implies --use-example-rag.",
    )
    parser.add_argument(
        "--max-examples",
        type=int,
        default=2,
        help="Maximum approved local examples to retrieve.",
    )
    parser.add_argument(
        "--max-variants",
        type=int,
        default=1,
        help="Return 1 to 3 approval-gated LLM draft variants when using SDK synthesis.",
    )
    parser.add_argument(
        "--include-call-prep",
        action="store_true",
        help="Include draft-only internal call-prep material in the output.",
    )
    parser.add_argument(
        "--include-follow-up-schedule",
        action="store_true",
        help="Include data-only follow-up recommendations requiring human approval.",
    )
    parser.add_argument(
        "--follow-up-date",
        default=None,
        help="Optional YYYY-MM-DD proposed date for a data-only follow-up recommendation.",
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=dry_run_default,
        help="Use deterministic fixture mode. Defaults to KEYSTONE_DRY_RUN or true.",
    )
    parser.add_argument(
        "--sdk", action="store_true", help="Construct the SDK agent without running it."
    )
    add_sdk_run_arguments(parser)
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    parser.add_argument("--markdown", action="store_true", help="Print markdown output.")
    parser.add_argument(
        "--test-pack-report-dir",
        default=None,
        help=(
            "Write sanitized OC-1 test-pack markdown and JSON report artifacts to this directory."
        ),
    )
    add_orchestrator_review_arguments(parser)
    parser.add_argument(
        "--save", action="store_true", help="Save draft and audit records to SQLite."
    )
    parser.add_argument(
        "--create-outreach-tracking",
        action="store_true",
        help=(
            "With --save, create an initial draft_pending_approval outreach_tracking row. "
            "Does not send or schedule outreach."
        ),
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
        help="Manual external-use approval decision to apply to this fixture run.",
    )
    parser.add_argument(
        "--drafting-approval-decision",
        choices=APPROVAL_DECISIONS,
        default=ApprovalState.APPROVED_FOR_DRAFTING.value,
        help="Manual approval decision for the before-drafting checkpoint.",
    )
    parser.add_argument(
        "--approval-scope",
        choices=APPROVAL_SCOPES,
        default=ApprovalScope.EXTERNAL_USE.value,
        help="Workflow scope the external-use approval decision applies to.",
    )
    parser.add_argument("--reviewer", default="", help="Human reviewer for saved approvals.")
    parser.add_argument("--approval-notes", default="", help="Reviewer notes for saved approvals.")
    parser.add_argument(
        "--live-slack", action="store_true", help="Post approval notification to Slack."
    )
    return parser


def _apply_live_test_defaults(args: argparse.Namespace) -> argparse.Namespace:
    """Promote env-backed live test defaults for Outreach Composer live runs."""

    if not args.sdk and not sdk_execution_requested(args) and cli_default_live_sdk():
        args.live_sdk = True
    return args


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
        return load_style_profile(args.email_style_profile_fixture)
    return None


def _template_and_example_context(
    args: argparse.Namespace,
    *,
    company_profile: Any,
    opportunity_record: Any | None,
) -> dict[str, Any]:
    try:
        template_context = (
            load_outreach_template_context(args.template_id) if args.template_id else None
        )
    except OutreachTemplateNotFoundError as exc:
        raise SystemExit(str(exc)) from exc
    example_query = args.example_query
    example_requested = bool(args.use_example_rag or example_query)
    if example_requested and not example_query:
        example_query = derive_outreach_example_query(
            company_profile=company_profile,
            opportunity_record=opportunity_record,
            template_context=template_context,
            stage=(
                template_context.stage
                if template_context is not None and template_context.stage
                else args.approval_scope
            ),
            outreach_goal=args.goal,
        )
    examples = (
        retrieve_outreach_example_guidance(
            example_query or "",
            max_examples=args.max_examples,
            database_url=args.database_url,
        )
        if example_requested
        else []
    )
    return {
        "outreach_template": template_context,
        "example_guidance": examples,
        "example_query": example_query or "",
    }


def _approved_context_payload(
    args: argparse.Namespace,
    *,
    founder_fit_profile: Any | None = None,
) -> dict[str, Any]:
    brief_only = bool(args.research_brief_fixture)
    company_profile = (
        load_research_brief_profile(args.research_brief_fixture)
        if brief_only
        else load_company_profile(args.fixture)
    )
    opportunity_record = None if brief_only else load_opportunity_record(args.opportunity_fixture)
    contact_context = load_contact_context(args.contact_fixture) if args.contact_fixture else None
    crm_context = (
        load_crm_account_context(args.crm_context_fixture) if args.crm_context_fixture else None
    )
    email_style_profile = _load_requested_style_profile(args)
    retrieved_context = _template_and_example_context(
        args,
        company_profile=company_profile,
        opportunity_record=opportunity_record,
    )

    payload: dict[str, Any] = {
        "company_profile": company_profile,
        "opportunity_record": opportunity_record,
        "research_brief_only": brief_only,
        "research_brief_fixture": args.research_brief_fixture or "",
        "context_policy": (
            "Attached approved research brief only. Use only source-backed facts in "
            "the brief plus approved Keystone profile facts. Do not invent shared "
            "contacts, traction, funding, reference accounts, detailed product "
            "capabilities, partnerships, metrics, or contact details absent from the brief."
            if brief_only
            else "Approved source-backed company and opportunity context."
        ),
        "include_call_prep": args.include_call_prep,
        "include_follow_up_schedule": args.include_follow_up_schedule,
        "follow_up_date": args.follow_up_date,
        "example_query": retrieved_context["example_query"],
        "default_style_guidance": KEYSTONE_WRITING_STYLE_GUIDANCE,
    }
    if retrieved_context["outreach_template"] is not None:
        payload["outreach_template"] = retrieved_context["outreach_template"]
    if retrieved_context["example_guidance"]:
        payload["example_guidance"] = retrieved_context["example_guidance"]
    if contact_context is not None and contact_context.approved_for_personalization:
        payload["contact_context"] = contact_context
    if crm_context is not None and crm_context.approved_for_personalization:
        payload["crm_context"] = crm_context
    if email_style_profile is not None and email_style_profile.approved_for_drafting:
        payload["email_style_profile"] = email_style_profile
    if founder_fit_profile is not None and founder_fit_profile.approved_for_drafting:
        payload["founder_fit_profile"] = founder_fit_profile
    return payload


def _approved_drafting_context_from_payload(
    args: argparse.Namespace,
    context: dict[str, Any],
    *,
    objective_override: str | None = None,
    max_variants: int | None = None,
) -> Any:
    return build_approved_outreach_drafting_context(
        company_profile=context["company_profile"],
        opportunity_record=context.get("opportunity_record"),
        contact_context=context.get("contact_context"),
        crm_context=context.get("crm_context"),
        email_style_profile=context.get("email_style_profile"),
        founder_fit_profile=context.get("founder_fit_profile"),
        outreach_template=context.get("outreach_template"),
        example_guidance=context.get("example_guidance") or [],
        objective=objective_override or args.goal,
        max_variants=max_variants if max_variants is not None else args.max_variants,
    )


def _compact_gemini_outcome_to_outreach_draft(
    args: argparse.Namespace,
    outcome: SDKSynthesisOutcome,
    *,
    objective_override: str | None = None,
) -> SDKSynthesisOutcome:
    compact_payload = jsonable(outcome.final_output)
    if not isinstance(compact_payload, dict):
        raise RuntimeError("Compact Gemini outreach synthesis did not return a JSON object.")
    linkedin_note = str(compact_payload.get("linkedin_note") or "")
    if len(linkedin_note) > 300:
        compact_payload["linkedin_note"] = ""
    source_ids_used = compact_payload.get("source_ids_used")
    if isinstance(source_ids_used, list) and "keystone_profile" not in source_ids_used:
        compact_payload["source_ids_used"] = [*source_ids_used, "keystone_profile"]
    if args.contact_name and not compact_payload.get("contact_name"):
        compact_payload["contact_name"] = args.contact_name
    if args.contact_title and not compact_payload.get("contact_title"):
        compact_payload["contact_title"] = args.contact_title
    approved_context = _approved_drafting_context_from_payload(
        args,
        outcome.raw_context,
        objective_override=objective_override,
    )
    draft = compose_outreach_draft_llm_constrained(
        approved_context=approved_context,
        llm_draft_payload=compact_payload,
        fallback_to_fixture=False,
    )
    return SDKSynthesisOutcome(
        agent_name=outcome.agent_name,
        raw_context=outcome.raw_context,
        typed_input=outcome.typed_input,
        result=TypedAgentRunResult(
            agent_name=outcome.result.agent_name,
            output=draft,
            raw_result=outcome.result.raw_result,
            live=outcome.result.live,
        ),
        storage=outcome.storage,
        audit_notes=(
            *outcome.audit_notes,
            (
                "Converted compact Gemini draft payload into the full OutreachDraft "
                "schema with approved-context validators."
            ),
        ),
        model_provider=outcome.model_provider,
        model_name=outcome.model_name,
        model_run_mode=outcome.model_run_mode,
        usage=outcome.usage,
        cost=outcome.cost,
        provider_usage_context=outcome.provider_usage_context,
        started_at_unix=outcome.started_at_unix,
        ended_at_unix=outcome.ended_at_unix,
    )


def _aggregate_usage(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {
            "available": False,
            "requests": None,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cached_input_tokens": None,
            "reasoning_output_tokens": None,
        }
    if not any(bool(item.get("available")) for item in items):
        return dict(items[-1])
    summed: dict[str, Any] = {
        "available": True,
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_input_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    for item in items:
        for key in (
            "requests",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_input_tokens",
            "reasoning_output_tokens",
        ):
            try:
                summed[key] += int(item.get(key) or 0)
            except (TypeError, ValueError):
                continue
    return summed


def _aggregate_cost(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {}
    if not any(item.get("source") == "local_pricing_table" for item in items):
        return dict(items[-1])
    total_amount = 0.0
    total_estimated = 0.0
    billable_tokens = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    components = {"input": 0.0, "cached_input": 0.0, "output": 0.0}
    for item in items:
        total_amount += float(item.get("amount_usd") or 0.0)
        total_estimated += float(item.get("estimated_usd") or 0.0)
        raw_item_tokens = item.get("billable_tokens")
        item_tokens = raw_item_tokens if isinstance(raw_item_tokens, dict) else {}
        raw_item_components = item.get("components_usd")
        item_components = raw_item_components if isinstance(raw_item_components, dict) else {}
        for key in billable_tokens:
            billable_tokens[key] += int(item_tokens.get(key) or 0)
        for key in components:
            components[key] += float(item_components.get(key) or 0.0)
    sample = next(item for item in items if item.get("source") == "local_pricing_table")
    return {
        "amount_usd": round(total_amount, 10),
        "estimated_usd": round(total_estimated, 10),
        "actual_usd": None,
        "currency": str(sample.get("currency") or "USD"),
        "source": "local_pricing_table",
        "confidence": "estimate",
        "pricing_provider": sample.get("pricing_provider"),
        "pricing_model": sample.get("pricing_model"),
        "pricing_as_of": sample.get("pricing_as_of"),
        "pricing_source_url": sample.get("pricing_source_url", ""),
        "billable_tokens": billable_tokens,
        "components_usd": {key: round(value, 10) for key, value in components.items()},
        "note": (
            "Estimated aggregate cost across multiple variant runs from provider response "
            "usage and the checked-in pricing table."
        ),
    }


def _provider_cost_window_for_outcomes(
    *,
    args: argparse.Namespace,
    outcomes: list[SDKSynthesisOutcome],
) -> dict[str, Any]:
    if not args.include_provider_cost_window or not outcomes:
        return provider_cost_window_unqueried()
    provider = outcomes[0].model_provider
    started = [item.started_at_unix for item in outcomes if item.started_at_unix is not None]
    ended = [item.ended_at_unix for item in outcomes if item.ended_at_unix is not None]
    if not started or not ended:
        return provider_cost_window_unqueried()
    return fetch_provider_cost_window(
        provider=provider,
        run_started_at=min(started),
        run_ended_at=max(ended),
        window_seconds=args.provider_cost_window_seconds,
        openai_project_id=args.openai_cost_project_id,
    )


def _outreach_sdk_context_payload(
    args: argparse.Namespace,
    *,
    founder_fit_profile: Any | None,
) -> dict[str, Any]:
    return _approved_context_payload(
        args,
        founder_fit_profile=founder_fit_profile,
    )


def _outreach_sdk_input_from_context(
    args: argparse.Namespace,
    context: dict[str, Any],
    *,
    objective: str | None,
) -> OutreachComposerSDKInput:
    company_profile = context["company_profile"]
    contact_context = context.get("contact_context")
    email_style_profile = context.get("email_style_profile")
    founder_profile = context.get("founder_fit_profile")
    outreach_template = context.get("outreach_template")
    example_guidance = context.get("example_guidance") or []
    context_header = (
        "Attached approved research brief only. Use no outside company facts."
        if context.get("research_brief_only")
        else "Approved source-backed outreach drafting context."
    )
    approved_context = "\n".join(
        [
            context_header,
            str(context.get("context_policy") or "").strip(),
            f"Default Keystone writing style guidance: {KEYSTONE_WRITING_STYLE_GUIDANCE}",
            founder_drafting_context(founder_profile),
            f"{json.dumps(jsonable(context), ensure_ascii=True, sort_keys=True)}",
        ]
    )
    style_context = (
        json.dumps(
            jsonable(email_style_profile),
            ensure_ascii=True,
            sort_keys=True,
        )
        if email_style_profile is not None
        else ""
    )
    template_context = (
        json.dumps(
            jsonable(outreach_template),
            ensure_ascii=True,
            sort_keys=True,
        )
        if outreach_template is not None
        else ""
    )
    example_context = (
        json.dumps(
            {
                "example_query": context.get("example_query") or "",
                "examples": jsonable(example_guidance),
                "facts_policy": (
                    "Examples guide tone, structure, pacing, CTA, and follow-up "
                    "pattern only. They do not provide factual claims about the "
                    "current prospect."
                ),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        if example_guidance
        else ""
    )
    return OutreachComposerSDKInput(
        company_name=company_profile.name,
        contact_name=(
            args.contact_name
            or getattr(contact_context, "contact_name", None)
            or getattr(contact_context, "name", None)
        ),
        contact_title=(
            args.contact_title
            or getattr(contact_context, "role_title", None)
            or getattr(contact_context, "title", None)
        ),
        recent_signal=args.recent_signal,
        outreach_goal=objective,
        approved_context=approved_context,
        email_style_profile=style_context,
        outreach_template=template_context,
        example_guidance=example_context,
    )


def _run_single_sdk_synthesis(
    args: argparse.Namespace,
    *,
    run_config: Any | None,
    live: bool,
    founder_fit_profile: Any | None,
    objective_override: str | None = None,
    variant_label: str | None = None,
) -> SDKSynthesisOutcome:
    objective = objective_override or args.goal

    def retrieve() -> dict[str, Any]:
        return _outreach_sdk_context_payload(
            args,
            founder_fit_profile=founder_fit_profile,
        )

    def normalize(context: dict[str, Any]) -> OutreachComposerSDKInput:
        return _outreach_sdk_input_from_context(args, context, objective=objective)

    storage = StorageTool(args.database_url) if args.save else None
    compact_gemini = _uses_compact_gemini_synthesis(live=live)
    outcome = run_retrieved_sdk_synthesis(
        agent=(
            build_outreach_composer_compact_synthesis_agent()
            if compact_gemini
            else build_outreach_composer_agent(
                include_tools=_include_outreach_tools_for_sdk_run(live=live),
            )
        ),
        output_type=OutreachLLMDraftPayload if compact_gemini else OutreachDraft,
        retrieve=retrieve,
        normalize=normalize,
        input_summary=(
            f"outreach SDK synthesis for {args.fixture}"
            + (f" [{variant_label}]" if variant_label else "")
        ),
        input_audit_payload={
            "fixture": args.fixture,
            "research_brief_fixture": args.research_brief_fixture,
            "opportunity_fixture": args.opportunity_fixture,
            "contact_name": args.contact_name,
            "contact_title": args.contact_title,
            "contact_fixture": args.contact_fixture,
            "crm_context_fixture": args.crm_context_fixture,
            "email_style_profile_fixture": args.email_style_profile_fixture,
            "email_style_profile_id": args.email_style_profile_id,
            "founder_fit_profile": founder_profile_audit_payload(
                args.founder_fit_profile,
                founder_fit_profile,
            ),
            "goal": objective,
            "recent_signal": args.recent_signal,
            "template_id": args.template_id,
            "use_example_rag": bool(args.use_example_rag or args.example_query),
            "example_query": args.example_query,
            "max_examples": args.max_examples,
            "max_variants": args.max_variants,
            "variant_label": variant_label,
            "include_call_prep": args.include_call_prep,
            "include_follow_up_schedule": args.include_follow_up_schedule,
            "follow_up_date": args.follow_up_date,
            "create_outreach_tracking": args.create_outreach_tracking,
            "sdk_synthesis": True,
        },
        run_config=run_config,
        live=live,
        trace_include_sensitive_data=args.trace_include_sensitive_data,
        save=args.save,
        storage=storage,
        model_label="sdk-live" if live else "sdk-local",
    )
    if compact_gemini:
        outcome = _compact_gemini_outcome_to_outreach_draft(
            args,
            outcome,
            objective_override=objective,
        )
    outcome = _ensure_sdk_follow_up_schedule(args, outcome)
    return outcome


def _ensure_sdk_follow_up_schedule(
    args: argparse.Namespace,
    outcome: SDKSynthesisOutcome,
) -> SDKSynthesisOutcome:
    if not args.include_follow_up_schedule:
        return outcome
    draft = outcome.final_output
    if not isinstance(draft, OutreachDraft) or draft.follow_up_schedules:
        return outcome
    updated_draft = draft.model_copy(
        update={
            "follow_up_schedules": [
                build_follow_up_schedule_record(
                    draft,
                    proposed_date=args.follow_up_date,
                )
            ]
        }
    )
    return replace(
        outcome,
        result=replace(outcome.result, output=updated_draft),
    )


def _compact_variant_outcome_to_variant_set(
    args: argparse.Namespace,
    outcome: SDKSynthesisOutcome,
    *,
    variant_labels: list[str],
) -> SDKSynthesisOutcome:
    compact_payload = jsonable(outcome.final_output)
    if not isinstance(compact_payload, dict):
        raise RuntimeError("Compact outreach variant synthesis did not return a JSON object.")
    variants = compact_payload.get("variants")
    if not isinstance(variants, list):
        raise RuntimeError("Compact outreach variant synthesis did not return variants.")

    approved_context = _approved_drafting_context_from_payload(
        args,
        outcome.raw_context,
        objective_override=args.goal,
        max_variants=len(variant_labels),
    )
    drafts: list[OutreachDraft] = []
    labels: list[str] = []
    for item in variants:
        if not isinstance(item, dict):
            continue
        label = str(item.get("variant_label") or "").strip()
        draft_payload = item.get("draft")
        if not label or not isinstance(draft_payload, dict):
            continue
        linkedin_note = str(draft_payload.get("linkedin_note") or "")
        if len(linkedin_note) > 300:
            draft_payload["linkedin_note"] = ""
        source_ids_used = draft_payload.get("source_ids_used")
        if isinstance(source_ids_used, list) and "keystone_profile" not in source_ids_used:
            draft_payload["source_ids_used"] = [*source_ids_used, "keystone_profile"]
        if args.contact_name and not draft_payload.get("contact_name"):
            draft_payload["contact_name"] = args.contact_name
        if args.contact_title and not draft_payload.get("contact_title"):
            draft_payload["contact_title"] = args.contact_title
        drafts.append(
            compose_outreach_draft_llm_constrained(
                approved_context=approved_context,
                llm_draft_payload=draft_payload,
                fallback_to_fixture=False,
            )
        )
        labels.append(label)

    variant_set = build_outreach_draft_variant_set(
        approved_context=approved_context,
        variant_labels=labels or variant_labels,
        drafts=drafts,
    )
    return SDKSynthesisOutcome(
        agent_name=outcome.agent_name,
        raw_context=outcome.raw_context,
        typed_input=outcome.typed_input,
        result=TypedAgentRunResult(
            agent_name=outcome.result.agent_name,
            output=variant_set,
            raw_result=outcome.result.raw_result,
            live=outcome.result.live,
        ),
        storage=outcome.storage,
        audit_notes=(
            *outcome.audit_notes,
            "Converted compact multi-variant payload into validated OutreachDraft variants.",
        ),
        model_provider=outcome.model_provider,
        model_name=outcome.model_name,
        model_run_mode=outcome.model_run_mode,
        usage=outcome.usage,
        cost=outcome.cost,
        provider_usage_context=outcome.provider_usage_context,
        started_at_unix=outcome.started_at_unix,
        ended_at_unix=outcome.ended_at_unix,
    )


def _run_compact_variant_set_sdk_synthesis(
    args: argparse.Namespace,
    *,
    run_config: Any | None,
    live: bool,
    founder_fit_profile: Any | None,
    variant_labels: list[str],
) -> SDKSynthesisOutcome:
    objective = "\n".join(
        [
            args.goal or "Write outreach variants.",
            "Return one compact variant for each requested label.",
            f"Requested variant labels: {', '.join(variant_labels)}.",
            "Keep source_ids_used identical across all variants.",
        ]
    )

    def retrieve() -> dict[str, Any]:
        return _outreach_sdk_context_payload(
            args,
            founder_fit_profile=founder_fit_profile,
        )

    def normalize(context: dict[str, Any]) -> OutreachComposerSDKInput:
        return _outreach_sdk_input_from_context(args, context, objective=objective)

    storage = StorageTool(args.database_url) if args.save else None
    outcome = run_retrieved_sdk_synthesis(
        agent=build_outreach_composer_compact_variant_agent(),
        output_type=OutreachLLMVariantSetPayload,
        retrieve=retrieve,
        normalize=normalize,
        input_summary=f"outreach compact variant SDK synthesis for {args.fixture}",
        input_audit_payload={
            "fixture": args.fixture,
            "research_brief_fixture": args.research_brief_fixture,
            "opportunity_fixture": args.opportunity_fixture,
            "goal": args.goal,
            "variant_labels": variant_labels,
            "max_variants": len(variant_labels),
            "sdk_synthesis": True,
            "single_pass_variant_synthesis": True,
        },
        run_config=run_config,
        live=live,
        trace_include_sensitive_data=args.trace_include_sensitive_data,
        save=args.save,
        storage=storage,
        model_label="sdk-live" if live else "sdk-local",
    )
    return _compact_variant_outcome_to_variant_set(
        args,
        outcome,
        variant_labels=variant_labels,
    )


def _run_sdk_synthesis(args: argparse.Namespace) -> dict[str, Any]:
    if args.create_outreach_tracking:
        raise SystemExit(
            "SDK synthesis --save records a sanitized agent-run audit row only. "
            "Use fixture mode to create outreach tracking."
        )
    if not 1 <= args.max_variants <= 3:
        raise SystemExit("--max-variants must be between 1 and 3.")
    reject_sdk_side_effect_flags(
        args,
        {
            "live_slack": "--live-slack",
            "request_approval": "--request-approval",
        },
    )
    if args.mode == RunMode.LIVE.value:
        raise SystemExit("SDK synthesis uses --live-sdk for model execution, not --mode live.")
    run_config, live = resolve_sdk_execution(
        args,
        run_config_factory=SDK_RUN_CONFIG_FACTORY,
    )
    founder_fit_profile = load_founder_fit_profile(args.founder_fit_profile)
    if args.max_variants == 1:
        outcome = _run_single_sdk_synthesis(
            args,
            run_config=run_config,
            live=live,
            founder_fit_profile=founder_fit_profile,
        )
        payload = sdk_synthesis_payload(
            outcome,
            include_provider_cost_window=args.include_provider_cost_window,
            provider_cost_window_seconds=args.provider_cost_window_seconds,
            openai_cost_project_id=args.openai_cost_project_id,
        )
    else:
        variant_labels = derive_outreach_variant_labels(
            objective=args.goal,
            max_variants=args.max_variants,
        )
        if live:
            outcome = _run_compact_variant_set_sdk_synthesis(
                args,
                run_config=run_config,
                live=live,
                founder_fit_profile=founder_fit_profile,
                variant_labels=variant_labels,
            )
            variant_set = outcome.final_output
            outcomes = [outcome]
            usage = outcome.usage
            cost = outcome.cost
            audit_notes = [
                f"Generated {len(variant_labels)} constrained outreach variants in one SDK call.",
                "Each variant kept approved context and draft-only safety checks unchanged.",
            ]
        else:
            outcomes = [
                _run_single_sdk_synthesis(
                    args,
                    run_config=run_config,
                    live=live,
                    founder_fit_profile=founder_fit_profile,
                    objective_override=outreach_goal_for_variant(
                        objective=args.goal,
                        variant_label=variant_label,
                    ),
                    variant_label=variant_label,
                )
                for variant_label in variant_labels
            ]
            approved_context = _approved_drafting_context_from_payload(
                args,
                outcomes[0].raw_context,
                objective_override=args.goal,
                max_variants=args.max_variants,
            )
            variant_set = build_outreach_draft_variant_set(
                approved_context=approved_context,
                variant_labels=variant_labels,
                drafts=[outcome.final_output for outcome in outcomes],
            )
            usage = _aggregate_usage([outcome.usage for outcome in outcomes])
            cost = _aggregate_cost([outcome.cost for outcome in outcomes])
            audit_notes = [
                f"Generated {len(variant_labels)} constrained outreach variants.",
                "Each variant kept approved context and draft-only safety checks unchanged.",
            ]
        payload = {
            "mode": "sdk-synthesis",
            "agent_name": "outreach_composer",
            "dry_run": not live,
            "live_sdk": live,
            "sdk_run_invoked": True,
            "model": {
                "provider": outcomes[0].model_provider,
                "name": outcomes[0].model_name,
                "run_mode": outcomes[0].model_run_mode,
            },
            "usage": jsonable(usage),
            "cost": jsonable(cost),
            "gemini_free_tier_usage": jsonable(
                gemini_free_tier_usage_context(
                    provider=outcomes[0].model_provider,
                    model=outcomes[0].model_name,
                    usage=usage,
                    observed_daily_requests=sum(
                        int(outcome.usage.get("requests") or 0) for outcome in outcomes
                    ),
                    daily_usage_source=(
                        "single_pass_variant_run" if live else "aggregated_variant_runs"
                    ),
                )
            ),
            "provider_cost_window": jsonable(
                _provider_cost_window_for_outcomes(args=args, outcomes=outcomes)
            ),
            "output_type": OutreachDraftVariantSet.__name__,
            "output": jsonable(variant_set),
            "storage": {"variant_runs": jsonable([outcome.storage for outcome in outcomes])},
            "audit_notes": [*audit_notes, *outcomes[0].audit_notes],
            "variant_labels": variant_labels,
        }
    if args.orchestrator_review:
        payload["orchestrator_review"] = build_cli_orchestrator_review(
            args,
            run_config_factory=ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY,
            agent_name="outreach_composer",
            output=payload["output"],
            request_summary=args.goal or f"outreach SDK synthesis for {args.fixture}",
            run_type="live SDK" if live else "local SDK",
        )
    report = (
        _write_oc1_test_pack_report(args=args, payload=payload) if args.max_variants == 1 else {}
    )
    if report:
        payload["test_pack_report"] = report
    return payload


@with_cli_environment()
def main() -> int:
    args = _apply_live_test_defaults(build_parser().parse_args())
    if args.create_outreach_tracking and not args.save:
        raise SystemExit("--create-outreach-tracking requires --save.")
    if args.max_variants > 1 and not sdk_execution_requested(args):
        raise SystemExit("--max-variants requires --run-sdk or --live-sdk.")
    if args.research_brief_fixture and not sdk_execution_requested(args):
        raise SystemExit(
            "--research-brief-fixture requires --run-sdk or --live-sdk so OC-1 "
            "brief-only outreach is synthesized by the LLM path."
        )
    if sdk_execution_requested(args):
        try:
            payload = _run_sdk_synthesis(args)
        except ValueError as exc:
            message = str(exc)
            if "approved source-backed company or opportunity context is required" in message:
                payload = _structured_outreach_status(
                    status="blocked",
                    reason="Approved source-backed context is required before drafting.",
                    clarification_request=(
                        "Provide an approved company profile, opportunity record, or "
                        "research brief with source-backed facts."
                    ),
                    missing_requirements=["approved source-backed company or opportunity context"],
                    recommended_next_action="Add approved context and rerun drafting.",
                )
            else:
                raise SystemExit(message) from exc
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        if args.json or not args.markdown:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            lines = [
                "# Outreach Composer SDK Synthesis",
                "",
                f"Agent: {payload['agent_name']}",
                f"Live SDK: {str(payload['live_sdk']).lower()}",
                "SDK run invoked: true",
            ]
            review_markdown = render_orchestrator_output_review(payload.get("orchestrator_review"))
            if review_markdown:
                lines.extend(["", review_markdown])
            Console().print("\n".join(lines), markup=False)
        return 0

    try:
        if args.live_slack:
            require_cli_live_confirmation(
                dry_run=args.dry_run,
                live_flag=True,
                flag_name="--live-slack",
                live_action="posting approval notifications to Slack",
                required_credentials=SLACK_APPROVAL_CREDENTIALS,
            )
        elif not args.dry_run:
            require_cli_live_confirmation(
                dry_run=False,
                live_flag=False,
                flag_name="--live-slack",
                live_action="live outreach composer side effects",
            )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    if args.mode == RunMode.LIVE.value:
        raise SystemExit(
            "Live outreach mode is not implemented. Fixture mode does not send anything."
        )
    if args.live_slack and not args.request_approval:
        raise SystemExit("--live-slack requires --request-approval.")

    drafting_allowed = state_allows_drafting(args.drafting_approval_decision)
    external_use_allowed = state_allows_external_use(args.approval_decision)
    if not drafting_allowed:
        payload: dict[str, object] = {
            "drafting_approval_decision": args.drafting_approval_decision,
            "approval_decision": args.approval_decision,
            "approval_scope": args.approval_scope,
            "approval_rationale": (
                "Outreach drafting is blocked until approved_for_drafting is recorded."
            ),
            "draft": None,
            "draft_created": False,
            "external_use_allowed": False,
            "send_enabled": False,
        }
        draft = None
    elif args.approval_decision in TERMINAL_APPROVAL_DECISIONS:
        payload: dict[str, object] = {
            "drafting_approval_decision": args.drafting_approval_decision,
            "approval_decision": args.approval_decision,
            "approval_scope": args.approval_scope,
            "approval_rationale": ("External-use approval is terminal; no new draft was created."),
            "draft": None,
            "draft_created": False,
            "external_use_allowed": external_use_allowed,
            "send_enabled": False,
        }
        draft = None
    elif _goal_requires_clarification(args.goal):
        payload = _structured_outreach_status(
            status="clarification_required",
            reason="The outreach objective is too generic to produce a grounded draft.",
            clarification_request=(
                "State the outreach angle or goal, such as Keystone's fit, a recent signal, "
                "or the specific topic to discuss."
            ),
            missing_requirements=["specific outreach objective"],
            recommended_next_action="Provide a specific outreach goal and rerun drafting.",
        )
        draft = None
    else:
        company_profile = load_company_profile(args.fixture)
        opportunity_record = load_opportunity_record(args.opportunity_fixture)
        contact_context = (
            load_contact_context(args.contact_fixture) if args.contact_fixture else None
        )
        crm_context = (
            load_crm_account_context(args.crm_context_fixture) if args.crm_context_fixture else None
        )
        email_style_profile = _load_requested_style_profile(args)
        retrieved_context = _template_and_example_context(
            args,
            company_profile=company_profile,
            opportunity_record=opportunity_record,
        )
        draft = compose_outreach_draft_fixture(
            company_profile=company_profile,
            opportunity_record=opportunity_record,
            contact_name=args.contact_name,
            contact_title=args.contact_title,
            contact_context=contact_context,
            crm_context=crm_context,
            email_style_profile=email_style_profile,
            outreach_template=retrieved_context["outreach_template"],
            example_guidance=retrieved_context["example_guidance"],
            recent_signal=args.recent_signal,
            outreach_goal=args.goal,
            include_call_prep=args.include_call_prep,
            include_follow_up_schedule=args.include_follow_up_schedule,
            follow_up_date=args.follow_up_date,
        )
        payload = {
            "drafting_approval_decision": args.drafting_approval_decision,
            "approval_decision": args.approval_decision,
            "approval_scope": args.approval_scope,
            "approval_rationale": (
                "Drafting checkpoint is approved; draft remains gated for external use."
            ),
            "draft": draft.model_dump(),
            "draft_created": True,
            "external_use_allowed": external_use_allowed,
            "send_enabled": False,
        }
        if args.request_approval:
            approval_request = post_approval_request(
                draft,
                context={
                    "object_type": "outreach_draft",
                    "object_id": draft.company_name,
                    "summary": draft.email_subject,
                    "decision": args.approval_decision,
                    "scope": args.approval_scope,
                    "reviewer": args.reviewer,
                    "notes": args.approval_notes,
                },
                live=args.live_slack,
            )
            payload["approval_request"] = approval_request.model_dump()
            queue_item = build_approval_queue_item(
                draft,
                context=_approval_context(
                    object_id=draft.company_name,
                    summary=draft.email_subject,
                    args=args,
                    slack_ts=getattr(approval_request, "slack_ts", None),
                ),
            )
            payload["approval_queue_item"] = queue_item.model_dump(mode="json")
    if args.orchestrator_review:
        payload["orchestrator_review"] = build_cli_orchestrator_review(
            args,
            run_config_factory=ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY,
            agent_name="outreach_composer",
            output=draft if draft is not None else payload,
            request_summary=args.goal or f"outreach draft for {args.fixture}",
            run_type="deterministic fixture",
        )
    if args.save:
        storage = StorageTool(args.database_url)
        payload["storage"] = {}
        if draft is not None:
            payload["storage"]["outreach_draft"] = storage.save_outreach_draft(draft)
            agent_run_output = draft.model_dump()
            if payload.get("orchestrator_review"):
                agent_run_output["orchestrator_review"] = payload["orchestrator_review"]
            payload["storage"]["agent_run"] = storage.save_agent_run(
                agent_name="outreach_composer",
                input_payload={
                    "fixture": args.fixture,
                    "opportunity_fixture": args.opportunity_fixture,
                    "contact_name": args.contact_name,
                    "contact_title": args.contact_title,
                    "contact_fixture": args.contact_fixture,
                    "crm_context_fixture": args.crm_context_fixture,
                    "email_style_profile_fixture": args.email_style_profile_fixture,
                    "email_style_profile_id": args.email_style_profile_id,
                    "goal": args.goal,
                    "recent_signal": args.recent_signal,
                    "template_id": args.template_id,
                    "use_example_rag": bool(args.use_example_rag or args.example_query),
                    "example_query": args.example_query,
                    "max_examples": args.max_examples,
                    "include_call_prep": args.include_call_prep,
                    "include_follow_up_schedule": args.include_follow_up_schedule,
                    "follow_up_date": args.follow_up_date,
                    "create_outreach_tracking": args.create_outreach_tracking,
                },
                input_summary=f"outreach draft for {draft.company_name}",
                output=agent_run_output,
                model="fixture",
                dry_run=True,
                status="success",
            )
            if args.create_outreach_tracking:
                payload["storage"]["outreach_tracking"] = storage.save_initial_outreach_tracking(
                    draft_id=payload["storage"]["outreach_draft"]["id"],
                    draft=draft,
                )
            follow_up_ids = []
            for schedule in draft.follow_up_schedules:
                updated_schedule = schedule.model_copy(
                    update={"related_draft_id": str(payload["storage"]["outreach_draft"]["id"])}
                )
                follow_up_ids.append(storage.save_follow_up_schedule(updated_schedule))
            if follow_up_ids:
                payload["storage"]["follow_up_schedules"] = follow_up_ids
        object_id = (
            payload["storage"]["outreach_draft"]["id"] if draft is not None else str(args.fixture)
        )
        payload["storage"]["approval"] = storage.save_approval(
            object_type="outreach_draft",
            object_id=object_id,
            decision=args.approval_decision,
            scope=args.approval_scope,
            reviewer=args.reviewer,
            notes=args.approval_notes,
            risk_flags=(draft.unsupported_claims_flagged if draft is not None else []),
            source_agent="outreach_composer",
        )
        queue_payload = (
            draft
            if draft is not None
            else {
                "company_name": str(args.fixture),
                "summary": "Outreach draft approval closed before drafting.",
            }
        )
        queue_item = build_approval_queue_item(
            queue_payload,
            context=_approval_context(
                object_id=object_id,
                summary=(
                    draft.email_subject
                    if draft is not None
                    else "Outreach draft approval closed before drafting."
                ),
                args=args,
                slack_ts=(
                    payload.get("approval_request", {}).get("slack_ts")
                    if isinstance(payload.get("approval_request"), dict)
                    else None
                ),
            ),
        )
        payload["storage"]["approval_queue"] = storage.save_approval_item(queue_item)
        payload["approval_queue_item"] = queue_item.model_dump(mode="json")
    if args.sdk:
        agent = build_outreach_composer_agent()
        payload["agent"] = sdk_agent_description(agent)

    report = _write_oc1_test_pack_report(args=args, payload=payload)
    if report:
        payload["test_pack_report"] = report

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        if draft is None:
            review_markdown = render_orchestrator_output_review(payload.get("orchestrator_review"))
            Console().print(
                "\n\n".join(
                    item
                    for item in (
                        f"Approval decision {args.approval_decision}; no draft created.",
                        review_markdown,
                    )
                    if item
                ),
                markup=False,
            )
        else:
            report = render_outreach_draft_report(draft)
            review_markdown = render_orchestrator_output_review(payload.get("orchestrator_review"))
            Console().print(
                "\n\n".join(item for item in (report, review_markdown) if item),
                markup=False,
            )
        if args.save:
            Console().print(f"\nSaved: {payload['storage']}", markup=False)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
