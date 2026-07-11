from __future__ import annotations

from types import SimpleNamespace

from keystone_agents.schemas.outreach import OutreachDraft
from scripts.run_outreach_selected_gmail_draft_lifecycle import execute_validation


class FakeGmail:
    live = True

    def __init__(self) -> None:
        self.drafts: dict[str, dict[str, object]] = {}

    def current_account_email(self):
        return "operator@example.test"

    def list_recent_messages(self, *, label, max_results, query):
        assert label == "SENT"
        assert max_results == 10
        assert "KBA_TEST_EMAIL" in query
        return [{"id": "message-test", "threadId": "thread-test"}]

    def get_thread(self, thread_id):
        assert thread_id == "thread-test"
        return {
            "subject": "KBA_TEST_EMAIL operational validation",
            "messages": [
                {
                    "id": "message-test",
                    "subject": "KBA_TEST_EMAIL operational validation",
                    "normalized_body": (
                        "KBA_TEST_EMAIL synthetic validation. Do not send; safe to delete."
                    ),
                    "to": ["recipient@example.test"],
                }
            ],
        }

    def create_draft(self, to, subject, body, *, expected_account=None):
        self.drafts["draft-test"] = {
            "draft_id": "draft-test",
            "to": to,
            "subject": subject,
            "body": body,
            "sent": False,
        }
        return {"draft_id": "draft-test", "sent": False, "status": "draft_created"}

    def get_draft(self, draft_id):
        return self.drafts[draft_id]

    def delete_draft(self, draft_id, *, expected_account=None):
        self.drafts.pop(draft_id)
        return {"draft_id": draft_id, "status": "draft_deleted"}

    def draft_exists(self, draft_id):
        return draft_id in self.drafts


def _model_runner(typed_input, *, context, model, trace_metadata):
    source_id = context.allowed_source_ids[0]
    draft = OutreachDraft(
        company_name="Keystone Business Agents validation",
        contact_name="there",
        subject="Validation follow-up",
        email_body=(
            "Hi there,\n\nFollowing up on the synthetic KBA test. "
            "Could you confirm receipt?\n\nSincerely,\nAnup"
        ),
        linkedin_note="",
        facts_used=[context.allowed_facts[0]],
        source_ids_used=[source_id],
        approved_context_used=True,
        approval_required=True,
        approval_scope="external_use",
        send_enabled=False,
        sent=False,
        can_send_email=False,
    )
    return SimpleNamespace(
        final_output=draft,
        usage={"requests": 1, "input_tokens": 100, "output_tokens": 50},
        cost={"estimated_usd": 0.001},
        request_cache={"rate_limit_retries": 0},
    )


def test_selected_synthetic_thread_to_outreach_provider_draft_cleanup(monkeypatch) -> None:
    monkeypatch.setenv("KEYSTONE_GMAIL_ALLOW_TEST_DRAFT_DELETES", "true")
    provider = FakeGmail()

    result = execute_validation(
        model="gpt-5.4-mini",
        budget_usd=0.05,
        provider=provider,
        account="operator@example.test",
        recipient="recipient@example.test",
        suffix="fixture",
        model_runner=_model_runner,
    )

    assert result["status"] == "pass"
    assert result["requests"] == 1
    assert result["checks"]["provider_draft_verified"] is True
    assert result["checks"]["provider_draft_deleted"] is True
    assert result["safety"]["email_sent"] is False
    assert provider.drafts == {}
    rendered = str(result)
    assert "operator@example.test" not in rendered
    assert "recipient@example.test" not in rendered
    assert "Following up" not in rendered
