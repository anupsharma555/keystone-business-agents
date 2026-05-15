# Slack Message Actions

Keystone supports a Slack message shortcut named `Run Keystone Agent`.

The Slack runtime should route `message_action` payloads with callback id
`keystone_run_agent_message` to:

```bash
.venv/bin/python scripts/handle_slack_agent_action.py --payload-file payload.json --json
```

That command writes a bounded selected-message context file under
`artifacts/slack_contexts/` and returns a Slack modal view. On modal
submission, route the `view_submission` payload back to the same script. The
handler starts a local WorkItem with `WorkflowRunRequest.context_file_path`.

Manifest fragment:

```yaml
features:
  shortcuts:
    - name: Run Keystone Agent
      type: message
      callback_id: keystone_run_agent_message
      description: Run a Keystone business agent using this message as context.
settings:
  interactivity:
    is_enabled: true
```

Only the selected message and explicitly supplied thread replies are stored.
If thread retrieval fails, the WorkItem still starts with selected-message
metadata and records a warning. Slack actions do not approve, send, post,
schedule, or publish anything.
