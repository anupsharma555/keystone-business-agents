"""Print the first bounded KBA model-validation batch without executing it."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

MODEL = "gpt-5.4-mini"
EXPECTED_REQUESTS_MIN = 7
EXPECTED_REQUESTS_MAX = 10
HARD_REQUEST_STOP = 10
HARD_BUDGET_USD = 2.0


@dataclass(frozen=True)
class Scenario:
    id: str
    proof: str
    expected_requests_min: int
    expected_requests_max: int
    provider_reads: tuple[str, ...]
    provider_writes: tuple[str, ...]
    command_shape: tuple[str, ...]
    stop_on: tuple[str, ...]


def build_plan() -> dict[str, object]:
    """Return an inspectable plan; this module deliberately has no executor."""

    scenarios = (
        Scenario(
            id="gmail_today_priority_summary",
            proof="Interpret a natural request to summarize today's relevant mailbox context.",
            expected_requests_min=1,
            expected_requests_max=1,
            provider_reads=("gmail",),
            provider_writes=(),
            command_shape=(
                ".venv/bin/python", "scripts/run_gmail_triage.py", "--live-gmail",
                "--allow-inbox", "--gmail-query", "newer_than:1d", "--max-messages",
                "5", "--priority-grouping", "--request",
                "Summarize my important emails from today and tell me what needs action.",
                "--live-sdk", "--no-dry-run", "--json",
            ),
            stop_on=("missing_usage", "wrong_date_scope", "private_content_in_receipt"),
        ),
        Scenario(
            id="gmail_selected_thread_draft",
            proof="Find one bounded test-relevant thread and produce useful draft-only reply text.",
            expected_requests_min=1,
            expected_requests_max=1,
            provider_reads=("gmail",),
            provider_writes=(),
            command_shape=(
                ".venv/bin/python", "scripts/run_gmail_triage.py", "--live-gmail",
                "--allow-inbox", "--gmail-query", "<runtime-bounded-query>",
                "--max-messages", "1", "--request",
                "Find this email, summarize the ask, and draft a concise reply without sending.",
                "--live-sdk", "--sdk-session", "--sdk-session-id",
                "<runtime-session-id>", "--no-dry-run", "--json",
            ),
            stop_on=("thread_mismatch", "unsupported_claim", "send_or_write_attempt"),
        ),
        Scenario(
            id="gmail_same_session_revision",
            proof=(
                "Interpret a natural follow-up and revise the prior draft without "
                "losing context."
            ),
            expected_requests_min=1,
            expected_requests_max=1,
            provider_reads=("gmail",),
            provider_writes=(),
            command_shape=(
                ".venv/bin/python", "scripts/run_gmail_triage.py", "--live-gmail",
                "--allow-inbox", "--gmail-query", "<same-runtime-bounded-query>",
                "--max-messages", "1", "--request",
                "Make the existing draft shorter and warmer; keep the approved "
                "facts and do not send.",
                "--live-sdk", "--sdk-session", "--sdk-session-id",
                "<same-runtime-session-id>", "--no-dry-run", "--json",
            ),
            stop_on=("new_draft_instead_of_revision", "context_loss", "send_or_write_attempt"),
        ),
        Scenario(
            id="graph_research_to_draft",
            proof=(
                "Complete a fixed-context research-to-draft job through the graph, "
                "not just its mechanics."
            ),
            expected_requests_min=4,
            expected_requests_max=7,
            provider_reads=(),
            provider_writes=(),
            command_shape=(
                ".venv/bin/python", "scripts/ask_agent.py",
                "@KNI use the supplied email and company packet to research the company, "
                "then prepare a source-bounded reply draft for review. Do not search live, "
                "send, post, or write externally.",
                "--context-file", "<runtime-sanitized-context-file>", "--live-sdk",
                "--sdk-session", "--sdk-session-id", "<runtime-graph-session-id>",
                "--max-manager-steps", "4", "--json",
            ),
            stop_on=("workflow_completion_without_job_completion", "source_loss",
                     "unexpected_retry", "side_effect"),
        ),
    )
    expected_min = sum(item.expected_requests_min for item in scenarios)
    expected_max = sum(item.expected_requests_max for item in scenarios)
    if (expected_min, expected_max) != (EXPECTED_REQUESTS_MIN, EXPECTED_REQUESTS_MAX):
        raise RuntimeError("Scenario request estimates do not match the batch contract.")
    return {
        "status": "plan_only",
        "execution_available": False,
        "openai_requests_made": 0,
        "model": MODEL,
        "expected_requests": {"min": expected_min, "max": expected_max},
        "hard_stops": {
            "requests": HARD_REQUEST_STOP,
            "budget_usd": HARD_BUDGET_USD,
            "first_shared_failure": True,
            "unexpected_retry": True,
            "missing_usage_or_trace": True,
            "any_side_effect": True,
        },
        "required_before_execution": (
            "explicit user approval for this exact batch",
            "refresh the existing logged-in Chrome billing tab immediately before run 1",
            "record the fresh credit balance and observation timestamp",
            "confirm KEYSTONE_OPENAI_API_KEY without printing it",
            "prepare a sanitized fixed-context graph packet",
            "create isolated runtime session IDs and receipt paths",
        ),
        "sequential": True,
        "live_search": False,
        "provider_writes": False,
        "scenarios": tuple(asdict(item) for item in scenarios),
    }


def main() -> int:
    print(json.dumps(build_plan(), ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
