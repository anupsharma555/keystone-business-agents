"""Contract tests for the shared canonical-plan execution boundary."""

import ast
from pathlib import Path

import pytest

from keystone_agents.provider_side_effect_policy import (
    semantic_provider_side_effect_policy,
)
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.semantic_execution import (
    ExecutionIntentAuthority,
    StageOperationBoundary,
    StageOutputContract,
    reconcile_stage_output,
)


def test_missing_plan_allows_compatibility_fallback() -> None:
    authority = ExecutionIntentAuthority.from_value(None)

    assert authority.supplied is False
    assert authority.canonical is False
    assert authority.compatibility is False
    assert authority.invalid is False
    assert authority.fallback_allowed is True


def test_heuristic_plan_remains_a_compatibility_hint() -> None:
    plan = ManualRequestPlan(
        source="heuristic",
        target_agent="chief_of_staff",
        intent="route_request",
    )

    authority = ExecutionIntentAuthority.from_value(plan)

    assert authority.supplied is True
    assert authority.canonical is False
    assert authority.compatibility is True
    assert authority.invalid is False
    assert authority.fallback_allowed is True


def test_canonical_plan_owns_route_and_provider_scope() -> None:
    plan = ManualRequestPlan(
        source="canonical",
        target_agent="gmail_triage",
        workflow=["gmail_triage", "outreach_composer"],
        intent="gmail_triage",
        provider_system="gmail",
        provider_operations=["read"],
    )

    authority = ExecutionIntentAuthority.from_value(plan)

    assert authority.canonical is True
    assert authority.fallback_allowed is False
    assert authority.requests_route("gmail_triage") is True
    assert authority.requests_route("outreach_composer") is True
    assert authority.requests_route("business_research_analyst") is False
    assert authority.authorizes_provider(
        "gmail",
        allowed_agents={"gmail_triage"},
        allowed_intents={"gmail_triage"},
    ) is True
    assert authority.authorizes_provider("google_calendar") is False


def test_provider_action_steps_refine_but_never_broaden_operations() -> None:
    authority = ExecutionIntentAuthority.from_value(
        ManualRequestPlan(
            source="llm",
            target_agent="google_workspace_context_agent",
            intent="context_lookup",
            provider_system="google_workspace",
            provider_operations=["read"],
            provider_action_steps=[
                {"operation": "read", "resource_type": "google_document"},
                {"operation": "delete", "resource_type": "google_document"},
            ],
        )
    )

    steps = authority.provider_action_steps("google_workspace")

    assert [(step.operation, step.resource_type) for step in steps] == [
        ("read", "google_document")
    ]
    assert authority.provider_action_steps("gmail") == ()


@pytest.mark.parametrize(
    ("source", "provider"),
    [
        ("llm", "airtable"),
        ("canonical", "google_workspace"),
        ("orchestrator_canonical", "zotero"),
        ("canonical:stored_work_item", "google_calendar"),
        ("canonical:replay_fixture", "gmail"),
    ],
)
def test_read_only_permission_is_the_effective_provider_operation_ceiling(
    source: str,
    provider: str,
) -> None:
    authority = ExecutionIntentAuthority.from_value(
        ManualRequestPlan(
            source=source,
            target_agent="chief_of_staff",
            intent="business_system_write",
            provider_system=provider,
            provider_operations=[
                "read",
                "search",
                "create",
                "update",
                "delete",
                "attach",
                "verify",
            ],
            provider_action_steps=[
                {"operation": "read", "resource_type": "unspecified"},
                {"operation": "create", "resource_type": "unspecified"},
                {"operation": "delete", "resource_type": "unspecified"},
                {"operation": "verify", "resource_type": "unspecified"},
            ],
            ask_shape={"permission_state": "read_only"},
        )
    )

    assert authority.canonical is True
    assert authority.effective_provider_operations(provider) == (
        "read",
        "search",
        "verify",
    )
    assert [
        step.operation for step in authority.provider_action_steps(provider)
    ] == ["read", "verify"]
    assert authority.effective_provider_operations("slack") == ()


@pytest.mark.parametrize("permission_state", ["draft_only", "approval_required"])
def test_non_read_only_permission_preserves_explicit_scoped_operations(
    permission_state: str,
) -> None:
    authority = ExecutionIntentAuthority.from_value(
        ManualRequestPlan(
            source="llm",
            target_agent="gmail_triage",
            intent="business_system_write",
            provider_system="gmail",
            provider_operations=["read", "create"],
            ask_shape={"permission_state": permission_state},
        )
    )

    assert authority.effective_provider_operations("gmail") == ("read", "create")


def test_provider_policy_uses_effective_read_only_operations() -> None:
    policy = semantic_provider_side_effect_policy(
        ManualRequestPlan(
            source="canonical:stored_work_item",
            target_agent="chief_of_staff",
            intent="business_system_write",
            primary_target="one selected record",
            provider_system="airtable",
            provider_operations=["read", "create", "attach"],
            ask_shape={"permission_state": "read_only"},
        )
    )

    assert policy is not None
    assert policy.startswith("Read-only airtable scope")
    assert "permits only read" in policy
    assert "authenticated operator requested" not in policy.lower()


def test_canonical_plan_owns_contact_enrichment_without_keyword_triggers() -> None:
    unrelated = ExecutionIntentAuthority.from_value(
        ManualRequestPlan(
            source="llm",
            target_agent="business_research_analyst",
            intent="company_research",
            task_objective="entity_research",
            expected_artifact_type="research_brief",
            objective=(
                "Explain the supplied note, including why its old contact-email "
                "language is not current evidence."
            ),
        )
    )
    contact_discovery = ExecutionIntentAuthority.from_value(
        ManualRequestPlan(
            source="llm",
            target_agent="business_research_analyst",
            intent="company_research",
            task_objective="contact_discovery",
            expected_artifact_type="contact_candidates",
            objective="Identify the appropriate decision owner.",
        )
    )

    assert unrelated.requests_contact_enrichment() is False
    assert contact_discovery.requests_contact_enrichment() is True


def test_canonical_plan_owns_internal_slack_artifact_mode() -> None:
    internal = ExecutionIntentAuthority.from_value(
        ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            workflow=["business_research_analyst", "outreach_composer"],
            intent="route_request",
            task_objective="outreach_draft",
            expected_artifact_type="outreach_draft",
            outreach_channel="internal_team_channel",
            side_effect_policy="draft_or_read_only",
        )
    )
    external = ExecutionIntentAuthority.from_value(
        ManualRequestPlan(
            source="llm",
            target_agent="outreach_composer",
            intent="outreach_draft",
            task_objective="outreach_draft",
            expected_artifact_type="outreach_draft",
            recipient="Maya",
            outreach_channel="email",
            side_effect_policy="draft_or_read_only",
        )
    )

    assert internal.requests_internal_slack_artifact() is True
    assert external.requests_internal_slack_artifact() is False


def test_read_only_stage_discards_unrequested_draft_text_without_blocking() -> None:
    reconciliation = reconcile_stage_output(
        {
            "important": [
                {
                    "message_id": "msg-1",
                    "summary": "A reply-worthy collaboration request.",
                    "draft_reply": "Thanks for reaching out.",
                    "draft_created": False,
                    "send_enabled": False,
                }
            ],
            "draft_count": 1,
            "provider_write": False,
        },
        contract=StageOutputContract(
            stage="candidate_ranking",
            operation_boundary=StageOperationBoundary.READ_ONLY,
            discard_fields=frozenset({"draft_reply", "draft_count"}),
        ),
    )

    assert reconciliation.safe is True
    assert reconciliation.payload["important"][0]["summary"] == (
        "A reply-worthy collaboration request."
    )
    assert reconciliation.payload["important"][0]["draft_reply"] is None
    assert reconciliation.payload["draft_count"] == 0
    assert reconciliation.discarded_paths == (
        "important[0].draft_reply",
        "draft_count",
    )


def test_read_only_stage_rejects_structured_provider_mutation_claims() -> None:
    reconciliation = reconcile_stage_output(
        {
            "answer": "The event exists.",
            "provider_write": True,
            "nested": {"event_updated": True},
        },
        contract=StageOutputContract(
            stage="calendar_verification",
            operation_boundary=StageOperationBoundary.READ_ONLY,
        ),
    )

    assert reconciliation.safe is False
    assert reconciliation.prohibited_effect_paths == (
        "provider_write",
        "nested.event_updated",
    )
    assert reconciliation.observed_effect_paths == (
        "provider_write",
        "nested.event_updated",
    )


def test_provider_write_stage_preserves_mutation_claim_for_receipt_verification() -> None:
    reconciliation = reconcile_stage_output(
        {"provider_write": True, "event_created": True},
        contract=StageOutputContract(
            stage="calendar_create",
            operation_boundary=StageOperationBoundary.PROVIDER_WRITE,
        ),
    )

    assert reconciliation.safe is True
    assert reconciliation.payload["event_created"] is True
    assert reconciliation.observed_effect_paths == (
        "provider_write",
        "event_created",
    )


def test_authority_distinguishes_explicit_fields_from_schema_defaults() -> None:
    mapping_authority = ExecutionIntentAuthority.from_value(
        {
            "source": "llm",
            "target_agent": "business_research_analyst",
            "intent": "company_research",
            "requires_live_search": True,
        }
    )
    model_authority = ExecutionIntentAuthority.from_value(
        ManualRequestPlan(
            source="llm",
            target_agent="business_research_analyst",
            intent="company_research",
        )
    )

    assert mapping_authority.field_supplied("requires_live_search") is True
    assert mapping_authority.field_supplied("target_type") is False
    assert model_authority.field_supplied("requires_live_search") is False
    assert model_authority.field_supplied("source") is True


def test_programmatic_canonical_source_preserves_origin_and_authority() -> None:
    authority = ExecutionIntentAuthority.from_value(
        ManualRequestPlan(
            source="canonical:finance_packet",
            target_agent="chief_of_staff",
            intent="context_lookup",
            task_objective="context_lookup",
            provider_system="airtable",
            provider_operations=["read"],
        )
    )

    assert authority.canonical is True
    assert authority.compatibility is False
    assert authority.fallback_allowed is False


def test_malformed_programmatic_canonical_plan_stops_without_fallback() -> None:
    authority = ExecutionIntentAuthority.from_value(
        {
            "source": "canonical:finance_packet",
            "target_agent": "chief_of_staff",
            "intent": "legacy-internal-intent",
        }
    )

    assert authority.invalid is True
    assert authority.fallback_allowed is False


def test_malformed_canonical_plan_stops_without_fallback() -> None:
    authority = ExecutionIntentAuthority.from_value(
        {
            "source": "llm",
            "target_agent": "gmail_triage",
            "intent": "not-a-supported-intent",
        }
    )

    assert authority.supplied is True
    assert authority.canonical is False
    assert authority.compatibility is False
    assert authority.invalid is True
    assert authority.fallback_allowed is False


def test_malformed_legacy_fixture_hint_keeps_compatibility_fallback() -> None:
    authority = ExecutionIntentAuthority.from_value(
        {
            "source": "test",
            "target_agent": "chief_of_staff",
            "intent": "legacy-test-intent",
        }
    )

    assert authority.supplied is True
    assert authority.canonical is False
    assert authority.compatibility is True
    assert authority.invalid is False
    assert authority.fallback_allowed is True


def test_downstream_modules_do_not_reimplement_plan_source_authority() -> None:
    package = Path(__file__).resolve().parents[1] / "src" / "keystone_agents"
    allowed = {
        package / "manual_request.py",
        package / "semantic_execution.py",
    }
    violations: list[str] = []
    for path in package.rglob("*.py"):
        if path in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            values = [node.left, *node.comparators]
            compares_plan_source = any(
                isinstance(value, ast.Attribute) and value.attr == "source"
                for value in values
            )
            mentions_llm_source = any(
                isinstance(value, ast.Constant) and value.value == "llm"
                for value in ast.walk(node)
            )
            if compares_plan_source and mentions_llm_source:
                violations.append(f"{path.relative_to(package)}:{node.lineno}")

    assert violations == [], (
        "Downstream plan-source checks bypass ExecutionIntentAuthority: "
        + ", ".join(violations)
    )
