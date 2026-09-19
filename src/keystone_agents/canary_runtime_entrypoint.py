"""Executable entrypoint for the mutation-disabled KBA canary policy."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from keystone_agents.canary_runtime import CanaryRuntimeConfig

REPO_ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if os.environ.get("KEYSTONE_CANARY_ACCEPTANCE_PROFILE"):
        from keystone_agents.canary_acceptance_entrypoint import main as acceptance_main

        return acceptance_main(arguments)
    try:
        config = CanaryRuntimeConfig.from_environment(repo_root=REPO_ROOT)
    except ValueError as exc:
        print(f"KBA no-write canary refused to start: {exc}", file=sys.stderr)
        return 2

    config.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if arguments == ["--canary-preview"]:
        print(json.dumps(config.public_manifest(), indent=2, sort_keys=True))
        return 0
    if arguments == ["--canary-import-check"]:
        import keystone_agents
        import keystone_agents.provider_read as provider_read
        from keystone_agents.execution_telemetry import ExecutionTelemetry

        print(
            json.dumps(
                {
                    "keystone_agents": str(Path(keystone_agents.__file__).resolve()),
                    "execution_telemetry": str(
                        Path(sys.modules[ExecutionTelemetry.__module__].__file__).resolve()
                    ),
                    "provider_read": str(Path(provider_read.__file__).resolve()),
                    "python": str((REPO_ROOT / ".venv" / "bin" / "python").resolve()),
                },
                sort_keys=True,
            )
        )
        return 0

    try:
        config.validate_python_arguments(arguments)
    except ValueError as exc:
        print(f"KBA no-write canary refused command: {exc}", file=sys.stderr)
        return 2
    config.stage_google_workspace_token()

    interpreter = REPO_ROOT / ".venv" / "bin" / "python"
    if not interpreter.is_file():
        print(
            f"KBA no-write canary interpreter is missing: {interpreter}",
            file=sys.stderr,
        )
        return 2
    child_env = config.build_child_environment()
    child_argv = [
        str(interpreter),
        *config.rewrite_python_arguments(arguments),
    ]
    os.execve(str(interpreter), child_argv, child_env)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
