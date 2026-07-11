# ANU-60 Live Slack Evidence Template

Copy this into the ANU-60 Linear issue or `docs/ORCHESTRATOR_BRIDGE_BACKLOG.md`
after live probes are explicitly approved and run. Do not fill this from
`#evals` rows alone; ANU-60 completion requires visible `#ai-agents-workflow`
evidence.

## Run Boundary

- Approval for Slack posting in `#ai-agents-workflow`: yes/no (recorded: yes)
- Approval for live model/search/API spend and stop condition: yes/no (recorded: yes; sequential allowance exhausted after the graph attempt)
- Preflight command result:
  - `npm run eval:slack:anu60-preflight`: pass/fail
  - `npm run eval:slack:strict-readiness -- --json`: pass/fail
  - `../keystone-slack/scripts/manage_slack_socket.sh status`: pass/fail
  - optional `npm run eval:slack:strict-live-readiness -- --json`: pass/fail/not run
- External sends/writes/drafts/feed refreshes approved: no
- Stop condition used: stop on first failed visible render, unexpected side effect, retry, or request/cost ceiling breach

## Probe Results

### RSS Context

- Prompt:
- Slack permalink:
- Local run id or WorkItem id:
- Route:
- Output type:
- Visible body starts with answer-first `human_summary`: yes/no
- Provider/model/timing metadata appears before answer: yes/no
- Required reference or retrieval-limit language present: yes/no
- External write/send/draft/feed-refresh observed: yes/no
- Result: pass/fail
- Notes:

### Preprints Context

- Prompt:
- Slack permalink:
- Local run id or WorkItem id:
- Route:
- Output type:
- Visible body starts with answer-first `human_summary`: yes/no
- Provider/model/timing metadata appears before answer: yes/no
- Required reference or retrieval-limit language present: yes/no
- External write/send/draft/feed-refresh observed: yes/no
- Result: pass/fail
- Notes:

### Direct Business Research

- Prompt: `@KNI business research analyst "research Suki AI. Return a concise brief covering what the company does, current signals, KNI fit, evidence gaps, recommendation, and visible source URLs."`
- Slack permalink: https://as-xkn6329.slack.com/archives/C0ASJ6QU1FX/p1783794811442589
- Local run id or WorkItem id: `sbar_613ad9e3000b4ef39b0323bf836685b4`; KBA run `456`
- Route: `business_research_analyst`
- Output type: company research brief
- Visible body starts with answer-first `human_summary`: yes
- Provider/model/timing metadata appears before answer: no
- Visible source URLs or source-limit language present: yes
- External write/send/draft/feed-refresh observed: no
- Result: pass
- Notes: one KNI reply updated in place; three OpenAI requests; estimated `$0.054459`

### Connector-backed Gmail Graph

- Prompt: `@KNI review the latest Gmail thread from the configured exact test sender, including all messages and the original inquiry. Identify the current conversation state, recommend the most useful KNI-specific collaboration next step using only that thread and approved KNI context, and include a reply only if replying now would move the relationship forward. Return a concise summary, supporting evidence, and any approval status that actually applies.`
- Slack permalink: https://as-xkn6329.slack.com/archives/C0ASJ6QU1FX/p1783802711400939
- Local run id or WorkItem id: `wi_b8a3b23a97ed41a5804f4c56c7b3f852`
- Runtime: LangGraph
- One final Slack response: yes
- Selected Gmail identity kept internal: yes
- Gmail → Research → Outreach completed: yes
- Approval state: not required because no immediate reply was recommended
- Provider/model/timing metadata appears before answer: no
- Gmail draft/send, external post, live search, file write, or provider mutation observed: no
- Result: pass
- Notes: one updated-in-place response; four messages reviewed; latest courtesy close controlled the recommendation; no stale scheduling action, reply copy, or unnecessary approval was shown. The graph attached a no-write collaboration recommendation, used two OpenAI requests at an estimated `$0.044811`, and performed no search or provider mutation.

### Conversational Business Research

- Prompt:
- Slack permalink:
- Local run id or WorkItem id:
- Route:
- Output type:
- Bridge accepted conversational named-agent wording before KBA planning: yes/no
- Visible body starts with answer-first `human_summary`: yes/no
- Provider/model/timing metadata appears before answer: yes/no
- Visible source URLs or source-limit language present: yes/no
- External write/send/draft/feed-refresh observed: yes/no
- Result: pass/fail
- Notes:

### Gmail Missing Context

- Prompt:
- Slack permalink:
- Local run id or WorkItem id:
- Route:
- Output type:
- Visible body says `Gmail Triage needs email context`: yes/no
- Copy reads as blocked/missing-context, not started or completed: yes/no
- Gmail draft created: yes/no
- Email sent: yes/no
- Result: pass/fail
- Notes:

### Timeout / Failure Fixture Boundary

- Sibling timeout/process-group fixture command:
  `python3 -B -m unittest tests.test_app_mentions.SlackAppMentionTests.test_business_agents_run_command_timeout_returns_structured_failure_and_kills_group tests.test_app_mentions.SlackAppMentionTests.test_business_agents_streaming_run_command_timeout_kills_group`
- Result: pass/fail
- Failure output is redacted and actionable: yes/no
- Process group cleanup covered by fixture: yes/no
- Live timeout probe approved: yes/no
- Live timeout Slack permalink, if approved and run:
- Notes:

## Acceptance Coverage Map

- Direct Business Research done criterion covered: yes/no
- Conversational Business Research done criterion covered: yes/no
- RSS context-agent done criterion covered: yes/no
- Preprints context-agent done criterion covered: yes/no
- Blocked preflight done criterion covered: yes/no
- Timeout/failure done criterion covered by fixture or approved live probe: yes/no

## Completion Summary

- All required probes passed: yes/no
- Slack permalinks captured for every probe: yes/no
- Local run ids or WorkItem ids captured for every probe: yes/no
- No metadata-first visible body observed: yes/no
- No external side effects observed: yes/no
- Residual proof boundary:
- Recommendation for ANU-60 state:
