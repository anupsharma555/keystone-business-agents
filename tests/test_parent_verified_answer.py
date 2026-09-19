"""Verified child answers must survive exact-shape parent rendering."""

import json
from types import SimpleNamespace

import pytest

from keystone_agents.entrypoints import cli_impl as cli
from keystone_agents.schemas.manual_request_plan import AskShapePolicy, ManualRequestPlan
from keystone_agents.schemas.output_constraints import InterpretedOutputConstraints


@pytest.mark.parametrize("layout", ["inline", "footer"])
@pytest.mark.parametrize("child_fits", [True, False])
def test_parent_chooses_complete_constraint_satisfying_answer_without_repair(
    monkeypatch,
    capsys,
    tmp_path,
    child_fits,
    layout,
):
    prose = "The newsletter describes a home monitoring device. It discusses a payment program."
    separator = " " if layout == "inline" else "\n\n"
    cited = prose + separator + "Gmail link: https://example.test/message"
    child = cited if child_fits else "Extra introduction. " + cited
    typed = prose if child_fits else cited
    plan = ManualRequestPlan(
        source="llm",
        target_agent="gmail_triage",
        intent="context_lookup",
        primary_target="Synthetic newsletter",
        ask_shape=AskShapePolicy(
            output_constraints=InterpretedOutputConstraints(
                scope="entire_response",
                sentence_count_mode="exact",
                sentence_count=2,
                include_source_urls=True,
            ),
        ),
    )
    monkeypatch.setattr(
        cli,
        "run_isolated_child_process",
        lambda *_a, **_k: SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                {
                    "status": "done",
                    "human_summary": child,
                    "user_facing_result_verified": True,
                    "send_enabled": False,
                    "output_type": "EmailTriageResult",
                    "public_result": {
                        "status": "completed",
                        "completion_confirmed": True,
                        "provider_write_attempted": False,
                    },
                    "tool_receipts": [{"status": "success", "operation": "read_gmail_context"}],
                    "output": {"summary": typed, "needs_reply": False},
                }
            ),
        ),
    )
    original = cli.resolve_instruction_following_response

    def offline_only(text, **kwargs):
        # The parent must supply already valid text; no model repair is allowed.
        assert text == cited
        kwargs["live"] = False
        return original(text, **kwargs)

    monkeypatch.setattr(cli, "resolve_instruction_following_response", offline_only)
    code = cli._run_ask_script_live(
        "gmail_triage",
        "Summarize this newsletter in two sentences and include its Gmail link.",
        ["unused-child-command"],
        json_output=True,
        manual_plan=plan,
        database_url=f"sqlite:///{tmp_path / 'isolated.db'}",
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["human_summary"] == cited
    assert payload["instruction_following"]["validation"]["passed"]
    assert not payload["instruction_following"]["repair_attempted"]
    assert payload["public_result"]["text"] == cited
    receipt = payload["child_result_promotion_receipt"]
    assert receipt["reader_ready"]
    assert receipt["child_public_result_verified"]


def test_parent_does_not_promote_unverified_child_answer(
    monkeypatch,
    capsys,
    tmp_path,
):
    prose = "The newsletter describes a home monitoring device. It discusses a payment program."
    unverified = prose + " Gmail link: https://unverified.example/message"
    plan = ManualRequestPlan(
        source="llm",
        target_agent="gmail_triage",
        intent="context_lookup",
        primary_target="Synthetic newsletter",
        ask_shape=AskShapePolicy(
            output_constraints=InterpretedOutputConstraints(
                scope="entire_response",
                sentence_count_mode="exact",
                sentence_count=2,
                include_source_urls=True,
            ),
        ),
    )
    monkeypatch.setattr(
        cli,
        "run_isolated_child_process",
        lambda *_a, **_k: SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                {
                    "status": "done",
                    "human_summary": unverified,
                    "user_facing_result_verified": False,
                    "send_enabled": False,
                    "output_type": "EmailTriageResult",
                    "public_result": {
                        "status": "completed",
                        "completion_confirmed": True,
                        "provider_write_attempted": False,
                    },
                    "tool_receipts": [
                        {"status": "success", "operation": "read_gmail_context"}
                    ],
                    "output": {"summary": prose, "needs_reply": False},
                }
            ),
        ),
    )
    original = cli.resolve_instruction_following_response
    monkeypatch.setattr(
        cli,
        "resolve_instruction_following_response",
        lambda text, **kwargs: original(text, **{**kwargs, "live": False}),
    )

    code = cli._run_ask_script_live(
        "gmail_triage",
        "Summarize this newsletter in two sentences and include its Gmail link.",
        ["unused-child-command"],
        json_output=True,
        manual_plan=plan,
        database_url=f"sqlite:///{tmp_path / 'unverified.db'}",
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["public_result"]["status"] == "blocked"
    assert payload["human_summary"] != unverified
    assert payload["script_payload"]["tool_receipts"][0]["status"] == "success"


def _verification_plan(formatting_metadata: str) -> ManualRequestPlan:
    kwargs = {
        "source": "llm",
        "target_agent": "gmail_triage",
        "intent": "context_lookup",
        "primary_target": "Synthetic newsletter",
    }
    if formatting_metadata == "present":
        kwargs["ask_shape"] = AskShapePolicy(
            output_constraints=InterpretedOutputConstraints(
                scope="entire_response",
                sentence_count_mode="exact",
                sentence_count=2,
                include_source_urls=True,
            )
        )
    elif formatting_metadata == "empty":
        kwargs["ask_shape"] = AskShapePolicy(
            output_constraints=InterpretedOutputConstraints()
        )
    elif formatting_metadata == "advisory_only":
        kwargs["ask_shape"] = AskShapePolicy(
            output_constraints=InterpretedOutputConstraints(
                interpretation="Keep the answer concise and readable.",
                style_requirements=["concise", "readable"],
            )
        )
    return ManualRequestPlan(**kwargs)


def _verification_child_payload(*, verified: bool) -> dict[str, object]:
    prose = (
        "The newsletter describes a synthetic monitoring device. "
        "It discusses a payment program."
    )
    return {
        "status": "done",
        "human_summary": prose + " Gmail link: https://example.test/message",
        "user_facing_result_verified": verified,
        "send_enabled": False,
        "output_type": "EmailTriageResult",
        "public_result": {
            "status": "completed",
            "completion_confirmed": True,
            "provider_write_attempted": False,
        },
        "tool_receipts": [
            {"status": "success", "operation": "read_gmail_context"}
        ],
        "output": {"summary": prose, "needs_reply": False},
    }


@pytest.mark.parametrize(
    "formatting_metadata",
    ["present", "omitted", "empty", "advisory_only"],
)
@pytest.mark.parametrize(
    "request_text",
    [
        "Summarize this synthetic newsletter and include its Gmail link.",
        (
            "Summarize this synthetic newsletter in exactly two sentences and "
            "include its Gmail link."
        ),
    ],
)
def test_explicit_unverified_child_blocks_independently_of_formatting(
    monkeypatch,
    capsys,
    tmp_path,
    formatting_metadata,
    request_text,
):
    child = _verification_child_payload(verified=False)
    monkeypatch.setattr(
        cli,
        "run_isolated_child_process",
        lambda *_a, **_k: SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(child),
        ),
    )
    monkeypatch.setattr(
        cli,
        "resolve_instruction_following_response",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("explicitly unverified content must not enter repair")
        ),
    )

    code = cli._run_ask_script_live(
        "gmail_triage",
        request_text,
        ["unused-child-command"],
        json_output=True,
        manual_plan=_verification_plan(formatting_metadata),
        database_url=f"sqlite:///{tmp_path / 'explicit-unverified.db'}",
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["block_kind"] == "child_result_verification_failed"
    assert payload["public_result"]["status"] == "blocked"
    assert payload["completion_confirmed"] is False
    assert payload["user_facing_result_verified"] is False
    assert payload["human_summary"] == (
        "The child result was explicitly marked unverified. Completion is "
        "not confirmed."
    )
    assert "instruction_following" not in payload
    receipt = payload["child_result_promotion_receipt"]
    assert receipt["reader_ready"] is False
    assert receipt["typed_display_verified"] is False
    assert receipt["verification_basis"] == []
    assert payload["script_payload"]["user_facing_result_verified"] is False


@pytest.mark.parametrize(
    "request_text",
    [
        "Summarize this synthetic newsletter and include its Gmail link.",
        (
            "Summarize this synthetic newsletter in exactly two sentences and "
            "include its Gmail link."
        ),
    ],
)
def test_verified_child_remains_promotable_without_formatting_metadata(
    monkeypatch,
    capsys,
    tmp_path,
    request_text,
):
    child = _verification_child_payload(verified=True)
    monkeypatch.setattr(
        cli,
        "run_isolated_child_process",
        lambda *_a, **_k: SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(child),
        ),
    )
    original = cli.resolve_instruction_following_response
    monkeypatch.setattr(
        cli,
        "resolve_instruction_following_response",
        lambda text, **kwargs: original(text, **{**kwargs, "live": False}),
    )

    code = cli._run_ask_script_live(
        "gmail_triage",
        request_text,
        ["unused-child-command"],
        json_output=True,
        manual_plan=_verification_plan("omitted"),
        database_url=f"sqlite:///{tmp_path / 'verified.db'}",
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["public_result"]["status"] == "completed"
    assert payload["completion_confirmed"] is True
    assert payload["user_facing_result_verified"] is True
    assert payload["human_summary"] == child["human_summary"]
    receipt = payload["child_result_promotion_receipt"]
    assert receipt["reader_ready"] is True
    assert receipt["child_public_result_verified"] is True


@pytest.mark.parametrize(
    ("child_summary", "expected_status", "expected_reader_ready"),
    [
        (
            "- Alpha is supported.\n- Beta is supported.",
            "completed",
            True,
        ),
        (
            "- Alpha is supported.\n- Beta is supported.\n- Gamma is extra.",
            "blocked",
            False,
        ),
    ],
)
def test_parent_recovers_raw_exact_item_contract_when_planner_bounds_are_missing(
    monkeypatch,
    capsys,
    tmp_path,
    child_summary,
    expected_status,
    expected_reader_ready,
):
    request_text = "Summarize the supplied findings in exactly two bullet points."
    plan = ManualRequestPlan(
        source="llm",
        target_agent="gmail_triage",
        intent="context_lookup",
        primary_target="Synthetic findings",
        ask_shape=AskShapePolicy(
            output_constraints=InterpretedOutputConstraints(
                scope="entire_response",
                item_count_mode="exact",
            ),
        ),
    )
    monkeypatch.setattr(
        cli,
        "run_isolated_child_process",
        lambda *_a, **_k: SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                {
                    "status": "done",
                    "human_summary": child_summary,
                    "user_facing_result_verified": True,
                    "send_enabled": False,
                    "output_type": "SyntheticResult",
                    "public_result": {
                        "status": "completed",
                        "completion_confirmed": True,
                        "provider_write_attempted": False,
                    },
                    "output": {"summary": child_summary},
                }
            ),
        ),
    )
    original = cli.resolve_instruction_following_response
    monkeypatch.setattr(
        cli,
        "resolve_instruction_following_response",
        lambda text, **kwargs: original(text, **{**kwargs, "live": False}),
    )

    code = cli._run_ask_script_live(
        "gmail_triage",
        request_text,
        ["unused-child-command"],
        json_output=True,
        manual_plan=plan,
        database_url=f"sqlite:///{tmp_path / 'raw-item-contract.db'}",
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["public_result"]["status"] == expected_status
    assert payload["child_result_promotion_receipt"]["reader_ready"] is (
        expected_reader_ready
    )
    assert payload["instruction_following"]["repair_attempted"] is False
    assert "item_count_metadata_recovered_from_raw_request" in (
        payload["instruction_following"]["constraint_admission_warnings"]
    )
