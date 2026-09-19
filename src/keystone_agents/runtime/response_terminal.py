"""Content-free classifications for terminal nonstreaming Responses API failures."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal

_REASONS = frozenset({"max_output_tokens", "max_messages", "content_filter", "steered"})
_ERROR_CODES = frozenset({
    "server_error", "rate_limit_exceeded", "invalid_prompt", "data_residency_mismatch",
    "bio_policy", "misalignment_policy_violation", "vector_store_timeout", "invalid_image",
    "invalid_image_format", "invalid_base64_image", "invalid_image_url", "image_too_large",
    "image_too_small", "image_parse_error", "image_content_policy_violation", "invalid_image_mode",
    "image_file_too_large", "unsupported_image_media_type", "empty_image_file",
    "failed_to_download_image", "image_file_not_found",
})


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def _allowed(value: Any, values: frozenset[str]) -> str:
    return value if type(value) is str and value in values else "unknown"


@dataclass(frozen=True)
class ResponseTerminalObservation:
    """Only bounded enum values and the numeric output cap; no content or IDs."""

    status: Literal["failed", "incomplete"]
    reason: str
    error_code: str
    max_output_tokens: int | None

    @classmethod
    def from_response(
        cls, response: Any, *, max_output_tokens: Any = None,
    ) -> ResponseTerminalObservation | None:
        status = _field(response, "status")
        if type(status) is not str or status not in {"failed", "incomplete"}:
            return None
        cap = max_output_tokens if type(max_output_tokens) is int else _field(
            response, "max_output_tokens",
        )
        return cls(
            status=status,
            reason=_allowed(_field(_field(response, "incomplete_details"), "reason"), _REASONS),
            error_code=_allowed(_field(_field(response, "error"), "code"), _ERROR_CODES),
            max_output_tokens=cap if type(cap) is int and cap >= 0 else None,
        )

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


def response_terminal_diagnostics(value: Any) -> dict[str, Any]:
    """Retrieve only a fresh bounded projection of KBA's terminal observation."""
    raw = getattr(value, "keystone_response_terminal", None)
    entries = raw.get("observations") if isinstance(raw, Mapping) else None
    if not isinstance(entries, list):
        return {}
    safe = []
    for entry in entries[:16]:
        if not isinstance(entry, Mapping):
            continue
        observation = ResponseTerminalObservation.from_response({
            "status": entry.get("status"),
            "incomplete_details": {"reason": entry.get("reason")},
            "error": {"code": entry.get("error_code")},
            "max_output_tokens": entry.get("max_output_tokens"),
        })
        if observation is not None:
            safe.append(observation.snapshot())
    return {"schema": "keystone.response_terminal.v1", "observations": safe} if safe else {}


def response_terminal_failure_kind(value: Any) -> str:
    observations = response_terminal_diagnostics(value).get("observations") or []
    if not observations:
        return ""
    terminal = observations[-1]
    if terminal["status"] == "incomplete":
        return (
            "model_output_limit_reached"
            if terminal["reason"] in {"max_output_tokens", "max_messages"}
            else "model_response_incomplete"
        )
    return "model_response_failed"
