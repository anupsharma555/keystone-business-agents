#!/usr/bin/env python3
"""Deliver a successful Chief Prior Week Packet synthesis to Doc and Slack."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from keystone_agents.config import load_settings, require_cli_live_confirmation
from keystone_agents.weekly_ops_delivery import deliver_weekly_ops_packet


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--live-google-workspace-write", action="store_true")
    parser.add_argument("--workspace-approval-reference", default="")
    parser.add_argument("--live-slack-post", action="store_true")
    parser.add_argument("--slack-channel-id", default="")
    parser.add_argument("--slack-approval-reference", default="")
    parser.add_argument("--resume-delivery-receipt", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/weekly-chief-packet-delivery.json"),
    )
    args = parser.parse_args()
    load_settings(force_dotenv=True)
    if args.live_google_workspace_write:
        require_cli_live_confirmation(
            dry_run=False,
            live_flag=True,
            flag_name="--live-google-workspace-write",
            live_action="creating and verifying one durable weekly packet Google Doc",
        )
    if args.live_slack_post:
        require_cli_live_confirmation(
            dry_run=False,
            live_flag=True,
            flag_name="--live-slack-post",
            live_action="posting one verified weekly packet link to ops-finance",
        )
    synthesis = json.loads(args.input.read_text(encoding="utf-8"))
    prior_delivery = (
        json.loads(args.resume_delivery_receipt.read_text(encoding="utf-8"))
        if args.resume_delivery_receipt
        else None
    )
    result = deliver_weekly_ops_packet(
        synthesis,
        live_workspace_write=bool(args.live_google_workspace_write),
        workspace_approval_reference=args.workspace_approval_reference,
        live_slack_post=bool(args.live_slack_post),
        slack_channel_id=args.slack_channel_id,
        slack_approval_reference=args.slack_approval_reference,
        prior_delivery_receipt=prior_delivery,
    )
    rendered = json.dumps(result, indent=2, sort_keys=True, default=str) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(args.output)
    print(rendered, end="")
    allowed = {"delivery_planned", "doc_verified_slack_pending", "passed"}
    return 0 if result["status"] in allowed else 1


if __name__ == "__main__":
    raise SystemExit(main())
