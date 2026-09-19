"""Operator-readable failure normalization for Keystone agent surfaces."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from keystone_agents.schemas.work_item import WorkItemBlocker, WorkItemNextAction

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"(?:api[_-]?key|token|secret|password)=\S+", flags=re.I),
)
OPERATOR_FAILURE_SCHEMA = "keystone.operator_failure.v1"


class WorkItemContextRequiredError(ValueError):
    """Carry existing typed readiness blockers without parsing exception prose."""

    def __init__(
        self, blockers: Sequence[WorkItemBlocker], next_action: WorkItemNextAction | None,
    ) -> None:
        self.blockers = tuple(blockers)
        self.next_action = next_action
        super().__init__(
            "The supplied WorkItem context did not satisfy its readiness requirements."
        )


@dataclass(frozen=True)
class OperatorReadableFailure:
    """Stable, redacted failure payload for CLI, WorkItem, SDK, and Slack bridge output."""

    kind: str
    summary: str
    reason: str
    next_step: str
    retryable: bool = False
    safe_to_continue: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": OPERATOR_FAILURE_SCHEMA,
            "kind": self.kind,
            "summary": self.summary,
            "reason": self.reason,
            "next_step": self.next_step,
            "retryable": self.retryable,
            "safe_to_continue": self.safe_to_continue,
        }


class OperatorReadableFailureError(ValueError):
    """Carry an existing failure envelope from a deterministic input check."""

    def __init__(self, failure: OperatorReadableFailure) -> None:
        self.failure = failure
        super().__init__(failure.summary)

    @classmethod
    def input_contract(cls, summary: str) -> OperatorReadableFailureError:
        """Use only fixed developer-authored summaries, never interpolated input."""
        return cls(OperatorReadableFailure(
            kind="schema_or_parse_error", summary=summary, reason="",
            next_step=(
                "Correct or compact the supplied context while preserving required "
                "source identities and evidence, then retry."
            ),
            retryable=False, safe_to_continue=True,
        ))


def redact_operator_text(value: object, *, max_chars: int = 1200) -> str:
    """Redact obvious secrets and cap text intended for operator-visible failures."""

    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    text = re.sub(r"\s+", " ", text).strip()
    if max_chars > 0 and len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text


def known_exception_to_operator_failure(
    exc: BaseException,
    *,
    context: str = "work_item",
    include_exception_reason: bool = True,
) -> OperatorReadableFailure:
    """Map known exception families to stable operator language.

    The helper intentionally does not decide safety policy. It only turns a
    Python exception into a bounded, redacted explanation that renderers can
    show without exposing stack traces or credentials.
    """

    error_type = type(exc).__name__
    if isinstance(exc, OperatorReadableFailureError):
        return exc.failure
    metadata = getattr(exc, "keystone_sdk_run_failure", None)
    failure_kind = metadata.get("failure_kind") if isinstance(metadata, Mapping) else None
    terminal_actions = {
        "model_output_limit_reached": (
            "reached its model-response output limit before completion.",
            "Review the retained limit and usage evidence, then narrow the requested response "
            "or explicitly adjust the output limit before resuming.",
        ),
        "model_response_incomplete": (
            "received an incomplete model response.",
            "Review the retained completion reason and usage before deciding whether to retry; "
            "preserve completed tool evidence.",
        ),
        "model_response_failed": (
            "received a failed model response from the provider.",
            "Review the retained provider status and usage, resolve the provider failure, "
            "then decide whether to retry using the preserved evidence.",
        ),
    }
    if isinstance(failure_kind, str) and failure_kind in terminal_actions:
        summary, next_step = terminal_actions[failure_kind]
        return OperatorReadableFailure(
            kind=failure_kind, summary=f"{_context_label(context)} {summary}", reason="",
            next_step=next_step, retryable=False, safe_to_continue=True,
        )
    # Strict callers classify by exception family and never inspect arbitrary
    # exception prose (which can contain input text or misleading source words).
    reason = redact_operator_text(str(exc), max_chars=800) if include_exception_reason else ""
    lowered = f"{error_type} {reason}".lower()
    prefix = _context_label(context)
    status_code = getattr(exc, "status_code", None)
    status_code = status_code if type(status_code) is int else None

    if error_type == "AgentRunBudgetExceededError":
        return OperatorReadableFailure(
            kind="agentrunbudgetexceedederror",
            summary=f"{prefix} stopped at its configured model-cost check.",
            reason=reason,
            next_step=(
                "Review the recorded usage, cost estimate and budget before resuming. "
                "Completed model requests may already be billable."
            ),
            retryable=False, safe_to_continue=True,
        )
    if error_type in {"APIConnectionError", "ConnectError", "NetworkError"}:
        return OperatorReadableFailure(
            kind=error_type.lower(),
            summary=f"{prefix} could not complete its provider connection.",
            reason=reason,
            next_step="Check provider connectivity and the retained attempt before retrying.",
            retryable=True, safe_to_continue=True,
        )
    if error_type in {"ModelProviderConfigurationError", "UnsupportedModelProviderError"}:
        return OperatorReadableFailure(
            kind=error_type.lower(),
            summary=f"{prefix} could not use its configured model provider.",
            reason=reason,
            next_step=(
                "Review the selected provider, model and gateway configuration before retrying."
            ),
            retryable=False, safe_to_continue=True,
        )
    if error_type == "RateLimitError" or status_code == 429:
        return OperatorReadableFailure(
            kind="provider_rate_limit",
            summary=f"{prefix} hit a live provider rate limit.",
            reason=reason,
            next_step="Wait for the provider window to reset or rerun with a smaller request.",
            retryable=True,
            safe_to_continue=True,
        )
    if error_type == "ModelRequestBudgetExhausted":
        return OperatorReadableFailure(
            kind="model_request_budget_exhausted",
            summary=(
                f"{prefix} stopped before an additional model request could exceed "
                "the configured request budget."
            ),
            reason=reason,
            next_step=(
                "Review the completed tool evidence and request-budget trace, then "
                "resume with a new explicit budget only if another model turn is needed."
            ),
            retryable=False,
            safe_to_continue=True,
        )
    if error_type == "ExecutionDeadlineExceeded":
        return OperatorReadableFailure(
            kind="execution_soft_deadline_exceeded",
            summary=(
                f"{prefix} stopped before starting more model or tool work because "
                "the request's bounded execution window had elapsed."
            ),
            reason=reason,
            next_step=(
                "Review the preserved deadline checkpoints and completed evidence; "
                "do not repeat completed reads or mutations automatically."
            ),
            retryable=False,
            safe_to_continue=True,
        )
    if error_type == "ToolGuardrailViolation" or "guardrail" in lowered:
        return OperatorReadableFailure(
            kind="guardrail_block",
            summary=(
                f"{prefix} stopped at a safety guardrail before completion."
            ),
            reason=reason,
            next_step="Review the blocked input/output scope, then rerun with safer context.",
            retryable=False,
            safe_to_continue=True,
        )
    if status_code == 401 or _looks_like_missing_credentials(lowered):
        return OperatorReadableFailure(
            kind="missing_credentials",
            summary=f"{prefix} could not use a required live provider credential.",
            reason=reason,
            next_step="Configure or disable the live provider, then rerun.",
            retryable=False,
            safe_to_continue=True,
        )
    if status_code == 403 or any(
        marker in lowered
        for marker in (
            "invalid_scope",
            "scope_missing",
            "googleworkspacescopeerror",
            "authorization does not include",
        )
    ):
        return OperatorReadableFailure(
            kind="provider_authorization_scope",
            summary=f"{prefix} could not use the provider scope required for this read.",
            reason=reason,
            next_step=(
                "Reauthorize only the named provider service, then rerun the same "
                "bounded request."
            ),
            retryable=False,
            safe_to_continue=True,
        )
    if error_type == "MaxTurnsExceeded" or "max turns" in lowered:
        return OperatorReadableFailure(
            kind="model_tool_turns_exhausted",
            summary=f"{prefix} used its allowed model/tool turns without completing.",
            reason=reason,
            next_step=(
                "Inspect the last tool result for a non-retryable provider blocker or "
                "an invalid argument before rerunning."
            ),
            retryable=False,
            safe_to_continue=True,
        )
    if error_type == "TimeoutExpired":
        return OperatorReadableFailure(
            kind="child_process_deadline_exceeded",
            summary=(
                f"{prefix} exceeded its outer execution deadline before the active "
                "child stage could be confirmed."
            ),
            reason=reason,
            next_step=(
                "Inspect the preserved child-stage evidence before deciding whether "
                "to retry, narrow the request, or change a provider."
            ),
            retryable=True,
            safe_to_continue=True,
        )
    if "timeout" in lowered:
        return OperatorReadableFailure(
            kind="provider_timeout",
            summary=f"{prefix} timed out while waiting for a live provider.",
            reason=reason,
            next_step="Retry with a narrower scope or alternate provider.",
            retryable=True,
            safe_to_continue=True,
        )
    if "rate limit" in lowered or "429" in lowered:
        return OperatorReadableFailure(
            kind="provider_rate_limit",
            summary=f"{prefix} hit a live provider rate limit.",
            reason=reason,
            next_step="Wait for the provider window to reset or rerun with a smaller request.",
            retryable=True,
            safe_to_continue=True,
        )
    if error_type in {"GmailAgentDecisionError", "AgentDecisionValidationError"} or any(
        marker in lowered
        for marker in ("json", "schema", "validationerror", "modelbehaviorerror", "parse")
    ):
        return OperatorReadableFailure(
            kind="schema_or_parse_error",
            summary=f"{prefix} returned data that could not be validated.",
            reason=reason,
            next_step="Keep the WorkItem blocked, inspect the malformed output, then rerun.",
            retryable=True,
            safe_to_continue=True,
        )
    if any(marker in lowered for marker in ("source", "extract", "retrieval", "crawl", "search")):
        return OperatorReadableFailure(
            kind="retrieval_error",
            summary=f"{prefix} could not complete source retrieval or extraction.",
            reason=reason,
            next_step="Retry retrieval, switch provider, or attach source context manually.",
            retryable=True,
            safe_to_continue=True,
        )
    return OperatorReadableFailure(
        kind="unknown_error",
        summary=f"{prefix} failed before it could complete.",
        reason=reason,
        next_step=(
            "Review the blocker, preserve the WorkItem state, then rerun after fixing the cause."
            if "workitem" in str(context or "").replace(" ", "").lower()
            else "Review the blocker, fix the underlying cause, then rerun."
        ),
        retryable=False,
        safe_to_continue=False,
    )


def operator_failure_from_mapping(value: object) -> OperatorReadableFailure | None:
    """Parse a serialized operator failure payload if it uses the current schema."""

    if not isinstance(value, dict):
        return None
    if str(value.get("schema") or "") != OPERATOR_FAILURE_SCHEMA:
        return None
    kind = str(value.get("kind") or "").strip()
    summary = str(value.get("summary") or "").strip()
    next_step = str(value.get("next_step") or "").strip()
    if not kind or not summary or not next_step:
        return None
    return OperatorReadableFailure(
        kind=kind,
        summary=redact_operator_text(summary, max_chars=800),
        reason=redact_operator_text(value.get("reason") or "", max_chars=800),
        next_step=redact_operator_text(next_step, max_chars=800),
        retryable=bool(value.get("retryable")),
        safe_to_continue=bool(value.get("safe_to_continue")),
    )


def _context_label(context: str) -> str:
    normalized = str(context or "").strip().replace("_", " ")
    if not normalized:
        return "The run"
    return f"The {normalized}"


def _looks_like_missing_credentials(lowered: str) -> bool:
    return any(
        marker in lowered
        for marker in (
            "credential",
            "api key",
            "apikey",
            "missing key",
            "not configured",
            "authentication",
            "unauthorized",
        )
    )
