"""Reviewable launcher for a bounded read-only Slack acceptance thread."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from keystone_agents.canary_acceptance import (
    CLAIM_ENV,
    PROFILE_ENV,
    AcceptanceProfile,
    CanaryPolicyError,
    ScopeLedger,
    _hash,
    canary_followup_request,
    child_environment,
    load_profile,
)


def prepare_root_arguments(
    profile: AcceptanceProfile, argv: list[str]
) -> tuple[list[str], dict[str, Any]]:
    if argv[:3] != ["-m", "keystone_agents.cli", "ask"]:
        raise CanaryPolicyError("canary_only_approved_root_ask_allowed")
    values = {
        "--database-url",
        "--context-file",
        "--max-openai-requests",
        "--max-manager-steps",
        "--max-results",
        "--agent",
        "--linked-work-item-id",
    }
    flags = {"--live-sdk", "--live-search", "--json"}
    parsed: dict[str, str] = {}
    seen_flags = set()
    request = ""
    index = 3
    while index < len(argv):
        item = argv[index]
        option, separator, inline = item.partition("=")
        if option in values:
            if option in parsed:
                raise CanaryPolicyError("canary_duplicate_cli_option")
            if separator:
                value = inline
            else:
                index += 1
                if index >= len(argv):
                    raise CanaryPolicyError("canary_missing_cli_value")
                value = argv[index]
            parsed[option] = value
        elif item in flags and item not in seen_flags:
            seen_flags.add(item)
        elif not item.startswith("-") and not request:
            request = item
        else:
            raise CanaryPolicyError("canary_cli_scope_not_allowed")
        index += 1
    if parsed.get("--agent", "chief_of_staff") != "chief_of_staff":
        raise CanaryPolicyError("canary_root_owner_not_allowed")
    if "--live-sdk" not in seen_flags or "--json" not in seen_flags:
        raise CanaryPolicyError("canary_explicit_sdk_and_json_required")
    if profile.is_public_preprint_scenario and "--live-search" in seen_flags:
        raise CanaryPolicyError("canary_public_preprint_live_search_not_allowed")
    path = Path(parsed.get("--context-file", "")).expanduser()
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or not path.resolve().is_relative_to((profile.state_dir / "slack-context").resolve())
        or path.stat().st_size > 1_000_000
    ):
        raise CanaryPolicyError("canary_context_path_not_confined")
    context = json.loads(path.read_text())
    if not isinstance(context, dict):
        raise CanaryPolicyError("canary_context_not_object")
    followup = bool(context.get("thread_ts")
                    and context.get("request_ts") != context.get("thread_ts"))
    if followup:
        if not profile.followup_request_sha256:
            raise CanaryPolicyError("canary_followup_not_approved")
        if (
            _hash(canary_followup_request(request)) != profile.followup_request_sha256
            or canary_followup_request(request)
            != canary_followup_request(str(context.get("request_text") or ""))
        ):
            raise CanaryPolicyError("canary_followup_request_digest_mismatch")
    elif _hash(request.strip()) != profile.request_sha256:
        raise CanaryPolicyError("canary_request_digest_mismatch")
    linked_id = parsed.get("--linked-work-item-id", "")
    if linked_id:
        if not followup:
            raise CanaryPolicyError("canary_root_linked_work_item_not_allowed")
        database = profile.state_dir / "keystone-agents.sqlite3"
        if not database.is_file() or database.is_symlink():
            raise CanaryPolicyError("canary_linked_work_item_state_missing")
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            row = connection.execute(
                "SELECT target_json FROM work_items WHERE id=?", (linked_id,),
            ).fetchone()
        metadata = json.loads(row[0]).get("metadata", {}) if row else {}
        binding = metadata.get("slack_context", {})
        if (binding.get("channel_id"), binding.get("thread_ts")) != (
            context.get("channel_id"), context.get("thread_ts"),
        ):
            raise CanaryPolicyError("canary_linked_work_item_thread_mismatch")
    try:
        request_limit = min(
            profile.max_requests,
            profile.max_requests_for_turn(2 if followup else 1),
            int(parsed.get("--max-openai-requests", profile.max_requests)),
        )
        manager_limit = min(5, int(parsed.get("--max-manager-steps", "5")))
        result_limit = min(1, int(parsed.get("--max-results", "1")))
    except ValueError as exc:
        raise CanaryPolicyError("canary_cli_limit_invalid") from exc
    if followup:
        status = ScopeLedger(profile).thread_status()
        if status["status"] == "awaiting_followup":
            request_limit = min(request_limit, status["remaining_model_requests"])
            if request_limit == 0:
                raise CanaryPolicyError("canary_request_budget_exhausted")
    if min(request_limit, manager_limit, result_limit) < 1:
        raise CanaryPolicyError("canary_cli_limit_invalid")
    rewritten = [
        "-m",
        "keystone_agents.cli",
        "ask",
        "--agent",
        "chief_of_staff",
        "--database-url",
        profile.database_url,
        "--context-file",
        str(path.resolve()),
        "--max-openai-requests",
        str(request_limit),
        "--max-manager-steps",
        str(manager_limit),
        "--max-results",
        str(result_limit),
        "--live-sdk",
        "--json",
    ]
    if "--live-search" in seen_flags:
        rewritten.append("--live-search")
    if linked_id:
        rewritten.extend(["--linked-work-item-id", linked_id])
    return [*rewritten, request], context


def run(profile_path: Path, argv: list[str], *, executor: Any = subprocess.run) -> int:
    profile = load_profile(profile_path)
    if profile is None:
        raise CanaryPolicyError("canary_profile_missing")
    profile.check_paths()
    from keystone_agents.runtime.provenance import build_runtime_fingerprint

    fingerprint = build_runtime_fingerprint(repo_root=profile.repo_root)
    driver = profile.repo_root / ".keystone/v2/slack-readiness/kba-child-launcher.py"
    manifest = {
        **profile.manifest(),
        "runtime_fingerprint": fingerprint,
        "driver_sha256": _hash(driver.read_bytes()) if driver.is_file() else "",
        "python": str(profile.repo_root / ".venv/bin/python"),
        "actual_python_executable": sys.executable,
        "actual_python_prefix": sys.prefix,
        "loaded_policy_module": str(Path(__file__).resolve()),
    }
    if argv in (["--canary-preview"], ["--canary-import-check"]):
        print(json.dumps(manifest, sort_keys=True))
        return 0
    if len(argv) == 3 and argv[0] == "--canary-renew-followup":
        replacement_path = Path(argv[1]).expanduser()
        review_path = Path(argv[2]).expanduser()
        for candidate in (replacement_path, review_path):
            if (
                not candidate.is_absolute()
                or candidate.is_symlink()
                or not candidate.is_file()
                or candidate.stat().st_size > 32_000
            ):
                raise CanaryPolicyError("canary_followup_renewal_file_invalid")
        replacement = AcceptanceProfile.model_validate_json(
            replacement_path.read_text()
        )
        review = json.loads(review_path.read_text())
        if not isinstance(review, dict):
            raise CanaryPolicyError("canary_followup_renewal_budget_evidence_unknown")
        receipt = ScopeLedger(profile).renew_awaiting_followup(
            replacement,
            expected_old_profile_digest=profile.digest,
            review=review,
        )
        print(json.dumps(receipt, sort_keys=True))
        return 0
    if argv in (["--canary-thread-status"], ["--canary-expire-thread"]):
        ledger = ScopeLedger(profile)
        if argv == ["--canary-expire-thread"]:
            ledger.expire_thread()
        print(json.dumps(ledger.thread_status(), sort_keys=True))
        return 0
    profile.check_paths()
    profile.check_fresh_balance()
    ledger = ScopeLedger(profile)
    turn_request_limit: int | None = None
    if len(argv) == 2 and argv[0] == "--canary-resume":
        claim = ledger.resume(argv[1])
        arguments = [
            str(profile.repo_root / "scripts/kba_execution.py"),
            "--database-url",
            profile.database_url,
            "resume",
            argv[1],
            "--live-sdk",
        ]
        if not profile.is_public_preprint_scenario:
            arguments.append("--live-search")
    else:
        arguments, context = prepare_root_arguments(profile, argv)
        turn_request_limit = int(
            arguments[arguments.index("--max-openai-requests") + 1]
        )
        followup = context.get("request_ts") != context.get("thread_ts")
        claim = ledger.claim_followup(context) if followup else ledger.claim(context)
        turn_key = _hash(str(context.get("request_ts") or ""))[:12]
        accepted_context = profile.state_dir / f"accepted-context-{claim[:16]}-{turn_key}.json"
        with accepted_context.open("x") as handle:
            os.chmod(accepted_context, 0o600)
            handle.write(json.dumps(context, sort_keys=True, ensure_ascii=True, allow_nan=False))
        arguments[arguments.index("--context-file") + 1] = str(accepted_context)
    try:
        env = child_environment(
            profile,
            claim,
            profile_path.resolve(),
            turn_request_limit=turn_request_limit,
        )
        env[CLAIM_ENV] = claim
        runtime_record = profile.state_dir / "loaded-runtime.json"
        runtime_record.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        runtime_record.chmod(0o600)
        result = executor(
            [str(profile.repo_root / ".venv/bin/python"), *arguments],
            cwd=str(profile.repo_root),
            env=env,
            check=False,
        )
        code = int(result.returncode)
    except BaseException:
        # Interrupted/unknown child state stays running; never permit an automatic resume.
        raise
    ledger.finish(claim, code)
    return code


def main(argv: list[str] | None = None) -> int:
    try:
        return run(
            Path(os.environ.get(PROFILE_ENV, "")), list(sys.argv[1:] if argv is None else argv)
        )
    except (CanaryPolicyError, ValueError, OSError, sqlite3.Error) as exc:
        reason = (str(exc) if isinstance(exc, CanaryPolicyError)
                  else "canary_local_configuration_invalid")
        message = {
            "canary_followup_not_approved": (
                "This acceptance profile covered a single root only. Its isolated context "
                "cannot be continued by this launcher; no model request was made."
            ),
            "canary_thread_not_awaiting_followup": (
                "This isolated test thread is closed or already processing a turn. "
                "No new model request was made."
            ),
            "canary_balance_observation_stale": (
                "The reviewed test window has expired. The isolated context is retained, "
                "but no model request was made."
            ),
        }.get(reason, "The reviewed canary scope or execution policy was not satisfied.")
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "kind": "canary_policy_rejected",
                    "reason_code": reason,
                    "message": message,
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
