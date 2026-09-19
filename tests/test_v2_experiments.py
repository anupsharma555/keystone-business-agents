"""Behavioral controls for isolated V2 experiments and actual context parity fixes."""

from __future__ import annotations

import base64
import hashlib
import json
from argparse import Namespace
from typing import Any

import pytest
from agents.models.interface import Model, ModelProvider, ModelResponse
from agents.usage import Usage
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from keystone_agents.experiments import v2
from keystone_agents.runtime.request_budget import activate_model_request_budget
from keystone_agents.schemas.experiment_v2 import ExperimentReview
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkItem,
    WorkItemKind,
    WorkItemRoute,
)
from keystone_agents.sdk import build_local_run_config


class FakeModel(Model):
    def __init__(self, outputs):
        self.outputs = outputs
        self.calls = []

    async def get_response(
        self,
        system_instructions,
        input,
        model_settings,
        tools,
        output_schema,
        handoffs,
        tracing,
        **kwargs,
    ):
        self.calls.append(
            {
                "input": input,
                "tools": tools,
                "handoffs": handoffs,
                "instructions": system_instructions,
            }
        )
        payload = self.outputs.pop(0)
        return ModelResponse(
            output=[
                ResponseOutputMessage(
                    id=f"output-{len(self.calls)}",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[
                        ResponseOutputText(
                            type="output_text", text=json.dumps(payload), annotations=[]
                        )
                    ],
                )
            ],
            usage=Usage(requests=1, input_tokens=10, output_tokens=10),
            response_id=f"response-{len(self.calls)}",
        )

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


class FakeProvider(ModelProvider):
    def __init__(self, model):
        self.model = model

    def get_model(self, model_name):
        return self.model


def _case():
    return v2.load_catalog()[1][0]


def test_all_seven_controls_include_held_out_cases_without_quality_claims(tmp_path):
    report = v2.run_experiments(split="held_out")

    assert len(report.definitions) == 7
    assert len(report.observations) == 13
    assert report.model_requests == 0
    assert report.quality_status == "UNMEASURED"
    from importlib.util import find_spec

    graph_available = find_spec("langgraph") is not None
    for row in report.observations:
        needs_graph = row.experiment_id in {"same_agent_stages", "checkpoint_replay"}
        if needs_graph and not graph_available:
            assert row.status == "failed"
            assert any(not check.passed for check in row.checks)
            assert row.model_requests == 0
        else:
            assert row.status == "completed"
            assert all(check.passed for check in row.checks)
    assert all(row.split == "held_out" for row in report.observations)
    v2.write_report(report, tmp_path)
    assert "UNMEASURED" in (tmp_path / "report.md").read_text()
    saved = json.loads((tmp_path / "report.json").read_text())
    assert saved["provider_writes"] == 0
    with pytest.raises(ValueError, match="Scripted/fake"):
        v2.apply_human_ratings(report, {})


@pytest.mark.parametrize("kind", ["unknown_review_claim", "unknown_review_source"])
def test_critic_must_bind_both_claim_and_source(kind):
    case = _case()
    review = ExperimentReview(
        findings=[
            {
                "claim_id": "unknown" if kind == "unknown_review_claim" else "c1",
                "source_ids": ["unknown" if kind == "unknown_review_source" else "pilot-primary"],
                "reason": "A source-bound concern is required.",
                "repair": "revise",
            }
        ],
        recommendation="revise",
    )

    assert v2.validate_review(review, case.scripted_answer, case) == [kind]
    with pytest.raises(ValueError):
        ExperimentReview(recommendation="accept", grants_authority=True)


def test_critic_can_report_omitted_obligation_but_cannot_invent_one():
    case = _case()
    omitted = case.scripted_answer.model_copy(update={"claims": []})
    finding = {
        "obligation_id": case.obligations[0].obligation_id,
        "reason": "The requested evidence distinction is missing.",
        "repair": "revise",
    }
    review = ExperimentReview(findings=[finding], recommendation="revise")
    assert v2.validate_review(review, omitted, case) == []
    assert review.findings[0].claim_id == ""
    unknown = ExperimentReview(
        findings=[{**finding, "obligation_id": "unrequested-deliverable"}], recommendation="revise"
    )
    assert v2.validate_review(unknown, omitted, case) == ["unknown_review_obligation"]
    with pytest.raises(ValueError):
        ExperimentReview(
            findings=[{**finding, "claim_id": "c1", "source_ids": ["pilot-primary"]}],
            recommendation="revise",
        )


@pytest.mark.parametrize("fault", ["both_ids", "neither_id", "claim_without_sources"])
def test_review_targets_are_exclusive_in_native_sdk_json_schema(fault):
    from agents.agent_output import AgentOutputSchema
    from agents.exceptions import ModelBehaviorError
    from jsonschema import Draft202012Validator

    finding = {
        "claim_id": "c1", "obligation_id": "", "source_ids": ["pilot-primary"],
        "reason": "The supplied evidence does not support the claim.", "repair": "revise",
    }
    if fault == "both_ids":
        finding["obligation_id"] = "requested-evidence"
    elif fault == "neither_id":
        finding["claim_id"] = ""
    else:
        finding["source_ids"] = []
    payload = {"findings": [finding], "recommendation": "revise", "grants_authority": False}
    native = AgentOutputSchema(ExperimentReview)
    # This must fail before Python-only model validators are considered.
    assert not Draft202012Validator(native.json_schema()).is_valid(payload)
    with pytest.raises(ModelBehaviorError):
        native.validate_json(json.dumps(payload))


@pytest.mark.parametrize("target", ["claim", "obligation"])
def test_review_target_schema_preserves_valid_flat_serialized_reports(target):
    from agents.agent_output import AgentOutputSchema
    from jsonschema import Draft202012Validator

    finding = {
        "claim_id": "c1" if target == "claim" else "",
        "obligation_id": "requested-evidence" if target == "obligation" else "",
        "source_ids": ["pilot-primary"] if target == "claim" else [],
        "reason": "Correct the claim or supply the requested missing work.", "repair": "revise",
    }
    old_serialized = {
        "findings": [finding], "recommendation": "revise", "grants_authority": False,
    }
    native = AgentOutputSchema(ExperimentReview)
    assert Draft202012Validator(native.json_schema()).is_valid(old_serialized)
    parsed = native.validate_json(json.dumps(old_serialized))
    assert parsed.model_dump(mode="json") == old_serialized
    assert ExperimentReview.model_validate_json(json.dumps(old_serialized)) == parsed
    assert set(parsed.findings[0].model_dump()) == set(finding)


def test_self_review_carries_author_instructions_and_context_while_critic_is_fresh():
    case = _case()
    calls = {}
    for variant in ("self_review", "independent_critic"):
        model = FakeModel(
            [case.scripted_answer.model_dump(), {"findings": [], "recommendation": "accept"}]
        )
        report = v2.run_experiments(
            experiment_id="independent_critic",
            case_id=case.case_id,
            variant=variant,
            max_model_requests=2,
            run_config=build_local_run_config(FakeProvider(model)),
        )
        assert report.observations[0].status == "completed"
        assert report.quality_status == "UNMEASURED"
        calls[variant] = model.calls
    author, self_review = calls["self_review"]
    _, critic = calls["independent_critic"]
    assert self_review["instructions"].startswith(author["instructions"])
    assert "Same-Author Second Pass" in self_review["instructions"]
    assert "prior_author_input" in json.dumps(self_review["input"])
    assert "copied_context" in json.dumps(self_review["input"])
    assert author["instructions"] not in critic["instructions"]
    assert "prior_author_input" not in json.dumps(critic["input"])
    assert "copied_context" not in json.dumps(critic["input"])
    assert "fresh_evidence_review" in json.dumps(critic["input"])
    assert case.obligations[0].obligation_id in json.dumps(critic["input"])


def test_fake_critic_and_one_revision_are_counted_but_quality_stays_unmeasured():
    case = _case()
    corrected = case.scripted_answer.model_copy(deep=True)
    corrected.claims[1].text = "The pilot cannot establish clinical efficacy."
    model = FakeModel(
        [
            case.scripted_answer.model_dump(),
            {
                "findings": [
                    {
                        "claim_id": "c2",
                        "source_ids": ["pilot-independent"],
                        "reason": "The review explicitly says efficacy is unmeasured.",
                        "repair": "revise",
                    }
                ],
                "recommendation": "revise",
            },
            corrected.model_dump(),
        ]
    )
    with activate_model_request_budget(5) as parent:
        report = v2.run_experiments(
            experiment_id="independent_critic",
            case_id=case.case_id,
            variant="independent_critic",
            max_model_requests=3,
            run_config=build_local_run_config(FakeProvider(model)),
        )
        assert parent.consumed == 3
        assert parent.remaining == 2

    row = report.observations[0]
    assert row.status == "completed"
    assert [stage["owner"] for stage in row.stages] == ["researcher", "critic", "researcher"]
    assert report.model_requests == 3
    assert row.answer == corrected
    assert report.quality_status == "UNMEASURED"
    assert all(not call["tools"] and not call["handoffs"] for call in model.calls)
    assert "planted_error_claim_ids" not in json.dumps(model.calls, default=str)
    assert v2.blinded_presentations(report)


def test_budget_stops_before_revision_and_preserves_completed_observations():
    case = _case()
    model = FakeModel(
        [
            case.scripted_answer.model_dump(),
            {
                "findings": [
                    {
                        "claim_id": "c2",
                        "source_ids": ["pilot-independent"],
                        "reason": "Efficacy is unmeasured.",
                        "repair": "revise",
                    }
                ],
                "recommendation": "revise",
            },
        ]
    )
    with activate_model_request_budget(2) as parent:
        report = v2.run_experiments(
            experiment_id="targeted_repair",
            case_id=case.case_id,
            variant="targeted_repair",
            max_model_requests=4,
            run_config=build_local_run_config(FakeProvider(model)),
        )
        assert parent.consumed == 2

    assert len(model.calls) == 2
    assert report.model_requests == 2
    row = report.observations[0]
    assert row.status == "blocked"
    assert row.review.recommendation == "revise"
    assert row.stages[-1]["model_requests"] == 0


def test_invalid_critic_does_not_trigger_revision():
    case = _case()
    model = FakeModel(
        [
            case.scripted_answer.model_dump(),
            {
                "findings": [
                    {
                        "claim_id": "invented",
                        "source_ids": ["pilot-independent"],
                        "reason": "Unbound objection.",
                        "repair": "revise",
                    }
                ],
                "recommendation": "revise",
            },
        ]
    )
    report = v2.run_experiments(
        experiment_id="independent_critic",
        case_id=case.case_id,
        variant="independent_critic",
        max_model_requests=3,
        run_config=build_local_run_config(FakeProvider(model)),
    )
    assert report.observations[0].status == "failed"
    assert len(model.calls) == 2


def test_invalid_review_preserves_both_attempt_reasons_and_failed_usage_at_request_cap(
    monkeypatch, tmp_path
):
    from agents import _debug

    # Use the SDK's production privacy default; do not enable model-data logging.
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    # Local runs disable retries normally; exercise that production branch offline.
    monkeypatch.setattr("keystone_agents.run._sdk_structured_output_max_retries", lambda **_: 1)
    case = _case()
    private_content = "Synthetic confidential detail " + "sk-" + "synthetic" * 6
    model = FakeModel([
        case.scripted_answer.model_dump(),
        {
            "findings": [{
                "claim_id": "c2", "obligation_id": case.obligations[0].obligation_id,
                "source_ids": [case.sources[0].source_id], "reason": private_content,
                "repair": "revise",
            }],
            "recommendation": "revise",
        },
        {"findings": [], "recommendation": "revise"},
    ])
    report = v2.run_experiments(
        experiment_id="independent_critic", case_id=case.case_id, variant="self_review",
        max_model_requests=3, model="gpt-5.4-mini",
        run_config=build_local_run_config(FakeProvider(model)),
    )
    row = report.observations[0]
    assert report.model_requests == len(model.calls) == 3
    assert row.status == "failed"  # a bad response at the cap is not an admission block
    author, review = row.stages
    assert author["status"] == "completed"
    assert review["status"] == "failed"
    assert review["model_request_admission_blocked"] is False
    assert review["model_requests"] == 2
    assert review["error_type"] == "ModelBehaviorError"
    assert "source-bound criticism" in row.error
    failures = review["validation_failures"]
    assert [item["attempt"] for item in failures] == [1, 2]
    assert {error["type"] for error in failures[0]["errors"]} == {"literal_error"}
    assert {error["location"][-1] for error in failures[0]["errors"]} == {
        "claim_id", "obligation_id",
    }
    assert "source-bound criticism" in failures[1]["errors"][0]["message"]
    assert review["usage"]["requests"] == 2
    # Both responses were observed before schema rejection; retain their numeric
    # usage and label the resulting cost as an estimate rather than an invoice.
    assert review["usage"]["available"] is True
    assert review["usage"]["complete"] is True
    assert review["usage"]["input_tokens"] == review["usage"]["output_tokens"] == 20
    assert review["cost"]["estimated_usd"] > 0
    assert review["cost"]["confidence"] == "estimate"
    failure = review["sdk_run_failure"]
    assert failure["attempt_count"] == 2
    assert failure["request_cache"]["structured_output_retries"] == 1
    assert failure["usage"] == review["usage"]
    assert failure["cost"] == review["cost"]
    assert private_content not in report.model_dump_json()
    assert "sk-syntheticsecret" not in report.model_dump_json()
    assert all("input" not in error and "ctx" not in error for item in failures
               for error in item["errors"])
    v2.write_report(report, tmp_path)
    persisted = json.loads((tmp_path / "report.json").read_text())
    assert persisted["observations"][0]["stages"][1]["usage"]["requests"] == 2
    assert "source-bound criticism" in (tmp_path / "report.md").read_text()


def test_available_failure_usage_and_cost_are_retained_without_sensitive_payload():
    error = RuntimeError("Synthetic private model output " + "sk-" + "synthetic" * 6)
    error.keystone_sdk_run_failure = {
        "schema": "keystone.sdk_run_failure.v1", "attempt_count": 2,
        "usage": {"available": True, "complete": True, "requests": 2, "input_tokens": 20},
        "cost": {"estimated_usd": 0.00001, "note": "x" * 1000, "api_key": "secret-value"},
        "request_cache": {
            "structured_output_retries": 1, "failed_model_attempts": 2,
            "raw_model_output": "synthetic private payload",
        },
        "tool_receipts": [{"message_body": "synthetic private payload"}],
        "raw_result": "synthetic private payload",
    }
    metadata = v2._bounded_failure_metadata(error)
    assert metadata["usage"] == error.keystone_sdk_run_failure["usage"]
    assert metadata["cost"]["estimated_usd"] == 0.00001
    assert len(metadata["cost"]["note"]) <= 400
    assert metadata["cost"]["api_key"] != "secret-value"
    assert metadata["request_cache"]["structured_output_retries"] == 1
    assert "synthetic private payload" not in json.dumps(metadata)
    assert "sk-syntheticsecret" not in v2._safe_failure_detail(error, [])


def test_diagnostic_schema_keeps_sdk_redacted_traceback_and_exact_acceptance(monkeypatch):
    from agents import _debug
    from agents.agent_output import AgentOutputSchema
    from agents.exceptions import ModelBehaviorError

    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    failures = []
    wrapped = v2._diagnostic_output_schema(ExperimentReview, failures)
    native = AgentOutputSchema(ExperimentReview)
    valid = json.dumps({"findings": [], "recommendation": "accept"})
    assert wrapped.validate_json(valid) == native.validate_json(valid)
    invalid = json.dumps({"findings": [], "recommendation": "revise"})
    for schema in (native, wrapped):
        with pytest.raises(ModelBehaviorError) as caught:
            schema.validate_json(invalid)
        assert "source-bound criticism" not in str(caught.value)  # SDK remains redacted
        frame = caught.value.__traceback__
        while frame:
            if frame.tb_frame.f_code.co_filename == v2.__file__:
                assert frame.tb_frame.f_locals.get("json_str") == "<redacted>"
            frame = frame.tb_next
    assert failures[0]["errors"][0]["type"] == "value_error"


def test_budget_block_before_review_keeps_zero_dispatches_distinct_from_failure():
    case = _case()
    model = FakeModel([case.scripted_answer.model_dump()])
    report = v2.run_experiments(
        experiment_id="independent_critic", case_id=case.case_id, variant="self_review",
        max_model_requests=1, run_config=build_local_run_config(FakeProvider(model)),
    )
    row = report.observations[0]
    assert row.status == "blocked"
    assert len(model.calls) == report.model_requests == 1
    review = row.stages[-1]
    assert review["status"] == "blocked"
    assert review["model_requests"] == 0
    assert review["model_request_admission_blocked"] is True
    assert review["error_type"] == "ModelRequestBudgetExhausted"
    assert review["usage"].get("requests", 0) == 0


def test_budget_block_on_review_retry_retains_the_prior_schema_failure(monkeypatch):
    monkeypatch.setattr("keystone_agents.run._sdk_structured_output_max_retries", lambda **_: 1)
    case = _case()
    model = FakeModel([
        case.scripted_answer.model_dump(), {"findings": [], "recommendation": "revise"},
    ])
    report = v2.run_experiments(
        experiment_id="independent_critic", case_id=case.case_id, variant="self_review",
        max_model_requests=2, run_config=build_local_run_config(FakeProvider(model)),
    )
    row = report.observations[0]
    assert row.status == "blocked"
    assert len(model.calls) == report.model_requests == 2
    review = row.stages[-1]
    assert review["model_requests"] == 1  # one bad response, then retry denied before dispatch
    assert review["model_request_admission_blocked"] is True
    assert review["error_type"] == "ModelRequestBudgetExhausted"
    assert "budget exhausted" in row.error.lower()
    assert len(review["validation_failures"]) == 1
    assert "source-bound criticism" in review["validation_failures"][0]["errors"][0]["message"]


def test_complementary_investigators_keep_disjoint_evidence_and_share_total_budget():
    case = _case()
    first = case.scripted_answer.model_copy(update={"claims": case.scripted_answer.claims[:1]})
    second = case.scripted_answer.model_copy(update={"claims": case.scripted_answer.claims[1:]})
    model = FakeModel([first.model_dump(), second.model_dump(), case.scripted_answer.model_dump()])
    report = v2.run_experiments(
        experiment_id="complementary_research",
        case_id=case.case_id,
        variant="complementary_investigators",
        max_model_requests=3,
        run_config=build_local_run_config(FakeProvider(model)),
    )
    row = report.observations[0]
    assert row.status == "completed"
    assert report.model_requests == 3
    assert [stage["owner"] for stage in row.stages] == [
        "investigator_1",
        "investigator_2",
        "researcher",
    ]
    assert "pilot-independent" not in json.dumps(model.calls[0]["input"])
    assert "pilot-primary" not in json.dumps(model.calls[1]["input"])
    assert "pilot-independent" in json.dumps(model.calls[2]["input"])
    assert "pilot-primary" in json.dumps(model.calls[2]["input"])
    assert report.quality_status == "UNMEASURED"


def test_context_variants_share_case_identity_and_preserve_both_layouts():
    case = _case()
    model = FakeModel([case.scripted_answer.model_dump(), case.scripted_answer.model_dump()])
    report = v2.run_experiments(
        experiment_id="context_memory",
        case_id=case.case_id,
        max_model_requests=2,
        run_config=build_local_run_config(FakeProvider(model)),
    )
    baseline, compact = report.observations
    assert baseline.input_fingerprint == compact.input_fingerprint
    assert baseline.answer == compact.answer
    assert compact.stages[0]["input_chars"] < baseline.stages[0]["input_chars"]
    assert compact.presentations.keys() == {"plain", "sectioned"}
    for text in compact.presentations.values():
        assert all(source.source_id in text for source in case.sources)
        assert all(limitation in text for limitation in case.scripted_answer.limitations)


def test_missing_source_cannot_be_recast_as_an_authorized_rewrite():
    with pytest.raises(ValueError, match="cannot be repaired by rewriting"):
        ExperimentReview(
            recommendation="revise",
            findings=[
                {
                    "claim_id": "c1",
                    "source_ids": ["pilot-primary"],
                    "reason": "A missing source is needed.",
                    "repair": "needs_source",
                }
            ],
        )


def _native_images(value):
    if isinstance(value, dict):
        if value.get("type") == "input_image":
            yield value
        for child in value.values():
            yield from _native_images(child)
    elif isinstance(value, list | tuple):
        for child in value:
            yield from _native_images(child)


def test_document_input_pair_passes_exact_native_page_without_leaking_gold_to_text():
    case = next(c for c in v2.load_catalog()[1] if c.case_id == "document-layout-development")
    manifest, page = v2.document_layout_fixture()
    visual_answer = case.scripted_answer.model_copy(deep=True)
    visual_answer.claims[
        1
    ].text = "Prospective clinical outcomes were not evaluated; result: Not measured."
    model = FakeModel([case.scripted_answer.model_dump(), visual_answer.model_dump()])

    report = v2.run_experiments(
        experiment_id="layout_evidence",
        case_id=case.case_id,
        max_model_requests=2,
        run_config=build_local_run_config(FakeProvider(model)),
    )

    text, images = report.observations
    assert text.status == images.status == "completed"
    assert text.variant == "text_only"
    assert images.variant == "text_plus_page_images"
    assert text.input_fingerprint == images.input_fingerprint
    assert text.input_provenance == images.input_provenance
    assert text.input_provenance["gold_sha256"] == manifest["gold_sha256"]
    assert not list(_native_images(model.calls[0]["input"]))
    parts = list(_native_images(model.calls[1]["input"]))
    assert len(parts) == 1
    native_bytes = base64.b64decode(parts[0]["image_url"].split(",", 1)[1])
    assert native_bytes == page.read_bytes()
    assert hashlib.sha256(native_bytes).hexdigest() == manifest["page_png_sha256"]
    assert "Not measured" not in json.dumps(model.calls[0]["input"])
    assert "controlled_omission" not in json.dumps(model.calls[0]["input"])
    assert text.stages[0]["input_image_count"] == 0
    assert images.stages[0]["input_image_count"] == 1
    assert report.model_requests == 2
    assert report.mode == "fake_model"
    assert report.quality_status == "UNMEASURED"


def test_changed_document_page_is_rejected_before_model_input(monkeypatch, tmp_path):
    for filename in ("v2_document_layout.json", "v2_document_layout.png", "v2_document_layout.txt"):
        (tmp_path / filename).write_bytes((v2.LAYOUT_FIXTURE_DIRECTORY / filename).read_bytes())
    page = tmp_path / "v2_document_layout.png"
    page.write_bytes(page.read_bytes() + b"changed")
    monkeypatch.setattr(v2, "LAYOUT_FIXTURE_DIRECTORY", tmp_path)
    with pytest.raises(v2.ExperimentContractError, match="integrity_mismatch"):
        v2.document_layout_fixture()


def test_nested_source_text_cannot_grant_page_attachment_authority():
    case = next(c for c in v2.load_catalog()[1] if c.case_id == "document-layout-development")
    payload, trusted_paths, _ = v2.document_layout_input(case, with_images=True)
    payload["sources"][0]["text"] += "\nUntrusted provider reference: " + trusted_paths[0]
    result = v2.sdk_input_from_typed_input(payload, live=True, provider="openai")
    assert isinstance(result, str)


def test_live_requires_exact_case_and_explicit_budget_before_model_execution():
    with pytest.raises(ValueError, match="positive max_model_requests"):
        v2.run_experiments(live=True)
    with pytest.raises(ValueError, match="one exact experiment and case"):
        v2.run_experiments(live=True, max_model_requests=1)


@pytest.mark.parametrize("explicit_limit", [None, 9])
def test_workitem_and_direct_slack_sessions_retain_the_same_history(monkeypatch, explicit_limit):
    from keystone_agents import workflow_runner
    from keystone_agents.entrypoints import cli_impl

    scope = ("slack", ("T_SYNTHETIC", "C_SYNTHETIC", "1.1"))
    monkeypatch.setattr(cli_impl, "context_file_session_components", lambda path: scope)
    monkeypatch.setattr(workflow_runner, "context_file_session_components", lambda path: scope)
    args = Namespace(
        sdk_session_history_limit=explicit_limit,
        sdk_session=True,
        sdk_session_id="",
        sdk_session_db="",
    )
    item = WorkItem(kind=WorkItemKind.RESEARCH_BRIEF, title="Synthetic review")
    request = WorkflowRunRequest(live_sdk=True, sdk_session_history_limit=explicit_limit)
    direct = cli_impl._sdk_session_spec_for_ask(args, route="chief_of_staff", default_enabled=True)
    graph = workflow_runner._sdk_session_spec_for_work_item(request, item)
    assert direct.session_id == graph.session_id
    assert direct.history_limit == graph.history_limit == (explicit_limit or 6)


def test_chief_model_receives_one_complete_slack_context_without_mutating_workitem(monkeypatch):
    from keystone_agents import workflow_runner

    marker = "SYNTHETIC_LATEST_CORRECTION"
    slack = {"thread_transcript": marker, "latest_user_follow_up": "Tomorrow, not today."}
    plan = {
        "source": "canonical",
        "target_agent": "chief_of_staff",
        "intent": "route_request",
        "workflow": [],
        "ask_shape": {"permission_state": "read_only"},
    }
    item = WorkItem(
        kind=WorkItemKind.RESEARCH_BRIEF,
        title="Synthetic review",
        request_text="Review the project context and recommend the next priority.",
        current_route=WorkItemRoute.CHIEF_OF_STAFF,
        target={"metadata": {"slack_context": slack, "manual_request_plan": plan}},
    )
    request = WorkflowRunRequest(
        request_text=item.request_text, live_sdk=True, save=False, manual_request_plan=plan
    )
    captured: dict[str, Any] = {}

    class StopBeforeModel(BaseException):
        pass

    def capture(payload, **kwargs):
        captured.update(payload)
        raise StopBeforeModel

    monkeypatch.setattr(workflow_runner, "run_chief_of_staff_sdk", capture)
    with pytest.raises(StopBeforeModel):
        workflow_runner._advance_chief_of_staff(item, request=request, store=None)
    assert captured["slack_context"] == slack
    assert json.dumps(captured).count(marker) == 1
    assert item.target.metadata["slack_context"] == slack
    assert captured["request"] == item.request_text
