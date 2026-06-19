"""Allowlisted context integration config shared with sibling Keystone repos."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

CONTEXT_CONFIG_REPO_ENV = "KEYSTONE_CONTEXT_CONFIG_REPO"
CONTEXT_CONFIG_OVERRIDE_ENV = "KEYSTONE_CONTEXT_CONFIG_OVERRIDE"
CONTEXT_CONFIG_OVERRIDE_KEYS_ENV = "KEYSTONE_CONTEXT_CONFIG_OVERRIDE_KEYS"
LEGACY_SLACK_REPO_ENV = "KEYSTONE_SLACK_REPO"

CONTEXT_ENV_KEYS: frozenset[str] = frozenset(
    {
        "AIRTABLE_ACCESS_TOKEN",
        "AIRTABLE_ALLOWED_TABLES",
        "AIRTABLE_BASE_ID",
        "AIRTABLE_BASE_NAME",
        "AIRTABLE_DEFAULT_TABLE",
        "AIRTABLE_DEFAULT_VIEW",
        "AIRTABLE_FINANCE_TAX_TRACKER_ACCESS_TOKEN",
        "AIRTABLE_FINANCE_TAX_TRACKER_ALLOWED_TABLES",
        "AIRTABLE_FINANCE_TAX_TRACKER_BASE_ID",
        "AIRTABLE_FINANCE_TAX_TRACKER_BASE_NAME",
        "AIRTABLE_FINANCE_TAX_TRACKER_DEFAULT_TABLE",
        "AIRTABLE_FINANCE_TAX_TRACKER_DEFAULT_VIEW",
        "AIRTABLE_KNI_OPS_ACCESS_TOKEN",
        "AIRTABLE_KNI_OPS_ALLOWED_TABLES",
        "AIRTABLE_KNI_OPS_BASE_ID",
        "AIRTABLE_KNI_OPS_BASE_NAME",
        "AIRTABLE_KNI_OPS_DEFAULT_TABLE",
        "AIRTABLE_KNI_OPS_DEFAULT_VIEW",
        "AIRTABLE_REQUEST_TIMEOUT_SECONDS",
        "AIRTABLE_READ_ALL_MAX_RECORDS",
        "GOOGLE_DRIVE_ACCOUNT",
        "GOOGLE_DRIVE_KNI_OPS_FOLDER",
        "GOOGLE_WORKSPACE_OAUTH_CLIENT_SECRET_PATH",
        "GOOGLE_WORKSPACE_OAUTH_TOKEN_PATH",
        "GOOGLE_WORKSPACE_REQUIRE_ACCOUNT_MATCH",
        "KEYSTONE_GOOGLE_DOCS_FOLDER",
        "KEYSTONE_ZOTERO_IMPORT_CACHE",
        "KEYSTONE_ZOTERO_IMPORT_REPO",
        "ZOTERO_API_KEY",
        "ZOTERO_COLLECTION_CACHE",
        "ZOTERO_ITEMS_CACHE",
        "ZOTERO_LIBRARY_ID",
        "ZOTERO_LIBRARY_TYPE",
    }
)


@dataclass(frozen=True)
class ContextEnvResolution:
    key: str
    value: str
    source: str
    source_repo: Path | None = None


def context_env_value(key: str, default: str = "") -> str:
    """Return a config value from KBA env, then allowlisted sibling repo env."""

    return resolve_context_env_value(key, default).value


def context_env_path(key: str, default: str = "") -> Path:
    """Resolve a possibly relative path using the env file it came from."""

    resolution = resolve_context_env_value(key, default)
    path = Path(resolution.value).expanduser()
    if not path.is_absolute() and resolution.source_repo is not None:
        path = resolution.source_repo / path
    return path.expanduser()


def resolve_context_env_value(key: str, default: str = "") -> ContextEnvResolution:
    """Resolve one allowlisted context variable without exposing secret values."""

    clean_key = str(key or "").strip()
    if not clean_key:
        return ContextEnvResolution(key=clean_key, value=default, source="default")

    override = _truthy(os.getenv(CONTEXT_CONFIG_OVERRIDE_ENV)) or clean_key in _override_keys()
    direct_value = os.getenv(clean_key, "").strip()
    if direct_value and not override:
        return ContextEnvResolution(key=clean_key, value=direct_value, source="environment")

    linked_value, linked_repo = _linked_context_env_value(clean_key)
    if linked_value:
        return ContextEnvResolution(
            key=clean_key,
            value=linked_value,
            source="linked_repo_env",
            source_repo=linked_repo,
        )
    if direct_value:
        return ContextEnvResolution(key=clean_key, value=direct_value, source="environment")
    return ContextEnvResolution(key=clean_key, value=default, source="default")


def linked_context_repo_path() -> Path:
    configured = os.getenv(CONTEXT_CONFIG_REPO_ENV) or os.getenv(LEGACY_SLACK_REPO_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path()


def _linked_context_env_value(key: str) -> tuple[str, Path | None]:
    if key not in CONTEXT_ENV_KEYS:
        return "", None
    if not (os.getenv(CONTEXT_CONFIG_REPO_ENV) or os.getenv(LEGACY_SLACK_REPO_ENV)):
        return "", None
    repo_path = linked_context_repo_path()
    env_path = repo_path / ".env"
    if not env_path.exists():
        return "", None
    values = _parse_env_file(env_path)
    return values.get(key, "").strip(), repo_path


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key.startswith("export "):
            key = key.removeprefix("export ").strip()
        if key not in CONTEXT_ENV_KEYS:
            continue
        values[key] = _strip_env_value(value.strip())
    return values


def _strip_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _override_keys() -> set[str]:
    raw = os.getenv(CONTEXT_CONFIG_OVERRIDE_KEYS_ENV, "")
    return {item.strip() for item in raw.split(",") if item.strip()}
