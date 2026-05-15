"""Run the KNI Chief of Staff agent in deterministic or explicit live-SDK mode."""

from __future__ import annotations

import argparse
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
        help="Run through live SDK model execution. Still cannot post or write externally.",
    )
    parser.add_argument("--model", default=None, help="Optional model override.")
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
) -> dict[str, object]:
    dumped = output.model_dump(mode="json") if hasattr(output, "model_dump") else output
    return {
        "agent_name": "chief_of_staff",
        "mode": mode,
        "live_sdk": live_sdk,
        "model": model,
        "output_type": type(output).__name__,
        "input_summary": input_text[:240],
        "send_enabled": False,
        "output": dumped,
    }


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
        typed_result = run_chief_of_staff_sdk(
            {
                "request": input_text,
                "slack_repo_path": args.slack_repo_path,
                "side_effect_policy": (
                    "read-only; no Slack post, Gmail send, calendar write, or repo write"
                ),
            },
            live=True,
            model=args.model,
            session=sdk_session_from_args(
                args,
                scope="chief_of_staff",
                components=("direct-script", os.environ.get("USER", "local"), str(Path.cwd())),
                default_enabled=True,
            ),
        )
        result = typed_result.output
        payload = _payload(
            mode="live_sdk",
            live_sdk=True,
            model=model_config.as_log_dict(),
            output=result,
            input_text=input_text,
        )
    else:
        if args.mode != RunMode.DRY_RUN.value:
            raise SystemExit("Only dry-run planning and --live-sdk model planning are supported.")
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
        )

    if args.json:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(render_chief_of_staff_result(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
