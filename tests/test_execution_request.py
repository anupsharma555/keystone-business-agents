from __future__ import annotations

from keystone_agents.execution_request import (
    attach_execution_public_result,
    build_execution_request,
    execution_request_planning_text,
)


def test_equivalent_cli_and_slack_root_asks_share_semantic_input() -> None:
    ask = "CoS summarize this supplied note in three bullets. Do not search the web."

    cli_request = build_execution_request(ask)
    slack_request = build_execution_request(ask, slack_context_input=True)

    assert cli_request.entrypoint == "cli"
    assert slack_request.entrypoint == "slack_root"
    assert cli_request.current_request == slack_request.current_request
    assert cli_request.requested_agent == slack_request.requested_agent == "chief_of_staff"
    assert cli_request.requested_agent_explicit is True
    assert slack_request.requested_agent_explicit is True


def test_slack_followup_keeps_current_request_authoritative_and_prior_state_bounded() -> None:
    envelope = "\n".join(
        [
            "chief of staff continue this prior Slack thread.",
            "Current user request (authoritative): Make that three bullets.",
            "Linked WorkItem: wi_example",
            "Provider affinity: calendar",
            "Previous request: Summarize the supplied note.",
            "Previous result title: Business Agents Chief of Staff",
            "Previous result: A longer summary.",
            "User follow-up: Make that three bullets.",
            "Continue the same agent task.",
        ]
    )

    request = build_execution_request(envelope)

    assert request.entrypoint == "slack_followup"
    assert request.current_request == "Make that three bullets."
    assert request.requested_agent == "chief_of_staff"
    assert request.continuation.work_item_id == "wi_example"
    assert request.continuation.provider_affinity == "calendar"
    assert request.continuation.prior_request == "Summarize the supplied note."
    assert request.continuation.prior_result_title == "Business Agents Chief of Staff"
    assert request.continuation.prior_result_summary == "A longer summary."
    assert "Previous result" not in request.current_request


def test_real_slack_continuation_instruction_is_not_part_of_operator_request() -> None:
    envelope = "\n".join(
        [
            "business agents continue this prior Slack thread.",
            (
                "Previous request: CoS return exactly three bullets from supplied "
                "facts. *Sent using*"
            ),
            "Previous result: A wrong research-oriented answer.",
            (
                "User follow-up: CoS, fix the last reply and return only the same "
                "three bullets. *Sent using*"
            ),
            (
                "Continue the same agent task, treating the current user request as "
                "authoritative. Return only the requested operator-facing answer."
            ),
        ]
    )

    request = build_execution_request(envelope)

    assert request.current_request == (
        "fix the last reply and return only the same three bullets."
    )
    assert request.requested_agent == "chief_of_staff"
    assert "Continue the same agent task" not in request.current_request
    assert request.continuation.prior_request == (
        "CoS return exactly three bullets from supplied facts."
    )
    assert "Sent using" not in request.continuation.prior_request


def test_slack_followup_planning_text_keeps_prior_context_and_latest_ask_last() -> None:
    request = build_execution_request(
        "\n".join(
            [
                "chief of staff continue this prior Slack thread.",
                "Previous request: Using only these facts, return three bullets: A; B; C.",
                "Previous result: A longer three-bullet answer.",
                "User follow-up: Keep only the three bullets with no note after them.",
                "Continue the same agent task.",
            ]
        )
    )

    planning_text = execution_request_planning_text(request)

    assert planning_text.startswith(
        "Using only these facts, return three bullets: A; B; C."
    )
    assert "Prior result for context: A longer three-bullet answer." in planning_text
    assert planning_text.endswith(
        "Authoritative follow-up: Keep only the three bullets with no note after them."
    )


def test_provider_followup_planning_text_drops_prior_failed_bot_prose() -> None:
    request = build_execution_request(
        "\n".join(
            [
                "chief of staff continue this prior Slack thread.",
                "Provider affinity: calendar",
                (
                    "Previous request: CoS add UT Austin Course Starts on August 15, "
                    "2026 to my Google Calendar."
                ),
                "Previous result title: Business Agents WorkItem Failed",
                "Previous result: No specialist or provider action ran.",
                "User follow-up: Is it on the calendar now?",
                "Continue the same agent task.",
            ]
        )
    )

    planning_text = execution_request_planning_text(request)

    assert planning_text.startswith(
        "CoS add UT Austin Course Starts on August 15, 2026 to my Google Calendar."
    )
    assert planning_text.endswith(
        "Authoritative follow-up: Is it on the calendar now?"
    )
    assert "No specialist or provider action ran" not in planning_text
    assert "WorkItem Failed" not in planning_text


def test_neutral_slack_followup_envelope_does_not_synthesize_prior_agent_authority() -> None:
    cases = [
        ("business_research_analyst", "focus the comparison on 2026 partnerships"),
        ("opportunity_scout", "rank those by evidence quality"),
        ("gmail_triage", "summarize the thread without drafting"),
        ("outreach_composer", "make the draft shorter but do not send it"),
        ("airtable_context_agent", "verify the exact record named above"),
    ]

    for prior_route, current_ask in cases:
        request = build_execution_request(
            "\n".join(
                [
                    "business agents continue this prior Slack thread.",
                    f"Current user request (authoritative): {current_ask}",
                    "Linked WorkItem: wi_context_only",
                    f"Previous request: prior {prior_route} task",
                    "Previous result: prior bounded result",
                    f"User follow-up: {current_ask}",
                    "Continue the same agent task.",
                ]
            )
        )

        assert request.current_request == current_ask
        assert request.requested_agent == ""
        assert request.requested_agent_explicit is False
        assert request.continuation.work_item_id == "wi_context_only"
        assert request.continuation.prior_request == f"prior {prior_route} task"


def test_new_explicit_agent_in_followup_supersedes_prior_owner() -> None:
    envelope = (
        "business research analyst continue this prior Slack thread. "
        "Previous request: Research the company. "
        "Previous result title: Business Agents Company Research Ready "
        "Previous result: Company brief. "
        "User follow-up: CoS turn this into a three-bullet internal decision note. "
        "Continue the same agent task."
    )

    request = build_execution_request(envelope)

    assert request.current_request == (
        "turn this into a three-bullet internal decision note."
    )
    assert request.requested_agent == "chief_of_staff"


def test_missing_optional_continuation_state_does_not_block_normalization() -> None:
    request = build_execution_request(
        "CoS continue this prior Slack thread. User follow-up: Make it shorter."
    )

    assert request.entrypoint == "slack_followup"
    assert request.current_request == "Make it shorter."
    assert request.requested_agent == "chief_of_staff"
    assert request.continuation.work_item_id == ""
    assert request.continuation.prior_result_summary == ""


def test_noninteractive_entrypoints_use_same_contract_without_forcing_backend() -> None:
    ask = "Find the next safe action from these approved facts."

    scheduled = build_execution_request(ask, entrypoint="scheduled")
    direct_sdk = build_execution_request(ask, entrypoint="direct_sdk")
    work_item = build_execution_request(ask, entrypoint="work_item")

    assert {
        scheduled.current_request,
        direct_sdk.current_request,
        work_item.current_request,
    } == {ask}
    assert {
        scheduled.requested_agent,
        direct_sdk.requested_agent,
        work_item.requested_agent,
    } == {""}


def test_recovered_result_keeps_answer_first_and_posts_concise_notice() -> None:
    payload = {
        "selected_agent": "chief_of_staff",
        "human_summary": "- First point.\n- Second point.\n- Third point.",
        "output": {
            "summary": "- First point.\n- Second point.\n- Third point.",
            "audit_notes": [
                (
                    "Live SDK structured output could not be parsed or validated; "
                    "deterministic Chief of Staff fallback was rendered instead "
                    "(ModelBehaviorError)."
                ),
                "Live SDK validation diagnostic: raw internal parser detail",
            ],
        },
    }

    result = attach_execution_public_result(payload)

    assert result.status == "recovered"
    assert result.completion_confirmed is True
    assert result.recovery_used is True
    assert result.text.startswith("- First point.")
    assert "raw internal parser detail" not in result.recovery_notice
    assert payload["human_summary"].startswith("- First point.")
    assert "safe fallback" in str(payload["human_summary"])


def test_unverified_provider_write_cannot_claim_completion() -> None:
    payload = {
        "status": "done",
        "human_summary": "Calendar event created.",
        "tool_receipt": {
            "operation": "create",
            "verification": {"passed": False},
        },
    }

    result = attach_execution_public_result(payload)

    assert result.status == "blocked"
    assert result.completion_confirmed is False
    assert result.provider_write_attempted is True
    assert result.provider_receipt_verified is False
    assert payload["completion_confirmed"] is False


def test_verified_write_receipt_list_confirms_provider_completion() -> None:
    payload = {
        "status": "done",
        "human_summary": "The exact record was updated and verified.",
        "tool_receipts": [
            {
                "operation": "update",
                "verification": {"passed": True, "record_id_match": True},
            }
        ],
        "side_effects": {"external_write_performed": True},
    }

    result = attach_execution_public_result(payload)

    assert result.status == "completed"
    assert result.completion_confirmed is True
    assert result.provider_write_attempted is True
    assert result.provider_receipt_verified is True


def test_nested_child_write_without_receipt_verification_cannot_complete() -> None:
    payload = {
        "status": "done",
        "human_summary": "The document write completed.",
        "script_payload": {
            "tool_receipts": [
                {
                    "operation": "write_doc",
                    "verification": {"passed": False},
                }
            ]
        },
    }

    result = attach_execution_public_result(payload)

    assert result.status == "blocked"
    assert result.completion_confirmed is False
    assert result.provider_write_attempted is True
    assert result.provider_receipt_verified is False


def test_failed_entrypoint_uses_same_public_failure_contract() -> None:
    payload = {
        "status": "failed",
        "output": {
            "summary": "Chief could not produce a valid structured result.",
            "error_type": "structured_output_invalid",
        },
    }

    result = attach_execution_public_result(payload)

    assert result.status == "failed"
    assert result.completion_confirmed is False
    assert result.failure_code == "structured_output_invalid"
    assert result.failure_summary == "Chief could not produce a valid structured result."
    assert payload["slack_display_title"] == "Business Agents Run Failed"


def test_generated_clarification_prose_cannot_claim_completion() -> None:
    payload = {
        "status": "done",
        "completion_confirmed": True,
        "human_summary": (
            "*Answer:*\nNeed clarification before selecting a Slack operations workflow.\n\n"
            "*Detailed Summary:*\nReference material was reviewed."
        ),
    }

    result = attach_execution_public_result(payload)

    assert result.status == "needs_input"
    assert result.completion_confirmed is False
    assert payload["completion_confirmed"] is False
    assert payload["slack_display_title"] == "Business Agents Need Input"


def test_llm_plan_prevents_cautious_answer_wording_from_becoming_blocker() -> None:
    payload = {
        "status": "done",
        "completion_confirmed": True,
        "human_summary": (
            "There is not enough evidence to confirm the broader claim, but the "
            "provider record confirms the requested date."
        ),
        "manual_request_plan": {
            "source": "llm",
            "target_agent": "chief_of_staff",
            "intent": "context_lookup",
            "missing_required_information": [],
        },
    }

    result = attach_execution_public_result(payload)

    assert result.status == "completed"
    assert result.completion_confirmed is True
    assert payload["slack_display_title"] == "Business Agents Result Ready"


def test_advisory_clarification_metadata_cannot_block_complete_direct_answer() -> None:
    payload = {
        "mode": "live_sdk",
        "human_summary": "- Constraint pruning works.\n- Live validation still remains.",
        "missing_information": [],
        "output": {
            "summary": "- Constraint pruning works.\n- Live validation still remains.",
            "recommended_route": {"workflow_type": "clarification"},
            "approval_required": True,
            "human_review_required": True,
            "send_enabled": False,
        },
    }

    result = attach_execution_public_result(payload)

    assert result.status == "completed"
    assert result.completion_confirmed is True
    assert result.title == "Business Agents Result Ready"
    assert result.failure_summary == ""


def test_exact_item_contract_omits_decorative_title_for_renderers() -> None:
    payload = {
        "mode": "live_sdk",
        "human_summary": "- What changed.\n- What still needs proof.",
        "manual_request_plan": {
            "ask_shape": {
                "output_constraints": {
                    "scope": "entire_response",
                    "item_count_mode": "exact",
                    "minimum_items": 2,
                    "maximum_items": 2,
                    "required_sections": [],
                }
            }
        },
    }

    result = attach_execution_public_result(payload)

    assert result.status == "completed"
    assert result.omit_title is True
    assert payload["public_result"]["omit_title"] is True
    assert result.title == "Business Agents Result Ready"


def test_exact_item_contract_keeps_requested_title_section() -> None:
    payload = {
        "mode": "live_sdk",
        "human_summary": "Decision\n- First point.\n- Second point.",
        "manual_request_plan": {
            "ask_shape": {
                "output_constraints": {
                    "scope": "entire_response",
                    "item_count_mode": "exact",
                    "minimum_items": 2,
                    "maximum_items": 2,
                    "required_sections": ["Title"],
                }
            }
        },
    }

    result = attach_execution_public_result(payload)

    assert result.status == "completed"
    assert result.omit_title is False


def test_provider_free_direct_context_answer_omits_generic_status_title() -> None:
    payload = {
        "mode": "live_sdk",
        "human_summary": (
            "Supported fact: Northstar Care sells referral-navigation software. "
            "First validation question: Is there audited outcomes evidence?"
        ),
        "manual_request_plan": {
            "intent": "route_request",
            "workflow": [],
            "requires_live_search": False,
            "requires_approved_context": False,
            "side_effect_policy": "draft_or_read_only",
            "ask_shape": {
                "prior_context_dependency": "selected_context",
                "output_constraints": {
                    "scope": "unspecified",
                    "required_sections": [],
                },
            },
        },
    }

    result = attach_execution_public_result(payload)

    assert result.status == "completed"
    assert result.omit_title is True
    assert result.title == "Business Agents Result Ready"


def test_canonical_internal_slack_artifact_omits_generic_status_title() -> None:
    payload = {
        "status": "done",
        "human_summary": (
            "*Decision brief:*\nBounded assessment.\n\n"
            "*Paste-ready internal Slack note:*\nReview this evidence gap first."
        ),
        "output": {
            "artifact_refs": [
                {
                    "artifact_type": "outreach_draft",
                    "metadata": {
                        "internal_slack_copy": True,
                        "external_write_performed": False,
                    },
                }
            ]
        },
    }

    result = attach_execution_public_result(payload)

    assert result.status == "completed"
    assert result.omit_title is True
    assert result.title == "Business Agents Result Ready"


def test_failure_title_is_never_suppressed_by_response_count_contract() -> None:
    payload = {
        "status": "failed",
        "human_summary": "The run failed.",
        "manual_request_plan": {
            "ask_shape": {
                "output_constraints": {
                    "scope": "entire_response",
                    "item_count_mode": "exact",
                    "minimum_items": 2,
                    "maximum_items": 2,
                }
            }
        },
    }

    result = attach_execution_public_result(payload)

    assert result.status == "failed"
    assert result.omit_title is False
    assert result.title == "Business Agents Run Failed"


def test_clarification_route_with_missing_information_still_needs_input() -> None:
    payload = {
        "mode": "live_sdk",
        "human_summary": "Please provide the exact document title.",
        "missing_information": ["exact document title"],
        "output": {
            "summary": "Please provide the exact document title.",
            "recommended_route": {"workflow_type": "clarification"},
        },
    }

    result = attach_execution_public_result(payload)

    assert result.status == "needs_input"
    assert result.completion_confirmed is False
