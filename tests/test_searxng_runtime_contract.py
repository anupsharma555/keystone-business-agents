from pathlib import Path


def test_searxng_compose_mounts_repo_settings_read_only() -> None:
    compose = Path("infra/searxng/docker-compose.yml").read_text(encoding="utf-8")
    settings = Path("infra/searxng/core-config/settings.yml").read_text(encoding="utf-8")

    assert "./core-config:/etc/searxng:ro" in compose
    assert "./core-config/settings.yml:/etc/searxng/settings.yml:ro" not in compose
    assert "FORCE_OWNERSHIP" not in compose
    assert "    - json" in settings
