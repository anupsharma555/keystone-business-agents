#!/usr/bin/env python3
"""Validate bounded RSS and preprint context reads without model or provider writes."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from typing import Any

from keystone_agents.tools.announcement_context_tools import (
    retrieve_preprint_announcement_history_impl,
    retrieve_rss_announcement_history_impl,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read canonical announcement context and emit content-safe receipts."
    )
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--rss-query", default="")
    parser.add_argument("--preprints-query", default="")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument(
        "--live-rss-slack",
        action="store_true",
        help="Use the explicitly enabled read-only Slack #announcements fallback.",
    )
    parser.add_argument(
        "--require-items",
        action="store_true",
        help="Exit nonzero unless both context sources return at least one usable item.",
    )
    return parser


def _receipt(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    items = list(payload.get("items") or [])
    evidence_statuses = Counter(
        str(item.get("evidence_status") or "unknown") for item in items
    )
    items_with_identity = sum(bool(item.get("feed_item_id")) for item in items)
    items_with_source = sum(bool(item.get("url") or item.get("publication_ids")) for item in items)
    read_sources = [
        f"{diagnostic.get('key')}:{diagnostic.get('value')}"
        for diagnostic in payload.get("diagnostics") or []
        if isinstance(diagnostic, dict)
        and diagnostic.get("key") in {
            "database_url_source",
            "linked_discovery_store",
            "slack_rss_history",
        }
    ]
    return {
        "kind": kind,
        "tool_status": payload.get("status"),
        "item_count": len(items),
        "items_with_identity": items_with_identity,
        "items_with_source": items_with_source,
        "evidence_status_counts": dict(sorted(evidence_statuses.items())),
        "read_sources": read_sources,
        "usable_context": bool(
            payload.get("status") == "success"
            and items
            and items_with_identity == len(items)
            and items_with_source == len(items)
        ),
        "blockers": list(payload.get("blockers") or []),
    }


def run_validation(
    *,
    database_url: str | None = None,
    rss_query: str = "",
    preprints_query: str = "",
    limit: int = 5,
    live_rss_slack: bool = False,
) -> dict[str, Any]:
    rss = _receipt(
        "rss",
        retrieve_rss_announcement_history_impl(
            query=rss_query,
            limit=limit,
            database_url=database_url,
            live=live_rss_slack,
        ),
    )
    preprints = _receipt(
        "preprints",
        retrieve_preprint_announcement_history_impl(
            query=preprints_query,
            limit=limit,
            database_url=database_url,
        ),
    )
    return {
        "schema": "keystone.announcement_context_read_validation.v1",
        "rss": rss,
        "preprints": preprints,
        "operational_ready": bool(rss["usable_context"] and preprints["usable_context"]),
        "side_effects": {
            "model_requests": 0,
            "search_requests": 0,
            "provider_writes": 0,
            "slack_posts": 0,
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_validation(
        database_url=args.database_url,
        rss_query=args.rss_query,
        preprints_query=args.preprints_query,
        limit=args.limit,
        live_rss_slack=args.live_rss_slack,
    )
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 1 if args.require_items and not result["operational_ready"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
