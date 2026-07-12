#!/usr/bin/env python3
"""Refresh and query the local presentation SQLite index without a model call."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from keystone_agents.config import with_cli_environment
from keystone_agents.presentation_index import (
    presentation_hits_to_artifact_refs,
    refresh_presentation_index,
    search_presentation_index,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="Clinical AI evidence")
    parser.add_argument("--max-decks", type=int, default=100)
    parser.add_argument("--max-results", type=int, default=10)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("artifacts/test-pack/presentation-index.sqlite3"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/presentation-index-validation.json"),
    )
    parser.add_argument("--live-local-read", action="store_true")
    return parser


@with_cli_environment()
def main() -> int:
    args = build_parser().parse_args()
    refresh = refresh_presentation_index(
        args.database,
        max_decks=args.max_decks,
        live=bool(args.live_local_read),
    )
    hits = (
        search_presentation_index(args.database, args.query, max_results=args.max_results)
        if refresh["status"] == "success"
        else []
    )
    refs = presentation_hits_to_artifact_refs(hits)
    payload = {
        "schema": "keystone.presentation_index_validation.v1",
        "status": (
            "pass"
            if refresh["status"] == "success" and refs
            else "preview"
            if refresh["status"] == "dry-run"
            else "no_match"
        ),
        "query": args.query,
        "openai_requests": 0,
        "provider_writes": 0,
        "parent_modified": False,
        "refresh": refresh,
        "hit_count": len(hits),
        "artifact_refs": [ref.model_dump(mode="json") for ref in refs],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(args.output)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] in {"pass", "preview"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
