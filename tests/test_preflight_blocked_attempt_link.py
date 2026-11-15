"""A blocked preflight keeps its current entry ID without a specialist result."""

import json

from keystone_agents.agents.orchestrator import run_orchestrator_preflight
from keystone_agents.entrypoints import cli_impl as cli
from keystone_agents.storage.sqlite_store import SQLiteStore


def test_blocked_preflight_emits_current_attempt_in_existing_failure_contract(
    monkeypatch, tmp_path, capsys,
):
    database = f"sqlite:///{tmp_path / 'blocked-entry.db'}"
    monkeypatch.setenv("KEYSTONE_SDK_SESSIONS", "false")
    monkeypatch.setenv("KEYSTONE_SDK_SESSION_DB", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB", str(tmp_path / "eval.db"))
    preflight = run_orchestrator_preflight("Explain a selected source.").model_copy(update={
        "execution_allowed": False,
        "blocked_by_orchestrator": True,
        "block_kind": "selected_source_identity_unverified",
        "block_reason": "Selected source identity needs verification.",
    })
    monkeypatch.setattr(cli, "run_orchestrator_preflight", lambda *a, **k: preflight)
    def unexpected_model(**kwargs):
        raise AssertionError("The blocked diagnostic must not invoke a model.")

    monkeypatch.setattr(cli, "run_typed_sdk_agent", unexpected_model)
    assert cli.main([
        "ask", "--live-sdk", "--json", "--database-url", database,
        "--input", "Explain a selected source.",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["failure"]["schema"] == "keystone.operator_failure.v1"
    assert payload["failure"]["retryable"] is False
    attempt_id = payload["execution_attempt_id"]
    assert type(attempt_id) is int and attempt_id > 0
    with SQLiteStore(database).managed_connection() as connection:
        row = connection.execute(
            "SELECT agent_name, output_json FROM agent_runs WHERE id=?", (attempt_id,),
        ).fetchone()
    assert row["agent_name"] == "kba_entrypoint"
    assert json.loads(row["output_json"])["terminal"]["status"] == "blocked"
