"""Validate observations owned by the tool-free supplied Opportunity phase."""

from __future__ import annotations

from typing import Any

from keystone_agents.schemas.opportunity import OpportunityScoutResult


def supplied_provenance_issue(output: OpportunityScoutResult) -> tuple[str, str] | None:
    """Reject invented quality/retrieval telemetry without changing semantic output.

    This phase acquires no numeric source-quality measurements. Its WorkItemSourceRef
    source_quality strings are descriptive labels, not typed scoring evidence. Prior
    model-artifact numbers do not authorize new SourceQualityScore/Summary objects.
    Model-owned claim confidence and deterministic score normalization are separate.
    """
    missing: list[str] = []

    def expect(path: str, expectation: str) -> None:
        if len(missing) < 24:
            missing.append(f"{path} expected {expectation}")

    def inspect_quality(value: Any, path: str) -> None:
        if getattr(value, "source_quality_summary", None) is not None:
            expect(f"{path}.source_quality_summary", "null")
        for index, source in enumerate(getattr(value, "sources", ())):
            if source.source_quality is not None:
                expect(f"{path}.sources[{index}].source_quality", "null")
        for index, bundle in enumerate(getattr(value, "source_bundles", ())):
            inspect_quality(bundle, f"{path}.source_bundles[{index}]")

    for field in ("raw_search_result_count", "deduped_candidate_count"):
        if getattr(output, field) != 0:
            expect(field, "0")
    for field in ("search_queries", "search_lanes", "search_time_windows"):
        if getattr(output, field):
            expect(field, "[]")
    inspect_quality(output, "output")
    for index, record in enumerate(output.records):
        inspect_quality(record, f"records[{index}]")
        for field in ("search_lanes", "search_time_windows"):
            if getattr(record, field):
                expect(f"records[{index}].{field}", "[]")
    if not missing:
        return None
    paths: list[str] = []
    for path in missing:
        if len("; ".join([*paths, path])) > 550:
            break
        paths.append(path)
    return (
        "supplied_provenance_unobserved",
        "This supplied-evidence phase performed no retrieval or numeric source-quality "
        "acquisition. Descriptive source labels and prior model scores are not measurements. "
        "Correct these fields without inventing observations: " + "; ".join(paths)
        + ". Keep model-owned claims/selection and source scope grounded; the existing "
        "normalizer still owns priority/consulting score arithmetic.",
    )
