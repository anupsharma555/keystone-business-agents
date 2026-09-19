from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import run_anu60_no_live_preflight as preflight


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def _initialize_repo(root: Path, *, origin_name: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Synthetic Test")
    _git(root, "config", "user.email", "synthetic@example.invalid")
    _git(root, "remote", "add", "origin", f"https://example.invalid/{origin_name}.git")


def _commit_repo(root: Path) -> None:
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "synthetic fixture")


def _synthetic_slack_test_source() -> str:
    methods = "\n".join(
        (
            f"    def {name}(self):\n"
            "        self._record()"
        )
        for name in preflight.SIBLING_BRIDGE_TESTS
    )
    return (
        "import os\n"
        "from pathlib import Path\n"
        "import unittest\n\n"
        "class SlackAppMentionTests(unittest.TestCase):\n"
        "    def _record(self):\n"
        "        marker = Path(os.environ['KBA_DUO_TEST_MARKER'])\n"
        "        with marker.open('a', encoding='utf-8') as handle:\n"
        "            handle.write(f'{Path.cwd()}|{Path(__file__).resolve()}\\n')\n\n"
        f"{methods}\n"
    )


def _make_slack_repo(root: Path, *, origin_name: str = "keystone-slack") -> Path:
    _initialize_repo(root, origin_name=origin_name)
    (root / "AGENTS.md").write_text("Synthetic test guidance.\n", encoding="utf-8")
    (root / "README.md").write_text("Synthetic Slack integration repo.\n", encoding="utf-8")
    bridge = root / "kni_integrations/business_agents_bridge.py"
    bridge.parent.mkdir(parents=True)
    bridge.write_text("# synthetic fixture\n", encoding="utf-8")
    test_file = root / "tests/test_app_mentions.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(_synthetic_slack_test_source(), encoding="utf-8")
    _commit_repo(root)
    return root


def _make_business_repo(root: Path) -> Path:
    _initialize_repo(root, origin_name="keystone-business-agents")
    (root / "README.md").write_text("Synthetic KBA repo.\n", encoding="utf-8")
    _commit_repo(root)
    return root


def test_resolver_keeps_ordinary_sibling_layout(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    business = _make_business_repo(projects / "keystone-business-agents")
    slack = _make_slack_repo(projects / "keystone-slack")

    result = preflight.resolve_sibling_slack_repo(business, environ={})

    assert result.root == slack.resolve()
    assert result.source == "ordinary_sibling"
    assert result.test_file == (slack / "tests/test_app_mentions.py").resolve()
    assert result.origin_repo_name == "keystone-slack"


def test_resolver_uses_git_common_dir_from_isolated_worktree(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    business = _make_business_repo(projects / "keystone-business-agents")
    slack = _make_slack_repo(projects / "keystone-slack")
    worktree = tmp_path / "worktrees/kba-v2"
    worktree.parent.mkdir(parents=True)
    _git(business, "worktree", "add", "-q", "-b", "synthetic-worktree", str(worktree))

    result = preflight.resolve_sibling_slack_repo(worktree, environ={})

    assert result.root == slack.resolve()
    assert result.source == "git_common_dir_sibling"


def test_resolver_honors_valid_explicit_override(tmp_path: Path) -> None:
    business = _make_business_repo(tmp_path / "business")
    slack = _make_slack_repo(tmp_path / "integrations/keystone-slack")

    result = preflight.resolve_sibling_slack_repo(
        business,
        environ={preflight.SLACK_REPO_ENV: str(slack)},
    )

    assert result.root == slack.resolve()
    assert result.source == "explicit_override"


def test_invalid_explicit_override_does_not_fall_back(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    business = _make_business_repo(projects / "keystone-business-agents")
    _make_slack_repo(projects / "keystone-slack")
    missing = tmp_path / "missing-slack"

    with pytest.raises(preflight.SiblingRepoResolutionError) as exc_info:
        preflight.resolve_sibling_slack_repo(
            business,
            environ={preflight.SLACK_REPO_ENV: str(missing)},
        )

    assert str(exc_info.value) == (
        f"{preflight.SLACK_REPO_ENV} points to a missing directory: {missing.resolve()}"
    )


def test_explicit_override_requires_an_absolute_path(tmp_path: Path) -> None:
    business = _make_business_repo(tmp_path / "business")

    with pytest.raises(preflight.SiblingRepoResolutionError) as exc_info:
        preflight.resolve_sibling_slack_repo(
            business,
            environ={preflight.SLACK_REPO_ENV: "relative/keystone-slack"},
        )

    assert "must be an absolute path" in str(exc_info.value)


def test_missing_sibling_fails_with_checked_locations(tmp_path: Path) -> None:
    business = _make_business_repo(tmp_path / "projects/keystone-business-agents")

    with pytest.raises(preflight.SiblingRepoResolutionError) as exc_info:
        preflight.resolve_sibling_slack_repo(business, environ={})

    message = str(exc_info.value)
    assert "No keystone-slack checkout was found" in message
    assert str((business.parent / "keystone-slack").resolve()) in message


def test_unrelated_sibling_directory_is_rejected(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    business = _make_business_repo(projects / "keystone-business-agents")
    _make_slack_repo(projects / "keystone-slack", origin_name="unrelated-repo")

    with pytest.raises(preflight.SiblingRepoResolutionError) as exc_info:
        preflight.resolve_sibling_slack_repo(business, environ={})

    assert "origin repository name is 'unrelated-repo', expected 'keystone-slack'" in str(
        exc_info.value
    )


def test_two_distinct_valid_candidates_require_explicit_override(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    business = _make_business_repo(projects / "keystone-business-agents")
    _make_slack_repo(projects / "keystone-slack")
    worktree = tmp_path / "worktrees/kba-v2"
    worktree.parent.mkdir(parents=True)
    _git(business, "worktree", "add", "-q", "-b", "synthetic-worktree", str(worktree))
    _make_slack_repo(worktree.parent / "keystone-slack")

    with pytest.raises(preflight.SiblingRepoResolutionError) as exc_info:
        preflight.resolve_sibling_slack_repo(worktree, environ={})

    assert "Multiple valid keystone-slack checkouts" in str(exc_info.value)
    assert preflight.SLACK_REPO_ENV in str(exc_info.value)


def test_sibling_fixture_runner_uses_verified_cwd_and_test_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business = _make_business_repo(tmp_path / "business")
    slack = _make_slack_repo(tmp_path / "integrations/keystone-slack")
    resolution = preflight.resolve_sibling_slack_repo(
        business,
        environ={preflight.SLACK_REPO_ENV: str(slack)},
    )
    marker = tmp_path / "fixture-runs.txt"
    monkeypatch.setenv("KBA_DUO_TEST_MARKER", str(marker))

    result = preflight._run_sibling_bridge_tests(resolution)

    lines = marker.read_text(encoding="utf-8").splitlines()
    expected = f"{slack.resolve()}|{resolution.test_file}"
    assert result == {"tests_run": 8, "failures": 0, "errors": 0}
    assert lines == [expected] * 8
    assert not list(slack.rglob("__pycache__"))


def test_no_live_environment_removes_credentials_and_forces_safe_flags() -> None:
    env = preflight._no_live_environment(
        {
            "KEYSTONE_OPENAI_API_KEY": "synthetic-key",
            "SLACK_BOT_TOKEN": "synthetic-token",
            "KNI_BUSINESS_AGENTS_LIVE_SDK": "true",
            "KNI_BUSINESS_AGENTS_LIVE_SEARCH": "true",
            "PYTHONPATH": "/existing/path",
        }
    )

    assert "KEYSTONE_OPENAI_API_KEY" not in env
    assert "SLACK_BOT_TOKEN" not in env
    assert env["KNI_BUSINESS_AGENTS_LIVE_SDK"] == "false"
    assert env["KNI_BUSINESS_AGENTS_LIVE_SEARCH"] == "false"
    assert env["KEYSTONE_LIVE_MODE"] == "false"
    assert env["KEYSTONE_DRY_RUN"] == "true"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_preflight_generates_expansion_artifact_before_validating_proof_packet() -> None:
    commands = preflight.anu60_commands()
    rendered = [" ".join(command) for _label, command, _cwd in commands]
    expansion_index = next(
        index
        for index, command in enumerate(rendered)
        if "run_slack_agent_expansion_gate.py" in command
    )
    validator_index = next(
        index
        for index, command in enumerate(rendered)
        if "validate_anu60_live_slack_proof_packet.py" in command
    )

    assert expansion_index < validator_index
    assert preflight.EXPANSION_ARTIFACT in rendered[expansion_index]
