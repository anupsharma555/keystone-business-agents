"""Quality budget policy for bounded SDK agent runs."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, Field


class QualityMode(StrEnum):
    """Supported run quality modes."""

    FAST = "fast"
    BALANCED = "balanced"
    DEEP = "deep"


class AgentQualityBudget(BaseModel):
    """Bounded budget controls and metadata for one agent run."""

    agent_name: str
    mode: QualityMode
    max_turns: int | None = None
    reasoning_effort: str | None = None
    verbosity: str | None = None
    max_tokens: int | None = None
    max_review_passes: int = 0
    max_tool_calls: int | None = None
    max_seconds: int | None = None
    retrieval_max_results: int | None = None
    hosted_web_search_max_calls: int | None = None
    tool_tier: str | None = None
    enable_synthesis_review: bool = False
    enable_context_deepening: bool = False
    enable_page_verification: bool = False
    allow_manager_loop_repair: bool | None = None
    include_contact_enrichment: bool | None = None
    reuse_existing_research: bool | None = None
    notes: list[str] = Field(default_factory=list)


def normalize_quality_mode(
    value: QualityMode | str | None,
    default: QualityMode,
) -> QualityMode:
    """Normalize an optional quality mode value."""

    if value is None:
        return default
    if isinstance(value, QualityMode):
        return value
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    try:
        return QualityMode(normalized)
    except ValueError as exc:
        allowed = ", ".join(mode.value for mode in QualityMode)
        raise ValueError(
            f"Unsupported quality mode {value!r}; expected one of: {allowed}."
        ) from exc


def chief_of_staff_quality_budget(
    mode: QualityMode | str | None = None,
    *,
    request_text: str = "",
    live_sdk: bool = False,
) -> AgentQualityBudget:
    """Resolve a Chief of Staff quality budget from explicit mode or request intent."""

    explicit = mode is not None and str(mode).strip() != ""
    resolved = normalize_quality_mode(
        mode,
        QualityMode.BALANCED if live_sdk else QualityMode.FAST,
    )
    notes: list[str] = []
    if explicit:
        notes.append(f"Quality mode explicitly requested: {resolved.value}.")
    else:
        if _looks_like_bounded_live_sdk_smoke_request(request_text):
            resolved = QualityMode.FAST
            notes.append("Quality mode inferred as fast for bounded live SDK smoke validation.")
        elif _looks_like_chief_of_staff_deep_request(request_text):
            resolved = QualityMode.DEEP
            notes.append("Quality mode inferred as deep from operational audit/planning request.")
        elif live_sdk and not _looks_like_chief_of_staff_fast_request(request_text):
            resolved = QualityMode.BALANCED
            notes.append("Quality mode inferred as balanced for live Chief of Staff SDK planning.")
        else:
            resolved = QualityMode.FAST
            notes.append(
                "Quality mode inferred as fast for simple deterministic Chief of Staff routing."
            )
    budget = _budget_for_mode(resolved, notes=notes)
    if _looks_like_bounded_live_sdk_smoke_request(request_text):
        return budget.model_copy(
            update={
                "max_turns": min(budget.max_turns or 3, 3),
                "max_tokens": min(budget.max_tokens or 1600, 1600),
                "max_tool_calls": min(budget.max_tool_calls or 3, 3),
                "enable_context_deepening": False,
                "hosted_web_search_max_calls": 0,
                "tool_tier": "core_read",
                "notes": [
                    *budget.notes,
                    (
                        "Bounded smoke validation capped Chief of Staff to three SDK "
                        "turns, disabled hosted web search, and kept tool use on the "
                        "core read tier."
                    ),
                ],
            }
        )
    return budget


def business_research_quality_budget(
    mode: QualityMode | str | None = None,
    *,
    request_text: str = "",
    live_search: bool = False,
    cost_profile: str = "standard",
) -> AgentQualityBudget:
    """Resolve retrieval/model controls for Business Research Analyst runs."""

    resolved, notes = _resolve_research_quality_mode(
        mode,
        request_text=request_text,
        live_search=live_search,
        cost_profile=cost_profile,
        agent_label="Business Research Analyst",
    )
    budget = _research_budget_for_mode(
        "business_research_analyst",
        resolved,
        notes=notes,
    )
    if resolved == QualityMode.DEEP:
        budget.enable_page_verification = True
        budget.allow_manager_loop_repair = True
        budget.include_contact_enrichment = True
        budget.reuse_existing_research = False
    elif resolved == QualityMode.BALANCED:
        budget.include_contact_enrichment = False
        budget.reuse_existing_research = True
    else:
        budget.allow_manager_loop_repair = False
        budget.include_contact_enrichment = False
        budget.reuse_existing_research = True
    return budget


def opportunity_scout_quality_budget(
    mode: QualityMode | str | None = None,
    *,
    request_text: str = "",
    live_search: bool = False,
    cost_profile: str = "standard",
    formal_opportunity: bool = False,
    source_context_required: bool = False,
) -> AgentQualityBudget:
    """Resolve retrieval/model controls for Opportunity Scout runs."""

    resolved, notes = _resolve_research_quality_mode(
        mode,
        request_text=request_text,
        live_search=live_search,
        cost_profile=cost_profile,
        agent_label="Opportunity Scout",
    )
    if formal_opportunity or source_context_required:
        resolved = QualityMode.DEEP
        notes.append(
            "Quality mode elevated to deep because formal/source-backed "
            "opportunity evidence was required."
        )
    budget = _research_budget_for_mode("opportunity_scout", resolved, notes=notes)
    if resolved == QualityMode.DEEP:
        budget.enable_page_verification = True
        budget.allow_manager_loop_repair = True
        budget.include_contact_enrichment = False
        budget.reuse_existing_research = False
    elif resolved == QualityMode.BALANCED:
        budget.include_contact_enrichment = False
        budget.reuse_existing_research = True
    else:
        budget.allow_manager_loop_repair = False
        budget.include_contact_enrichment = False
        budget.reuse_existing_research = True
    return budget


def _budget_for_mode(mode: QualityMode, *, notes: list[str]) -> AgentQualityBudget:
    if mode == QualityMode.FAST:
        return AgentQualityBudget(
            agent_name="chief_of_staff",
            mode=mode,
            max_turns=4,
            reasoning_effort="low",
            verbosity="low",
            max_tokens=2500,
            max_review_passes=0,
            max_tool_calls=6,
            max_seconds=45,
            tool_tier="core_read",
            enable_synthesis_review=False,
            enable_context_deepening=False,
            notes=notes,
        )
    if mode == QualityMode.DEEP:
        return AgentQualityBudget(
            agent_name="chief_of_staff",
            mode=mode,
            max_turns=14,
            reasoning_effort="medium",
            verbosity="medium",
            max_tokens=6500,
            max_review_passes=0,
            max_tool_calls=24,
            max_seconds=300,
            tool_tier="deep_retrieval",
            enable_synthesis_review=False,
            enable_context_deepening=True,
            notes=notes,
        )
    return AgentQualityBudget(
        agent_name="chief_of_staff",
        mode=QualityMode.BALANCED,
        max_turns=8,
        reasoning_effort="low",
        verbosity="low",
        max_tokens=4000,
        max_review_passes=0,
        max_tool_calls=12,
        max_seconds=120,
        tool_tier="web_search",
        enable_synthesis_review=False,
        enable_context_deepening=True,
        notes=notes,
    )


def _research_budget_for_mode(
    agent_name: str,
    mode: QualityMode,
    *,
    notes: list[str],
) -> AgentQualityBudget:
    if mode == QualityMode.FAST:
        return AgentQualityBudget(
            agent_name=agent_name,
            mode=mode,
            max_turns=4,
            reasoning_effort="low",
            verbosity="low",
            max_tokens=2200,
            max_review_passes=0,
            max_tool_calls=6,
            max_seconds=45,
            retrieval_max_results=3,
            hosted_web_search_max_calls=0,
            tool_tier="core_read",
            enable_synthesis_review=False,
            enable_context_deepening=False,
            enable_page_verification=False,
            notes=notes,
        )
    if mode == QualityMode.DEEP:
        return AgentQualityBudget(
            agent_name=agent_name,
            mode=mode,
            max_turns=12,
            reasoning_effort="medium",
            verbosity="medium",
            max_tokens=6500,
            max_review_passes=1,
            max_tool_calls=24,
            max_seconds=300,
            retrieval_max_results=8,
            hosted_web_search_max_calls=2,
            tool_tier="deep_retrieval",
            enable_synthesis_review=True,
            enable_context_deepening=True,
            enable_page_verification=True,
            notes=notes,
        )
    return AgentQualityBudget(
        agent_name=agent_name,
        mode=QualityMode.BALANCED,
        max_turns=8,
        reasoning_effort="low",
        verbosity="low",
        max_tokens=4200,
        max_review_passes=0,
        max_tool_calls=12,
        max_seconds=120,
        retrieval_max_results=5,
        hosted_web_search_max_calls=1,
        tool_tier="web_search",
        enable_synthesis_review=True,
        enable_context_deepening=True,
        enable_page_verification=False,
        notes=notes,
    )


def _resolve_research_quality_mode(
    mode: QualityMode | str | None,
    *,
    request_text: str,
    live_search: bool,
    cost_profile: str,
    agent_label: str,
) -> tuple[QualityMode, list[str]]:
    explicit = mode is not None and str(mode).strip() != ""
    if explicit:
        resolved = normalize_quality_mode(mode, QualityMode.BALANCED)
        return resolved, [f"Quality mode explicitly requested: {resolved.value}."]

    profile_mode = _quality_mode_from_cost_profile(cost_profile)
    if profile_mode is not None:
        return profile_mode, [
            f"Quality mode inferred as {profile_mode.value} from cost profile {cost_profile!r}."
        ]

    if _looks_like_deep_research_request(request_text):
        return QualityMode.DEEP, [
            f"Quality mode inferred as deep for {agent_label} source-backed/deeper research."
        ]

    if live_search:
        return QualityMode.BALANCED, [
            f"Quality mode inferred as balanced for live {agent_label} research."
        ]

    return QualityMode.FAST, [
        f"Quality mode inferred as fast for fixture/dry-run {agent_label} research."
    ]


def _quality_mode_from_cost_profile(cost_profile: str) -> QualityMode | None:
    normalized = str(cost_profile or "").strip().lower().replace("-", "_")
    if not normalized or normalized == "standard":
        return None
    if normalized.endswith("_deep") or normalized in {"deep", "thorough", "high_quality"}:
        return QualityMode.DEEP
    if normalized.endswith("_balanced") or normalized in {"balanced", "default"}:
        return QualityMode.BALANCED
    if normalized.endswith("_fast") or normalized in {
        "fast",
        "light",
        "slack_context_light",
        "slack_smoke_limited",
        "slack_conservative",
        "slack_cost_conservative",
    }:
        return QualityMode.FAST
    return None


def quality_mode_from_cost_profile(cost_profile: str) -> QualityMode | None:
    """Return the quality mode implied by a shared cost profile, if any."""

    return _quality_mode_from_cost_profile(cost_profile)


def _looks_like_deep_research_request(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    if not normalized:
        return False
    if any(
        marker in normalized
        for marker in (
            "deep search",
            "deeper search",
            "deepened search",
            "detailed search",
            "source-backed",
            "source backed",
            "provider comparison",
            "provider diagnostics",
            "compare first-pass",
            "beyond a broad first pass",
            "read the links",
            "summarize the source data",
        )
    ):
        return True
    return bool(
        ("visible source" in normalized or "source urls" in normalized)
        and ("synthesis" in normalized or "comparison" in normalized or "table" in normalized)
    )


def _looks_like_chief_of_staff_deep_request(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    if not normalized:
        return False
    deep_markers = (
        "audit automation",
        "audit current automation",
        "review current automation",
        "evaluate automation inventory",
        "automation health",
        "debug automation",
        "analyze the slack runtime",
        "inspect repo context",
        "make an operating plan",
        "compare workflow options",
        "cross-channel",
        "across channels",
        "active work items",
        "active workitems",
        "pending automation approvals",
        "recent runs",
        "stale threads",
        "day end summary",
        "end-of-day summary",
        "weekly synthesis",
        "recommend how to improve",
    )
    return any(marker in normalized for marker in deep_markers)


def _looks_like_chief_of_staff_fast_request(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    if not normalized:
        return True
    fast_markers = (
        "what is your scope",
        "what can you do",
        "what do you do",
        "what is your role",
        "which workflow",
        "route this",
        "calendar today",
        "gmail summarize",
        "keep this for future reference",
        "remember this",
        "save this",
        "bookmark this",
    )
    return any(marker in normalized for marker in fast_markers)


def _looks_like_bounded_live_sdk_smoke_request(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    if "smoke" not in normalized:
        return False
    return bool(
        re.search(r"\bbounded\b|\bread[- ]only\b|\blive sdk is approved only\b", normalized)
    )
