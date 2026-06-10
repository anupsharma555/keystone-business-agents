from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "switch_operator_mode.py"


def load_module():
    spec = importlib.util.spec_from_file_location("switch_operator_mode", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load switch_operator_mode.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dry_run_profile_updates_known_keys_and_preserves_unrelated_content(tmp_path) -> None:
    module = load_module()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# Existing config",
                "KEEP_ME=secret-value",
                "KEYSTONE_LIVE_MODE = true # old",
                "KEYSTONE_DRY_RUN=false",
                "KEYSTONE_ENABLE_LIVE_GMAIL=true",
                "SEARCH_PROVIDER = serper # current",
                "AUTO_SEND_EMAIL=1",
                "CUSTOM_FLAG=keep-this",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = module.main(["dry-run", "--env-file", str(env_file)])

    assert result == 0
    text = env_file.read_text(encoding="utf-8")
    assert "# Existing config" in text
    assert "KEEP_ME=secret-value" in text
    assert "CUSTOM_FLAG=keep-this" in text
    assert "KEYSTONE_LIVE_MODE = false # old" in text
    assert "KEYSTONE_DRY_RUN=true" in text
    assert "KEYSTONE_ENABLE_LIVE_GMAIL=false" in text
    assert "SEARCH_PROVIDER = dry-run # current" in text
    assert "AUTO_SEND_EMAIL=false" in text
    assert "KEYSTONE_ENABLE_LIVE_SLACK=false" in text
    assert "KEYSTONE_ENABLE_LIVE_RESEARCH=false" in text
    assert "KEYSTONE_ENABLE_LIVE_CRM=false" in text
    assert "KEYSTONE_ENABLE_WEBSITE_EXTRACTION=false" in text
    assert "KEYSTONE_WEBSITE_EXTRACTOR=trafilatura" in text
    assert "SEARXNG_BASE_URL=http://127.0.0.1:18080" in text
    assert "KEYSTONE_SEARXNG_TRANSIENT=true" in text
    assert "KEYSTONE_EXA_SEARCH_FALLBACK=true" in text
    assert "KEYSTONE_EXA_SEARCH_MAX_CALLS_PER_RUN=2" in text
    assert "KEYSTONE_SERPER_ENABLED=false" in text
    assert "KEYSTONE_TAVILY_SEARCH_FALLBACK=false" in text
    assert "KEYSTONE_TAVILY_SEARCH_MAX_CALLS_PER_RUN=2" in text
    assert "KEYSTONE_AGENT_HTML_REVIEW=false" in text
    assert "KEYSTONE_AGENT_HTML_REVIEW_MAX_PAGES=2" in text
    assert "KEYSTONE_LIVE_MODEL_TIMEOUT_SECONDS=120" in text
    assert "KEYSTONE_LIVE_MODEL_MAX_RETRIES=1" in text


def test_live_test_profile_replaces_existing_serper_default_with_searxng(tmp_path) -> None:
    module = load_module()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "SEARCH_PROVIDER=serper",
                "KEYSTONE_LIVE_MODE=false",
                "KEYSTONE_DRY_RUN=true",
                "",
            ]
        ),
        encoding="utf-8",
    )

    module.main(["live-test", "--env-file", str(env_file)])

    text = env_file.read_text(encoding="utf-8")
    assert "KEYSTONE_LIVE_MODE=true" in text
    assert "KEYSTONE_DRY_RUN=false" in text
    assert "KEYSTONE_ENABLE_LIVE_GMAIL=true" in text
    assert "KEYSTONE_ENABLE_LIVE_SLACK=false" in text
    assert "KEYSTONE_ENABLE_LIVE_RESEARCH=true" in text
    assert "KEYSTONE_ENABLE_LIVE_CRM=false" in text
    assert "SEARCH_PROVIDER=searxng" in text
    assert "KEYSTONE_ENABLE_WEBSITE_EXTRACTION=true" in text
    assert "KEYSTONE_WEBSITE_EXTRACTOR=trafilatura" in text
    assert "SEARXNG_BASE_URL=http://127.0.0.1:18080" in text
    assert "KEYSTONE_SEARXNG_TRANSIENT=true" in text
    assert "KEYSTONE_EXA_SEARCH_FALLBACK=true" in text
    assert "KEYSTONE_EXA_SEARCH_MAX_CALLS_PER_RUN=2" in text
    assert "KEYSTONE_SERPER_ENABLED=false" in text
    assert "KEYSTONE_TAVILY_SEARCH_FALLBACK=false" in text
    assert "KEYSTONE_TAVILY_SEARCH_MAX_CALLS_PER_RUN=2" in text
    assert "KEYSTONE_AGENT_HTML_REVIEW=true" in text
    assert "KEYSTONE_AGENT_HTML_REVIEW_MAX_PAGES=2" in text
    assert "KEYSTONE_LIVE_MODEL_TIMEOUT_SECONDS=120" in text
    assert "KEYSTONE_LIVE_MODEL_MAX_RETRIES=1" in text
    assert "AUTO_SEND_EMAIL=false" in text


def test_live_test_profile_can_explicitly_select_exa(tmp_path) -> None:
    module = load_module()
    env_file = tmp_path / ".env"

    module.main(["live-test", "--env-file", str(env_file), "--search-provider", "exa"])

    text = env_file.read_text(encoding="utf-8")
    assert "SEARCH_PROVIDER=exa" in text
    assert "KEYSTONE_ENABLE_WEBSITE_EXTRACTION=true" in text
    assert "KEYSTONE_WEBSITE_EXTRACTOR=trafilatura" in text
    assert "SEARXNG_BASE_URL=http://127.0.0.1:18080" in text
    assert "KEYSTONE_EXA_SEARCH_FALLBACK=true" in text
    assert "KEYSTONE_EXA_SEARCH_MAX_CALLS_PER_RUN=2" in text
    assert "KEYSTONE_SERPER_ENABLED=false" in text
    assert "KEYSTONE_TAVILY_SEARCH_FALLBACK=false" in text
    assert "KEYSTONE_TAVILY_SEARCH_MAX_CALLS_PER_RUN=2" in text
    assert "KEYSTONE_AGENT_HTML_REVIEW=true" in text


def test_full_live_profile_creates_missing_env_file_with_expected_values(tmp_path) -> None:
    module = load_module()
    env_file = tmp_path / ".env.local"

    module.main(["full-live", "--env-file", str(env_file)])

    text = env_file.read_text(encoding="utf-8")
    assert text.startswith("# Managed by scripts/switch_operator_mode.py\n")
    assert "KEYSTONE_LIVE_MODE=true" in text
    assert "KEYSTONE_DRY_RUN=false" in text
    assert "KEYSTONE_ENABLE_LIVE_GMAIL=true" in text
    assert "KEYSTONE_ENABLE_LIVE_SLACK=true" in text
    assert "KEYSTONE_ENABLE_LIVE_RESEARCH=true" in text
    assert "KEYSTONE_ENABLE_LIVE_CRM=false" in text
    assert "KEYSTONE_ENABLE_WEBSITE_EXTRACTION=true" in text
    assert "KEYSTONE_WEBSITE_EXTRACTOR=trafilatura" in text
    assert "SEARXNG_BASE_URL=http://127.0.0.1:18080" in text
    assert "KEYSTONE_SEARXNG_TRANSIENT=true" in text
    assert "KEYSTONE_EXA_SEARCH_FALLBACK=true" in text
    assert "KEYSTONE_EXA_SEARCH_MAX_CALLS_PER_RUN=2" in text
    assert "KEYSTONE_SERPER_ENABLED=false" in text
    assert "KEYSTONE_TAVILY_SEARCH_FALLBACK=false" in text
    assert "KEYSTONE_TAVILY_SEARCH_MAX_CALLS_PER_RUN=2" in text
    assert "KEYSTONE_AGENT_HTML_REVIEW=true" in text
    assert "KEYSTONE_AGENT_HTML_REVIEW_MAX_PAGES=2" in text
    assert "KEYSTONE_LIVE_MODEL_TIMEOUT_SECONDS=120" in text
    assert "KEYSTONE_LIVE_MODEL_MAX_RETRIES=1" in text
    assert "SEARCH_PROVIDER=searxng" in text
    assert "AUTO_SEND_EMAIL=false" in text
