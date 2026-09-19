"""Request-local admission budgets for model-called function tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any

TOOL_CALL_BUDGET_SCHEMA = "keystone.tool_call_budget.v1"
TOOL_CALL_BUDGET_BLOCK_SCHEMA = "keystone.tool_call_budget_block.v1"


@dataclass(frozen=True)
class ToolCallLimit:
    """Maximum admitted model invocations for one attached function tool."""

    tool_name: str
    max_calls: int

    def __post_init__(self) -> None:
        normalized_name = str(self.tool_name or "").strip()
        if not normalized_name:
            raise ValueError("Tool call limits require a tool name.")
        if int(self.max_calls) < 0:
            raise ValueError("Tool call limits must be non-negative.")
        object.__setattr__(self, "tool_name", normalized_name)
        object.__setattr__(self, "max_calls", int(self.max_calls))


@dataclass(frozen=True)
class ToolCallBudgetContract:
    """Total and per-tool ceilings for one model-controlled semantic stage."""

    limits: tuple[ToolCallLimit, ...] = ()
    max_total_calls: int | None = None
    stage: str = "model_stage"

    def __post_init__(self) -> None:
        normalized_stage = str(self.stage or "model_stage").strip() or "model_stage"
        names = [limit.tool_name for limit in self.limits]
        if self.max_total_calls is not None and int(self.max_total_calls) < 0:
            raise ValueError("Total tool call limits must be non-negative.")
        if not self.limits and self.max_total_calls is None:
            raise ValueError(
                "Tool call budget contracts require a total or per-tool limit."
            )
        if len(names) != len(set(names)):
            raise ValueError("Tool call budget limits must use unique tool names.")
        if self.max_total_calls is not None:
            object.__setattr__(self, "max_total_calls", int(self.max_total_calls))
        object.__setattr__(self, "stage", normalized_stage)

    def prompt_context(self) -> str:
        per_tool_limits = ", ".join(
            f"{limit.tool_name}={limit.max_calls}" for limit in self.limits
        )
        limits = "; ".join(
            value
            for value in (
                (
                    f"total={self.max_total_calls}"
                    if self.max_total_calls is not None
                    else ""
                ),
                per_tool_limits,
            )
            if value
        )
        return (
            "\n\nBounded tool-call budget:\n"
            f"Stage {self.stage} permits at most these model tool invocations: {limits}. "
            "Choose the most informative calls first. If a tool reports that its budget "
            "is exhausted, do not call it again; decide from the evidence already returned "
            "or explicitly request more context."
        )


@dataclass(frozen=True)
class ToolCallAdmission:
    """One atomic pre-tool admission decision."""

    admitted: bool
    tool_name: str
    stage: str
    limit: int | None
    consumed: int
    blocked_count: int
    total_limit: int | None
    total_consumed: int
    recoverable: bool
    reason_code: str = "tool_call_budget_exhausted"

    def blocked_output(self) -> dict[str, Any]:
        if self.admitted:
            raise ValueError("Admitted tool calls do not have a blocked output.")
        return {
            "schema": TOOL_CALL_BUDGET_BLOCK_SCHEMA,
            "status": "blocked",
            "reason_code": self.reason_code,
            "tool_name": self.tool_name,
            "stage": self.stage,
            "limit": self.limit,
            "consumed": self.consumed,
            "remaining": 0,
            "blocked_count": self.blocked_count,
            "total_limit": self.total_limit,
            "total_consumed": self.total_consumed,
            "provider_action_performed": False,
            "recoverable": self.recoverable,
            "instruction": (
                "Do not call this tool again. "
                + (
                    "Use the evidence already returned, or explicitly report that "
                    "more context is required."
                    if self.recoverable
                    else "The requested mutation was not performed; return a truthful "
                    "blocked result without retrying or widening the write."
                )
            ),
        }


class ToolCallBudgetExceededError(RuntimeError):
    """Raised when a hosted tool exceeds a post-response call ceiling."""

    def __init__(self, *, stage: str, tool_name: str, consumed: int, limit: int) -> None:
        self.stage = stage
        self.tool_name = tool_name
        self.consumed = consumed
        self.limit = limit
        super().__init__(
            f"Hosted tool call budget exceeded for {stage}: "
            f"{tool_name} consumed {consumed} calls with limit {limit}."
        )


@dataclass
class ToolCallBudgetLedger:
    """Thread-safe ledger shared by every turn and repair in one SDK run."""

    contract: ToolCallBudgetContract
    _consumed: dict[str, int] = field(default_factory=dict)
    _blocked: dict[str, int] = field(default_factory=dict)
    _total_consumed: int = 0
    _total_blocked: int = 0
    _events: list[dict[str, Any]] = field(default_factory=list)
    _observed_hosted_call_ids: set[str] = field(default_factory=set)
    _hosted_observed_total: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False)

    def admit(self, tool_name: str, *, mutation: bool = False) -> ToolCallAdmission:
        normalized_name = str(tool_name or "").strip()
        limits = {limit.tool_name: limit.max_calls for limit in self.contract.limits}
        limit = limits.get(normalized_name)
        with self._lock:
            consumed = int(self._consumed.get(normalized_name, 0))
            blocked = int(self._blocked.get(normalized_name, 0))
            total_limit = self.contract.max_total_calls
            per_tool_exhausted = limit is not None and consumed >= limit
            total_exhausted = (
                total_limit is not None and self._total_consumed >= total_limit
            )
            if per_tool_exhausted or total_exhausted:
                blocked += 1
                self._blocked[normalized_name] = blocked
                self._total_blocked += 1
                reason_code = (
                    "total_tool_call_budget_exhausted"
                    if total_exhausted
                    else "tool_call_budget_exhausted"
                )
                self._events.append(
                    {
                        "tool_name": normalized_name,
                        "status": "rejected_before_tool_invocation",
                        "consumed": consumed,
                        "limit": limit,
                        "total_consumed": self._total_consumed,
                        "total_limit": total_limit,
                        "blocked_count": blocked,
                        "reason_code": reason_code,
                        "mutation": bool(mutation),
                    }
                )
                return ToolCallAdmission(
                    admitted=False,
                    tool_name=normalized_name,
                    stage=self.contract.stage,
                    limit=limit,
                    consumed=consumed,
                    blocked_count=blocked,
                    total_limit=total_limit,
                    total_consumed=self._total_consumed,
                    recoverable=not mutation,
                    reason_code=reason_code,
                )
            self._total_consumed += 1
            if limit is not None:
                consumed += 1
                self._consumed[normalized_name] = consumed
            self._events.append(
                {
                    "tool_name": normalized_name,
                    "status": "admitted_before_tool_invocation",
                    "consumed": consumed,
                    "limit": limit,
                    "total_consumed": self._total_consumed,
                    "total_limit": total_limit,
                    "mutation": bool(mutation),
                }
            )
            return ToolCallAdmission(
                admitted=True,
                tool_name=normalized_name,
                stage=self.contract.stage,
                limit=limit,
                consumed=consumed,
                blocked_count=blocked,
                total_limit=total_limit,
                total_consumed=self._total_consumed,
                recoverable=not mutation,
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            limits = {
                limit.tool_name: limit.max_calls for limit in self.contract.limits
            }
            snapshot = {
                "schema": TOOL_CALL_BUDGET_SCHEMA,
                "stage": self.contract.stage,
                "max_total_calls": self.contract.max_total_calls,
                "total_consumed": self._total_consumed,
                "total_blocked": self._total_blocked,
                "limits": dict(limits),
                "consumed": {
                    name: int(self._consumed.get(name, 0)) for name in limits
                },
                "blocked": {
                    name: int(self._blocked.get(name, 0)) for name in limits
                },
                "exhausted_tool_names": [
                    name
                    for name, limit in limits.items()
                    if int(self._consumed.get(name, 0)) >= limit
                ],
                "enforcement": "pre_tool_invocation",
                "provider_action_prevented_for_blocked_calls": True,
                "events": [dict(event) for event in self._events],
            }
            if self._hosted_observed_total:
                snapshot.update(
                    {
                        "enforcement": (
                            "pre_invocation_function_tools_and_"
                            "post_response_hosted_validation"
                        ),
                        "hosted_observed_total": self._hosted_observed_total,
                        "hosted_provider_action_prevented_on_overage": False,
                    }
                )
            return snapshot

    def observe_hosted_calls(self, records: object) -> None:
        """Account for hosted calls that cannot use function-tool admission hooks."""

        limits = {limit.tool_name: limit.max_calls for limit in self.contract.limits}
        with self._lock:
            for record in records if isinstance(records, (list, tuple)) else ():
                if not bool(getattr(record, "self_contained_hosted", False)):
                    continue
                call_id = str(getattr(record, "call_id", "") or "").strip()
                tool_name = str(getattr(record, "tool_name", "") or "").strip()
                if (
                    not call_id
                    or not tool_name
                    or call_id in self._observed_hosted_call_ids
                ):
                    continue
                self._observed_hosted_call_ids.add(call_id)
                self._hosted_observed_total += 1
                self._total_consumed += 1
                self._consumed[tool_name] = int(self._consumed.get(tool_name, 0)) + 1
                consumed = self._consumed[tool_name]
                limit = limits.get(tool_name)
                total_limit = self.contract.max_total_calls
                over_limit = bool(
                    (limit is not None and consumed > limit)
                    or (
                        total_limit is not None
                        and self._total_consumed > total_limit
                    )
                )
                self._events.append(
                    {
                        "tool_name": tool_name,
                        "status": (
                            "observed_after_provider_over_budget"
                            if over_limit
                            else "observed_after_hosted_provider_call"
                        ),
                        "consumed": consumed,
                        "limit": limit,
                        "total_consumed": self._total_consumed,
                        "total_limit": total_limit,
                        "provider_action_already_performed": True,
                    }
                )
                if over_limit:
                    raise ToolCallBudgetExceededError(
                        stage=self.contract.stage,
                        tool_name=tool_name,
                        consumed=consumed,
                        limit=(
                            limit
                            if limit is not None and consumed > limit
                            else int(total_limit or 0)
                        ),
                    )


__all__ = [
    "TOOL_CALL_BUDGET_BLOCK_SCHEMA",
    "TOOL_CALL_BUDGET_SCHEMA",
    "ToolCallAdmission",
    "ToolCallBudgetContract",
    "ToolCallBudgetExceededError",
    "ToolCallBudgetLedger",
    "ToolCallLimit",
]
