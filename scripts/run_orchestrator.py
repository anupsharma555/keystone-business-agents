"""Run the orchestrator router in deterministic or explicit live-SDK mode."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from keystone_agents.agents.manual_request_planner import resolve_manual_request_plan
from keystone_agents.agents.orchestrator import (
    reconcile_orchestrator_work_item_inspection,
    route_request,
    run_orchestrator_sdk,
)
from keystone_agents.config import load_settings
from keystone_agents.model_provider import get_runtime_agent_model_config
from keystone_agents.models import RunMode
from keystone_agents.reporting import render_operator_feedback_question
from keystone_agents.tools.storage_tool import StorageTool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Route a Keystone business-agent request.")
    parser.add_argument(
        "--mode", choices=[RunMode.DRY_RUN.value, RunMode.LIVE.value], default="dry-run"
    )
    parser.add_argument(
        "--input",
        default="",
        help="Request text or a local fixture path.",
    )
    parser.add_argument(
        "--live-sdk",
        action="store_true",
        help=(
            "Run the orchestrator through live SDK model execution. Loads repo .env, "
            "does not invoke specialist handoffs, and still cannot send or write externally."
        ),
    )
    parser.add_argument(
        "--live-manual-plan",
        action="store_true",
        help=(
            "Plan ambiguous manual routing with the SDK before deterministic routing. "
            "Falls back to local planning if unavailable."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    parser.add_argument(
        "--save", action="store_true", help="Save route decision and audit record to SQLite."
    )
    parser.add_argument(
        "--ask-feedback",
        action="store_true",
        help="Attach an optional operator feedback request to the route decision.",
    )
    parser.add_argument("--database-url", default=None, help="SQLite URL for --save.")
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


def _result_payload(
    *,
    mode: str,
    live_sdk: bool,
    model: object,
    output: object,
    input_text: str,
    manual_plan: object | None = None,
    sdk_result: object | None = None,
) -> dict[str, object]:
    dumped = output.model_dump(mode="json") if hasattr(output, "model_dump") else output
    payload = {
        "agent_name": "orchestrator",
        "mode": mode,
        "live_sdk": live_sdk,
        "model": model,
        "output_type": type(output).__name__,
        "input_summary": input_text[:240],
        "output": dumped,
    }
    if manual_plan is not None:
        payload["manual_request_plan"] = (
            manual_plan.model_dump(mode="json")
            if hasattr(manual_plan, "model_dump")
            else manual_plan
        )
    if sdk_result is not None:
        for field in (
            "usage",
            "cost",
            "budget_guard",
            "request_cache",
            "execution_telemetry",
            "tool_receipts",
        ):
            value = getattr(sdk_result, field, None)
            if value:
                payload[field] = value
    return payload


def _print_human_result(result: object) -> None:
    print("Agent: Keystone Orchestrator Agent")
    print(f"Route: {getattr(result, 'route', '')}")
    print(f"Target: {getattr(result, 'target_agent', None) or 'clarification'}")
    print(f"Send enabled: {getattr(result, 'send_enabled', False)}")
    retrieval_hint = getattr(result, "retrieval_hint", None)
    if retrieval_hint is not None:
        print(
            "Retrieval hint: "
            f"{json.dumps(retrieval_hint.model_dump(mode='json'), ensure_ascii=True)}"
        )
    stop_reason = getattr(result, "stop_reason", None)
    if stop_reason:
        print(f"Stop reason: {stop_reason}")
    clarification_request = getattr(result, "clarification_request", None)
    if clarification_request:
        print(f"Clarification: {clarification_request}")
    feedback_request = getattr(result, "operator_feedback_request", None)
    if feedback_request:
        print(f"Feedback object: {feedback_request.object_type} {feedback_request.object_id}")
        print(render_operator_feedback_question(feedback_request))
        print(f"Feedback tags: {', '.join(feedback_request.suggested_tags)}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == RunMode.LIVE.value and not args.live_sdk:
        raise SystemExit(
            "Use --live-sdk for explicit orchestrator model execution. "
            "Live side-effect mode is not supported."
        )

    input_text = _read_input(args.input)
    manual_plan = None
    if args.live_manual_plan:
        load_settings(force_dotenv=True)
        manual_plan = resolve_manual_request_plan(
            input_text,
            requested_agent="orchestrator",
            live=True,
        )
    elif args.live_sdk:
        manual_plan = resolve_manual_request_plan(
            input_text,
            requested_agent="orchestrator",
            live=False,
        )
    if args.live_sdk:
        load_settings(force_dotenv=True)
        model_config = get_runtime_agent_model_config("orchestrator")
        typed_result = run_orchestrator_sdk(
            input_text,
            live=True,
            manual_request_plan=manual_plan,
        )
        result = reconcile_orchestrator_work_item_inspection(
            typed_result.output,
            request_text=input_text,
            manual_request_plan=manual_plan,
            database_url=args.database_url,
        )
        payload = _result_payload(
            mode="live_sdk",
            live_sdk=True,
            model=model_config.as_log_dict(),
            output=result,
            input_text=input_text,
            manual_plan=manual_plan,
            sdk_result=typed_result,
        )
        if args.save:
            payload["storage"] = StorageTool(args.database_url).save_agent_run(
                agent_name="orchestrator",
                input_payload={"input": input_text},
                input_summary=input_text[:180],
                output=result.model_dump(mode="json"),
                model=str(model_config.model),
                dry_run=False,
                status="success",
            )
        if args.json:
            print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        else:
            _print_human_result(result)
        return 0

    if args.mode != RunMode.DRY_RUN.value:
        raise SystemExit("Only dry-run routing and --live-sdk model routing are supported.")

    result = route_request(
        input_text,
        manual_plan=manual_plan,
        use_manual_plan=args.live_manual_plan,
        include_operator_feedback_request=args.ask_feedback,
    )
    storage_result = None
    if args.save:
        storage_result = StorageTool(args.database_url).save_agent_run(
            agent_name="orchestrator",
            input_payload={"input": input_text},
            input_summary=input_text[:180],
            output=result.model_dump(),
            model="fixture",
            dry_run=True,
            status="success",
        )
    if args.json:
        print(
            json.dumps(
                _result_payload(
                    mode="dry_run",
                    live_sdk=False,
                    model="fixture",
                    output=result,
                    input_text=input_text,
                    manual_plan=manual_plan,
                ),
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
    else:
        _print_human_result(result)
    if storage_result:
        print(f"Saved: {storage_result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
