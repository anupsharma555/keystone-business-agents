from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_dev_quality_tools_are_declared() -> None:
    text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert '"coverage[toml]"' in text
    assert '"pip-audit"' in text


def test_vulnerable_dependency_floors_are_constrained() -> None:
    text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    for requirement in (
        '"aiohttp>=3.13.4"',
        '"orjson>=3.11.6"',
        '"PyJWT>=2.12.0"',
        '"python-dotenv>=1.2.2"',
        '"python-multipart>=0.0.27"',
        '"urllib3>=2.7.0"',
    ):
        assert requirement in text


def test_ruff_excludes_local_artifacts() -> None:
    text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'extend-exclude = ["artifacts"]' in text


def test_coverage_is_configured() -> None:
    text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "[tool.coverage.run]" in text
    assert "branch = true" in text
    assert "[tool.coverage.report]" in text
    assert "fail_under = 77" in text


def test_quality_script_exists() -> None:
    script = PROJECT_ROOT / "scripts" / "check_quality.py"

    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert "pip_audit" in text
    assert "--skip-editable" in text
