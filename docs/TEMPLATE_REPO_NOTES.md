# Template Repository Notes

These notes summarize the four reference repositories as architecture inputs for Keystone. They are not implementation instructions to copy code.

Repository access was available through public GitHub pages on 2026-04-21. Some notes are based on README and visible source files rather than a full local clone.

## Email Inbox Agent: `DanielJD1216/Email-Inbox-Agent---Doo-Made`

Reference URL: https://github.com/DanielJD1216/Email-Inbox-Agent---Doo-Made

### What It Appears To Do

This is a Python Gmail assistant using the OpenAI Agents SDK and Gmail API OAuth. It reads Gmail messages, classifies them into actions such as ignore, reply, and suspicious, applies managed labels, and creates reply drafts. Its README explicitly emphasizes draft-only safety and no automatic sending.

The visible source includes:

- `app/agents.py` with OpenAI SDK `Agent` builders and Pydantic output for triage.
- `app/gmail_client.py` for Gmail API access.
- `app/tools.py` for agent-callable wrappers.
- `app/workflows.py` for message processing, label application, fallback behavior, and draft creation.

### Gmail OAuth Pattern

The template uses a desktop OAuth flow with local `credentials.json` and `token.json` files. The Gmail client loads an existing token, refreshes it when possible, or starts a local browser consent flow when no valid token exists. Keystone should keep this as a future live-integration pattern only; fixture mode must not require OAuth files.

### Unread Email Triage Pattern

The template reads inbox messages with `UNREAD` filtering by default and supports limited backfill options. Each message is passed to a triage agent that returns an action such as ignore, reply, or suspicious plus a topic category, confidence, suspicious signals, and a short reason.

### Label Recommendation Pattern

The template treats topic labels as a managed exclusive set and uses an `Action Required` overlay for messages that need replies. Keystone adapts this as recommended labels in fixture output, not live label writes.

### Gmail Draft Creation Pattern

The template builds a reply in the original thread by reading the source message, deriving a `Re:` subject, preserving reply headers when available, and creating a Gmail draft. Keystone fixture mode mirrors the draft intent in structured output and dry-run tools only.

### Draft-Only Safety Pattern

The template README states that the system creates drafts only and never auto-sends. Keystone keeps the same safety boundary: draft content must be marked for human approval and no tool wrapper may send email.

### No Auto-Send Behavior

No automatic send path is part of the Keystone Gmail triage implementation. If any future send function is introduced, it must be outside tests by default, gated by explicit live flags, and raise until approval, audit, and credential handling are implemented.

### Architectural Ideas To Borrow

- Treat Gmail triage as a typed SDK agent, not a simple classifier function.
- Use a Pydantic triage result with action, category, confidence, suspicious signals, and rationale.
- Keep label names configurable.
- Apply one managed category label plus an action-required overlay label.
- Default failed or invalid triage to manual review.
- Keep reply generation separate from triage.
- Create Gmail drafts only, never send automatically.
- Use OAuth credentials outside committed source.

### What Should Not Be Copied

- Do not inline large prompt strings in Python files. Keystone prompts must live in markdown files.
- Do not copy labels, categories, or productivity-specific heuristics without adapting to Keystone business workflows.
- Do not allow the draft agent to decide when to save a draft without a Keystone approval and safety wrapper.
- Do not copy setup scripts or scheduler behavior as v1 architecture.

### Risks And Caveats

- Gmail API OAuth and token handling are easy places to accidentally commit credentials. Keystone must keep tokens outside git and fixtures.
- Any label-writing tool is still an external side effect. It must be dry-run by default and audited.
- Suspicious message handling should be conservative and should not downgrade risk without clear evidence and tests.
- Draft-only behavior must be enforced in both tool availability and guardrails.

## Mira: `DimiMikadze/mira`

Reference URL: https://github.com/DimiMikadze/mira

### What It Appears To Do

Mira is a multi-agent company research system. The README describes gathering company information from websites, LinkedIn profiles, and Google Search, then assembling structured profiles with confidence scores and source attribution. It includes a framework-agnostic core library, a Next.js frontend, workspace management, and bulk processing.

The visible repository structure includes:

- `packages/mira-ai` as the core package.
- `apps` for application surfaces.
- `supabase` for persistence.
- Source selection, confidence scoring, source attribution, criteria matching, outreach generation, progress events, and bulk processing features described in the README.

### Architectural Ideas To Borrow

- Multi-source research with configurable source selection.
- Fact-level source attribution.
- Confidence scoring per fact and per profile.
- Structured company profiles rather than free-form reports only.
- Early termination once enough confidence has been reached.
- Criteria matching and fit scoring with explicit rationale.
- Progress and audit events that can later support a UI.

### Business Research Analyst Patterns For Keystone

- Multi-agent company research architecture: Mira separates discovery, internal page review, LinkedIn research, Google Search, and company analysis. Keystone keeps v1 smaller with one Business Research Analyst and explicit tools that map to those responsibilities while also supporting institutes, conferences, topics, Zotero collections, and article collections.
- Website, LinkedIn, Google Search source pattern: Mira treats the landing page as a baseline source, then optionally adds internal website pages, LinkedIn/profile data, and Google Search for gaps. Keystone mirrors this with source-attributed fixture loaders, `SearchProvider` search, and live-gated selected-page website extraction. Search remains dry-run by default, with explicit live opt-in for company research and opportunity scouting through SearXNG, Serper, or Firecrawl.
- Structured company profile pattern: Mira assembles enriched company records rather than a prose-only report. Keystone returns `CompanyProfile` with source records, evidence, fit summary, missing information, and domain scores.
- Source attribution: Mira emphasizes source attribution for each data point. Keystone requires every fixture-mode profile source to include a title, source type, URL or fixture reference, supported claims, and confidence.
- Confidence scoring: Mira uses confidence thresholds to merge facts and stop early. Keystone v1 computes a profile-level `confidence_score` from number of sources, evidence count, and supplied website/profile metadata.
- Fit scoring: Mira supports criteria matching with numeric fit scores and reasoning. Keystone adapts this to behavioral health relevance, clinical AI relevance, CNS/neuro relevance, evidence generation need, outside consulting likelihood, and overall consulting fit.
- Bulk processing or CLI patterns: Mira includes a bulk processor with resume and export behavior. Keystone v1 only adds `scripts/run_company_research.py`; resumable bulk processing should wait until the single-company fixture contract is stable.

### What Should Not Be Copied

- Do not port the TypeScript monorepo shape into Keystone unless needed.
- Do not copy frontend, workspace, Supabase, or bulk-processing complexity for v1.
- Do not copy any scraping assumptions without reviewing legality, terms, rate limits, and source policy.
- Do not treat LinkedIn or search data as authoritative without source-specific confidence and caveats.

### Risks And Caveats

- Multi-source research can create false confidence if sources repeat the same unsupported claim.
- Scraping and LinkedIn access have compliance and reliability risks.
- Source attribution must capture enough detail to audit facts later, not just a URL.
- Keystone must avoid unsupported claims about its own prior experience even when source context suggests a persuasive outreach angle.

## Lead Intelligence Platform: `harshalsp0011/Lead-Intelligence-Platform`

Reference URL: https://github.com/harshalsp0011/Lead-Intelligence-Platform

### What It Appears To Do

This is a multi-agent lead intelligence and outreach platform for utility cost-reduction consulting. The README describes Scout, Analyst, Writer, Outreach, Tracker, and Orchestrator components, a FastAPI backend, a React dashboard, PostgreSQL persistence, external search and enrichment APIs, scoring, email drafting, human approval checkpoints, SendGrid sending, and follow-up scheduling.

The README emphasizes two human checkpoints: approval after scoring and approval after drafting. It also describes an operational pipeline where Scout discovers companies, Analyst enriches and scores them, Writer drafts emails with a critic loop, and humans review drafts before sending.

### Architectural Ideas To Borrow

- Separate discovery, analysis, scoring, and writing responsibilities.
- Persist pipeline state across agent stages.
- Score opportunities with both numeric score and narrative rationale.
- Require human approval after scoring before writing.
- Require human approval after drafting before any outbound action.
- Keep an audit trail for pipeline transitions.
- Use a review queue model for outbound drafts.
- Consider a future critic or rewrite loop for outreach quality.

### What Should Not Be Copied

- Do not copy the utility cost-reduction domain logic, scoring rubric, SendGrid send path, follow-up scheduling, sender signatures, or dashboard scope.
- Do not implement automatic sending in Keystone v1.
- Do not require PostgreSQL, Docker, or a frontend before the core agent contracts are stable.
- Do not use external enrichment providers by default.

### Risks And Caveats

- Its full system includes outbound sending and scheduled follow-ups, which conflict with Keystone v1 safety constraints.
- Many external providers increase setup burden, cost, and secret-management risk.
- Human approval can become ambiguous unless represented as explicit persisted state.
- A writer or critic loop can still produce unsupported claims if source constraints are not enforced by schemas and guardrails.

## Sales Outreach Automation LangGraph: `kaymen99/sales-outreach-automation-langgraph`

Reference URL: https://github.com/kaymen99/sales-outreach-automation-langgraph

### What It Appears To Do

This is a Python LangGraph workflow for sales outreach automation. The README describes connecting to CRMs such as HubSpot, Airtable, and Google Sheets; researching leads through LinkedIn, websites, news, and social media; generating reports; qualifying leads; creating personalized emails and interview scripts; saving reports to Google Docs; and updating CRM records.

The visible source includes:

- `src/graph.py` defining a `StateGraph` workflow.
- `src/nodes.py` with node implementations.
- `src/state.py` with Pydantic and typed state models.
- `src/structured_outputs.py` with structured output models for website data and email response.
- `src/prompts.py` and `src/tools/` for prompts and integrations.

### Architectural Ideas To Borrow

- Treat lead research, qualification, outreach material creation, and CRM update as separate workflow nodes.
- Use typed state that carries research outputs forward.
- Branch on qualification before composing outreach.
- Generate outreach from researched pain points and CRM context.
- Keep prompts and structured outputs separate from graph wiring.
- Design an orchestration layer that could later be replaced or expanded with LangGraph.

### What Should Not Be Copied

- Do not use LangGraph in Keystone v1. The OpenAI Agents SDK must be the core agent layer.
- Do not copy the sample agency profile, marketing claims, case study structure, or CRM-specific field assumptions.
- Do not create Google Docs reports or update live CRM records by default.
- Do not copy LinkedIn scraping or API assumptions without provider review.

### Risks And Caveats

- The workflow appears automation-heavy and can blur the boundary between drafting and acting.
- CRM writes and Google Docs writes are external side effects that need dry-run defaults and audit logs.
- Generated outreach can overstate fit unless source-backed claims are required.
- LangGraph is useful for later resumable workflows, but adding it too early would complicate the initial OpenAI Agents SDK contract.

### Detailed Outreach Composer Notes For Fixture Mode

Follow-up inspection source URLs:

- https://github.com/kaymen99/sales-outreach-automation-langgraph
- https://github.com/kaymen99/sales-outreach-automation-langgraph/blob/main/docs/system-workflow.md
- https://github.com/kaymen99/sales-outreach-automation-langgraph/blob/main/src/nodes.py
- https://github.com/kaymen99/sales-outreach-automation-langgraph/blob/main/src/tools/leads_loader/lead_loader_base.py

Lead research and qualification flow:

- The workflow starts by fetching leads from a CRM, then processes one current lead through LinkedIn enrichment, company website review, blog/social/news analysis, consolidated digital presence reporting, global lead research reporting, scoring, and a qualification branch.
- Qualified leads continue into outreach material generation. Unqualified leads skip outreach and proceed to report persistence and CRM update.
- Keystone should borrow the staged shape, but fixture mode must stop at deterministic company/opportunity context and must not scrape LinkedIn, search the web, call Gmail, or invoke an LLM.

CRM context patterns:

- The template uses a loader abstraction with `fetch_records` and `update_record`, plus lead statuses such as `NEW`, `UNQUALIFIED`, and `ATTEMPTED_TO_CONTACT`.
- Airtable and Google Sheets are treated as interchangeable CRM sources behind that loader shape.
- Keystone should represent CRM context as explicit input fields and fixture records first. Later live CRM support should use a separate adapter and approval/audit layer rather than being embedded in the outreach composer.

Personalized outreach generation:

- The template generates outreach from accumulated lead and company reports, an outreach report link, and case-study retrieval. It also prepares personalized email and interview-script artifacts.
- Keystone should use the same idea of research-backed personalization, but it must not reference case studies, prior results, prior client work, or report links unless those facts are explicitly supplied and source-backed.
- Keystone fixture drafts should be concise, professional, and physician-scientist-oriented instead of generic SaaS sales copy.

Report generation:

- The template creates separate reports for general lead research, digital presence, global lead analysis, custom outreach, personalized email, and interview scripts, then can save locally and to Google Docs.
- Keystone should not create Google Docs or update CRM records in fixture mode. The useful pattern is to keep enough rationale and facts-used metadata on `OutreachDraft` so a later runbook or UI can audit why the draft was produced.

LangGraph step management:

- The template wires named nodes through a `StateGraph`, with sequential edges for enrichment and report generation, parallel-style fan-out for company information collection, and conditional edges after scoring.
- It loops over remaining leads until none remain, and the graph state carries lead data, company data, reports, scores, links, and counters across nodes.
- Keystone should not implement LangGraph yet. For now, the OpenAI Agents SDK builder owns the agent contract, and fixture-mode helper functions provide deterministic local behavior. LangGraph can be revisited once the SDK agent contracts, schemas, approvals, and storage events are stable.

## Cross-Reference Patterns For Keystone

Patterns to adopt:

- One clear agent boundary per business capability.
- Structured Pydantic outputs for every agent.
- Prompt files separated from code.
- Function-tool wrappers for every provider action.
- Dry-run fixture mode as the default.
- Source attribution and confidence scoring for research and opportunity claims.
- Human approval gates before drafting and before any future outbound send.
- Persistence and audit events for every agent run and tool intent.
- Explicit safety guardrails for PHI, professional advice, unsupported claims, secrets, outbound drafts, and no auto-send behavior.

Patterns to avoid:

- Hard-coded helper scripts in place of SDK agents.
- Inline production prompts as Python string literals.
- Live integrations in tests.
- Any automatic external email sending.
- Broad provider integration scope before dry-run contracts are proven.
- Copying domain claims, scoring rubrics, or sample customer proof points from reference projects.
- Adding LangGraph before the v1 SDK agents and contracts are tested.

## Prompt And Instruction Patterns For Keystone Prompts

This prompt pass re-inspected the reference repositories through public GitHub pages on 2026-04-21. The prompts below are pattern adaptations only; no reference prompt text or domain-specific claims were copied.

### Gmail Triage Pattern

Source: https://github.com/DanielJD1216/Email-Inbox-Agent---Doo-Made

Observed instruction pattern:

- Classify each message into a small action set such as ignore, reply, or suspicious.
- Assign a topic or workflow label alongside the action.
- Include confidence, suspicious signals, and a concise rationale.
- Treat draft creation as separate from triage.
- Keep Gmail safety explicit: drafts only, never automatic sending.

Keystone prompt adaptation:

- `gmail_triage.md` classifies inbound mail, recommends labels, decides whether a reply is needed, and flags finance, legal, contract, PHI, patient-specific, and suspicious content.
- Any reply path is draft-only and approval-gated.

### Researcher / Company Research Pattern

Source: https://github.com/DimiMikadze/mira

Observed instruction pattern:

- Use multiple configurable sources when available.
- Produce structured company profiles rather than free-form notes only.
- Attach source attribution to material facts.
- Score fact and profile confidence.
- Make gaps explicit instead of filling missing facts.

Keystone prompt adaptation:

- `business_research_analyst.md` requires source attribution, confidence scoring, Keystone fit scoring, outside consulting likelihood scoring, and explicit unknowns.
- The prompt forbids hallucinating missing facts or claiming Keystone prior experience without input support.

### Opportunity Pipeline Pattern

Source: https://github.com/harshalsp0011/Lead-Intelligence-Platform

Observed instruction pattern:

- Separate Scout, Analyst, and Writer responsibilities.
- Scout discovers candidates and signals.
- Analyst enriches, scores, and explains fit.
- Writer drafts only after approval.
- Human checkpoints are represented before drafting and before outbound action.

Keystone prompt adaptation:

- `opportunity_scout.md` uses Scout and Analyst responsibilities but blocks Writer drafting until human approval.
- It prioritizes signals relevant to external consulting or advisory support, including funding, hiring, partnerships, validation, trials, outcomes, payer partnerships, conferences, and publications.

### Lead Intelligence Platform Details For Fixture Opportunity Scout

Source: https://github.com/harshalsp0011/Lead-Intelligence-Platform

Observed pattern:

- Scout discovers companies from external signals, then deduplicates and stores candidate companies.
- Analyst enriches each candidate with company and contact context, computes a 0-100 score, and produces a plain-English score rationale.
- Writer reads approved company context and score rationale only after a human checkpoint.
- The reference pipeline tracks stages, agent runs, and tool-call logs so discovery, scoring, drafting, and later outbound action are auditable.
- Human approval appears twice in the reference flow: once after scoring before writing, and once after writing before sending.

Keystone adaptation:

- `run_opportunity_scout.py` and `scout_opportunities_fixture()` implement only Scout plus light Analyst behavior in fixture mode.
- The scout searches fixture opportunity signals, scores the records deterministically, records sources, and marks high-priority records for Business Research Analyst handoff.
- Optional live SearchProvider search follows the same Scout plus Analyst boundary: targeted queries discover candidate companies, local extraction and deduplication normalize them, local scoring ranks them, and high-priority records are marked for Business Research Analyst handoff.
- High-priority means source-backed signals are strong enough for business research, not outreach.
- No Writer behavior, outreach copy, sending, follow-up scheduling, contact enrichment waterfall, or live provider calls are included in this phase.
- Persistence is represented by `save_opportunity_placeholder`, which returns a dry-run audit result. A later real store should persist candidate, score, source, handoff, approval, and run-log events before adding any live workflow.

### Outreach Personalization Pattern

Source: https://github.com/kaymen99/sales-outreach-automation-langgraph

Observed instruction pattern:

- Research and CRM context feed qualification and outreach preparation.
- Outreach is personalized from researched pain points, company context, and prior analysis.
- The workflow separates lead research, qualification, and outreach material generation.
- CRM context and generated reports can inform message content.

Keystone prompt adaptation:

- `outreach_composer.md` uses only approved company, opportunity, CRM, and source-attributed context.
- It requires concise, research-backed personalization, a non-salesy physician-scientist tone, no unsupported claims, no em dashes, cold email under 180 words, LinkedIn note under 300 characters, and approval-gated draft-only output.
