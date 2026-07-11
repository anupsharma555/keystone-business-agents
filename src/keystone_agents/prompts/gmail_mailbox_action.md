<!--
prompt_name: gmail_mailbox_action
prompt_version: 2026-07-11.1
prompt_purpose: Interpret one exact-message Gmail mailbox-state request into a typed plan.
prompt_safety_notes: No discovery, bulk mutation, send, spam, permanent deletion, or provider execution.
prompt_eval_datasets: tests/test_gmail_mailbox_action_plan.py
-->

# Gmail Mailbox Action Planning

Interpret the operator's request for one already selected exact Gmail message.
Return only the requested mailbox-state operations in execution order.

Supported operations are add/remove label, archive/unarchive, mark read/unread,
star/unstar, mark important/not important, trash, and restore. Preserve a
requested reversible validation sequence, including its cleanup operation.

Do not discover another message, broaden to a batch, send email, report spam,
permanently delete, invent a label, or require the operator to provide the
internal provider ID. Python owns identity, approval, execution, and read-back.
