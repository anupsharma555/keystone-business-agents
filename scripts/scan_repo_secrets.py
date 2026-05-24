"""Scan git-tracked text files for high-signal secret patterns."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

PATTERN_SPECS: tuple[tuple[str, str, int], ...] = (
    ("openai_api_key", r"\bsk-[A-Za-z0-9_-]{20,}\b", 0),
    ("github_pat_classic", r"\bghp_[A-Za-z0-9]{36}\b", 0),
    ("github_pat_fine_grained", r"\bgithub_pat_[A-Za-z0-9_]{80,}\b", 0),
    ("slack_token", r"\bxox[baprs]-[0-9A-Za-z-]{20,}\b", re.IGNORECASE),
    ("aws_access_key_id", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", 0),
    ("google_api_key", r"\bAIza[0-9A-Za-z_-]{35}\b", 0),
    ("google_oauth_token", r"\bya29\.[0-9A-Za-z_-]+\b", 0),
    ("private_key_header", r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----", 0),
)
COMPILED_PATTERNS = tuple(
    (name, re.compile(pattern, flags)) for name, pattern, flags in PATTERN_SPECS
)


@dataclass(frozen=True)
class SecretFinding:
    path: str
    pattern: str
    line: int
    sample: str


@dataclass(frozen=True)
class SecretScanReport:
    root: str
    scanned_files: int
    findings: tuple[SecretFinding, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "root": self.root,
            "scanned_files": self.scanned_files,
            "findings": [finding.__dict__ for finding in self.findings],
            "finding_count": len(self.findings),
        }


def _tracked_files(root: Path) -> list[Path]:
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    entries = [entry for entry in completed.stdout.decode("utf-8").split("\0") if entry]
    return [root / entry for entry in entries]


def _redact_match(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}***{value[-4:]}"


def _read_text(path: Path) -> str | None:
    raw = path.read_bytes()
    if b"\x00" in raw:
        return None
    return raw.decode("utf-8", errors="ignore")


def scan_tracked_files(root: Path) -> SecretScanReport:
    findings: list[SecretFinding] = []
    scanned_files = 0

    for path in _tracked_files(root):
        if not path.is_file():
            continue
        text = _read_text(path)
        if text is None:
            continue
        scanned_files += 1
        relative_path = path.relative_to(root).as_posix()
        for pattern_name, pattern in COMPILED_PATTERNS:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(
                    SecretFinding(
                        path=relative_path,
                        pattern=pattern_name,
                        line=line,
                        sample=_redact_match(match.group(0)),
                    )
                )

    return SecretScanReport(root=str(root), scanned_files=scanned_files, findings=tuple(findings))


def _default_root() -> Path:
    return Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=_default_root(),
        help="Git repository root to scan (defaults to this repository root).",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    report = scan_tracked_files(root)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    elif report.findings:
        print(f"Scanned {report.scanned_files} tracked text files under {root}.")
        print(f"Found {len(report.findings)} potential secret(s):")
        for finding in report.findings:
            print(
                f"- {finding.path}:{finding.line} "
                f"[{finding.pattern}] sample={finding.sample}"
            )
    else:
        print(
            "Scanned "
            f"{report.scanned_files} tracked text files under {root}. "
            "No high-signal secret patterns were found."
        )

    return 1 if report.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
