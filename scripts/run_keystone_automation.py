"""Conservative automation controller for Keystone operator-run jobs."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from keystone_agents.automation_inventory import (
    automation_spec_for_command,
    ensure_default_automation_inventory,
)
from keystone_agents.child_process import run_isolated_child_process
from keystone_agents.health import (
    SEVERITY_ERROR,
    SEVERITY_HIGH,
    format_health_report,
    run_health_check,
)
from keystone_agents.schemas.approval import ApprovalQueueStatus
from keystone_agents.schemas.automation import AutomationRun, AutomationRunStatus
from keystone_agents.storage.sqlite_store import SQLiteStore, redact_secrets
from keystone_agents.tools.slack_tool import SlackTool

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency is declared.
    load_dotenv = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK_DIR = PROJECT_ROOT / ".keystone" / "locks"
WEEKLY_STAGES = {"dry-run", "live-research"}
ANNOUNCEMENTS_STAGES = {"dry-run", "live-research"}
GMAIL_STAGES = {"label-preview", "label-apply", "draft-create"}
MAX_SCHEDULED_OPPORTUNITIES = 5
MAX_SCHEDULED_ANNOUNCEMENTS = 5
MAX_SCHEDULED_GMAIL_MESSAGES = 3
MIN_LABEL_APPLY_PREVIEWS = 2
FATAL_HEALTH_CODES = {
    "database_unhealthy",
    "imports_failed",
    "prompts_failed",
    "agent_builders_failed",
    "dry_run_scripts_failed",
    "fixtures_failed",
    "live_credentials_missing",
    "gmail_oauth_misconfigured",
    "model_provider_unhealthy",
    "python_version_unsupported",
    "slack_live_without_approval_channel",
}


@dataclass(frozen=True)
class ChildRun:
    """Result from a guarded child CLI run."""

    command: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def parsed_stdout(self) -> Any:
        text = self.stdout.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw_stdout": _redacted_excerpt(text)}


class LockFile:
    """Small exclusive lock using atomic file creation."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def __enter__(self) -> LockFile:
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self._fd)
            self._fd = None
            raise SystemExit(
                f"Automation lock is held at {self.path}. Review the active run."
            ) from exc
        os.ftruncate(self._fd, 0)
        os.write(self._fd, f"pid={os.getpid()}\n".encode())
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        # Keep the inode: unlinking a lock file permits two independent owners.


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run bounded Keystone automation stages with safety preflights."
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Dotenv file to load before preflight. Pass an empty value to skip.",
    )
    parser.add_argument(
        "--no-health-preflight",
        action="store_true",
        help="Skip offline health preflight. Intended only for focused local tests.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=900,
        help="Timeout for the delegated Keystone CLI command.",
    )
    parser.add_argument(
        "--lock-file",
        default=None,
        help="Exclusive lock path. Defaults under .keystone/locks/ by subcommand.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON automation summary.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    weekly = subparsers.add_parser(
        "weekly-opportunity",
        help="Run the scheduled Opportunity Scout -> Research -> approval workflow.",
    )
    weekly.add_argument("--stage", choices=sorted(WEEKLY_STAGES), default="dry-run")
    weekly.add_argument(
        "--topic",
        default="Find Keystone-relevant opportunities for weekly review.",
    )
    weekly.add_argument("--max-opportunities", type=int, default=3)
    weekly.add_argument("--database-url", default=None)
    weekly.add_argument("--approval-channel", default="#ai-agents-workflow")
    weekly.add_argument("--search-provider", default=None)
    weekly.add_argument("--fallback-search-provider", default=None)
    weekly.add_argument("--live-sdk", action="store_true")
    weekly.add_argument(
        "--notify-slack",
        action="store_true",
        help="Request approval notifications. Dry-run unless --live-slack is also set.",
    )
    weekly.add_argument("--live-slack", action="store_true")
    weekly.add_argument("--confirm-live", action="store_true")
    weekly.add_argument(
        "--allow-pending-approvals",
        action="store_true",
        help="Allow a scheduled run even when pending approval items already exist.",
    )
    weekly.add_argument("--failure-slack-channel", default=None)

    gmail = subparsers.add_parser(
        "gmail-triage",
        help="Run staged Gmail automation: preview, apply labels, or create approved drafts.",
    )
    gmail.add_argument("--stage", choices=sorted(GMAIL_STAGES), required=True)
    gmail.add_argument("--label-filter", required=True)
    gmail.add_argument("--gmail-query", default=None)
    gmail.add_argument("--max-messages", type=int, default=1)
    gmail.add_argument("--database-url", default=None)
    gmail.add_argument("--cleanup-labels", action="store_true")
    gmail.add_argument("--confirm-live", action="store_true")
    gmail.add_argument("--failure-slack-channel", default=None)
    gmail.add_argument(
        "--successful-preview-count",
        type=int,
        default=0,
        help="Number of reviewed successful label previews before label mutation.",
    )
    gmail.add_argument(
        "--apply-labels-approved",
        action="store_true",
        help="Operator confirmation for the label-apply stage after reviewed previews.",
    )
    gmail.add_argument(
        "--draft-create-approved",
        action="store_true",
        help="Operator confirmation for approved Gmail draft creation.",
    )

    announcements = subparsers.add_parser(
        "announcements-research",
        help="Run the scheduled announcements/preprint research synthesis workflow.",
    )
    announcements.add_argument("--stage", choices=sorted(ANNOUNCEMENTS_STAGES), default="dry-run")
    announcements.add_argument("--database-url", default=None)
    announcements.add_argument("--input-json", default="", help="Inline JSON announcement payload.")
    announcements.add_argument("--input-file", default="", help="Path to JSON announcement payload.")
    announcements.add_argument("--min-items", type=int, default=3)
    announcements.add_argument("--max-items", type=int, default=5)
    announcements.add_argument("--live-sdk", action="store_true")
    announcements.add_argument("--confirm-live", action="store_true")
    announcements.add_argument("--failure-slack-channel", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _load_env(args.env_file)
    lock_path = _lock_path(args)
    env = _automation_env(args)
    if args.command == "weekly-opportunity":
        _validate_weekly_args(args)
    elif args.command == "announcements-research":
        _validate_announcements_args(args)
    elif args.command == "gmail-triage":
        _validate_gmail_args(args)
    else:  # pragma: no cover - argparse enforces choices.
        raise SystemExit(f"Unsupported automation command: {args.command}")
    with LockFile(lock_path):
        if not args.no_health_preflight:
            _run_health_preflight(args, env)
        if args.command == "weekly-opportunity":
            _check_pending_approval_backlog(args)
            child = _run_child(_weekly_command(args), env=env, timeout_seconds=args.timeout_seconds)
        elif args.command == "announcements-research":
            child = _run_child(
                _announcements_command(args),
                env=env,
                timeout_seconds=args.timeout_seconds,
            )
        else:
            child = _run_child(_gmail_command(args), env=env, timeout_seconds=args.timeout_seconds)

    if child.returncode != 0:
        summary = _automation_summary(args, child)
        _record_automation_summary(args, child, summary)
        _notify_failure(args, child)
        if args.json:
            print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
        _print_child_failure(child)
        return child.returncode

    summary = _automation_summary(args, child)
    _record_automation_summary(args, child, summary)
    if args.json:
        print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(_format_summary(summary))
    return 0


def _load_env(env_file: str | None) -> None:
    if env_file and load_dotenv is not None:
        load_dotenv(env_file)


def _lock_path(args: argparse.Namespace) -> Path:
    if args.lock_file:
        return Path(args.lock_file).expanduser()
    if args.command == "weekly-opportunity":
        suffix = "weekly-opportunity"
    elif args.command == "announcements-research":
        suffix = "announcements-research"
    else:
        suffix = "gmail-triage"
    return DEFAULT_LOCK_DIR / f"{suffix}.lock"


def _automation_env(args: argparse.Namespace) -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    if args.command in {"weekly-opportunity", "announcements-research"} and args.stage == "dry-run":
        env.update(
            {
                "KEYSTONE_LIVE_MODE": "false",
                "KEYSTONE_DRY_RUN": "true",
                "KEYSTONE_ENABLE_LIVE_GMAIL": "false",
                "KEYSTONE_ENABLE_LIVE_RESEARCH": "false",
                "KEYSTONE_ENABLE_LIVE_SLACK": "false",
            }
        )
    elif args.command in {"weekly-opportunity", "announcements-research"}:
        env.update(
            {
                "KEYSTONE_LIVE_MODE": "true",
                "KEYSTONE_DRY_RUN": "false",
                "KEYSTONE_ENABLE_LIVE_GMAIL": "false",
                "KEYSTONE_ENABLE_LIVE_RESEARCH": "true",
                "KEYSTONE_ENABLE_LIVE_SLACK": "true" if args.live_slack else "false",
            }
        )
    else:
        env.update(
            {
                "KEYSTONE_LIVE_MODE": "true",
                "KEYSTONE_DRY_RUN": "false",
                "KEYSTONE_ENABLE_LIVE_GMAIL": "true",
                "KEYSTONE_ENABLE_LIVE_RESEARCH": "false",
                "KEYSTONE_ENABLE_LIVE_SLACK": "false",
            }
        )
    return env


def _run_health_preflight(args: argparse.Namespace, env: dict[str, str]) -> None:
    report = run_health_check(env=env, database_url=getattr(args, "database_url", None))
    fatal_messages = [
        message
        for message in report.messages
        if message.severity in {SEVERITY_ERROR, SEVERITY_HIGH} or message.code in FATAL_HEALTH_CODES
    ]
    if report.overall_status == "error" or fatal_messages:
        details = format_health_report(report, verbose=False)
        codes = ", ".join(message.code for message in fatal_messages) or report.overall_status
        raise SystemExit(f"Automation health preflight failed ({codes}).\n{details}")


def _validate_weekly_args(args: argparse.Namespace) -> None:
    if args.max_opportunities < 1 or args.max_opportunities > MAX_SCHEDULED_OPPORTUNITIES:
        raise SystemExit(
            f"--max-opportunities must be between 1 and {MAX_SCHEDULED_OPPORTUNITIES}."
        )
    if args.stage == "live-research" and not args.confirm_live:
        raise SystemExit("--stage live-research requires --confirm-live.")
    if args.stage == "dry-run" and (args.live_sdk or args.live_slack):
        raise SystemExit("Dry-run automation cannot enable --live-sdk or --live-slack.")
    if args.live_slack and not args.notify_slack:
        raise SystemExit("--live-slack requires --notify-slack.")


def _validate_gmail_args(args: argparse.Namespace) -> None:
    if args.max_messages < 1 or args.max_messages > MAX_SCHEDULED_GMAIL_MESSAGES:
        raise SystemExit(f"--max-messages must be between 1 and {MAX_SCHEDULED_GMAIL_MESSAGES}.")
    if not args.confirm_live:
        raise SystemExit("Gmail automation stages require --confirm-live.")
    if not str(args.label_filter).strip():
        raise SystemExit("Gmail automation requires a narrow --label-filter.")
    if args.stage == "label-preview":
        return
    if args.stage == "label-apply":
        if not args.apply_labels_approved:
            raise SystemExit("--stage label-apply requires --apply-labels-approved.")
        if args.successful_preview_count < MIN_LABEL_APPLY_PREVIEWS:
            raise SystemExit(
                "--stage label-apply requires at least "
                f"{MIN_LABEL_APPLY_PREVIEWS} reviewed successful previews."
            )
        return
    if args.stage == "draft-create":
        if not args.draft_create_approved:
            raise SystemExit("--stage draft-create requires --draft-create-approved.")
        if args.max_messages != 1:
            raise SystemExit("--stage draft-create requires --max-messages 1.")
        if not args.gmail_query:
            raise SystemExit("--stage draft-create requires a target --gmail-query.")


def _validate_announcements_args(args: argparse.Namespace) -> None:
    if args.min_items < 1 or args.min_items > MAX_SCHEDULED_ANNOUNCEMENTS:
        raise SystemExit(
            f"--min-items must be between 1 and {MAX_SCHEDULED_ANNOUNCEMENTS}."
        )
    if args.max_items < args.min_items or args.max_items > MAX_SCHEDULED_ANNOUNCEMENTS:
        raise SystemExit(
            f"--max-items must be between --min-items and {MAX_SCHEDULED_ANNOUNCEMENTS}."
        )
    if args.stage == "live-research" and not args.confirm_live:
        raise SystemExit("--stage live-research requires --confirm-live.")
    if args.stage == "dry-run" and args.live_sdk:
        raise SystemExit("Dry-run announcements automation cannot enable --live-sdk.")


def _check_pending_approval_backlog(args: argparse.Namespace) -> None:
    if args.command != "weekly-opportunity" or args.allow_pending_approvals:
        return
    store = SQLiteStore(args.database_url)
    pending = store.list_approval_items(status=ApprovalQueueStatus.PENDING)
    if pending:
        raise SystemExit(
            f"Pending approval backlog has {len(pending)} item(s). Review, expire, "
            "archive, or pass --allow-pending-approvals before scheduled automation."
        )


def _weekly_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "run_weekly_opportunity_workflow.py"),
        "--topic",
        args.topic,
        "--max-opportunities",
        str(args.max_opportunities),
        "--approval-channel",
        args.approval_channel,
        "--save",
        "--json",
    ]
    if args.database_url:
        command.extend(["--database-url", args.database_url])
    if args.stage == "dry-run":
        command.append("--dry-run")
    else:
        command.extend(["--no-dry-run", "--live-search"])
        if args.live_sdk:
            command.append("--live-sdk")
    if args.search_provider:
        command.extend(["--search-provider", args.search_provider])
    if args.fallback_search_provider:
        command.extend(["--fallback-search-provider", args.fallback_search_provider])
    if args.notify_slack:
        command.append("--request-approval")
    if args.live_slack:
        command.append("--live-slack")
    return command


def _announcements_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "run_multi_agent_automation.py"),
        "--kind",
        "announcements-research",
        "--json",
        "--min-items",
        str(args.min_items),
        "--max-items",
        str(args.max_items),
    ]
    if args.database_url:
        command.extend(["--database-url", args.database_url])
    if args.input_json:
        command.extend(["--input-json", args.input_json])
    if args.input_file:
        command.extend(["--input-file", args.input_file])
    if args.stage == "dry-run":
        command.append("--dry-run")
    else:
        command.extend(["--no-dry-run", "--live-search"])
        if args.live_sdk:
            command.append("--live-sdk")
    return command


def _gmail_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "run_gmail_triage.py"),
        "--live-gmail",
        "--no-dry-run",
        "--label-filter",
        args.label_filter,
        "--max-messages",
        str(args.max_messages),
        "--json",
    ]
    if args.database_url:
        command.extend(["--database-url", args.database_url])
    if args.gmail_query:
        command.extend(["--gmail-query", args.gmail_query])
    if args.stage == "label-preview":
        command.append("--preview-labels")
    elif args.stage == "label-apply":
        command.append("--apply-labels")
    elif args.stage == "draft-create":
        command.append("--create-draft")
    if args.cleanup_labels and args.stage in {"label-preview", "label-apply"}:
        command.append("--cleanup-labels")
    return command


def _run_child(command: list[str], *, env: dict[str, str], timeout_seconds: int) -> ChildRun:
    try:
        completed = run_isolated_child_process(
            command,
            env=env,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.output if isinstance(exc.output, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        message = f"Child command timed out after {timeout_seconds} seconds."
        return ChildRun(
            command=command,
            returncode=124,
            stdout=stdout,
            stderr=f"{message}\n{stderr}".strip(),
        )
    return ChildRun(
        command=command,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _notify_failure(args: argparse.Namespace, child: ChildRun) -> dict[str, Any] | None:
    channel = getattr(args, "failure_slack_channel", None)
    if not channel:
        return None
    text = (
        "Keystone automation failed before completing.\n"
        f"Command: {args.command}\n"
        f"Stage: {getattr(args, 'stage', '')}\n"
        f"Exit code: {child.returncode}\n"
        f"Error: {_redacted_excerpt(child.stderr or child.stdout)}"
    )
    return SlackTool(live=bool(getattr(args, "live_slack", False))).post_message(channel, text)


def _print_child_failure(child: ChildRun) -> None:
    if child.stderr:
        print(_redacted_excerpt(child.stderr), file=sys.stderr)
    if child.stdout:
        print(_redacted_excerpt(child.stdout), file=sys.stderr)


def _automation_summary(args: argparse.Namespace, child: ChildRun) -> dict[str, Any]:
    output = child.parsed_stdout
    storage = output.get("storage") if isinstance(output, dict) else {}
    items = storage.get("items") if isinstance(storage, dict) else []
    agent_run = storage.get("agent_run") if isinstance(storage, dict) else {}
    approval_count = len(items) if isinstance(items, list) else 0
    work_item_id = _first_nested_value(output, "work_item_id") or _first_nested_value(
        output,
        "work_item",
        "id",
    )
    return {
        "automation": {
            "command": args.command,
            "stage": getattr(args, "stage", ""),
            "returncode": child.returncode,
            "send_enabled": False,
            "email_sent": False,
            "gmail_writes_enabled": _gmail_writes_enabled(args),
            "external_writes_enabled": _external_writes_enabled(args),
            "agent_run_id": agent_run.get("id") if isinstance(agent_run, dict) else None,
            "approval_queue_items": approval_count,
            "work_item_id": work_item_id or "",
            "channel": getattr(args, "approval_channel", "")
            or getattr(args, "failure_slack_channel", "")
            or "",
        },
        "child_output": output,
    }


def _record_automation_summary(
    args: argparse.Namespace,
    child: ChildRun,
    summary: dict[str, Any],
) -> None:
    """Persist AutomationSpec and AutomationRun state for Chief of Staff review."""

    database_url = getattr(args, "database_url", None)
    store = SQLiteStore(database_url)
    ensure_default_automation_inventory(store)
    spec = automation_spec_for_command(
        str(args.command),
        stage=str(getattr(args, "stage", "")),
    )
    store.save_automation_spec(spec)
    automation = summary.get("automation") if isinstance(summary, dict) else {}
    if not isinstance(automation, dict):
        automation = {}
    status = _automation_run_status(args, child)
    run = AutomationRun(
        automation_id=spec.id,
        automation_name=spec.name,
        stage=str(getattr(args, "stage", "")),
        status=status,
        work_item_id=str(automation.get("work_item_id") or ""),
        channel=str(automation.get("channel") or spec.default_channel),
        approval_count=int(automation.get("approval_queue_items") or 0),
        failure_summary=_redacted_excerpt(child.stderr or child.stdout) if child.returncode else "",
        next_safe_action=_next_safe_action_for_automation(args, child),
        command=child.command,
        output_summary={
            "agent_run_id": automation.get("agent_run_id"),
            "returncode": child.returncode,
            "gmail_writes_enabled": automation.get("gmail_writes_enabled"),
            "external_writes_enabled": automation.get("external_writes_enabled"),
        },
        metadata={
            "controller": "scripts/run_keystone_automation.py",
            "child_stdout_present": bool(child.stdout.strip()),
            "child_stderr_present": bool(child.stderr.strip()),
        },
    )
    store.save_automation_run(run)


def _automation_run_status(
    args: argparse.Namespace,
    child: ChildRun,
) -> AutomationRunStatus:
    if child.returncode != 0:
        return AutomationRunStatus.FAILED
    if getattr(args, "stage", "") == "dry-run":
        return AutomationRunStatus.DRY_RUN
    return AutomationRunStatus.SUCCESS


def _next_safe_action_for_automation(args: argparse.Namespace, child: ChildRun) -> str:
    if child.returncode != 0:
        return "Review the redacted child error, fix the blocker, then rerun dry-run first."
    if args.command == "weekly-opportunity":
        return "Review generated opportunities and pending approval queue items."
    if args.command == "announcements-research":
        return "Review announcement research summaries and persisted feed history."
    if args.command == "gmail-triage":
        return "Review Gmail triage output before enabling the next gated stage."
    return "Review automation output."


def _first_nested_value(payload: Any, *keys: str) -> str:
    if not keys:
        return ""
    if isinstance(payload, dict):
        if len(keys) == 1 and keys[0] in payload and not isinstance(payload[keys[0]], dict):
            return str(payload[keys[0]] or "")
        if keys[0] in payload:
            value = _first_nested_value(payload[keys[0]], *keys[1:])
            if value:
                return value
        for value in payload.values():
            found = _first_nested_value(value, *keys)
            if found:
                return found
    if isinstance(payload, list):
        for item in payload:
            found = _first_nested_value(item, *keys)
            if found:
                return found
    return ""


def _gmail_writes_enabled(args: argparse.Namespace) -> bool:
    return args.command == "gmail-triage" and args.stage in {"label-apply", "draft-create"}


def _external_writes_enabled(args: argparse.Namespace) -> bool:
    if args.command == "weekly-opportunity":
        return bool(args.live_slack)
    return _gmail_writes_enabled(args)


def _format_summary(summary: dict[str, Any]) -> str:
    automation = summary["automation"]
    lines = [
        "Keystone automation complete",
        f"Command: {automation['command']}",
        f"Stage: {automation['stage']}",
        f"Agent run id: {automation.get('agent_run_id') or 'not saved'}",
        f"Approval queue items: {automation['approval_queue_items']}",
        f"Gmail writes enabled: {str(automation['gmail_writes_enabled']).lower()}",
        f"External writes enabled: {str(automation['external_writes_enabled']).lower()}",
        "Send enabled: false",
        "Email sent: false",
    ]
    return "\n".join(lines)


def _redacted_excerpt(value: Any, *, max_chars: int = 1200) -> str:
    text = str(redact_secrets(value) or "")
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3].rstrip()}..."


if __name__ == "__main__":
    raise SystemExit(main())
