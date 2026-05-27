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
```

Track benchmark scores over time:

```bash
.venv/bin/python scripts/run_evals.py --agent all --json --record-benchmark
.venv/bin/python scripts/run_local_evals.py --json --record-benchmark
.venv/bin/python scripts/summarize_benchmark_results.py
```

Live OpenAI API benchmark runs should use the same case IDs and benchmark store,
with explicit labels, provider/model metadata, and redacted artifacts.

See `docs/EVALS.md` for the benchmark model, roadmap, and scoring plan.
