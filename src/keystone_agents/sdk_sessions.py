"""Local Agents SDK conversation-session helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from keystone_agents.sdk import build_sqlite_session

SDK_SESSIONS_ENABLED_ENV = "KEYSTONE_SDK_SESSIONS"
SDK_SESSION_ID_ENV = "KEYSTONE_SDK_SESSION_ID"
SDK_SESSION_DB_ENV = "KEYSTONE_SDK_SESSION_DB"
DEFAULT_SESSION_DB_PATH = ".keystone/sdk_sessions.sqlite3"

_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}
_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
_SAFE_SCOPE_RE = re.compile(r"[^a-z0-9_]+")
_SESSION_AUDIT_METADATA_BY_OBJECT_ID: dict[int, dict[str, Any]] = {}


@dataclass(frozen=True)
class SDKSessionSpec:
    """Resolved local SDK session configuration."""

    enabled: bool
    session_id: str = ""
    database_path: str = ""
    scope: str = ""
    source: str = ""

    def log_metadata(self) -> dict[str, Any]:
        """Return audit-safe metadata without raw source identifiers."""

        return {
            "enabled": self.enabled,
            "scope": self.scope,
            "source": self.source,
            "session_id_hash": _short_hash(self.session_id) if self.session_id else "",
            "database_path": self.database_path if self.enabled else "",
        }


def sdk_sessions_enabled(default: bool = False) -> bool:
    """Return whether local SDK sessions are enabled by environment."""

    raw = os.environ.get(SDK_SESSIONS_ENABLED_ENV)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return default


def default_session_database_path() -> str:
    """Return the local SQLite path used for SDK session history."""

    configured = os.environ.get(SDK_SESSION_DB_ENV, "").strip()
    return configured or DEFAULT_SESSION_DB_PATH


def derive_sdk_session_id(scope: str, components: tuple[str, ...] | list[str]) -> str:
    """Return an opaque stable session id for a logical conversation scope."""

    safe_scope = _safe_scope(scope)
    normalized = [str(component or "").strip() for component in components]
    if not any(normalized):
        normalized = ["default"]
    payload = json.dumps(
        {"scope": safe_scope, "components": normalized},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"kba_{safe_scope}_{digest}"


def resolve_sdk_session_spec(
    *,
    scope: str,
    components: tuple[str, ...] | list[str] = (),
    enabled: bool | None = None,
    explicit_session_id: str = "",
    database_path: str = "",
    default_enabled: bool = False,
) -> SDKSessionSpec:
    """Resolve a local SDK session spec from CLI/env policy."""

    if enabled is None:
        enabled = bool(explicit_session_id or database_path) or sdk_sessions_enabled(
            default=default_enabled
        )
    if not enabled:
        return SDKSessionSpec(enabled=False, scope=_safe_scope(scope), source="disabled")

    resolved_components = (
        ("explicit", explicit_session_id) if explicit_session_id else tuple(components)
    )
    session_id = derive_sdk_session_id(scope, list(resolved_components))
    return SDKSessionSpec(
        enabled=True,
        session_id=session_id,
        database_path=database_path.strip() or default_session_database_path(),
        scope=_safe_scope(scope),
        source="explicit" if explicit_session_id else "derived",
    )


def sdk_session_env(spec: SDKSessionSpec) -> dict[str, str]:
    """Return environment variables that make child SDK runs use this session."""

    if not spec.enabled:
        return {SDK_SESSIONS_ENABLED_ENV: "false"}
    return {
        SDK_SESSIONS_ENABLED_ENV: "true",
        SDK_SESSION_ID_ENV: spec.session_id,
        SDK_SESSION_DB_ENV: spec.database_path,
    }


def apply_sdk_session_env(spec: SDKSessionSpec) -> None:
    """Apply a resolved SDK session spec to this process environment."""

    for key, value in sdk_session_env(spec).items():
        os.environ[key] = value


def build_sdk_session(spec: SDKSessionSpec) -> Any | None:
    """Build a local SDK session for a resolved spec."""

    if not spec.enabled:
        return None
    session = build_sqlite_session(spec.session_id, spec.database_path)
    return _annotate_session(session, spec.log_metadata())


def build_sdk_session_from_env() -> Any | None:
    """Build a local SDK session from explicit session environment variables."""

    session_id = os.environ.get(SDK_SESSION_ID_ENV, "").strip()
    if not session_id or not sdk_sessions_enabled(default=True):
        return None
    database_path = default_session_database_path()
    session = build_sqlite_session(session_id, database_path)
    return _annotate_session(
        session,
        {
            "enabled": True,
            "scope": _scope_from_session_id(session_id),
            "source": "env",
            "session_id_hash": _short_hash(session_id),
            "database_path": database_path,
        },
    )


def session_audit_metadata(session: Any | None) -> dict[str, Any]:
    """Return audit-safe metadata attached to a locally built SDK session."""

    if session is None:
        return {}
    metadata = _SESSION_AUDIT_METADATA_BY_OBJECT_ID.get(id(session), {})
    if metadata:
        return dict(metadata)
    return {
        "scope": str(getattr(session, "_keystone_scope", "") or ""),
        "source": str(getattr(session, "_keystone_source", "") or ""),
        "session_id_hash": str(getattr(session, "_keystone_session_id_hash", "") or ""),
    }


def context_file_session_components(
    context_file_path: str | Path,
) -> tuple[str, tuple[str, ...]] | None:
    """Return a session scope/components tuple for supported local context files."""

    if not context_file_path:
        return None
    try:
        data = json.loads(Path(context_file_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    schema = str(data.get("schema") or data.get("schema_") or "").strip()
    if not schema.startswith("keystone.slack."):
        return None
    team_id = str(data.get("team_id") or "").strip()
    channel_id = str(data.get("channel_id") or "").strip()
    thread_ts = str(
        data.get("thread_ts") or data.get("selected_message_ts") or data.get("request_ts") or ""
    ).strip()
    if not (team_id or channel_id or thread_ts):
        return None
    return "slack", (team_id, channel_id, thread_ts)


def default_cli_ask_session_components(route: str = "") -> tuple[str, ...]:
    """Return stable local components for a default operator CLI conversation."""

    operator = os.environ.get("KEYSTONE_OPERATOR_ID") or os.environ.get("USER") or "local"
    return (operator, str(Path.cwd()), route or "ask")


def _safe_scope(scope: str) -> str:
    safe = _SAFE_SCOPE_RE.sub("_", str(scope or "session").strip().lower()).strip("_")
    return safe or "session"


def _scope_from_session_id(session_id: str) -> str:
    match = re.match(r"^kba_(?P<scope>.+)_[0-9a-f]{32}$", session_id)
    return match.group("scope") if match else "session"


def _annotate_session(session: Any, metadata: dict[str, Any]) -> Any:
    _SESSION_AUDIT_METADATA_BY_OBJECT_ID[id(session)] = dict(metadata)
    for key, value in metadata.items():
        try:
            setattr(session, f"_keystone_{key}", value)
        except Exception:
            continue
    return session


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
