"""Run the KNI Chief of Staff agent in deterministic or explicit live-SDK mode."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from keystone_agents.agents.chief_of_staff import (
    plan_chief_of_staff_request,
    render_chief_of_staff_result,
    run_chief_of_staff_sdk,
)
from keystone_agents.agents.manual_request_planner import resolve_manual_request_plan
from keystone_agents.agents.orchestrator import review_specialist_output
from keystone_agents.cli_sdk import add_sdk_session_arguments, sdk_session_from_args
from keystone_agents.config import load_settings
from keystone_agents.cost_tracking import parse_cost_tracking_directive
from keystone_agents.model_provider import get_runtime_agent_model_config
from keystone_agents.models import RunMode
from keystone_agents.orchestrator.preflight_context import (
    load_manual_request_plan_from_env,
    load_orchestrator_preflight_from_env,
)
from keystone_agents.quality_budget import AgentQualityBudget, chief_of_staff_quality_budget


def _chief_of_staff_session_from_args(args: argparse.Namespace) -> object | None:
    if (
        getattr(args, "sdk_session", None) is None
        and not getattr(args, "sdk_session_id", "")
        and not getattr(args, "sdk_session_db", "")
    ):
        return None
    return sdk_session_from_args(
        args,
        scope="chief_of_staff",
        components=("direct-script", os.environ.get("USER", "local"), str(Path.cwd())),
        default_enabled=False,
    )


def _approval_reference_for_request(input_text: str) -> str:
    digest = hashlib.sha256(str(input_text or "").encode("utf-8")).hexdigest()[:12]
    return f"chief-of-staff-command:{digest}"


def _requests_google_workspace_artifact(input_text: str) -> bool:
    lowered = str(input_text or "").lower()
    return any(
        marker in lowered
        for marker in (
            "google doc",
            "google docs",
            "gdrive",
            "google drive",
            "drive folder",
            "doc link",
        )
    ) and any(
        marker in lowered
        for marker in (
            "create",
            "write",
            "provide a link",
            "provide link",
            "save",
            "analysis",
            "analyze",
            "summary",
            "report",
        )
    )


def _live_side_effect_policy(input_text: str) -> str:
    if _requests_google_workspace_artifact(input_text):
        return (
            "Live internal Airtable reads are allowed for this command. Live Google "
            "Workspace folder/doc writes are allowed only for the explicitly requested "
            "internal KNIOps artifact, using the supplied approval_reference and typed "
            "Google Workspace tools. Do not post to Slack beyond the normal result, send "
            "Gmail, create calendar events, write the repo, file tax returns, make tax "
            "payments, or mutate Airtable unless separately requested."
        )
    return "read-only; no Slack post, Gmail send, calendar write, repo write, or external action"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan KNI Slack operations routing.")
    parser.add_argument(
        "--mode",
        choices=[RunMode.DRY_RUN.value, RunMode.LIVE.value],
        default=RunMode.DRY_RUN.value,
    )
    parser.add_argument("--input", default="", help="Request text or a local fixture path.")
    parser.add_argument(
        "--slack-repo-path",
        default=None,
        help="Optional Keystone Slack repo path for context tools.",
    )
    parser.add_argument("--database-url", default=None, help="SQLite URL for local state.")
    parser.add_argument(
        "--live-sdk",
        action="store_true",
        help=(
            "Run through live SDK model execution. Slack posts still require channel policy; "
            "external writes remain gated."
        ),
    )
    parser.add_argument("--model", default=None, help="Optional model override.")
    parser.add_argument(
        "--quality",
        choices=["fast", "balanced", "deep"],
        default=None,
        help="Chief of Staff quality budget for SDK planning.",
    )
    add_sdk_session_arguments(parser)
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    return parser


def _read_input(value: str) -> str:
    if not value:
        return ""
    if len(value) < 240 and "\n" not in value:
        try:
            path = Path(value)
            if path.is_file():
                return path.read_text(encoding="utf-8")
        except OSError:
            pass
    return value


def _cost_tracking_note(*, usage: object | None, cost: object | None) -> str:
    usage_data = usage if isinstance(usage, dict) else {}
    cost_data = cost if isinstance(cost, dict) else {}
    if cost_data.get("estimated_usd") is not None:
        tokens = usage_data if usage_data.get("available") else cost_data.get("billable_tokens", {})
        input_tokens = tokens.get("input_tokens")
        cached_tokens = tokens.get("cached_input_tokens")
        output_tokens = tokens.get("output_tokens")
        token_parts = []
        if input_tokens is not None:
            token_parts.append(f"input={input_tokens}")
        if cached_tokens is not None:
            token_parts.append(f"cached_input={cached_tokens}")
        if output_tokens is not None:
            token_parts.append(f"output={output_tokens}")
        token_text = f"; tokens {', '.join(token_parts)}" if token_parts else ""
        return (
            f"Run cost tracked: estimated ${float(cost_data['estimated_usd']):.6f} USD"
            f"{token_text}. This is a local pricing-table estimate, not an invoice."
        )
    note = str(cost_data.get("note") or "").strip()
    if note:
        return f"Run cost tracking requested, but no dollar estimate is available: {note}"
    return "Run cost tracking requested, but provider usage/cost metadata was not available."


def _with_cost_tracking_note(
    output: object,
    *,
    requested: bool,
    usage: object | None,
    cost: object | None,
) -> object:
    if not requested or not hasattr(output, "model_copy"):
        return output
    note = _cost_tracking_note(usage=usage, cost=cost)
    summary = str(getattr(output, "summary", "") or "").strip()
    audit_notes = list(getattr(output, "audit_notes", []) or [])
    if note not in audit_notes:
        audit_notes.append(note)
    if note not in summary:
        summary = f"{summary}\n\n{note}" if summary else note
    return output.model_copy(update={"summary": summary, "audit_notes": audit_notes})


def _payload(
    *,
    mode: str,
    live_sdk: bool,
    model: object,
    output: object,
    input_text: str,
    quality_budget: AgentQualityBudget | None = None,
    manual_request_plan: object | None = None,
    orchestrator_preflight: object | None = None,
    orchestrator_review: object | None = None,
    original_orchestrator_review: object | None = None,
    usage: object | None = None,
    cost: object | None = None,
    request_cache: object | None = None,
) -> dict[str, object]:
    dumped = output.model_dump(mode="json") if hasattr(output, "model_dump") else output
    payload: dict[str, object] = {
        "agent_name": "chief_of_staff",
        "mode": mode,
        "live_sdk": live_sdk,
        "model": model,
        "output_type": type(output).__name__,
        "input_summary": input_text[:240],
        "send_enabled": False,
        "output": dumped,
    }
    if quality_budget is not None:
        payload["quality_budget"] = quality_budget.model_dump(mode="json")
    if usage is not None:
        payload["usage"] = usage
    if cost is not None:
        payload["cost"] = cost
    if request_cache is not None:
        payload["request_cache"] = request_cache
    if manual_request_plan is not None:
        payload["manual_request_plan"] = (
            manual_request_plan.model_dump(mode="json")
            if hasattr(manual_request_plan, "model_dump")
            else manual_request_plan
        )
    if orchestrator_preflight is not None:
        payload["orchestrator_preflight"] = orchestrator_preflight
    if orchestrator_review is not None:
        payload["orchestrator_review"] = (
            orchestrator_review.model_dump(mode="json")
            if hasattr(orchestrator_review, "model_dump")
            else orchestrator_review
        )
    if original_orchestrator_review is not None:
        payload["original_orchestrator_review"] = (
            original_orchestrator_review.model_dump(mode="json")
            if hasattr(original_orchestrator_review, "model_dump")
            else original_orchestrator_review
        )
    return payload


def _chief_of_staff_output_review(
    *,
    input_text: str,
    output: object,
    run_type: str,
) -> object:
    return review_specialist_output(
        agent_name="chief_of_staff",
        output=output,
        request_summary=input_text,
        run_type=run_type,
    )


def _review_detected_unrelated_output(review: object) -> bool:
    relevance = getattr(review, "relevance", None)
    if str(getattr(relevance, "status", "") or "") != "fail":
        return False
    gaps = getattr(review, "observed_gaps", []) or []
    return any("Request/output term overlap is low" in str(gap) for gap in gaps)


def _fallback_after_unrelated_live_output(
    *,
    input_text: str,
    slack_repo_path: str | None,
    database_url: str | None,
    manual_request_plan: object | None = None,
) -> object:
    result = plan_chief_of_staff_request(
        input_text,
        slack_repo_path=slack_repo_path,
        database_url=database_url,
        manual_request_plan=manual_request_plan,
    )
    audit_notes = [
        *getattr(result, "audit_notes", []),
        (
            "Live SDK output failed orchestrator request-alignment review; "
            "deterministic Chief of Staff fallback was rendered instead."
        ),
    ]
    if hasattr(result, "model_copy"):
        return result.model_copy(update={"audit_notes": audit_notes})
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == RunMode.LIVE.value and not args.live_sdk:
        raise SystemExit(
            "Use --live-sdk for explicit Chief of Staff model execution. "
            "Live side-effect mode is not supported."
        )

    raw_input_text = _read_input(args.input)
    cost_directive = parse_cost_tracking_directive(raw_input_text)
    input_text = (cost_directive.cleaned_text or raw_input_text).strip()
    orchestrator_preflight = load_orchestrator_preflight_from_env()
    parent_manual_plan = load_manual_request_plan_from_env()
    if args.live_sdk:
        load_settings(force_dotenv=True)
        model_config = get_runtime_agent_model_config("chief_of_staff", model_override=args.model)
        manual_plan = parent_manual_plan or resolve_manual_request_plan(
            input_text,
            requested_agent="chief_of_staff",
            live=True,
            model=args.model,
        )
        budget = chief_of_staff_quality_budget(
            args.quality,
            request_text=input_text,
            live_sdk=True,
        )
        typed_result = run_chief_of_staff_sdk(
            {
                "request": input_text,
                "slack_repo_path": args.slack_repo_path,
                "approval_reference": _approval_reference_for_request(input_text),
                "side_effect_policy": _live_side_effect_policy(input_text),
                "manual_request_plan": manual_plan.model_dump(mode="json"),
                "orchestrator_preflight": orchestrator_preflight,
            },
            live=True,
            model=args.model,
            quality_budget=budget,
            session=_chief_of_staff_session_from_args(args),
            force_sdk_interpretation=True,
            manual_request_plan=manual_plan,
        )
        result = typed_result.output
        result = _with_cost_tracking_note(
            result,
            requested=cost_directive.requested,
            usage=typed_result.usage,
            cost=typed_result.cost,
        )
        review = _chief_of_staff_output_review(
            input_text=input_text,
            output=result,
            run_type="live_sdk",
        )
        original_review = None
        if _review_detected_unrelated_output(review):
            original_review = review
            result = _fallback_after_unrelated_live_output(
                input_text=input_text,
                slack_repo_path=args.slack_repo_path,
                database_url=args.database_url,
                manual_request_plan=manual_plan,
            )
            review = _chief_of_staff_output_review(
                input_text=input_text,
                output=result,
                run_type="deterministic_fallback_after_live_review",
            )
        payload = _payload(
            mode="live_sdk",
            live_sdk=True,
            model=model_config.as_log_dict(),
            output=result,
            input_text=input_text,
            quality_budget=budget,
            manual_request_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            orchestrator_review=review,
            original_orchestrator_review=original_review,
            usage=typed_result.usage,
            cost=typed_result.cost,
            request_cache=typed_result.request_cache,
        )
    else:
        if args.mode != RunMode.DRY_RUN.value:
            raise SystemExit("Only dry-run planning and --live-sdk model planning are supported.")
        budget = chief_of_staff_quality_budget(
            args.quality,
            request_text=input_text,
            live_sdk=False,
        )
        manual_plan = parent_manual_plan or resolve_manual_request_plan(
            input_text,
            requested_agent="chief_of_staff",
            live=False,
        )
        result = plan_chief_of_staff_request(
            input_text,
            slack_repo_path=args.slack_repo_path,
            database_url=args.database_url,
            manual_request_plan=manual_plan,
        )
        result = _with_cost_tracking_note(
            result,
            requested=cost_directive.requested,
            usage=None,
            cost=None,
        )
        review = _chief_of_staff_output_review(
            input_text=input_text,
            output=result,
            run_type="dry_run",
        )
        payload = _payload(
            mode="dry_run",
            live_sdk=False,
            model="fixture",
            output=result,
            input_text=input_text,
            quality_budget=budget,
            manual_request_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            orchestrator_review=review,
        )

    if args.json:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(render_chief_of_staff_result(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
