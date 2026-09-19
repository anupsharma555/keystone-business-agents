"""A tool-free decision must not depend on model-invented execution bookkeeping."""

from __future__ import annotations

import pytest
from agents import AgentOutputSchema
from pydantic import ValidationError

from keystone_agents.schemas.opportunity import (
    OpportunityScoutResult,
    SuppliedOpportunityRecord,
    SuppliedOpportunityResult,
)

FIELDS = (
    "search_provider",
    "search_queries",
    "search_lanes",
    "search_time_windows",
    "raw_search_result_count",
    "deduped_candidate_count",
)


def test_actual_sdk_schema_omits_only_host_owned_search_fields():
    schema = AgentOutputSchema(SuppliedOpportunityResult).json_schema()
    assert all(field not in schema["properties"] for field in FIELDS)
    record = schema["$defs"]["SuppliedOpportunityRecord"]["properties"]
    assert "search_lanes" not in record and "search_time_windows" not in record
    assert {
        "opportunity_status",
        "opportunity_type",
        "sources",
        "claims",
        "source_signals",
    } <= record.keys()
    assert {"human_summary", "records", "decision"} <= schema["properties"].keys()
    generic = AgentOutputSchema(OpportunityScoutResult).json_schema()
    assert all(field in generic["properties"] for field in FIELDS)


def test_host_defaults_round_trip_through_canonical_result_without_inventing_search():
    supplied = SuppliedOpportunityResult.model_validate(
        {
            "human_summary": "The supplied announcement does not invite proposals.",
            "records": [],
        }
    )
    canonical = OpportunityScoutResult.model_validate_json(supplied.model_dump_json())
    assert canonical.human_summary == supplied.human_summary
    assert canonical.search_provider == "source-provided"
    assert canonical.raw_search_result_count == canonical.deduped_candidate_count == 0
    assert canonical.search_queries == canonical.search_lanes == canonical.search_time_windows == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("search_queries", ["invented query"]),
        ("search_lanes", ["source-provided"]),
        ("search_time_windows", ["current"]),
        ("raw_search_result_count", 1),
        ("deduped_candidate_count", 4),
        ("search_provider", "invented provider"),
    ],
)
def test_explicit_unobserved_bookkeeping_is_rejected_not_silently_erased(field, value):
    with pytest.raises(ValidationError, match="Tool-free assessment"):
        SuppliedOpportunityResult.model_validate({field: value})


@pytest.mark.parametrize("field", ["search_lanes", "search_time_windows"])
def test_explicit_record_bookkeeping_is_rejected(field):
    # Validate the actual field even when other required record data is missing.
    with pytest.raises(ValidationError) as exc:
        SuppliedOpportunityRecord.model_validate({field: ["unobserved"]})
    assert any(
        e["loc"] == (field,) and "Tool-free assessment" in e["msg"] for e in exc.value.errors()
    )
