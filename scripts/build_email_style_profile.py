"""Build a safe aggregate sent-email style profile."""

from __future__ import annotations

import argparse
import json
from typing import Any

from keystone_agents.config import require_cli_live_confirmation
from keystone_agents.schemas.approval import ApprovalScope, ApprovalState
from keystone_agents.schemas.email_triage import GmailMessageEnvelope
from keystone_agents.tools.email_style_tool import (
    SentEmailStyleSample,
    build_email_style_profile_from_samples,
    load_sent_email_style_samples_fixture,
)
from keystone_agents.tools.gmail_tool import GmailAPIError, GmailConfigurationError, GmailTool
from keystone_agents.tools.storage_tool import StorageTool

APPROVAL_DECISIONS = [state.value for state in ApprovalState]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Derive an aggregate EmailStyleProfile from sent-email samples."
    )
    parser.add_argument(
        "--fixture",
        default="sample_sent_email_style_messages",
        help="Local fixture name or path containing fake sent-email style samples.",
    )
    parser.add_argument(
        "--live-gmail",
        action="store_true",
        help="Sample live Gmail SENT messages. Requires --no-dry-run.",
    )
    parser.add_argument(
        "--max-messages",
        type=int,
        default=5,
        help="Maximum sent messages to sample in live Gmail mode.",
    )
    parser.add_argument("--profile-id", default="sent-default")
    parser.add_argument(
        "--approval-state",
        choices=APPROVAL_DECISIONS,
        default=ApprovalState.PENDING.value,
        help="Human approval state for the generated profile. Defaults to pending.",
    )
    parser.add_argument("--reviewer", default="", help="Reviewer for saved approval records.")
    parser.add_argument("--approval-notes", default="", help="Reviewer notes.")
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep fixture mode by default. Pass --no-dry-run with --live-gmail.",
    )
    parser.add_argument("--save", action="store_true", help="Save profile and audit rows.")
    parser.add_argument("--database-url", default=None, help="SQLite URL for --save.")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    parser.add_argument("--markdown", action="store_true", help="Print markdown output.")
    return parser


def _sent_samples_from_live_gmail(max_messages: int) -> list[SentEmailStyleSample]:
    gmail = GmailTool(live=True)
    refs = gmail.list_recent_messages(label="SENT", max_results=max_messages)
    samples: list[SentEmailStyleSample] = []
    for ref in refs:
        message_id = str(ref.get("id") or "")
        if not message_id:
            continue
        message = gmail.get_message(message_id)
        envelope = GmailMessageEnvelope.model_validate(message.get("envelope") or {})
        samples.append(
            SentEmailStyleSample(
                source_id=f"gmail:sent:{message_id}",
                source_url=f"gmail://sent/{message_id}",
                subject=envelope.subject,
                body=envelope.normalized_body,
            )
        )
    return samples


def _build_result(args: argparse.Namespace) -> dict[str, Any]:
    if args.max_messages < 1:
        raise SystemExit("--max-messages must be at least 1.")
    if args.max_messages > 25:
        raise SystemExit("--max-messages must be 25 or less for style profiling.")

    try:
        if args.live_gmail:
            require_cli_live_confirmation(
                dry_run=args.dry_run,
                live_flag=True,
                flag_name="--live-gmail",
                live_action="sampling Gmail SENT messages for aggregate style profiling",
            )
            samples = _sent_samples_from_live_gmail(args.max_messages)
            source = "live_gmail_sent"
            source_id = "gmail:sent"
            source_url = "gmail://sent"
        else:
            if not args.dry_run:
                require_cli_live_confirmation(
                    dry_run=False,
                    live_flag=False,
                    flag_name="--live-gmail",
                    live_action="live sent-email style profiling",
                )
            samples = load_sent_email_style_samples_fixture(args.fixture)
            source = f"fixture://{args.fixture}"
            source_id = f"fixture:{args.fixture}"
            source_url = source
    except (GmailAPIError, GmailConfigurationError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc

    result = build_email_style_profile_from_samples(
        samples,
        profile_id=args.profile_id,
        source=source,
        source_id=source_id,
        source_url=source_url,
        approval_state=args.approval_state,
    )
    payload = result.model_dump(mode="json")
    payload["mode"] = "live-gmail-sent" if args.live_gmail else "fixture"
    payload["sample_count"] = len(samples)
    payload["dry_run"] = not args.live_gmail
    payload["send_enabled"] = False
    payload["sent"] = False

    if args.save:
        storage = StorageTool(args.database_url)
        profile_id = storage.save_email_style_profile(result.profile)
        payload["storage"] = {
            "email_style_profile": profile_id,
            "agent_run": storage.save_agent_run(
                agent_name="email_style_profiler",
                input_payload={
                    "fixture": args.fixture if not args.live_gmail else None,
                    "live_gmail": args.live_gmail,
                    "max_messages": args.max_messages,
                    "profile_id": args.profile_id,
                    "approval_state": args.approval_state,
                },
                input_summary=f"email style profile {args.profile_id}",
                output=payload,
                model="fixture" if not args.live_gmail else "live-gmail-sent-local",
                dry_run=not args.live_gmail,
                status="success",
            ),
            "approval": storage.save_approval(
                object_type="email_style_profile",
                object_id=args.profile_id,
                decision=args.approval_state,
                scope=ApprovalScope.DRAFTING,
                reviewer=args.reviewer,
                notes=args.approval_notes,
                source_agent="email_style_profiler",
            ),
        }
    return payload


def _render_markdown(payload: dict[str, Any]) -> str:
    profile = payload["profile"]
    lines = [
        "# Email Style Profile",
        "",
        f"- Profile ID: {profile['profile_id']}",
        f"- Approval state: {profile['approval_state']}",
        f"- Approval required: {str(payload['approval_required']).lower()}",
        f"- Sample count: {payload['sample_count']}",
        f"- Raw bodies included: {str(payload['raw_sent_email_bodies_included']).lower()}",
        f"- Send enabled: {str(payload['send_enabled']).lower()}",
        f"- Greeting patterns: {', '.join(profile['greeting_patterns'])}",
        f"- Signoffs: {', '.join(profile['signoffs'])}",
        f"- Preferred phrases: {', '.join(profile['preferred_phrases'])}",
        "",
        "Limitations:",
        *[f"- {item}" for item in payload["limitations"]],
    ]
    return "\n".join(lines)


def main() -> int:
    args = build_parser().parse_args()
    payload = _build_result(args)
    if args.markdown and not args.json:
        print(_render_markdown(payload))
    else:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
