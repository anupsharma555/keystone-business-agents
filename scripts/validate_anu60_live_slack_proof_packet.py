from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROOF_PLAN = PROJECT_ROOT / "docs" / "ANU60_LIVE_SLACK_PROOF_PLAN.md"
EVIDENCE_TEMPLATE = PROJECT_ROOT / "docs" / "ANU60_LIVE_SLACK_EVIDENCE_TEMPLATE.md"
WORKFLOW_STATUS = PROJECT_ROOT / "docs" / "AI_AGENTS_WORKFLOW_TEST_STATUS.md"
BRIDGE_BACKLOG = PROJECT_ROOT / "docs" / "ORCHESTRATOR_BRIDGE_BACKLOG.md"
PACKAGE_JSON = PROJECT_ROOT / "package.json"
EXPANSION_ARTIFACT = (
    PROJECT_ROOT / "artifacts" / "anu60_expansion_gate_after_acceptance_map.json"
)


REQUIRED_PROOF_PLAN_MARKERS = (
    "ANU-60 acceptance still needs visible output proof in `#ai-agents-workflow`",
    "Do not treat the `#evals` readiness pass as the final ANU-60 proof",
    "artifacts/anu60_expansion_gate_after_acceptance_map.json",
    "npm run eval:slack:anu60-proof",
    "npm run eval:slack:anu60-preflight",
    "Slack posting in `#ai-agents-workflow` is approved",
    "npm run eval:slack:strict-readiness -- --json",
    "../keystone-slack/scripts/manage_slack_socket.sh status",
    "slack_rss_context_announcement_history_001",
    "slack_preprints_context_preliminary_evidence_001",
    "Suki AI",
    "Nabla",
    "slack_gmail_missing_thread_identity_001",
    "## Acceptance Coverage Map",
    "Direct Business Research Slack probes render the KBA answer first",
    "Conversational Business Research Slack probes render the KBA answer first",
    "RSS context-agent Slack probes display `RssContextResult` summaries",
    "Preprints context-agent Slack probes display `PreprintsContextResult` summaries",
    "Blocked preflights render accurate, redacted, actionable statuses",
    "Timeouts render accurate, redacted, actionable statuses",
    "test_business_agents_run_command_timeout_returns_structured_failure_and_kills_group",
    "test_business_agents_streaming_run_command_timeout_kills_group",
    "ANU-60 can move to Done only when the live evidence above is captured",
)


REQUIRED_TEMPLATE_MARKERS = (
    "## Run Boundary",
    "Approval for Slack posting in `#ai-agents-workflow`: yes/no",
    "Approval for live model/search/API spend and stop condition: yes/no",
    "npm run eval:slack:anu60-preflight",
    "### RSS Context",
    "### Preprints Context",
    "### Direct Business Research",
    "### Conversational Business Research",
    "### Gmail Missing Context",
    "### Timeout / Failure Fixture Boundary",
    "## Acceptance Coverage Map",
    "## Completion Summary",
    "Slack permalink:",
    "Local run id or WorkItem id:",
    "Visible body starts with answer-first `human_summary`: yes/no",
    "Provider/model/timing metadata appears before answer: yes/no",
    "External write/send/draft/feed-refresh observed: yes/no",
    "Bridge accepted conversational named-agent wording before KBA planning: yes/no",
    "Visible body says `Gmail Triage needs email context`: yes/no",
    "Failure output is redacted and actionable: yes/no",
    "Timeout/failure done criterion covered by fixture or approved live probe: yes/no",
    "Recommendation for ANU-60 state:",
)


REQUIRED_STATUS_MARKERS = (
    "docs/ANU60_LIVE_SLACK_PROOF_PLAN.md",
    "docs/ANU60_LIVE_SLACK_EVIDENCE_TEMPLATE.md",
    "artifacts/anu60_expansion_gate_after_acceptance_map.json",
    "npm run eval:slack:anu60-proof",
    "npm run eval:slack:anu60-preflight",
    "do not move ANU-60 to Done until fresh live",
)


REQUIRED_BACKLOG_MARKERS = (
    "docs/ANU60_LIVE_SLACK_PROOF_PLAN.md",
    "docs/ANU60_LIVE_SLACK_EVIDENCE_TEMPLATE.md",
    "artifacts/anu60_expansion_gate_after_acceptance_map.json",
    "npm run eval:slack:anu60-proof",
    "npm run eval:slack:anu60-preflight",
    "test_business_agents_run_command_timeout_returns_structured_failure_and_kills_group",
    "test_business_agents_streaming_run_command_timeout_kills_group",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _missing_markers(path: Path, markers: tuple[str, ...]) -> list[str]:
    text = _read(path)
    return [marker for marker in markers if marker not in text]


def validate_anu60_live_slack_proof_packet() -> list[str]:
    failures: list[str] = []
    required_files = (
        PROOF_PLAN,
        EVIDENCE_TEMPLATE,
        WORKFLOW_STATUS,
        BRIDGE_BACKLOG,
        PACKAGE_JSON,
        EXPANSION_ARTIFACT,
    )

    for path in required_files:
        if not path.exists():
            failures.append(f"Missing required file: {path.relative_to(PROJECT_ROOT)}")

    if failures:
        return failures

    for path, markers in (
        (PROOF_PLAN, REQUIRED_PROOF_PLAN_MARKERS),
        (EVIDENCE_TEMPLATE, REQUIRED_TEMPLATE_MARKERS),
        (WORKFLOW_STATUS, REQUIRED_STATUS_MARKERS),
        (BRIDGE_BACKLOG, REQUIRED_BACKLOG_MARKERS),
    ):
        for marker in _missing_markers(path, markers):
            failures.append(
                f"{path.relative_to(PROJECT_ROOT)} missing required marker: {marker}"
            )

    package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    scripts = package.get("scripts") or {}
    if (
        scripts.get("eval:slack:anu60-proof")
        != ".venv/bin/python scripts/validate_anu60_live_slack_proof_packet.py"
    ):
        failures.append(
            "package.json missing eval:slack:anu60-proof script for the ANU-60 proof validator"
        )
    if (
        scripts.get("eval:slack:anu60-preflight")
        != ".venv/bin/python scripts/run_anu60_no_live_preflight.py"
    ):
        failures.append(
            "package.json missing eval:slack:anu60-preflight script "
            "for the ANU-60 no-live preflight"
        )

    return failures


def main() -> int:
    failures = validate_anu60_live_slack_proof_packet()
    if failures:
        print("ANU-60 live Slack proof packet validation failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("Validated ANU-60 live Slack proof packet; no live Slack/API calls made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
