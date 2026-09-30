# Optional KBA integration

Read only when connecting a model to KBA is requested. An Ollama installation,
API probe, Codex picker entry, or GUI profile does not establish agent compatibility.
Complete a bounded standalone result first and carry its limitations forward.

Choose the integration boundary from the complementary role: a conversational
model uses the provider/agent path below; an embedding or reranking model belongs
in the retrieval tool boundary and requires retrieval-specific tests. Neither
role automatically replaces the current model or enables automatic selection.

1. Read current provider helpers, builders, registry, and runtime policy. Apply
   `codex-skills/kba-agent-contract-change/SKILL.md` for contract changes. Preserve
   Orchestrator-first execution, specialist ownership, review, sessions, schemas,
   and Python permission gates.
   Start inspection at `src/keystone_agents/model_provider.py`,
   `src/keystone_agents/adapters/providers.py`, and
   `src/keystone_agents/agent_registry.py`; follow current imports to the owning
   implementation and focused tests. Discover accepted provider values, endpoint
   handling and credential checks from code; do not assume that `ollama` is already
   a supported provider identifier or invent an environment variable.
2. Probe the installed endpoint for text, schema output, one synthetic function
   call round trip, and follow-up continuity. Native Ollama chat and its compatible
   API are separate surfaces; neither requires a hosted OpenAI call. Do not infer
   SDK compatibility from a simple HTTP success.
3. Reuse the provider boundary or add a minimal explicit local profile. Model choice
   remains separate from agent/skill identity. Carry the selected local provider
   through Orchestrator, specialists, and required review stages. Verify unsupported
   capabilities fail clearly without a cloud call. Preserve production defaults
   unless changing them is explicitly requested.
4. Test contracts with mocks, then one bounded local end-to-end workflow with
   synthetic evidence. Verify structured outputs, permitted tools, follow-ups,
   no-send behavior, and actual provider identity at every stage.
5. Treat Slack rollout, automatic fallback, paid inference, and remote exposure as
   separate requested operations. Slack still needs internet. Without deployment
   scope, deliver the tested patch/specification and remaining acceptance steps.

## Optional Codex model picker

Register a model only when requested. Consult current local configuration and
product guidance; do not invent settings keys, restart active tasks, or switch the
default model. Preserve prior settings; verify the actual selector and a bounded
local answer. Selecting a model does not load this Codex skill into native Ollama.

## Acceptance

Separate endpoint, SDK, complete agent routing, GUI, offline, and deployed Slack
results. Keep missing proof pending; do not mark production integration complete
based on a download or smoke test.
