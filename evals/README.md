# Keystone Evals

This folder is the single home for Keystone benchmark datasets.

```text
evals/
  static/       # JSON fixture-backed specialist evals for scripts/run_evals.py
  local/        # JSONL deterministic workflow and prompt-contract evals
  provider/     # Provider-specific search and browser extraction eval cases
```

Run the default offline suites:

```bash
.venv/bin/python scripts/run_evals.py --agent all --json
.venv/bin/python scripts/run_local_evals.py --json
.venv/bin/python scripts/run_skill_task_matrix.py --json
.venv/bin/python scripts/run_skill_task_matrix.py --surface slack --json
.venv/bin/python scripts/run_skill_task_matrix.py --surface computer --json
```

Track benchmark scores over time:

```bash
.venv/bin/python scripts/run_evals.py --agent all --json --record-benchmark
.venv/bin/python scripts/run_local_evals.py --json --record-benchmark
.venv/bin/python scripts/summarize_benchmark_results.py
```

The skill task matrix is dry-run only. It verifies one agent at a time across
skill-relevant Slack and Computer-style operator surfaces by checking both the
deterministic selected skill set and the actual composed instruction markers.
It does not call Slack, control the desktop, run live models, or perform writes.
Skill matrix results include selection reasons and are included in the default
`run_local_evals.py` suite; use `--dataset skill_task_matrix` to run only that
dataset, or `--surface slack` / `--surface computer` to isolate one operator
surface.

The reusable Slack source-read and multi-target research path is covered by
`evals/local/slack_research_workflow.jsonl`. Run only that offline case with:

```bash
.venv/bin/python scripts/run_local_evals.py --dataset slack_research_workflow --json
```

Live OpenAI API benchmark runs should use the same case IDs and benchmark store,
with explicit labels, provider/model metadata, and redacted artifacts.

See `docs/EVALS.md` for the benchmark model, roadmap, and scoring plan.
