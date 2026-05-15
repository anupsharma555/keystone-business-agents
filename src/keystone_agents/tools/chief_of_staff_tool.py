"""Read-only Slack operations context tools for the KNI Chief of Staff agent."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from keystone_agents.guardrails import keystone_tool_guardrail_kwargs
from keystone_agents.sdk import function_tool

DEFAULT_SLACK_REPO_PATH = Path(
    os.getenv("KNI_SLACK_REPO_PATH")
    or os.getenv("KEYSTONE_SLACK_REPO_PATH")
    or "/Users/anup/gitProjects/keystone-slack"
)
TEXT_EXTENSIONS = frozenset({".md", ".py", ".txt", ".json", ".yaml", ".yml", ".toml", ".sh"})
SKIPPED_DIR_NAMES = frozenset(
    {
        ".git",
        ".local",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
        "dist",
        "build",
    }
)
SENSITIVE_NAME_TERMS = (
    ".env",
    "credential",
    "credentials",
    "secret",
    "token",
    "oauth",
    "private",
    "key",
)
SENSITIVE_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3", ".pem", ".key", ".log"})
MAX_WALK_FILES = 2_000
MAX_SEARCH_RESULTS = 25
MAX_SEARCH_FILE_BYTES = 500_000
MAX_READ_CHARS = 12_000


@dataclass(frozen=True)
class _DocRecord:
    title: str
    url: str
    source_type: str
    keywords: tuple[str, ...]
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "source_type": self.source_type,
            "note": self.note,
        }


OFFICIAL_OPERATIONS_DOCS: tuple[_DocRecord, ...] = (
    _DocRecord(
        title="OpenAI Agents SDK",
        url="https://developers.openai.com/api/docs/guides/agents",
        source_type="openai_docs",
        keywords=("openai", "agents", "sdk", "agent", "tools", "state", "run"),
        note="Use for code-first Agents SDK architecture and runtime choices.",
    ),
    _DocRecord(
        title="OpenAI Agent definitions",
        url="https://developers.openai.com/api/docs/guides/agents/define-agents",
        source_type="openai_docs",
        keywords=("openai", "agent", "definition", "tools", "handoffs", "structured", "output"),
        note="Use for deciding what belongs in one SDK agent.",
    ),
    _DocRecord(
        title="OpenAI orchestration and handoffs",
        url="https://developers.openai.com/api/docs/guides/agents/orchestration",
        source_type="openai_docs",
        keywords=("openai", "orchestration", "handoff", "manager", "agent as tool"),
        note="Use when choosing between a separate agent, handoff, or manager pattern.",
    ),
    _DocRecord(
        title="OpenAI guardrails and human review",
        url="https://developers.openai.com/api/docs/guides/agents/guardrails-approvals",
        source_type="openai_docs",
        keywords=("openai", "guardrails", "approval", "human review", "side effects"),
        note="Use for approval gates before risky operations.",
    ),
    _DocRecord(
        title="Slack Socket Mode",
        url="https://docs.slack.dev/apis/events-api/using-socket-mode",
        source_type="slack_docs",
        keywords=("slack", "socket", "socket mode", "events", "websocket", "app"),
        note="Use for KNI Slack's websocket runtime model.",
    ),
    _DocRecord(
        title="Slack Python Socket Mode client",
        url="https://docs.slack.dev/tools/python-slack-sdk/socket-mode/",
        source_type="slack_docs",
        keywords=("slack", "python", "socket mode", "client", "commands", "messages"),
        note="Use for Python Slack SDK Socket Mode implementation details.",
    ),
    _DocRecord(
        title="Slack slash commands",
        url="https://docs.slack.dev/interactivity/implementing-slash-commands",
        source_type="slack_docs",
        keywords=("slack", "slash", "commands", "ack", "response", "request"),
        note="Use for slash-command routing and response behavior.",
    ),
    _DocRecord(
        title="Slack chat.postMessage",
        url="https://docs.slack.dev/reference/methods/chat.postMessage",
        source_type="slack_docs",
        keywords=("slack", "post", "message", "chat.postmessage", "channel", "write"),
        note="Use only as a reference for post semantics; Chief of Staff v1 cannot post.",
    ),
)

CORE_CONTEXT_SOURCES: tuple[dict[str, Any], ...] = (
    {
        "source_id": "operator_and_agent_policy",
        "status": "always_on",
        "access": "prompt_context",
        "contents": [
            "AGENTS.md",
            "Keystone profile",
            "shared safety policy",
            "memory policy",
            "writing style policy",
            "operator context",
        ],
        "guardrails": ["never overrides no-send or approval gates"],
    },
    {
        "source_id": "keystone_slack_runtime_repo",
        "status": "enabled",
        "access": "read_only_allowlisted_files",
        "contents": [
            "Socket Mode runtime",
            "WorkflowRunner command routing",
            "business-agents bridge",
            "Slack config defaults",
            "workflow family modules",
        ],
        "guardrails": ["no .env, OAuth tokens, databases, logs, or repo writes"],
    },
    {
        "source_id": "official_developer_docs",
        "status": "enabled",
        "access": "curated_official_docs_catalog",
        "contents": ["OpenAI Agents SDK docs", "Slack developer docs"],
        "guardrails": ["use official docs links for volatile SDK or Slack behavior"],
    },
    {
        "source_id": "keystone_local_context",
        "status": "enabled_when_configured",
        "access": "existing local_context tools",
        "contents": [
            "Keystone Neuroinformatics folders",
            "Zotero active/import-cache folders",
            "operator-approved local reference files",
        ],
        "guardrails": [
            "allowlisted folders only",
            "private context is not approval for external use",
        ],
    },
    {
        "source_id": "selected_gmail_context",
        "status": "future_live_gate_or_handoff",
        "access": "selected thread or digest only",
        "contents": ["Gmail thread summaries", "Gmail triage digests", "onboarding thread hints"],
        "guardrails": ["no raw inbox scan by default", "no email sending"],
    },
    {
        "source_id": "selected_calendar_context",
        "status": "future_live_gate_or_handoff",
        "access": "selected window only",
        "contents": ["meeting windows", "calendar briefs", "deadline/prep summaries"],
        "guardrails": ["read-only", "no event creation or updates"],
    },
    {
        "source_id": "github_and_local_repos",
        "status": "future_live_gate_or_allowlisted_local_context",
        "access": "repo-specific read-only context",
        "contents": ["Keystone repos", "issues/PRs when a GitHub connector is explicitly enabled"],
        "guardrails": ["no repo writes", "no broad home-directory scans"],
    },
    {
        "source_id": "business_workflow_state",
        "status": "future_optional_tool",
        "access": "redacted WorkItem and approval summaries",
        "contents": ["pending approvals", "active WorkItems", "recent agent artifacts"],
        "guardrails": ["state informs routing only", "does not grant send approval"],
    },
)


def _json_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)


def _slack_repo_root(repo_path: str | None = None) -> Path:
    return Path(repo_path or DEFAULT_SLACK_REPO_PATH).expanduser().resolve()


def _is_sensitive_path(path: Path) -> bool:
    parts = [part.lower() for part in path.parts]
    name = path.name.lower()
    if any(part in SKIPPED_DIR_NAMES for part in parts):
        return True
    if any(term in name for term in SENSITIVE_NAME_TERMS):
        return True
    return path.suffix.lower() in SENSITIVE_SUFFIXES


def _is_allowed_text_file(path: Path) -> bool:
    return path.suffix.lower() in TEXT_EXTENSIONS and not _is_sensitive_path(path)


def _resolve_repo_file(relative_path: str, *, repo_path: str | None = None) -> tuple[Path, Path]:
    if not str(relative_path or "").strip():
        raise ValueError("relative_path is required.")
    raw_relative = Path(relative_path)
    if raw_relative.is_absolute():
        raise ValueError("relative_path must be relative to the Keystone Slack repo.")
    root = _slack_repo_root(repo_path)
    candidate = (root / raw_relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("relative_path escapes the Keystone Slack repo.")
    if not candidate.is_file():
        raise FileNotFoundError(f"Slack repo file not found: {relative_path}")
    if not _is_allowed_text_file(candidate):
        raise ValueError("Only allowlisted non-sensitive text files may be read.")
    return root, candidate


def _relative_to_repo(root: Path, path: Path) -> str:
    return str(path.resolve().relative_to(root.resolve()))


def _walk_repo_text_files(root: Path) -> Iterable[Path]:
    yielded = 0
    for current_root, dirs, files in os.walk(root):
        dirs[:] = [
            dirname
            for dirname in dirs
            if dirname not in SKIPPED_DIR_NAMES and not dirname.startswith(".")
        ]
        for filename in files:
            if filename.startswith("."):
                continue
            path = Path(current_root) / filename
            if not _is_allowed_text_file(path):
                continue
            yielded += 1
            if yielded > MAX_WALK_FILES:
                return
            yield path


def _channel_name(value: str) -> str:
    return value.strip().lstrip("#")


def _extract_default_channels(config_text: str) -> dict[str, str]:
    channels: dict[str, str] = {}
    pattern = re.compile(
        r"(?P<field>[a-z_]*channel)\s*=\s*get\(\s*\"(?P<env>[A-Z0-9_]+)\"\s*,\s*\"(?P<default>[^\"]+)\"",
        re.MULTILINE,
    )
    for match in pattern.finditer(config_text):
        channels[match.group("field")] = match.group("default")
    return channels


def _capability_for_topic(topic: str) -> dict[str, Any]:
    normalized = " ".join(topic.lower().strip().split())
    explicit_channel = ""
    channel_match = re.search(r"#([a-z0-9_-]+)", topic, flags=re.I)
    if channel_match:
        explicit_channel = channel_match.group(1)

    if _looks_like_scope_question(normalized):
        return {
            "workflow_type": "slack-runtime-review",
            "command_text": (
                "@KNI chief of staff <calendar, Gmail, channel, or Slack operations request>"
            ),
            "target_channel": explicit_channel or "current-thread",
            "requires_live_connector": False,
            "side_effect_policy": (
                "explain Chief of Staff scope; no Slack post, Gmail send, calendar write, "
                "or repo write"
            ),
            "notes": [
                (
                    "Plan KNI Slack routing across calendar, Gmail, business-agent, "
                    "and runtime workflows."
                ),
                (
                    "Inspect read-only Slack runtime repo context, curated official docs, "
                    "and allowlisted Keystone/Zotero local context."
                ),
                "Recommend existing KNI commands and target channels for human review.",
                (
                    "Represent selected Gmail, Calendar, GitHub, and workflow-state context "
                    "as explicit gated sources."
                ),
            ],
        }
    if any(
        term in normalized
        for term in (
            "github",
            "repo",
            "repository",
            "zotero",
            "developer docs",
            "agents sdk",
            "slack docs",
        )
    ):
        return {
            "workflow_type": "slack-runtime-review",
            "command_text": "@KNI chief of staff review <repo, docs, or context question>",
            "target_channel": explicit_channel or "current-thread",
            "requires_live_connector": False,
            "side_effect_policy": "review read-only context and recommend a safe next step",
            "notes": [
                "Use read-only repo, curated docs, and allowlisted local context tools.",
                "Do not write repositories, post to Slack, or expose secret-bearing files.",
            ],
        }
    if any(term in normalized for term in ("calendar", "meeting", "meetings", "agenda")):
        return {
            "workflow_type": "calendar-read",
            "command_text": "/kni calendar today",
            "target_channel": explicit_channel or "calendar",
            "requires_live_connector": True,
            "side_effect_policy": "read-only calendar brief; post only after human approval",
            "notes": [
                "Use `/kni calendar next week` or `/kni calendar month` for wider windows.",
                "KNI Slack safety rules do not allow direct calendar event creation.",
            ],
        }
    if any(term in normalized for term in ("gmail", "email", "inbox", "mail", "onboarding")):
        command = (
            "/kni gmail summarize onboarding"
            if "onboard" in normalized
            else "/kni gmail triage today"
        )
        return {
            "workflow_type": "gmail-summary" if "summar" in command else "gmail-triage",
            "command_text": command,
            "target_channel": explicit_channel or "gmail",
            "requires_live_connector": True,
            "side_effect_policy": "read-only Gmail summary or triage; no email send",
            "notes": [
                "Use `/kni-gmail-summarize <thread-hint>` for one thread.",
                "Use `/kni-gmail-all today` for a full account window.",
            ],
        }
    if any(
        term in normalized for term in ("business agent", "research", "opportunity", "outreach")
    ):
        return {
            "workflow_type": "business-agents-route",
            "command_text": "@KNI business agents <request>",
            "target_channel": explicit_channel or "ai-agents-workflow",
            "requires_live_connector": False,
            "side_effect_policy": (
                "delegate to Keystone business agents; drafts and writes stay gated"
            ),
            "notes": [
                (
                    "Use the business-agents bridge for research, scout, triage, "
                    "and outreach requests."
                ),
                "Outreach stays draft-only and approval-gated.",
            ],
        }
    return {
        "workflow_type": "clarification",
        "command_text": "/kni help",
        "target_channel": explicit_channel,
        "requires_live_connector": False,
        "side_effect_policy": "ask for the target workflow, source, and channel",
        "notes": ["No safe Slack workflow could be selected without clarification."],
    }


def _looks_like_scope_question(normalized: str) -> bool:
    if not normalized:
        return False
    scope_terms = (
        "what is your scope",
        "what's your scope",
        "your scope",
        "what can you do",
        "what are you able to do",
        "what do you do",
        "your role",
        "what is your role",
        "scope for this slack",
        "in this slack",
        "this slack",
        "capabilities",
    )
    return any(term in normalized for term in scope_terms)


@function_tool(**keystone_tool_guardrail_kwargs())
def list_chief_of_staff_context_sources() -> str:
    """List the Chief of Staff core context model and access gates."""

    return _json_payload(
        {
            "mode": "chief_of_staff_context_sources",
            "send_enabled": False,
            "slack_post_allowed": False,
            "repo_write_enabled": False,
            "sources": list(CORE_CONTEXT_SOURCES),
            "notes": [
                "Use always-on prompt and read-only repo/docs/local-context sources first.",
                (
                    "Use selected Gmail, Calendar, GitHub, and workflow-state context only "
                    "through explicit gates or future handoffs."
                ),
                (
                    "Core context never authorizes Slack posting, Gmail sending, calendar "
                    "writes, or repository writes."
                ),
            ],
        }
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def summarize_slack_runtime_config(repo_path: str | None = None) -> str:
    """Summarize the local KNI Slack runtime without reading secrets or state."""

    root = _slack_repo_root(repo_path)
    key_files = (
        "AGENTS.md",
        "README.md",
        "ARCHITECTURE.md",
        "SLACK_APP_SETUP.md",
        "kni_integrations/config.py",
        "kni_integrations/slack_socket_mode.py",
        "kni_integrations/workflow_runner.py",
        "kni_integrations/business_agents_bridge.py",
    )
    present = {relative: (root / relative).is_file() for relative in key_files}
    channels: dict[str, str] = {}
    config_path = root / "kni_integrations" / "config.py"
    if config_path.is_file() and _is_allowed_text_file(config_path):
        channels = _extract_default_channels(
            config_path.read_text(encoding="utf-8", errors="ignore")
        )

    return _json_payload(
        {
            "mode": "slack_runtime_config_summary",
            "repo_path": str(root),
            "repo_present": root.is_dir(),
            "send_enabled": False,
            "repo_write_enabled": False,
            "key_files_present": present,
            "default_channels": channels,
            "runtime_layers": [
                "Slack slash command or app mention enters the socket-mode runtime.",
                "WorkflowRunner routes commands and creates WorkflowReceipt objects.",
                "slack_socket_mode delivers ephemeral responses, posts, or canvases.",
                "business_agents_bridge calls this business-agents repo for agent workflows.",
            ],
            "safety_boundaries": [
                "Chief of Staff tools are read-only.",
                "Slack posting is not allowed from this agent.",
                "Gmail sends and calendar writes remain blocked.",
                "Secrets, local databases, OAuth files, logs, and .env files are not read.",
            ],
        }
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def search_slack_repo_context(
    query: str,
    repo_path: str | None = None,
    max_results: int = 8,
) -> str:
    """Search non-sensitive KNI Slack repo text files and return capped snippets."""

    normalized_query = " ".join(str(query or "").lower().split())
    if not normalized_query:
        raise ValueError("query is required.")
    terms = normalized_query.split()
    root = _slack_repo_root(repo_path)
    limit = max(1, min(max_results, MAX_SEARCH_RESULTS))
    matches: list[dict[str, Any]] = []
    if not root.is_dir():
        return _json_payload(
            {
                "mode": "slack_repo_context_search",
                "query": query,
                "repo_path": str(root),
                "repo_present": False,
                "send_enabled": False,
                "raw_file_bodies_included": False,
                "matches": [],
            }
        )

    for path in _walk_repo_text_files(root):
        if len(matches) >= limit:
            break
        try:
            if path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lowered = text.lower()
        if not all(term in lowered for term in terms):
            continue
        indexes = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
        index = min(indexes) if indexes else 0
        start = max(0, index - 220)
        end = min(len(text), index + 520)
        snippet = " ".join(text[start:end].split())
        matches.append(
            {
                "relative_path": _relative_to_repo(root, path),
                "snippet": snippet,
            }
        )

    return _json_payload(
        {
            "mode": "slack_repo_context_search",
            "query": query,
            "repo_path": str(root),
            "repo_present": True,
            "send_enabled": False,
            "raw_file_bodies_included": False,
            "matches": matches,
        }
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def read_slack_repo_context_file(
    relative_path: str,
    repo_path: str | None = None,
    max_chars: int = 4_000,
) -> str:
    """Read one capped, non-sensitive text file from the KNI Slack repo."""

    root, path = _resolve_repo_file(relative_path, repo_path=repo_path)
    limit = max(1, min(max_chars, MAX_READ_CHARS))
    text = path.read_text(encoding="utf-8", errors="ignore")
    return _json_payload(
        {
            "mode": "slack_repo_context_file",
            "repo_path": str(root),
            "relative_path": _relative_to_repo(root, path),
            "send_enabled": False,
            "repo_write_enabled": False,
            "truncated": len(text) > limit,
            "content": text[:limit],
        }
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def lookup_slack_workflow_capability(topic: str) -> str:
    """Map an operations request to the safest existing KNI Slack workflow."""

    capability = _capability_for_topic(topic)
    return _json_payload(
        {
            "mode": "slack_workflow_capability",
            "topic": topic,
            "send_enabled": False,
            "slack_post_allowed": False,
            "capability": capability,
        }
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def search_official_operations_docs(query: str, max_results: int = 5) -> str:
    """Search a curated catalog of official OpenAI and Slack operation docs."""

    normalized_query = " ".join(str(query or "").lower().split())
    terms = normalized_query.split()
    limit = max(1, min(max_results, len(OFFICIAL_OPERATIONS_DOCS)))
    scored: list[tuple[int, _DocRecord]] = []
    for doc in OFFICIAL_OPERATIONS_DOCS:
        haystack = " ".join((doc.title, doc.note, " ".join(doc.keywords), doc.url)).lower()
        score = sum(1 for term in terms if term in haystack)
        if score or not terms:
            scored.append((score, doc))
    scored.sort(key=lambda item: (-item[0], item[1].title))
    return _json_payload(
        {
            "mode": "official_operations_docs_search",
            "query": query,
            "send_enabled": False,
            "results": [doc.to_dict() for _, doc in scored[:limit]],
        }
    )
