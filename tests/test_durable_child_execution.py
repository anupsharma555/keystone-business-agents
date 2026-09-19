"""A real local child reuses its parent's journal without model/provider traffic."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from keystone_agents.runtime.durable_execution import (
    EXECUTION_REFERENCE_ENV,
    ExecutionConflict,
    ExecutionStore,
    activate_execution,
    current_execution,
    execution_child_environment,
)
from keystone_agents.runtime.provenance import current_runtime_fingerprint


def _execution(tmp_path, *, budget=3):
    store = ExecutionStore(tmp_path / "execution.db")
    row = store.begin(
        {"request": "Inspect synthetic evidence"},
        budget_limit=budget,
        source_fingerprint=current_runtime_fingerprint()["runtime_sha256"],
    )
    store.update(row["id"], status="running")
    with activate_execution(store, row["id"]):
        environment = execution_child_environment()
    return store, row["id"], environment


def _child_environment(reference):
    # No credentials, provider clients, or operator database configuration.
    return {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "PYTHON_DOTENV_DISABLED": "1",
        "KEYSTONE_TEST_MODE": "1",
        "KEYSTONE_MODEL_REQUEST_BUDGET_LIMIT": "999",  # cannot enlarge journal budget
        **reference,
    }


_CHILD = """
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from keystone_agents.receipts.journal import durable_provider_tool
from keystone_agents.runtime.durable_execution import current_execution

counter = Path(sys.argv[1])
@durable_provider_tool('airtable_write_record')
def synthetic_provider(value, *, live=True):
    count = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(count + 1))
    return {
        'status': 'success', 'provider': 'airtable', 'operation': 'create_record',
        'record_id': 'rec_synthetic_child', 'provider_write': True,
        'verification': {'passed': True},
    }

# This deterministic provider boundary deliberately precedes SDK import/start.
receipt = synthetic_provider('synthetic')
from keystone_agents.sdk import _ExecutionBoundaryRunHooks
from keystone_agents.runtime.request_budget import current_model_request_budget
local_budget = current_model_request_budget()
asyncio.run(_ExecutionBoundaryRunHooks(local_budget, None).on_llm_start(
    None, SimpleNamespace(name='synthetic_child'), None, []))
execution = current_execution()
print(json.dumps({
    'execution_id': execution.execution_id,
    'consumed': execution.store.get(execution.execution_id)['consumed'],
    'local_limit': local_budget.limit if local_budget else None,
    'record_id': receipt['record_id'],
    'output_type': 'TestOutput',
    'human_summary': 'Synthetic evidence inspected.',
    'output': {'summary': 'Synthetic evidence inspected.', 'send_enabled': False},
    'send_enabled': False,
}))
"""


def test_real_children_share_provider_receipt_and_persisted_sdk_budget(tmp_path):
    store, execution_id, environment = _execution(tmp_path)
    store.reserve_model(execution_id, "parent_preflight")
    counter = tmp_path / "synthetic-provider-count"
    outputs = []
    with activate_execution(store, execution_id) as parent:
        for _ in range(3):
            outputs.append(subprocess.run(
                [sys.executable, "-c", _CHILD, str(counter)],
                env=_child_environment(environment), capture_output=True, text=True, timeout=30,
            ))
        assert current_execution() is parent
        assert EXECUTION_REFERENCE_ENV not in os.environ
    assert current_execution() is None
    assert [result.returncode for result in outputs] == [0, 0, 1]
    assert [json.loads(result.stdout)["consumed"] for result in outputs[:2]] == [2, 3]
    assert [json.loads(result.stdout)["local_limit"] for result in outputs[:2]] == [999, 999]
    assert {json.loads(result.stdout)["execution_id"] for result in outputs[:2]} == {execution_id}
    assert "Durable model request budget exhausted" in outputs[2].stderr
    assert counter.read_text() == "1"
    assert store.get(execution_id)["consumed"] == 3
    assert len(store.list_executions()) == 1
    assert [operation["status"] for operation in store.operations(execution_id)] == ["verified"]


def test_cli_passes_reference_to_actual_child_without_setting_parent_environment(
    tmp_path, monkeypatch, capsys
):
    from keystone_agents.entrypoints import cli_impl as cli

    store, execution_id, _ = _execution(tmp_path)
    counter = tmp_path / "synthetic-provider-count"
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))
    with activate_execution(store, execution_id) as parent:
        code = cli._run_ask_script_live(
            "business_research_analyst", "Inspect synthetic evidence.",
            [sys.executable, "-c", _CHILD, str(counter)],
            json_output=True, manual_plan=None,
            database_url=f"sqlite:///{tmp_path / 'business.db'}",
        )
        assert current_execution() is parent
    # The deliberately minimal synthetic answer is rejected by public-output
    # review; that must not erase the completed child's durable tool/model work.
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["script_payload"]["execution_id"] == execution_id
    assert store.get(execution_id)["consumed"] == 1
    assert counter.read_text() == "1"
    assert EXECUTION_REFERENCE_ENV not in os.environ
    assert current_execution() is None


@pytest.mark.parametrize("fault", [
    "empty", "malformed", "missing_fields", "missing_database", "missing_execution",
    "completed", "failed", "pending", "schema", "request", "source", "runtime", "relative",
])
def test_invalid_inherited_reference_fails_before_provider_and_creates_nothing(
    tmp_path, monkeypatch, fault
):
    from keystone_agents.receipts.journal import durable_provider_tool

    store, execution_id, environment = _execution(tmp_path)
    reference = json.loads(environment[EXECUTION_REFERENCE_ENV])
    if fault == "missing_fields":
        reference.pop("journal_path")
    elif fault == "missing_database":
        reference["journal_path"] = str(tmp_path / "never-created.db")
    elif fault == "missing_execution":
        reference["execution_id"] = "ex_missing_synthetic"
    elif fault in {"completed", "failed", "pending"}:
        store.update(execution_id, status=fault)
    elif fault == "runtime":
        reference["runtime_identity"]["source_sha256"] = "incompatible"
    elif fault == "relative":
        reference["journal_path"] = "execution.db"
    elif fault in {"schema", "request", "source"}:
        key = {"schema": "schema", "request": "request_hash", "source": "source_fingerprint"}
        reference[key[fault]] = "incompatible"
    encoded = {"empty": "", "malformed": "{"}.get(fault, json.dumps(reference))
    monkeypatch.setenv(EXECUTION_REFERENCE_ENV, encoded)
    calls = []

    @durable_provider_tool("airtable_write_record")
    def provider():
        calls.append("executed")

    before = sorted(path.name for path in tmp_path.iterdir())
    with pytest.raises(ExecutionConflict, match="reference"):
        provider()
    assert calls == []
    assert sorted(path.name for path in tmp_path.iterdir()) == before
    assert len(store.list_executions()) == 1
    assert store.get(execution_id)["consumed"] == 0


def test_inherited_reference_rechecks_status_and_does_not_leak_context(tmp_path, monkeypatch):
    store, execution_id, environment = _execution(tmp_path)
    monkeypatch.setenv(EXECUTION_REFERENCE_ENV, environment[EXECUTION_REFERENCE_ENV])
    assert current_execution().execution_id == execution_id
    store.update(execution_id, status="completed")
    with pytest.raises(ExecutionConflict):
        current_execution()
    monkeypatch.delenv(EXECUTION_REFERENCE_ENV)
    assert current_execution() is None


@pytest.mark.parametrize("explicit_child_flag", [False, True])
def test_explicit_business_database_reaches_child_without_parent_environment_mutation(
    tmp_path, monkeypatch, capsys, explicit_child_flag
):
    from keystone_agents.entrypoints import cli_impl as cli
    from keystone_agents.storage.sqlite_store import SQLiteStore

    store, execution_id, _ = _execution(tmp_path)
    unrelated = f"sqlite:///{tmp_path / 'unrelated-parent.db'}"
    supplied = f"sqlite:///{tmp_path / 'supplied-business.db'}"
    explicit = f"sqlite:///{tmp_path / 'explicit-child.db'}"
    monkeypatch.setenv("DATABASE_URL", unrelated)
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))
    script = """
import argparse
import json
import os
from keystone_agents.storage.sqlite_store import SQLiteStore
parser = argparse.ArgumentParser()
parser.add_argument('--database-url', default=None)
args = parser.parse_args()
database = args.database_url or os.environ['DATABASE_URL']
SQLiteStore(database).save_agent_run(
    agent_name='synthetic_child', output={'summary': 'Synthetic fixture saved.'},
    status='success', dry_run=True)
print(json.dumps({
    'database_url': database, 'environment_database_url': os.environ['DATABASE_URL'],
    'output': {'summary': 'Synthetic fixture saved.', 'send_enabled': False},
    'send_enabled': False,
}))
"""
    command = [sys.executable, "-c", script]
    if explicit_child_flag:
        command.extend(["--database-url", explicit])
    with activate_execution(store, execution_id):
        cli._run_ask_script_live(
            "business_research_analyst", "Inspect synthetic evidence.", command,
            json_output=True, manual_plan=None, database_url=supplied,
        )
    child = json.loads(capsys.readouterr().out)["script_payload"]
    expected = explicit if explicit_child_flag else supplied
    assert child["environment_database_url"] == supplied
    assert child["database_url"] == expected
    assert os.environ["DATABASE_URL"] == unrelated
    assert not (tmp_path / "unrelated-parent.db").exists()
    with SQLiteStore(expected).connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM agent_runs WHERE agent_name='synthetic_child'"
        ).fetchone()[0]
    assert count == 1


def test_reopening_missing_journal_does_not_recreate_it(tmp_path):
    store, _, _ = _execution(tmp_path)
    reopened = ExecutionStore(store.path, initialize=False)
    store.path.unlink()
    import sqlite3

    with pytest.raises(sqlite3.OperationalError):
        reopened.list_executions()
    assert not store.path.exists()
