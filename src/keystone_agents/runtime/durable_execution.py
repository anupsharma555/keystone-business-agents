"""Local execution identities, effect receipts, and restart-safe request budgets.

Business WorkItems remain authoritative. This journal records execution progress;
it never grants provider permission or infers that an uncertain write succeeded.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from keystone_agents.storage.sqlite_store import sqlite_path_from_url, stable_hash, stable_json

EXECUTION_SCHEMA = "keystone.durable_execution.v1"
EXECUTION_REFERENCE_ENV = "KEYSTONE_DURABLE_EXECUTION_REFERENCE"


class ExecutionConflict(RuntimeError):
    """An execution is active, incompatible, or requires explicit reconciliation."""


class UncertainOperation(ExecutionConflict):
    """An effect may already exist; replay must not issue another write."""


def _preview_without_write_evidence(receipt: Mapping[str, Any]) -> bool:
    verification = receipt.get("verification")
    return bool(
        receipt.get("provider_write") is not True
        and receipt.get("provider_mutated") is not True
        and not (isinstance(verification, Mapping) and verification.get("passed") is True)
        and str(receipt.get("status") or "").strip().lower()
        in {"dry-run", "dry_run", "preview", "success", "ok", ""}
    )


def operation_input(arguments: Any) -> Any:
    """Normalize SDK JSON argument serialization before computing operation identity."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return arguments
    if isinstance(arguments, Mapping):
        normalized = dict(arguments)
        # These provider-tool parameters carry serialized structures, not user prose.
        for key in ("fields_json", "values_json", "updates_json", "records_json", "requests_json"):
            value = normalized.get(key)
            if isinstance(value, str):
                try:
                    normalized[key] = json.loads(value)
                except ValueError:
                    pass  # The owning tool still validates malformed provider arguments.
        return normalized
    return arguments


def execution_database_path(database_url: str | None = None) -> Path:
    path = str(sqlite_path_from_url(database_url))
    if path == ":memory:":
        raise ValueError("Durable execution requires a file-backed business database.")
    from keystone_agents.storage.sqlite_store import _assert_test_database_is_isolated

    _assert_test_database_is_isolated(path)
    business_path = Path(path).expanduser().resolve()
    return business_path.with_name(f"{business_path.name}.execution.sqlite3")


@contextmanager
def execution_lock(path: Path) -> Iterator[None]:
    """Nonblocking OS-owned lock; process death releases it without stale deletion."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ExecutionConflict("This execution is already running.") from exc
        yield
    finally:
        os.close(descriptor)


class ExecutionStore:
    """Transactional local metadata shared by graph and direct execution."""

    def __init__(self, path: str | Path, *, initialize: bool = True) -> None:
        self.path = Path(path).expanduser().resolve()
        self._existing_only = not initialize
        if not initialize:
            if not self.path.is_file():
                raise ExecutionConflict("Execution database was not found.")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        with self.connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS executions (
                    id TEXT PRIMARY KEY, origin_hash TEXT UNIQUE,
                    schema_name TEXT NOT NULL, request_hash TEXT NOT NULL,
                    request_json TEXT NOT NULL, work_item_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending', result_json TEXT,
                    budget_limit INTEGER, consumed INTEGER NOT NULL DEFAULT 0,
                    source_fingerprint TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS execution_operations (
                    execution_id TEXT NOT NULL, operation_key TEXT NOT NULL,
                    tool_name TEXT NOT NULL, input_hash TEXT NOT NULL,
                    status TEXT NOT NULL, receipt_json TEXT,
                    PRIMARY KEY(execution_id, operation_key)
                );
                CREATE TABLE IF NOT EXISTS execution_provider_scopes (
                    execution_id TEXT NOT NULL, tool_name TEXT NOT NULL,
                    scope_kind TEXT NOT NULL, scope_hash TEXT NOT NULL,
                    PRIMARY KEY(execution_id, tool_name, scope_kind)
                );
                CREATE TABLE IF NOT EXISTS execution_model_attempts (
                    execution_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    stage TEXT NOT NULL, PRIMARY KEY(execution_id, ordinal)
                );
                CREATE TABLE IF NOT EXISTS execution_stages (
                    execution_id TEXT NOT NULL, stage_id TEXT NOT NULL,
                    input_hash TEXT NOT NULL, output_json TEXT NOT NULL,
                    PRIMARY KEY(execution_id, stage_id, input_hash)
                );
                CREATE TABLE IF NOT EXISTS execution_deliveries (
                    execution_id TEXT NOT NULL, destination_hash TEXT NOT NULL,
                    result_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    provider_message_id TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(execution_id, destination_hash, result_hash)
                );
            """)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            f"{self.path.as_uri()}?mode=rw" if self._existing_only else self.path,
            uri=self._existing_only,
            timeout=30,
        )
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def begin(
        self,
        request: Mapping[str, Any],
        *,
        execution_id: str = "",
        origin: str = "",
        budget_limit: int | None = None,
        source_fingerprint: str = "",
    ) -> dict[str, Any]:
        execution_id = execution_id or f"ex_{uuid4().hex}"
        if len(execution_id) > 200 or not execution_id.strip():
            raise ValueError("Invalid execution identity.")
        # Reject secret-bearing inputs rather than checkpointing an unusable redacted request.
        encoded = stable_json(dict(request))
        if json.loads(encoded) != json.loads(json.dumps(dict(request), default=str)):
            raise ExecutionConflict("Execution input requires redaction before persistence.")
        digest = stable_hash(dict(request))
        origin_hash = stable_hash(origin) if origin else None
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM executions WHERE id=? OR (? IS NOT NULL AND origin_hash=?)",
                (execution_id, origin_hash, origin_hash),
            ).fetchone()
            if row:
                if row["request_hash"] != digest or row["schema_name"] != EXECUTION_SCHEMA:
                    raise ExecutionConflict(
                        "Execution identity refers to different or incompatible work."
                    )
                return dict(row)
            conn.execute(
                "INSERT INTO executions(id,origin_hash,schema_name,request_hash,request_json,"
                "budget_limit,source_fingerprint) VALUES(?,?,?,?,?,?,?)",
                (
                    execution_id,
                    origin_hash,
                    EXECUTION_SCHEMA,
                    digest,
                    encoded,
                    budget_limit,
                    source_fingerprint,
                ),
            )
        return self.get(execution_id)

    def get(self, execution_id: str) -> dict[str, Any]:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
        if row is None:
            raise ExecutionConflict("Execution was not found.")
        if row["schema_name"] != EXECUTION_SCHEMA:
            raise ExecutionConflict("Checkpoint schema is incompatible.")
        return dict(row)

    def find(self, *, execution_id: str = "", origin: str = "") -> dict[str, Any] | None:
        """Look up an explicit identity without treating identical wording as a retry."""
        if not execution_id and not origin:
            return None
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT id FROM executions WHERE id=? OR (?!='' AND origin_hash=?)",
                (execution_id, origin, stable_hash(origin)),
            ).fetchall()
        if len(rows) > 1:
            raise ExecutionConflict("Execution and origin identities refer to different work.")
        return self.get(rows[0]["id"]) if rows else None

    def list_executions(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT id,work_item_id,status,budget_limit,consumed,created_at,updated_at "
                    "FROM executions ORDER BY created_at DESC,rowid DESC LIMIT ?",
                    (max(1, min(limit, 100)),),
                )
            ]

    def update(
        self,
        execution_id: str,
        *,
        status: str,
        work_item_id: str = "",
        result: Mapping[str, Any] | None = None,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE executions SET status=?,work_item_id=CASE WHEN ?='' THEN work_item_id "
                "ELSE ? END,result_json=COALESCE(?,result_json),"
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (
                    status,
                    work_item_id,
                    work_item_id,
                    stable_json(dict(result)) if result is not None else None,
                    execution_id,
                ),
            )

    def reserve_model(self, execution_id: str, stage: str) -> int:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT consumed,budget_limit FROM executions WHERE id=?", (execution_id,)
            ).fetchone()
            if row is None:
                raise ExecutionConflict("Execution was not found.")
            if row["budget_limit"] is not None and row["consumed"] >= row["budget_limit"]:
                raise ExecutionConflict("Durable model request budget exhausted.")
            ordinal = row["consumed"] + 1
            conn.execute("UPDATE executions SET consumed=? WHERE id=?", (ordinal, execution_id))
            conn.execute(
                "INSERT INTO execution_model_attempts VALUES(?,?,?)", (execution_id, ordinal, stage)
            )
        return ordinal

    def bind_provider_scope(
        self,
        execution_id: str,
        tool_name: str,
        scope: Mapping[str, str],
        *,
        scope_kind: str = "configuration",
    ) -> None:
        """Reject target drift without minting another mutation identity.

        Only an execution-salted digest is stored. Callers supply allowlisted
        configuration and declared owner identifiers, never credential contents.
        """
        digest = stable_hash({"execution": execution_id, "scope": dict(scope)})
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT scope_hash FROM execution_provider_scopes "
                "WHERE execution_id=? AND tool_name=? AND scope_kind=?",
                (execution_id, tool_name, scope_kind),
            ).fetchone()
            if row is not None:
                if row["scope_hash"] != digest:
                    raise ExecutionConflict(
                        "Provider scope changed during this execution; reconcile before retry."
                    )
                return
            if (
                scope_kind == "configuration"
                and conn.execute(
                    "SELECT 1 FROM execution_operations "
                    "WHERE execution_id=? AND tool_name=? LIMIT 1",
                    (execution_id, tool_name),
                ).fetchone()
            ):
                raise ExecutionConflict(
                    "Prior operation has no recorded provider scope; reconciliation is required."
                )
            conn.execute(
                "INSERT INTO execution_provider_scopes VALUES(?,?,?,?)",
                (execution_id, tool_name, scope_kind, digest),
            )

    def before_operation(self, execution_id: str, tool_name: str, arguments: Any) -> dict | None:
        digest = stable_hash(operation_input(arguments))
        key = stable_hash({"tool": tool_name, "input": digest})
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM execution_operations WHERE execution_id=? AND operation_key=?",
                (execution_id, key),
            ).fetchone()
            if row:
                if row["status"] == "verified":
                    return json.loads(row["receipt_json"])
                if row["status"] != "no_effect":
                    raise UncertainOperation("A prior operation needs reconciliation before retry.")
            # Keep this barrier in the intent transaction: changing arguments or
            # racing another worker must not bypass an unresolved provider effect.
            unresolved = conn.execute(
                "SELECT operation.execution_id FROM execution_operations AS operation "
                "LEFT JOIN executions AS owner ON owner.id=operation.execution_id "
                "WHERE operation.status NOT IN ('verified','no_effect') "
                "AND (operation.execution_id=? OR "
                "(owner.work_item_id!='' AND owner.work_item_id="
                "(SELECT work_item_id FROM executions WHERE id=?))) "
                "ORDER BY operation.rowid LIMIT 1",
                (execution_id, execution_id),
            ).fetchone()
            if unresolved:
                raise UncertainOperation(
                    "An unresolved provider operation in execution "
                    f"{stable_json(unresolved['execution_id'])} blocks a new mutation; "
                    "reconcile its journal first."
                )
            if row:
                # A preview is not a successful write replay. Discard only its
                # matching output cache before admitting the actual invocation.
                conn.execute(
                    "DELETE FROM execution_stages WHERE execution_id=? AND stage_id=? "
                    "AND input_hash=?",
                    (execution_id, f"tool_result:{tool_name}", digest),
                )
                conn.execute(
                    "UPDATE execution_operations SET status='intent',receipt_json=NULL "
                    "WHERE execution_id=? AND operation_key=?", (execution_id, key),
                )
            else:
                conn.execute(
                    "INSERT INTO execution_operations VALUES(?,?,?,?,?,NULL)",
                    (execution_id, key, tool_name, digest, "intent"),
                )
        return None

    def observe_operation(
        self, execution_id: str, tool_name: str, arguments: Any, receipt: Mapping[str, Any] | None
    ) -> None:
        from keystone_agents.receipts.normalization import normalize_provider_mutation_receipt

        key = stable_hash({"tool": tool_name, "input": stable_hash(operation_input(arguments))})
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT status,receipt_json FROM execution_operations "
                "WHERE execution_id=? AND operation_key=?",
                (execution_id, key),
            ).fetchone()
            prior_data = (
                json.loads(previous["receipt_json"])
                if previous and previous["receipt_json"]
                else {}
            )
            data = dict(receipt or {})
            preview_without_write = _preview_without_write_evidence(data)
            if (
                data.get("dry_run") is True and data.get("provider_write") is False
                and preview_without_write
            ):
                if previous and previous["status"] in {"intent", "no_effect"} and (
                    not prior_data or previous["status"] == "no_effect"
                ):
                    conn.execute(
                        "UPDATE execution_operations SET status='no_effect',receipt_json=? "
                        "WHERE execution_id=? AND operation_key=?",
                        (stable_json(data), execution_id, key),
                    )
                # A dry-run claim must never clear a prior actual/unknown effect.
                return
            # Status-only legacy results can retain operation provenance already
            # observed at the provider boundary; never manufacture missing object IDs.
            for field in ("operation", "provider"):
                if not data.get(field) and prior_data.get(field):
                    data[field] = prior_data[field]
            normalized = None
            try:
                if data:
                    normalized = normalize_provider_mutation_receipt(
                        {**data, "tool_name": tool_name}
                    )
            except (TypeError, ValueError):
                normalized = None
            status = "unknown"
            if normalized is not None and normalized.object_id:
                status = "verified" if normalized.verification_passed is True else "observed"
            if prior_data and not (previous and previous["status"] == "no_effect"):
                try:
                    prior = normalize_provider_mutation_receipt(
                        {**prior_data, "tool_name": tool_name}
                    )
                except (TypeError, ValueError):
                    prior = None
                if prior is not None and prior.object_id:
                    if status == "unknown":
                        return
                    if normalized.object_id != prior.object_id:
                        raise ExecutionConflict(
                            "Provider response changed the recorded operation identity."
                        )
            conn.execute(
                "UPDATE execution_operations SET status=?,receipt_json=? "
                "WHERE execution_id=? AND operation_key=?",
                (status, stable_json(data) if data else None, execution_id, key),
            )

    def reconcile_operation(
        self, execution_id: str, tool_name: str, arguments: Any, receipt: Mapping[str, Any]
    ) -> None:
        """Accept exact-object read-back without retrying an unknown write."""
        from keystone_agents.receipts.normalization import normalize_provider_mutation_receipt

        key = stable_hash({"tool": tool_name, "input": stable_hash(operation_input(arguments))})
        with self.connection() as conn:
            row = conn.execute(
                "SELECT receipt_json,status FROM execution_operations "
                "WHERE execution_id=? AND operation_key=?",
                (execution_id, key),
            ).fetchone()
        if row is None or not row[0] or row["status"] == "no_effect":
            raise UncertainOperation("No observed object exists for automatic reconciliation.")
        prior = normalize_provider_mutation_receipt({**json.loads(row[0]), "tool_name": tool_name})
        new = normalize_provider_mutation_receipt({**receipt, "tool_name": tool_name})
        if (
            not prior.object_id
            or prior.object_id != new.object_id
            or new.verification_passed is not True
        ):
            raise ExecutionConflict("Reconciliation must verify the same observed provider object.")
        self.observe_operation(execution_id, tool_name, arguments, receipt)

    def operations(self, execution_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM execution_operations WHERE execution_id=? ORDER BY rowid",
                    (execution_id,),
                )
            ]

    def stage_result(self, execution_id: str, stage_id: str, input_value: Any) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT output_json FROM execution_stages WHERE execution_id=? "
                "AND stage_id=? AND input_hash=?",
                (execution_id, stage_id, stable_hash(input_value)),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def save_stage(
        self, execution_id: str, stage_id: str, input_value: Any, output: Mapping[str, Any]
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO execution_stages VALUES(?,?,?,?)",
                (execution_id, stage_id, stable_hash(input_value), stable_json(dict(output))),
            )

    def prepare_delivery(
        self, execution_id: str, destination: str, result: Mapping[str, Any]
    ) -> dict:
        destination_hash, result_hash = stable_hash(destination), stable_hash(dict(result))
        with self.connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO execution_deliveries "
                "(execution_id,destination_hash,result_hash) VALUES(?,?,?)",
                (execution_id, destination_hash, result_hash),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM execution_deliveries WHERE execution_id=? "
                    "AND destination_hash=? AND result_hash=?",
                    (execution_id, destination_hash, result_hash),
                ).fetchone()
            )

    def acknowledge_delivery(
        self,
        execution_id: str,
        destination: str,
        result: Mapping[str, Any],
        provider_message_id: str,
    ) -> None:
        if not provider_message_id.strip():
            raise ValueError("Delivery acknowledgment requires exact provider message identity.")
        with self.connection() as conn:
            cursor = conn.execute(
                "UPDATE execution_deliveries SET status='delivered',provider_message_id=? "
                "WHERE execution_id=? AND destination_hash=? AND result_hash=? "
                "AND (provider_message_id='' OR provider_message_id=?)",
                (
                    provider_message_id,
                    execution_id,
                    stable_hash(destination),
                    stable_hash(dict(result)),
                    provider_message_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ExecutionConflict("Delivery identity conflicts with the pending result.")


@dataclass(frozen=True)
class DurableExecution:
    store: ExecutionStore
    execution_id: str


_ACTIVE: ContextVar[DurableExecution | None] = ContextVar("kba_durable_execution", default=None)


def current_execution() -> DurableExecution | None:
    active = _ACTIVE.get()
    if active is not None:
        return active
    reference = os.environ.get(EXECUTION_REFERENCE_ENV)
    if reference is None:
        return None
    return _execution_from_child_reference(reference)


def _child_runtime_identity() -> dict[str, Any]:
    from keystone_agents.runtime.provenance import current_runtime_fingerprint

    fingerprint = current_runtime_fingerprint()
    # Child deadlines and request-budget leases intentionally change configuration.
    # Compare loaded code/dependencies instead; provider scopes are bound separately.
    return {
        key: fingerprint[key]
        for key in (
            "schema", "source_sha256", "source_read_error_count", "dependency_sha256",
            "python_runtime", "package_versions",
        )
    }


def execution_child_environment() -> dict[str, str]:
    """Export identity only, never a new execution, budget, client, or approval."""
    execution = current_execution()
    if execution is None:
        return {}
    row = execution.store.get(execution.execution_id)
    if row["status"] != "running":
        raise ExecutionConflict("Child execution requires an active parent execution.")
    reference = {
        "schema": EXECUTION_SCHEMA,
        "journal_path": str(execution.store.path),
        "execution_id": execution.execution_id,
        "request_hash": row["request_hash"],
        "source_fingerprint": row["source_fingerprint"],
        "runtime_identity": _child_runtime_identity(),
    }
    return {EXECUTION_REFERENCE_ENV: json.dumps(reference, separators=(",", ":"))}


def _execution_from_child_reference(encoded: str) -> DurableExecution:
    """Reopen the exact existing parent journal before any child model/tool work.

    Do not set a ContextVar: inherited references must disappear when their
    environment is removed, and each boundary rechecks the authoritative status.
    """
    from keystone_agents.storage.sqlite_store import _assert_test_database_is_isolated

    try:
        reference = json.loads(encoded) if len(encoded) <= 16_384 else None
        if not isinstance(reference, dict) or set(reference) != {
            "schema", "journal_path", "execution_id", "request_hash",
            "source_fingerprint", "runtime_identity",
        }:
            raise ValueError("Invalid reference shape")
        for key in ("schema", "journal_path", "execution_id", "request_hash"):
            if not isinstance(reference[key], str) or not reference[key].strip():
                raise ValueError("Missing reference identity")
        if reference["schema"] != EXECUTION_SCHEMA:
            raise ValueError("Incompatible reference schema")
        path = Path(reference["journal_path"])
        if not path.is_absolute() or str(path.resolve()) != str(path):
            raise ValueError("Journal path must be canonical and absolute")
        _assert_test_database_is_isolated(str(path))
        _assert_test_database_is_isolated(str(path).removesuffix(".execution.sqlite3"))
        store = ExecutionStore(path, initialize=False)
        row = store.get(reference["execution_id"])
        if (
            row["status"] != "running"
            or row["request_hash"] != reference["request_hash"]
            or row["source_fingerprint"] != reference["source_fingerprint"]
            or reference["runtime_identity"] != _child_runtime_identity()
        ):
            raise ValueError("Inactive or incompatible parent execution")
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, RuntimeError) as exc:
        raise ExecutionConflict("Child execution reference is missing or incompatible.") from exc
    return DurableExecution(store, row["id"])


@contextmanager
def activate_execution(store: ExecutionStore, execution_id: str) -> Iterator[DurableExecution]:
    value = DurableExecution(store, execution_id)
    token = _ACTIVE.set(value)
    try:
        yield value
    finally:
        _ACTIVE.reset(token)
