"""Deterministic static evals for Keystone specialist agents."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from keystone_agents.agents.gmail_triage import load_email_fixture, triage_email_fixture
from keystone_agents.agents.opportunity_scout import scout_opportunities_fixture
from keystone_agents.agents.outreach_composer import (
    build_approved_outreach_drafting_context,
    compose_outreach_draft_fixture,
    compose_outreach_draft_llm_constrained,
    load_company_profile,
    load_contact_context,
    load_crm_account_context,
    load_opportunity_record,
    load_style_profile,
)
from keystone_agents.company_research import research_company_fixture
from keystone_agents.sdk import prompt_metadata_for_files, prompt_version_references

EvalAgent = Literal["all", "gmail", "company", "scout", "outreach"]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EVAL_DIR = PROJECT_ROOT / "evals" / "static"

AGENT_CASE_FILES: dict[str, str] = {
    "gmail": "gmail_triage_cases.json",
    "company": "business_research_analyst_cases.json",
    "scout": "opportunity_scout_cases.json",
    "outreach": "outreach_composer_cases.json",
}

AGENT_PROMPT_FILES: dict[str, tuple[str, ...]] = {
    "gmail": ("keystone_profile.md", "gmail_triage.md"),
    "company": ("keystone_profile.md", "safety_policy.md", "business_research_analyst.md"),
    "scout": ("keystone_profile.md", "safety_policy.md", "opportunity_scout.md"),
    "outreach": ("keystone_profile.md", "outreach_composer.md"),
}

CHECK_SKILL_LABELS: dict[str, tuple[str, ...]] = {
    "source_attribution": ("evidence_attribution_and_claim_mapping",),
    "no_hallucinated_facts": (
        "evidence_attribution_and_claim_mapping",
        "unsupported_claim_and_gap_handling",
    ),
    "no_unsupported_claims": ("unsupported_claim_and_gap_handling",),
    "unsupported_claims_min": ("unsupported_claim_and_gap_handling",),
    "unsupported_claim_explanations": ("unsupported_claim_and_gap_handling",),
    "no_auto_send": ("action_boundary_enforcement",),
    "approval_required": ("context_permission_gating", "action_boundary_enforcement"),
    "approval_status": ("context_permission_gating",),
    "approved_context_used": ("context_permission_gating",),
    "source_ids_used": ("evidence_attribution_and_claim_mapping",),
    "facts_used": ("evidence_attribution_and_claim_mapping",),
    "risk_flag_correctness": ("gmail_triage_specialist_contracts",),
    "draft_quality": ("gmail_triage_specialist_contracts", "action_boundary_enforcement"),
    "category_correctness": ("gmail_triage_specialist_contracts",),
    "needs_reply_correctness": ("gmail_triage_specialist_contracts",),
    "priority_score": ("opportunity_scout_specialist_contracts",),
    "outside_consulting_likelihood": ("opportunity_scout_specialist_contracts",),
    "score_breakdown": ("opportunity_scout_specialist_contracts",),
    "duplicate_state_skip": ("prior_work_and_duplicate_checking",),
    "unique_companies": ("identity_and_record_resolution",),
    "ranking_quality": ("opportunity_scout_specialist_contracts",),
    "record_count": ("opportunity_scout_specialist_contracts",),
    "copy_required_terms": ("outreach_composer_specialist_contracts",),
    "copy_forbidden_terms": ("outreach_composer_specialist_contracts",),
    "tone": ("outreach_composer_specialist_contracts",),
    "email_concision": ("outreach_composer_specialist_contracts",),
    "linkedin_concision": ("outreach_composer_specialist_contracts",),
    "no_em_dashes": ("structured_output_quality_review",),
}


@dataclass(frozen=True)
class EvalCheck:
    name: str
    passed: bool
    score: float
    message: str = ""
    skill_labels: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "score": self.score,
            "message": self.message,
            "skill_labels": list(self.skill_labels),
        }


@dataclass(frozen=True)
class EvalScore:
    agent: str
    score: float
    passed: bool
    checks: list[EvalCheck] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        skill_labels = sorted(
            {label for check in self.checks for label in check.skill_labels if str(label).strip()}
        )
        return {
            "agent": self.agent,
            "score": self.score,
            "passed": self.passed,
            "skill_labels": skill_labels,
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass(frozen=True)
class EvalCaseResult:
    case_id: str
    dataset: str
    agent: str
    score: float
    passed: bool
    checks: list[EvalCheck]
    observed: dict[str, Any]
    prompt_metadata: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        skill_labels = sorted(
            {label for check in self.checks for label in check.skill_labels if str(label).strip()}
        )
        return {
            "id": self.case_id,
            "dataset": self.dataset,
            "agent": self.agent,
            "score": self.score,
            "passed": self.passed,
            "skill_labels": skill_labels,
            "checks": [check.to_dict() for check in self.checks],
            "observed": self.observed,
            "prompt_metadata": self.prompt_metadata,
            "prompt_versions": [str(metadata["reference"]) for metadata in self.prompt_metadata],
        }


@dataclass(frozen=True)
class EvalSuiteResult:
    results: list[EvalCaseResult]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for result in self.results if result.passed)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def average_score(self) -> float:
        if not self.results:
            return 0.0
        return round(sum(result.score for result in self.results) / len(self.results), 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "average_score": self.average_score,
            "datasets": sorted({result.dataset for result in self.results}),
            "skill_labels": sorted(
                {
                    label
                    for result in self.results
                    for check in result.checks
                    for label in check.skill_labels
                    if str(label).strip()
                }
            ),
            "prompt_metadata": _unique_prompt_metadata(self.results),
            "results": [result.to_dict() for result in self.results],
        }


def run_static_evals(
    agent: EvalAgent = "all",
    *,
    eval_dir: str | Path = DEFAULT_EVAL_DIR,
) -> EvalSuiteResult:
    """Run deterministic fixture-backed evals for one agent or all agents."""

    eval_root = Path(eval_dir)
    results: list[EvalCaseResult] = []
    for case in _load_eval_cases(agent, eval_root):
        case_agent = str(case.get("agent") or agent)
        output = _run_fixture_case(case_agent, case)
        score = score_output_against_expected(
            output,
            case.get("expected", {}),
            agent=case_agent,
        )
        results.append(
            EvalCaseResult(
                case_id=str(case["id"]),
                dataset=str(case["_dataset"]),
                agent=case_agent,
                score=score.score,
                passed=score.passed,
                checks=score.checks,
                observed=_plain_data(output),
                prompt_metadata=_prompt_metadata_for_agent(case_agent),
            )
        )
    return EvalSuiteResult(results=results)


def score_output_against_expected(
    output: Any,
    expected: Mapping[str, Any],
    *,
    agent: str,
) -> EvalScore:
    """Score one observed output against expected deterministic eval criteria."""

    observed = _plain_data(output)
    if agent == "gmail":
        checks = _score_gmail(observed, expected)
    elif agent == "company":
        checks = _score_company(observed, expected)
    elif agent == "scout":
        checks = _score_scout(observed, expected)
    elif agent == "outreach":
        checks = _score_outreach(observed, expected)
    else:
        checks = [_check("agent_supported", False, f"unknown eval agent: {agent}")]

    score = round(sum(check.score for check in checks) / len(checks), 3) if checks else 0.0
    return EvalScore(
        agent=agent,
        score=score,
        passed=all(check.passed for check in checks),
        checks=checks,
    )


def generate_eval_report(summary: EvalSuiteResult | Mapping[str, Any]) -> str:
    """Render a compact markdown report for static eval results."""

    data = summary.to_dict() if isinstance(summary, EvalSuiteResult) else dict(summary)
    results = data.get("results", [])
    lines = [
        "# Keystone Static Eval Report",
        "",
        f"- Total cases: {data.get('total', 0)}",
        f"- Passed: {data.get('passed', 0)}",
        f"- Failed: {data.get('failed', 0)}",
        f"- Average score: {data.get('average_score', 0.0)}",
    ]
    if data.get("datasets"):
        lines.append(f"- Datasets: {', '.join(data['datasets'])}")
    if data.get("skill_labels"):
        lines.append(f"- Skill labels: {', '.join(data['skill_labels'])}")
    if data.get("prompt_metadata"):
        prompt_refs = [metadata["reference"] for metadata in data["prompt_metadata"]]
        lines.append(f"- Prompt versions: {', '.join(prompt_refs)}")
    for result in results:
        status = "PASS" if result.get("passed") else "FAIL"
        prompt_versions = ", ".join(result.get("prompt_versions", [])) or "not recorded"
        lines.extend(
            [
                "",
                f"## {status} {result.get('agent')}/{result.get('id')}",
                "",
                f"- Dataset: {result.get('dataset')}",
                f"- Prompt versions: {prompt_versions}",
                f"- Score: {result.get('score')}",
            ]
        )
        for check in result.get("checks", []):
            check_status = "pass" if check.get("passed") else "fail"
            message = f": {check.get('message')}" if check.get("message") else ""
            labels = ", ".join(check.get("skill_labels", []))
            label_text = f" [{labels}]" if labels else ""
            lines.append(f"- {check_status} `{check.get('name')}`{label_text}{message}")
    return "\n".join(lines)


def _load_eval_cases(agent: EvalAgent, eval_root: Path) -> list[dict[str, Any]]:
    agents = list(AGENT_CASE_FILES) if agent == "all" else [agent]
    cases: list[dict[str, Any]] = []
    for agent_name in agents:
        filename = AGENT_CASE_FILES.get(agent_name)
        if filename is None:
            raise ValueError(f"agent must be one of: all, {', '.join(AGENT_CASE_FILES)}")
        path = eval_root / filename
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"{path} must contain a JSON array of eval cases")
        for index, row in enumerate(data, start=1):
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{index} eval case must be an object")
            if not row.get("id"):
                raise ValueError(f"{path}:{index} eval case missing id")
            row.setdefault("agent", agent_name)
            row["_dataset"] = _dataset_label(path)
            cases.append(row)
    return cases


def _dataset_label(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _prompt_metadata_for_agent(agent: str) -> list[dict[str, Any]]:
    prompt_files = AGENT_PROMPT_FILES.get(agent, ())
    return prompt_metadata_for_files(prompt_files)


def _unique_prompt_metadata(results: list[EvalCaseResult]) -> list[dict[str, Any]]:
    by_reference: dict[str, dict[str, Any]] = {}
    for result in results:
        for metadata in result.prompt_metadata:
            by_reference[str(metadata["reference"])] = metadata
    return [by_reference[reference] for reference in sorted(by_reference)]


def eval_prompt_versions_for_agent(agent: str) -> list[str]:
    """Return compact prompt version references for a static eval agent."""

    prompt_files = AGENT_PROMPT_FILES.get(agent, ())
    return prompt_version_references(prompt_files)


def _run_fixture_case(agent: str, case: Mapping[str, Any]) -> Any:
    payload = _mapping(case.get("input"))
    if agent == "gmail":
        fixture = payload.get("fixture")
        email = load_email_fixture(
            _project_path(str(fixture)) if fixture else None,
            subject=str(payload.get("subject", "")),
            sender_name=str(payload.get("sender_name", "")),
            sender_email=str(payload.get("sender_email", "")),
        )
        return triage_email_fixture(email)

    if agent == "company":
        fixture = payload.get("fixture")
        return research_company_fixture(
            company_name=str(payload.get("company_name", "")),
            company_url=_optional_str(payload.get("company_url")),
            fixture_json=_project_path(str(fixture)) if fixture else None,
        )

    if agent == "scout":
        fixture = payload.get("fixture")
        existing_state = payload.get("existing_state")
        return scout_opportunities_fixture(
            fixture=_project_path(str(fixture)) if fixture else None,
            topic=_optional_str(payload.get("topic")),
            max_results=int(payload.get("max_results", 5)),
            dry_run=True,
            existing_state=_project_path(str(existing_state)) if existing_state else None,
        )

    if agent == "outreach":
        company_profile = load_company_profile(str(payload.get("company_fixture", "")))
        opportunity_fixture = payload.get("opportunity_fixture")
        opportunity_record = (
            load_opportunity_record(str(opportunity_fixture)) if opportunity_fixture else None
        )
        contact_context = (
            load_contact_context(str(payload["contact_fixture"]))
            if "contact_fixture" in payload
            else None
        )
        crm_context = (
            load_crm_account_context(str(payload["crm_fixture"]))
            if "crm_fixture" in payload
            else None
        )
        style_profile = (
            load_style_profile(str(payload["style_fixture"]))
            if "style_fixture" in payload
            else payload.get("email_style_profile")
        )
        if payload.get("mode") == "llm_constrained":
            approved_context = build_approved_outreach_drafting_context(
                company_profile=company_profile,
                opportunity_record=opportunity_record,
                contact_context=contact_context,
                crm_context=crm_context,
                email_style_profile=style_profile,
                objective=_optional_str(payload.get("outreach_goal")),
                blocked_facts=[str(item) for item in _list(payload.get("blocked_facts"))],
                revision_request=_optional_str(payload.get("revision_request")),
            )
            return compose_outreach_draft_llm_constrained(
                approved_context=approved_context,
                llm_draft_payload=payload.get("llm_draft_payload"),
            )
        return compose_outreach_draft_fixture(
            company_profile=company_profile,
            opportunity_record=opportunity_record,
            contact_name=_optional_str(payload.get("contact_name")),
            contact_title=_optional_str(payload.get("contact_title")),
            contact_context=contact_context,
            crm_context=crm_context,
            email_style_profile=style_profile,
            recent_signal=_optional_str(payload.get("recent_signal")),
            outreach_goal=_optional_str(payload.get("outreach_goal")),
            include_call_prep=bool(payload.get("include_call_prep", False)),
            include_follow_up_schedule=bool(payload.get("include_follow_up_schedule", False)),
            follow_up_date=_optional_str(payload.get("follow_up_date")),
        )

    raise ValueError(f"unknown eval agent: {agent}")


def _score_gmail(observed: Mapping[str, Any], expected: Mapping[str, Any]) -> list[EvalCheck]:
    draft = str(observed.get("draft_reply") or "")
    draft_quality = _mapping(expected.get("draft_quality"))
    checks = [
        _exact("category_correctness", observed.get("category"), expected.get("category")),
        _exact("needs_reply_correctness", observed.get("needs_reply"), expected.get("needs_reply")),
        _risk_flags_check(observed, expected),
        _no_auto_send_check(observed),
        _draft_quality_check(draft, expected, draft_quality),
    ]
    return checks


def _score_company(observed: Mapping[str, Any], expected: Mapping[str, Any]) -> list[EvalCheck]:
    sources = _list_of_mappings(observed.get("sources"))
    source_ids = {str(source.get("source_id")) for source in sources}
    claims = _list_of_mappings(observed.get("claims"))
    next_action = _recommended_company_action(observed)
    text = _json_text(observed)
    source_quality = _mapping(observed.get("source_quality_summary"))
    research_completeness = _mapping(observed.get("research_completeness"))

    checks = [
        _source_attribution_check(sources, expected),
        _range_check(
            "fit_score_plausibility",
            int(observed.get("consulting_fit_score") or 0),
            expected.get("fit_score_min"),
            expected.get("fit_score_max"),
        ),
        _no_hallucinated_facts_check(
            text,
            sources,
            claims,
            source_ids,
            observed.get("unsupported_claims_flagged", []),
            expected,
        ),
        _contains_terms_check(
            "recommended_next_action_quality",
            next_action,
            expected.get("recommended_action_contains", []),
        ),
        _range_check(
            "confidence_score",
            float(observed.get("confidence_score") or 0),
            expected.get("confidence_score_min"),
            expected.get("confidence_score_max"),
        ),
        _range_check(
            "source_recency",
            int(source_quality.get("average_recency_score") or 0),
            expected.get("source_recency_min"),
            expected.get("source_recency_max"),
        ),
        _range_check(
            "research_completeness",
            int(research_completeness.get("score") or 0),
            expected.get("research_completeness_min"),
            expected.get("research_completeness_max"),
        ),
    ]
    return checks


def _scout_strategic_fit_check(
    top: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> EvalCheck:
    specification = _mapping(expected.get("strategic_fit"))
    if not specification:
        return _contains_terms_check(
            "strategic_fit",
            str(top.get("keystone_fit_reason", "")),
            expected.get("strategic_fit_terms", []),
        )

    expected_types = {
        str(value).strip().casefold()
        for value in _list(specification.get("opportunity_types"))
        if str(value).strip()
    }
    required_signals = {
        str(value).strip().casefold()
        for value in _list(specification.get("required_signals"))
        if str(value).strip()
    }
    observed_type = str(top.get("opportunity_type") or "").strip().casefold()
    observed_signals = {
        str(value).strip().casefold()
        for value in _list(top.get("source_signals"))
        if str(value).strip()
    }
    rationale = str(top.get("keystone_fit_reason") or "").strip()
    source_confidence = int(
        _mapping(top.get("source_quality_summary")).get("overall_score") or 0
    )
    minimum_confidence = specification.get("source_confidence_min")
    missing_signals = sorted(required_signals - observed_signals)
    failures: list[str] = []
    if not rationale:
        failures.append("empty fit rationale")
    if expected_types and observed_type not in expected_types:
        failures.append(f"unexpected opportunity type: {observed_type or 'missing'}")
    if missing_signals:
        failures.append(f"missing supported signals: {missing_signals!r}")
    if minimum_confidence is not None and source_confidence < int(minimum_confidence):
        failures.append(
            f"source confidence {source_confidence} below {int(minimum_confidence)}"
        )
    return _check("strategic_fit", not failures, "; ".join(failures))


def _score_scout(observed: Mapping[str, Any], expected: Mapping[str, Any]) -> list[EvalCheck]:
    records = _list_of_mappings(observed.get("records"))
    top = records[0] if records else {}
    names = [str(record.get("company_name", "")) for record in records]
    top_quality = _mapping(top.get("source_quality_summary"))
    top_bundles = _list_of_mappings(top.get("source_bundles"))
    top_bundle_categories = [str(bundle.get("source_category", "")) for bundle in top_bundles]
    checks = [
        _exact("top_company", top.get("company_name"), expected.get("top_company")),
        _scout_strategic_fit_check(top, expected),
        _contains_terms_check(
            "why_now_signal_quality",
            str(top.get("why_now_signal", "")),
            expected.get("why_now_terms", []),
        ),
        _range_check(
            "outside_consulting_likelihood",
            int(top.get("outside_consulting_likelihood") or 0),
            expected.get("outside_consulting_likelihood_min"),
            expected.get("outside_consulting_likelihood_max"),
        ),
        _range_check(
            "priority_score",
            int(top.get("priority_score") or 0),
            expected.get("priority_score_min"),
            expected.get("priority_score_max"),
        ),
        _score_breakdown_check(top, expected),
        _exact(
            "approval_required",
            top.get("approval_required_before_outreach"),
            expected.get("approval_required_before_outreach"),
        ),
        _check(
            "no_outreach_generation",
            not bool(observed.get("outreach_generated"))
            and not any(record.get("outreach_draft") for record in records),
        ),
        _ranking_check(names, expected.get("ranking_order", [])),
        _unique_companies_check(names, expected),
        _record_count_check(records, expected),
        _all_records_range_check(
            records,
            "priority_score",
            "all_priority_score",
            expected,
        ),
        _all_records_exact_check(
            records,
            "handoff_to_business_research_analyst",
            expected.get("all_handoff_to_business_research_analyst"),
        ),
    ]
    if expected.get("source_confidence_max") is not None:
        checks.append(
            _range_check(
                "source_confidence_max",
                int(top_quality.get("overall_score") or 0),
                None,
                expected.get("source_confidence_max"),
            )
        )
    if expected.get("stale_signal_count_min") is not None:
        checks.append(
            _range_check(
                "stale_signal_count",
                int(top.get("stale_signal_count") or 0),
                expected.get("stale_signal_count_min"),
                None,
            )
        )
    if expected.get("missing_evidence_terms"):
        checks.append(
            _contains_terms_check(
                "missing_evidence_quality",
                " ".join(str(item) for item in _list(top.get("missing_evidence"))),
                expected.get("missing_evidence_terms", []),
            )
        )
    expected_bundle_categories = [
        str(category) for category in expected.get("source_bundle_categories", [])
    ]
    if expected_bundle_categories:
        checks.append(
            _check(
                "source_bundle_categories",
                set(expected_bundle_categories).issubset(set(top_bundle_categories)),
                f"observed={top_bundle_categories!r}, expected={expected_bundle_categories!r}",
            )
        )
    skipped = [str(company) for company in observed.get("duplicate_companies_skipped", [])]
    expected_skipped = [
        str(company) for company in expected.get("duplicate_companies_skipped_contains", [])
    ]
    if expected_skipped:
        checks.append(
            _check(
                "duplicate_state_skip",
                set(expected_skipped).issubset(set(skipped)),
                f"observed={skipped!r}, expected={expected_skipped!r}",
            )
        )
    if expected.get("state_action"):
        checks.append(_exact("state_action", top.get("state_action"), expected.get("state_action")))
    return checks


def _score_outreach(observed: Mapping[str, Any], expected: Mapping[str, Any]) -> list[EvalCheck]:
    subject = str(observed.get("email_subject") or observed.get("subject") or "")
    body = str(observed.get("email_body") or observed.get("body") or "")
    linkedin = str(observed.get("linkedin_note") or "")
    all_copy = "\n".join([subject, body, linkedin])
    checks = [
        _range_check(
            "email_concision",
            len(body.split()),
            None,
            expected.get("email_max_words"),
        ),
        _range_check(
            "linkedin_concision",
            len(linkedin),
            None,
            expected.get("linkedin_max_chars"),
        ),
        _no_em_dash_check(all_copy, expected),
        _unsupported_claims_check(observed, expected),
        _unsupported_claim_explanations_check(observed, expected),
        _contains_terms_check(
            "personalization_quality",
            "\n".join([all_copy, str(observed.get("personalization_rationale", ""))]),
            expected.get("personalization_terms", []),
        ),
        _range_check(
            "facts_used",
            len(_list(observed.get("facts_used"))),
            expected.get("facts_used_min"),
            None,
        ),
        _range_check(
            "source_ids_used",
            len(_list(observed.get("source_ids_used"))),
            expected.get("source_ids_used_min"),
            None,
        ),
        _exact(
            "approved_context_used",
            observed.get("approved_context_used"),
            expected.get("approved_context_used"),
        ),
        _exact(
            "style_profile_used",
            observed.get("style_profile_used"),
            expected.get("style_profile_used"),
        ),
        _exact(
            "style_profile_id",
            observed.get("style_profile_id"),
            expected.get("style_profile_id"),
        ),
        _exact(
            "drafting_mode",
            observed.get("drafting_mode"),
            expected.get("drafting_mode"),
        ),
        _exact(
            "approval_required",
            observed.get("approval_required"),
            expected.get("approval_required"),
        ),
        _exact(
            "approval_status",
            _approval_status(observed),
            expected.get("approval_status"),
        ),
        _no_auto_send_check(observed),
        _contains_terms_check(
            "copy_required_terms",
            all_copy,
            expected.get("copy_must_contain", []),
        ),
        _forbidden_terms_check(
            "copy_forbidden_terms",
            all_copy,
            expected.get("copy_must_not_contain", []),
        ),
        _tone_check(all_copy, expected),
    ]
    return checks


def _check(name: str, passed: bool, message: str = "") -> EvalCheck:
    return EvalCheck(
        name=name,
        passed=passed,
        score=1.0 if passed else 0.0,
        message=message,
        skill_labels=_skill_labels_for_check(name),
    )


def _skill_labels_for_check(name: str) -> tuple[str, ...]:
    labels = CHECK_SKILL_LABELS.get(name)
    if labels is not None:
        return labels
    if name.startswith("all_handoff") or name == "state_action":
        return ("workflow_lifecycle_tracking", "handoff_contract_packaging")
    if name.endswith("_range") or name in {"fit_score_plausibility", "confidence_score"}:
        return ("structured_output_quality_review",)
    if name in {"source_recency", "research_completeness"}:
        return ("business_research_specialist_contracts",)
    return ("structured_output_quality_review",)


def _exact(name: str, observed: Any, expected: Any) -> EvalCheck:
    if expected is None:
        return _check(name, True, "no expectation")
    passed = observed == expected
    return _check(name, passed, f"observed={observed!r}, expected={expected!r}")


def _range_check(
    name: str,
    observed: int | float,
    minimum: Any = None,
    maximum: Any = None,
) -> EvalCheck:
    min_value = float(minimum) if minimum is not None else None
    max_value = float(maximum) if maximum is not None else None
    passed = True
    if min_value is not None and observed < min_value:
        passed = False
    if max_value is not None and observed > max_value:
        passed = False
    return _check(name, passed, f"observed={observed}, min={min_value}, max={max_value}")


def _risk_flags_check(
    observed: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> EvalCheck:
    flags = [str(flag) for flag in observed.get("risk_flags", [])]
    if "risk_flags_exact" in expected:
        expected_flags = [str(flag) for flag in expected["risk_flags_exact"]]
        return _check(
            "risk_flag_correctness",
            sorted(flags) == sorted(expected_flags),
            f"observed={flags!r}, expected={expected_flags!r}",
        )
    expected_contains = [str(flag) for flag in expected.get("risk_flags_contains", [])]
    missing = [flag for flag in expected_contains if flag not in flags]
    return _check("risk_flag_correctness", not missing, f"missing={missing!r}")


def _no_auto_send_check(observed: Mapping[str, Any]) -> EvalCheck:
    sent = bool(observed.get("sent") or observed.get("email_sent") or observed.get("send_enabled"))
    can_send = bool(observed.get("can_send_email"))
    return _check("no_auto_send", not sent and not can_send)


def _draft_quality_check(
    draft: str,
    expected: Mapping[str, Any],
    draft_quality: Mapping[str, Any],
) -> EvalCheck:
    draft_required = bool(expected.get("draft_required"))
    if not draft_required:
        return _check("draft_quality", not draft, "draft should not be created")
    terms_pass = _contains_all(draft, draft_quality.get("required_terms", []))
    forbidden_pass = _contains_none(draft, draft_quality.get("forbidden_terms", []))
    words = len(draft.split())
    min_words = draft_quality.get("min_words")
    max_words = draft_quality.get("max_words")
    length_pass = (min_words is None or words >= int(min_words)) and (
        max_words is None or words <= int(max_words)
    )
    passed = bool(draft.strip()) and terms_pass and forbidden_pass and length_pass
    return _check("draft_quality", passed, f"words={words}")


def _source_attribution_check(
    sources: list[Mapping[str, Any]],
    expected: Mapping[str, Any],
) -> EvalCheck:
    min_sources = int(expected.get("min_sources", 0))
    enough = len(sources) >= min_sources
    urls = not expected.get("require_source_urls") or all(source.get("url") for source in sources)
    claims = not expected.get("require_supported_claims") or all(
        source.get("supported_claims") for source in sources
    )
    return _check("source_attribution", enough and urls and claims, f"sources={len(sources)}")


def _no_hallucinated_facts_check(
    text: str,
    sources: list[Mapping[str, Any]],
    claims: list[Mapping[str, Any]],
    source_ids: set[str],
    unsupported_claims: Any,
    expected: Mapping[str, Any],
) -> EvalCheck:
    no_forbidden = _contains_none(text, expected.get("forbidden_facts", []))
    fixture_only = True
    if expected.get("fixture_source_only"):
        fixture_only = all(
            str(source.get("url", "")).startswith("fixture://") for source in sources
        )
    unsupported_expected = expected.get("unsupported_claims_exact")
    unsupported_pass = True
    if unsupported_expected is not None:
        unsupported_pass = [str(item) for item in _list(unsupported_claims)] == [
            str(item) for item in _list(unsupported_expected)
        ]
    claim_sources_valid = all(str(claim.get("source_id")) in source_ids for claim in claims)
    passed = no_forbidden and fixture_only and unsupported_pass and claim_sources_valid
    return _check("no_hallucinated_facts", passed)


def _contains_terms_check(name: str, text: str, terms: Any) -> EvalCheck:
    required = [str(term) for term in _list(terms)]
    missing = [term for term in required if term.lower() not in text.lower()]
    return _check(name, not missing, f"missing={missing!r}")


def _ranking_check(observed_names: list[str], expected_order: Any) -> EvalCheck:
    expected_names = [str(name) for name in _list(expected_order)]
    if not expected_names:
        return _check("ranking_quality", True, "no ranking expectation")
    observed_prefix = observed_names[: len(expected_names)]
    return _check(
        "ranking_quality",
        observed_prefix == expected_names,
        f"observed={observed_prefix!r}, expected={expected_names!r}",
    )


def _unique_companies_check(observed_names: list[str], expected: Mapping[str, Any]) -> EvalCheck:
    if not expected.get("unique_companies"):
        return _check("unique_companies", True, "no expectation")
    normalized = [_normalize_company_name(name) for name in observed_names]
    duplicates = sorted({name for name in normalized if normalized.count(name) > 1})
    return _check("unique_companies", not duplicates, f"duplicates={duplicates!r}")


def _record_count_check(
    records: list[Mapping[str, Any]],
    expected: Mapping[str, Any],
) -> EvalCheck:
    if "records_min" not in expected and "records_max" not in expected:
        return _check("record_count", True, "no expectation")
    return _range_check(
        "record_count",
        len(records),
        expected.get("records_min"),
        expected.get("records_max"),
    )


def _all_records_range_check(
    records: list[Mapping[str, Any]],
    field: str,
    expectation_prefix: str,
    expected: Mapping[str, Any],
) -> EvalCheck:
    minimum_key = f"{expectation_prefix}_min"
    maximum_key = f"{expectation_prefix}_max"
    if minimum_key not in expected and maximum_key not in expected:
        return _check(f"{expectation_prefix}_range", True, "no expectation")
    values = [int(record.get(field) or 0) for record in records]
    failures = [
        value
        for value in values
        if (minimum_key in expected and value < int(expected[minimum_key]))
        or (maximum_key in expected and value > int(expected[maximum_key]))
    ]
    return _check(
        f"{expectation_prefix}_range",
        not failures,
        f"observed={values!r}, failures={failures!r}",
    )


def _all_records_exact_check(
    records: list[Mapping[str, Any]],
    field: str,
    expected_value: Any,
) -> EvalCheck:
    if expected_value is None:
        return _check(f"all_{field}", True, "no expectation")
    values = [record.get(field) for record in records]
    return _check(
        f"all_{field}",
        all(value == expected_value for value in values),
        f"observed={values!r}, expected={expected_value!r}",
    )


def _forbidden_terms_check(name: str, text: str, terms: Any) -> EvalCheck:
    forbidden = [str(term) for term in _list(terms)]
    present = [term for term in forbidden if term.lower() in text.lower()]
    return _check(name, not present, f"present={present!r}")


def _normalize_company_name(value: str) -> str:
    text = value.lower()
    text = re.sub(r"\b(?:inc|llc|ltd|corp|corporation|company|co)\b", "", text)
    return re.sub(r"[^a-z0-9]+", "", text).strip()


def _score_breakdown_check(
    record: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> EvalCheck:
    required = [str(item) for item in _list(expected.get("required_score_components"))]
    if not required:
        return _check("score_breakdown", True, "no expectation")
    breakdown = _mapping(record.get("score_breakdown"))
    missing = [
        component for component in required if not isinstance(breakdown.get(component), int | float)
    ]
    rationale = str(record.get("score_rationale") or breakdown.get("rationale") or "")
    passed = not missing and bool(rationale.strip())
    return _check("score_breakdown", passed, f"missing={missing!r}")


def _no_em_dash_check(text: str, expected: Mapping[str, Any]) -> EvalCheck:
    if not expected.get("no_em_dash", True):
        return _check("no_em_dashes", True, "no expectation")
    return _check("no_em_dashes", "\u2014" not in text)


def _unsupported_claims_check(
    observed: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> EvalCheck:
    unsupported = [str(item) for item in observed.get("unsupported_claims_flagged", [])]
    if "unsupported_claims_exact" in expected:
        expected_claims = [str(item) for item in expected["unsupported_claims_exact"]]
        return _check(
            "no_unsupported_claims",
            unsupported == expected_claims,
            f"observed={unsupported!r}, expected={expected_claims!r}",
        )
    if "unsupported_claims_min" in expected:
        minimum = int(expected["unsupported_claims_min"])
        return _check(
            "unsupported_claims_min",
            len(unsupported) >= minimum,
            f"observed={len(unsupported)}, min={minimum}",
        )
    return _check("no_unsupported_claims", not unsupported, f"observed={unsupported!r}")


def _unsupported_claim_explanations_check(
    observed: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> EvalCheck:
    explanations = [str(item) for item in observed.get("unsupported_claim_explanations", [])]
    if "unsupported_claim_explanations_exact" in expected:
        expected_explanations = [
            str(item) for item in expected["unsupported_claim_explanations_exact"]
        ]
        return _check(
            "unsupported_claim_explanations",
            explanations == expected_explanations,
            f"observed={explanations!r}, expected={expected_explanations!r}",
        )
    if "unsupported_claim_explanations_min" in expected:
        minimum = int(expected["unsupported_claim_explanations_min"])
        return _check(
            "unsupported_claim_explanations",
            len(explanations) >= minimum,
            f"observed={len(explanations)}, min={minimum}",
        )
    return _check("unsupported_claim_explanations", True, "no expectation")


def _tone_check(text: str, expected: Mapping[str, Any]) -> EvalCheck:
    required_ok = _contains_all(text, expected.get("tone_required_terms", []))
    forbidden_ok = _contains_none(text, expected.get("tone_forbidden_terms", []))
    return _check("tone", required_ok and forbidden_ok)


def _contains_all(text: str, terms: Any) -> bool:
    return all(str(term).lower() in text.lower() for term in _list(terms))


def _contains_none(text: str, terms: Any) -> bool:
    return all(str(term).lower() not in text.lower() for term in _list(terms))


def _recommended_company_action(data: Mapping[str, Any]) -> str:
    score = int(data.get("consulting_fit_score") or 0)
    if score >= 70:
        return "Review sources and prepare an approval-gated outreach draft."
    if score >= 40:
        return "Gather additional source-backed evidence before outreach."
    return "Do not prioritize unless new source-backed signals emerge."


def _approval_status(observed: Mapping[str, Any]) -> str | None:
    state = observed.get("approval_status") or observed.get("approval_state")
    if isinstance(state, Mapping):
        return str(state.get("value") or state)
    return str(state) if state is not None else None


def _plain_data(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"eval output must be a mapping or Pydantic model, got {type(value)!r}")


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _list_of_mappings(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in _list(value) if isinstance(item, Mapping)]


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path
