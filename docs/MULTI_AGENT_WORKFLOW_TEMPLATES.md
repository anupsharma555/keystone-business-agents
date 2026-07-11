# Multi-Agent Workflow Templates

KBA should support independent agent runs, combined WorkItem workflows, scheduled
loops, and safe modification loops through backend-selected workflow templates.
The operator should not need prompt flags to choose these. Orchestrator,
Chief of Staff, WorkItem state, automation triggers, and available context should
select the template when the required inputs exist.

The canonical template catalog is
`src/keystone_agents/multi_agent_workflow_templates.py`. Each template names the
trigger, cadence, handoff contract, tool tier, approval gate, output
destinations, budget/stop condition, validation path, source attribution
contract, and side-effect boundary.

## Operating Model

Independent runs remain safe specialist tasks: a named agent can read, synthesize,
plan, or draft within its own schema and return blockers when required context is
missing.

Combined workflows sequence agents through WorkItem state. Orchestrator captures
the raw request and routing memo, Python gates enforce source sufficiency and
approval state, specialists receive typed context packs, and Chief of Staff or
deterministic review prepares the human-facing summary.

Scheduled workflows are declared backend jobs with bounded inputs, cadence,
tool tier, approval gate, output destination, budget limit, diagnostics, and stop
condition. A schedule can start a dry-run or read-only synthesis, but it never
grants send, post, schedule, Workspace, Airtable, Gmail, CRM, or Zotero write
authority by itself.

Modification loops revise prior WorkItems, drafts, research packets,
opportunity records, or documentation plans while retaining the original source
refs, approval state, blockers, and audit history.

## First Templates

1. `gmail_thread_research_opportunity_outreach`: Gmail thread context to
   Business Research, Opportunity Scout, Workspace/Airtable documentation plans,
   and approval-gated Outreach Composer draft.
2. `workspace_research_packet_refresh`: Workspace document or folder context to
   source-backed research refresh and Google Doc update plan.
3. `rss_zotero_preprint_research_digest`: RSS/announcement deltas, Zotero
   context, preprint context, and Business Research synthesis for a digest.
4. `weekly_opportunity_to_outreach_review`: scheduled Opportunity Scout scan
   through research enrichment, contact-path review, and draft approval queue.
5. `key_email_response_queue`: scheduled Gmail triage for important response
   needs, with research support and draft-only reply plans.
6. `contact_opportunity_documentation_refresh`: opportunity/contact state,
   Airtable context, Workspace context, and research evidence into update plans.
7. `scheduled_review_retry_loop`: review open WorkItems, failed automation runs,
   stale approvals, and blocked drafts; recommend safe retry or clarification.

## ANU-123 Real Keystone Workflow Wiring Map

The first real-project wiring slice should not require new agents. It should add
a thin project context layer and no-live fixtures around existing agents,
context packs, and typed tools.

| Workflow | Real Keystone operating case | Current agents, context packs, and tools | Smallest repo change or test to make executable |
| --- | --- | --- | --- |
| Inbound project or partner email to research and draft-only follow-up | A Gmail thread asks whether Keystone can support evaluation, advisory, clinical AI, or measurement work. | Orchestrator -> Gmail Triage -> Business Research -> Opportunity Scout -> Outreach Composer -> Chief of Staff. Uses `GmailContextPack`, `ResearchContextPack`, `OpportunityContextPack`, `OutreachContextPack`; Gmail tools, `search_web`, website extraction, approved contact/context loaders, approval queue, Workspace/Airtable write-plan tools. | Add a synthetic project fixture with Gmail thread refs, target company hint, source refs, and allowed actions. Add a no-live WorkItem test that preserves project id through Gmail -> Research -> Outreach blocker/approval checkpoint and proves no send/draft/write occurs. |
| Weekly opportunity review to research and outreach review queue | Keystone wants a weekly scan of grants, RFPs, partnerships, conferences, roles, or companies tied to KNI fit. | Orchestrator -> Opportunity Scout -> Business Research -> Outreach Composer -> Chief of Staff. Uses `OpportunityContextPack`, `ResearchContextPack`, `OutreachContextPack`; opportunity source tools, scoring/dedup helpers, `search_web`, source extraction, approval queue. | Promote `weekly_opportunity_to_outreach_review` from candidate to a fixture-backed scheduled WorkItem. Add a no-live test for source-backed scoring, duplicate/rejected-candidate handling, and a human review checkpoint instead of external outreach. |
| KNIOps research packet / Google Workspace refresh | A scoped Drive folder, Doc, or Sheet needs a current internal research/update packet with source-backed facts and stale-fact notes. | Orchestrator -> Google Workspace Context -> Business Research -> Chief of Staff. Uses `ResearchContextPack`; Workspace read tools, Google Doc/Sheet write-plan tools, `search_web`, source extraction, source attribution review. | Add `ProjectContextPack` fields for Drive folder/doc/sheet refs and sensitivity flags. Add a fixture test that reads synthetic Workspace context, prepares a Google Doc update plan, and blocks actual Workspace writes without approval. |
| RSS, preprint, and Zotero intelligence digest | Keystone wants recurring psychiatry/clinical AI intelligence from RSS announcements, preliminary articles, and Zotero library context before deciding what to research or pursue. | Orchestrator -> RSS Context -> Preprints Context -> Zotero Context -> Business Research -> Chief of Staff. Uses `ResearchContextPack`; RSS/preprint history tools, Zotero context tools, local context search/read tools, source attribution/caveat handling. | Add a synthetic project fixture that points to feed item refs, preprint refs, and Zotero collection refs. Add a no-live digest test that preserves source IDs/URLs/dates, labels preprints as preliminary, and performs no Slack post or Zotero import. |
| Contact and opportunity documentation refresh | Keystone needs Airtable/Workspace opportunity or contact records refreshed from source-backed research without mutating business systems. | Orchestrator -> Opportunity Scout -> Business Research -> Airtable Context -> Google Workspace Context -> Chief of Staff. Uses `OpportunityContextPack`, `ResearchContextPack`; Airtable schema/read tools, Workspace read/write-plan tools, source extraction, record identity blockers. | Add project fixture fields for Airtable base/table/view/record refs plus Workspace artifact refs. Add a test where ambiguous record identity blocks writes, while exact identity produces Airtable and Workspace update plans only. |

First controlled pilot recommendation: start with
`gmail_thread_research_opportunity_outreach`. It exercises the widest practical
Keystone workflow, already matches current WorkItem/context-pack contracts, and
can be validated with synthetic Gmail context before any live Gmail retrieval or
draft creation. The pilot is resolved only when the selected project, source
basis, WorkItem state, approval boundary, and next safe action survive the full
no-live flow.

Common repo work across all five workflows:

- The minimal `ProjectContextPack` contract is now implemented with project
  id/name/objective, status, owner/reviewer, sensitivity and approval flags,
  source refs, Slack/Workspace/Airtable/Zotero refs, WorkItem links, and
  allowed/blocked actions. It is nested beside every route-specific specialist
  pack and removed from the loose target-metadata summary before prompting.
- One synthetic Gmail/research/outreach project fixture now proves project
  identity survives Gmail, Research, and Outreach pack construction. It does
  not promote that workflow into live project use. A second
  Workspace/Airtable/Zotero/RSS fixture remains future work only when a selected
  pilot workflow requires it.
- Project context remains optional for ordinary asks. When supplied, missing
  identity, absent agent-use approval, or any PHI/patient-specific flag becomes
  an authoritative readiness blocker before specialist use; blocked actions
  override overlapping allowed actions.
- Add a focused no-live acceptance test for each promoted workflow template and
  keep live/API pilots behind the KBA cost-aware ladder.

## ANU-198 Through ANU-202 Context-Agent Milestone

The Airtable, Google Workspace, Zotero, RSS, and Preprints context-agent
contracts are tracked in `docs/CONTEXT_AGENT_CONTRACTS.md` and
`src/keystone_agents/context_agent_contracts.py`. Treat that catalog as the
milestone surface for:

- context-pack contracts: source refs, required identifiers, uncertainty fields,
  and bounded evidence packets passed into `ResearchContextPack`,
  `OpportunityContextPack`, and, where safe, `OutreachContextPack`;
- graph-candidate edges: context agents remain tool/context nodes now, with
  graph staging only for WorkItem boundaries that need checkpointing, source
  preservation, approval review, or retry behavior;
- validation requirements: every promotion from candidate to executable workflow
  needs fixture-backed no-live tests for source attribution, exact identity,
  approval gates, no hidden writes, and blocked unsupported mutation.

## Validation

The focused test gate is:

```bash
.venv/bin/python -m pytest tests/test_multi_agent_workflow_templates.py
```

These tests verify the catalog has five to eight templates, covers the requested
Gmail/research/outreach, Workspace/research, RSS/Zotero/research, opportunity,
and scheduled review-loop domains, and preserves source attribution, approval
state, and no-send/no-write boundaries.
