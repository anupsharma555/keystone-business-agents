"""Read and summarize the latest bounded Slack thread without model calls or posts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from keystone_agents.storage.sqlite_store import redact_secrets
from keystone_agents.tools.slack_tool import SlackTool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--thread-ts", default="")
    parser.add_argument("--max-messages", type=int, default=30)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/slack-latest-thread-summary-live.json"),
    )
    return parser


def _compact(value: Any, *, limit: int = 360) -> str:
    text = " ".join(str(redact_secrets(value) or "").split())
    text = re.sub(r"<@[^>]+>", "Operator", text)
    text = re.sub(r"\s*\*Sent using\*.*$", "", text, flags=re.I)
    text = re.sub(
        r"<mailto:[^|>]+\|[^>]+>|\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b",
        "[email omitted]",
        text,
    )
    text = re.sub(
        r"\bEvent ID:\s*.*?(?=\s+-\s+|$)",
        "Event ID: [retained internally]",
        text,
        flags=re.I,
    )
    return text if len(text) <= limit else f"{text[: limit - 3].rstrip()}..."


def _insights(messages: list[dict[str, Any]]) -> dict[str, list[str] | str]:
    texts = [_compact(message.get("text"), limit=700) for message in messages]
    texts = [text for text in texts if text]
    human = [
        _compact(message.get("text"), limit=360)
        for message in messages
        if not str(message.get("bot_id") or "").strip() and message.get("text")
    ]
    bot = [
        _compact(message.get("text"), limit=900)
        for message in messages
        if str(message.get("bot_id") or "").strip() and message.get("text")
    ]
    root_request = human[0] if human else (texts[0] if texts else "")
    latest_outcome = _answer_excerpt(bot[-1]) if bot else ""
    decisions = list(
        dict.fromkeys(
            text
            for text in [*human[1:], latest_outcome]
            if text
            and re.search(
                r"\b(?:agree|approved|year|timezone|calendar|should|will|policy|decision|keep)\b",
                text,
                re.I,
            )
        )
    )[:5]
    actions = _action_excerpts(bot)[:5]
    return {
        "summary": _compact(
            f"Request: {root_request}. Latest agent outcome: {latest_outcome}",
            limit=900,
        ),
        "decisions": decisions,
        "action_items": actions,
    }


def _answer_excerpt(text: str) -> str:
    match = re.search(r"\*?Answer:\*?\s*(.*?)(?=\*?Detailed Summary|\Z)", text, re.I)
    return _compact(match.group(1) if match else text, limit=360)


def _action_excerpts(bot_messages: list[str]) -> list[str]:
    actions: list[str] = []
    for text in reversed(bot_messages):
        section = re.search(
            r"(?:Recommended actions|Still needs attention|Next step):?\s*(.*?)"
            r"(?=\*?[A-Z][^:]{2,30}:|\Z)",
            text,
            re.I,
        )
        if section:
            for item in re.findall(r"(?:^|\s)[-*]\s+([^*]{4,240})", section.group(1)):
                compact = _compact(item, limit=240)
                if compact:
                    actions.append(compact)
        else:
            next_step = re.search(
                r"(To (?:change|modify|delete)(?:\s+or\s+(?:change|modify|delete))* "
                r"it later,.*?)(?:\.|$)",
                text,
                re.I,
            )
            if next_step:
                actions.append(_compact(next_step.group(1), limit=240))
            elif re.search(r"provider verification:\s*passed", text, re.I):
                actions.append("No further action; the provider operation was verified.")
        if actions:
            break
    return list(dict.fromkeys(actions))


def run_validation(
    *, channel: str, thread_ts: str, max_messages: int, slack: Any
) -> dict[str, Any]:
    latest = slack.resolve_latest_thread_root(channel, scan_limit=50) if not thread_ts else {}
    selected_ts = thread_ts or str(latest.get("thread_ts") or "")
    if not selected_ts:
        return {"status": "blocked", "reason": "no_thread_root", "openai_requests": 0}
    thread = slack.read_thread(channel, selected_ts, limit=max_messages)
    messages = list(thread.get("messages") or [])
    insights = _insights(messages)
    passed = bool(
        thread.get("status") == "success"
        and messages
        and insights["summary"]
        and (insights["decisions"] or insights["action_items"])
    )
    return {
        "status": "pass" if passed else "partial",
        "scenario": "latest_slack_thread_summary_and_actions",
        "thread_ref": {
            "channel_hash": hashlib.sha256(channel.encode()).hexdigest()[:12],
            "thread_ts_hash": hashlib.sha256(selected_ts.encode()).hexdigest()[:12],
        },
        "message_count": len(messages),
        **insights,
        "openai_requests": 0,
        "slack_posts": 0,
        "provider_mutations": 0,
        "post_enabled": False,
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
        thread_ts=args.thread_ts,
        max_messages=max(1, min(50, args.max_messages)),
        slack=SlackTool(live=True),
    )
    _write_atomic(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
