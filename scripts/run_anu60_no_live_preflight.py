from __future__ import annotations

import ast
import hashlib
import importlib.util
import os
import re
import subprocess
import sys
import unittest
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SLACK_REPO_ENV = "KEYSTONE_SLACK_REPO"
EXPECTED_SLACK_REPO_NAME = "keystone-slack"
EXPANSION_ARTIFACT = "artifacts/anu60_expansion_gate_after_acceptance_map.json"

SIBLING_BRIDGE_TESTS = (
    "test_direct_rss_context_agent_request_renders_human_summary",
    "test_direct_preprints_context_agent_request_renders_human_summary",
    "test_conversational_named_agent_request_uses_canonical_ask_and_human_summary",
    "test_focused_company_brief_prefers_human_summary_over_metadata",
    "test_focused_company_brief_renders_links_contacts_and_clean_copy",
    "test_business_agents_run_command_timeout_returns_structured_failure_and_kills_group",
    "test_business_agents_streaming_run_command_timeout_kills_group",
    "test_business_agents_run_agent_submission_blocked_preflight_renders_blocked",
)

_REQUIRED_SLACK_REPO_FILES = (
    "AGENTS.md",
    "README.md",
    "kni_integrations/business_agents_bridge.py",
    "tests/test_app_mentions.py",
)
_LIVE_CREDENTIAL_KEYS = (
    "KEYSTONE_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "SERPER_API_KEY",
    "TAVILY_API_KEY",
    "EXA_API_KEY",
    "FIRECRAWL_API_KEY",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_USER_TOKEN",
    "SLACK_WEBHOOK_URL",
    "SLACK_SIGNING_SECRET",
    "GOOGLE_APPLICATION_CREDENTIALS",
)


class SiblingRepoResolutionError(RuntimeError):
    """The ANU-60 fixture repository could not be resolved or verified."""


@dataclass(frozen=True)
class SiblingSlackRepo:
    root: Path
    source: str
    git_toplevel: Path
    origin_repo_name: str
    test_file: Path
    test_file_sha256: str


def _git_value(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        text=True,
        capture_output=True,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        operation = " ".join(args)
        raise SiblingRepoResolutionError(
            f"Git metadata check failed for {root}: git {operation}"
        )
    return value


def _remote_repo_name(remote: str) -> str:
    tail = re.split(r"[/:]", str(remote or "").strip().rstrip("/"))[-1]
    return tail.removesuffix(".git")


def _declared_fixture_methods(test_file: Path) -> set[str]:
    try:
        tree = ast.parse(test_file.read_text(encoding="utf-8"), filename=str(test_file))
    except (OSError, SyntaxError) as exc:
        raise SiblingRepoResolutionError(
            f"Sibling fixture file is not readable Python: {test_file}"
        ) from exc
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "SlackAppMentionTests":
            return {
                child.name
                for child in node.body
                if isinstance(child, ast.FunctionDef)
            }
    raise SiblingRepoResolutionError(
        f"Sibling fixture class SlackAppMentionTests is missing: {test_file}"
    )


def _validate_sibling_slack_repo(candidate: Path, *, source: str) -> SiblingSlackRepo:
    if candidate.is_symlink():
        raise SiblingRepoResolutionError(
            f"Resolved keystone-slack candidate must not be a symlink: {candidate}"
        )
    root = candidate.expanduser().resolve()
    if not root.is_dir():
        raise SiblingRepoResolutionError(f"Missing keystone-slack directory: {root}")
    missing = [
        relative
        for relative in _REQUIRED_SLACK_REPO_FILES
        if not (root / relative).is_file()
    ]
    if missing:
        raise SiblingRepoResolutionError(
            f"Candidate {root} is missing required keystone-slack files: {', '.join(missing)}"
        )

    git_toplevel = Path(_git_value(root, "rev-parse", "--show-toplevel")).resolve()
    if git_toplevel != root:
        raise SiblingRepoResolutionError(
            f"Candidate {root} is nested inside a different Git repository: {git_toplevel}"
        )
    origin_repo_name = _remote_repo_name(
        _git_value(root, "config", "--get", "remote.origin.url")
    )
    if origin_repo_name != EXPECTED_SLACK_REPO_NAME:
        raise SiblingRepoResolutionError(
            f"Candidate {root} origin repository name is {origin_repo_name!r}, "
            f"expected {EXPECTED_SLACK_REPO_NAME!r}"
        )
    _git_value(
        root,
        "ls-files",
        "--error-unmatch",
        "kni_integrations/business_agents_bridge.py",
        "tests/test_app_mentions.py",
    )

    test_file = (root / "tests/test_app_mentions.py").resolve()
    if test_file.is_symlink() or not test_file.is_relative_to(root):
        raise SiblingRepoResolutionError(
            f"Sibling fixture must be a regular file inside {root}: {test_file}"
        )
    methods = _declared_fixture_methods(test_file)
    missing_methods = [name for name in SIBLING_BRIDGE_TESTS if name not in methods]
    if missing_methods:
        raise SiblingRepoResolutionError(
            "Sibling SlackAppMentionTests is missing selected ANU-60 fixtures: "
            + ", ".join(missing_methods)
        )
    return SiblingSlackRepo(
        root=root,
        source=source,
        git_toplevel=git_toplevel,
        origin_repo_name=origin_repo_name,
        test_file=test_file,
        test_file_sha256=hashlib.sha256(test_file.read_bytes()).hexdigest(),
    )


def resolve_sibling_slack_repo(
    project_root: Path = PROJECT_ROOT,
    *,
    environ: Mapping[str, str] | None = None,
) -> SiblingSlackRepo:
    """Resolve one verified keystone-slack checkout without directory scanning."""

    env = os.environ if environ is None else environ
    configured = str(env.get(SLACK_REPO_ENV) or "").strip()
    if configured:
        configured_path = Path(configured).expanduser()
        if not configured_path.is_absolute():
            raise SiblingRepoResolutionError(
                f"{SLACK_REPO_ENV} must be an absolute path: {configured}"
            )
        if not configured_path.is_dir():
            raise SiblingRepoResolutionError(
                f"{SLACK_REPO_ENV} points to a missing directory: "
                f"{configured_path.resolve()}"
            )
        return _validate_sibling_slack_repo(
            configured_path,
            source="explicit_override",
        )

    root = project_root.expanduser().resolve()
    candidates: dict[Path, set[str]] = {}
    ordinary = (root.parent / EXPECTED_SLACK_REPO_NAME).resolve()
    candidates.setdefault(ordinary, set()).add("ordinary_sibling")

    common_value = _git_value(root, "rev-parse", "--git-common-dir")
    common_dir = Path(common_value)
    if not common_dir.is_absolute():
        common_dir = root / common_dir
    common_dir = common_dir.resolve()
    if common_dir.name != ".git" or not common_dir.is_dir():
        raise SiblingRepoResolutionError(
            f"Unexpected Git common directory for {root}: {common_dir}"
        )
    git_sibling = (common_dir.parent.parent / EXPECTED_SLACK_REPO_NAME).resolve()
    candidates.setdefault(git_sibling, set()).add("git_common_dir_sibling")

    existing = [(path, sources) for path, sources in candidates.items() if path.exists()]
    if not existing:
        checked = ", ".join(str(path) for path in candidates)
        raise SiblingRepoResolutionError(
            f"No keystone-slack checkout was found; checked: {checked}. "
            f"Set {SLACK_REPO_ENV} to one verified absolute checkout path."
        )

    verified: list[SiblingSlackRepo] = []
    errors: list[str] = []
    for path, sources in existing:
        source = (
            "ordinary_sibling"
            if "ordinary_sibling" in sources
            else "git_common_dir_sibling"
        )
        try:
            verified.append(_validate_sibling_slack_repo(path, source=source))
        except SiblingRepoResolutionError as exc:
            errors.append(str(exc))
    if errors:
        raise SiblingRepoResolutionError("; ".join(errors))
    if len(verified) != 1:
        choices = ", ".join(str(item.root) for item in verified)
        raise SiblingRepoResolutionError(
            f"Multiple valid keystone-slack checkouts were found: {choices}. "
            f"Set {SLACK_REPO_ENV} to the intended absolute checkout path."
        )
    return verified[0]


def _no_live_environment(source: Mapping[str, str]) -> dict[str, str]:
    env = dict(source)
    for key in _LIVE_CREDENTIAL_KEYS:
        env.pop(key, None)
    env.update(
        {
            "PYTHON_DOTENV_DISABLED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "KEYSTONE_TEST_MODE": "1",
            "KEYSTONE_LIVE_MODE": "false",
            "KEYSTONE_DRY_RUN": "true",
            "KEYSTONE_ENABLE_LIVE_GMAIL": "false",
            "KEYSTONE_ENABLE_LIVE_SLACK": "false",
            "KEYSTONE_ENABLE_LIVE_RESEARCH": "false",
            "KEYSTONE_ENABLE_LIVE_CRM": "false",
            "KEYSTONE_ENABLE_WEBSITE_EXTRACTION": "false",
            "KNI_BUSINESS_AGENTS_LIVE_SDK": "false",
            "KNI_BUSINESS_AGENTS_LIVE_SEARCH": "false",
            "SEARCH_PROVIDER": "dry-run",
            "AUTO_SEND_EMAIL": "false",
        }
    )
    return env


def _run(label: str, command: list[str], cwd: Path = PROJECT_ROOT) -> None:
    print(f"\n== {label} ==")
    print(f"cwd: {cwd}")
    print(" ".join(command))
    env = _no_live_environment(os.environ)
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(cwd)
        if not existing_pythonpath
        else f"{cwd}{os.pathsep}{existing_pythonpath}"
    )
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _run_sibling_bridge_tests(resolution: SiblingSlackRepo) -> dict[str, int]:
    verified = _validate_sibling_slack_repo(resolution.root, source=resolution.source)
    if verified.test_file_sha256 != resolution.test_file_sha256:
        raise SiblingRepoResolutionError(
            "Sibling fixture changed after resolution; refusing to import it."
        )
    print("\n== Sibling keystone-slack ANU-60 bridge fixtures ==")
    print(f"resolved root: {verified.root}")
    print(f"resolution source: {verified.source}")
    print(f"verified origin repository: {verified.origin_repo_name}")
    print(f"fixture file: {verified.test_file}")
    print(f"fixture sha256: {verified.test_file_sha256}")
    print(f"selected fixture count: {len(SIBLING_BRIDGE_TESTS)}")

    previous_cwd = Path.cwd()
    previous_path = list(sys.path)
    previous_env = dict(os.environ)
    previous_dont_write_bytecode = sys.dont_write_bytecode
    module_name = "anu60_sibling_test_app_mentions"
    sys.path.insert(0, str(verified.root))
    try:
        os.environ.clear()
        os.environ.update(_no_live_environment(previous_env))
        os.chdir(verified.root)
        sys.dont_write_bytecode = True
        spec = importlib.util.spec_from_file_location(module_name, verified.test_file)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load sibling test file: {verified.test_file}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        suite = unittest.TestSuite()
        for test_name in SIBLING_BRIDGE_TESTS:
            suite.addTest(module.SlackAppMentionTests(test_name))
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        if not result.wasSuccessful():
            raise RuntimeError("Sibling keystone-slack ANU-60 bridge fixtures failed")
        return {
            "tests_run": result.testsRun,
            "failures": len(result.failures),
            "errors": len(result.errors),
        }
    finally:
        os.chdir(previous_cwd)
        sys.path[:] = previous_path
        sys.dont_write_bytecode = previous_dont_write_bytecode
        os.environ.clear()
        os.environ.update(previous_env)
        sys.modules.pop(module_name, None)


def anu60_commands() -> tuple[tuple[str, list[str], Path], ...]:
    """Return the ordered clean-checkout ANU-60 validation ladder."""

    return (
        (
            "KBA Slack expansion gate",
            [
                sys.executable,
                "scripts/run_slack_agent_expansion_gate.py",
                "--quiet",
                "--json-output",
                EXPANSION_ARTIFACT,
            ],
            PROJECT_ROOT,
        ),
        (
            "ANU-60 proof packet validator",
            [sys.executable, "scripts/validate_anu60_live_slack_proof_packet.py"],
            PROJECT_ROOT,
        ),
        (
            "KBA prompt/doc and Slack action contract tests",
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_prompt_contracts.py",
                "tests/test_slack_action_contract.py",
                "-q",
            ],
            PROJECT_ROOT,
        ),
        (
            "KBA Slack bridge contract validator",
            [sys.executable, "scripts/validate_slack_bridge_contract.py"],
            PROJECT_ROOT,
        ),
        (
            "KBA Slack rendering examples validator",
            [sys.executable, "scripts/validate_slack_result_rendering_examples.py"],
            PROJECT_ROOT,
        ),
    )


def main() -> int:
    try:
        sibling = resolve_sibling_slack_repo()
    except SiblingRepoResolutionError as exc:
        print(f"Sibling repository resolution failed: {exc}", file=sys.stderr)
        return 1

    print(
        "Resolved sibling keystone-slack: "
        f"root={sibling.root}; source={sibling.source}; "
        f"origin={sibling.origin_repo_name}; fixture_sha256={sibling.test_file_sha256}"
    )
    for label, command, cwd in anu60_commands():
        _run(label, command, cwd)
    sibling_result = _run_sibling_bridge_tests(sibling)

    print(
        "\nANU-60 no-live preflight passed; "
        f"sibling fixtures={sibling_result['tests_run']}; "
        "no live Slack/API/model/search calls made."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
