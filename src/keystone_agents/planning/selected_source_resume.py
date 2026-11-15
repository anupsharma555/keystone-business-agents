"""Bind an exact WorkItem continuation to its already selected source identity.

This adapts record identity after model-owned routing. It does not select a new
source, infer an operation from prose, or turn stored evidence into permission.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from keystone_agents.execution_request import (
    explicit_current_work_item_id,
    latest_slack_operator_request,
)
from keystone_agents.instruction_following import canonical_source_url, canonical_source_urls
from keystone_agents.operator_failures import OperatorReadableFailureError
from keystone_agents.planning.composition_admission import is_selected_public_url_read_plan
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.schemas.work_item import WorkItem, WorkItemSourceRef
from keystone_agents.tools.website_extraction_tool import (
    WebsiteExtractionError,
    validate_public_http_url,
    validate_retained_web_source_access,
)

_SUPPORTED_READ_ARTIFACTS = frozenset({"source_summary", "research_brief"})


@dataclass(frozen=True)
class SelectedSourceResumeBinding:
    plan: ManualRequestPlan
    source: WorkItemSourceRef
    work_item_id: str

    def provenance(self) -> dict[str, Any]:
        return {
            "schema": "keystone.selected_source_resume_binding.v1",
            "work_item_id": self.work_item_id,
            "source_id": self.source.source_id,
            "source_url": self.source.url,
            "identity_source": "saved_work_item_target_and_source",
            "new_source_selected": False,
            "broad_search_permitted": False,
            "new_extraction_permitted": False,
            "external_write_permitted": False,
        }


def bind_selected_source_resume(
    *,
    plan: ManualRequestPlan | Mapping[str, Any] | None,
    work_item: WorkItem | None,
    work_item_id: str | None,
    current_request: str,
    route_result: Mapping[str, Any] | None,
    slack_context_path: str = "",
) -> SelectedSourceResumeBinding | None:
    """Require one saved source, an exact current record, and a read-only model plan."""

    if plan is None or work_item is None or not work_item_id:
        return None
    route_result = dict(route_result or {})
    current = latest_slack_operator_request(current_request)
    explicit_id = explicit_current_work_item_id(current)
    current_ids = set(re.findall(r"\bwi_[A-Za-z0-9_-]{1,160}\b", current))
    if current_ids and current_ids != {work_item_id}:
        return None
    relation = str(route_result.get("linked_work_item_relation") or "unspecified")
    if relation in {"branch_existing", "new_request", "clarify"}:
        return None
    linked_context = bool(
        not explicit_id and relation == "continue_existing"
        and matches_authenticated_slack_thread(work_item, slack_context_path)
    )
    if not (
        route_result.get("routing_mode") == "llm"
        and route_result.get("route") == "business_research_analyst"
        and not route_result.get("refused")
        and not route_result.get("context_only_response")
        and work_item_id == work_item.id
        and (explicit_id == work_item_id or linked_context)
    ):
        return None
    # Internal handoff plans have their own contract. Validate the public plan
    # only after proving this is the exact model-routed Research continuation.
    plan = ManualRequestPlan.model_validate(plan)
    if linked_context and plan.source == "heuristic":
        saved = work_item.target.metadata.get("manual_request_plan")
        try:
            saved_plan = ManualRequestPlan.model_validate(saved)
        except (TypeError, ValueError):
            saved_plan = None
        if (
            saved_plan is not None
            and saved_plan.source in {
                "canonical:work_item_selected_source_resume",
                "canonical:orchestrator_selected_source",
            }
            and saved_plan.target_agent == "business_research_analyst"
            and saved_plan.target_type == "url"
            and canonical_source_urls(saved_plan.primary_target)
            == canonical_source_urls(work_item.target.url)
            and saved_plan.expected_artifact_type in _SUPPORTED_READ_ARTIFACTS
            and saved_plan.provider_operations == ["read"]
            and not saved_plan.requires_live_search
            and saved_plan.ask_shape.permission_state == "read_only"
            and saved_plan.side_effect_policy == "draft_or_read_only"
            and not route_result.get("requires_live_search")
            and plan.provider_system == "unspecified"
            and not plan.provider_operations
            and not plan.provider_action_steps
            and not plan.provider_context_requirements
            and not plan.requires_approved_context
            and plan.side_effect_policy == "draft_or_read_only"
        ):
            # A short continuation can leave the heuristic plan with a prose
            # target or search guess. The model chose the same task; restore
            # only its saved, narrower read permission and source operation.
            plan = plan.model_copy(update={
                "requires_live_search": False,
                "intent": saved_plan.intent,
                "task_objective": saved_plan.task_objective,
                "expected_artifact_type": saved_plan.expected_artifact_type,
                "requires_durable_state": saved_plan.requires_durable_state,
                "ask_shape": plan.ask_shape.model_copy(update={
                    "prior_context_dependency": "selected_context",
                    "permission_state": "read_only",
                }),
            })
        elif plan.ask_shape.prior_context_dependency != "selected_context":
            # A vague short reply cannot revive source-read permission from
            # thread memory when the saved plan is absent or ineligible.
            return None
    dependency = plan.ask_shape.prior_context_dependency
    if linked_context and dependency == "none":
        return None
    model_selected_url = str(route_result.get("selected_source_url") or "")
    # An exact current WorkItem ID or model-owned continuation can leave the
    # source URL implicit. The saved record may supply identity only after
    # its unique cached snapshot passes the checks below.
    implicit_selected_context = dependency == "unspecified"
    if not (
        plan.target_agent == "business_research_analyst"
        and plan.expected_artifact_type in _SUPPORTED_READ_ARTIFACTS
        and (dependency == "selected_context" or implicit_selected_context)
    ):
        return None
    saved_artifact = str(
        work_item.target.metadata.get("manual_expected_artifact_type") or ""
    )
    if saved_artifact and saved_artifact != plan.expected_artifact_type:
        return None
    if not work_item.sources and is_selected_public_url_read_plan(plan):
        # A fully identified current URL read can acquire its own evidence. There
        # is no saved source identity here to adapt or reuse.
        return None

    def reject() -> NoReturn:
        raise OperatorReadableFailureError.input_contract(
            "The exact source continuation could not bind one saved source under "
            "the current read-only plan. Preserve this WorkItem and clarify the "
            "source identity or requested operation before running providers."
        )

    if (
        plan.requires_live_search
        or plan.requires_approved_context
        or route_result.get("requires_live_search")
        or plan.side_effect_policy != "draft_or_read_only"
        or plan.ask_shape.permission_state != "read_only"
        or plan.provider_system != "unspecified"
        or set(plan.provider_operations).difference({"read"})
        or plan.provider_action_steps
        or plan.provider_context_requirements
        or set(plan.workflow).difference({"business_research_analyst"})
        or set(route_result.get("workflow") or []).difference({"business_research_analyst"})
    ):
        reject()
    try:
        saved_url = validate_public_http_url(work_item.target.url, resolve_hostname=False)
    except (ValueError, WebsiteExtractionError):
        reject()
    identity = canonical_source_url(saved_url)
    if canonical_source_urls(work_item.target.url) != (identity,):
        reject()
    sources = [
        source for source in work_item.sources
        if source.url.startswith(("http://", "https://"))
        and canonical_source_url(source.url) == identity
        and source.provider not in {"fixture", "promptfoo"}
        and source.source_type not in {"fixture", "dry_run_context_handoff"}
    ]
    if (
        not sources
        or len({source.source_id for source in sources}) != 1
        or not sources[0].source_id
    ):
        reject()
    selected_url = model_selected_url
    current_urls = canonical_source_urls(latest_slack_operator_request(current_request))
    if (
        selected_url and canonical_source_url(selected_url) != identity
        or plan.target_type == "url" and canonical_source_url(plan.primary_target) != identity
        or any(url != identity for url in current_urls)
    ):
        reject()
    source = sources[0]
    if any(other != source for other in sources[1:]) or any(
        other.source_id == source.source_id and canonical_source_url(other.url) != identity
        for other in work_item.sources
    ):
        reject()
    access = source.web_source_access
    if access and access.selected_url and canonical_source_url(access.selected_url) != identity:
        reject()
    if implicit_selected_context and (
        access is None or access.source_id != source.source_id
        or not validate_retained_web_source_access(access)
    ):
        raise OperatorReadableFailureError.input_contract(
            "The exact saved cached snapshot is unavailable or failed identity validation. "
            "No new extraction, source search, or replacement source was attempted."
        )
    adapted = plan.model_copy(update={
        "source": "canonical:work_item_selected_source_resume",
        "primary_target": saved_url,
        "target_type": "url",
        "provider_operations": ["read"],
        "provider_read_scope": "single_item",
        "requires_live_search": False,
        "ask_shape": plan.ask_shape.model_copy(update={
            "strict_filter_mode": "exact", "prior_context_dependency": "selected_context",
        }),
        "planner_warnings": list(dict.fromkeys([
            *plan.planner_warnings,
            "Bound the current WorkItem's existing selected source without selecting a new source.",
        ])),
    })
    return SelectedSourceResumeBinding(adapted, source, work_item.id)


def matches_authenticated_slack_thread(work_item: WorkItem, context_path: str) -> bool:
    """Use only current host context to validate a linked record's thread identity."""

    if not context_path:
        return False
    try:
        payload = json.loads(Path(context_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(payload, dict) or not str(payload.get("schema") or "").startswith(
        "keystone.slack."
    ):
        return False
    stored = work_item.target.metadata.get("slack_context")
    if not isinstance(stored, dict):
        return False
    return bool(
        payload.get("channel_id") and payload.get("thread_ts")
        and str(payload["channel_id"]) == str(stored.get("channel_id") or "")
        and str(payload["thread_ts"]) == str(stored.get("thread_ts") or "")
    )
