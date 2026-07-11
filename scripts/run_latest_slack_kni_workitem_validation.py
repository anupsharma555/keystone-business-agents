"""Route the latest bounded Slack @KNI request through a no-live WorkItem step."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from keystone_agents.schemas.work_item import WorkflowRunRequest
from keystone_agents.tools.slack_tool import SlackTool
from keystone_agents.workflow_runner import advance_work_item


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/slack-latest-kni-workitem-live.json"),
    )
    return parser


def run_validation(*, channel: str, database: Path, slack: Any) -> dict[str, Any]:
    selected = slack.find_latest_kni_request(channel, scan_limit=200)
    request_text = str(selected.get("text") or "").strip()
    if not request_text:
        return {"status": "blocked", "reason": "kni_request_not_found", "openai_requests": 0}
    database.parent.mkdir(parents=True, exist_ok=True)
    result = advance_work_item(
        WorkflowRunRequest(
            request_text=request_text,
            database_url=f"sqlite:///{database}",
            save=True,
            live_sdk=False,
            live_search=False,
        )
    )
    next_action = result.next_action.model_dump(mode="json") if result.next_action else None
    passed = bool(
        result.work_item.id
        and result.work_item.request_text == request_text
        and result.route.value
        and result.status.value
    )
    return {
        "status": "pass" if passed else "partial",
        "scenario": "latest_slack_kni_request_workitem_route",
        "slack_ref": {
            "channel_hash": hashlib.sha256(channel.encode()).hexdigest()[:12],
            "ts_hash": hashlib.sha256(str(selected.get("ts") or "").encode()).hexdigest()[:12],
        },
        "request_hash": hashlib.sha256(request_text.encode()).hexdigest()[:16],
        "stored_request_matches": result.work_item.request_text == request_text,
        "work_item_id": result.work_item.id,
        "route": result.route.value,
        "work_item_status": result.status.value,
        "next_action": next_action,
        "blocker_codes": [blocker.code for blocker in result.blockers],
        "openai_requests": 0,
        "live_search": False,
        "slack_posts": 0,
        "external_writes": 0,
        "send_enabled": False,
    }


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def main() -> int:
    args = build_parser().parse_args()
    payload = run_validation(
        channel=args.channel,
        database=args.database,
        slack=SlackTool(live=True),
    )
    _write_atomic(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
