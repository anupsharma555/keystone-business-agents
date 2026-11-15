"""Bounded child-process helpers for local agent runners."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path


def run_isolated_child_process(
    command: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a child command in its own process group and clean it up on timeout."""

    popen_kwargs = {
        "cwd": cwd,
        "text": True,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": dict(env) if env is not None else None,
    }
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(list(command), **popen_kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_child_group(process)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired as cleanup_error:
            # A descendant may retain a pipe even after the group leader exits.
            # Cleanup must obey a deadline too.
            stdout, stderr = cleanup_error.output, cleanup_error.stderr
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        exc.output = stdout
        exc.stderr = stderr
        raise
    return subprocess.CompletedProcess(
        args=list(command),
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _kill_child_group(process: subprocess.Popen[str]) -> None:
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
    if process.poll() is None:
        process.kill()
