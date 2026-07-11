from pathlib import Path


def test_anu223_live_plan_is_serial_bounded_and_reuses_existing_proof() -> None:
    text = " ".join(
        Path("docs/ANU223_LIVE_DELEGATION_PLAN.md").read_text(encoding="utf-8").split()
    )

    assert "Calendar and Gmail already have joined" in text
    assert "Do not rerun them" in text
    assert "Airtable structural base/table creation is out of scope" in text
    assert "18 OpenAI requests" in text
    assert "$0.32" in text
    assert "one provider family at a time" in text
    assert "Stop after the first" in text
    assert "no duplicate approval loop" in text
    assert "zero or ambiguous target matches" in text


def test_anu223_live_plan_requires_identity_readback_cleanup_and_cost_evidence() -> None:
    text = " ".join(
        Path("docs/ANU223_LIVE_DELEGATION_PLAN.md").read_text(encoding="utf-8").split()
    )

    for marker in (
        "Raw natural-request hash",
        "Provider-safe identity retained internally",
        "Create/read-back",
        "cleanup",
        "absence/trash verification",
        "Request count",
        "token/cost evidence",
        "retry count",
        "no-unintended-write",
    ):
        assert marker in text
