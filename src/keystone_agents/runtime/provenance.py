"""Privacy-safe fingerprint of the KBA code and runtime configuration in use."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

RUNTIME_FINGERPRINT_SCHEMA = "keystone.runtime_fingerprint.v1"

_RUNTIME_PACKAGES = (
    "openai-agents",
    "openai",
    "pydantic",
    "pydantic-core",
    "langgraph",
    "langgraph-sdk",
    "langgraph-prebuilt",
    "langchain-core",
    "langchain-protocol",
    "langsmith",
    "langgraph-checkpoint",
    "langgraph-checkpoint-sqlite",
    "langgraph-checkpoint-postgres",
    "aiosqlite",
    "sqlite-vec",
    "mcp",
    "griffelib",
    "httpx",
    "httpx2",
    "httpcore",
    "httpcore2",
    "jiter",
    "aiohttp",
    "google-api-python-client",
    "google-auth",
    "idna",
    "orjson",
    "pypdf",
    "PyJWT",
    "python-dotenv",
    "python-multipart",
    "requests",
    "rich",
    "fastapi",
    "starlette",
    "trafilatura",
    "urllib3",
)

_SAFE_CONFIG_NAMES = {
    "MODEL_PROVIDER",
    "OPENAI_MODEL",
    "SEARCH_PROVIDER",
}
_SAFE_KEYSTONE_CONFIG = re.compile(
    r"(?:MODEL|PROVIDER|ALLOW|ENABLE|LIVE|DRY_RUN|MAX|MIN|TIMEOUT|TURN|BUDGET|"
    r"EXTRACTOR|TRACE|CACHE|RETRY|REPAIR|FALLBACK)"
)
_SENSITIVE_CONFIG = re.compile(
    r"(?:API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTHORIZATION|CLIENT_ID|"
    r"CLIENT_SECRET|EMAIL|USER_ID|ACCOUNT_ID|BASE_URL|DATABASE_URL|FILE|PATH)"
)


def build_runtime_fingerprint(
    *,
    repo_root: Path | None = None,
    env: Mapping[str, str] | None = None,
    process_started_at_utc: datetime | None = None,
    source_paths: Sequence[Path] | None = None,
) -> dict[str, Any]:
    """Build a comparable fingerprint without exposing source, config, or secrets."""

    root = (repo_root or Path(__file__).resolve().parents[3]).resolve()
    started = (process_started_at_utc or datetime.now(UTC)).astimezone(UTC)
    paths = list(source_paths) if source_paths is not None else _runtime_source_paths(root)
    source_sha256, source_file_count, source_read_error_count = _source_digest(
        root=root,
        paths=paths,
    )
    dependency_sha256, dependency_file_count = _dependency_digest(root)
    safe_config = _safe_runtime_config(os.environ if env is None else env)
    config_sha256 = _sha256_json(safe_config)
    python_runtime = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    package_versions = _runtime_package_versions()
    comparable = {
        "schema": RUNTIME_FINGERPRINT_SCHEMA,
        "source_sha256": source_sha256,
        "source_file_count": source_file_count,
        "source_read_error_count": source_read_error_count,
        "dependency_sha256": dependency_sha256,
        "dependency_file_count": dependency_file_count,
        "config_sha256": config_sha256,
        "config_key_count": len(safe_config),
        "python_runtime": python_runtime,
        "package_versions": package_versions,
    }
    return {
        **comparable,
        "runtime_sha256": _sha256_json(comparable),
        "process_started_at_utc": started.isoformat(),
    }


def current_runtime_fingerprint() -> dict[str, Any]:
    """Return the code/config snapshot captured when this module was imported."""

    return {
        **_PROCESS_RUNTIME_FINGERPRINT,
        "package_versions": dict(_PROCESS_RUNTIME_FINGERPRINT["package_versions"]),
    }


def _runtime_source_paths(root: Path) -> list[Path]:
    package_root = root / "src" / "keystone_agents"
    paths = [
        *package_root.rglob("*.py"),
        *(package_root / "prompts").rglob("*.md"),
        *(package_root / "skills").rglob("SKILL.md"),
    ]
    scripts_root = root / "scripts"
    paths.extend(path for path in scripts_root.glob("run_*.py") if path.is_file())
    ask_agent = scripts_root / "ask_agent.py"
    if ask_agent.is_file():
        paths.append(ask_agent)
    return sorted({path.resolve() for path in paths if path.is_file()}, key=str)


def _source_digest(*, root: Path, paths: Sequence[Path]) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    count = 0
    errors = 0
    for path in sorted({path.resolve() for path in paths}, key=str):
        try:
            relative = path.relative_to(root).as_posix()
            content = path.read_bytes()
        except (OSError, ValueError):
            errors += 1
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(content)
        digest.update(b"\x00")
        count += 1
    return digest.hexdigest(), count, errors


def _dependency_digest(root: Path) -> tuple[str, int]:
    paths = [root / "pyproject.toml", root / "uv.lock"]
    paths.extend(sorted((root / "constraints").glob("*.txt")))
    digest, count, _errors = _source_digest(
        root=root,
        paths=[path for path in paths if path.is_file()],
    )
    return digest, count


def _runtime_package_versions() -> dict[str, str]:
    """Inspect only known runtime distributions, never unrelated installed packages."""

    versions: dict[str, str] = {}
    for name in _RUNTIME_PACKAGES:
        try:
            versions[name] = version(name)
        except PackageNotFoundError:
            versions[name] = "not_installed"
    return versions


def _safe_runtime_config(env: Mapping[str, str]) -> dict[str, str]:
    selected: dict[str, str] = {}
    for raw_name, raw_value in env.items():
        name = str(raw_name).strip().upper()
        if not name or _SENSITIVE_CONFIG.search(name):
            continue
        if name in _SAFE_CONFIG_NAMES or (
            name.startswith("KEYSTONE_") and _SAFE_KEYSTONE_CONFIG.search(name)
        ):
            selected[name] = str(raw_value)
    return dict(sorted(selected.items()))


def _sha256_json(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_PROCESS_RUNTIME_FINGERPRINT = build_runtime_fingerprint()
