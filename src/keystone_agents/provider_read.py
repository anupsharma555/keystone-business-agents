"""Typed, request-scoped control plane for bounded provider reads.

Provider tools remain responsible for authentication, provider-specific
schemas, pagination, and source projection.  This module gives those tools one
additive contract for budgets, completeness, retries, request-local reuse, and
privacy-safe audit receipts.  Nothing is activated unless a caller explicitly
enters :func:`activate_provider_read_context`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from threading import BoundedSemaphore, RLock
from time import monotonic
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ProviderReadOperation = Literal[
    "aggregate",
    "content",
    "count",
    "list",
    "metadata",
    "project",
    "read",
    "schema",
    "search",
    "verify",
]
ProviderReadCompleteness = Literal["complete", "truncated", "unknown"]
ProviderReadStatus = Literal[
    "success",
    "empty",
    "partial",
    "blocked",
    "error",
    "deadline_exceeded",
]
ProviderReadErrorCode = Literal[
    "byte_limit_exceeded",
    "call_limit_exceeded",
    "completeness_required",
    "concurrency_limited",
    "continuation_invalid",
    "deadline_exceeded",
    "incomplete_response",
    "invalid_response",
    "item_limit_exceeded",
    "page_limit_exceeded",
    "provider_auth_failed",
    "provider_throttled",
    "provider_timeout",
    "provider_unavailable",
    "read_blocked",
]

_READ_SCHEMA = "keystone.provider_read_plan.v1"
_SNAPSHOT_SCHEMA = "keystone.provider_read_snapshot.v1"
_RECEIPT_SCHEMA = "keystone.provider_read_receipt.v1"
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_MAX_PROVIDER_CALLS = 200
_MAX_READ_BYTES = 100 * 1024 * 1024
_MAX_SECRET_TEXT = 4_000
_MAX_QUERY_TEXT = 8_000

T = TypeVar("T")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_fingerprint(value: Any) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return _sha256_text(serialized)


def _fingerprints_for_mapping(value: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(_sha256_text(f"{key}\0{item}") for key, item in sorted(value.items()))


def _validate_sha256(value: str, *, field_name: str) -> str:
    clean = str(value or "").strip().lower()
    if clean and not _SHA256.fullmatch(clean):
        raise ValueError(f"{field_name} must be an empty value or a SHA-256 fingerprint")
    return clean


def _bounded_secret_mapping(value: Any, *, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    if len(value) > 12:
        raise ValueError(f"{field_name} may contain at most 12 entries")
    bounded: dict[str, str] = {}
    for raw_key, raw_item in value.items():
        key = str(raw_key or "").strip().lower()
        item = str(raw_item or "").strip()
        if not _SAFE_NAME.fullmatch(key):
            raise ValueError(f"{field_name} contains an invalid key")
        if not item:
            continue
        if len(item) > _MAX_SECRET_TEXT:
            raise ValueError(f"{field_name} values may not exceed {_MAX_SECRET_TEXT} characters")
        bounded[key] = item
    return bounded


class ProviderReadPolicy(BaseModel):
    """Hard ceilings and safe retry behavior for one logical provider read."""

    model_config = ConfigDict(frozen=True)

    max_items: int = Field(default=100, ge=1, le=5_000)
    max_pages: int = Field(default=10, ge=1, le=100)
    max_bytes: int = Field(default=5 * 1024 * 1024, ge=1, le=_MAX_READ_BYTES)
    max_provider_calls: int = Field(default=20, ge=1, le=_MAX_PROVIDER_CALLS)
    timeout_seconds: float = Field(default=30.0, gt=0.0, le=300.0)
    max_retries: int = Field(default=1, ge=0, le=3)
    max_concurrency: int = Field(default=1, ge=1, le=8)
    base_retry_delay_seconds: float = Field(default=0.25, ge=0.0, le=5.0)
    max_retry_delay_seconds: float = Field(default=8.0, ge=0.0, le=30.0)
    retry_jitter_ratio: float = Field(default=0.2, ge=0.0, le=0.5)
    retryable_http_statuses: tuple[int, ...] = tuple(sorted(_RETRYABLE_HTTP_STATUSES))

    @field_validator("retryable_http_statuses", mode="before")
    @classmethod
    def _validate_retryable_statuses(cls, value: Any) -> tuple[int, ...]:
        statuses = tuple(dict.fromkeys(int(item) for item in (value or ())))
        unsupported = set(statuses).difference(_RETRYABLE_HTTP_STATUSES)
        if unsupported:
            raise ValueError(
                "retryable_http_statuses contains a status outside the bounded "
                "read-only retry allowlist"
            )
        return statuses

    @model_validator(mode="after")
    def _validate_retry_delays(self) -> ProviderReadPolicy:
        if self.max_retry_delay_seconds < self.base_retry_delay_seconds:
            raise ValueError(
                "max_retry_delay_seconds must be greater than or equal to base_retry_delay_seconds"
            )
        if self.max_provider_calls < self.max_retries + 1:
            raise ValueError("max_provider_calls must reserve the initial attempt plus every retry")
        return self

    def can_retry_http_status(self, status_code: int, *, retry_number: int) -> bool:
        """Return whether one numbered retry is allowed for a transient read failure."""

        return bool(
            1 <= retry_number <= self.max_retries
            and int(status_code) in self.retryable_http_statuses
        )

    def retry_delay_seconds(
        self,
        retry_number: int,
        *,
        retry_after_seconds: float | None = None,
        jitter_sample: float = 0.5,
    ) -> float:
        """Return a bounded exponential delay without sleeping.

        ``jitter_sample`` is injectable so tests and callers with recorded
        randomness can reproduce the exact policy decision.
        """

        if not 1 <= int(retry_number) <= self.max_retries:
            raise ValueError("retry_number exceeds the configured retry budget")
        exponential = min(
            self.max_retry_delay_seconds,
            self.base_retry_delay_seconds * (2 ** (int(retry_number) - 1)),
        )
        sample = max(0.0, min(1.0, float(jitter_sample)))
        jitter_factor = 1.0 + self.retry_jitter_ratio * ((2.0 * sample) - 1.0)
        delay = exponential * jitter_factor
        if retry_after_seconds is not None:
            bounded_retry_after = max(
                0.0,
                min(self.max_retry_delay_seconds, float(retry_after_seconds)),
            )
            delay = max(delay, bounded_retry_after)
        return round(max(0.0, min(self.max_retry_delay_seconds, delay)), 6)


class ProviderReadPlan(BaseModel):
    """Privacy-aware execution plan for one bounded read-only provider operation."""

    model_config = ConfigDict(frozen=True)

    schema_name: str = _READ_SCHEMA
    provider: str
    operation: ProviderReadOperation
    resource: str
    projection: tuple[str, ...] = ()
    policy: ProviderReadPolicy = Field(default_factory=ProviderReadPolicy)
    completeness_required: bool = False
    capability_profile_fingerprint: str = ""

    # Exact identifiers, queries, scope values, and continuation tokens are
    # usable by provider adapters but excluded from serialization and repr.
    identity: dict[str, str] = Field(default_factory=dict, exclude=True, repr=False)
    scope: dict[str, str] = Field(default_factory=dict, exclude=True, repr=False)
    query: str = Field(default="", exclude=True, repr=False)
    continuation_token: str = Field(default="", exclude=True, repr=False)

    identity_fingerprints: tuple[str, ...] = ()
    scope_fingerprint: str = ""
    query_fingerprint: str = ""
    continuation_fingerprint: str = ""
    plan_fingerprint: str = ""

    @field_validator("provider", "resource", mode="before")
    @classmethod
    def _validate_name(cls, value: Any) -> str:
        clean = str(value or "").strip().lower()
        if not _SAFE_NAME.fullmatch(clean):
            raise ValueError("provider and resource names must be bounded lowercase identifiers")
        return clean

    @field_validator("projection", mode="before")
    @classmethod
    def _validate_projection(cls, value: Any) -> tuple[str, ...]:
        fields = tuple(
            dict.fromkeys(
                str(item or "").strip() for item in (value or ()) if str(item or "").strip()
            )
        )
        if len(fields) > 50:
            raise ValueError("projection may contain at most 50 fields")
        if any(len(item) > 160 or any(ord(char) < 32 for char in item) for item in fields):
            raise ValueError("projection contains an invalid field")
        return fields

    @field_validator("identity", mode="before")
    @classmethod
    def _validate_identity(cls, value: Any) -> dict[str, str]:
        return _bounded_secret_mapping(value, field_name="identity")

    @field_validator("scope", mode="before")
    @classmethod
    def _validate_scope(cls, value: Any) -> dict[str, str]:
        return _bounded_secret_mapping(value, field_name="scope")

    @field_validator("query", mode="before")
    @classmethod
    def _validate_query(cls, value: Any) -> str:
        clean = str(value or "").strip()
        if len(clean) > _MAX_QUERY_TEXT:
            raise ValueError(f"query may not exceed {_MAX_QUERY_TEXT} characters")
        return clean

    @field_validator("continuation_token", mode="before")
    @classmethod
    def _validate_continuation_token(cls, value: Any) -> str:
        clean = str(value or "").strip()
        if len(clean) > _MAX_SECRET_TEXT:
            raise ValueError(f"continuation_token may not exceed {_MAX_SECRET_TEXT} characters")
        return clean

    @field_validator(
        "capability_profile_fingerprint",
        "scope_fingerprint",
        "query_fingerprint",
        "continuation_fingerprint",
        "plan_fingerprint",
        mode="before",
    )
    @classmethod
    def _validate_fingerprint_fields(cls, value: Any, info: Any) -> str:
        return _validate_sha256(str(value or ""), field_name=info.field_name)

    @field_validator("identity_fingerprints", mode="before")
    @classmethod
    def _validate_identity_fingerprints(cls, value: Any) -> tuple[str, ...]:
        return tuple(
            _validate_sha256(str(item or ""), field_name="identity_fingerprints")
            for item in (value or ())
        )

    @model_validator(mode="after")
    def _compile_fingerprints(self) -> ProviderReadPlan:
        identity_fingerprints = _fingerprints_for_mapping(self.identity)
        if self.identity and self.identity_fingerprints not in {(), identity_fingerprints}:
            raise ValueError("identity_fingerprints do not match the private identity")
        identity_fingerprints = self.identity_fingerprints or identity_fingerprints

        scope_fingerprint = _stable_fingerprint(self.scope) if self.scope else ""
        if self.scope and self.scope_fingerprint not in {"", scope_fingerprint}:
            raise ValueError("scope_fingerprint does not match the private scope")
        scope_fingerprint = self.scope_fingerprint or scope_fingerprint

        query_fingerprint = _sha256_text(self.query) if self.query else ""
        if self.query and self.query_fingerprint not in {"", query_fingerprint}:
            raise ValueError("query_fingerprint does not match the private query")
        query_fingerprint = self.query_fingerprint or query_fingerprint

        continuation_fingerprint = (
            _sha256_text(self.continuation_token) if self.continuation_token else ""
        )
        if self.continuation_token and self.continuation_fingerprint not in {
            "",
            continuation_fingerprint,
        }:
            raise ValueError(
                "continuation_fingerprint does not match the private continuation token"
            )
        continuation_fingerprint = self.continuation_fingerprint or continuation_fingerprint

        fingerprint_payload = {
            "provider": self.provider,
            "operation": self.operation,
            "resource": self.resource,
            "projection": self.projection,
            "policy": self.policy.model_dump(mode="json"),
            "completeness_required": self.completeness_required,
            "capability_profile_fingerprint": self.capability_profile_fingerprint,
            "identity_fingerprints": identity_fingerprints,
            "scope_fingerprint": scope_fingerprint,
            "query_fingerprint": query_fingerprint,
            "continuation_fingerprint": continuation_fingerprint,
        }
        plan_fingerprint = _stable_fingerprint(fingerprint_payload)
        if self.plan_fingerprint not in {"", plan_fingerprint}:
            raise ValueError("plan_fingerprint does not match the read plan")

        object.__setattr__(self, "identity_fingerprints", identity_fingerprints)
        object.__setattr__(self, "scope_fingerprint", scope_fingerprint)
        object.__setattr__(self, "query_fingerprint", query_fingerprint)
        object.__setattr__(self, "continuation_fingerprint", continuation_fingerprint)
        object.__setattr__(self, "plan_fingerprint", plan_fingerprint)
        return self


class ProviderReadSnapshot(BaseModel):
    """Bounded provider data plus serialization-safe completeness metadata."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    schema_name: str = _SNAPSHOT_SCHEMA
    completeness: ProviderReadCompleteness
    item_count: int = Field(default=0, ge=0, le=5_000)
    page_count: int = Field(default=0, ge=0, le=100)
    bytes_read: int = Field(default=0, ge=0, le=_MAX_READ_BYTES)

    # Raw provider payloads and identities must never enter generic receipts.
    payload: Any = Field(default=None, exclude=True, repr=False)
    identity: dict[str, str] = Field(default_factory=dict, exclude=True, repr=False)
    continuation_token: str = Field(default="", exclude=True, repr=False)

    identity_fingerprints: tuple[str, ...] = ()
    continuation_fingerprint: str = ""
    payload_fingerprint: str = ""
    snapshot_fingerprint: str = ""

    @field_validator("identity", mode="before")
    @classmethod
    def _validate_identity(cls, value: Any) -> dict[str, str]:
        return _bounded_secret_mapping(value, field_name="identity")

    @field_validator("continuation_token", mode="before")
    @classmethod
    def _validate_continuation_token(cls, value: Any) -> str:
        clean = str(value or "").strip()
        if len(clean) > _MAX_SECRET_TEXT:
            raise ValueError(f"continuation_token may not exceed {_MAX_SECRET_TEXT} characters")
        return clean

    @field_validator(
        "continuation_fingerprint",
        "payload_fingerprint",
        "snapshot_fingerprint",
        mode="before",
    )
    @classmethod
    def _validate_fingerprint_fields(cls, value: Any, info: Any) -> str:
        return _validate_sha256(str(value or ""), field_name=info.field_name)

    @field_validator("identity_fingerprints", mode="before")
    @classmethod
    def _validate_identity_fingerprints(cls, value: Any) -> tuple[str, ...]:
        return tuple(
            _validate_sha256(str(item or ""), field_name="identity_fingerprints")
            for item in (value or ())
        )

    @model_validator(mode="after")
    def _validate_and_fingerprint(self) -> ProviderReadSnapshot:
        identity_fingerprints = _fingerprints_for_mapping(self.identity)
        if self.identity and self.identity_fingerprints not in {(), identity_fingerprints}:
            raise ValueError("identity_fingerprints do not match the private identity")
        identity_fingerprints = self.identity_fingerprints or identity_fingerprints

        continuation_fingerprint = (
            _sha256_text(self.continuation_token) if self.continuation_token else ""
        )
        if self.continuation_token and self.continuation_fingerprint not in {
            "",
            continuation_fingerprint,
        }:
            raise ValueError(
                "continuation_fingerprint does not match the private continuation token"
            )
        continuation_fingerprint = self.continuation_fingerprint or continuation_fingerprint
        if self.completeness == "complete" and continuation_fingerprint:
            raise ValueError("a complete snapshot cannot expose a continuation")
        if continuation_fingerprint and self.completeness != "truncated":
            raise ValueError("a continuation requires truncated completeness")

        payload_fingerprint = _stable_fingerprint(self.payload) if self.payload is not None else ""
        if self.payload is not None and self.payload_fingerprint not in {
            "",
            payload_fingerprint,
        }:
            raise ValueError("payload_fingerprint does not match the private payload")
        payload_fingerprint = self.payload_fingerprint or payload_fingerprint

        fingerprint_payload = {
            "completeness": self.completeness,
            "item_count": self.item_count,
            "page_count": self.page_count,
            "bytes_read": self.bytes_read,
            "identity_fingerprints": identity_fingerprints,
            "continuation_fingerprint": continuation_fingerprint,
            "payload_fingerprint": payload_fingerprint,
        }
        snapshot_fingerprint = _stable_fingerprint(fingerprint_payload)
        if self.snapshot_fingerprint not in {"", snapshot_fingerprint}:
            raise ValueError("snapshot_fingerprint does not match the snapshot")

        object.__setattr__(self, "identity_fingerprints", identity_fingerprints)
        object.__setattr__(self, "continuation_fingerprint", continuation_fingerprint)
        object.__setattr__(self, "payload_fingerprint", payload_fingerprint)
        object.__setattr__(self, "snapshot_fingerprint", snapshot_fingerprint)
        return self

    @property
    def complete(self) -> bool:
        return self.completeness == "complete"

    @property
    def truncated(self) -> bool:
        return self.completeness == "truncated"

    @property
    def continuation_available(self) -> bool:
        return bool(self.continuation_fingerprint)


class ProviderReadReceipt(BaseModel):
    """Bounded audit receipt that cannot carry provider content or raw identifiers."""

    model_config = ConfigDict(frozen=True)

    schema_name: str = _RECEIPT_SCHEMA
    provider: str
    operation: ProviderReadOperation
    resource: str
    status: ProviderReadStatus
    provider_read: Literal[True] = True
    provider_write: Literal[False] = False
    send_enabled: Literal[False] = False
    plan_fingerprint: str
    capability_profile_fingerprint: str = ""
    snapshot_fingerprint: str = ""
    completeness: ProviderReadCompleteness = "unknown"
    item_count: int = Field(default=0, ge=0, le=5_000)
    page_count: int = Field(default=0, ge=0, le=100)
    bytes_read: int = Field(default=0, ge=0, le=_MAX_READ_BYTES)
    continuation_available: bool = False
    attempt_count: int = Field(default=0, ge=0, le=_MAX_PROVIDER_CALLS)
    retry_count: int = Field(default=0, ge=0, le=3)
    elapsed_ms: float = Field(default=0.0, ge=0.0)
    deadline_exceeded: bool = False
    cache_hit: bool = False
    identity_fingerprints: tuple[str, ...] = ()
    error_codes: tuple[ProviderReadErrorCode, ...] = ()

    @field_validator("provider", "resource", mode="before")
    @classmethod
    def _validate_name(cls, value: Any) -> str:
        clean = str(value or "").strip().lower()
        if not _SAFE_NAME.fullmatch(clean):
            raise ValueError("provider and resource names must be bounded lowercase identifiers")
        return clean

    @field_validator(
        "plan_fingerprint",
        "capability_profile_fingerprint",
        "snapshot_fingerprint",
        mode="before",
    )
    @classmethod
    def _validate_fingerprint_fields(cls, value: Any, info: Any) -> str:
        return _validate_sha256(str(value or ""), field_name=info.field_name)

    @field_validator("identity_fingerprints", mode="before")
    @classmethod
    def _validate_identity_fingerprints(cls, value: Any) -> tuple[str, ...]:
        return tuple(
            _validate_sha256(str(item or ""), field_name="identity_fingerprints")
            for item in (value or ())
        )

    @field_validator("error_codes", mode="before")
    @classmethod
    def _validate_error_codes(cls, value: Any) -> tuple[str, ...]:
        codes = tuple(
            dict.fromkeys(
                str(item or "").strip() for item in (value or ()) if str(item or "").strip()
            )
        )
        if len(codes) > 8:
            raise ValueError("error_codes may contain at most 8 symbolic codes")
        return codes

    @model_validator(mode="after")
    def _validate_state(self) -> ProviderReadReceipt:
        if self.retry_count > self.attempt_count:
            raise ValueError("retry_count cannot exceed attempt_count")
        if self.retry_count and self.attempt_count < self.retry_count + 1:
            raise ValueError("a retry receipt must include the initial attempt")
        if self.completeness == "complete" and (
            self.continuation_available or self.status in {"partial", "deadline_exceeded"}
        ):
            raise ValueError("complete receipts cannot be partial or expose continuation")
        if self.continuation_available and self.completeness != "truncated":
            raise ValueError("continuation_available requires truncated completeness")
        if self.status in {"success", "empty"} and self.completeness != "complete":
            raise ValueError("successful receipts require complete snapshot semantics")
        if self.status == "deadline_exceeded" and not self.deadline_exceeded:
            raise ValueError("deadline_exceeded status requires deadline_exceeded=true")
        return self

    def receipt(self) -> dict[str, Any]:
        """Return the safe serialized receipt for traces and request metadata."""

        return self.model_dump(mode="json")


class ProviderReadContextError(RuntimeError):
    """Base error for bounded provider-read execution context failures."""


class ProviderReadDeadlineExceeded(ProviderReadContextError):
    """The request deadline elapsed before another read could start."""


class ProviderReadConcurrencyLimitExceeded(ProviderReadContextError):
    """No bounded provider-read slot became available before the wait ceiling."""


class ProviderReadExecutionContext:
    """Thread-safe budgets and request-local reuse for one provider read plan."""

    def __init__(
        self,
        plan: ProviderReadPlan,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.plan = plan
        self._clock = clock or monotonic
        self._started_at = float(self._clock())
        self._lock = RLock()
        self._slots = BoundedSemaphore(plan.policy.max_concurrency)
        self._attempt_count = 0
        self._retry_count = 0
        self._active_count = 0
        self._max_active_count = 0
        self._services: dict[str, Any] = {}
        self._snapshots: dict[str, ProviderReadSnapshot] = {}

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, float(self._clock()) - self._started_at)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.plan.policy.timeout_seconds - self.elapsed_seconds)

    @property
    def deadline_exceeded(self) -> bool:
        return self.remaining_seconds <= 0.0

    @property
    def attempt_count(self) -> int:
        with self._lock:
            return self._attempt_count

    @property
    def retry_count(self) -> int:
        with self._lock:
            return self._retry_count

    @property
    def max_active_count(self) -> int:
        with self._lock:
            return self._max_active_count

    def try_start_attempt(self, *, retry: bool = False) -> bool:
        """Consume one provider-call slot without performing provider I/O."""

        with self._lock:
            if self.deadline_exceeded:
                return False
            if self._attempt_count >= self.plan.policy.max_provider_calls:
                return False
            if retry and self._attempt_count == 0:
                return False
            if retry and self._retry_count >= self.plan.policy.max_retries:
                return False
            self._attempt_count += 1
            if retry:
                self._retry_count += 1
            return True

    def retry_delay_seconds(
        self,
        status_code: int,
        *,
        retry_after_seconds: float | None = None,
        jitter_sample: float = 0.5,
    ) -> float | None:
        """Return the next safe delay, or ``None`` when retry is not admissible."""

        retry_number = self.retry_count + 1
        if not self.plan.policy.can_retry_http_status(
            status_code,
            retry_number=retry_number,
        ):
            return None
        return self.plan.policy.retry_delay_seconds(
            retry_number,
            retry_after_seconds=retry_after_seconds,
            jitter_sample=jitter_sample,
        )

    @contextmanager
    def read_slot(self, *, timeout_seconds: float | None = None) -> Iterator[None]:
        """Acquire one bounded concurrency slot for provider-owned read code."""

        if self.deadline_exceeded:
            raise ProviderReadDeadlineExceeded("provider read deadline exceeded")
        wait_seconds = self.remaining_seconds
        if timeout_seconds is not None:
            wait_seconds = min(wait_seconds, max(0.0, float(timeout_seconds)))
        acquired = self._slots.acquire(timeout=wait_seconds)
        if not acquired:
            if self.deadline_exceeded:
                raise ProviderReadDeadlineExceeded("provider read deadline exceeded")
            raise ProviderReadConcurrencyLimitExceeded(
                "provider read concurrency slot was unavailable"
            )
        with self._lock:
            self._active_count += 1
            self._max_active_count = max(self._max_active_count, self._active_count)
        try:
            yield
        finally:
            with self._lock:
                self._active_count -= 1
            self._slots.release()

    def service(self, key: str, factory: Callable[[], T]) -> T:
        """Build one request-local service once and reuse it by exact key."""

        clean_key = self._cache_key(key)
        with self._lock:
            if clean_key not in self._services:
                self._services[clean_key] = factory()
            return self._services[clean_key]

    def remember_snapshot(self, key: str, snapshot: ProviderReadSnapshot) -> None:
        """Store one immutable, policy-compliant request-local snapshot."""

        self._validate_snapshot_bounds(snapshot)
        clean_key = self._cache_key(key)
        with self._lock:
            self._snapshots[clean_key] = snapshot

    def snapshot(self, key: str) -> ProviderReadSnapshot | None:
        clean_key = self._cache_key(key)
        with self._lock:
            return self._snapshots.get(clean_key)

    def build_receipt(
        self,
        *,
        snapshot: ProviderReadSnapshot | None = None,
        status: ProviderReadStatus | None = None,
        cache_hit: bool = False,
        error_codes: tuple[ProviderReadErrorCode, ...] = (),
    ) -> ProviderReadReceipt:
        """Build a privacy-safe receipt from counters and bounded snapshot metadata."""

        if snapshot is not None:
            self._validate_snapshot_bounds(snapshot)
        deadline_exceeded = bool(
            (status == "deadline_exceeded")
            or (self.deadline_exceeded and not (snapshot is not None and snapshot.complete))
        )
        if status is None:
            if snapshot is not None and snapshot.complete:
                status = "empty" if snapshot.item_count == 0 else "success"
            elif deadline_exceeded:
                status = "deadline_exceeded"
            elif snapshot is not None:
                status = "partial"
            else:
                status = "blocked"

        return ProviderReadReceipt(
            provider=self.plan.provider,
            operation=self.plan.operation,
            resource=self.plan.resource,
            status=status,
            plan_fingerprint=self.plan.plan_fingerprint,
            capability_profile_fingerprint=self.plan.capability_profile_fingerprint,
            snapshot_fingerprint=(snapshot.snapshot_fingerprint if snapshot is not None else ""),
            completeness=snapshot.completeness if snapshot is not None else "unknown",
            item_count=snapshot.item_count if snapshot is not None else 0,
            page_count=snapshot.page_count if snapshot is not None else 0,
            bytes_read=snapshot.bytes_read if snapshot is not None else 0,
            continuation_available=(
                snapshot.continuation_available if snapshot is not None else False
            ),
            attempt_count=self.attempt_count,
            retry_count=self.retry_count,
            elapsed_ms=round(self.elapsed_seconds * 1000.0, 3),
            deadline_exceeded=deadline_exceeded,
            cache_hit=cache_hit,
            identity_fingerprints=(
                snapshot.identity_fingerprints
                if snapshot is not None and snapshot.identity_fingerprints
                else self.plan.identity_fingerprints
            ),
            error_codes=error_codes,
        )

    def _validate_snapshot_bounds(self, snapshot: ProviderReadSnapshot) -> None:
        policy = self.plan.policy
        if snapshot.item_count > policy.max_items:
            raise ValueError("snapshot item_count exceeds the provider read plan")
        if snapshot.page_count > policy.max_pages:
            raise ValueError("snapshot page_count exceeds the provider read plan")
        if snapshot.bytes_read > policy.max_bytes:
            raise ValueError("snapshot bytes_read exceeds the provider read plan")
        if self.plan.completeness_required and not snapshot.complete:
            raise ValueError("the provider read plan requires a complete snapshot")

    @staticmethod
    def _cache_key(value: str) -> str:
        clean = str(value or "").strip()
        if not clean or len(clean) > 240 or any(ord(char) < 32 for char in clean):
            raise ValueError("request-local cache keys must be non-empty bounded text")
        return clean


_PROVIDER_READ_CONTEXT: ContextVar[ProviderReadExecutionContext | None] = ContextVar(
    "keystone_provider_read_context",
    default=None,
)


def current_provider_read_context() -> ProviderReadExecutionContext | None:
    """Return the active context, or ``None`` for unchanged legacy behavior."""

    return _PROVIDER_READ_CONTEXT.get()


def record_provider_read_result(
    tool_name: str,
    result: Any,
) -> ProviderReadReceipt | None:
    """Journal one bounded provider result without storing its raw content."""

    context = current_provider_read_context()
    if context is None or context.attempt_count <= 0:
        return None
    result_status = (
        str(result.get("status") or "").strip().lower() if isinstance(result, Mapping) else ""
    )
    if result_status in {"dry-run", "preview"}:
        return None
    completeness: ProviderReadCompleteness = "complete"
    if isinstance(result, Mapping) and (
        bool(result.get("truncated"))
        or bool(result.get("continuation_available"))
        or bool(result.get("next_page_token"))
        or bool(result.get("nextPageToken"))
    ):
        completeness = "truncated"
    snapshot = ProviderReadSnapshot(
        completeness=completeness,
        item_count=_provider_result_item_count(result),
        page_count=_provider_result_page_count(result),
        bytes_read=len(
            json.dumps(
                result,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ),
        payload=result,
    )
    snapshot_key = f"{str(tool_name or 'provider_read').strip()}:{context.attempt_count}"
    context.remember_snapshot(snapshot_key, snapshot)
    status: ProviderReadStatus | None = None
    if result_status in {"blocked", "denied"}:
        status = "blocked"
    elif result_status in {"error", "failed"}:
        status = "error"
    receipt = context.build_receipt(snapshot=snapshot, status=status)

    from keystone_agents.tool_receipt_journal import record_tool_output

    record_tool_output(f"{tool_name}.provider_read", receipt.receipt())
    return receipt


def _provider_result_item_count(result: Any) -> int:
    if isinstance(result, Mapping):
        explicit = result.get("item_count")
        if isinstance(explicit, int) and explicit >= 0:
            return explicit
        for key in ("items", "messages", "threads", "events", "rows", "files"):
            value = result.get(key)
            if isinstance(value, list | tuple):
                return len(value)
        return 0 if str(result.get("status") or "").lower() in {"missing", "empty"} else 1
    if isinstance(result, list | tuple):
        return len(result)
    return int(result is not None)


def _provider_result_page_count(result: Any) -> int:
    if isinstance(result, Mapping):
        explicit = result.get("page_count")
        if isinstance(explicit, int) and explicit >= 0:
            return explicit
    return 1


@contextmanager
def activate_provider_read_context(
    plan: ProviderReadPlan | None,
    *,
    clock: Callable[[], float] | None = None,
) -> Iterator[ProviderReadExecutionContext | None]:
    """Activate one isolated context; ``None`` is an intentional no-op."""

    if plan is None:
        yield current_provider_read_context()
        return
    context = ProviderReadExecutionContext(plan, clock=clock)
    token = _PROVIDER_READ_CONTEXT.set(context)
    try:
        yield context
    finally:
        _PROVIDER_READ_CONTEXT.reset(token)


__all__ = [
    "ProviderReadCompleteness",
    "ProviderReadConcurrencyLimitExceeded",
    "ProviderReadContextError",
    "ProviderReadDeadlineExceeded",
    "ProviderReadErrorCode",
    "ProviderReadExecutionContext",
    "ProviderReadOperation",
    "ProviderReadPlan",
    "ProviderReadPolicy",
    "ProviderReadReceipt",
    "ProviderReadSnapshot",
    "ProviderReadStatus",
    "activate_provider_read_context",
    "current_provider_read_context",
    "record_provider_read_result",
]
