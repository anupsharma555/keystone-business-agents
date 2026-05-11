"""Run the weekly Keystone opportunity discovery loop."""

from __future__ import annotations

import argparse
import json

from keystone_agents.config import (
    cli_default_dry_run,
    cli_default_live_research,
    require_cli_live_confirmation,
    with_cli_environment,
)
from keystone_agents.schemas.approval import ApprovalState
from keystone_agents.tools.serper_tool import SearchProviderName
from keystone_agents.tools.slack_tool import SlackTool, slack_review_message_from_approval_item
from keystone_agents.workflows import (
    run_weekly_opportunity_workflow,
    weekly_opportunity_workflow_markdown,
)

APPROVAL_STATES = [
    state.value
    for state in ApprovalState
    if state
    not in {
        ApprovalState.APPROVED_FOR_EXTERNAL_USE,
        ApprovalState.APPROVED_FOR_SEND,
    }
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the weekly OS -> CR -> OD opportunity workflow."
    )
    parser.add_argument(
        "--topic",
        default="Find Keystone-relevant opportunities for weekly review.",
        help="Opportunity discovery topic for the weekly loop.",
    )
    parser.add_argument(
        "--max-opportunities",
        type=int,
        default=5,
        help="Maximum opportunities to process through company research.",
    )
    parser.add_argument(
        "--approval-state",
        choices=APPROVAL_STATES,
        default=ApprovalState.PENDING.value,
        help="Approval state for the opportunity-to-draft gate.",
    )
    parser.add_argument(
        "--approval-channel",
        default="#ai-agents-workflow",
        help="Human approval channel referenced in saved approval metadata.",
    )
    parser.add_argument(
        "--outreach-channel",
        choices=["email", "linkedin"],
        default="email",
        help="Single approval artifact channel to queue for review.",
    )
    parser.add_argument(
        "--live-search",
        action="store_true",
        default=cli_default_live_research(),
        help="Use configured live search providers for scouting and company research.",
    )
    parser.add_argument(
        "--search-provider",
        choices=[provider.value for provider in SearchProviderName],
        default=None,
        help="Live search provider override.",
    )
    parser.add_argument(
        "--fallback-search-provider",
        choices=[
            provider.value
            for provider in SearchProviderName
            if provider != SearchProviderName.DRY_RUN
        ],
        default=None,
        help="Optional fallback provider for live opportunity scouting.",
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=cli_default_dry_run(),
        help="Use deterministic fixture mode. Pass --no-dry-run with --live-search for live.",
    )
    parser.add_argument("--save", action="store_true", help="Save artifacts to SQLite.")
    parser.add_argument(
        "--request-approval",
        action="store_true",
        help="Post saved approval review packets to Slack or return dry-run Slack previews.",
    )
    parser.add_argument(
        "--live-slack",
        action="store_true",
        help="Post approval notifications to Slack. Requires --request-approval and --save.",
    )
    parser.add_argument(
        "--live-sdk",
        action="store_true",
        help=(
            "Use live SDK synthesis for human-facing Opportunity Scout, Account "
            "Researcher, and Outreach Composer output. Does not send email or post "
            "outside the existing approval notification path."
        ),
    )
    parser.add_argument("--database-url", default=None, help="SQLite URL for --save.")
    parser.add_argument("--contact-name", default=None, help="Optional test contact name override.")
    parser.add_argument(
        "--contact-title",
        default=None,
        help="Optional test contact title override.",
    )
    parser.add_argument("--contact-email", default=None, help="Optional test recipient email.")
    parser.add_argument(
        "--contact-linkedin-url",
        default=None,
        help="Optional test contact or company LinkedIn/profile URL.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    parser.add_argument("--markdown", action="store_true", help="Print markdown output.")
    return parser


@with_cli_environment()
def main() -> int:
    args = build_parser().parse_args()
    if args.live_search:
        try:
            require_cli_live_confirmation(
                dry_run=args.dry_run,
                live_flag=True,
                flag_name="--live-search",
                live_action="weekly opportunity live search workflow",
            )
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
    elif not args.dry_run and not args.live_slack:
        raise SystemExit("--no-dry-run requires --live-search or --live-slack.")
    if args.live_slack and not args.request_approval:
        raise SystemExit("--live-slack requires --request-approval.")
    if args.live_sdk and args.dry_run:
        raise SystemExit("--live-sdk requires --no-dry-run for explicit live model execution.")
    if args.request_approval and not args.save:
        raise SystemExit("--request-approval requires --save so approval ids are persisted.")
    if args.live_slack:
        try:
            require_cli_live_confirmation(
                dry_run=args.dry_run,
                live_flag=True,
                flag_name="--live-slack",
                live_action="posting weekly opportunity approval notifications to Slack",
            )
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc

    result = run_weekly_opportunity_workflow(
        topic=args.topic,
        max_opportunities=args.max_opportunities,
        approval_state=args.approval_state,
        dry_run=args.dry_run,
        live_search=args.live_search,
        search_provider=args.search_provider,
        fallback_search_provider=args.fallback_search_provider,
        save=args.save,
        database_url=args.database_url,
        approval_channel=args.approval_channel,
        contact_name=args.contact_name,
        contact_title=args.contact_title,
        contact_email=args.contact_email,
        contact_linkedin_url=args.contact_linkedin_url,
        outreach_channel=args.outreach_channel,
        live_sdk_synthesis=args.live_sdk,
    )
    if args.request_approval:
        slack = SlackTool(live=args.live_slack, approvals_channel=args.approval_channel)
        posted = []
        for saved_item in result.storage.get("items", []):
            approval_item = saved_item.get("approval_queue_item")
            if not isinstance(approval_item, dict):
                continue
            message = slack_review_message_from_approval_item(approval_item)
            posted.append(
                slack.post_review_message(
                    message,
                    channel=args.approval_channel,
                )
            )
        result.storage["slack_approval_posts"] = posted
    if args.markdown and not args.json:
        print(weekly_opportunity_workflow_markdown(result))
    else:
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
