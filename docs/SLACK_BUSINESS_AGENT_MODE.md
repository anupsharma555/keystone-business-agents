# Slack Business-Agent Mode

Slack business-agent mode is the bridge from the local `keystone-slack` Socket
Mode worker into this repository's `keystone ask` and script-backed agent
workflows. Slack is the request and review surface. SQLite remains the audit
record. Keystone still does not send external email, publish outbound copy, or
approve work automatically.

The current path is Orchestrator-first. The bridge passes the raw Slack request,
selected-message context, compact thread replies, prior same-thread run
summaries, WorkItem metadata, and channel automation hints into Keystone before
specialist execution. Orchestrator preflight reads that context and writes
route advice, blockers, retrieval hints, and planner rationale. Specialists
then receive the raw request plus the Orchestrator memo/context; deterministic
Python gates still own approvals, exact record identity, source sufficiency,
and side-effect blocking.

## Safe Operating Model

- Dry-run is the default in this repo.
- Live model execution is separate from live search, Slack posting, Gmail draft
  creation, and external sending.
- `@KNI` Slack context can route a request into the business-agents CLI, but it
  does not grant permission to send, publish, schedule, or bypass approval.
- `@KNI keystone ask ...` is accepted as an alias for the same natural-language
  entrypoint; the bridge should strip the CLI-shaped prefix and route the
  remaining request normally.
- Slack approval cards are notifications only unless a human uses the local
  approval flow or a Slack button that records a local SQLite decision.
- Gmail draft creation is draft-only and requires a local approved approval
  item. Gmail send is not implemented.
- Slack history access is disabled by default and should stay disabled unless
  the workspace intentionally grants and reviews history scopes.

## Config Reference

These variables are read by the `keystone-slack` runtime. They are listed here
so business-agent developers can see the safety boundary from this repo.

| Feature | Env vars | Requires | Boundary |
| --- | --- | --- | --- |
| Slack mention context | `KNI_BUSINESS_AGENTS_SLACK_CONTEXT_ENABLED`, `KNI_BUSINESS_AGENTS_REPO`, `KNI_BUSINESS_AGENTS_PYTHON`, `KNI_BUSINESS_AGENTS_DATABASE_URL` | Slack `app_mentions:read`, `chat:write`, `SLACK_BOT_TOKEN` | Routes text into local agent workflows only. |
| Background thread results | `KNI_BUSINESS_AGENTS_BACKGROUND_RUNS` | Slack `chat:write`, `SLACK_BOT_TOKEN` | Posts started/final messages; does not alter agent side-effect gates. |
| Approval packets | `KNI_BUSINESS_AGENTS_APPROVALS_ENABLED`, `KNI_BUSINESS_AGENTS_LIVE_SLACK`, `KNI_BUSINESS_AGENTS_APPROVAL_CHANNEL` | Slack `chat:write`, `SLACK_BOT_TOKEN` | Posts review cards. Approval remains local and scoped. |
| Slack message actions | `KNI_BUSINESS_AGENTS_MESSAGE_ACTIONS_ENABLED` | Slack interactivity enabled | Buttons update local approval records for the named scope; they do not send email, post externally, publish, or schedule. |
| Slack history context | `KNI_BUSINESS_AGENTS_HISTORY_CONTEXT_ENABLED`, `SLACK_APP_MENTION_POLLING_ENABLED` | Slack `channels:history` and `groups:history` if intentionally enabled | Optional fallback/history access; disabled by default. |
| Selected-message thread context | `KNI_BUSINESS_AGENTS_THREAD_CONTEXT_ENABLED`, `KNI_BUSINESS_AGENTS_THREAD_CONTEXT_MAX_MESSAGES`, `KNI_BUSINESS_AGENTS_THREAD_CONTEXT_MAX_CHARS` | Slack `conversations.replies` access for the selected channel plus `SLACK_BOT_TOKEN` | Optional bounded thread context for message actions. The bridge writes a compact context file for KBA; failures are warnings, not approval or send authority. |
| LangGraph WorkItem orchestration | `KNI_BUSINESS_AGENTS_LANGGRAPH` | KBA installed with `.[orchestration]` in `KNI_BUSINESS_AGENTS_PYTHON` | Routes WorkItem advancement through the optional graph wrapper; does not change live side-effect gates. |
| SDK conversation sessions | `KEYSTONE_SDK_SESSIONS`, `KEYSTONE_SDK_SESSION_DB`, `KEYSTONE_SDK_SESSION_ID` | OpenAI Agents SDK live or local SDK run | Local SQLite conversation continuity for follow-up wording. Default automatic capture is limited to Chief of Staff and live WorkItem scopes; WorkItems remain canonical state. |
| Live model execution | `KNI_BUSINESS_AGENTS_LIVE_SDK` and workflow-specific live SDK flags | Business-agent model credentials | Enables LLM synthesis/planning only. |
| Live search | `KNI_BUSINESS_AGENTS_LIVE_SEARCH` | Search provider config in this repo | Enables retrieval only. |
| Gmail drafts | `KNI_BUSINESS_AGENTS_LIVE_GMAIL_DRAFTS`, `KNI_BUSINESS_AGENTS_GMAIL_DRAFT_ACCOUNT` or `KEYSTONE_GMAIL_DRAFT_ACCOUNT` | Gmail OAuth plus an approval item that explicitly allows Slack-triggered Gmail draft creation | Creates Gmail drafts only in the configured account; no send path. |

`SLACK_CONFIGURED_BOT_SCOPES` is optional in `keystone-slack`. When set to the
installed app's bot-scope list, Socket Mode startup can warn if enabled features
are missing required scopes.

## Promptfoo Eval Channel

Use `#evals` for Promptfoo-backed eval asks and human review threads. The KNI
workspace channel id from the current Slack app is `C0BA17Y9C01`. In the Slack
bridge, keep eval-specific configuration pointed at that channel:

```bash
KNI_BUSINESS_AGENTS_EVAL_CHANNEL=C0BA17Y9C01
```

Post the root ask in natural language in `#evals`; the run-completed message,
agent answer, automatic eval footer, human review link, and any optional status
check should remain in that Slack thread. The Slack bridge/KBA context payload
attaches eval metadata for the channel in the background, for example
`case_id=slack_agents_sdk_course_001` for the natural Agents SDK course ask.
Visible `eval case <case_id>` wording is kept only as a debug/fallback command.
This keeps eval traffic out of `#ai-agents-workflow` while preserving the same
no-send, approval, and read-only context boundaries. The run-completed reply
should expose the resolved case id, run id, direct case dashboard link, and
human review form link so a second AI-agent scorecard response is not needed.

Use the review form link as the normal scoring path. If the form is unavailable
during local testing, the deterministic fallback parser can still save compact
score text such as `@KNI here are my scores: accuracy 4 relevance 5 ... safety
pass` in the same thread. That fallback does not require live model, search,
Slack posting, or external side effects.

Use `@KNI how is this eval doing?` in the same thread to get the merged
Promptfoo, Slack run, and human-review status plus the case dashboard link.

## Search Runtime Boundary

Slack and Business Agents can each have a local SearXNG process. The Slack
runtime may use its own search service for Slack-native workflows. Business
Agents child processes should receive the KBA search configuration from the
exported environment contract and default to the KBA SearXNG URL
`http://127.0.0.1:18080` when no explicit override is set. This prevents a Slack
run from silently using a different retrieval lane than a manual KBA CLI or
`@KNI` agent run.

KBA live retrieval emits a compact `retrieval_diagnostics` object for Slack and
CLI renderers. It is safe to display because it contains provider names,
reachability/fallback flags, result/source counts, extraction failures, and
timing summaries only; it must not include credentials, raw headers, OAuth
tokens, or verbose traces.

## End-To-End Workflows

1. `@KNI business agents status` checks bridge configuration and live flag
   posture.
2. `@KNI business research analyst research Lindus Health` routes a named
   specialist request. Live SDK and live search are still controlled by explicit
   flags.
   `@KNI keystone ask business research analyst research Lindus Health` is the
   equivalent CLI-shaped alias.
   Business Research Analyst, Opportunity Scout, Gmail Triage, and Outreach
   Composer do not get broad default SDK conversation sessions unless routed
   through a scoped WorkItem/Slack context or explicitly opted in.
3. `@KNI opportunity scout "find 5 behavioral health AI advisory opportunities"`
   can save local opportunity artifacts. It does not draft outreach by itself.
4. `@KNI business agents weekly opportunities for digital mental health
   partnerships` can create local approval items. Set
   `KNI_BUSINESS_AGENTS_LIVE_SLACK=true` only when review cards should be posted
   to Slack.
5. `@KNI workitem continue <work_item_id>` advances an existing WorkItem. With
   `KNI_BUSINESS_AGENTS_LANGGRAPH=true`, the same command records a
   `langgraph_orchestration` timeline event and a stable graph thread key.
   Direct Slack WorkItem actions keep the deterministic target WorkItem/route,
   attach Orchestrator preflight context, and record an
   `orchestrator_action_review` event after execution.
6. A human approval updates SQLite approval state. If
   `KNI_BUSINESS_AGENTS_LIVE_GMAIL_DRAFTS=true`, an approved email item may
   create a Gmail draft only when the approval card says it is for saving a
   Gmail draft and the OAuth account matches the configured draft account.
   Nothing sends the draft.

Run this repo's health check after changing flags:

```bash
.venv/bin/python scripts/health_check.py --verbose
```

Run `keystone-slack` Socket Mode startup after changing Slack app scopes. It now
prints actionable config warnings for enabled business-agent features that lack
required local config or declared Slack scopes.

## Tested Contracts

The bridge has fixture-mode smoke coverage for the highest-risk cross-repo
contracts: status command construction, natural-language WorkItem creation,
WorkItem continuation, selected Slack context handoff, approval actions that do
not send externally, Orchestrator preflight/review metadata on Slack actions,
child-process environment propagation, and live flag defaults. These tests
should stay deterministic and must not depend on network, real Slack APIs, real
Gmail, or live model calls.
