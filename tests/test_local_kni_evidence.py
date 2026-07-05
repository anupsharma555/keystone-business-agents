from __future__ import annotations

from pathlib import Path

import pytest

import keystone_agents.local_kni_evidence as local_kni_evidence
from keystone_agents.schemas.chief_of_staff import ChiefOfStaffResult, ChiefOfStaffSourceRef


def _fake_read_payload(relative_path: str, *, content: str = "") -> dict[str, object]:
    return {
        "relative_path": relative_path,
        "title": Path(relative_path).stem,
        "extension": Path(relative_path).suffix.lower().lstrip(".") or "md",
        "sensitivity_status": "allowed",
        "review_required": "Insurance" in relative_path or "Formation" in relative_path,
        "review_reasons": ["insurance_policy"] if "Insurance" in relative_path else [],
        "truncated": False,
        "content": content or f"Evidence from {relative_path}",
        "local_only": True,
        "model_context_allowed": True,
        "send_enabled": False,
    }


def test_local_kni_evidence_lookup_detection_handles_short_insurance_followups() -> None:
    assert local_kni_evidence.looks_like_local_kni_evidence_lookup(
        "who was the broker for the CFC insurance?"
    )
    assert local_kni_evidence.looks_like_local_kni_evidence_lookup(
        "using local KNI documents, what date was Keystone Neuroinformatics LLC formed?"
    )
    assert local_kni_evidence.looks_like_local_kni_evidence_lookup(
        "who formally organized Keystone Neuroinformatics LLC in Pennsylvania?"
    )
    assert local_kni_evidence.looks_like_local_kni_evidence_lookup(
        "which registered agent appears in the Keystone Neuroinformatics filing?"
    )
    assert not local_kni_evidence.looks_like_local_kni_evidence_lookup(
        "summarize the Slack thread follow ups"
    )


def test_local_kni_live_instruction_requires_flexible_latest_question_reasoning() -> None:
    instruction = local_kni_evidence.local_kni_live_instruction()

    assert "prewritten final answer" in instruction
    assert "Re-rank the candidate documents against the latest user question" in instruction
    assert "insurer" in instruction
    assert "broker" in instruction
    assert "producer" in instruction


def test_local_kni_evidence_packet_filters_insurance_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = ChiefOfStaffResult(
        mode="deterministic",
        summary="Prefetch only.",
        sources=[
            ChiefOfStaffSourceRef(
                title="00_Admin/Insurance/InsurancePolicy/COI_Operator_2026.pdf",
                url="",
                source_type="local_kni_document",
            )
        ],
        retrieval_diagnostics={
            "lookup_kind": "insurance",
            "answer_focus": "broker",
            "evidence_path": "00_Admin/Insurance/InsurancePolicy/COI_Operator_2026.pdf",
            "local_only": True,
            "send_enabled": False,
        },
    )

    def fake_search(query: str, *, max_results: int = 8) -> dict[str, object]:
        return {
            "status": "ready",
            "matches": [
                {
                    "relative_path": "06_Archive/2026/Template_Sources_To_Adapt/example.md",
                    "title": "Template",
                    "snippet": "Unrelated template",
                    "sensitivity_status": "allowed",
                    "review_required": False,
                    "review_reasons": [],
                },
                {
                    "relative_path": "00_Admin/Insurance/InsurancePolicy/CFC POLICY stamped 042126.pdf",
                    "title": "CFC policy stamped",
                    "snippet": "Coverholder: CFC Underwriting Limited",
                    "sensitivity_status": "allowed",
                    "review_required": True,
                    "review_reasons": ["insurance_policy"],
                },
                {
                    "relative_path": "00_Admin/Insurance/InsuranceQuote/CFC QUOTE Keystone Neuroinformatics 041626.pdf",
                    "title": "CFC quote",
                    "snippet": "Security: Lloyd's underwriters",
                    "sensitivity_status": "allowed",
                    "review_required": True,
                    "review_reasons": ["insurance_policy"],
                },
            ],
            "blocked_result_count": 0,
            "local_only": True,
            "send_enabled": False,
        }

    def fake_read(relative_path: str, *, max_chars: int = 4_000) -> dict[str, object]:
        return _fake_read_payload(
            relative_path,
            content=(
                "PRODUCER IAO, Inc. DBA ProAssurance Agency"
                if "COI_" in relative_path
                else "CFC insurance evidence"
            ),
        )

    monkeypatch.setattr(local_kni_evidence, "search_kni_documents_impl", fake_search)
    monkeypatch.setattr(local_kni_evidence, "read_kni_document_file_impl", fake_read)

    packet = local_kni_evidence.build_local_kni_evidence_packet(
        output,
        query_text="who was the broker for the CFC insurance?",
    )
    paths = [doc["relative_path"] for doc in packet["candidate_documents"]]

    assert packet["packet_type"] == "bounded_local_kni_document_evidence"
    assert packet["local_only"] is True
    assert packet["send_enabled"] is False
    assert "summary" not in packet
    assert "synthesis" not in packet
    assert paths[0] == "00_Admin/Insurance/InsurancePolicy/COI_Operator_2026.pdf"
    assert set(paths[1:]) == {
        "00_Admin/Insurance/InsurancePolicy/CFC POLICY stamped 042126.pdf",
        "00_Admin/Insurance/InsuranceQuote/CFC QUOTE Keystone Neuroinformatics 041626.pdf",
    }
    assert all("Template_Sources_To_Adapt" not in path for path in paths)
    assert "ProAssurance" in packet["candidate_documents"][0]["content_excerpt"]


def test_local_kni_evidence_packet_broker_focus_beats_prefetch_insurer_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = ChiefOfStaffResult(
        mode="deterministic",
        summary="Prefetch only.",
        sources=[
            ChiefOfStaffSourceRef(
                title="00_Admin/Insurance/InsurancePolicy/CFC POLICY stamped 042126.pdf",
                url="",
                source_type="local_kni_document",
            )
        ],
        retrieval_diagnostics={
            "lookup_kind": "insurance",
            "answer_focus": "broker",
            "evidence_path": "00_Admin/Insurance/InsurancePolicy/CFC POLICY stamped 042126.pdf",
            "local_only": True,
            "send_enabled": False,
        },
    )

    def fake_search(query: str, *, max_results: int = 8) -> dict[str, object]:
        return {
            "status": "ready",
            "matches": [
                {
                    "relative_path": "00_Admin/Insurance/InsurancePolicy/CFC POLICY stamped 042126.pdf",
                    "title": "CFC policy stamped",
                    "snippet": "Coverholder: CFC Underwriting Limited",
                    "sensitivity_status": "allowed",
                    "review_required": True,
                    "review_reasons": ["insurance_policy"],
                },
                {
                    "relative_path": "00_Admin/Insurance/InsurancePolicy/COI_Operator_2026.pdf",
                    "title": "COI Operator 2026",
                    "snippet": "PRODUCER IAO, Inc. DBA ProAssurance Agency",
                    "sensitivity_status": "allowed",
                    "review_required": True,
                    "review_reasons": ["insurance_policy"],
                },
            ],
            "blocked_result_count": 0,
            "local_only": True,
            "send_enabled": False,
        }

    def fake_read(relative_path: str, *, max_chars: int = 4_000) -> dict[str, object]:
        return _fake_read_payload(
            relative_path,
            content=(
                "\n\n  PRODUCER IAO, Inc. DBA ProAssurance Agency"
                if "COI_" in relative_path
                else "Coverholder: CFC Underwriting Limited"
            ),
        )

    monkeypatch.setattr(local_kni_evidence, "search_kni_documents_impl", fake_search)
    monkeypatch.setattr(local_kni_evidence, "read_kni_document_file_impl", fake_read)

    packet = local_kni_evidence.build_local_kni_evidence_packet(
        output,
        query_text="who was the broker for the CFC insurance?",
    )

    assert packet["candidate_documents"][0]["relative_path"].endswith("COI_Operator_2026.pdf")
    assert packet["candidate_documents"][0]["content_excerpt"].startswith("PRODUCER IAO")
    assert packet["retrieval_diagnostics"]["evidence_path"].endswith(
        "CFC POLICY stamped 042126.pdf"
    )


def test_local_kni_evidence_packet_filters_formation_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = ChiefOfStaffResult(
        mode="deterministic",
        summary="Prefetch only.",
        sources=[
            ChiefOfStaffSourceRef(
                title=(
                    "00_Admin/Formation/2-12-26-PA-FormationDocument-"
                    "Keystone Neuroinformatics LLC.pdf"
                ),
                url="",
                source_type="local_kni_document",
            )
        ],
        retrieval_diagnostics={
            "lookup_kind": "formation",
            "evidence_path": (
                "00_Admin/Formation/2-12-26-PA-FormationDocument-"
                "Keystone Neuroinformatics LLC.pdf"
            ),
            "local_only": True,
            "send_enabled": False,
        },
    )

    def fake_search(query: str, *, max_results: int = 8) -> dict[str, object]:
        return {
            "status": "ready",
            "matches": [
                {
                    "relative_path": "00_Admin/Internal_Policies/BoardRoom-Memos/roadmap.md",
                    "title": "Roadmap",
                    "snippet": "Broad KNI context",
                    "sensitivity_status": "allowed",
                    "review_required": False,
                    "review_reasons": [],
                },
                {
                    "relative_path": (
                        "00_Admin/Formation/InitialResolutions-LLC Single Member--"
                        "Keystone Neuroinformatics LLC.pdf"
                    ),
                    "title": "Initial resolutions",
                    "snippet": "Formation support",
                    "sensitivity_status": "allowed",
                    "review_required": True,
                    "review_reasons": ["legal_contract"],
                },
            ],
            "blocked_result_count": 0,
            "local_only": True,
            "send_enabled": False,
        }

    monkeypatch.setattr(local_kni_evidence, "search_kni_documents_impl", fake_search)
    monkeypatch.setattr(
        local_kni_evidence,
        "read_kni_document_file_impl",
        lambda relative_path, *, max_chars=4_000: _fake_read_payload(relative_path),
    )

    packet = local_kni_evidence.build_local_kni_evidence_packet(
        output,
        query_text="what date was Keystone Neuroinformatics LLC formed?",
    )
    paths = [doc["relative_path"] for doc in packet["candidate_documents"]]

    assert paths == [
        "00_Admin/Formation/2-12-26-PA-FormationDocument-Keystone Neuroinformatics LLC.pdf",
        "00_Admin/Formation/InitialResolutions-LLC Single Member--Keystone Neuroinformatics LLC.pdf",
    ]
    assert all("BoardRoom-Memos" not in path for path in paths)


@pytest.mark.parametrize(
    "query_text",
    [
        "who formally organized Keystone Neuroinformatics LLC in Pennsylvania?",
        "which registered agent or signer appears in the Keystone Neuroinformatics filing?",
    ],
)
def test_local_kni_evidence_packet_infers_formation_role_queries_from_raw_text(
    monkeypatch: pytest.MonkeyPatch,
    query_text: str,
) -> None:
    output = ChiefOfStaffResult(
        mode="deterministic",
        summary="Prefetch selected a broad memo, but the packet should re-query.",
        sources=[
            ChiefOfStaffSourceRef(
                title=(
                    "00_Admin/Internal_Policies/BoardRoom-Memos/"
                    "2026-03-29-keystone-neuroinformatics-12-month-positioning.md"
                ),
                url="",
                source_type="local_kni_document",
            )
        ],
        retrieval_diagnostics={
            "lookup_kind": "generic",
            "evidence_path": (
                "00_Admin/Internal_Policies/BoardRoom-Memos/"
                "2026-03-29-keystone-neuroinformatics-12-month-positioning.md"
            ),
            "local_only": True,
            "send_enabled": False,
        },
    )
    search_queries: list[str] = []

    def fake_search(query: str, *, max_results: int = 8) -> dict[str, object]:
        search_queries.append(query)
        return {
            "status": "ready",
            "matches": [
                {
                    "relative_path": (
                        "00_Admin/Internal_Policies/BoardRoom-Memos/"
                        "2026-03-29-keystone-neuroinformatics-12-month-positioning.md"
                    ),
                    "title": "Keystone positioning",
                    "snippet": "Founder profile and Keystone positioning",
                    "sensitivity_status": "allowed",
                    "review_required": False,
                    "review_reasons": [],
                },
                {
                    "relative_path": (
                        "00_Admin/Formation/2-12-26-PA-FormationDocument-"
                        "Keystone Neuroinformatics LLC.pdf"
                    ),
                    "title": "PA FormationDocument Keystone Neuroinformatics LLC",
                    "snippet": (
                        "Certificate of Organization Organizers Registered Agents Inc "
                        "Electronic Signature"
                    ),
                    "sensitivity_status": "allowed",
                    "review_required": False,
                    "review_reasons": [],
                },
            ],
            "blocked_result_count": 0,
            "local_only": True,
            "send_enabled": False,
        }

    def fake_read(relative_path: str, *, max_chars: int = 4_000) -> dict[str, object]:
        return _fake_read_payload(relative_path)

    monkeypatch.setattr(local_kni_evidence, "search_kni_documents_impl", fake_search)
    monkeypatch.setattr(local_kni_evidence, "read_kni_document_file_impl", fake_read)

    packet = local_kni_evidence.build_local_kni_evidence_packet(
        output,
        query_text=query_text,
    )

    paths = [doc["relative_path"] for doc in packet["candidate_documents"]]
    assert paths[0].startswith("00_Admin/Formation/2-12-26-PA-FormationDocument")
    assert all("BoardRoom-Memos" not in path for path in paths)
    assert packet["retrieval_diagnostics"]["effective_lookup_kind"] == "formation"
    assert packet["retrieval_diagnostics"]["effective_answer_focus"] == "filing_role"
    assert any("organizer" in query.lower() for query in search_queries)
