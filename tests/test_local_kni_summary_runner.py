from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from keystone_agents.local_kni_summary_runner import (
    local_kni_summary_privacy_preview,
    run_local_kni_capability_summary,
)
from keystone_agents.privacy_minimized_synthesis import (
    build_concept_signal_packet,
    sanitized_source_id,
)
from keystone_agents.schemas.chief_of_staff import ChiefOfStaffResult, ChiefOfStaffSourceRef
from keystone_agents.sdk import active_sdk_data_handling_profile


def _packet() -> dict[str, object]:
    return {
        "local_only": True,
        "send_enabled": False,
        "retrieval_diagnostics": {"candidate_selection_valid": True},
        "candidate_documents": [
            {
                "relative_path": "07_Marketing/Capability_Statement.md",
                "content_excerpt": "KNI service areas include clinical AI evaluation.",
                "model_context_allowed": True,
                "review_required": False,
            },
            {
                "relative_path": "05_Clients/Proposal.docx",
                "content_excerpt": "A client proposal with research and advisory services.",
                "model_context_allowed": True,
                "review_required": True,
            },
        ],
    }


def _result(*, retries: int = 0, include_source: bool = True):
    packet = build_concept_signal_packet(
        workflow="capability_summary",
        sources={
            str(item["relative_path"]): str(item["content_excerpt"])
            for item in _packet()["candidate_documents"]
        },
        taxonomy={"clinical_ai_evaluation": ("clinical ai evaluation",)},
        constraints=("human_review_required",),
    )
    source = sanitized_source_id(packet.source_hashes[0])
    output = ChiefOfStaffResult(
        mode="llm",
        summary=(
            "The current capability material describes clinical AI evaluation, research, "
            "data strategy, and advisory support, subject to human review."
        ),
        sources=[ChiefOfStaffSourceRef(title=source, source_type="local_kni_document")]
        if include_source
        else [],
    )
    return SimpleNamespace(
        output=output,
        usage={"requests": 1, "input_tokens": 100, "output_tokens": 30},
        cost={"estimated_usd": 0.02},
        request_cache={"rate_limit_retries": retries},
    )


def test_offline_plan_is_sanitized_and_ready(monkeypatch) -> None:
    monkeypatch.setattr(
        "keystone_agents.local_kni_summary_runner.build_local_kni_evidence_packet_for_query",
        lambda _query: _packet(),
    )

    result = run_local_kni_capability_summary("summarize local KNI capabilities")

    assert result["status"] == "validated_offline"
    assert result["plan"]["synthesis_ready"] is True
    assert result["plan"]["candidate_count"] == 2
    assert "Capability_Statement" not in str(result)
    assert "clinical AI evaluation" not in str(result)


def test_privacy_preview_materializes_exact_safe_model_packet(monkeypatch) -> None:
    monkeypatch.setattr(
        "keystone_agents.local_kni_summary_runner.build_local_kni_evidence_packet_for_query",
        lambda _query: _packet(),
    )

    result = local_kni_summary_privacy_preview("summarize local KNI capabilities")

    assert result["status"] == "privacy_minimized_preview"
    assert result["openai_requests_made"] == 0
    assert result["provider_writes"] == 0
    assert result["local_source_mapping_verified"] is True
    assert result["bundle"]["proof_scope"] == "sanitized_context_proof"
    assert result["bundle"]["transmission_contract"] == {
        "raw_text": False,
        "personal_identifiers": False,
        "provider_identifiers": False,
        "file_paths": False,
        "urls": False,
        "exact_financial_values": False,
        "secrets": False,
        "phi": False,
    }
    rendered = str(result)
    assert "Capability_Statement" not in rendered
    assert "Proposal.docx" not in rendered
    assert "clinical AI evaluation" not in rendered
    assert "client proposal" not in rendered


def test_live_summary_uses_one_no_tool_turn_and_returns_sanitized_receipt(monkeypatch) -> None:
    monkeypatch.setattr(
        "keystone_agents.local_kni_summary_runner.build_local_kni_evidence_packet_for_query",
        lambda _query: _packet(),
    )
    captured = {}

    def fake_runner(sdk_input, **kwargs):
        captured["sdk_input"] = sdk_input
        captured["kwargs"] = kwargs
        profile = active_sdk_data_handling_profile()
        captured["data_handling"] = profile.audit_metadata() if profile else None
        return _result()

    result = run_local_kni_capability_summary(
        "summarize local KNI capabilities",
        live_sdk=True,
        approved_privacy_minimized_context=True,
        runner=fake_runner,
    )

    assert result["status"] == "success"
    assert result["usage"]["requests"] == 1
    assert result["used_source_count"] == 1
    assert result["local_only"] is True
    assert result["send_enabled"] is False
    assert result["summary_storage"] == "local_artifact_only"
    assert "clinical AI evaluation" in result["summary"]
    assert "Capability_Statement" not in str(result)
    assert captured["kwargs"]["attach_tools"] is False
    assert captured["kwargs"]["include_specialist_tools"] is False
    assert captured["kwargs"]["quality_budget"].max_turns == 1
    assert captured["kwargs"]["quality_budget"].max_tool_calls == 0
    outbound = captured["sdk_input"]["privacy_minimized_context"]
    assert outbound["proof_scope"] == "sanitized_context_proof"
    assert outbound["facts"][0]["concept"] == "clinical_ai_evaluation"
    assert "content_excerpt" not in str(outbound)
    assert captured["data_handling"] == {
        "name": "bounded_private_context",
        "response_store": False,
        "prompt_cache_retention": "in_memory",
        "tracing_disabled": True,
        "trace_include_sensitive_data": False,
    }
    assert result["data_handling"] == captured["data_handling"]


def test_live_summary_requires_private_context_approval(monkeypatch) -> None:
    monkeypatch.setattr(
        "keystone_agents.local_kni_summary_runner.build_local_kni_evidence_packet_for_query",
        lambda _query: _packet(),
    )

    with pytest.raises(ValueError, match="explicit approval"):
        run_local_kni_capability_summary(
            "summarize local KNI capabilities",
            live_sdk=True,
        )


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (_result(retries=1), "unexpected retry"),
        (_result(include_source=False), "sanitized source ID"),
    ],
)
def test_live_summary_rejects_missing_evidence(monkeypatch, result, message) -> None:
    monkeypatch.setattr(
        "keystone_agents.local_kni_summary_runner.build_local_kni_evidence_packet_for_query",
        lambda _query: _packet(),
    )

    with pytest.raises(RuntimeError, match=message):
        run_local_kni_capability_summary(
            "summarize local KNI capabilities",
            live_sdk=True,
            approved_privacy_minimized_context=True,
            runner=lambda *_args, **_kwargs: result,
        )


def test_live_summary_rejects_broader_limits(monkeypatch) -> None:
    monkeypatch.setattr(
        "keystone_agents.local_kni_summary_runner.build_local_kni_evidence_packet_for_query",
        lambda _query: _packet(),
    )

    with pytest.raises(ValueError, match="max_openai_requests=1"):
        run_local_kni_capability_summary(
            "summarize local KNI capabilities",
            max_openai_requests=2,
        )
    with pytest.raises(ValueError, match="cost ceiling"):
        run_local_kni_capability_summary(
            "summarize local KNI capabilities",
            max_cost_usd=Decimal("0.06"),
        )


def test_offline_plan_rejects_semantically_invalid_candidate_selection(monkeypatch) -> None:
    packet = _packet()
    packet["retrieval_diagnostics"] = {"candidate_selection_valid": False}
    monkeypatch.setattr(
        "keystone_agents.local_kni_summary_runner.build_local_kni_evidence_packet_for_query",
        lambda _query: packet,
    )

    with pytest.raises(ValueError, match="not synthesis-ready"):
        run_local_kni_capability_summary("summarize local KNI capabilities")
