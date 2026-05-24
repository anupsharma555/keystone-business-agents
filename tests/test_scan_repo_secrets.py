from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.scan_repo_secrets import scan_tracked_files

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_scan_repo_secrets_reports_current_repo_clean() -> None:
    report = scan_tracked_files(PROJECT_ROOT)

    assert report.scanned_files > 0
    assert report.findings == ()


def test_scan_repo_secrets_detects_seeded_high_signal_token(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
    )
    fake_secret = "sk-" + "1234567890abcdefghijklmnop"
    (repo / ".env.example").write_text(
        f"KEYSTONE_OPENAI_API_KEY={fake_secret}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo), "add", ".env.example"], check=True)

    report = scan_tracked_files(repo)

    assert report.findings
    finding = report.findings[0]
    assert finding.path == ".env.example"
    assert finding.pattern == "openai_api_key"
