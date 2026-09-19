"""Cooperative single-host ownership of an existing WorkItem during advancement."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import get_ident

from keystone_agents.runtime.durable_execution import (
    ExecutionConflict,
    current_execution,
    execution_database_path,
    execution_lock,
)
from keystone_agents.storage.sqlite_store import sqlite_path_from_url, stable_hash


@dataclass
class _Lease:
    # Copied contexts share this object, so an expired owner cannot bypass a
    # subsequently acquired OS lock using a stale reentrancy marker.
    closed: bool = False


_HELD: ContextVar[dict[tuple[str, str], _Lease] | None] = ContextVar(
    "kba_work_item_advancement_locks", default=None
)
_OWNER: ContextVar[str] = ContextVar("kba_work_item_advancement_owner", default="")


@contextmanager
def work_item_execution_lock(
    database_url: str | None,
    work_item_id: str,
    *,
    owner: str = "",
) -> Iterator[None]:
    """Reject a competing owner; permit nested stages in the same execution.

    Acquire this before the signal-admission database lock. The context marker
    follows LangGraph's worker context, so a nested signal stage does not contend
    with the execution that already owns its WorkItem. Standalone calls are
    reentrant only on their calling thread. No business record is read or changed.
    """

    identity = str(work_item_id or "").strip()
    if not identity:
        yield
        return
    database = str(sqlite_path_from_url(database_url))
    if database == ":memory:":
        # Separate in-memory SQLite connections are not a shared durable WorkItem.
        yield
        return
    path = execution_database_path(database_url)
    lock_path = path.with_name(f"{path.name}.work-item-{stable_hash(identity)[:24]}.lock")
    execution = current_execution()
    if not owner:
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        owner = (
            execution.execution_id
            if execution
            else _OWNER.get()
            if _OWNER.get()
            else f"task:{id(task)}"
            if task is not None
            else f"thread:{get_ident()}"
        )
    key = (str(lock_path), owner)
    held = _HELD.get() or {}
    lease = held.get(key)
    if lease is not None:
        if lease.closed:
            raise ExecutionConflict("This WorkItem advancement ownership has expired.")
        yield
        return
    with ExitStack() as stack:
        try:
            stack.enter_context(execution_lock(lock_path))
        except ExecutionConflict as exc:
            raise ExecutionConflict(
                "This WorkItem is already being advanced by another execution."
            ) from exc
        lease = _Lease()
        token = _HELD.set({**held, key: lease})
        owner_token = _OWNER.set(owner)
        try:
            yield
        finally:
            lease.closed = True
            _OWNER.reset(owner_token)
            _HELD.reset(token)
