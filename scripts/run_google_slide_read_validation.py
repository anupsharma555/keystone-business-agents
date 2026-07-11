"""Find and read one scoped Google Slides or PowerPoint deck without modification."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from keystone_agents.config import with_cli_environment
from keystone_agents.tools.internal_data_tools import (
    GOOGLE_SLIDES_MIME_TYPE,
    POWERPOINT_MIME_TYPE,
    google_drive_search_files_impl,
    google_slide_deck_read_impl,
)

DEFAULT_OUTPUT = Path("artifacts/test-pack/google-slide-read-validation.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="Clinical AI")
    parser.add_argument("--folder-path", default="KNIOps")
    parser.add_argument("--presentation-id", default="")
    parser.add_argument("--max-slides", type=int, default=40)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--live-google-workspace", action="store_true")
    return parser


@with_cli_environment()
def main() -> int:
    args = build_parser().parse_args()
    live = bool(args.live_google_workspace)
    candidates: list[dict[str, object]] = []
    presentation_id = args.presentation_id.strip()
    if not presentation_id:
        for mime_type in (GOOGLE_SLIDES_MIME_TYPE, POWERPOINT_MIME_TYPE):
            result = google_drive_search_files_impl(
                args.query,
                folder_path=args.folder_path,
                mime_type=mime_type,
                max_items=20,
                live=live,
            )
            candidates.extend(
                item for item in result.get("items", []) if isinstance(item, dict)
            )
        unique = {str(item.get("id") or ""): item for item in candidates}
        candidates = [item for key, item in unique.items() if key]
        if len(candidates) == 1:
            presentation_id = str(candidates[0].get("id") or "")

    if not live:
        payload = {
            "status": "preview",
            "openai_requests": 0,
            "query": args.query,
            "folder_path": args.folder_path,
            "presentation_id": presentation_id,
            "candidate_count": len(candidates),
            "parent_modified": False,
        }
        _write_receipt(args.output, payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if not presentation_id:
        payload = {
            "status": "missing" if not candidates else "ambiguous",
            "openai_requests": 0,
            "query": args.query,
            "folder_path": args.folder_path,
            "candidate_count": len(candidates),
            "candidates": [_candidate_receipt(item) for item in candidates],
            "parent_modified": False,
        }
        _write_receipt(args.output, payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2

    result = google_slide_deck_read_impl(
        presentation_id,
        folder_path=args.folder_path,
        max_slides=args.max_slides,
        live=True,
    )
    payload = {
        "status": "passed" if result.get("status") == "success" else "failed",
        "openai_requests": 0,
        "query": args.query,
        "folder_path": args.folder_path,
        "candidate_count": len(candidates),
        "deck": result,
    }
    _write_receipt(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == "passed" else 1


def _candidate_receipt(item: dict[str, object]) -> dict[str, object]:
    return {
        key: item.get(key)
        for key in ("id", "name", "mime_type", "url", "modified_time")
        if key in item
    }


def _write_receipt(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
