# Keystone Business Agents Implementation Plan

## Project Purpose

`keystone-business-agents` will provide four internal business agents for Keystone Neuroinformatics LLC. The system should help triage inbound email, research accounts and companies, scout opportunities, and compose draft outreach while preserving strict safety boundaries.

The core agent layer for version 1 must be the OpenAI Agents SDK. The project must not be implemented as a set of hard-coded helper scripts. Each business capability gets a first-class SDK `Agent`, prompt markdown, tool wrappers, structured Pydantic outputs, guardrails, dry-run fixture support, and tests.

Default behavior is dry-run and draft-only. Live integrations are explicitly opt-in and must never send external email without human approval.

## Version 1 Scope

Version 1 should implement the four agents as callable library components with a thin orchestrator. The initial surface can be CLI or test harness driven, but the internal architecture must already support later API, dashboard, scheduler, and workflow automation layers.

Required agents:

1. Gmail Inbound Triage Agent
2. Business Research Analyst
3. Opportunity Scout Agent
4. Outreach Composer Agent

Required safety defaults:

- Never send external email automatically.
- Create drafts only unless a separate, explicit human approval workflow is added later.
- Block PHI and patient-specific data processing.
- Do not provide medical, legal, tax, or regulatory advice.
- Do not claim Keystone prior work, outcomes, customers, credentials, or case studies unless present in `prompts/keystone_profile.md` or explicitly supplied input.
- Do not use em dashes in outbound copy.
- Keep secrets out of code, docs, tests, fixtures, and logs.
- Keep all live integrations behind explicit flags.
- Make dry-run fixture mode the default.
- Ensure tests require no live API keys and no network calls.

## Four-Agent Architecture

### 1. Gmail Inbound Triage Agent

Builder:

```python
def build_gmail_triage_agent() -> Agent[GmailTriageResult]:
    ...
```

Responsibilities:

- Classify inbound Gmail messages into business-relevant categories.
- Identify whether a response is needed, whether the message is suspicious, and whether it should be ignored.
- Recommend label changes through tool wrappers.
- Produce a draft request for the Outreach Composer when a response is appropriate.
- Never send an email.

Primary inputs:

- Message metadata: sender, recipient, subject, labels, thread id, received timestamp.
- Sanitized message body and snippets.
- Existing Keystone profile context, if needed.

Primary output schema:

- `GmailTriageResult`
  - `message_id`
  - `thread_id`
  - `classification`
  - `recommended_labels`
  - `requires_response`
  - `risk_level`
  - `suspicious_signals`
  - `contains_phi_or_patient_data`
  - `recommended_next_agent`
  - `rationale`
  - `audit_notes`

Borrowed reference patterns:

- From the Email Inbox Agent reference: unread message filtering, exclusive managed labels, overlay action-required label, OAuth-backed Gmail access, failure fallback to manual review, and draft-only reply creation.

Keystone-specific changes:

- Instructions must live in `prompts/gmail_triage.md`.
- Gmail actions must be explicit wrappers, not direct Gmail SDK calls inside agent code.
- The outbound path can only request a draft through the Outreach Composer, never send.

### 2. Business Research Analyst

Builder:

```python
def build_business_research_analyst_agent() -> Agent[CompanyResearchProfile]:
    ...
```

Responsibilities:

- Build a structured profile for a company or account and broader source-backed
  briefs for institutes, conferences, labs, topics, Zotero collections, and
  article collections.
- Use configurable sources: provided CRM context, company website, public web search, user-supplied notes, and dry-run fixtures.
- Attribute sources for each material fact.
- Assign confidence scores to facts and to the overall profile.
- Identify unsupported or contradictory claims.

Primary inputs:

- Company name, domain, CRM record id, contact context, and optional research brief.
- Allowed source configuration.

Primary output schema:

- `CompanyResearchProfile`
  - `company_name`
  - `domain`
  - `industry`
  - `business_summary`
  - `neuroinformatics_relevance`
  - `signals`
  - `open_questions`
  - `facts`
  - `sources`
  - `confidence_score`
  - `research_limitations`
  - `audit_notes`

Borrowed reference patterns:

- From Mira: multi-source research, configurable sources, structured profiles, fact-level confidence, source attribution, and stopping once sufficient confidence is reached.

Keystone-specific changes:

- The agent must not infer clinical claims, regulatory posture, or prior Keystone experience without cited source input.
- Source records should preserve retrieval metadata, not just URLs.

### 3. Opportunity Scout Agent

Builder:

```python
def build_opportunity_scout_agent() -> Agent[OpportunityScoutResult]:
    ...
```

Responsibilities:

- Discover and rank potential business opportunities from research profiles, inbound signals, CRM context, and configured scout sources.
- Produce scored opportunities with rationale and source traceability.
- Route promising opportunities to the Outreach Composer only after human approval is represented in state.
- Persist candidate, score, decision, and audit events.

Primary inputs:

- Company research profiles.
- Inbound triage results.
- User-supplied ICP criteria.
- Dry-run opportunity fixtures.

Primary output schema:

- `OpportunityScoutResult`
  - `opportunities`
  - `score_summary`
  - `approval_required`
  - `recommended_follow_up`
  - `disqualification_reasons`
  - `sources`
  - `audit_notes`

Borrowed reference patterns:

- From Lead Intelligence Platform: Scout, Analyst, Writer separation; 0-100 scoring with narrative rationale; database-backed pipeline state; human approval after scoring and before outbound communication; audit-oriented flow.

Keystone-specific changes:

- Version 1 should not auto-run scheduled searches.
- Opportunity state should distinguish `candidate`, `researched`, `approved_for_drafting`, `drafted`, `rejected`, and `archived`.
- No send path belongs in v1.

### 4. Outreach Composer Agent

Builder:

```python
def build_outreach_composer_agent() -> Agent[OutreachDraftResult]:
    ...
```

Responsibilities:

- Compose draft-only email copy from approved context.
- Personalize using business research, opportunity rationale, CRM notes, and Keystone profile facts.
- Produce subject lines and body copy in structured output.
- Flag any unsupported claim or risky content instead of writing around it.
- Enforce no em dashes in generated outbound copy.

Primary inputs:

- Approved opportunity record.
- Company profile.
- Contact context.
- Keystone profile.
- Requested tone, objective, and constraints.

Primary output schema:

- `OutreachDraftResult`
  - `subject`
  - `body`
  - `personalization_points`
  - `claims_used`
  - `unsupported_claims_blocked`
  - `requires_human_approval`
  - `draft_status`
  - `audit_notes`

Borrowed reference patterns:

- From the LangGraph outreach reference: lead research feeding qualification, qualification feeding personalized outreach, CRM context, report-backed personalization, and separate structured outputs for email content.
- From Lead Intelligence Platform: critic or review loop can be added later, but v1 should keep a simpler deterministic safety review after drafting.

Keystone-specific changes:

- The composer must never call a send tool.
- Gmail integration can create a Gmail draft only through a draft wrapper.
- The default output status is `draft_pending_approval`.

## OpenAI Agents SDK Requirements

Each agent module must expose exactly one builder for its primary agent:

- `build_gmail_triage_agent()`
- `build_business_research_analyst_agent()`
- `build_opportunity_scout_agent()`
- `build_outreach_composer_agent()`

Each builder must:

- Return an OpenAI Agents SDK `Agent` object.
- Load instructions from a markdown prompt file.
- Set a Pydantic `output_type`.
- Attach only allowed function tools for that agent.
- Attach safety guardrails appropriate to the agent.
- Use dry-run fixture tools by default.

SDK usage expectations:

- Use `Agent` as the business capability boundary.
- Use `function_tool` wrappers for custom external actions and data access.
- Use agent output schemas for typed final results.
- Use input guardrails for PHI, patient-specific content, forbidden advice requests, and secrets.
- Use output guardrails for unsupported claims, outbound safety, em dash checks, and auto-send prevention.
- Use tool guardrails around external action wrappers, especially Gmail, CRM, web search, and storage writes.
- Use handoffs only for explicit transfers between the four agents or through an orchestrator.

## Optional LangGraph Orchestration Layer

LangGraph is implemented only as an optional WorkItem graph runtime. It is not
part of the core dependency set and does not replace OpenAI Agents SDK agent
builders, prompts, schemas, tools, WorkItems, context packs, or Python safety
gates.

Version 1 orchestration remains a small Python graph/runtime module that:

- Runs agents in a deterministic order.
- Exposes graph-native WorkItem nodes for opted-in LangGraph execution.
- Passes structured outputs between agents.
- Persists audit events.
- Stops at human approval gates.
- Never sends external communication.

Future LangGraph expansion candidates:

- Inbound email to triage to draft workflow.
- Business research waterfall.
- Opportunity discovery to scoring to approval queue.
- Outreach draft to review to CRM update workflow.

## Shared Tool Layer

Tools should live behind explicit wrappers under `src/keystone_agents/tools/`. Agent code should not directly import provider SDK clients.

Recommended tool groups:

- `tools/gmail.py`
  - `fetch_inbound_messages`
  - `get_message_details`
  - `apply_gmail_labels`
  - `create_gmail_draft`
  - No `send_email` wrapper in v1.
- `tools/research.py`
  - `search_public_web`
  - `fetch_company_site_summary`
  - `read_fixture_company_profile`
  - `extract_source_facts`
- `tools/crm.py`
  - `lookup_account_context`
  - `lookup_contact_context`
  - `write_crm_note_dry_run`
- `tools/storage.py`
  - `save_agent_run`
  - `save_audit_event`
  - `save_draft_record`
  - `load_fixture`
- `tools/approval.py`
  - `record_human_approval`
  - `get_approval_status`

Tool requirements:

- Every external integration has a dry-run implementation.
- Live implementations require both a global live flag and a provider-specific flag.
- Tool outputs are structured Pydantic models or simple serializable DTOs.
- Tool wrappers must redact secrets before logging.
- Tool wrappers must record audit metadata for action intent, dry-run status, provider, and correlation ids.

## Schema Layer

Schemas should live under `src/keystone_agents/schemas/`.

Core schema files:

- `schemas/common.py`
  - `SourceReference`
  - `EvidenceFact`
  - `ConfidenceScore`
  - `RiskLevel`
  - `ApprovalStatus`
  - `AgentAuditNote`
- `schemas/gmail.py`
  - `InboundEmail`
  - `GmailTriageResult`
  - `GmailLabelRecommendation`
- `schemas/company.py`
  - `CompanyResearchProfile`
  - `CompanySignal`
  - `ResearchSourceSet`
- `schemas/opportunity.py`
  - `OpportunityCandidate`
  - `OpportunityScore`
  - `OpportunityScoutResult`
- `schemas/outreach.py`
  - `OutboundClaim`
  - `OutreachDraftResult`
  - `DraftSafetyReview`

Schema requirements:

- Use Pydantic validation for enums, confidence ranges, required source attribution, and approval states.
- Include `requires_human_approval` or equivalent on any schema that can lead to outbound copy.
- Include `dry_run` and `source_ids` where actions or claims are material.
- Reject invalid outbound text that includes em dashes.
- Keep schema behavior independently testable without SDK calls.

## Prompt Layer

Prompts should live under `src/keystone_agents/prompts/`.

Required prompt files:

- `keystone_profile.md`
- `gmail_triage.md`
- `business_research_analyst.md`
- `opportunity_scout.md`
- `outreach_composer.md`
- `shared_safety.md`

Prompt requirements:

- Prompts are loaded at build time through a prompt loader.
- Agent builders should compose `shared_safety.md`, `keystone_profile.md` where relevant, and the agent-specific prompt.
- Prompt files must not contain secrets, real access tokens, private customer details, PHI, or unverified Keystone claims.
- The Outreach Composer prompt must require draft-only output, human approval, no em dashes, and claim traceability.

## Guardrail Layer

Guardrails should live under `src/keystone_agents/guardrails/`.

Required guardrails:

- PHI and patient-specific data guardrail.
- Professional advice guardrail for medical, legal, tax, and regulatory advice.
- Secrets guardrail for inputs, tool arguments, tool outputs, logs, and final outputs.
- Unsupported Keystone claims guardrail.
- Outbound safety guardrail requiring draft-only and human approval.
- No auto-send guardrail that blocks any send-like tool invocation or schema status.
- No em dash guardrail for outbound copy.

Guardrail behavior:

- If PHI or patient-specific data is detected, stop and return a safe refusal or escalation result.
- If professional advice is requested, refuse advice and optionally suggest human expert review.
- If a claim lacks an allowed source, remove or block the claim and record it in `unsupported_claims_blocked`.
- If any outbound result is not marked draft-only and pending approval, trip the guardrail.

## Handoff And Orchestrator Plan

The v1 orchestrator should live in `src/keystone_agents/agents/orchestrator.py`.

Recommended handoffs:

- Gmail Inbound Triage can request Business Research Analyst when an inbound message references an unknown organization or broader research target.
- Gmail Inbound Triage can request Outreach Composer only when `requires_response=True` and no safety guardrail trips.
- Business Research Analyst can pass profiles to Opportunity Scout.
- Opportunity Scout can pass approved opportunities to Outreach Composer only when an approval record exists.

Workflow examples:

- Inbound triage:
  1. Load fixture or Gmail message.
  2. Run Gmail Inbound Triage Agent.
  3. Apply dry-run label recommendations.
  4. Stop if ignored, suspicious, PHI, or advice blocked.
  5. Create a draft request for Outreach Composer when response is needed.
  6. Save audit event.

- Account opportunity:
  1. Load account context.
  2. Run Business Research Analyst.
  3. Run Opportunity Scout Agent.
  4. Stop at approval gate.
  5. After approval, run Outreach Composer Agent.
  6. Create draft record only.

## Dry-Run And Fixture Testing Plan

Dry-run mode is the default. Tests must not call live provider APIs, require real OAuth, require network access, or require OpenAI API keys.

Fixture layout:

- `fixtures/gmail/`
  - inbound email samples, label maps, suspicious messages, PHI-like blocked examples.
- `fixtures/company/`
  - company profiles, public-source snippets, source attribution examples.
- `fixtures/opportunity/`
  - scored candidates, rejected candidates, approval records.
- `fixtures/outreach/`
  - approved contexts, draft examples, unsafe draft examples.

Required tests:

- Architecture tests:
  - Each builder exists.
  - Each builder returns an SDK `Agent`.
  - Each builder loads markdown instructions.
  - Each builder has structured output type.
  - Each builder has only allowed tools.
- Schema tests:
  - Confidence ranges are enforced.
  - Source attribution is required where expected.
  - Approval state is required before outbound draft creation.
  - Em dashes are rejected in outbound copy.
- Safety tests:
  - PHI and patient-specific input trips guardrail.
  - Advice requests are blocked.
  - Unsupported Keystone claims are blocked.
  - Send-like actions are unavailable or blocked.
- Dry-run tests:
  - Dry-run is default when no flags are set.
  - Fixture tools return deterministic results.
  - No test requires live API keys.
  - No test performs network calls.
- No auto-send tests:
  - No production `send_email` tool exists in v1.
  - Gmail draft creation remains draft-only.
  - Outreach outputs require human approval.

## Storage And Audit Logging Plan

Version 1 should use a simple local persistence adapter that can later be swapped for Postgres.

Recommended storage:

- SQLite or JSONL for local dry-run development.
- In-memory or `tmp_path` storage for tests.
- No committed runtime databases.

Audit event fields:

- `event_id`
- `run_id`
- `agent_name`
- `action`
- `dry_run`
- `live_integration_enabled`
- `input_ref`
- `output_ref`
- `approval_status`
- `tool_name`
- `provider`
- `source_ids`
- `safety_flags`
- `created_at`

Audit requirements:

- Record every agent run.
- Record every tool intent, including dry-run tool intents.
- Record every approval decision.
- Record every draft creation request.
- Never log secrets, raw tokens, PHI, or full sensitive email bodies.

## Live Integration Plan

Live integrations are intentionally not part of the first documentation-only step and should remain disabled by default.

Required flags before any live integration can run:

- `KEYSTONE_DRY_RUN=false`
- `KEYSTONE_ENABLE_LIVE_INTEGRATIONS=true`
- Provider-specific flag, such as `KEYSTONE_ENABLE_GMAIL=true`
- Valid credentials supplied through environment or approved secret manager

Live integration gates:

- Gmail OAuth requires user-owned credentials and token storage outside git.
- Web search requires explicit source configuration and rate limits.
- CRM integrations require read/write scope separation.
- Storage migrations require a reviewed schema.
- Outbound sending remains out of scope for v1.

## Phased Implementation Roadmap

### Phase 0: Documentation And Repository Skeleton

- Add implementation plan and template notes.
- Add package structure only if needed later.
- Do not implement production code yet.
- Do not run live integrations.

### Phase 1: Core Contracts

- Add Pydantic schemas.
- Add prompt loader.
- Add placeholder prompt markdown files.
- Add settings with dry-run default.
- Add guardrail modules.
- Add fixture loader.
- Add tests for schema and guardrail behavior.

### Phase 2: Agent Builders

- Implement the four `build_*_agent()` functions.
- Attach prompt markdown, output schemas, tools, and guardrails.
- Use fixture-backed tools by default.
- Add architecture tests proving each builder returns an SDK `Agent`.

### Phase 3: Dry-Run Tool Layer

- Implement fixture Gmail, company research, CRM, storage, and approval tools.
- Add deterministic fixtures.
- Add no-network tests.
- Add no-auto-send tests.

### Phase 4: Orchestrator

- Implement deterministic v1 orchestrator.
- Persist audit events.
- Stop at approval gates.
- Add integration-style dry-run tests over the full four-agent flow.

### Phase 5: Optional Live Read Integrations

- Add live Gmail read and label wrappers behind flags.
- Add live public research wrappers behind flags.
- Keep write operations dry-run unless explicitly enabled and reviewed.
- Add contract tests with mocked provider clients.

### Phase 6: Draft Creation Integration

- Add live Gmail draft creation behind flags.
- Preserve human approval requirement.
- Add audit logging for draft creation.
- Do not add email sending.

### Phase 7: Future Orchestration And UI

- Evaluate LangGraph for resumable, long-running workflows.
- Add dashboard or API approval queue if needed.
- Add Postgres persistence if local storage becomes limiting.

## What Should Not Be Implemented Yet

- No production code in the current documentation task.
- No external email sending.
- No SendGrid, HubSpot send path, Gmail send path, or scheduler-based outbound messaging.
- No live API calls in tests.
- No committed OAuth tokens, `.env` files, API keys, credentials, or sample secrets.
- No autonomous processing of PHI or patient-specific records.
- No medical, legal, tax, or regulatory advice generation.
- No unsupported Keystone claims, case studies, customer references, or outcomes.
- No graph-native rewrite of SDK specialists.
- No browser dashboard until the core contracts, tests, and dry-run flows are stable.
- No broad web scraping system until source policy, rate limits, robots considerations, and attribution behavior are reviewed.

## Reference Basis

This plan was informed by public GitHub pages for the four template repositories and OpenAI Agents SDK documentation for agents, guardrails, tools, and handoffs. The templates are architectural references only and should not be copied directly.
