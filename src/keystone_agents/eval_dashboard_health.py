"""Local eval dashboard manager path and readiness helpers."""

from __future__ import annotations

import importlib
import os
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

DEFAULT_DASHBOARD_URL = "http://127.0.0.1:8769/dashboard"
DEFAULT_HEALTH_PATH = "/api/status"
DEFAULT_LAUNCHD_LABEL = "com.keystone.kba-eval-dashboard"


@dataclass(frozen=True)
class DashboardManagerResolution:
    manager_path: Path
    exists: bool
    executable: bool
    candidates: tuple[Path, ...]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_dashboard_manager_path(
    raw_path: str | Path | None = None,
    *,
    cwd: str | Path | None = None,
    root: str | Path | None = None,
) -> DashboardManagerResolution:
    """Resolve the eval dashboard manager from cwd, repo root, or absolute path."""

    root_path = Path(root).resolve() if root is not None else repo_root()
    cwd_path = Path(cwd).resolve() if cwd is not None else Path.cwd().resolve()
    requested = Path(
        raw_path
        or os.environ.get("KEYSTONE_EVAL_DASHBOARD_MANAGER")
        or "scripts/manage_eval_dashboard.sh"
    )
    candidates: list[Path] = []

    def add(path: Path) -> None:
        resolved = path.resolve()
        if resolved not in candidates:
            candidates.append(resolved)

    if requested.is_absolute():
        add(requested)
    else:
        add(cwd_path / requested)
        add(root_path / requested)
        parts = requested.parts
        if parts and parts[0] == root_path.name:
            add(root_path.parent / requested)
            add(root_path / Path(*parts[1:]))
    add(root_path / "scripts" / "manage_eval_dashboard.sh")

    chosen = next((candidate for candidate in candidates if candidate.is_file()), candidates[-1])
    return DashboardManagerResolution(
        manager_path=chosen,
        exists=chosen.is_file(),
        executable=os.access(chosen, os.X_OK) if chosen.is_file() else False,
        candidates=tuple(candidates),
    )


def eval_dashboard_readiness(
    *,
    manager_path: str | Path | None = None,
    dashboard_url: str = DEFAULT_DASHBOARD_URL,
    health_url: str | None = None,
    cwd: str | Path | None = None,
    root: str | Path | None = None,
    probe_dashboard_url: bool = True,
) -> dict[str, object]:
    """Return a local-only readiness snapshot for the eval dashboard server."""

    root_path = Path(root).resolve() if root is not None else repo_root()
    resolution = resolve_dashboard_manager_path(manager_path, cwd=cwd, root=root_path)
    resolved_health_url = str(health_url or _default_health_url(dashboard_url))
    parsed = urlparse(resolved_health_url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    host = parsed.hostname or "127.0.0.1"
    venv_python = root_path / ".venv" / "bin" / "python"
    server_script = root_path / "scripts" / "serve_promptfoo_eval_dashboard.py"
    log_dir = root_path / ".keystone" / "promptfoo" / "logs"
    pid_file = log_dir / "eval-dashboard.pid"
    launchd_plist = Path.home() / "Library" / "LaunchAgents" / f"{DEFAULT_LAUNCHD_LABEL}.plist"
    import_ok = _module_importable("promptfoo.eval_dashboard_server")
    port_open = _port_open(host, port)
    launchd_running = _launchd_service_running(DEFAULT_LAUNCHD_LABEL)
    health_reachable = (
        _dashboard_reachable(resolved_health_url)
        if probe_dashboard_url
        else bool(port_open or launchd_running)
    )
    issues: list[str] = []

    if not resolution.exists:
        issues.append(f"manager script missing at {resolution.manager_path}")
    elif not resolution.executable:
        issues.append(f"manager script is not executable at {resolution.manager_path}")
    if not venv_python.is_file():
        issues.append(f"Python virtualenv missing at {venv_python}")
    if not server_script.is_file():
        issues.append(f"dashboard server script missing at {server_script}")
    if not import_ok:
        issues.append("dashboard server import failed: promptfoo.eval_dashboard_server")

    return {
        "schema": "keystone.eval_dashboard_readiness.v1",
        "manager_path": str(resolution.manager_path),
        "manager_exists": resolution.exists,
        "manager_executable": resolution.executable,
        "manager_candidates": [str(path) for path in resolution.candidates],
        "venv_python": str(venv_python),
        "venv_python_exists": venv_python.is_file(),
        "server_script": str(server_script),
        "server_script_exists": server_script.is_file(),
        "dashboard_url": dashboard_url,
        "health_url": resolved_health_url,
        "host": host,
        "port": port,
        "port_open": port_open,
        "dashboard_reachable": health_reachable,
        "health_reachable": health_reachable,
        "dashboard_probe_enabled": probe_dashboard_url,
        "pid_file": str(pid_file),
        "pid_file_exists": pid_file.is_file(),
        "launchd_plist": str(launchd_plist),
        "launchd_plist_exists": launchd_plist.is_file(),
        "launchd_running": launchd_running,
        "log_dir": str(log_dir),
        "log_dir_exists": log_dir.is_dir(),
        "server_import_ok": import_ok,
        "issues": issues,
    }


def _default_health_url(dashboard_url: str) -> str:
    value = str(dashboard_url or DEFAULT_DASHBOARD_URL).strip() or DEFAULT_DASHBOARD_URL
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        return parsed._replace(path=DEFAULT_HEALTH_PATH, params="", query="", fragment="").geturl()
    if value.endswith("/dashboard"):
        return f"{value[:-len('/dashboard')]}{DEFAULT_HEALTH_PATH}"
    return DEFAULT_DASHBOARD_URL.replace("/dashboard", DEFAULT_HEALTH_PATH, 1)


def _module_importable(module_name: str) -> bool:
    try:
        importlib.import_module(module_name)
    except Exception:
        return False
    return True


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=1.0):
            return True
    except OSError:
        return False


def _dashboard_reachable(dashboard_url: str) -> bool:
    request = Request(dashboard_url, method="GET")
    try:
        with urlopen(request, timeout=3.0) as response:
            return int(getattr(response, "status", 0) or 0) < 500
    except HTTPError as exc:
        return int(exc.code) < 500
    except (OSError, URLError, ValueError):
        return _dashboard_reachable_with_curl(dashboard_url)


def _dashboard_reachable_with_curl(dashboard_url: str) -> bool:
    try:
        completed = subprocess.run(
            ["curl", "-fsS", "--max-time", "3", "-o", os.devnull, dashboard_url],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _launchd_service_running(label: str) -> bool:
    if os.uname().sysname != "Darwin":
        return False
    try:
        completed = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and "state = running" in completed.stdout
