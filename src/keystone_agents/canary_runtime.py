"""Mutation-disabled runtime policy for reversible Slack acceptance canaries."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from keystone_agents.context_env import resolve_context_env_value

CANARY_RUNTIME_SCHEMA: Final[str] = "keystone.no_write_canary_runtime.v1"
CANARY_STATE_DIR_ENV: Final[str] = "KEYSTONE_CANARY_STATE_DIR"
CANARY_MAX_OPENAI_REQUESTS_ENV: Final[str] = "KEYSTONE_CANARY_MAX_OPENAI_REQUESTS"
CANARY_ALLOWED_AGENTS: Final[frozenset[str]] = frozenset(
    {
        "business_research_analyst",
        "gmail_triage",
        "google_workspace_context_agent",
        "opportunity_scout",
        "outreach_composer",
    }
)
CANARY_SCRUBBED_ENV_KEYS: Final[frozenset[str]] = frozenset(
    {
        "KNI_BUSINESS_AGENT_ACTION_SECRET",
        "KNI_SLACK_ACTION_SECRET",
        "KNI_SLACK_APP_TOKEN",
        "KNI_SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "SLACK_SIGNING_SECRET",
    }
)

# These values are deliberately applied after the Slack bridge has loaded both
# repos' environment files. They are defense in depth around the typed Python
# approval and exact-identity gates, not a replacement for those gates.
MUTATION_DISABLED_ENV: Final[dict[str, str]] = {
    "AIRTABLE_ALLOW_ATTACHMENT_UPLOADS": "false",
    "AIRTABLE_ALLOW_DUPLICATE_CLEANUP": "false",
    "AIRTABLE_ALLOW_TEST_DELETES": "false",
    "AIRTABLE_ALLOW_WRITES": "false",
    "AIRTABLE_WRITE_DRY_RUN": "true",
    "AUTO_SEND_EMAIL": "false",
    "GOOGLE_WORKSPACE_WRITES_ENABLED": "false",
    "KEYSTONE_AIRTABLE_ALLOWED_OPERATION": "read",
    "KEYSTONE_AIRTABLE_ALLOW_TEST_BASE_WRITES": "false",
    "KEYSTONE_ENABLE_LIVE_CRM": "false",
    "KEYSTONE_ENABLE_LIVE_GMAIL": "false",
    "KEYSTONE_ENABLE_LIVE_SLACK": "false",
    "KEYSTONE_ENABLE_GEMINI_FALLBACK": "false",
    "KEYSTONE_GMAIL_ALLOW_DRAFT_ATTACHMENTS": "false",
    "KEYSTONE_GMAIL_ALLOW_MAILBOX_WRITES": "false",
    "KEYSTONE_GMAIL_ALLOW_TEST_DRAFT_DELETES": "false",
    "KEYSTONE_GMAIL_ALLOW_TEST_SENDS": "false",
    "KEYSTONE_GMAIL_TEST_SEND_MAX": "0",
    "KEYSTONE_GMAIL_TEST_SEND_RECIPIENT": "",
    "KEYSTONE_GMAIL_TEST_SENDER": "",
    "KEYSTONE_GOOGLE_CALENDAR_ALLOW_WRITES": "false",
    "KEYSTONE_GOOGLE_WORKSPACE_ALLOW_TEST_LIFECYCLE": "false",
    "KEYSTONE_PRESENTATION_ALLOW_DERIVED_WRITES": "false",
    "KEYSTONE_PLAYWRIGHT_ENABLED": "false",
    "KEYSTONE_SANDBOX_LIVE": "false",
    "KEYSTONE_SANDBOX_SEARCH_REVIEW_AUTO_EXECUTE": "false",
    "KEYSTONE_SANDBOX_SEARCH_REVIEW_LIVE": "false",
    "KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES": "0",
    "KEYSTONE_SDK_STRUCTURED_OUTPUT_MAX_RETRIES": "0",
    "KEYSTONE_SLACK_ALLOW_TEST_MESSAGE_WRITES": "false",
    "KEYSTONE_TRACING_DISABLED": "true",
    "KEYSTONE_ZOTERO_ALLOW_TEST_LIBRARY_WRITES": "false",
    "KEYSTONE_ZOTERO_ALLOW_TEST_NOTE_WRITES": "false",
    "KEYSTONE_ZOTERO_IMPORTER_ALLOW_WRITES": "false",
    # A nonempty invalid sentinel prevents the linked-context fallback from
    # reloading the sibling Slack repo's real bot token for this child.
    "SLACK_BOT_TOKEN": "disabled-in-kba-canary",
    "SLACK_CHANNEL_APPROVALS": "",
}


@dataclass(frozen=True)
class CanaryRuntimeConfig:
    """Validated local state and request budget for one canary worker."""

    repo_root: Path
    state_dir: Path
    max_openai_requests: int

    @classmethod
    def from_environment(
        cls,
        *,
        repo_root: str | Path,
        env: Mapping[str, str] | None = None,
        allowed_state_roots: Sequence[str | Path] | None = None,
    ) -> CanaryRuntimeConfig:
        source = os.environ if env is None else env
        raw_state_dir = str(source.get(CANARY_STATE_DIR_ENV) or "").strip()
        if not raw_state_dir:
            raise ValueError(f"{CANARY_STATE_DIR_ENV} must name an isolated temp directory.")
        raw_max_requests = str(source.get(CANARY_MAX_OPENAI_REQUESTS_ENV) or "").strip()
        try:
            max_requests = int(raw_max_requests)
        except ValueError as exc:
            raise ValueError(f"{CANARY_MAX_OPENAI_REQUESTS_ENV} must be an integer.") from exc
        if max_requests < 1 or max_requests > 12:
            raise ValueError(f"{CANARY_MAX_OPENAI_REQUESTS_ENV} must be between 1 and 12.")
        return cls(
            repo_root=Path(repo_root).expanduser().resolve(),
            state_dir=validate_canary_state_dir(
                raw_state_dir,
                allowed_roots=allowed_state_roots,
            ),
            max_openai_requests=max_requests,
        )

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.state_dir / 'keystone-agents.sqlite3'}"

    @property
    def google_workspace_token_path(self) -> Path:
        return self.state_dir / "google-workspace-oauth-token.json"

    def stage_google_workspace_token(
        self,
        source_path: str | Path | None = None,
    ) -> Path:
        """Copy a provider token once so refresh writes stay in canary state."""

        target = self.google_workspace_token_path
        if target.is_file():
            target.chmod(0o600)
            return target
        source = (
            Path(source_path).expanduser().resolve()
            if source_path is not None
            else _google_workspace_token_source(self.repo_root)
        )
        if source is None or not source.is_file():
            return target
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o600)
        return target

    def build_child_environment(
        self,
        base_env: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Return the inherited read/model environment with writes disabled."""

        child = dict(os.environ if base_env is None else base_env)
        for key in CANARY_SCRUBBED_ENV_KEYS:
            child.pop(key, None)
        child.update(MUTATION_DISABLED_ENV)
        child.update(
            {
                "DATABASE_URL": self.database_url,
                "GOOGLE_TOKEN_FILE": str(self.google_workspace_token_path),
                "GOOGLE_WORKSPACE_OAUTH_TOKEN_PATH": str(self.google_workspace_token_path),
                "KEYSTONE_BENCHMARK_DB": str(self.state_dir / "benchmark-evals.sqlite3"),
                "KEYSTONE_BUSINESS_AGENTS_DATABASE_URL": self.database_url,
                "KEYSTONE_CANARY_RUNTIME": "true",
                CANARY_MAX_OPENAI_REQUESTS_ENV: str(self.max_openai_requests),
                CANARY_STATE_DIR_ENV: str(self.state_dir),
                "KEYSTONE_CONTEXT_CONFIG_OVERRIDE": "false",
                "KEYSTONE_CONTEXT_CONFIG_OVERRIDE_KEYS": "",
                "KEYSTONE_HOME": str(self.state_dir / ".keystone"),
                "KEYSTONE_LIVE_MODEL_MAX_RETRIES": "0",
                "KEYSTONE_PLAYWRIGHT_IMAGE_DIR": str(self.state_dir / "playwright-images"),
                "KEYSTONE_PRESENTATION_DERIVED_ROOT": str(self.state_dir / "presentation-derived"),
                "KEYSTONE_PROMPTFOO_DASHBOARD_PATH": str(
                    self.state_dir / "promptfoo-dashboard.html"
                ),
                "KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB": str(self.state_dir / "human-reviews.sqlite3"),
                "KEYSTONE_RUNTIME_STATE_DIR": str(self.state_dir),
                "KEYSTONE_SDK_SESSION_DB": str(self.state_dir / "sdk-sessions.sqlite3"),
                "KEYSTONE_TAVILY_USAGE_PATH": str(self.state_dir / "tavily-usage.json"),
                "KEYSTONE_TRACE_SUMMARY_DB": str(self.state_dir / "trace-summaries.sqlite3"),
                "KNI_BUSINESS_AGENTS_DATABASE_URL": self.database_url,
                "PYTHONPATH": _canary_pythonpath(
                    self.repo_root,
                    child.get("PYTHONPATH", ""),
                ),
            }
        )
        return child

    def validate_python_arguments(self, argv: Sequence[str]) -> str:
        """Admit only one named-agent natural-language ask."""

        arguments = [str(argument) for argument in argv]
        if arguments[:3] != ["-m", "keystone_agents.cli", "ask"]:
            raise ValueError("Canary execution only allows '-m keystone_agents.cli ask'.")
        if arguments.count("-m") != 1 or "-c" in arguments:
            raise ValueError("Canary execution does not allow alternate Python entrypoints.")
        agent_options = [
            argument
            for argument in arguments
            if argument == "--agent" or argument.startswith("--agent=")
        ]
        if len(agent_options) != 1:
            raise ValueError("Canary execution requires exactly one --agent option.")
        agent = _option_value(arguments, "--agent")
        if agent not in CANARY_ALLOWED_AGENTS:
            allowed = ", ".join(sorted(CANARY_ALLOWED_AGENTS))
            raise ValueError(f"Canary --agent must be one of: {allowed}.")
        return agent

    def rewrite_python_arguments(self, argv: Sequence[str]) -> list[str]:
        """Confine bridge-supplied storage and request ceilings."""

        self.validate_python_arguments(argv)
        rewritten: list[str] = []
        index = 0
        while index < len(argv):
            argument = str(argv[index])
            if argument == "--database-url":
                rewritten.extend(("--database-url", self.database_url))
                index += 2 if index + 1 < len(argv) else 1
                continue
            if argument.startswith("--database-url="):
                rewritten.append(f"--database-url={self.database_url}")
                index += 1
                continue
            if argument == "--max-openai-requests":
                current = _bounded_request_value(
                    argv[index + 1] if index + 1 < len(argv) else "",
                    ceiling=self.max_openai_requests,
                )
                rewritten.extend(("--max-openai-requests", str(current)))
                index += 2 if index + 1 < len(argv) else 1
                continue
            if argument.startswith("--max-openai-requests="):
                _, _, raw_value = argument.partition("=")
                rewritten.append(
                    "--max-openai-requests="
                    f"{_bounded_request_value(raw_value, ceiling=self.max_openai_requests)}"
                )
                index += 1
                continue
            rewritten.append(argument)
            index += 1

        ask_index = _canonical_ask_index(rewritten)
        if ask_index is not None and not _has_option(
            rewritten,
            "--max-openai-requests",
        ):
            rewritten[ask_index + 1 : ask_index + 1] = [
                "--max-openai-requests",
                str(self.max_openai_requests),
            ]
        return rewritten

    def public_manifest(self) -> dict[str, object]:
        """Return a secret-free summary suitable for local verification."""

        return {
            "schema": CANARY_RUNTIME_SCHEMA,
            "repo_root": str(self.repo_root),
            "state_dir": str(self.state_dir),
            "database_name": Path(self.database_url.removeprefix("sqlite:///")).name,
            "max_openai_requests": self.max_openai_requests,
            "allowed_agents": sorted(CANARY_ALLOWED_AGENTS),
            "mutation_disabled_keys": sorted(MUTATION_DISABLED_ENV),
            "scrubbed_secret_keys": sorted(CANARY_SCRUBBED_ENV_KEYS),
            "provider_read_receipt_contains_raw_content": False,
            "provider_read_policy_allowed": True,
            "live_model_policy_allowed": True,
            "openai_request_limit_kind": "admission_ceiling",
        }


def validate_canary_state_dir(
    value: str | Path,
    *,
    allowed_roots: Sequence[str | Path] | None = None,
) -> Path:
    """Require a specific child directory under a system temp root."""

    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{CANARY_STATE_DIR_ENV} must be an absolute path.")
    resolved = candidate.resolve()
    roots = tuple(
        Path(root).expanduser().resolve()
        for root in (
            allowed_roots
            if allowed_roots is not None
            else (Path("/private/tmp"), Path(tempfile.gettempdir()))
        )
    )
    if resolved in roots or not any(resolved.is_relative_to(root) for root in roots):
        raise ValueError(f"{CANARY_STATE_DIR_ENV} must be a child of an approved temp root.")
    return resolved


def _bounded_request_value(value: object, *, ceiling: int) -> int:
    try:
        requested = int(str(value))
    except ValueError:
        return ceiling
    return max(1, min(requested, ceiling))


def _canonical_ask_index(argv: Sequence[str]) -> int | None:
    for index in range(len(argv) - 2):
        if list(argv[index : index + 3]) == ["-m", "keystone_agents.cli", "ask"]:
            return index + 2
    return None


def _has_option(argv: Sequence[str], option: str) -> bool:
    return any(
        str(argument) == option or str(argument).startswith(f"{option}=") for argument in argv
    )


def _option_value(argv: Sequence[str], option: str) -> str:
    for index, argument in enumerate(argv):
        value = str(argument)
        if value == option:
            return str(argv[index + 1]).strip() if index + 1 < len(argv) else ""
        if value.startswith(f"{option}="):
            return value.partition("=")[2].strip()
    return ""


def _google_workspace_token_source(repo_root: Path) -> Path | None:
    for key in ("GOOGLE_WORKSPACE_OAUTH_TOKEN_PATH", "GOOGLE_TOKEN_FILE"):
        resolution = resolve_context_env_value(key)
        raw_path = resolution.value.strip()
        if not raw_path:
            continue
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (resolution.source_repo or repo_root) / path
        return path.expanduser().resolve()
    return None


def _canary_pythonpath(repo_root: Path, current: str) -> str:
    entries = [str(repo_root), str(repo_root / "src")]
    for entry in str(current or "").split(os.pathsep):
        clean = entry.strip()
        if clean and clean not in entries:
            entries.append(clean)
    return os.pathsep.join(entries)


__all__ = [
    "CANARY_MAX_OPENAI_REQUESTS_ENV",
    "CANARY_ALLOWED_AGENTS",
    "CANARY_RUNTIME_SCHEMA",
    "CANARY_SCRUBBED_ENV_KEYS",
    "CANARY_STATE_DIR_ENV",
    "CanaryRuntimeConfig",
    "MUTATION_DISABLED_ENV",
    "validate_canary_state_dir",
]
