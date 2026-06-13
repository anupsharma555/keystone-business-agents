"""Run deterministic local evals for Keystone agents.

This harness intentionally does not call the OpenAI Evals API. It runs local JSONL
datasets against fixture-mode agent functions and pure Python graders so it can be
used in offline development and CI.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from keystone_agents.agent_registry import AGENT_REGISTRY
from keystone_agents.agents.chief_of_staff import plan_chief_of_staff_request
from keystone_agents.agents.gmail_triage import EmailFixture, triage_email_fixture
from keystone_agents.agents.opportunity_scout import (
    score_opportunity_impl,
    scout_opportunities_fixture,
)
from keystone_agents.agents.orchestrator import route_request
from keystone_agents.agents.outreach_composer import (
    check_unsupported_claims,
    compose_outreach_draft_fixture,
    load_company_profile,
    load_contact_context,
    load_crm_account_context,
    load_opportunity_record,
    load_style_profile,
)
from keystone_agents.benchmark_tracking import record_eval_summary
from keystone_agents.company_research import research_company_fixture
from keystone_agents.guardrails import assess_text_guardrails
from keystone_agents.multi_target_research import (
    MultiTargetResearchPlan,
    run_multi_target_research,
)
from keystone_agents.schemas.approval import (
    ApprovalQueueItem,
    approval_queue_status_allows_sending,
)
from keystone_agents.schemas.company_profile import CompanyProfile, SourceRecord
from keystone_agents.schemas.retrieval import RetrievalHint
from keystone_agents.schemas.work_item import (
    WorkflowRunResult,
    WorkItem,
    WorkItemApprovalGate,
    WorkItemArtifactRef,
    WorkItemBlocker,
    WorkItemKind,
    WorkItemRoute,
    WorkItemStatus,
)
from keystone_agents.sdk import prompt_metadata_for_files, prompt_version_references
from keystone_agents.skill_contract_gates import (
    check_business_research_claim_gate,
    check_chief_artifact_publish_gate,
    check_gmail_sensitive_message_gate,
    check_outreach_approval_claim_gate,
)
from keystone_agents.skill_evals import (
    DEFAULT_SKILL_TASK_MATRIX,
    SkillTaskEvalResult,
    run_skill_task_eval_suite,
)
from keystone_agents.slack_query_prompts import (
    build_slack_query_prompt_input,
    resolve_slack_query_prompt,
)
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools.search_provider import SearchResult

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVAL_DIR = PROJECT_ROOT / "evals" / "local"
SKILL_TASK_MATRIX_DATASET = "skill_task_matrix"
SPECIALIZED_EVAL_DATASETS = frozenset(
    {
        "browser_extraction_cases.jsonl",
        "search_coverage_cases.jsonl",
        "skill_contracts.jsonl",
        f"{SKILL_TASK_MATRIX_DATASET}.jsonl",
    }
)

LOCAL_EVAL_PROMPT_FILES: dict[str, tuple[str, ...]] = {
    "gmail_triage": ("keystone_profile.md", "gmail_triage.md"),
    "orchestrator_routing": (
        "keystone_profile.md",
        "safety_policy.md",
        "orchestrator.md",
    ),
    "safety_refusal": ("safety_policy.md", "orchestrator.md"),
    "source_attribution": (
        "keystone_profile.md",
        "safety_policy.md",
        "business_research_analyst.md",
        "opportunity_scout.md",
    ),
    "slack_research_workflow": (
        "keystone_profile.md",
        "safety_policy.md",
        "business_research_analyst.md",
    ),
    "opportunity_scoring": (
        "keystone_profile.md",
        "safety_policy.md",
        "opportunity_scout.md",
    ),
    "outreach_copy_constraints": ("keystone_profile.md", "outreach_composer.md"),
    "approval_queue_revision": (
        "keystone_profile.md",
        "safety_policy.md",
        "outreach_composer.md",
    ),
    "skill_gate_failures": (
        "keystone_profile.md",
        "safety_policy.md",
        "business_research_analyst.md",
        "gmail_triage.md",
        "outreach_composer.md",
        "chief_of_staff.md",
    ),
}


@dataclass(frozen=True)
class LocalEvalCase:
    dataset: str
    line_number: int
    case_id: str
    task: str
    input_payload: Mapping[str, Any]
    expected: Mapping[str, Any]
    validates_prompts: tuple[str, ...] = ()
    validates_skills: tuple[str, ...] = ()
    surface: str = "cli"


@dataclass(frozen=True)
class LocalEvalResult:
    dataset: str
    case_id: str
    task: str
    passed: bool
    failures: list[str]
    observed: Mapping[str, Any]
    prompt_metadata: list[dict[str, Any]]
    validates_prompts: tuple[str, ...] = ()
    validates_skills: tuple[str, ...] = ()
    surface: str = "cli"

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "id": self.case_id,
            "task": self.task,
            "passed": self.passed,
            "failures": self.failures,
            "observed": dict(self.observed),
            "prompt_metadata": self.prompt_metadata,
            "prompt_versions": [str(metadata["reference"]) for metadata in self.prompt_metadata],
            "validates_prompts": list(self.validates_prompts),
            "validates_skills": list(self.validates_skills),
            "surface": self.surface,
        }


@dataclass(frozen=True)
class LocalEvalSummary:
    results: list[LocalEvalResult]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for result in self.results if result.passed)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "datasets": sorted({result.dataset for result in self.results}),
            "prompt_metadata": _unique_prompt_metadata(self.results),
            "results": [result.to_dict() for result in self.results],
        }


def _fail(failures: list[str], field: str, observed: Any, expected: Any) -> None:
    failures.append(f"{field}: observed {observed!r}, expected {expected!r}")


def _expect_equal(
    failures: list[str],
    observed: Mapping[str, Any],
    expected: Mapping[str, Any],
    field: str,
) -> None:
    if field in expected and observed.get(field) != expected[field]:
        _fail(failures, field, observed.get(field), expected[field])


def _expect_contains_all(
    failures: list[str],
    observed_items: Iterable[Any],
    expected_items: Iterable[Any],
    field: str,
) -> None:
    observed_set = {str(item) for item in observed_items}
    missing = [item for item in expected_items if str(item) not in observed_set]
    if missing:
        failures.append(f"{field}: missing expected values {missing!r}")


def _expect_range(
    failures: list[str],
    value: int | float,
    *,
    field: str,
    minimum: int | float | None = None,
    maximum: int | float | None = None,
) -> None:
    if minimum is not None and value < minimum:
        failures.append(f"{field}: observed {value!r}, expected >= {minimum!r}")
    if maximum is not None and value > maximum:
        failures.append(f"{field}: observed {value!r}, expected <= {maximum!r}")


def _path_from_project(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _normalize_company_name(value: str) -> str:
    text = value.lower()
    text = re.sub(r"\b(?:inc|llc|ltd|corp|corporation|company|co)\b", "", text)
    return re.sub(r"[^a-z0-9]+", "", text).strip()


def _load_jsonl_cases(path: Path) -> list[LocalEvalCase]:
    cases: list[LocalEvalCase] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSONL row: {exc}") from exc

        if not isinstance(row, dict):
            raise ValueError(f"{path}:{line_number}: row must be a JSON object")
        if not isinstance(row.get("input"), dict):
            raise ValueError(f"{path}:{line_number}: row input must be a JSON object")
        if not isinstance(row.get("expected"), dict):
            raise ValueError(f"{path}:{line_number}: row expected must be a JSON object")

        case_id = row.get("id")
        task = row.get("task")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f"{path}:{line_number}: row id must be a non-empty string")
        if not isinstance(task, str) or not task:
            raise ValueError(f"{path}:{line_number}: row task must be a non-empty string")

        cases.append(
            LocalEvalCase(
                dataset=path.stem,
                line_number=line_number,
                case_id=case_id,
                task=task,
                input_payload=row["input"],
                expected=row["expected"],
                validates_prompts=_validates_prompts(row, task),
                validates_skills=_string_tuple(row.get("validates_skills")),
                surface=str(row.get("surface") or "cli"),
            )
        )
    return cases


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    if not all(isinstance(item, str) and item for item in value):
        raise ValueError("Expected a list of non-empty strings")
    return tuple(value)


def _validates_prompts(row: Mapping[str, Any], task: str) -> tuple[str, ...]:
    provided = row.get("validates_prompts")
    if isinstance(provided, list) and all(isinstance(item, str) for item in provided):
        return tuple(provided)
    return tuple(prompt_version_references(LOCAL_EVAL_PROMPT_FILES.get(task, ())))


def _prompt_metadata_for_case(case: LocalEvalCase) -> list[dict[str, Any]]:
    if case.validates_prompts:
        prompt_files = tuple(
            _prompt_file_from_reference(reference) for reference in case.validates_prompts
        )
        return prompt_metadata_for_files(prompt_files)
    return prompt_metadata_for_files(LOCAL_EVAL_PROMPT_FILES.get(case.task, ()))


def _prompt_file_from_reference(reference: str) -> str:
    prompt_name = reference.split("@", 1)[0].strip()
    return prompt_name if prompt_name.endswith(".md") else f"{prompt_name}.md"


def _unique_prompt_metadata(results: list[LocalEvalResult]) -> list[dict[str, Any]]:
    by_reference: dict[str, dict[str, Any]] = {}
    for result in results:
        for metadata in result.prompt_metadata:
            by_reference[str(metadata["reference"])] = metadata
    return [by_reference[reference] for reference in sorted(by_reference)]


def _result(
    case: LocalEvalCase,
    failures: list[str],
    observed: Mapping[str, Any],
) -> LocalEvalResult:
    prompt_metadata = _prompt_metadata_for_case(case)
    return LocalEvalResult(
        dataset=case.dataset,
        case_id=case.case_id,
        task=case.task,
        passed=not failures,
        failures=failures,
        observed=observed,
        prompt_metadata=prompt_metadata,
        validates_prompts=case.validates_prompts,
        validates_skills=case.validates_skills,
        surface=case.surface,
    )


def grade_gmail_triage(case: LocalEvalCase) -> LocalEvalResult:
    payload = case.input_payload
    expected = case.expected
    style_profile = payload.get("email_style_profile")
    if "style_fixture" in payload:
        style_profile = load_style_profile(str(payload["style_fixture"]))
    result = triage_email_fixture(
        EmailFixture(
            subject=str(payload.get("subject", "")),
            body=str(payload.get("body", "")),
            sender_name=str(payload.get("sender_name", "")),
            sender_email=str(payload.get("sender_email", "")),
            message_id=str(payload.get("message_id", case.case_id)),
        ),
        email_style_profile=style_profile,
    )
    observed = result.model_dump()
    failures: list[str] = []

    for field in (
        "category",
        "priority",
        "needs_reply",
        "draft_created",
        "approval_required",
        "style_profile_used",
        "style_profile_id",
    ):
        _expect_equal(failures, observed, expected, field)
    if "risk_flags_exact" in expected and observed["risk_flags"] != expected["risk_flags_exact"]:
        _fail(failures, "risk_flags", observed["risk_flags"], expected["risk_flags_exact"])
    _expect_contains_all(
        failures,
        observed["risk_flags"],
        expected.get("risk_flags_contains", []),
        "risk_flags",
    )
    _expect_contains_all(
        failures,
        observed["recommended_labels"],
        expected.get("recommended_labels_contains", []),
        "recommended_labels",
    )

    draft_reply = str(observed.get("draft_reply") or "")
    for required in expected.get("draft_contains", []):
        if str(required).lower() not in draft_reply.lower():
            failures.append(f"draft_reply: missing required text {required!r}")
    for forbidden in expected.get("draft_must_not_contain", []):
        if str(forbidden).lower() in draft_reply.lower():
            failures.append(f"draft_reply: contained forbidden text {forbidden!r}")
    if "\u2014" in json.dumps(observed, ensure_ascii=False):
        failures.append("observed output contains an em dash")

    return _result(case, failures, observed)


def _approved_company_profile(payload: Mapping[str, Any]) -> CompanyProfile | None:
    profile = payload.get("approved_company_profile")
    if profile is None:
        return None
    if not isinstance(profile, Mapping):
        raise ValueError("approved_company_profile must be a JSON object")
    return CompanyProfile.model_validate(dict(profile))


def _check_route_result(
    failures: list[str],
    observed: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    for field in (
        "route",
        "target_agent",
        "refused",
        "approved_context_present",
        "send_enabled",
        "can_send_email",
        "routing_mode",
        "state_context_used",
    ):
        _expect_equal(failures, observed, expected, field)
    if "stop_reason_contains" in expected:
        stop_reason = str(observed.get("stop_reason") or "")
        if str(expected["stop_reason_contains"]) not in stop_reason:
            failures.append(
                f"stop_reason: missing expected text {expected['stop_reason_contains']!r}"
            )
    _expect_contains_all(
        failures,
        observed.get("forbidden_actions", []),
        expected.get("forbidden_actions_contains", []),
        "forbidden_actions",
    )


def grade_orchestrator_routing(case: LocalEvalCase) -> LocalEvalResult:
    expected = case.expected
    profile = _approved_company_profile(case.input_payload)
    decision = route_request(
        case.input_payload.get("request"),
        approved_company_profile=profile,
        workflow_state=case.input_payload.get("workflow_state"),
    )
    observed = decision.model_dump()
    failures: list[str] = []
    _check_route_result(failures, observed, expected)
    return _result(case, failures, observed)


def grade_safety_refusal(case: LocalEvalCase) -> LocalEvalResult:
    mode = str(case.input_payload.get("mode", "guardrail"))
    expected = case.expected
    failures: list[str] = []

    if mode == "guardrail":
        assessment = assess_text_guardrails(str(case.input_payload.get("text", "")))
        observed: Mapping[str, Any] = {
            "allowed": assessment.allowed,
            "manual_review_required": assessment.manual_review_required,
            "risk_flags": list(assessment.risk_flags),
            "reasons": list(assessment.reasons),
            "draft_policy": assessment.draft_policy,
        }
        for field in ("allowed", "manual_review_required", "draft_policy"):
            _expect_equal(failures, observed, expected, field)
        _expect_contains_all(
            failures,
            observed["risk_flags"],
            expected.get("risk_flags_contains", []),
            "risk_flags",
        )
        return _result(case, failures, observed)

    if mode == "route":
        decision = route_request(case.input_payload.get("request"))
        observed = decision.model_dump()
        _check_route_result(failures, observed, expected)
        return _result(case, failures, observed)

    raise ValueError(f"{case.case_id}: unsupported safety_refusal mode {mode!r}")


def grade_source_attribution(case: LocalEvalCase) -> LocalEvalResult:
    kind = str(case.input_payload.get("kind", "company_profile"))
    expected = case.expected
    failures: list[str] = []

    if kind == "company_profile":
        fixture = case.input_payload.get("fixture")
        profile = research_company_fixture(
            company_name=str(case.input_payload.get("company_name", "")),
            fixture_json=_path_from_project(str(fixture)) if fixture else None,
        )
        observed = profile.model_dump()
        sources = profile.sources
        _expect_range(
            failures,
            len(sources),
            field="sources",
            minimum=int(expected.get("min_sources", 0)),
        )
        _expect_range(
            failures,
            len(profile.evidence),
            field="evidence",
            minimum=int(expected.get("min_evidence_items", 0)),
        )
        if expected.get("require_source_urls") and not all(source.url for source in sources):
            failures.append("sources: each source must include a URL")
        if expected.get("require_supported_claims") and not all(
            source.supported_claims for source in sources
        ):
            failures.append("sources: each source must include supported claims")
        source_quality = profile.source_quality_summary
        if source_quality is not None:
            _expect_range(
                failures,
                source_quality.average_recency_score,
                field="source_quality.average_recency_score",
                minimum=expected.get("min_average_recency_score"),
                maximum=expected.get("max_average_recency_score"),
            )
            _expect_range(
                failures,
                source_quality.low_quality_source_count,
                field="source_quality.low_quality_source_count",
                minimum=expected.get("min_low_quality_source_count"),
                maximum=expected.get("max_low_quality_source_count"),
            )
        _expect_range(
            failures,
            profile.confidence_score,
            field="confidence_score",
            minimum=expected.get("min_confidence_score"),
            maximum=expected.get("max_confidence_score"),
        )
        return _result(case, failures, observed)

    if kind == "opportunity_scout":
        fixture = case.input_payload.get("fixture")
        result = scout_opportunities_fixture(
            fixture=_path_from_project(str(fixture)) if fixture else None,
            max_results=int(case.input_payload.get("max_results", 5)),
        )
        observed = result.model_dump()
        records = result.records
        _expect_range(
            failures,
            len(records),
            field="records",
            minimum=int(expected.get("min_records", 0)),
        )
        if expected.get("require_record_sources") and not all(record.sources for record in records):
            failures.append("records: each record must include sources")
        if expected.get("require_source_urls") and not all(
            source.url for record in records for source in record.sources
        ):
            failures.append("records: each source must include a URL")
        if expected.get("require_supported_signals") and not all(
            source.supported_signal for record in records for source in record.sources
        ):
            failures.append("records: each source must include a supported signal")
        _expect_range(
            failures,
            len(records),
            field="records",
            maximum=expected.get("max_records"),
        )
        if expected.get("unique_companies"):
            normalized_names = [_normalize_company_name(record.company_name) for record in records]
            duplicates = sorted(
                {name for name in normalized_names if normalized_names.count(name) > 1}
            )
            if duplicates:
                failures.append(f"records: duplicate companies {duplicates!r}")
        return _result(case, failures, observed)

    raise ValueError(f"{case.case_id}: unsupported source_attribution kind {kind!r}")


class _SlackResearchEvalSearchProvider:
    provider_name = "local_eval"

    def __init__(self, results_by_marker: Mapping[str, Sequence[SearchResult]]) -> None:
        self._results_by_marker = results_by_marker

    def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
        for marker, results in self._results_by_marker.items():
            if marker in query:
                return list(results[:num_results])
        return []


def _slack_research_eval_provider_builder(
    results_by_marker: Mapping[str, Sequence[SearchResult]],
) -> Callable[..., _SlackResearchEvalSearchProvider]:
    def build_provider(**_kwargs: Any) -> _SlackResearchEvalSearchProvider:
        return _SlackResearchEvalSearchProvider(results_by_marker)

    return build_provider


def _eval_source(source_id: str, title: str, url: str, claim: str) -> SourceRecord:
    return SourceRecord(
        source_id=source_id,
        title=title,
        url=url,
        source_type="company_site",
        supported_claims=[claim],
        evidence_excerpt=claim,
        confidence=0.85,
    )


def grade_slack_research_workflow(case: LocalEvalCase) -> LocalEvalResult:
    """Grade reusable Slack prompt selection plus offline multi-target research."""

    payload = case.input_payload
    expected = case.expected
    request_text = str(payload.get("request") or "")
    selection = resolve_slack_query_prompt(
        build_slack_query_prompt_input(
            raw_request=request_text,
            selected_message_text=str(payload.get("selected_message_text") or ""),
            thread_summary=str(payload.get("thread_summary") or ""),
            manual_plan=(
                payload.get("manual_plan")
                if isinstance(payload.get("manual_plan"), dict)
                else None
            ),
            target_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        )
    )
    if selection is None:
        observed = {"prompt_selected": False}
        return _result(case, ["slack query prompt was not selected"], observed)

    plan = MultiTargetResearchPlan(
        topic=str(payload.get("topic") or "three public AI companion or chatbot products"),
        desired_count=int(payload.get("desired_count") or 3),
        requested_dimensions=[
            str(item) for item in payload.get("requested_dimensions", []) if str(item).strip()
        ],
        request_text=request_text,
    )
    discovery_results = [
        SearchResult(
            title="Character.AI teen safety",
            link="https://character.ai/safety",
            snippet="Character.AI describes teen safety for its AI companion.",
            source="local_eval",
        ),
        SearchResult(
            title="ChatGPT safety protections",
            link="https://chatgpt.com/parent-resources/safety-protections/",
            snippet="ChatGPT describes teen safety and escalation protections.",
            source="local_eval",
        ),
        SearchResult(
            title="Replika crisis guidance",
            link="https://replika.com/safety",
            snippet="Replika describes escalation guidance for its AI companion.",
            source="local_eval",
        ),
        SearchResult(
            title="AP News reports on AI chatbot safety",
            link="https://apnews.com/article/openai-chatgpt-chatbot-ai-online-safety",
            snippet="AP News is a source about ChatGPT safety, not a product target.",
            source="local_eval",
        ),
    ]
    results_by_marker = {
        "official products": discovery_results,
        "product safety pages": discovery_results,
        "teen safety escalation": discovery_results,
    }

    def retrieve_profile(**kwargs: Any) -> tuple[CompanyProfile, dict[str, Any]]:
        company = str(kwargs["company"])
        source_map = {
            "Character.AI": _eval_source(
                "characterai:teen-safety",
                "Character.AI teen safety",
                "https://character.ai/safety/teen-safety",
                "Character.AI describes teen safety controls and escalation boundaries.",
            ),
            "ChatGPT": _eval_source(
                "chatgpt:safety",
                "ChatGPT safety protections",
                "https://chatgpt.com/parent-resources/safety-protections/",
                "ChatGPT describes teen safety protections and escalation safeguards.",
            ),
            "Replika": _eval_source(
                "replika:crisis",
                "Replika crisis guidance",
                "https://replika.com/safety",
                "Replika describes safety guidance and escalation to crisis resources.",
            ),
        }
        source = source_map[company]
        profile = research_company_fixture(company_name=company).model_copy(
            update={"sources": [source]}
        )
        return profile, {"retrieval_diagnostics": {"provider_summary": "local_eval"}}

    result = run_multi_target_research(
        plan,
        live_search=True,
        retrieval_hint=RetrievalHint(allow_deepening=False),
        search_provider_builder=_slack_research_eval_provider_builder(results_by_marker),
        retrieve_profile=retrieve_profile,
    )
    source_urls = [
        str(source.get("url") or "")
        for packet in result.packets
        for source in packet.source_refs
        if isinstance(source, dict) and source.get("url")
    ]
    target_selection = result.diagnostics.get("target_selection", {})
    observed = {
        "prompt_selected": True,
        "prompt_schema": selection.schema_,
        "prompt_version": selection.version,
        "prompt_kind": selection.kind.value,
        "target_route": selection.target_route.value,
        "context_flags": selection.context_flags,
        "task_brief": selection.task_brief,
        "dynamic_content_sha256_length": len(selection.dynamic_content_sha256),
        "comparison_ready": result.comparison_ready,
        "pass_types": result.pass_types,
        "selected_targets": result.selected_targets,
        "source_urls": source_urls,
        "ready_packet_count": result.diagnostics.get("ready_packet_count", 0),
        "weak_targets": target_selection.get("weak_targets", []),
        "top_candidates": [
            item.get("name")
            for item in target_selection.get("top_candidates", [])
            if isinstance(item, dict)
        ],
    }
    failures: list[str] = []
    for field in (
        "prompt_selected",
        "prompt_schema",
        "prompt_version",
        "prompt_kind",
        "target_route",
        "comparison_ready",
        "ready_packet_count",
    ):
        _expect_equal(failures, observed, expected, field)
    _expect_contains_all(
        failures,
        observed["pass_types"],
        expected.get("pass_types_contains", []),
        "pass_types",
    )
    _expect_contains_all(
        failures,
        observed["selected_targets"],
        expected.get("selected_targets_contains", []),
        "selected_targets",
    )
    forbidden_targets = {str(item) for item in expected.get("forbidden_targets", [])}
    bad_targets = sorted(forbidden_targets.intersection(set(observed["selected_targets"])))
    if bad_targets:
        failures.append(f"selected_targets: included forbidden targets {bad_targets!r}")
    if expected.get("require_context_flag_source_triage") and not observed["context_flags"].get(
        "needs_source_triage"
    ):
        failures.append("context_flags: missing needs_source_triage")
    if expected.get("require_dynamic_hash") and observed["dynamic_content_sha256_length"] != 64:
        failures.append("dynamic_content_sha256: expected 64 hex characters")
    if expected.get("require_visible_source_urls"):
        _expect_range(
            failures,
            len(observed["source_urls"]),
            field="source_urls",
            minimum=int(expected.get("min_source_urls", 1)),
        )
    for text in expected.get("task_brief_contains", []):
        if str(text) not in selection.task_brief:
            failures.append(f"task_brief: missing expected text {text!r}")
    return _result(case, failures, observed)


def grade_opportunity_scoring(case: LocalEvalCase) -> LocalEvalResult:
    expected = case.expected
    observed = json.loads(
        score_opportunity_impl(
            company_name=str(case.input_payload.get("company_name", "")),
            opportunity_type=str(case.input_payload.get("opportunity_type", "")),
            signals=[str(signal) for signal in case.input_payload.get("signals", [])],
        )
    )
    failures: list[str] = []
    _expect_range(
        failures,
        int(observed["priority_score"]),
        field="priority_score",
        minimum=expected.get("min_priority_score"),
        maximum=expected.get("max_priority_score"),
    )
    _expect_range(
        failures,
        int(observed["outside_consulting_likelihood"]),
        field="outside_consulting_likelihood",
        minimum=expected.get("min_outside_consulting_likelihood"),
        maximum=expected.get("max_outside_consulting_likelihood"),
    )
    _expect_equal(failures, observed, expected, "handoff_to_business_research_analyst")
    required_components = [str(item) for item in expected.get("required_score_components", [])]
    missing_components = [
        component
        for component in required_components
        if component not in observed.get("score_breakdown", {})
    ]
    if missing_components:
        failures.append(f"score_breakdown: missing components {missing_components!r}")
    return _result(case, failures, observed)


def grade_outreach_copy_constraints(case: LocalEvalCase) -> LocalEvalResult:
    mode = str(case.input_payload.get("mode", "fixture_draft"))
    expected = case.expected
    failures: list[str] = []

    if mode == "fixture_draft":
        contact_context = (
            load_contact_context(str(case.input_payload["contact_fixture"]))
            if "contact_fixture" in case.input_payload
            else None
        )
        crm_context = (
            load_crm_account_context(str(case.input_payload["crm_fixture"]))
            if "crm_fixture" in case.input_payload
            else None
        )
        style_profile = (
            load_style_profile(str(case.input_payload["style_fixture"]))
            if "style_fixture" in case.input_payload
            else case.input_payload.get("email_style_profile")
        )
        draft = compose_outreach_draft_fixture(
            company_profile=load_company_profile(
                str(case.input_payload.get("company_fixture", ""))
            ),
            opportunity_record=load_opportunity_record(
                str(case.input_payload.get("opportunity_fixture", ""))
            ),
            contact_name=case.input_payload.get("contact_name"),
            contact_title=case.input_payload.get("contact_title"),
            contact_context=contact_context,
            crm_context=crm_context,
            email_style_profile=style_profile,
            recent_signal=case.input_payload.get("recent_signal"),
            outreach_goal=case.input_payload.get("outreach_goal"),
            include_call_prep=bool(case.input_payload.get("include_call_prep", False)),
            include_follow_up_schedule=bool(
                case.input_payload.get("include_follow_up_schedule", False)
            ),
            follow_up_date=case.input_payload.get("follow_up_date"),
        )
        observed = {**draft.model_dump(), "approval_status": draft.approval_status}
        for field in (
            "approval_required",
            "approval_status",
            "approved_context_used",
            "send_enabled",
            "sent",
            "can_send_email",
            "style_profile_used",
            "style_profile_id",
        ):
            _expect_equal(failures, observed, expected, field)
        _expect_range(
            failures,
            len(draft.email_body.split()),
            field="email_body_words",
            maximum=expected.get("email_max_words"),
        )
        _expect_range(
            failures,
            len(draft.linkedin_note),
            field="linkedin_note_chars",
            maximum=expected.get("linkedin_max_chars"),
        )
        if expected.get("no_em_dash") and "\u2014" in json.dumps(observed, ensure_ascii=False):
            failures.append("outreach copy contains an em dash")
        if (
            "unsupported_claims_exact" in expected
            and draft.unsupported_claims_flagged != expected["unsupported_claims_exact"]
        ):
            _fail(
                failures,
                "unsupported_claims_flagged",
                draft.unsupported_claims_flagged,
                expected["unsupported_claims_exact"],
            )
        _expect_range(
            failures,
            len(draft.unsupported_claims_flagged),
            field="unsupported_claims_flagged",
            minimum=expected.get("unsupported_claims_min"),
        )
        _expect_range(
            failures,
            len(draft.facts_used),
            field="facts_used",
            minimum=expected.get("facts_used_min"),
        )
        _expect_range(
            failures,
            len(draft.source_ids_used),
            field="source_ids_used",
            minimum=expected.get("source_ids_used_min"),
        )
        _expect_range(
            failures,
            len(draft.unsupported_claim_explanations),
            field="unsupported_claim_explanations",
            minimum=expected.get("unsupported_claim_explanations_min"),
        )
        if (
            "unsupported_claim_explanations_exact" in expected
            and draft.unsupported_claim_explanations
            != expected["unsupported_claim_explanations_exact"]
        ):
            _fail(
                failures,
                "unsupported_claim_explanations",
                draft.unsupported_claim_explanations,
                expected["unsupported_claim_explanations_exact"],
            )
        observed["approved_context_used"] = draft.approved_context_used
        _expect_equal(failures, observed, expected, "approved_context_used")
        copy_text = "\n".join([draft.email_subject, draft.email_body, draft.linkedin_note]).lower()
        for required in expected.get("copy_must_contain", []):
            if str(required).lower() not in copy_text:
                failures.append(f"outreach copy missing required text {required!r}")
        for forbidden in expected.get("copy_must_not_contain", []):
            if str(forbidden).lower() in copy_text:
                failures.append(f"outreach copy contained forbidden text {forbidden!r}")
        for required in expected.get("style_required_terms", []):
            if str(required).lower() not in copy_text:
                failures.append(f"style: missing required text {required!r}")
        for forbidden in expected.get("style_forbidden_terms", []):
            if str(forbidden).lower() in copy_text:
                failures.append(f"style: contained forbidden text {forbidden!r}")
        schedules = [dict(item) for item in observed.get("follow_up_schedules") or []]
        _expect_range(
            failures,
            len(schedules),
            field="follow_up_schedules",
            minimum=expected.get("follow_up_schedules_min"),
        )
        if "follow_up_approval_required" in expected and not all(
            schedule.get("approval_required") is expected["follow_up_approval_required"]
            for schedule in schedules
        ):
            failures.append("follow_up_schedules: approval_required did not match")
        for field in ("send_enabled", "gmail_scheduled", "background_job_created"):
            expectation_key = f"follow_up_{field}" if field == "send_enabled" else field
            expected_schedule_value = expected.get(expectation_key)
            if (
                field == "send_enabled"
                and expectation_key not in expected
                and schedules
                and "send_enabled" in expected
            ):
                expected_schedule_value = expected["send_enabled"]
            if expected_schedule_value is not None and not all(
                schedule.get(field) is expected_schedule_value for schedule in schedules
            ):
                failures.append(f"follow_up_schedules: {field} did not match")

        call_prep = observed.get("call_prep") if isinstance(observed.get("call_prep"), dict) else {}
        if expected.get("call_prep_required") and not call_prep:
            failures.append("call_prep: expected call prep artifact")
        if call_prep:
            if "call_prep_draft_only_internal" in expected:
                _expect_equal(
                    failures,
                    call_prep,
                    {"draft_only_internal": expected["call_prep_draft_only_internal"]},
                    "draft_only_internal",
                )
            _expect_range(
                failures,
                len(call_prep.get("discovery_questions", [])),
                field="call_prep.discovery_questions",
                minimum=expected.get("discovery_questions_min"),
            )
            if expected.get("known_facts_source_backed"):
                invalid_sources = [
                    fact.get("source_id")
                    for fact in call_prep.get("known_facts", [])
                    if str(fact.get("source_id", "")).startswith(("user:", "unbacked:"))
                ]
                if invalid_sources:
                    failures.append(f"call_prep.known_facts: unbacked sources {invalid_sources!r}")
            if expected.get("no_professional_advice"):
                call_prep_text = json.dumps(call_prep, ensure_ascii=False).lower()
                advice_terms = ("diagnose", "prescribe", "treatment plan", "medical advice")
                if any(term in call_prep_text for term in advice_terms):
                    failures.append("call_prep: contains professional advice wording")
        return _result(case, failures, observed)

    if mode == "claim_check":
        observed = check_unsupported_claims(
            str(case.input_payload.get("text", "")),
            allowed_claims=[str(claim) for claim in case.input_payload.get("allowed_claims", [])],
        )
        _expect_equal(failures, observed, expected, "has_unsupported_claims")
        _expect_range(
            failures,
            len(observed["unsupported_claims"]),
            field="unsupported_claims",
            minimum=expected.get("unsupported_claims_min"),
        )
        return _result(case, failures, observed)

    raise ValueError(f"{case.case_id}: unsupported outreach_copy_constraints mode {mode!r}")


def grade_approval_queue_revision(case: LocalEvalCase) -> LocalEvalResult:
    expected = case.expected
    payload = case.input_payload
    store = SQLiteStore(":memory:")
    item = ApprovalQueueItem(
        id=str(payload.get("approval_id") or case.case_id),
        object_type=str(payload.get("object_type") or "outreach_draft"),
        object_id=str(payload.get("object_id") or "draft-1"),
        title=str(payload.get("title") or "Outreach draft needs review"),
        summary=str(payload.get("summary") or "Draft remains approval-gated."),
        draft_text=str(payload.get("draft_text") or "Draft-only outreach body."),
        source_agent=str(payload.get("source_agent") or "outreach_composer"),
        risk_flags=[str(flag) for flag in payload.get("risk_flags", [])],
        approval_status="pending",
    )
    store.save_approval_item(item)
    revised = store.update_approval_status(
        item.id,
        "revise",
        reviewer=str(payload.get("reviewer") or "Human Reviewer"),
        notes=str(payload.get("revision_notes") or "Revise unsupported claim."),
    )
    reopened = store.update_approval_status(
        item.id,
        "pending",
        reviewer=str(payload.get("reviewer") or "Human Reviewer"),
        notes=str(payload.get("requeue_notes") or "Requeued after revision."),
    )
    observed = {
        "initial_status": item.approval_status.value,
        "revision_status": revised.approval_status.value,
        "reopened_status": reopened.approval_status.value,
        "reviewer": reopened.reviewer,
        "reviewer_notes": reopened.reviewer_notes,
        "send_enabled": approval_queue_status_allows_sending(reopened.approval_status),
        "draft_text": reopened.draft_text,
    }
    failures: list[str] = []
    for field in (
        "initial_status",
        "revision_status",
        "reopened_status",
        "reviewer",
        "send_enabled",
    ):
        _expect_equal(failures, observed, expected, field)
    if "reviewer_notes_contains" in expected:
        reviewer_notes = str(observed.get("reviewer_notes") or "")
        if str(expected["reviewer_notes_contains"]) not in reviewer_notes:
            failures.append(
                f"reviewer_notes: missing expected text {expected['reviewer_notes_contains']!r}"
            )
    for forbidden in expected.get("draft_must_not_contain", []):
        if str(forbidden).lower() in str(observed.get("draft_text") or "").lower():
            failures.append(f"draft_text: contained forbidden text {forbidden!r}")
    return _result(case, failures, observed)


def grade_chief_of_staff(case: LocalEvalCase) -> LocalEvalResult:
    expected = case.expected
    request = str(case.input_payload.get("request") or "")
    result = plan_chief_of_staff_request(request)
    observed = result.model_dump(mode="json")
    failures: list[str] = []

    for field in (
        "mode",
        "summary",
        "approval_required",
        "human_review_required",
        "send_enabled",
        "slack_post_allowed",
        "slack_post_policy",
    ):
        _expect_equal(failures, observed, expected, field)
    route = observed.get("recommended_route")
    if isinstance(route, Mapping):
        for field in ("workflow_type", "target_channel", "requires_live_connector"):
            expected_key = f"recommended_route_{field}"
            if expected_key in expected and route.get(field) != expected[expected_key]:
                _fail(failures, expected_key, route.get(field), expected[expected_key])
    else:
        failures.append("recommended_route: missing route recommendation")
    _expect_contains_all(
        failures,
        observed.get("operating_capabilities", []),
        expected.get("operating_capabilities_contains", []),
        "operating_capabilities",
    )
    _expect_contains_all(
        failures,
        observed.get("blocked_side_effects", []),
        expected.get("blocked_side_effects_contains", []),
        "blocked_side_effects",
    )
    _expect_contains_all(
        failures,
        observed.get("context_sources_considered", []),
        expected.get("context_sources_contains", []),
        "context_sources_considered",
    )
    _expect_range(
        failures,
        len(observed.get("sources", [])),
        field="sources",
        minimum=expected.get("min_sources"),
    )
    if expected.get("require_source_urls") and not all(
        source.get("url") for source in observed.get("sources", []) if isinstance(source, Mapping)
    ):
        failures.append("sources: each source must include a URL")
    if expected.get("write_requests_empty") and observed.get("write_requests"):
        failures.append("write_requests: expected no write requests")
    return _result(case, failures, observed)


def grade_skill_gate_failures(case: LocalEvalCase) -> LocalEvalResult:
    expected = case.expected
    gate = _skill_gate_failure_case(case)
    observed = gate.to_dict()
    failures: list[str] = []
    for field in ("gate_id", "status", "domain"):
        _expect_equal(failures, observed, expected, field)
    _expect_contains_all(
        failures,
        observed.get("eval_labels", []),
        expected.get("eval_labels_contains", []),
        "eval_labels",
    )
    _expect_contains_all(
        failures,
        observed.get("skill_ids", []),
        expected.get("skill_ids_contains", []),
        "skill_ids",
    )
    if "hard_gate" in expected:
        _expect_equal(failures, observed, expected, "hard_gate")
    return _result(case, failures, observed)


def _skill_gate_failure_case(case: LocalEvalCase):
    mode = str(case.input_payload.get("mode", ""))
    if mode == "business_research_missing_sources":
        item = WorkItem(
            kind=WorkItemKind.COMPANY_RESEARCH,
            title="Research missing sources",
            current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            artifact_refs=[
                WorkItemArtifactRef(
                    artifact_type="company_profile",
                    artifact_id="company-1",
                    source_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                    metadata={},
                )
            ],
        )
        return check_business_research_claim_gate(item)
    if mode == "business_research_source_blocker":
        item = WorkItem(
            kind=WorkItemKind.COMPANY_RESEARCH,
            title="Research blocked",
            current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            blockers=[
                WorkItemBlocker(
                    code="source_bundle_required",
                    message="Source bundle required before claim synthesis.",
                )
            ],
        )
        return check_business_research_claim_gate(item)
    if mode == "outreach_send_flag":
        item = WorkItem(
            kind=WorkItemKind.OUTREACH,
            title="Outreach unsafe send flag",
            current_route=WorkItemRoute.OUTREACH_COMPOSER,
            artifact_refs=[
                WorkItemArtifactRef(
                    artifact_type="outreach_draft",
                    artifact_id="draft-1",
                    source_agent=WorkItemRoute.OUTREACH_COMPOSER.value,
                    metadata={"send_enabled": True},
                )
            ],
            approval_gates=[WorkItemApprovalGate(scope="external_use", state="pending")],
        )
        return check_outreach_approval_claim_gate(item)
    if mode == "gmail_sensitive_missing_flags":
        item = WorkItem(
            kind=WorkItemKind.GMAIL_THREAD,
            title="Gmail sensitive missing flags",
            request_text="Payment attachment and credential reset link.",
            current_route=WorkItemRoute.GMAIL_TRIAGE,
            artifact_refs=[
                WorkItemArtifactRef(
                    artifact_type="gmail_triage_report",
                    artifact_id="triage-1",
                    source_agent=WorkItemRoute.GMAIL_TRIAGE.value,
                    metadata={"send_enabled": False},
                )
            ],
        )
        result = WorkflowRunResult(
            work_item=item,
            route=WorkItemRoute.GMAIL_TRIAGE,
            status=WorkItemStatus.DONE,
            advanced=True,
            artifact_refs=list(item.artifact_refs),
        )
        return check_gmail_sensitive_message_gate(
            item,
            result=result,
            request_text=item.request_text,
        )
    if mode == "chief_publish_without_approval":
        item = WorkItem(
            kind=WorkItemKind.WEEKLY_SCAN,
            title="Chief publish unsafe",
            current_route=WorkItemRoute.CHIEF_OF_STAFF,
            artifact_refs=[
                WorkItemArtifactRef(
                    artifact_type="chief_of_staff_plan",
                    artifact_id="chief-1",
                    source_agent=WorkItemRoute.CHIEF_OF_STAFF.value,
                    metadata={"slack_post_allowed": True},
                )
            ],
        )
        result = WorkflowRunResult(
            work_item=item,
            route=WorkItemRoute.CHIEF_OF_STAFF,
            status=WorkItemStatus.IN_PROGRESS,
            advanced=True,
            artifact_refs=list(item.artifact_refs),
        )
        return check_chief_artifact_publish_gate(item, result=result)
    raise ValueError(f"{case.case_id}: unsupported skill_gate_failures mode {mode!r}")


GRADERS: dict[str, Callable[[LocalEvalCase], LocalEvalResult]] = {
    "chief_of_staff": grade_chief_of_staff,
    "gmail_triage": grade_gmail_triage,
    "orchestrator_routing": grade_orchestrator_routing,
    "safety_refusal": grade_safety_refusal,
    "source_attribution": grade_source_attribution,
    "slack_research_workflow": grade_slack_research_workflow,
    "opportunity_scoring": grade_opportunity_scoring,
    "outreach_copy_constraints": grade_outreach_copy_constraints,
    "approval_queue_revision": grade_approval_queue_revision,
    "skill_gate_failures": grade_skill_gate_failures,
}


def resolve_eval_paths(eval_dir: Path, dataset_names: Sequence[str] = ()) -> list[Path]:
    if dataset_names:
        paths: list[Path] = []
        for name in dataset_names:
            raw_path = Path(name)
            if raw_path.suffix != ".jsonl":
                raw_path = raw_path.with_suffix(".jsonl")
            path = raw_path if raw_path.is_absolute() else eval_dir / raw_path
            if not path.is_file():
                raise FileNotFoundError(f"Eval dataset not found: {path}")
            paths.append(path)
        return paths

    paths = [
        path
        for path in sorted(eval_dir.glob("*.jsonl"))
        if path.name not in SPECIALIZED_EVAL_DATASETS
    ]
    if not paths:
        raise FileNotFoundError(f"No eval JSONL datasets found in {eval_dir}")
    return paths


def run_cases(cases: Iterable[LocalEvalCase]) -> list[LocalEvalResult]:
    results: list[LocalEvalResult] = []
    for case in cases:
        grader = GRADERS.get(case.task)
        if grader is None:
            results.append(
                _result(
                    case,
                    [f"unknown task {case.task!r}"],
                    {"task": case.task, "line_number": case.line_number},
                )
            )
            continue
        results.append(grader(case))
    return results


def _dataset_stem(value: str) -> str:
    return Path(value).stem


def _skill_task_matrix_requested(dataset_names: Sequence[str]) -> bool:
    return not dataset_names or any(
        _dataset_stem(dataset_name) == SKILL_TASK_MATRIX_DATASET for dataset_name in dataset_names
    )


def _standard_dataset_names(dataset_names: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        dataset_name
        for dataset_name in dataset_names
        if _dataset_stem(dataset_name) != SKILL_TASK_MATRIX_DATASET
    )


def _skill_task_result_to_local(result: SkillTaskEvalResult) -> LocalEvalResult:
    spec = AGENT_REGISTRY[result.agent]
    prompt_metadata = prompt_metadata_for_files(spec.prompt_files)
    return LocalEvalResult(
        dataset=SKILL_TASK_MATRIX_DATASET,
        case_id=result.case_id,
        task=SKILL_TASK_MATRIX_DATASET,
        passed=result.passed,
        failures=list(result.failures),
        observed=result.to_dict(),
        prompt_metadata=prompt_metadata,
        validates_prompts=tuple(prompt_version_references(spec.prompt_files)),
        validates_skills=result.selected_skills,
        surface=result.surface,
    )


def run_local_evals(
    *,
    eval_dir: Path = DEFAULT_EVAL_DIR,
    dataset_names: Sequence[str] = (),
) -> LocalEvalSummary:
    cases: list[LocalEvalCase] = []
    standard_dataset_names = _standard_dataset_names(dataset_names)
    if standard_dataset_names or not dataset_names:
        for path in resolve_eval_paths(eval_dir, standard_dataset_names):
            cases.extend(_load_jsonl_cases(path))
    results = run_cases(cases)
    if _skill_task_matrix_requested(dataset_names):
        skill_matrix_path = eval_dir / DEFAULT_SKILL_TASK_MATRIX.name
        skill_summary = run_skill_task_eval_suite(skill_matrix_path)
        results.extend(_skill_task_result_to_local(result) for result in skill_summary.results)
    return LocalEvalSummary(results=results)


def format_summary(summary: LocalEvalSummary) -> str:
    lines = [
        "Local evals",
        f"Total: {summary.total}",
        f"Passed: {summary.passed}",
        f"Failed: {summary.failed}",
    ]
    prompt_refs = [str(metadata["reference"]) for metadata in summary.to_dict()["prompt_metadata"]]
    if prompt_refs:
        lines.append(f"Prompt versions: {', '.join(prompt_refs)}")
    for result in summary.results:
        status = "PASS" if result.passed else "FAIL"
        versions = ", ".join(str(metadata["reference"]) for metadata in result.prompt_metadata)
        lines.append(f"{status} {result.dataset}/{result.case_id} [{versions}]")
        for failure in result.failures:
            lines.append(f"  - {failure}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run deterministic local Keystone evals.")
    parser.add_argument(
        "--eval-dir",
        default=str(DEFAULT_EVAL_DIR),
        help="Directory containing local eval JSONL datasets.",
    )
    parser.add_argument(
        "--dataset",
        action="append",
        default=[],
        help="Dataset name or JSONL path. Can be passed more than once.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary.")
    parser.add_argument(
        "--record-benchmark",
        action="store_true",
        help="Record this eval run in the local benchmark SQLite store.",
    )
    parser.add_argument(
        "--benchmark-db",
        default=None,
        help=(
            "Optional benchmark SQLite path. Defaults to KEYSTONE_BENCHMARK_DB or .keystone/state."
        ),
    )
    parser.add_argument(
        "--benchmark-label",
        default="",
        help="Optional label for comparing benchmark runs over time.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_local_evals(eval_dir=Path(args.eval_dir), dataset_names=args.dataset)
    if args.json:
        print(json.dumps(summary.to_dict(), ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(format_summary(summary))
    if args.record_benchmark:
        record = record_eval_summary(
            summary.to_dict(),
            suite="local",
            db_path=args.benchmark_db,
            run_label=args.benchmark_label,
            entrypoint="scripts/run_local_evals.py",
            metadata={"datasets": args.dataset or ["all"]},
        )
        print(f"Recorded benchmark run {record.run_id} in {record.db_path}", file=sys.stderr)
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
