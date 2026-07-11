# Initial Operational Model Validation

This is the first paid proof after current no-model execution gates. It is not
the deferred Promptfoo suite and it does not attempt to validate every agent at
once.

Run `scripts/plan_initial_operational_model_validation.py` to inspect the exact
plan. The planner cannot execute subprocesses or call a model. It covers three
serial Gmail reasoning checks and one fixed-context LangGraph workflow:

1. summarize today's important mailbox context;
2. find and reason over one bounded thread, then prepare reply text without a
   provider write;
3. revise that answer in the same local SDK session; and
4. complete a supplied-material research-to-draft graph job without live search
   or external writes.

The expected total is 7-10 OpenAI requests. Stop at 10 requests or $2, whichever
comes first, and also stop on the first shared failure, unexpected retry,
missing usage/trace evidence, or side effect. Provider draft mechanics are not
part of this paid batch because create/read/update/read/delete/absence is
already proven deterministically.

Before run 1, obtain explicit approval for this exact batch, refresh the
existing logged-in Chrome billing page, record its newly observed credit
balance and timestamp, verify `KEYSTONE_OPENAI_API_KEY` without displaying it,
and create isolated session and receipt paths. The previously observed balance
must not be used as the live baseline without this refresh.

## Current Checkpoint: 2026-07-10

The operator approved scenarios 1 and 2 only, to be run serially with a fix
between failures. Both used `gpt-5.4-mini`, isolated local SDK sessions,
read-only Gmail retrieval, no live search, and no provider write.

| Scenario | Status | Current evidence | Remaining proof |
|---|---|---|---|
| 1. Today-only Gmail summary and action triage | **Partial** | One request read three bounded messages, grouped every message, returned next actions, and preserved no-send. | One routine account-setup notification was over-escalated and bucket/priority fields contradicted each other. The prompt and schema fixes pass offline; repeat once live. |
| 2. Selected-thread summary and reply text | **Pass** | One request used prior-thread context, accurately described a warm collaboration follow-up, and produced concise reply text with `draft_created=false`. | No capability rerun needed. Its trace-export envelope should be confirmed by the scenario 1 repeat. |

The two requests used 35,120 and 30,937 tokens respectively. Maintained local
pricing estimates were `$0.03025125` and `$0.02993775`, or `$0.060189`
combined. The logged-in billing page was refreshed before and after the runs:
credit balance changed from `$4.16` to `$4.10`. Current-day Admin Costs had not
yet aggregated the requests.

The second run exposed a non-fatal SDK trace-export 401 because the exporter
consulted generic `OPENAI_API_KEY`, which is not this project's key. Live KBA
run configuration now explicitly supplies `KEYSTONE_OPENAI_API_KEY` to trace
export. The next API action is not implicitly approved: present one exact
scenario-1 repeat, one expected request, `gpt-5.4-mini`, no-write tool scope,
and its budget, then refresh billing again and wait for approval.
