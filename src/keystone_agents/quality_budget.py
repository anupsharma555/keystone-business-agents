"""Quality budget policy for bounded SDK agent runs."""

from __future__ import annotations

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
    enable_synthesis_review: bool = False
    enable_context_deepening: bool = False
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
        raise ValueError(f"Unsupported quality mode {value!r}; expected one of: {allowed}.") from exc


def chief_of_staff_quality_budget(
    mode: QualityMode | str | None = None,
    *,
    request_text: str = "",
    live_sdk: bool = False,
) -> AgentQualityBudget:
    """Resolve a Chief of Staff quality budget from explicit mode or request intent."""

    explicit = mode is not None and str(mode).strip() != ""
    resolved = normalize_quality_mode(mode, QualityMode.BALANCED if live_sdk else QualityMode.FAST)
    notes: list[str] = []
    if explicit:
        notes.append(f"Quality mode explicitly requested: {resolved.value}.")
    else:
        if _looks_like_chief_of_staff_deep_request(request_text):
            resolved = QualityMode.DEEP
            notes.append("Quality mode inferred as deep from operational audit/planning request.")
        elif live_sdk and not _looks_like_chief_of_staff_fast_request(request_text):
            resolved = QualityMode.BALANCED
            notes.append("Quality mode inferred as balanced for live Chief of Staff SDK planning.")
        else:
            resolved = QualityMode.FAST
            notes.append("Quality mode inferred as fast for simple deterministic Chief of Staff routing.")
    return _budget_for_mode(resolved, notes=notes)


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
        enable_synthesis_review=False,
        enable_context_deepening=True,
        notes=notes,
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
