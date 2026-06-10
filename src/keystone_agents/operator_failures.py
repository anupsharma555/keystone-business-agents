"""Operator-readable failure normalization for Keystone agent surfaces."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"(?:api[_-]?key|token|secret|password)=\S+", flags=re.I),
)
OPERATOR_FAILURE_SCHEMA = "keystone.operator_failure.v1"


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
) -> OperatorReadableFailure:
    """Map known exception families to stable operator language.

    The helper intentionally does not decide safety policy. It only turns a
    Python exception into a bounded, redacted explanation that renderers can
    show without exposing stack traces or credentials.
    """

    error_type = type(exc).__name__
    reason = redact_operator_text(str(exc), max_chars=800)
    lowered = f"{error_type} {reason}".lower()
    prefix = _context_label(context)

    if error_type == "ToolGuardrailViolation" or "guardrail" in lowered:
        return OperatorReadableFailure(
            kind="guardrail_block",
            summary=(
                f"{prefix} stopped at a safety guardrail before completion. "
                "No send or external write action was performed."
            ),
            reason=reason,
            next_step="Review the blocked input/output scope, then rerun with safer context.",
            retryable=False,
            safe_to_continue=True,
        )
    if _looks_like_missing_credentials(lowered):
        return OperatorReadableFailure(
            kind="missing_credentials",
            summary=f"{prefix} could not use a required live provider credential.",
            reason=reason,
            next_step="Configure or disable the live provider, then rerun.",
            retryable=False,
            safe_to_continue=True,
        )
    if "timeout" in lowered or error_type == "TimeoutExpired":
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
    if any(
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
