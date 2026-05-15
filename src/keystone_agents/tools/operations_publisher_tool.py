"""Typed internal publishing tools for Chief of Staff operating reports."""

from __future__ import annotations

import json
from typing import Any

from keystone_agents.automation_inventory import (
    render_automation_inventory_markdown,
    report_from_json,
)
from keystone_agents.guardrails import keystone_tool_guardrail_kwargs
from keystone_agents.schemas.automation import (
    AutomationArtifactRef,
    AutomationWriteDestination,
)
from keystone_agents.sdk import function_tool
from keystone_agents.tools.slack_tool import SlackTool


def publish_document_report_impl(
    report_json: str,
    *,
    destination: str = "local_markdown",
    live: bool = False,
) -> dict[str, Any]:
    """Publish a Chief of Staff report to an internal document destination."""

    report = report_from_json(report_json)
    normalized_destination = AutomationWriteDestination(destination)
    markdown = render_automation_inventory_markdown(report)
    if live:
        if normalized_destination != AutomationWriteDestination.GOOGLE_DOC:
            raise RuntimeError("Live document publishing currently supports Google Doc only.")
        raise RuntimeError(
            "Live Google Docs publishing is not implemented in this repo yet. "
            "Use dry-run/local publishing or add a reviewed Google Docs adapter."
        )
    artifact = AutomationArtifactRef(
        artifact_type="automation_inventory_report",
        title=report.title,
        provider=normalized_destination.value,
        dry_run=True,
        url=(
            f"dry-run://google-doc/{report.report_id}"
            if normalized_destination == AutomationWriteDestination.GOOGLE_DOC
            else ""
        ),
        path=(
            f"artifacts/{report.report_id}.md"
            if normalized_destination == AutomationWriteDestination.LOCAL_MARKDOWN
            else ""
        ),
        metadata={
            "report_id": report.report_id,
            "markdown_preview": markdown[:1200],
            "external_sharing_enabled": False,
        },
    )
    return {
        "status": "dry-run",
        "destination": normalized_destination.value,
        "artifact": artifact.model_dump(mode="json"),
        "send_enabled": False,
        "external_sharing_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def publish_document_report(
    report_json: str,
    destination: str = "local_markdown",
    live: bool = False,
) -> str:
    """Create a local or dry-run Google Doc internal automation report."""

    return json.dumps(
        publish_document_report_impl(
            report_json,
            destination=destination,
            live=live,
        ),
        ensure_ascii=True,
        sort_keys=True,
    )


def publish_table_mirror_impl(
    report_json: str,
    *,
    destination: str = "airtable",
    live: bool = False,
) -> dict[str, Any]:
    """Publish automation findings to an Airtable-shaped review mirror."""

    report = report_from_json(report_json)
    normalized_destination = AutomationWriteDestination(destination)
    if normalized_destination != AutomationWriteDestination.AIRTABLE:
        raise ValueError("table mirror destination must be airtable")
    if live:
        raise RuntimeError(
            "Live Airtable publishing is not implemented in this repo yet. "
            "Use dry-run Airtable-shaped rows or add a reviewed Airtable adapter."
        )
    rows = [
        {
            "automation_id": finding.automation_id,
            "run_id": finding.run_id,
            "channel": finding.channel,
            "finding_type": finding.finding_type,
            "severity": finding.severity.value,
            "title": finding.title,
            "summary": finding.summary,
            "recommendation": finding.recommendation,
            "report_id": report.report_id,
        }
        for finding in report.findings
    ]
    artifact = AutomationArtifactRef(
        artifact_type="automation_airtable_mirror",
        title=f"{report.title} Airtable Mirror",
        provider="airtable",
        dry_run=True,
        url=f"dry-run://airtable/{report.report_id}",
        metadata={
            "report_id": report.report_id,
            "row_count": len(rows),
            "rows": rows,
            "canonical_state": "sqlite",
        },
    )
    return {
        "status": "dry-run",
        "destination": "airtable",
        "artifact": artifact.model_dump(mode="json"),
        "rows": rows,
        "canonical_state": "sqlite",
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def publish_table_mirror(
    report_json: str,
    destination: str = "airtable",
    live: bool = False,
) -> str:
    """Create Airtable-shaped rows for automation review without changing canonical state."""

    return json.dumps(
        publish_table_mirror_impl(
            report_json,
            destination=destination,
            live=live,
        ),
        ensure_ascii=True,
        sort_keys=True,
    )


def publish_slack_summary_impl(
    report_json: str,
    *,
    channel: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Publish a short internal Slack summary through the existing Slack boundary."""

    report = report_from_json(report_json)
    text = "\n".join(
        [
            f"{report.title}",
            report.summary,
            f"Findings: {len(report.findings)}",
            f"Pending approvals: {report.pending_approval_count}",
            "Writes are internal-review only; public posts and external actions remain gated.",
        ]
    )
    result = SlackTool(live=live).post_message(channel or "#ai-agents-workflow", text)
    artifact = AutomationArtifactRef(
        artifact_type="automation_slack_summary",
        title=f"{report.title} Slack Summary",
        provider="slack",
        dry_run=not live,
        url=f"slack://{result.get('channel', channel)}/{result.get('ts', '')}",
        metadata={
            "report_id": report.report_id,
            "slack_result": result,
            "public_post_approved": False,
        },
    )
    return {
        "status": result.get("status", "posted" if live else "dry-run"),
        "artifact": artifact.model_dump(mode="json"),
        "send_enabled": False,
        "slack_result": result,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def publish_slack_summary(
    report_json: str,
    channel: str = "",
    live: bool = False,
) -> str:
    """Post or dry-run an internal Slack summary for a Chief of Staff report."""

    return json.dumps(
        publish_slack_summary_impl(report_json, channel=channel, live=live),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )
