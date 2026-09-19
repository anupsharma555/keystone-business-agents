"""Actual CLI preflight failures retain safe SDK diagnostics in the business DB."""

from __future__ import annotations

import json

from agents import _debug
from agents.models.interface import Model, ModelProvider, ModelResponse
from agents.usage import Usage
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from keystone_agents import cli
from keystone_agents.agents import orchestrator
from keystone_agents.schemas.orchestrator import OrchestratorResult, OrchestratorRouteDecision
from keystone_agents.sdk import build_local_run_config
from keystone_agents.storage.sqlite_store import SQLiteStore


def test_cli_persists_actual_preflight_schema_diagnostics_without_model_values(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    monkeypatch.delenv("KEYSTONE_TRACE_PROCESSOR", raising=False)
    monkeypatch.delenv("KEYSTONE_TRACE_SUMMARY_DB", raising=False)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    private_key = "synthetic_private_dynamic_key"
    private_value = "synthetic_private_model_value"
    routes = ["business_research_analyst", "opportunity_scout"]
    output = OrchestratorResult(
        route=routes[0],
        target_agent=routes[0],
        workflow=routes,
        routing_mode="llm",
        decision=OrchestratorRouteDecision(
            decision_owner="orchestrator",
            decision_stage="orchestrator_route_selection",
            selected_candidate_id=routes[0],
            selected_candidate_ids=routes,
            candidate_assessments=[
                {
                    "candidate_id": route,
                    "disposition": "selected",
                    "rationale": private_value,
                }
                for route in routes
            ],
            reasoning=private_value,
        ),
    ).model_dump(mode="json", exclude={"retrieval_diagnostics"})
    # The decision root validator fails without including the model's private values.
    output["decision"]["selected_candidate_ids"] = routes[:1]
    output["audit_notes"] = [private_key, private_value]

    class InvalidPreflightModel(Model):
        calls = 0

        async def get_response(self, *args, **kwargs):
            self.calls += 1
            return ModelResponse(
                output=[
                    ResponseOutputMessage(
                        id="synthetic-preflight-output",
                        type="message",
                        role="assistant",
                        status="completed",
                        content=[
                            ResponseOutputText(
                                type="output_text", text=json.dumps(output), annotations=[]
                            )
                        ],
                    )
                ],
                usage=Usage(requests=1, input_tokens=10, output_tokens=20, total_tokens=30),
                response_id="synthetic-preflight-response",
            )

        def stream_response(self, *args, **kwargs):
            raise NotImplementedError

    model = InvalidPreflightModel()

    class Provider(ModelProvider):
        def get_model(self, model_name):
            return model

    local_config = build_local_run_config(Provider())
    actual_preflight = orchestrator.run_orchestrator_preflight

    def preflight_with_fake_model(request, **kwargs):
        return actual_preflight(
            request,
            **{
                **kwargs,
                "run_config": local_config,
                "live_orchestrator": False,
                "live_manual_plan": False,
            },
        )

    monkeypatch.setattr(cli, "run_orchestrator_preflight", preflight_with_fake_model)
    database_path = tmp_path / "business.db"
    database_url = f"sqlite:///{database_path}"
    exit_code = cli.main(
        [
            "ask",
            "--agent",
            "orchestrator",
            "--no-live-sdk",
            "--json",
            "--database-url",
            database_url,
            "--max-openai-requests",
            "1",
            "Research Example Analytics and identify potential collaboration opportunities.",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert model.calls == 1
    store = SQLiteStore(database_url)
    attempts = [
        row for row in store.fetch_all("agent_runs") if row["agent_name"] == "kba_entrypoint"
    ]
    assert len(attempts) == 1
    attempt = json.loads(attempts[0]["output_json"])
    assert attempts[0]["status"] == attempt["terminal"]["status"] == "failed"
    stages = attempt["decision_trace"]["stages"]
    assert len(stages) == 1
    diagnostic = stages[0]["request_cache"]["validation_diagnostics"]
    detail = diagnostic["failures"][0]["errors"][0]
    assert detail["type"] == "value_error"
    assert detail["rule_code"] == "decision_selected_set_mismatch"
    assert detail["location"] == ["decision"]
    assert detail["validator"]["function"] == "_validate_selection_shape"
    assert detail["validator"]["file"] == "src/keystone_agents/schemas/decision_ownership.py"
    assert detail["validator"]["line"] > 0
    for private in (private_key, private_value):
        assert private not in json.dumps(attempt)
        assert private not in captured.out + captured.err
        assert private.encode() not in database_path.read_bytes()
