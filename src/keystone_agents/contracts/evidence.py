"""Provider-neutral evidence-gap receipts for bounded research repair."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ResearchEvidenceGapReceipt(BaseModel):
    """Typed explanation of why one target packet is not yet usable."""

    model_config = ConfigDict(frozen=True)

    schema_name: str = "keystone.research_evidence_gap.v1"
    target_name: str
    anchor: bool = False
    missing_official_source: bool = False
    missing_dimensions: tuple[str, ...] = ()
    extraction_gap: bool = False
    retained_source_count: int = 0
    result_bearing_query_count: int = 0
    recommended_query_terms: tuple[str, ...] = ()


def compile_research_evidence_gap_receipt(
    *,
    target_name: str,
    anchor: bool,
    gaps: list[str] | tuple[str, ...],
    missing_dimensions: list[str] | tuple[str, ...],
    retained_source_count: int,
    result_bearing_query_count: int,
) -> ResearchEvidenceGapReceipt:
    """Compile one bounded repair request from already assessed evidence."""

    normalized_gaps = tuple(
        dict.fromkeys(str(gap or "").strip() for gap in gaps if str(gap or "").strip())
    )
    normalized_dimensions = tuple(
        dict.fromkeys(
            str(dimension or "").strip()
            for dimension in missing_dimensions
            if str(dimension or "").strip()
        )
    )
    missing_official = any(
        "official" in gap.lower() or "canonical public url" in gap.lower()
        for gap in normalized_gaps
    )
    extraction_gap = any(
        marker in gap.lower()
        for gap in normalized_gaps
        for marker in ("snippet-only", "unextracted", "reader-usable")
    )
    query_terms = [
        *normalized_dimensions,
        *(["official product technology"] if missing_official else []),
        *(["full page product evidence"] if extraction_gap else []),
    ]
    return ResearchEvidenceGapReceipt(
        target_name=" ".join(str(target_name or "").split()),
        anchor=anchor,
        missing_official_source=missing_official,
        missing_dimensions=normalized_dimensions,
        extraction_gap=extraction_gap,
        retained_source_count=max(0, int(retained_source_count or 0)),
        result_bearing_query_count=max(0, int(result_bearing_query_count or 0)),
        recommended_query_terms=tuple(dict.fromkeys(query_terms)),
    )


__all__ = [
    "ResearchEvidenceGapReceipt",
    "compile_research_evidence_gap_receipt",
]
