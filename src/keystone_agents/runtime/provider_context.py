"""Request-scoped provider execution limits shared by retrieval workers."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from threading import BoundedSemaphore
from time import perf_counter
from typing import Any, TypeVar

_T = TypeVar("_T")


@dataclass(frozen=True)
class ProviderExecutionContext:
    """One logical request's shared provider budget, concurrency, and lease."""

    settings: Any
    provider_config: Any
    network_semaphore: BoundedSemaphore
    provider_executor: ThreadPoolExecutor
    started_at: float
    deadline_at: float
    runtime_metadata: Mapping[str, Any]

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_at - perf_counter())

    def run_provider_call(self, callback: Callable[[], _T]) -> _T:
        """Run one provider call within the aggregate concurrency and deadline."""

        remaining = self.remaining_seconds()
        if remaining <= 0:
            raise TimeoutError("Provider execution deadline was exhausted.")

        def invoke() -> _T:
            with self.network_slot():
                return callback()

        future = self.provider_executor.submit(invoke)
        try:
            return future.result(timeout=remaining)
        except TimeoutError:
            future.cancel()
            raise TimeoutError("Provider call exceeded the request deadline.") from None

    @contextmanager
    def network_slot(self) -> Iterator[None]:
        """Bound aggregate provider concurrency across nested worker pools."""

        remaining = self.remaining_seconds()
        if remaining <= 0:
            raise TimeoutError("Provider execution deadline was exhausted.")
        acquired = self.network_semaphore.acquire(timeout=remaining)
        if not acquired:
            raise TimeoutError(
                "Provider execution deadline expired while waiting for a network slot."
            )
        try:
            if self.remaining_seconds() <= 0:
                raise TimeoutError("Provider execution deadline was exhausted.")
            yield
        finally:
            self.network_semaphore.release()


__all__ = ["ProviderExecutionContext"]
