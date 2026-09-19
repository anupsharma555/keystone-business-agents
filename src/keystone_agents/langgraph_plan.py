"""Dependency-free design scaffold for optional future LangGraph orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

NodeName = Literal[
    "classify_input",
    "gmail_triage",
    "account_research",
    "rag_retrieval",
    "opportunity_scoring",
    "outreach_drafting",
    "approval_checkpoint",
    "storage",
    "report",
]


@dataclass(frozen=True)
class PlannedEdge:
    """Document a future orchestration edge without importing LangGraph."""

    source: NodeName
    target: NodeName
    condition: str


PLANNED_NODES: tuple[NodeName, ...] = (
    "classify_input",
    "gmail_triage",
    "account_research",
    "rag_retrieval",
    "opportunity_scoring",
    "outreach_drafting",
    "approval_checkpoint",
    "storage",
    "report",
)


PLANNED_EDGES: tuple[PlannedEdge, ...] = (
    PlannedEdge("classify_input", "gmail_triage", "input is inbound email"),
    PlannedEdge("classify_input", "account_research", "input is company context"),
    PlannedEdge(
        "classify_input",
        "rag_retrieval",
        "typed plan explicitly selects vector-store retrieval",
    ),
    PlannedEdge("classify_input", "opportunity_scoring", "input is scout request"),
    PlannedEdge("gmail_triage", "approval_checkpoint", "draft reply or risk flag exists"),
    PlannedEdge("gmail_triage", "account_research", "relevant company identified"),
    PlannedEdge("account_research", "opportunity_scoring", "source-backed fit exists"),
    PlannedEdge("opportunity_scoring", "account_research", "high-priority enrichment needed"),
    PlannedEdge("opportunity_scoring", "approval_checkpoint", "outreach may be drafted"),
    PlannedEdge("approval_checkpoint", "outreach_drafting", "human approved drafting"),
    PlannedEdge("outreach_drafting", "approval_checkpoint", "draft requires review"),
    PlannedEdge("approval_checkpoint", "storage", "decision recorded"),
    PlannedEdge("storage", "report", "final output requested"),
)
