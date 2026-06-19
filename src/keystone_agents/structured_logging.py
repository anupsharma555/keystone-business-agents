"""Shared structured logging envelope for local runtime JSONL surfaces."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from typing import Any

STRUCTURED_LOG_SCHEMA = "keystone.structured_log.v1"

_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|secret|token|password|credential|authorization|auth[_-]?token)",
    re.I,
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", re.I),
    re.compile(r"\bauthorization\s*[:=]\s*bearer\s+[A-Za-z0-9._~+/=-]{8,}", re.I),
)
_BODY_KEYS = {
    "body",
    "content",
    "draft_body",
    "draft_reply",
    "draft_text",
    "email_body",
    "full_text",
    "message",
    "raw_body",
    "response",
    "stderr",
    "stdout",
    "text",
}
_CORRELATION_KEYS = {
    "agent",
    "case_id",
    "eval_id",
    "failure_kind",
    "route",
    "run_id",
    "slack_channel_id",
    "slack_thread_ts",
    "stage",
    "status",
    "trace_id",
    "work_item_id",
}


def structured_log_event(
    *,
    component: str,
    event: str,
    level: str = "info",
    payload: dict[str, Any] | None = None,
    **correlation: Any,
) -> dict[str, Any]:
    """Return a redacted JSONL-safe structured log event."""

    payload = payload or {}
    fields = {
        key: _clean_scalar(value)
        for key, value in {**_extract_correlation(payload), **correlation}.items()
        if key in _CORRELATION_KEYS and _clean_scalar(value)
    }
    return {
        "schema": STRUCTURED_LOG_SCHEMA,
        "timestamp": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "level": _clean_scalar(level).lower() or "info",
        "component": _clean_scalar(component),
        "event": _clean_scalar(event),
        "pid": os.getpid(),
        "correlation": fields,
        "redaction": {
            "status": "redacted",
            "raw_payload_included": False,
        },
        "payload": _redact_value(payload),
    }


def _extract_correlation(payload: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key in _CORRELATION_KEYS:
        if key in payload:
            values[key] = payload[key]
    failure = payload.get("failure")
    if isinstance(failure, dict) and "kind" in failure:
        values["failure_kind"] = failure.get("kind")
    return values


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = _clean_scalar(key)
            key_lookup = key_text.lower()
            if _SECRET_KEY_RE.search(key_lookup):
                result[key_text] = "[REDACTED]"
            elif key_lookup in _BODY_KEYS and isinstance(item, str):
                result[f"{key_text}_summary"] = _redact_text(item)[:240]
                result[f"{key_text}_length"] = len(item)
            else:
                result[key_text] = _redact_value(item)
        return result
    if isinstance(value, list | tuple):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redact_text(value: str) -> str:
    text = str(value or "")
    for pattern in _SECRET_VALUE_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def _clean_scalar(value: Any) -> str:
    return " ".join(str(value or "").strip().split())
