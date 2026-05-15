from __future__ import annotations

import os
import socket
from collections.abc import Callable
from typing import Any

import pytest

NETWORK_BLOCKED_MESSAGE = "Network calls are disabled during pytest; mock live integrations."
LOCAL_HOSTS = {"", "0.0.0.0", "127.0.0.1", "::1", "localhost", None}

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("KEYSTONE_TEST_MODE", "1")

LIVE_ENV_KEYS = (
    "KEYSTONE_LIVE_MODE",
    "KEYSTONE_ENABLE_LIVE_GMAIL",
    "KEYSTONE_ENABLE_LIVE_SLACK",
    "KEYSTONE_ENABLE_LIVE_RESEARCH",
    "KEYSTONE_ENABLE_LIVE_CRM",
    "KEYSTONE_DRY_RUN",
    "KEYSTONE_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "SERPER_API_KEY",
    "SLACK_BOT_TOKEN",
    "SLACK_WEBHOOK_URL",
    "APIFY_API_TOKEN",
    "BROWSERLESS_API_KEY",
    "FIRECRAWL_API_KEY",
    "FIRECRAWL_BASE_URL",
    "KEYSTONE_TAVILY_MONTHLY_CREDIT_LIMIT",
    "KEYSTONE_TAVILY_MONTHLY_SOFT_LIMIT",
    "KEYSTONE_TAVILY_CREDIT_ENFORCEMENT",
    "KEYSTONE_TAVILY_USAGE_PATH",
    "KEYSTONE_ENABLE_WEBSITE_EXTRACTION",
    "KEYSTONE_WEBSITE_EXTRACTOR",
    "KEYSTONE_WEBSITE_EXTRACTOR_FALLBACK",
    "KEYSTONE_WEBSITE_EXTRACTION_MAX_PAGES",
)


def _host_value(value: object) -> object:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return value


def _is_local_host(value: object) -> bool:
    return _host_value(value) in LOCAL_HOSTS


@pytest.fixture(autouse=True)
def disable_dotenv_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep local `.env` credentials out of tests unless a test opts in explicitly."""

    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("KEYSTONE_TEST_MODE", "1")


@pytest.fixture(autouse=True)
def isolate_live_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep operator live-mode environment out of unit tests and subprocess CLIs."""

    for key in LIVE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "true")


@pytest.fixture(autouse=True)
def block_live_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail fast if a test accidentally reaches an external network path."""

    original_getaddrinfo: Callable[..., Any] = socket.getaddrinfo
    original_connect = socket.socket.connect

    def guarded_getaddrinfo(
        host: str | bytes | None,
        port: str | int | None,
        family: int = 0,
        type: int = 0,
        proto: int = 0,
        flags: int = 0,
    ) -> list[tuple[Any, ...]]:
        if _is_local_host(host):
            return original_getaddrinfo(host, port, family, type, proto, flags)
        raise AssertionError(NETWORK_BLOCKED_MESSAGE)

    def guarded_connect(self: socket.socket, address: object) -> object:
        host = address[0] if isinstance(address, tuple) and address else address
        if _is_local_host(host):
            return original_connect(self, address)
        raise AssertionError(NETWORK_BLOCKED_MESSAGE)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
