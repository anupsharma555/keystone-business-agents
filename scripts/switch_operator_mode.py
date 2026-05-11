"""Switch local Keystone operator modes by updating known `.env` flags only."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Final

MANAGED_KEYS: Final[tuple[str, ...]] = (
    "KEYSTONE_LIVE_MODE",
    "KEYSTONE_DRY_RUN",
    "KEYSTONE_ENABLE_LIVE_GMAIL",
    "KEYSTONE_ENABLE_LIVE_SLACK",
    "KEYSTONE_ENABLE_LIVE_RESEARCH",
    "KEYSTONE_ENABLE_LIVE_CRM",
    "SEARCH_PROVIDER",
    "AUTO_SEND_EMAIL",
)
LIVE_SEARCH_PROVIDERS: Final[tuple[str, ...]] = ("serper", "searxng")
ENV_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<indent>\s*)(?P<export>export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P<pre_equals>\s*)=(?P<post_equals>\s*)(?P<body>.*)$"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Switch local Keystone operator mode by rewriting known .env safety/live flags."
        )
    )
    parser.add_argument(
        "mode",
        choices=("dry-run", "live-test", "full-live"),
        help="Target operator profile.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to the .env-style file to update. Defaults to ./.env.",
    )
    parser.add_argument(
        "--search-provider",
        choices=LIVE_SEARCH_PROVIDERS,
        default=None,
        help=(
            "Live search provider for live-test/full-live. Defaults to the current live "
            "provider if present, otherwise serper."
        ),
    )
    return parser


def _strip_quotes(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1].strip()
    return text


def _managed_key(line: str) -> str | None:
    match = ENV_LINE_RE.match(line.rstrip("\n"))
    if match is None:
        return None
    key = match.group("key")
    if key not in MANAGED_KEYS:
        return None
    return key


def _current_values(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in lines:
        match = ENV_LINE_RE.match(line.rstrip("\n"))
        if match is None:
            continue
        key = match.group("key")
        if key not in MANAGED_KEYS:
            continue
        values[key] = _strip_quotes(match.group("body"))
    return values


def _default_live_search_provider(current_values: dict[str, str]) -> str:
    existing = current_values.get("SEARCH_PROVIDER", "").strip().lower()
    if existing in LIVE_SEARCH_PROVIDERS:
        return existing
    return "searxng"


def mode_values(
    mode: str,
    *,
    current_values: dict[str, str] | None = None,
    search_provider: str | None = None,
) -> dict[str, str]:
    resolved_current = current_values or {}
    live_search_provider = search_provider or _default_live_search_provider(resolved_current)
    if mode == "dry-run":
        return {
            "KEYSTONE_LIVE_MODE": "false",
            "KEYSTONE_DRY_RUN": "true",
            "KEYSTONE_ENABLE_LIVE_GMAIL": "false",
            "KEYSTONE_ENABLE_LIVE_SLACK": "false",
            "KEYSTONE_ENABLE_LIVE_RESEARCH": "false",
            "KEYSTONE_ENABLE_LIVE_CRM": "false",
            "SEARCH_PROVIDER": "dry-run",
            "AUTO_SEND_EMAIL": "false",
        }
    if mode == "live-test":
        return {
            "KEYSTONE_LIVE_MODE": "true",
            "KEYSTONE_DRY_RUN": "false",
            "KEYSTONE_ENABLE_LIVE_GMAIL": "true",
            "KEYSTONE_ENABLE_LIVE_SLACK": "false",
            "KEYSTONE_ENABLE_LIVE_RESEARCH": "true",
            "KEYSTONE_ENABLE_LIVE_CRM": "false",
            "SEARCH_PROVIDER": live_search_provider,
            "AUTO_SEND_EMAIL": "false",
        }
    if mode == "full-live":
        return {
            "KEYSTONE_LIVE_MODE": "true",
            "KEYSTONE_DRY_RUN": "false",
            "KEYSTONE_ENABLE_LIVE_GMAIL": "true",
            "KEYSTONE_ENABLE_LIVE_SLACK": "true",
            "KEYSTONE_ENABLE_LIVE_RESEARCH": "true",
            "KEYSTONE_ENABLE_LIVE_CRM": "false",
            "SEARCH_PROVIDER": live_search_provider,
            "AUTO_SEND_EMAIL": "false",
        }
    raise ValueError(f"Unsupported mode: {mode}")


def _split_body_and_comment(body: str) -> tuple[str, str]:
    hash_index = body.find(" #")
    if hash_index == -1:
        return body.rstrip(), ""
    return body[:hash_index].rstrip(), body[hash_index:]


def _render_updated_line(line: str, value: str) -> str:
    newline = "\n" if line.endswith("\n") else ""
    match = ENV_LINE_RE.match(line.rstrip("\n"))
    if match is None:
        raise ValueError(f"Cannot update non-env line: {line!r}")
    _body, comment = _split_body_and_comment(match.group("body"))
    return (
        f"{match.group('indent')}{match.group('export') or ''}{match.group('key')}"
        f"{match.group('pre_equals')}={match.group('post_equals')}{value}{comment}{newline}"
    )


def rewrite_env_text(text: str, values: dict[str, str]) -> str:
    lines = text.splitlines(keepends=True)
    found_keys: set[str] = set()
    updated_lines: list[str] = []

    for line in lines:
        key = _managed_key(line)
        if key is None:
            updated_lines.append(line)
            continue
        updated_lines.append(_render_updated_line(line, values[key]))
        found_keys.add(key)

    missing_keys = [key for key in MANAGED_KEYS if key not in found_keys]
    if missing_keys:
        if updated_lines and not updated_lines[-1].endswith("\n"):
            updated_lines[-1] = f"{updated_lines[-1]}\n"
        if updated_lines and updated_lines[-1].strip():
            updated_lines.append("\n")
        updated_lines.append("# Managed by scripts/switch_operator_mode.py\n")
        for key in missing_keys:
            updated_lines.append(f"{key}={values[key]}\n")
    return "".join(updated_lines)


def switch_env_file(
    env_file: Path,
    *,
    mode: str,
    search_provider: str | None = None,
) -> dict[str, str]:
    original_text = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    current_values = _current_values(original_text.splitlines())
    values = mode_values(mode, current_values=current_values, search_provider=search_provider)
    updated_text = rewrite_env_text(original_text, values)
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(updated_text, encoding="utf-8")
    return values


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env_file = Path(args.env_file)
    values = switch_env_file(
        env_file,
        mode=args.mode,
        search_provider=args.search_provider,
    )
    print(f"Updated {env_file} to {args.mode}.")
    for key in MANAGED_KEYS:
        print(f"{key}={values[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
