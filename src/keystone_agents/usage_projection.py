"""Dependency-free, content-free usage projections for persisted diagnostics."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

TOKEN_USAGE_FIELDS = (
    "input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
    "cache_write_input_tokens", "reasoning_output_tokens",
)
NUMERIC_TOKEN_USAGE_KEYS = frozenset({
    *TOKEN_USAGE_FIELDS, "input_cached_tokens", "prompt_token_count",
    "prompt_tokens", "total_token_count",
})
REQUEST_USAGE_FIELDS = (*TOKEN_USAGE_FIELDS, "requests", "request_ordinal")
BILLABLE_TOKEN_FIELDS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens",
)


def nonnegative_usage_integer(value: Any) -> int | None:
    """Unknown or invalid counts stay unknown; strings never become an exception."""
    return value if type(value) is int and value >= 0 else None


def project_billable_tokens(value: Any) -> dict[str, int | None]:
    """Keep only the numeric component names emitted by production cost estimation."""
    if not isinstance(value, Mapping):
        return {}
    return {
        field: nonnegative_usage_integer(value[field])
        for field in BILLABLE_TOKEN_FIELDS if field in value
    }


def project_request_usage_entries(value: Any) -> list[dict[str, int | None]]:
    """Preserve response boundaries and only allowlisted integer/None fields.

    Invalid entries retain an empty boundary rather than silently disappearing
    from a multi-request audit. No prompt, provider payload, or free text survives.
    """
    if not isinstance(value, list | tuple):
        return []
    return [
        {
            field: nonnegative_usage_integer(entry[field])
            for field in REQUEST_USAGE_FIELDS if field in entry
        }
        if isinstance(entry, Mapping) else {}
        for entry in value
    ]
