#!/usr/bin/env python3
# ruff: noqa: E501
"""Sync a completed #evals Slack thread into the local eval database.

This is a read-only Slack importer. It does not post, rerun, score, or call a
model. Use it when a live #evals thread completed but the eval database did not
receive the Slack run row automatically.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from promptfoo.eval_dashboard import slack_run_post_save_state
from promptfoo.eval_database import (
    DEFAULT_EVAL_DB,
    DEFAULT_EVAL_SLACK_CHANNEL_ID,
    DEFAULT_EVAL_SLACK_CHANNEL_NAME,
    record_slack_eval_run,
    resolve_slack_eval_case_id,
)
from promptfoo.eval_urls import eval_dashboard_case_url, eval_review_case_url

RUN_ID_RE = re.compile(r"\b(?:Run|Run ID|WorkItem|Work item)\s*:\s*([A-Za-z]+_[A-Za-z0-9]+)\b")
FALLBACK_RUN_ID_RE = re.compile(r"\b((?:sbar|wi)_[A-Za-z0-9]+)\b")
CASE_ID_RE = re.compile(r"\beval\s+case\s*:?\s*([A-Za-z0-9_.-]+)\b", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s<>)\]]+")

AGENT_ALIASES: tuple[tuple[str, str], ...] = (
    ("chief of staff", "chief_of_staff"),
    ("business research analyst", "business_research_analyst"),
    ("opportunity scout", "opportunity_scout"),
    ("outreach composer", "outreach_composer"),
    ("gmail triage", "gmail_triage"),
    ("orchestrator", "orchestrator"),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record a completed #evals Slack thread in the local eval database."
    )
    parser.add_argument("--database-path", default=str(DEFAULT_EVAL_DB))
    parser.add_argument("--case-id", default="", help="Override case id; otherwise parsed/resolved.")
    parser.add_argument("--agent", default="", help="Override agent; otherwise inferred from the prompt.")
    parser.add_argument("--channel-id", default=DEFAULT_EVAL_SLACK_CHANNEL_ID)
    parser.add_argument("--channel-name", default=DEFAULT_EVAL_SLACK_CHANNEL_NAME)
    parser.add_argument("--thread-ts", default="", help="Slack thread timestamp.")
    parser.add_argument(
        "--thread-json",
        default="",
        help="Slack conversations.replies JSON fixture/path. Use '-' to read JSON from stdin.",
    )
    parser.add_argument("--live-slack", action="store_true", help="Read thread via Slack Web API.")
    parser.add_argument("--slack-token-env", default="SLACK_BOT_TOKEN")
    parser.add_argument("--permalink", default="")
    parser.add_argument("--run-label", default="slack_eval_thread_sync")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        payload = sync_slack_eval_thread(
            database_path=Path(args.database_path),
            case_id=args.case_id,
            agent=args.agent,
            channel_id=args.channel_id,
            channel_name=args.channel_name,
            thread_ts=args.thread_ts,
            thread_json_path=args.thread_json,
            live_slack=args.live_slack,
            slack_token_env=args.slack_token_env,
            permalink=args.permalink,
            run_label=args.run_label,
        )
    except Exception as exc:
        print(f"sync failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(f"synced case {payload['case_id']} run {payload['run_id']}")
        print(payload["dashboard_url"])
        print(payload["review_url"])
    return 0


def sync_slack_eval_thread(
    *,
    database_path: Path,
    case_id: str = "",
    agent: str = "",
    channel_id: str = DEFAULT_EVAL_SLACK_CHANNEL_ID,
    channel_name: str = DEFAULT_EVAL_SLACK_CHANNEL_NAME,
    thread_ts: str = "",
    thread_json_path: str = "",
    live_slack: bool = False,
    slack_token_env: str = "SLACK_BOT_TOKEN",
    permalink: str = "",
    run_label: str = "slack_eval_thread_sync",
) -> dict[str, Any]:
    messages = _load_thread_messages(
        thread_json_path=thread_json_path,
        live_slack=live_slack,
        channel_id=channel_id,
        thread_ts=thread_ts,
        slack_token_env=slack_token_env,
    )
    if not messages:
        raise ValueError("thread has no messages")

    root_text = _message_text(messages[0])
    resolved_agent = (agent or _infer_agent(root_text)).strip()
    resolved_case_id = (case_id or _extract_case_id(root_text)).strip()
    if not resolved_case_id:
        resolved_case_id = resolve_slack_eval_case_id(
            request_text=root_text,
            agent=resolved_agent,
            slack_channel_id=channel_id,
            slack_channel_name=channel_name,
            database_path=database_path,
        )
    if not resolved_case_id:
        raise ValueError("could not resolve eval case id from the thread")

    run_id, run_id_source_ts = _extract_run_id(messages)
    if not run_id:
        raise ValueError("could not find a saved run id in the thread")

    result_summary = _latest_result_summary(messages)
    status = _infer_status(messages)
    effective_thread_ts = str(thread_ts or messages[0].get("thread_ts") or messages[0].get("ts") or "").strip()
    urls = sorted(set(URL_RE.findall(result_summary)))
    evidence = {
        "schema": "keystone.eval.slack_thread_sync.v1",
        "sync_source": "scripts/sync_slack_eval_thread.py",
        "slack_channel_id": channel_id,
        "slack_channel_name": channel_name,
        "slack_thread_ts": effective_thread_ts,
        "thread_message_count": len(messages),
        "run_id_source_ts": run_id_source_ts,
        "message_timestamps": [
            str(message.get("ts") or "")
            for message in messages
            if str(message.get("ts") or "").strip()
        ],
        "source_urls": urls[:20],
    }
    row_id = record_slack_eval_run(
        case_id=resolved_case_id,
        run_id=run_id,
        agent=resolved_agent,
        work_item_id=run_id,
        slack_channel_id=channel_id,
        slack_channel_name=channel_name,
        slack_thread_ts=effective_thread_ts,
        permalink=permalink,
        request_text=root_text,
        result_summary=result_summary,
        route=resolved_agent,
        status=status,
        context_policy="thread_sync",
        thread_fetch_status="ok",
        thread_message_count=len(messages),
        cost_profile="unknown",
        source_count=len(urls),
        visible_source_count=len(urls),
        evidence=evidence,
        run_mode="slack_thread_sync_no_api",
        run_label=run_label,
        storage_mode="local_review",
        database_path=database_path,
    )
    post_save = slack_run_post_save_state(
        case_id=resolved_case_id,
        row_id=row_id,
        database_path=database_path,
    )
    return {
        "id": row_id,
        "case_id": resolved_case_id,
        "run_id": run_id,
        "agent": resolved_agent,
        "status": status,
        "slack_thread_ts": effective_thread_ts,
        "thread_message_count": len(messages),
        "database_path": str(database_path),
        "dashboard_url": eval_dashboard_case_url(resolved_case_id),
        "review_url": eval_review_case_url(resolved_case_id),
        "post_save_state": post_save,
    }


def _load_thread_messages(
    *,
    thread_json_path: str,
    live_slack: bool,
    channel_id: str,
    thread_ts: str,
    slack_token_env: str,
) -> list[dict[str, Any]]:
    if bool(thread_json_path) == bool(live_slack):
        raise ValueError("use exactly one of --thread-json or --live-slack")
    if thread_json_path:
        raw_payload = sys.stdin.read() if thread_json_path == "-" else Path(thread_json_path).read_text(encoding="utf-8")
        return _messages_from_payload(json.loads(raw_payload))
    return _fetch_live_slack_thread(
        channel_id=channel_id,
        thread_ts=thread_ts,
        slack_token_env=slack_token_env,
    )


def _messages_from_payload(payload: Any) -> list[dict[str, Any]]:
    messages = payload.get("messages") if isinstance(payload, dict) else payload
    if not isinstance(messages, list):
        raise ValueError("thread JSON must be a Slack response object or list of messages")
    normalized = [message for message in messages if isinstance(message, dict)]
    return sorted(normalized, key=lambda message: str(message.get("ts") or ""))


def _fetch_live_slack_thread(
    *,
    channel_id: str,
    thread_ts: str,
    slack_token_env: str,
) -> list[dict[str, Any]]:
    token = os.environ.get(slack_token_env, "").strip()
    if not token:
        raise ValueError(f"{slack_token_env} is required for --live-slack")
    if not channel_id or not thread_ts:
        raise ValueError("--channel-id and --thread-ts are required for --live-slack")
    params = urllib.parse.urlencode({"channel": channel_id, "ts": thread_ts, "limit": 50})
    request = urllib.request.Request(
        f"https://slack.com/api/conversations.replies?{params}",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok"):
        raise ValueError(f"Slack conversations.replies failed: {payload.get('error') or 'unknown_error'}")
    return _messages_from_payload(payload)


def _message_text(message: dict[str, Any]) -> str:
    text = str(message.get("text") or "").strip()
    if text:
        return text
    blocks = message.get("blocks")
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        text_obj = block.get("text")
        if isinstance(text_obj, dict):
            part = str(text_obj.get("text") or "").strip()
            if part:
                parts.append(part)
    return "\n".join(parts).strip()


def _extract_case_id(root_text: str) -> str:
    match = CASE_ID_RE.search(root_text)
    return match.group(1).strip() if match else ""


def _infer_agent(root_text: str) -> str:
    lowered = root_text.lower().replace("_", " ")
    for alias, agent in AGENT_ALIASES:
        if alias in lowered:
            return agent
    return ""


def _extract_run_id(messages: list[dict[str, Any]]) -> tuple[str, str]:
    for message in messages:
        text = _message_text(message)
        match = RUN_ID_RE.search(text)
        if match:
            return match.group(1), str(message.get("ts") or "")
    for message in messages:
        text = _message_text(message)
        match = FALLBACK_RUN_ID_RE.search(text)
        if match:
            return match.group(1), str(message.get("ts") or "")
    return "", ""


def _infer_status(messages: list[dict[str, Any]]) -> str:
    combined = "\n".join(_message_text(message).lower() for message in messages)
    if "run completed" in combined or "status: completed" in combined:
        return "done"
    if "blocked" in combined:
        return "blocked"
    if "failed" in combined or "error" in combined:
        return "failed"
    if "in progress" in combined:
        return "running"
    return "tbd"


def _latest_result_summary(messages: list[dict[str, Any]]) -> str:
    candidates: list[str] = []
    for message in messages[1:]:
        text = _message_text(message)
        if not text:
            continue
        lowered = text.lower()
        if "business agents run completed" in lowered and "metadata:" not in lowered:
            continue
        candidates.append(text)
    if candidates:
        return candidates[-1]
    return _message_text(messages[-1])


if __name__ == "__main__":
    raise SystemExit(main())
