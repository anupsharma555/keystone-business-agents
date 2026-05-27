# Ranked Agent Goals

This is a ranked improvement target list for Keystone Business Agents. The order prioritizes goals that most directly improve agent reasoning, execution reliability, Slack/human workflow usefulness, benchmarkability, and alignment with the current Orchestrator-first OpenAI Agents SDK architecture.

The benchmarking additions are based on current agent-evaluation guidance and public benchmark patterns: trace-graded OpenAI agent workflows, GAIA-style general assistant tasks, tau-bench-style tool-agent-user interaction, tau2-bench-style shared-control workflows, WebArena-style realistic tool environments, and production reliability metrics that go beyond pass/fail completion.

## 1. Unified Agent Control Plane

Make Orchestrator the consistent first reasoning layer for Slack, CLI, WorkItems, automations, and explicit agent mentions. It should interpret the raw request, identify the objective, route or plan the work, and pass compact guidance to specialists while Python gates remain authoritative for approvals, safety, and side effects.

## 2. Request Understanding Before Execution

Every meaningful request should be interpreted for objective, target, constraints, missing context, risk, required tools, and expected output shape before specialist execution begins. This is the foundation for better routing, fewer wrong-lane answers, and more useful blocked states.

## 3. Diverse Request Matrix Coverage

Agents should handle both broad open-ended asks and exact deterministic requests across Slack, CLI, Gmail, research, opportunity scouting, outreach, operations, and follow-up workflows. The matrix should include ambiguous wording, repeated asks, selected-thread references, side-effect requests, no-result cases, conflicting evidence, and multi-agent workflows.

## 4. Benchmark-Driven Agent Reliability

Build a Keystone-specific benchmark suite that evaluates the complete agent system: model, prompts, tools, context packs, retrieval, guardrails, handoffs, renderers, and persistence. Generic benchmarks are useful inspiration, but Keystone needs domain-specific tests for its actual business workflows and tools.

## 5. Trace-Graded Workflow Evaluation

Use trace-style grading to evaluate whether the agent selected the right tool, routed or handed off correctly, respected guardrails, preserved context, recovered from errors, and produced the right final state. This should complement output-only tests because many agent failures happen inside the workflow before the final response.

## 6. Repeatability And pass-k Reliability

Measure whether agents succeed consistently across repeated runs, not only whether one run passed. For important Slack, Gmail, research, opportunity, and outreach flows, track repeated-run stability, route variance, source variance, approval-boundary consistency, and whether a second or third attempt changes the answer without new evidence.

## 7. State-Correctness Benchmarks

For workflows that read or write local state, evaluate the final SQLite, WorkItem, approval, artifact, source, and memory state against an expected goal state. This mirrors tool-agent-user benchmark practice: the final database or workflow state matters more than whether the final prose sounds plausible.

## 8. Seamless Cross-Agent Handoffs

Research, Opportunity Scout, Outreach, Gmail, Chief of Staff, and Orchestrator should pass structured context through WorkItems, context packs, artifact refs, source refs, blockers, approval gates, and prior run summaries without losing the original user request or downstream intent.

## 9. Reliable Context Selection

Improve handling of references like "this thread," "the prior post," "that company," "run it again," and "continue" so agents bind to the correct Slack, Gmail, WorkItem, artifact, or prior-run context. If the target cannot be resolved, the system should block with a precise missing-context explanation.

## 10. Richer, Safer Context Layer

Expand context quality across Slack threads, WorkItems, prior runs, selected artifacts, approved facts, source bundles, Gmail threads, local docs, and memory. The goal is more relevant context with less stale, unrelated, or unsafe context.

## 11. Deeper User And Aim Understanding

Improve the agents' model of Keystone's priorities, business goals, operator preferences, writing style, risk tolerance, current projects, and recurring workflows. Responses should reflect what the user is trying to accomplish, not just the literal command.

## 12. Slack As The Human Work Cockpit

Make Slack responses concise, actionable, and downstream-useful. A Slack result should foreground the answer, blockers, next safe action, approval state, and useful buttons while hiding low-value workflow metadata.

## 13. Human Approval As A First-Class Workflow

Treat approvals as durable work objects. Every draft, Slack post, CRM write, Gmail draft, external-use action, or live write plan should carry scope, reviewer state, source basis, allowed action, and next step.

## 14. Agent Output Designed For Downstream Work

Every agent result should be usable by the next step. Research should produce decision-ready evidence and concerns, Scout should produce ranked handoff-ready opportunities, Outreach should produce approval-ready drafts, Gmail should produce triage and reply decisions, and Chief of Staff should produce operational plans.

## 15. Evidence-First Reasoning

Agents should separate facts, inferences, missing evidence, and recommendations. Company, opportunity, current-year, and "latest" claims should use source attribution and independent evidence when available, and should say when evidence is too thin.

## 16. Bounded Repair Loop

Strengthen Orchestrator review and repair so off-target, shallow, stale, unsafe, or weakly sourced outputs get one targeted correction before the system blocks or asks the human for input. Repair should improve quality without creating uncontrolled loops.

## 17. Error Recovery And Escalation Metrics

Track where agents fail, whether they recover, and when they ask for human intervention. Useful metrics include escalation rate, time-to-first-error, recovery success rate, blocker precision, and whether the agent stops safely when context, approval, source evidence, or tool access is insufficient.

## 18. Low-Latency Response Modes

Support clear fast and deep execution modes. Quick responses should use available context and deterministic checks; deeper runs should use broader retrieval and synthesis. When tool or time budgets run out, the system should say what was checked and what remains unresolved.

## 19. Cost And Tool-Budget Discipline

Measure token use, tool calls, retrieval breadth, elapsed time, repair count, and cost by request category. A result that is technically correct but too slow or expensive for routine Slack use should fail the practical benchmark for that workflow.

## 20. Personal Operating Memory With Boundaries

Use approved memory to capture recurring aims, preferred answer style, business focus areas, prior decisions, useful corrections, and approved examples. Memory must not override the current request, source attribution, privacy constraints, no-PHI rules, or approval gates.

## 21. Cross-Channel Continuity

Let the user start in Slack, continue in CLI, approve through a WorkItem, and later ask a follow-up without losing the thread, confusing stale context, or repeating completed work. WorkItems and audit state should remain the canonical continuity layer.

## 22. OpenAI Agents SDK Alignment

Keep the repo standardized around top-level OpenAI Agents SDK patterns: SDK `Agent` builders, tools, structured outputs, guardrails, handoffs, sessions, tracing, approval patterns, and agents-as-tools where useful. Avoid custom orchestration that duplicates SDK primitives without a clear project-specific reason.

## 23. Tool Surfaces That Match Agent Jobs

Keep tools explicit, typed, and schema-first. Add new helpers only when they improve bounded reads, writes, retrieval, source extraction, diagnostics, or review. Registry, policy, prompt, safety, and test updates should move together.

## 24. Operational Observability Without Noise

Track route, latency, repair count, retrieval diagnostics, source sufficiency, cost, final status, guardrail outcomes, and state diffs in audit logs. Human-facing Slack and CLI output should remain focused on the answer, blockers, and next action.

## 25. Feedback-To-Eval Learning Loop

Convert human corrections into structured feedback, prompt-safe memory, fixtures, evals, and documented acceptance criteria. Repeated operator complaints should become durable regression coverage before being considered solved.

## 26. SDK-Native Evaluation And Tracing

Move closer to standard OpenAI agent implementation practice by capturing trace IDs, handoff summaries, tool-call summaries, guardrail outcomes, and structured eval results in a way that supports debugging and quality review without exposing sensitive data.

## 27. Live Integration Confidence Ladder

Preserve dry-run-first development, then promote capabilities through fixture tests, local evals, live smoke tests, and approval-gated live operation. Slack, Gmail, search, Google Workspace, Airtable, and CRM should each stay independently gated.

## 28. Chief Of Staff As Workflow Integrator

Use the Chief of Staff agent to synthesize operational state across Slack, WorkItems, automations, approvals, recent agent runs, and repo context. It should own broad questions such as "what should we do next?" and "why did the system respond this way?"

## Benchmarking Resources Reviewed

- [OpenAI Agent Evals](https://developers.openai.com/api/docs/guides/agent-evals): trace grading, datasets, eval runs, and workflow-level regression checks.
- [OpenAI Trace Grading](https://developers.openai.com/api/docs/guides/trace-grading): structured grading of traces to inspect decisions, tool calls, and behavior failures.
- [OpenAI Practical Guide To Building Agents](https://openai.com/business/guides-and-resources/a-practical-guide-to-building-ai-agents/): layered guardrails, tool risk, and human intervention patterns.
- [GAIA](https://arxiv.org/abs/2311.12983): general assistant tasks requiring reasoning, tool use, web browsing, and robustness on simple-to-humans questions.
- [tau-bench](https://arxiv.org/abs/2406.12045): tool-agent-user interaction, policy following, final database-state evaluation, and repeated-run reliability.
- [tau2-bench](https://arxiv.org/abs/2506.07982): dual-control environments where both user and agent act in shared state, emphasizing coordination and communication.
- [WebArena](https://webarena.dev/): realistic web environments for evaluating autonomous web agents.
- [SWE-bench](https://www.swebench.com/): objective patch/test evaluation, cost and step comparison patterns, and benchmark-family structure.
- [Benchmarking Agents Review](https://benchmarkingagents.com/agent-benchmarks/): overview of major public agent benchmarks and cautions about benchmark-to-production gaps.
- [Agentic Academy: Benchmarking Agent Reliability](https://agentic-academy.ai/posts/benchmarking-agent-reliability/): production metrics such as cost variance, escalation rate, time-to-first-error, and recovery success.
