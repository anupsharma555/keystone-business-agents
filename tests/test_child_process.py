from __future__ import annotations

import subprocess

import pytest

import keystone_agents.child_process as child_process


def test_run_isolated_child_process_starts_new_session(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 123
        returncode = 0

        def communicate(self, *, timeout: float | None = None):
            captured["timeout"] = timeout
            return "ok", ""

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(child_process.subprocess, "Popen", fake_popen)

    result = child_process.run_isolated_child_process(["echo", "ok"], timeout=2)

    assert result.returncode == 0
    assert result.stdout == "ok"
    assert captured["timeout"] == 2
    assert captured["kwargs"]["start_new_session"] is True


def test_run_isolated_child_process_kills_process_group_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[tuple[int, int]] = []

    class FakeProcess:
        pid = 456
        returncode: int | None = None
        calls = 0

        def communicate(self, *, timeout: float | None = None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(["sleep"], timeout)
            self.returncode = -9
            return "partial", "timed out"

        def poll(self):
            return self.returncode

    monkeypatch.setattr(child_process.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    monkeypatch.setattr(child_process.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    with pytest.raises(subprocess.TimeoutExpired) as exc_info:
        child_process.run_isolated_child_process(["sleep", "10"], timeout=1)

    assert killed == [(456, child_process.signal.SIGKILL)]
    assert exc_info.value.output == "partial"
    assert exc_info.value.stderr == "timed out"


def test_timeout_cleanup_is_bounded_when_descendant_keeps_pipe_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from io import StringIO

    class FakeProcess:
        pid = 789
        returncode = 0  # Leader exited; a descendant still owns stdout.
        stdout = StringIO()
        stderr = StringIO()
        timeouts: list[float | None] = []

        def communicate(self, *, timeout=None):
            self.timeouts.append(timeout)
            raise subprocess.TimeoutExpired(["child"], timeout, output=b"partial")

        def poll(self):
            return self.returncode

    process = FakeProcess()
    killed: list[int] = []
    monkeypatch.setattr(child_process.subprocess, "Popen", lambda *_a, **_kw: process)
    monkeypatch.setattr(child_process.os, "killpg", lambda pid, _sig: killed.append(pid))

    with pytest.raises(subprocess.TimeoutExpired):
        child_process.run_isolated_child_process(["child"], timeout=1)

    assert process.timeouts == [1, 5]
    assert killed == [789]
    assert process.stdout.closed and process.stderr.closed
