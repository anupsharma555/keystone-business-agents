from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_isolated(code: str) -> subprocess.CompletedProcess[str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"LANG", "LC_ALL", "PATH", "SSL_CERT_DIR", "SSL_CERT_FILE", "TMPDIR"}
    }
    env.update(
        {
            "PYTHONPATH": str(PROJECT_ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHON_DOTENV_DISABLED": "1",
            "KEYSTONE_TEST_MODE": "1",
            "KEYSTONE_LIVE_MODE": "false",
            "KEYSTONE_DRY_RUN": "true",
        }
    )
    return subprocess.run(
        (sys.executable, "-c", code),
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_cli_import_and_root_help_do_not_load_full_implementation() -> None:
    completed = _run_isolated(
        """
import sys
import keystone_agents.cli as cli

assert "keystone_agents.entrypoints.cli_impl" not in sys.modules
try:
    cli.main(["--help"])
except SystemExit as exc:
    assert exc.code == 0
else:
    raise AssertionError("root help should exit through argparse")
assert "keystone_agents.entrypoints.cli_impl" not in sys.modules
"""
    )

    assert "Keystone business-agent operations." in completed.stdout


def test_cli_replays_public_and_private_overrides_set_before_lazy_load() -> None:
    _run_isolated(
        """
import keystone_agents.cli as cli

public_override = object()
private_override = object()
cli.run_isolated_child_process = public_override
cli._run_ask_work_item = private_override
assert cli._IMPLEMENTATION is None

implementation = cli._implementation()
assert implementation.run_isolated_child_process is public_override
assert implementation._run_ask_work_item is private_override
"""
    )
