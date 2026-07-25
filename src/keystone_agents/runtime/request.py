"""One request-scoped owner for reusable local runtime services."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from keystone_agents.schemas.work_item import WorkflowRunRequest
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env

T = TypeVar("T")


@dataclass
class RequestRuntime:
    """Reuse local services without changing serialized request contracts."""

    request: WorkflowRunRequest
    store: SQLiteStore | None
    _services: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_workflow_request(cls, request: WorkflowRunRequest) -> RequestRuntime:
        """Compose the local services allowed by one workflow request."""

        store = (
            SQLiteStore(request.database_url or database_url_from_env())
            if request.save
            else None
        )
        return cls(request=request, store=store)

    def with_request(self, request: WorkflowRunRequest) -> RequestRuntime:
        """Retain services when normalization preserves their configuration."""

        if (
            request.save == self.request.save
            and (request.database_url or database_url_from_env())
            == (self.request.database_url or database_url_from_env())
        ):
            self.request = request
            return self
        return self.from_workflow_request(request)

    def service(self, key: str, factory: Callable[[], T]) -> T:
        """Build one named request service at most once."""

        clean_key = str(key or "").strip()
        if not clean_key:
            raise ValueError("Request runtime services require a stable key.")
        if clean_key not in self._services:
            self._services[clean_key] = factory()
        return self._services[clean_key]


__all__ = ["RequestRuntime"]
