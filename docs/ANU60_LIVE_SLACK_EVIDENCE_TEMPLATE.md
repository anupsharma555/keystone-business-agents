# ANU-60 Live Slack Evidence Template

Copy this into the ANU-60 Linear issue or `docs/ORCHESTRATOR_BRIDGE_BACKLOG.md`
after live probes are explicitly approved and run. Do not fill this from
`#evals` rows alone; ANU-60 completion requires visible `#ai-agents-workflow`
evidence.

## Run Boundary

- Approval for Slack posting in `#ai-agents-workflow`: yes/no
- Approval for live model/search/API spend and stop condition: yes/no
- Preflight command result:
  - `npm run eval:slack:anu60-preflight`: pass/fail
  - `npm run eval:slack:strict-readiness -- --json`: pass/fail
  - `../keystone-slack/scripts/manage_slack_socket.sh status`: pass/fail
  - optional `npm run eval:slack:strict-live-readiness -- --json`: pass/fail/not run
- External sends/writes/drafts/feed refreshes approved: no
- Stop condition used:

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

- Prompt:
- Slack permalink:
- Local run id or WorkItem id:
- Route:
- Output type:
- Visible body starts with answer-first `human_summary`: yes/no
- Provider/model/timing metadata appears before answer: yes/no
- Visible source URLs or source-limit language present: yes/no
- External write/send/draft/feed-refresh observed: yes/no
- Result: pass/fail
- Notes:

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
