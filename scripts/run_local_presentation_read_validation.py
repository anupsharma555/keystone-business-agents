"""Read one exact allowlisted local PowerPoint deck without modifying it."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from keystone_agents.config import with_cli_environment
from keystone_agents.tools.internal_data_tools import (
    presentation_read_local_impl,
    presentation_search_local_impl,
)

DEFAULT_OUTPUT = Path("artifacts/test-pack/local-presentation-read-validation.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("relative_path", nargs="?", default="")
    parser.add_argument("--query", default="")
    parser.add_argument("--max-slides", type=int, default=40)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--live-local-read", action="store_true")
    return parser


@with_cli_environment()
def main() -> int:
    args = build_parser().parse_args()
    relative_path = args.relative_path.strip()
    search_result: dict[str, object] = {}
    if not relative_path and args.query.strip():
        search_result = presentation_search_local_impl(
            args.query,
            max_items=20,
            live=bool(args.live_local_read),
        )
        items = search_result.get("items", [])
        candidates = [item for item in items if isinstance(item, dict)]
        if len(candidates) == 1 or (
            len(candidates) > 1
            and int(candidates[0].get("query_term_matches") or 0)
            > int(candidates[1].get("query_term_matches") or 0)
        ):
            relative_path = str(candidates[0].get("relative_path") or "")
        elif args.live_local_read:
            payload = {
                "status": "missing" if not candidates else "ambiguous",
                "openai_requests": 0,
                "query": args.query,
                "candidate_count": len(candidates),
                "candidates": candidates,
                "parent_modified": False,
            }
            _write_receipt(args.output, payload)
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 2
    if not relative_path:
        raise SystemExit("Provide relative_path or --query.")
    result = presentation_read_local_impl(
        relative_path,
        max_slides=args.max_slides,
        live=bool(args.live_local_read),
    )
    payload = {
        "status": (
            "passed"
            if result.get("status") == "success"
            else "preview"
            if result.get("status") == "dry-run"
            else "failed"
        ),
        "openai_requests": 0,
        "query": args.query,
        "search": search_result,
        "deck": result,
    }
    _write_receipt(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] in {"passed", "preview"} else 1


def _write_receipt(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
