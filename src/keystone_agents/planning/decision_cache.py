"""Safe, exact, advisory caching for canonical manual-request plans."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.storage.sqlite_store import (
    database_url_from_env,
    sqlite_path_from_url,
)

PLANNER_DECISION_CACHE_ENV = "KEYSTONE_PLANNER_DECISION_CACHE"
PLANNER_DECISION_CACHE_TTL_SECONDS_ENV = "KEYSTONE_PLANNER_DECISION_CACHE_TTL_SECONDS"
DEFAULT_PLANNER_DECISION_CACHE_TTL_SECONDS = 6 * 60 * 60
PLANNER_DECISION_CACHE_SCHEMA = "keystone.planner_decision_cache.v1"
_FALSE_VALUES = {"", "0", "false", "no", "off", "disabled"}
_MUTATING_OPERATIONS = {"create", "update", "delete", "attach"}
_UNSAFE_INTENTS = {
    "blocked_send",
    "business_system_write",
    "clarification",
    "continue_work_item",
    "outreach_draft",
}
_TEMPORAL_REQUEST = re.compile(
    r"\b(?:today|tomorrow|yesterday|now|currently|current|latest|newest|recent|"
    r"this\s+(?:week|month|quarter|year)|next\s+(?:week|month|quarter|year))\b",
    re.IGNORECASE,
)
_CONTEXTUAL_REFERENCE = re.compile(
    r"\b(?:this|that|it|same|above|again|those|these|former|latter)\b",
    re.IGNORECASE,
)
_SENSITIVE_REQUEST = re.compile(
    r"\b(?:patient|mrn|medical\s+record\s+number|date\s+of\s+birth|dob|ssn|"
    r"social\s+security|api[_ -]?key|password|secret|access[_ -]?token|"
    r"authorization\s*:\s*bearer)\b",
    re.IGNORECASE,
)

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS planner_decision_cache (
    cache_key TEXT PRIMARY KEY,
    schema_name TEXT NOT NULL,
    scope_hash TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    context_hash TEXT NOT NULL,
    profile_fingerprint TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    created_at_unix REAL NOT NULL,
    expires_at_unix REAL NOT NULL,
    last_hit_at_unix REAL,
    hit_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_planner_decision_cache_expires
ON planner_decision_cache(expires_at_unix);
"""


@dataclass(frozen=True)
class PlannerDecisionCacheIdentity:
    """Audit-safe identity for one exact eligible planning request."""

    cache_key: str
    key_hash: str
    scope_hash: str
    request_hash: str
    context_hash: str
    profile_fingerprint: str


@dataclass(frozen=True)
class PlannerDecisionCacheEligibility:
    """Explain whether one canonical plan may enter the advisory cache."""

    eligible: bool
    reason: str


class PlannerDecisionCache:
    """Persist validated plans in the normal local SQLite state database."""

    def __init__(
        self,
        database_url: str | Path | None = None,
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        self.database_url = str(database_url or database_url_from_env())
        self.path = sqlite_path_from_url(database_url or self.database_url)
        self.ttl_seconds = max(
            60,
            int(
                ttl_seconds
                if ttl_seconds is not None
                else _planner_cache_ttl_seconds()
            ),
        )
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._memory_connection: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self.path == ":memory:":
            if self._memory_connection is None:
                self._memory_connection = sqlite3.connect(":memory:")
                self._memory_connection.row_factory = sqlite3.Row
            return self._memory_connection
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _prepare(self, connection: sqlite3.Connection) -> None:
        connection.executescript(_TABLE_SQL)

    def get(
        self,
        identity: PlannerDecisionCacheIdentity,
        *,
        now: float | None = None,
    ) -> ManualRequestPlan | None:
        timestamp = time.time() if now is None else float(now)
        connection = self._connect()
        should_close = connection is not self._memory_connection
        try:
            self._prepare(connection)
            connection.execute(
                "DELETE FROM planner_decision_cache WHERE expires_at_unix <= ?",
                (timestamp,),
            )
            row = connection.execute(
                """
                SELECT plan_json
                FROM planner_decision_cache
                WHERE cache_key = ?
                  AND schema_name = ?
                  AND scope_hash = ?
                  AND request_hash = ?
                  AND context_hash = ?
                  AND profile_fingerprint = ?
                  AND expires_at_unix > ?
                """,
                (
                    identity.cache_key,
                    PLANNER_DECISION_CACHE_SCHEMA,
                    identity.scope_hash,
                    identity.request_hash,
                    identity.context_hash,
                    identity.profile_fingerprint,
                    timestamp,
                ),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            try:
                plan = ManualRequestPlan.model_validate_json(row["plan_json"])
            except (TypeError, ValueError):
                connection.execute(
                    "DELETE FROM planner_decision_cache WHERE cache_key = ?",
                    (identity.cache_key,),
                )
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE planner_decision_cache
                SET hit_count = hit_count + 1, last_hit_at_unix = ?
                WHERE cache_key = ?
                """,
                (timestamp, identity.cache_key),
            )
            connection.commit()
            return plan.model_copy(update={"source": "canonical:planner_cache"})
        finally:
            if should_close:
                connection.close()

    def put(
        self,
        identity: PlannerDecisionCacheIdentity,
        plan: ManualRequestPlan,
        *,
        now: float | None = None,
    ) -> None:
        timestamp = time.time() if now is None else float(now)
        canonical = plan.model_copy(update={"source": "llm"})
        connection = self._connect()
        should_close = connection is not self._memory_connection
        try:
            self._prepare(connection)
            connection.execute(
                """
                INSERT INTO planner_decision_cache (
                    cache_key,
                    schema_name,
                    scope_hash,
                    request_hash,
                    context_hash,
                    profile_fingerprint,
                    plan_json,
                    created_at_unix,
                    expires_at_unix,
                    last_hit_at_unix,
                    hit_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)
                ON CONFLICT(cache_key) DO UPDATE SET
                    schema_name = excluded.schema_name,
                    scope_hash = excluded.scope_hash,
                    request_hash = excluded.request_hash,
                    context_hash = excluded.context_hash,
                    profile_fingerprint = excluded.profile_fingerprint,
                    plan_json = excluded.plan_json,
                    created_at_unix = excluded.created_at_unix,
                    expires_at_unix = excluded.expires_at_unix,
                    last_hit_at_unix = NULL,
                    hit_count = 0
                """,
                (
                    identity.cache_key,
                    PLANNER_DECISION_CACHE_SCHEMA,
                    identity.scope_hash,
                    identity.request_hash,
                    identity.context_hash,
                    identity.profile_fingerprint,
                    canonical.model_dump_json(),
                    timestamp,
                    timestamp + self.ttl_seconds,
                ),
            )
            connection.commit()
        finally:
            if should_close:
                connection.close()

    def stats(self) -> dict[str, int]:
        connection = self._connect()
        should_close = connection is not self._memory_connection
        try:
            self._prepare(connection)
            row = connection.execute(
                """
                SELECT COUNT(*) AS entry_count, COALESCE(SUM(hit_count), 0) AS hit_count
                FROM planner_decision_cache
                """
            ).fetchone()
            return {
                "entry_count": int(row["entry_count"] if row is not None else 0),
                "hit_count": int(row["hit_count"] if row is not None else 0),
            }
        finally:
            if should_close:
                connection.close()


def planner_decision_cache_enabled() -> bool:
    raw = os.getenv(PLANNER_DECISION_CACHE_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSE_VALUES


def planner_decision_cache_eligibility(
    request_text: str,
    plan: ManualRequestPlan,
    *,
    context_hash: str = "",
) -> PlannerDecisionCacheEligibility:
    """Allow only bounded, read/draft-free, non-sensitive canonical advice."""

    normalized = normalize_planner_request(request_text)
    if not normalized:
        return PlannerDecisionCacheEligibility(False, "empty_request")
    if _SENSITIVE_REQUEST.search(normalized):
        return PlannerDecisionCacheEligibility(False, "sensitive_request")
    if _TEMPORAL_REQUEST.search(normalized):
        return PlannerDecisionCacheEligibility(False, "temporal_request")
    if _CONTEXTUAL_REFERENCE.search(normalized) and not context_hash:
        return PlannerDecisionCacheEligibility(False, "unresolved_context_reference")
    if plan.intent in _UNSAFE_INTENTS:
        return PlannerDecisionCacheEligibility(False, f"unsafe_intent:{plan.intent}")
    if _MUTATING_OPERATIONS.intersection(plan.provider_operations):
        return PlannerDecisionCacheEligibility(False, "provider_mutation")
    if plan.requires_approved_context:
        return PlannerDecisionCacheEligibility(False, "approved_context_required")
    if plan.requires_durable_state or plan.workflow:
        return PlannerDecisionCacheEligibility(False, "durable_or_multi_owner")
    if plan.missing_required_information:
        return PlannerDecisionCacheEligibility(False, "missing_required_information")
    if plan.side_effect_policy != "draft_or_read_only":
        return PlannerDecisionCacheEligibility(False, "side_effect_policy")
    return PlannerDecisionCacheEligibility(True, "eligible_exact_advisory_plan")


def build_planner_decision_cache_identity(
    *,
    request_text: str,
    requested_agent: str | None,
    workflow_context: Mapping[str, Any] | None,
    profile_fingerprint: str,
    scope: str | None = None,
) -> PlannerDecisionCacheIdentity:
    normalized_request = normalize_planner_request(request_text)
    request_payload = {
        "request": normalized_request,
        "requested_agent": str(requested_agent or "").strip().lower(),
    }
    context_payload = planner_context_revision_payload(workflow_context)
    resolved_scope = (
        str(scope).strip()
        if scope is not None
        else str(os.getenv("KEYSTONE_SDK_PROMPT_CACHE_SCOPE") or "local-single-operator")
    )
    scope_hash = _hash_text(resolved_scope)
    request_hash = _hash_json(request_payload)
    context_hash = _hash_json(context_payload) if context_payload else ""
    key_payload = {
        "schema": PLANNER_DECISION_CACHE_SCHEMA,
        "scope_hash": scope_hash,
        "request_hash": request_hash,
        "context_hash": context_hash,
        "profile_fingerprint": profile_fingerprint,
    }
    cache_key = f"kba:planner:v1:{_hash_json(key_payload)}"
    return PlannerDecisionCacheIdentity(
        cache_key=cache_key,
        key_hash=_hash_text(cache_key)[:12],
        scope_hash=scope_hash,
        request_hash=request_hash,
        context_hash=context_hash,
        profile_fingerprint=profile_fingerprint,
    )


def planner_context_revision_payload(
    workflow_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return bounded continuity identity without retaining message bodies."""

    if not workflow_context:
        return {}
    context = dict(workflow_context)
    payload: dict[str, Any] = {}
    thread_root = str(context.get("slack_thread_root") or "").strip()
    if thread_root:
        payload["thread_root_hash"] = _hash_text(thread_root)
    continuation = context.get("execution_continuation")
    if isinstance(continuation, Mapping):
        payload["continuation"] = {
            key: (
                _hash_text(str(value))
                if key == "prior_request"
                else str(value or "")[:120]
            )
            for key, value in continuation.items()
            if key in {"work_item_id", "prior_agent", "provider_affinity", "prior_request"}
            and str(value or "").strip()
        }
    work_item = context.get("current_work_item")
    if isinstance(work_item, Mapping):
        payload["work_item"] = {
            key: str(work_item.get(key) or "")[:160]
            for key in ("id", "status", "route", "next_action", "updated_at")
            if str(work_item.get(key) or "").strip()
        }
    provider_scope = context.get("prior_provider_result_scope")
    if isinstance(provider_scope, Mapping) and provider_scope.get("verified") is True:
        payload["provider_scope"] = {
            key: provider_scope.get(key)
            for key in (
                "source_run_id",
                "provider_system",
                "provider_read_scope",
                "result_count",
                "complete",
                "item_refs",
            )
            if provider_scope.get(key) not in (None, "", [])
        }
    recent = context.get("recent_slack_thread")
    if isinstance(recent, list):
        human_hashes: list[str] = []
        for item in recent[-8:]:
            if not isinstance(item, Mapping):
                continue
            role = str(item.get("role") or "").lower()
            source = str(item.get("source_agent") or "").lower()
            if role not in {"user", "human", "operator"} and source in {
                "agent",
                "bot",
                "kni",
            }:
                continue
            summary = str(item.get("summary") or "").strip()
            if summary:
                human_hashes.append(_hash_text(summary))
        if human_hashes:
            payload["recent_human_message_hashes"] = human_hashes[-4:]
    return payload


def planner_profile_fingerprint(
    *,
    instructions: str,
    output_type: type[Any],
    provider: str,
    model: str,
) -> str:
    schema = (
        output_type.model_json_schema()
        if hasattr(output_type, "model_json_schema")
        else {}
    )
    return _hash_json(
        {
            "schema": PLANNER_DECISION_CACHE_SCHEMA,
            "instructions_sha256": _hash_text(instructions),
            "output_schema": schema,
            "provider": str(provider or "").strip().lower(),
            "model": str(model or "").strip(),
        }
    )


def normalize_planner_request(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = re.sub(r"<@[^>]+>", "@kni", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip().casefold()
    return normalized


def _planner_cache_ttl_seconds() -> int:
    raw = os.getenv(PLANNER_DECISION_CACHE_TTL_SECONDS_ENV)
    try:
        value = int(raw) if raw is not None else DEFAULT_PLANNER_DECISION_CACHE_TTL_SECONDS
    except (TypeError, ValueError):
        value = DEFAULT_PLANNER_DECISION_CACHE_TTL_SECONDS
    return max(60, min(value, 24 * 60 * 60))


def _hash_json(value: Any) -> str:
    return _hash_text(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    )


def _hash_text(value: str) -> str:
    return sha256(str(value).encode("utf-8")).hexdigest()


__all__ = [
    "PLANNER_DECISION_CACHE_SCHEMA",
    "PlannerDecisionCache",
    "PlannerDecisionCacheEligibility",
    "PlannerDecisionCacheIdentity",
    "build_planner_decision_cache_identity",
    "normalize_planner_request",
    "planner_context_revision_payload",
    "planner_decision_cache_eligibility",
    "planner_decision_cache_enabled",
    "planner_profile_fingerprint",
]
