"""Automation inventory helpers for the Chief of Staff operating layer."""

from __future__ import annotations

import json
from typing import Any

from keystone_agents.schemas.approval import ApprovalQueueStatus
from keystone_agents.schemas.automation import (
    AutomationChannelBinding,
    AutomationFinding,
    AutomationFindingSeverity,
    AutomationInventoryReport,
    AutomationRun,
    AutomationRunStatus,
    AutomationSpec,
    AutomationStatus,
    AutomationTriggerType,
)
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env

DEFAULT_AUTOMATION_SPECS: tuple[AutomationSpec, ...] = (
    AutomationSpec(
        id="auto_weekly_opportunity",
        name="Weekly Opportunity Scan",
        description="Scheduled Opportunity Scout to research and queue Keystone-relevant leads.",
        trigger_type=AutomationTriggerType.SCHEDULE,
        schedule="weekly",
        target_agent="opportunity_scout",
        workflow="weekly-opportunity",
        input_template="Find Keystone-relevant opportunities for weekly review.",
        default_channel="#ai-agents-workflow",
        live_flags=["--confirm-live", "--live-sdk", "--live-search"],
    ),
    AutomationSpec(
        id="auto_gmail_triage",
        name="Gmail Triage",
        description=(
            "Staged Gmail automation for label previews, label application, "
            "and draft creation."
        ),
        trigger_type=AutomationTriggerType.GMAIL_QUERY,
        schedule="operator-controlled",
        target_agent="gmail_triage",
        workflow="gmail-triage",
        input_template="Run Gmail triage for a narrow label/query.",
        default_channel="#ai-agents-workflow",
        live_flags=["--confirm-live", "--live-gmail"],
    ),
    AutomationSpec(
        id="auto_slack_approvals",
        name="Slack Approval Notifications",
        description="Review-channel notifications for approval queue items and WorkItem actions.",
        trigger_type=AutomationTriggerType.SLACK_ACTION,
        schedule="event-driven",
        target_agent="chief_of_staff",
        workflow="slack-approval-notification",
        input_template="Notify reviewers about pending approval items.",
        default_channel="#ai-agents-workflow",
        live_flags=["--notify-slack", "--live-slack"],
    ),
)


DEFAULT_CHANNEL_BINDINGS: tuple[AutomationChannelBinding, ...] = (
    AutomationChannelBinding(
        id="acb_weekly_opportunity_review",
        automation_id="auto_weekly_opportunity",
        channel_name="ai-agents-workflow",
        destination_type="slack",
        purpose="approval_review",
    ),
    AutomationChannelBinding(
        id="acb_gmail_triage_review",
        automation_id="auto_gmail_triage",
        channel_name="ai-agents-workflow",
        destination_type="slack",
        purpose="gmail_review",
    ),
    AutomationChannelBinding(
        id="acb_slack_approval_review",
        automation_id="auto_slack_approvals",
        channel_name="ai-agents-workflow",
        destination_type="slack",
        purpose="approval_notification",
    ),
)


def ensure_default_automation_inventory(store: SQLiteStore) -> None:
    """Seed known automation specs and bindings if they are not present."""

    for spec in DEFAULT_AUTOMATION_SPECS:
        existing = store.get_automation_spec(spec.id)
        if existing is None:
            store.save_automation_spec(spec)
    existing_binding_ids = {
        binding.id for binding in store.list_automation_channel_bindings(limit=500)
    }
    for binding in DEFAULT_CHANNEL_BINDINGS:
        if binding.id not in existing_binding_ids:
            store.save_automation_channel_binding(binding)


def automation_spec_for_command(command: str, *, stage: str = "") -> AutomationSpec:
    """Return the default automation spec for an automation controller command."""

    normalized = str(command or "").strip()
    if normalized == "weekly-opportunity":
        return DEFAULT_AUTOMATION_SPECS[0].model_copy(
            update={"metadata": {"last_stage": stage}}
        )
    if normalized == "gmail-triage":
        return DEFAULT_AUTOMATION_SPECS[1].model_copy(update={"metadata": {"last_stage": stage}})
    return AutomationSpec(
        id=f"auto_{normalized.replace('-', '_') or 'manual'}",
        name=normalized.replace("-", " ").title() or "Manual Automation",
        description="Operator-run Keystone automation.",
        trigger_type=AutomationTriggerType.MANUAL,
        workflow=normalized,
        metadata={"last_stage": stage},
    )


def build_automation_inventory_report(
    *,
    database_url: str | None = None,
    channels: list[str] | None = None,
    limit: int = 20,
) -> AutomationInventoryReport:
    """Build a deterministic Chief of Staff automation inventory report."""

    store = SQLiteStore(database_url or database_url_from_env())
    ensure_default_automation_inventory(store)
    specs = store.list_automation_specs(limit=100)
    bindings = store.list_automation_channel_bindings(limit=200)
    runs = store.list_automation_runs(limit=limit)
    pending = store.list_approval_items(status=ApprovalQueueStatus.PENDING)
    channel_filter = {_normalize_channel(channel) for channel in channels or [] if channel}
    if channel_filter:
        bindings = [
            binding
            for binding in bindings
            if _normalize_channel(binding.channel_name) in channel_filter
            or _normalize_channel(binding.channel_id) in channel_filter
        ]
        automation_ids = {binding.automation_id for binding in bindings}
        specs = [spec for spec in specs if spec.id in automation_ids]
        runs = [run for run in runs if run.automation_id in automation_ids]
    findings = _findings_from_inventory(specs=specs, runs=runs, pending_approval_count=len(pending))
    for finding in findings:
        store.save_automation_finding(finding)
    reviewed = sorted(
        {
            binding.channel_name or binding.channel_id
            for binding in bindings
            if binding.channel_name or binding.channel_id
        }
    )
    summary = (
        f"Reviewed {len(specs)} automation spec(s), {len(runs)} recent run(s), "
        f"and {len(pending)} pending approval item(s)."
    )
    return AutomationInventoryReport(
        summary=summary,
        channels_reviewed=reviewed,
        automation_specs=specs,
        recent_runs=runs,
        channel_bindings=bindings,
        findings=findings,
        pending_approval_count=len(pending),
        recommended_actions=_recommended_actions(findings, pending_approval_count=len(pending)),
        blocked_actions=[
            "Do not treat Google Docs or Airtable rows as canonical state.",
            (
                "Do not send email, mutate calendars, write CRM records, "
                "or post public Slack messages."
            ),
            "Do not change automation schedules without an explicit approval gate.",
        ],
        audit_notes=[
            "SQLite, WorkItems, and approval queue remain canonical.",
            "Chief of Staff automation inventory used bounded local state.",
        ],
    )


def render_automation_inventory_markdown(report: AutomationInventoryReport) -> str:
    """Render a compact internal markdown report."""

    lines = [
        f"# {report.title}",
        "",
        report.summary,
        "",
        "## Automations",
    ]
    if not report.automation_specs:
        lines.append("- No automation specs found.")
    for spec in report.automation_specs:
        lines.append(
            f"- **{spec.name}**: {spec.status.value}, workflow `{spec.workflow}`, "
            f"default channel {spec.default_channel or 'none'}."
        )
    lines.extend(["", "## Recent Runs"])
    if not report.recent_runs:
        lines.append("- No recent runs recorded.")
    for run in report.recent_runs:
        detail = f"{run.failure_summary}" if run.failure_summary else run.next_safe_action
        lines.append(
            f"- **{run.automation_name or run.automation_id}** `{run.stage}`: "
            f"{run.status.value}; approvals {run.approval_count}; {detail or 'no next action'}"
        )
    lines.extend(["", "## Findings"])
    if not report.findings:
        lines.append("- No findings.")
    for finding in report.findings:
        lines.append(
            f"- **{finding.severity.value.upper()} - {finding.title}**: "
            f"{finding.summary} {finding.recommendation}".strip()
        )
    lines.extend(["", "## Recommended Actions"])
    for action in report.recommended_actions or ["Review the automation inventory."]:
        lines.append(f"- {action}")
    return "\n".join(lines).strip() + "\n"


def report_from_json(report_json: str | dict[str, Any]) -> AutomationInventoryReport:
    """Parse an AutomationInventoryReport from tool JSON input."""

    if isinstance(report_json, dict):
        return AutomationInventoryReport.model_validate(report_json)
    return AutomationInventoryReport.model_validate(json.loads(str(report_json or "{}")))


def _findings_from_inventory(
    *,
    specs: list[AutomationSpec],
    runs: list[AutomationRun],
    pending_approval_count: int,
) -> list[AutomationFinding]:
    findings: list[AutomationFinding] = []
    run_by_automation: dict[str, list[AutomationRun]] = {}
    for run in runs:
        run_by_automation.setdefault(run.automation_id, []).append(run)
        if run.status == AutomationRunStatus.FAILED:
            findings.append(
                AutomationFinding(
                    automation_id=run.automation_id,
                    run_id=run.id,
                    channel=run.channel,
                    finding_type="failure",
                    severity=AutomationFindingSeverity.ERROR,
                    title=f"{run.automation_name or run.automation_id} failed",
                    summary=run.failure_summary or "Automation returned a non-zero status.",
                    recommendation=run.next_safe_action
                    or "Review the child command output and rerun after fixing the blocker.",
                )
            )
    for spec in specs:
        if spec.status == AutomationStatus.PAUSED:
            findings.append(
                AutomationFinding(
                    automation_id=spec.id,
                    channel=spec.default_channel,
                    finding_type="paused",
                    severity=AutomationFindingSeverity.WARNING,
                    title=f"{spec.name} is paused",
                    summary="Automation is configured but not active.",
                    recommendation="Confirm whether it should remain paused.",
                )
            )
        if spec.id not in run_by_automation:
            findings.append(
                AutomationFinding(
                    automation_id=spec.id,
                    channel=spec.default_channel,
                    finding_type="stale",
                    severity=AutomationFindingSeverity.INFO,
                    title=f"{spec.name} has no recorded runs",
                    summary="No AutomationRun has been recorded in local state.",
                    recommendation="Run a dry-run stage to establish a baseline.",
                )
            )
    if pending_approval_count:
        findings.append(
            AutomationFinding(
                finding_type="pending_approvals",
                severity=AutomationFindingSeverity.WARNING,
                title="Pending approval backlog",
                summary=f"{pending_approval_count} approval item(s) are pending.",
                recommendation=(
                    "Review approvals before scheduled automation accumulates more work."
                ),
            )
        )
    return findings


def _recommended_actions(
    findings: list[AutomationFinding],
    *,
    pending_approval_count: int,
) -> list[str]:
    actions: list[str] = []
    if pending_approval_count:
        actions.append("Review pending approval queue before running more scheduled automation.")
    if any(finding.severity in {"error", "blocker"} for finding in findings):
        actions.append("Resolve failing automation runs before enabling live writes.")
    if any(finding.finding_type == "stale" for finding in findings):
        actions.append("Run dry-run baselines for automations with no recorded history.")
    actions.append("Use Chief of Staff publishing tools for internal reports only.")
    return list(dict.fromkeys(actions))


def _normalize_channel(channel: str) -> str:
    return str(channel or "").strip().lstrip("#").lower()
