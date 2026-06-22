# ruff: noqa: E501
"""Orchestrator Review scoring for saved #evals review forms."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from keystone_agents.agents.orchestrator import review_specialist_output
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.sdk import build_sdk_agent, compose_instructions
from promptfoo.eval_database import (
    DEFAULT_EVAL_DB,
    eval_case_status,
    record_eval_trace_event,
    resolve_human_review_target,
    validate_human_review_target,
)
from promptfoo.human_review import (
    REVIEW_KIND_ORCHESTRATOR_JUDGE,
    RUBRIC_PROMPTS,
    SCORE_DIMENSIONS,
    HumanEvalReview,
    save_human_review,
)

JUDGE_REVIEWER = "orchestrator_judge"
JUDGE_ENV_FLAG = "KEYSTONE_EVAL_LLM_JUDGE"


class EvalJudgeScores(BaseModel):
    """Fixed rubric score fields for Agents SDK strict structured output."""

    model_config = ConfigDict(extra="forbid")

    accuracy: float = Field(..., ge=0, le=5)
    relevance: float = Field(..., ge=0, le=5)
    explainability: float = Field(..., ge=0, le=5)
    readability: float = Field(..., ge=0, le=5)
    source_quality: float = Field(..., ge=0, le=5)
    search_quality: float = Field(..., ge=0, le=5)
    synthesis_quality: float = Field(..., ge=0, le=5)
    uniqueness: float = Field(..., ge=0, le=5)
    format_quality: float = Field(..., ge=0, le=5)
    instruction_following: float = Field(..., ge=0, le=5)
    usefulness: float = Field(..., ge=0, le=5)

    @model_validator(mode="before")
    @classmethod
    def _validate_complete_scores(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        missing = [dimension for dimension in SCORE_DIMENSIONS if dimension not in value]
        if missing:
            raise ValueError("complete scorecard is required; missing scores: " + ", ".join(missing))
        return value

    def as_dict(self) -> dict[str, float]:
        return {dimension: float(getattr(self, dimension)) for dimension in SCORE_DIMENSIONS}


class EvalJudgeRationales(BaseModel):
    """Fixed optional rationale fields matching the review-form rubric."""

    model_config = ConfigDict(extra="forbid")

    accuracy: str = ""
    relevance: str = ""
    explainability: str = ""
    readability: str = ""
    source_quality: str = ""
    search_quality: str = ""
    synthesis_quality: str = ""
    uniqueness: str = ""
    format_quality: str = ""
    instruction_following: str = ""
    usefulness: str = ""

    def as_dict(self) -> dict[str, str]:
        return {dimension: str(getattr(self, dimension)).strip() for dimension in SCORE_DIMENSIONS}


class OrchestratorEvalJudgeScorecard(BaseModel):
    """Form-shaped model scorecard for one saved #evals run."""

    model_config = ConfigDict(extra="forbid")

    scores: EvalJudgeScores
    safety: Literal["pass", "fail"]
    notes: str = Field(
        "",
        description=(
            "Overall run comment explaining how the saved response performed for this "
            "specific run, including the main reasons for the score."
        ),
    )
    dimension_rationales: EvalJudgeRationales = Field(default_factory=EvalJudgeRationales)
    recommended_next_action: str = ""
    confidence: float = Field(0.0, ge=0, le=1)

    @field_validator("confidence")
    @classmethod
    def _validate_confidence(cls, value: float) -> float:
        confidence = float(value)
        if confidence < 0 or confidence > 1:
            raise ValueError("confidence must be between 0 and 1")
        return confidence

    @model_validator(mode="after")
    def _default_notes(self) -> OrchestratorEvalJudgeScorecard:
        if not self.notes.strip():
            self.notes = (
                "Orchestrator Review completed the backend review form; no run-specific "
                "comment was provided."
            )
        return self

    @property
    def average_score(self) -> float:
        scores = self.scores.as_dict()
        return round(sum(scores.values()) / len(scores), 3)

    def scores_dict(self) -> dict[str, float]:
        return self.scores.as_dict()

    def rationales_dict(self) -> dict[str, str]:
        return self.dimension_rationales.as_dict()


EvalJudgeScorer = Callable[[dict[str, Any]], OrchestratorEvalJudgeScorecard | dict[str, Any]]


def eval_llm_judge_enabled(env: dict[str, str] | None = None) -> bool:
    env_map = env if env is not None else os.environ
    return str(env_map.get(JUDGE_ENV_FLAG) or "").strip().lower() in {"1", "true", "yes", "on"}


def score_eval_run_with_orchestrator_judge(
    *,
    case_id: str,
    database_path: str | Path = DEFAULT_EVAL_DB,
    run_id: str = "",
    slack_thread_ts: str = "",
    live: bool = False,
    run_config: Any | None = None,
    model: str | None = None,
    scorer: EvalJudgeScorer | None = None,
    require_enabled: bool = True,
) -> dict[str, Any]:
    """Score one saved #evals run through Orchestrator Review."""

    if require_enabled and scorer is None and run_config is None and not live:
        raise ValueError(
            f"Orchestrator Review scoring requires {JUDGE_ENV_FLAG}=true or an explicit run_config."
        )
    if require_enabled and scorer is None and not eval_llm_judge_enabled() and run_config is None:
        raise ValueError(f"{JUDGE_ENV_FLAG}=true is required for dashboard Orchestrator Review scoring")

    packet = build_orchestrator_judge_packet(
        case_id=case_id,
        database_path=database_path,
        run_id=run_id,
        slack_thread_ts=slack_thread_ts,
    )
    target = packet["review_target"]
    scorecard = _coerce_scorecard(
        scorer(packet) if scorer is not None else _run_orchestrator_eval_judge(
            packet,
            live=live,
            run_config=run_config,
            model=model,
        )
    )
    review = HumanEvalReview(
        case_id=str(packet["case_id"]),
        run_id=str(target.get("run_id") or ""),
        agent=str(target.get("agent") or packet.get("agent") or ""),
        reviewer=JUDGE_REVIEWER,
        review_kind=REVIEW_KIND_ORCHESTRATOR_JUDGE,
        scores=scorecard.scores_dict(),
        safety=scorecard.safety,
        notes=_judge_notes(scorecard),
        slack_channel_id=str(target.get("slack_channel_id") or "C0BA17Y9C01"),
        slack_channel_name=str(target.get("slack_channel_name") or "evals"),
        slack_thread_ts=str(target.get("slack_thread_ts") or ""),
        raw_text=json.dumps(scorecard.model_dump(), ensure_ascii=True, sort_keys=True),
        storage_mode=str(target.get("storage_mode") or "local_review"),
    )
    review = resolve_human_review_target(
        review,
        database_path=database_path,
        require_recorded_response=True,
    )
    review_target = validate_human_review_target(
        review,
        database_path=database_path,
        require_recorded_response=True,
    )
    if review_target.get("target_type") != "slack":
        raise ValueError("Orchestrator Review scoring is only available for saved #evals Slack runs")
    row_id = save_human_review(review, database_path=database_path)
    record_eval_trace_event(
        event_type="orchestrator_judge_review_saved",
        trace_id=f"orchestrator_judge_review:{row_id}",
        span_id=f"orchestrator_judge_review_row:{row_id}",
        name="orchestrator_judge_eval_review_saved",
        group_id=review.case_id,
        metadata={
            "schema": "keystone.eval.orchestrator_judge_review.v1",
            "case_id": review.case_id,
            "run_id": review.run_id,
            "slack_thread_ts": review.slack_thread_ts,
            "average_score": review.average_score,
            "safety": review.safety,
            "review_kind": review.review_kind,
            "confidence": scorecard.confidence,
            "recommended_next_action": scorecard.recommended_next_action,
        },
        database_path=database_path,
    )
    payload = review.to_dict()
    payload["id"] = row_id
    payload["review_target"] = review_target
    payload["dimension_rationales"] = scorecard.rationales_dict()
    payload["recommended_next_action"] = scorecard.recommended_next_action
    payload["confidence"] = scorecard.confidence
    return payload


def build_orchestrator_judge_packet(
    *,
    case_id: str,
    database_path: str | Path = DEFAULT_EVAL_DB,
    run_id: str = "",
    slack_thread_ts: str = "",
) -> dict[str, Any]:
    """Build the bounded #evals packet Orchestrator Review scores."""

    normalized = str(case_id or "").strip()
    if not normalized:
        raise ValueError("case_id is required")
    status = eval_case_status(normalized, database_path=database_path)
    case = status.get("case") or {}
    slack_runs = status.get("slack_runs") or []
    slack = _select_slack_run(slack_runs, run_id=run_id, slack_thread_ts=slack_thread_ts)
    if not slack:
        raise ValueError("Orchestrator Review scoring requires a saved #evals Slack run")
    if str(slack.get("slack_channel_name") or "evals").strip() != "evals":
        raise ValueError("Orchestrator Review scoring is only available for #evals Slack runs")
    response = str(slack.get("result_summary") or "").strip()
    if not response:
        raise ValueError("Orchestrator Review scoring requires a saved Slack response/result")
    agent = str(slack.get("agent") or slack.get("route") or case.get("agent_under_test") or "")
    deterministic_review = review_specialist_output(
        agent_name=agent or "unknown",
        output={"summary": response, "evidence": slack.get("evidence") or {}},
        request_summary=str(slack.get("request_text") or case.get("user_input") or ""),
        run_type="eval_orchestrator_judge_baseline",
    )
    return {
        "schema": "keystone.eval.orchestrator_judge_packet.v1",
        "case_id": normalized,
        "agent": agent,
        "dimensions": _split_dimensions(case.get("eval_dimensions") or ""),
        "prompt": str(slack.get("request_text") or case.get("user_input") or ""),
        "latest_response": response,
        "rubric": list(RUBRIC_PROMPTS),
        "machine_check": status.get("latest_promptfoo") or {},
        "slack_run": {
            key: slack.get(key)
            for key in (
                "run_id",
                "work_item_id",
                "status",
                "route",
                "slack_channel_id",
                "slack_channel_name",
                "slack_thread_ts",
                "thread_fetch_status",
                "thread_message_count",
                "warning_count",
                "source_count",
                "visible_source_count",
                "cost_profile",
                "created_at",
            )
        },
        "evidence": slack.get("evidence") or {},
        "warnings": slack.get("warnings") or [],
        "deterministic_review": deterministic_review.model_dump(mode="json"),
        "review_target": {
            "target_type": "slack",
            "run_id": str(slack.get("run_id") or slack.get("work_item_id") or ""),
            "agent": agent,
            "slack_thread_ts": str(slack.get("slack_thread_ts") or ""),
            "slack_channel_id": str(slack.get("slack_channel_id") or "C0BA17Y9C01"),
            "slack_channel_name": str(slack.get("slack_channel_name") or "evals"),
            "storage_mode": str(slack.get("storage_mode") or "local_review"),
        },
    }


def _run_orchestrator_eval_judge(
    packet: dict[str, Any],
    *,
    live: bool,
    run_config: Any | None,
    model: str | None,
) -> OrchestratorEvalJudgeScorecard:
    if run_config is None and not live:
        raise RuntimeError("Orchestrator Review scoring requires live=True or an explicit run_config")
    agent = build_sdk_agent(
        name="orchestrator_eval_judge",
        instructions=compose_instructions(
            "keystone_profile.md",
            "safety_policy.md",
            "orchestrator_eval_judge.md",
        ),
        output_type=OrchestratorEvalJudgeScorecard,
        tools=[],
        model=model,
        handoff_description="Scores saved #evals outputs using the backend eval review form rubric.",
    )
    result = run_typed_sdk_agent(
        agent=agent,
        typed_input=json.dumps(packet, ensure_ascii=True, sort_keys=True),
        output_type=OrchestratorEvalJudgeScorecard,
        run_config=run_config,
        live=live,
        workflow_name="Keystone #evals Orchestrator Review scoring",
        group_id=str(packet.get("case_id") or ""),
        trace_metadata={
            "agent": "orchestrator_eval_judge",
            "case_id": str(packet.get("case_id") or ""),
            "review_kind": REVIEW_KIND_ORCHESTRATOR_JUDGE,
            "channel": "evals",
        },
    )
    return result.output


def _coerce_scorecard(value: OrchestratorEvalJudgeScorecard | dict[str, Any]) -> OrchestratorEvalJudgeScorecard:
    if isinstance(value, OrchestratorEvalJudgeScorecard):
        return value
    return OrchestratorEvalJudgeScorecard.model_validate(value)


def _judge_notes(scorecard: OrchestratorEvalJudgeScorecard) -> str:
    parts = [scorecard.notes.strip()]
    if scorecard.recommended_next_action.strip():
        parts.append(f"Recommended next action: {scorecard.recommended_next_action.strip()}")
    return "\n".join(part for part in parts if part)


def _select_slack_run(
    rows: list[dict[str, Any]],
    *,
    run_id: str = "",
    slack_thread_ts: str = "",
) -> dict[str, Any]:
    candidates = list(rows)
    normalized_run_id = str(run_id or "").strip()
    normalized_thread_ts = str(slack_thread_ts or "").strip()
    if normalized_run_id:
        candidates = [
            row
            for row in candidates
            if normalized_run_id in {str(row.get("run_id") or ""), str(row.get("work_item_id") or "")}
        ]
    if normalized_thread_ts:
        candidates = [
            row for row in candidates if str(row.get("slack_thread_ts") or "") == normalized_thread_ts
        ]
    return candidates[0] if candidates else {}


def _split_dimensions(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").replace(";", ",").split(",") if part.strip()]
