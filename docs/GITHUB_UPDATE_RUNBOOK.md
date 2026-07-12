# GitHub Update Runbook

Use this checklist when publishing local Keystone Business Agents changes to
GitHub. The goal is to avoid a slow rediscovery loop: first determine whether
GitHub is behind only because of local uncommitted work, then run the same CI
gates locally before pushing.

## Safety Rules

- Do not stage local-only configuration, credential, token, database, cache, or
  generated artifact files.
- Keep screenshots, exports, and scratch files out of the commit unless they are
  intentionally documented assets.
- Review staged environment examples before committing; examples may include
  empty placeholders and documented flags, but never live values.
- Prefer one focused commit that describes the local behavior being published.

## Fast Path

Use this when the local branch and `origin/main` are already aligned and the
only goal is to publish local working-tree changes.

```bash
git fetch --all --prune
git rev-list --left-right --count HEAD...origin/main
git status --short
```

Expected divergence for the fast path is `0 0`. If the output is not `0 0`,
stop and inspect the remote/local commit difference before staging.

Stage with intent:

```bash
git add -u
git add <intentional-new-files>
git status --short
```

Run the publish gate:

```bash
.venv/bin/python scripts/check_staged_publish_files.py
.venv/bin/python scripts/scan_repo_secrets.py
git diff --cached --check
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest -q
```

If all checks pass, commit and push:

```bash
git commit -m "<short behavior-focused message>"
git push origin main
git rev-list --left-right --count HEAD...origin/main
```

Expected final divergence is `0 0`.

## Full Review Flow

1. Refresh remote state.

```bash
git fetch --all --prune
git status --short --branch
git rev-list --left-right --count HEAD...origin/main
```

2. Review local changes before staging.

```bash
git diff --stat
git diff --check
git status --short
```

3. Stage tracked edits first, then add only intentional new files.

```bash
git add -u
git add <intentional-new-files>
git status --short
```

4. Run the secret and diff hygiene checks.

```bash
.venv/bin/python scripts/check_staged_publish_files.py
.venv/bin/python scripts/scan_repo_secrets.py
git diff --cached --check
git diff --cached --stat
```

5. Run CI-parity checks.

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest <focused-test-targets>
.venv/bin/python -m pytest -q
```

6. Commit and push.

```bash
git commit -m "<short behavior-focused message>"
git push origin main
```

7. Verify GitHub is current.

```bash
git status --short --branch
git rev-list --left-right --count HEAD...origin/main
```

The expected final state is no local/remote divergence and only intentionally
untracked local scratch files, if any.

## Efficiency Notes

- Prefer `git rev-list --left-right --count HEAD...origin/main` over browsing
  GitHub first. It answers whether GitHub is behind, ahead, or diverged.
- Use `git diff --cached --name-only` as the final staged-file manifest. It is
  faster and less noisy than reviewing the full patch late in the process.
- The staged-file guard rejects browser captures, test-pack artifacts, output
  folders, and root dashboard screenshots even if they were force-added. Move
  an intentionally public image under `docs/` before staging it.
- Run `scripts/scan_repo_secrets.py` after staging, not only before staging, so
  the check reflects exactly what would be committed.
- If Ruff fails on import order or formatting, run `ruff check . --fix` and
  `ruff format .`, then rerun Ruff and pytest. Stage any formatter changes only
  after confirming they are intentional.
- If Ruff exposes broad existing line-length debt in prompt-heavy files, prefer
  a narrow per-file ignore over disabling line length globally.
- Keep one local screenshot or scratch artifact untracked if useful, but call it
  out in the final status so it is not mistaken for a missed publish item.
- Do not push after only a focused test when CI runs the full suite. Focused
  tests are for fast diagnosis; the final publish gate is Ruff plus full pytest.
