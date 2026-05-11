# LangGraph Orchestration Option

## Position

OpenAI Agents SDK remains the core specialist-agent layer for Keystone. The four specialists stay as SDK agents:

- Gmail Triage
- Business Research Analyst
- Opportunity Scout
- Outreach Composer

Each specialist keeps its `build_*_agent()` function, markdown prompts, Pydantic structured output, explicit tool wrappers, dry-run fixture mode, and safety guardrails. LangGraph, if added later, should orchestrate these specialists; it should not replace them.

## Why SDK First

The SDK agents are the stable unit of behavior. They own prompts, output schemas, tool access, and guardrails. This keeps each agent testable without a larger workflow engine and prevents orchestration concerns from leaking into specialist prompts.

Keeping the SDK layer first also supports:

- simpler fixture-mode tests
- direct CLI execution of each specialist
- focused safety checks for outbound copy
- source-attributed company, broader research, and opportunity research
- future orchestration that can swap workflow engines without rewriting specialists

## When To Add LangGraph

Add LangGraph only when the project needs stateful orchestration beyond the current CLI and deterministic fixture flows:

- resumable multi-step runs
- persisted checkpoints between specialists
- retry policies per node
- explicit human approval interruptions
- branching workflows for inbound Gmail, researcher/account work, opportunity scoring, and outreach
- audit trails across a complete business-development run

Do not add LangGraph merely to call one specialist agent or to hide business logic in graph nodes.

## Proposed Nodes

- `classify_input`: Decide whether the input is inbound email, company/account context, broader research request, opportunity scouting request, or approved outreach context.
- `gmail_triage`: Run the SDK Gmail Triage agent or fixture equivalent.
- `account_research`: Compatibility node for the SDK Business Research Analyst or fixture equivalent.
- `opportunity_scoring`: Run the SDK Opportunity Scout agent or fixture equivalent.
- `outreach_drafting`: Run the SDK Outreach Composer agent only after approved context exists.
- `approval_checkpoint`: Interrupt for human review before outbound copy is generated or used.
- `storage`: Persist inputs, structured outputs, approvals, and audit records.
- `report`: Render markdown and JSON reports from structured outputs.

## Proposed Edges

- `classify_input -> gmail_triage` for inbound email.
- `classify_input -> account_research` for known companies or accounts.
- `classify_input -> opportunity_scoring` for scout requests.
- `gmail_triage -> approval_checkpoint` when a reply draft or risky content is present.
- `gmail_triage -> account_research` when the email identifies a relevant company.
- `account_research -> opportunity_scoring` when source-backed fit signals exist.
- `opportunity_scoring -> account_research` when a high-priority opportunity needs company enrichment.
- `opportunity_scoring -> approval_checkpoint` before any outreach drafting.
- `approval_checkpoint -> outreach_drafting` only after explicit approval.
- `outreach_drafting -> approval_checkpoint` before any outbound use of draft copy.
- `approval_checkpoint -> storage` after each human decision.
- `storage -> report` for final run output.

## Interruptions And Approval Checkpoints

Interruptions should be explicit and persisted. The graph should pause when:

- outbound email, LinkedIn, Slack, or CRM copy may be produced
- a draft exists and requires review
- PHI or patient-specific content is detected
- legal, financial, security, or contractual content is present
- source attribution is insufficient for a company or opportunity claim
- a live integration flag is requested

Approval checkpoints must record the reviewer, decision, timestamp, scope, and approved next action. Approval to draft does not imply approval to send. No external email may be sent automatically.

## What Not To Implement Yet

- Do not add a LangGraph dependency.
- Do not rewrite SDK specialists as graph-native nodes.
- Do not remove or bypass `build_*_agent()` functions.
- Do not create live integration paths as part of orchestration.
- Do not implement automatic email sending, CRM writes, or scheduled follow-ups.
- Do not put prompts or scoring logic into graph edge definitions.
- Do not persist PHI, secrets, raw credentials, or sensitive draft material in logs.

## Initial Migration Shape

The first implementation should be a thin orchestrator around existing SDK agent calls and fixture helpers. Each graph node should accept typed state, call one specialist or utility, store the structured output, and return the next state. Reports should be generated from structured outputs, not from graph internals.
