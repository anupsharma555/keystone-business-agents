"""Canonical boundary for assembling one entrypoint-neutral public result."""

from __future__ import annotations

from typing import Any

from keystone_agents.execution_request import (
    attach_execution_public_result as _attach_execution_public_result,
)
from keystone_agents.schemas.execution_request import ExecutionPublicResult


def attach_execution_public_result(
    payload: dict[str, Any],
) -> ExecutionPublicResult:
    """Validate, reconcile, and mirror one canonical public result."""

    return _attach_execution_public_result(payload)


__all__ = ["attach_execution_public_result"]
