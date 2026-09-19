#!/usr/bin/env python3
"""Reject staged local diagnostic artifacts before a GitHub publish."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Iterable
from pathlib import PurePosixPath

BLOCKED_DIRECTORY_PREFIXES: tuple[str, ...] = (
    ".keystone/",
    ".venv/",
    ".playwright-cli/",
    ".playwright-mcp/",
    "artifacts/",
    "output/",
)
BLOCKED_ROOT_GLOBS: tuple[str, ...] = (
    ".venv",
    ".env",
    "*.db.execution.sqlite3.*.lock",
    "kni-slack-dashboard-*.png",
)


def blocked_staged_paths(paths: Iterable[str]) -> tuple[str, ...]:
    """Return sorted staged paths that belong to local diagnostic surfaces."""

    blocked: set[str] = set()
    for raw_path in paths:
        path = raw_path.strip().removeprefix("./")
        if not path:
            continue
        if path.startswith(BLOCKED_DIRECTORY_PREFIXES):
            blocked.add(path)
            continue
        pure_path = PurePosixPath(path)
        if len(pure_path.parts) == 1 and any(
            pure_path.match(pattern) for pattern in BLOCKED_ROOT_GLOBS
        ):
            blocked.add(path)
    return tuple(sorted(blocked))


def staged_publish_paths() -> tuple[str, ...]:
    """Read added, copied, modified, or renamed paths from the Git index."""

    completed = subprocess.run(
        (
            "git",
            "diff",
            "--cached",
            "--name-only",
            "--diff-filter=ACMR",
            "-z",
        ),
        check=True,
        capture_output=True,
    )
    return tuple(
        path.decode("utf-8", errors="surrogateescape")
        for path in completed.stdout.split(b"\0")
        if path
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit a JSON result.")
    args = parser.parse_args(argv)
    staged_paths = staged_publish_paths()
    blocked_paths = blocked_staged_paths(staged_paths)
    payload = {
        "schema": "keystone.staged_publish_files.v1",
        "status": "blocked" if blocked_paths else "pass",
        "staged_path_count": len(staged_paths),
        "blocked_path_count": len(blocked_paths),
        "blocked_paths": list(blocked_paths),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif blocked_paths:
        print("Blocked local diagnostic artifacts are staged:")
        for path in blocked_paths:
            print(f"- {path}")
        print("Unstage these paths or move an intentional public asset under docs/.")
    else:
        print(f"Staged publish-file check passed ({len(staged_paths)} paths inspected).")
    return 1 if blocked_paths else 0


if __name__ == "__main__":
    raise SystemExit(main())
