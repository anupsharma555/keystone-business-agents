"""Hard, request-local model invocation budgets for Agents SDK execution."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from typing import Any
from uuid import uuid4

MODEL_REQUEST_BUDGET_LIMIT_ENV = "KEYSTONE_MODEL_REQUEST_BUDGET_LIMIT"
MODEL_REQUEST_BUDGET_CORRELATION_ENV = "KEYSTONE_MODEL_REQUEST_BUDGET_CORRELATION_ID"
MODEL_REQUEST_BUDGET_PARENT_CONSUMED_ENV = (
    "KEYSTONE_MODEL_REQUEST_BUDGET_PARENT_CONSUMED"
)
MODEL_REQUEST_BUDGET_SCHEMA = "keystone.model_request_budget.v1"
MODEL_REQUEST_CAPACITY_SCHEMA = "keystone.model_request_capacity.v1"
FINAL_RESPONSE_REQUEST_RESERVE = 1


class ModelRequestBudgetExhausted(RuntimeError):
    """Raised immediately before an SDK model invocation would exceed its budget."""

    def __init__(
        self,
        *,
        limit: int,
        consumed: int,
        stage: str,
        correlation_id: str,
    ) -> None:
        self.limit = int(limit)
        self.consumed = int(consumed)
        self.remaining = max(0, self.limit - self.consumed)
        self.stage = str(stage or "sdk_model_invocation")
        self.correlation_id = str(correlation_id)
        super().__init__(
            "Model request budget exhausted before "
            f"{self.stage}: consumed {self.consumed} of {self.limit}."
        )

    def telemetry(self) -> dict[str, Any]:
        return {
            "schema": MODEL_REQUEST_BUDGET_SCHEMA,
            "correlation_id": self.correlation_id,
            "limit": self.limit,
            "consumed": self.consumed,
            "remaining": self.remaining,
            "exhausted": True,
            "exhaustion_stage": self.stage,
            "enforcement": "pre_model_invocation",
        }


@dataclass(frozen=True)
class ModelRequestCapacity:
    """Model-visible projection of one enforced request budget.

    The mutable ledger remains authoritative. This projection intentionally omits
    correlation IDs and event history; it gives an agent only the capacity facts
    needed to avoid starting a tool sequence that cannot leave room for a final
    response.
    """

    limit: int | None
    consumed: int
    remaining: int | None
    parent_consumed: int = 0
    reserved_final_response_requests: int = FINAL_RESPONSE_REQUEST_RESERVE

    def receipt(self, *, required_future_requests: int = 0) -> dict[str, Any]:
        required = max(0, int(required_future_requests))
        sufficient = None if self.remaining is None else self.remaining >= required
        return {
            "schema": MODEL_REQUEST_CAPACITY_SCHEMA,
            "configured": self.limit is not None,
            "limit": self.limit,
            "consumed": self.consumed,
            "parent_consumed": self.parent_consumed,
            "remaining_model_requests": self.remaining,
            "reserved_final_response_requests": self.reserved_final_response_requests,
            "required_future_requests": required,
            "capacity_sufficient": sufficient,
        }


def current_model_request_capacity(
    *,
    load_environment: bool = True,
) -> ModelRequestCapacity | None:
    """Return a compact projection of the active hard request ledger, if any."""

    ledger = current_model_request_budget(load_environment=load_environment)
    if ledger is None:
        return None
    snapshot = ledger.snapshot()
    return ModelRequestCapacity(
        limit=snapshot.get("limit"),
        consumed=int(snapshot.get("consumed") or 0),
        remaining=snapshot.get("remaining"),
        parent_consumed=int(snapshot.get("parent_consumed") or 0),
    )


@dataclass
class ModelRequestBudget:
    """One mutable, thread-safe ledger shared by all model stages in a request."""

    limit: int | None
    correlation_id: str = field(default_factory=lambda: uuid4().hex)
    parent_consumed: int = 0
    _consumed: int = 0
    _reserved: int = 0
    _exhaustion_stage: str = ""
    _events: list[dict[str, Any]] = field(default_factory=list)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def __post_init__(self) -> None:
        if self.limit is not None and int(self.limit) < 0:
            raise ValueError("Model request budget limit must be non-negative.")
        self.limit = int(self.limit) if self.limit is not None else None
        self.parent_consumed = max(0, int(self.parent_consumed))

    def consume(self, *, stage: str) -> int:
        """Atomically admit and count one model invocation before provider dispatch."""

        normalized_stage = str(stage or "sdk_model_invocation")
        with self._lock:
            if self.limit is not None and self._consumed + self._reserved >= self.limit:
                self._exhaustion_stage = normalized_stage
                self._events.append(
                    {
                        "ordinal": self._consumed + 1,
                        "stage": normalized_stage,
                        "status": "rejected_before_model_invocation",
                    }
                )
                raise ModelRequestBudgetExhausted(
                    limit=self.limit,
                    consumed=self._consumed,
                    stage=normalized_stage,
                    correlation_id=self.correlation_id,
                )
            self._consumed += 1
            self._events.append(
                {
                    "ordinal": self._consumed,
                    "stage": normalized_stage,
                    "status": "consumed_before_model_invocation",
                }
            )
            return self._consumed

    def reserve_child(self, *, stage: str) -> ModelRequestBudgetLease:
        """Reserve the current remaining allowance for one sequential child process."""

        normalized_stage = str(stage or "child_agent_process")
        with self._lock:
            allocation = None
            if self.limit is not None:
                allocation = max(0, self.limit - self._consumed - self._reserved)
                self._reserved += allocation
            self._events.append(
                {
                    "ordinal": self._consumed + 1,
                    "stage": normalized_stage,
                    "status": "child_budget_reserved",
                    "allocated": allocation,
                }
            )
        return ModelRequestBudgetLease(
            ledger=self,
            stage=normalized_stage,
            allocated=allocation,
        )

    def _settle_child(
        self,
        *,
        stage: str,
        allocated: int | None,
        actual: int | None,
        exhaustion_stage: str = "",
    ) -> None:
        with self._lock:
            if allocated is not None:
                self._reserved = max(0, self._reserved - allocated)
            if actual is None:
                consumed = allocated or 0
                status = "child_budget_settled_pessimistically"
            else:
                consumed = max(0, int(actual))
                status = "child_budget_settled"
            if allocated is not None and consumed > allocated:
                self._exhaustion_stage = stage
                consumed = allocated
                status = "child_budget_report_exceeded_allocation"
            self._consumed += consumed
            if self.limit is not None and self._consumed > self.limit:
                self._exhaustion_stage = stage
                self._consumed = self.limit
            if exhaustion_stage:
                self._exhaustion_stage = f"{stage}/{exhaustion_stage}"
            self._events.append(
                {
                    "ordinal": self._consumed,
                    "stage": stage,
                    "status": status,
                    "allocated": allocated,
                    "reported_consumed": actual,
                    "consumed": consumed,
                    "child_exhaustion_stage": exhaustion_stage,
                }
            )

    @property
    def consumed(self) -> int:
        with self._lock:
            return self._consumed

    @property
    def remaining(self) -> int | None:
        with self._lock:
            if self.limit is None:
                return None
            return max(0, self.limit - self._consumed - self._reserved)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            remaining = (
                None
                if self.limit is None
                else max(0, self.limit - self._consumed - self._reserved)
            )
            return {
                "schema": MODEL_REQUEST_BUDGET_SCHEMA,
                "correlation_id": self.correlation_id,
                "limit": self.limit,
                "consumed": self._consumed,
                "parent_consumed": self.parent_consumed,
                "remaining": remaining,
                "reserved": self._reserved,
                "exhausted": bool(self._exhaustion_stage),
                "exhaustion_stage": self._exhaustion_stage,
                "enforcement": "pre_model_invocation",
                "transport_retries_counted_separately": False,
                "events": [dict(event) for event in self._events],
            }


@dataclass
class ModelRequestBudgetLease:
    """A sequential child-process allocation reconciled against child usage."""

    ledger: ModelRequestBudget
    stage: str
    allocated: int | None
    settled: bool = False

    def environment(self) -> dict[str, str]:
        values = {
            MODEL_REQUEST_BUDGET_CORRELATION_ENV: self.ledger.correlation_id,
            MODEL_REQUEST_BUDGET_PARENT_CONSUMED_ENV: str(self.ledger.consumed),
        }
        if self.allocated is not None:
            values[MODEL_REQUEST_BUDGET_LIMIT_ENV] = str(self.allocated)
        return values

    def settle(
        self,
        actual_requests: int | None,
        *,
        exhaustion_stage: str = "",
    ) -> None:
        if self.settled:
            return
        self.ledger._settle_child(
            stage=self.stage,
            allocated=self.allocated,
            actual=actual_requests,
            exhaustion_stage=str(exhaustion_stage or ""),
        )
        self.settled = True


_ACTIVE_MODEL_REQUEST_BUDGET: ContextVar[ModelRequestBudget | None] = ContextVar(
    "keystone_active_model_request_budget",
    default=None,
)


@contextmanager
def activate_model_request_budget(
    limit: int | None,
    *,
    correlation_id: str | None = None,
    parent_consumed: int = 0,
) -> Iterator[ModelRequestBudget]:
    ledger = ModelRequestBudget(
        limit=limit,
        correlation_id=correlation_id or uuid4().hex,
        parent_consumed=parent_consumed,
    )
    token = _ACTIVE_MODEL_REQUEST_BUDGET.set(ledger)
    try:
        yield ledger
    finally:
        _ACTIVE_MODEL_REQUEST_BUDGET.reset(token)


def current_model_request_budget(*, load_environment: bool = True) -> ModelRequestBudget | None:
    ledger = _ACTIVE_MODEL_REQUEST_BUDGET.get()
    if ledger is not None or not load_environment:
        return ledger
    raw_limit = str(os.getenv(MODEL_REQUEST_BUDGET_LIMIT_ENV, "") or "").strip()
    correlation_id = str(
        os.getenv(MODEL_REQUEST_BUDGET_CORRELATION_ENV, "") or ""
    ).strip()
    if not raw_limit and not correlation_id:
        return None
    try:
        limit = int(raw_limit) if raw_limit else None
        parent_consumed = int(
            str(os.getenv(MODEL_REQUEST_BUDGET_PARENT_CONSUMED_ENV, "0") or "0")
        )
    except ValueError:
        return None
    ledger = ModelRequestBudget(
        limit=limit,
        correlation_id=correlation_id or uuid4().hex,
        parent_consumed=parent_consumed,
    )
    _ACTIVE_MODEL_REQUEST_BUDGET.set(ledger)
    return ledger


def current_model_request_budget_snapshot() -> dict[str, Any] | None:
    ledger = current_model_request_budget(load_environment=False)
    return ledger.snapshot() if ledger is not None else None


def reserve_child_model_request_budget(*, stage: str) -> ModelRequestBudgetLease | None:
    ledger = current_model_request_budget()
    return ledger.reserve_child(stage=stage) if ledger is not None else None


def configured_model_request_budget_limit(cli_limit: object) -> int | None:
    if cli_limit is not None:
        try:
            value = int(cli_limit)
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None
    raw = str(os.getenv(MODEL_REQUEST_BUDGET_LIMIT_ENV, "") or "").strip()
    try:
        value = int(raw) if raw else None
    except ValueError:
        return None
    return value if value is not None and value >= 0 else None


def model_request_count_from_payload(payload: object) -> int | None:
    """Reconcile child model counts without confusing search-provider requests."""

    if not isinstance(payload, Mapping):
        return None
    counts: list[int] = []
    budget = model_request_budget_from_payload(payload)
    if budget is not None:
        value = _nonnegative_int(budget.get("consumed"))
        if value is not None:
            counts.append(value)
    usage = payload.get("usage")
    if isinstance(usage, Mapping):
        value = _nonnegative_int(usage.get("requests"))
        if value is not None:
            counts.append(value)
    for key in ("openai_requests", "openai_requests_made"):
        value = _nonnegative_int(payload.get(key))
        if value is not None:
            counts.append(value)
    model = payload.get("model")
    if isinstance(model, Mapping):
        model_usage = model.get("usage")
        if isinstance(model_usage, Mapping):
            value = _nonnegative_int(model_usage.get("requests"))
            if value is not None:
                counts.append(value)
    events = payload.get("sdk_usage_events")
    if isinstance(events, list):
        event_counts: list[int] = []
        for event in events:
            if not isinstance(event, Mapping):
                continue
            event_usage = event.get("usage")
            if not isinstance(event_usage, Mapping):
                continue
            value = _nonnegative_int(event_usage.get("requests"))
            if value is not None:
                event_counts.append(value)
        if event_counts:
            counts.append(sum(event_counts))
    return max(counts) if counts else None


def model_request_budget_from_payload(payload: object) -> dict[str, Any] | None:
    """Return one child hard-budget snapshot from stable telemetry surfaces."""

    if not isinstance(payload, Mapping):
        return None
    candidates: list[object] = [payload.get("request_budget")]
    request_cache = payload.get("request_cache")
    if isinstance(request_cache, Mapping):
        candidates.append(request_cache.get("request_budget"))
    sdk_failure = payload.get("sdk_run_failure")
    if isinstance(sdk_failure, Mapping):
        failure_cache = sdk_failure.get("request_cache")
        if isinstance(failure_cache, Mapping):
            candidates.append(failure_cache.get("request_budget"))
    output = payload.get("output")
    if isinstance(output, Mapping):
        candidates.append(output.get("request_budget"))
    for candidate in candidates:
        if (
            isinstance(candidate, Mapping)
            and str(candidate.get("schema") or "") == MODEL_REQUEST_BUDGET_SCHEMA
        ):
            return dict(candidate)
    return None


def _nonnegative_int(value: object) -> int | None:
    try:
        resolved = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return resolved if resolved >= 0 else None


__all__ = [
    "FINAL_RESPONSE_REQUEST_RESERVE",
    "MODEL_REQUEST_BUDGET_CORRELATION_ENV",
    "MODEL_REQUEST_BUDGET_LIMIT_ENV",
    "MODEL_REQUEST_BUDGET_PARENT_CONSUMED_ENV",
    "ModelRequestBudget",
    "ModelRequestCapacity",
    "ModelRequestBudgetExhausted",
    "ModelRequestBudgetLease",
    "activate_model_request_budget",
    "configured_model_request_budget_limit",
    "current_model_request_capacity",
    "current_model_request_budget",
    "current_model_request_budget_snapshot",
    "model_request_budget_from_payload",
    "model_request_count_from_payload",
    "reserve_child_model_request_budget",
]
