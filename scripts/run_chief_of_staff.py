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
from keystone_agents.cli_sdk import add_sdk_session_arguments, sdk_session_from_args
from keystone_agents.config import load_settings
from keystone_agents.model_provider import get_runtime_agent_model_config
from keystone_agents.models import RunMode
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


def _payload(
    *,
    mode: str,
    live_sdk: bool,
    model: object,
    output: object,
    input_text: str,
    quality_budget: AgentQualityBudget | None = None,
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
    return payload


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == RunMode.LIVE.value and not args.live_sdk:
        raise SystemExit(
            "Use --live-sdk for explicit Chief of Staff model execution. "
            "Live side-effect mode is not supported."
        )

    input_text = _read_input(args.input)
    if args.live_sdk:
        load_settings(force_dotenv=True)
        model_config = get_runtime_agent_model_config("chief_of_staff", model_override=args.model)
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
            },
            live=True,
            model=args.model,
            quality_budget=budget,
            session=_chief_of_staff_session_from_args(args),
            force_sdk_interpretation=True,
        )
        result = typed_result.output
        payload = _payload(
            mode="live_sdk",
            live_sdk=True,
            model=model_config.as_log_dict(),
            output=result,
            input_text=input_text,
            quality_budget=budget,
        )
    else:
        if args.mode != RunMode.DRY_RUN.value:
            raise SystemExit("Only dry-run planning and --live-sdk model planning are supported.")
        budget = chief_of_staff_quality_budget(
            args.quality,
            request_text=input_text,
            live_sdk=False,
        )
        result = plan_chief_of_staff_request(
            input_text,
            slack_repo_path=args.slack_repo_path,
            database_url=args.database_url,
        )
        payload = _payload(
            mode="dry_run",
            live_sdk=False,
            model="fixture",
            output=result,
            input_text=input_text,
            quality_budget=budget,
        )

    if args.json:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(render_chief_of_staff_result(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
