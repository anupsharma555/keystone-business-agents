"""Historical post evidence stays bounded, scoped and distinct from article reads."""

import json
from copy import deepcopy

import pytest

from keystone_agents.runtime.channel_answer_evidence import completed_channel_answer_evidence
from keystone_agents.tools.slack_channel_context_tools import SCHEMA_VERSION


def _state():
    snapshot = {
        "schema_version": SCHEMA_VERSION, "channel_id": "C123TEST",
        "request_ts": "1794700000.000000", "oldest_ts": "1794095200.000000",
    }
    return {
        "channel_context_capability": snapshot,
        "channel_context_evidence": [{
            "ok": True, "operation": "read", "snapshot": deepcopy(snapshot),
            "messages": [{
                "candidate_id": "historical-post", "channel_id": "C123TEST",
                "message_ts": "1794500000.000000", "thread_ts": "1794500000.000000",
                "excerpt": "A saved preview describes mutation testing.",
                "source_links": [{"ordinal": 2, "url": "https://example.org/testing"}],
                "source_kind": "slack_message", "role": "bot",
                "authority": "historical_context_only", "content_kind": "message",
                "text_truncated": False, "revision_ts": "",
            }],
            "coverage": {"truncated": True, "pages": 1},
        }],
    }


def test_completed_read_preserves_identity_limits_and_visible_source_membership():
    state = _state()
    evidence = completed_channel_answer_evidence(state)
    assert evidence is not None
    assert evidence.model_context["messages"] == state["channel_context_evidence"][0]["messages"]
    assert evidence.model_context["coverage"][0]["truncated"] is True
    assert any("unread" in limit for limit in evidence.model_context["limitations"])
    assert evidence.validates_answer("The post reports testing. https://example.org/testing")
    assert not evidence.validates_answer("The post reports testing.")
    assert not evidence.validates_answer("Unseen source https://example.org/other")


@pytest.mark.parametrize("invalid", ["query", "failed", "snapshot", "channel", "authority"])
def test_unread_failed_stale_or_out_of_scope_messages_cannot_support_an_answer(invalid):
    state = _state()
    receipt = state["channel_context_evidence"][0]
    if invalid == "query":
        receipt["operation"] = "query"
    elif invalid == "failed":
        receipt["ok"] = False
    elif invalid == "snapshot":
        receipt["snapshot"]["request_ts"] = "1794700001.000000"
    elif invalid == "channel":
        receipt["messages"][0]["channel_id"] = "C999OTHER"
    else:
        receipt["messages"][0]["authority"] = "instructions"
    assert completed_channel_answer_evidence(state) is None


def test_projection_budget_never_promotes_links_from_an_omitted_message():
    state = _state()
    large = deepcopy(state["channel_context_evidence"][0]["messages"][0])
    large.update(candidate_id="large-post", excerpt="X" * 24000,
                 source_links=[{"ordinal": 1, "url": "https://example.org/omitted"}])
    state["channel_context_evidence"][0]["messages"].append(large)
    evidence = completed_channel_answer_evidence(state)
    assert evidence is not None and evidence.model_context["projection_truncated"]
    assert len(evidence.model_context["messages"]) == 1
    assert not evidence.validates_answer("https://example.org/omitted")


@pytest.mark.parametrize("answer,reason,visible_count,unsupported_count", [
    ("The historical post describes testing.", "visible_source_missing", 0, 0),
    ("Unseen source https://example.org/unknown", "source_not_returned", 1, 1),
    ("https://example.org/testing and https://example.org/unknown", "source_not_returned", 2, 1),
    ("The historical post: https://example.org/testing", "passed", 1, 0),
    ("[Post](https://example.org/testing) repeats https://example.org/testing", "passed", 1, 0),
])
def test_content_free_verdict_distinguishes_missing_unknown_and_valid_citations(
    answer, reason, visible_count, unsupported_count,
):
    evidence = completed_channel_answer_evidence(_state())
    verdict = evidence.answer_verdict(answer)
    assert verdict.reason == reason
    assert verdict.passed is (reason == "passed")
    assert verdict.allowed_url_count == 1
    assert verdict.visible_url_count == visible_count
    assert verdict.unsupported_url_count == unsupported_count
    assert evidence.validates_answer(answer) is verdict.passed
    assert verdict.telemetry() == {
        "passed": reason == "passed", "reason": reason, "allowed_url_count": 1,
        "visible_url_count": visible_count, "unsupported_url_count": unsupported_count,
    }


@pytest.mark.parametrize("answer,passed", [
    ("Only the historical message text is available.", True),
    ("Invented reference https://example.org/not-in-the-message", False),
])
def test_messages_without_links_do_not_require_invented_citations(answer, passed):
    state = _state()
    state["channel_context_evidence"][0]["messages"][0]["source_links"] = []
    evidence = completed_channel_answer_evidence(state)
    verdict = evidence.answer_verdict(answer)
    assert verdict.allowed_url_count == 0
    assert verdict.passed is passed
    assert verdict.unsupported_url_count == (0 if passed else 1)


def test_repair_feedback_retains_no_content_and_does_not_choose_a_source():
    state = _state()
    evidence = completed_channel_answer_evidence(state)
    original_context = deepcopy(evidence.model_context)
    original_answer = "Private discarded candidate with https://example.org/private-unknown"
    verdict = evidence.answer_verdict(original_answer)
    feedback = verdict.repair_feedback()
    serialized = json.dumps(feedback)
    assert not feedback["passed"]
    assert feedback["repairable"]
    assert feedback["provider_reads_allowed"] is False
    assert feedback["source_selection_owner"] == "model"
    assert "Do not fetch, search, or acquire new context" in feedback["instruction"]
    assert "validator has not selected one" in feedback["instruction"]
    assert "https://" not in serialized
    assert "historical-post" not in serialized
    assert "Private discarded candidate" not in serialized
    assert "mutation testing" not in serialized
    assert evidence.model_context == original_context
    assert evidence.answer_verdict("https://example.org/testing").passed


def test_passed_verdict_does_not_request_another_model_attempt():
    evidence = completed_channel_answer_evidence(_state())
    feedback = evidence.answer_verdict("https://example.org/testing").repair_feedback()
    assert feedback["passed"]
    assert feedback["repairable"] is False
    assert feedback["instruction"] == ""
