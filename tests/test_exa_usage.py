from __future__ import annotations

from keystone_agents.exa_usage import EXA_DASHBOARD_URL, exa_usage_snapshot


class ExaUsageResponse:
    status_code = 200

    def json(self) -> dict[str, object]:
        return {
            "api_key_id": "key_123",
            "period": {
                "start": "2026-06-01T00:00:00Z",
                "end": "2026-06-09T00:00:00Z",
            },
            "total_cost_usd": 0,
            "cost_breakdown": [
                {
                    "price_id": "price_search",
                    "price_name": "Search",
                    "quantity": 123,
                    "amount_usd": 0,
                },
                {
                    "price_id": "price_content_retrieval",
                    "price_name": "Content Retrieval",
                    "quantity": 10,
                    "amount_usd": 0,
                },
            ],
        }


class ExaApiKeysResponse:
    status_code = 200

    def json(self) -> dict[str, object]:
        return {
            "apiKeys": [
                {
                    "id": "key_123",
                    "name": "default",
                    "rateLimit": 600,
                    "budgetCents": 0,
                    "isOverBudget": False,
                }
            ]
        }


def test_exa_usage_snapshot_reports_missing_configuration(monkeypatch) -> None:
    monkeypatch.delenv("EXA_API_KEY_ID", raising=False)
    monkeypatch.delenv("EXA_API_KEY_NAME", raising=False)
    monkeypatch.delenv("EXA_SERVICE_API_KEY", raising=False)

    snapshot = exa_usage_snapshot()

    assert snapshot.available is False
    assert snapshot.status == "missing_configuration"
    assert "EXA_API_KEY_ID" in snapshot.note
    assert snapshot.dashboard_url == EXA_DASHBOARD_URL


def test_exa_usage_snapshot_fetches_usage_and_estimates_free_tier_remaining(
    monkeypatch,
) -> None:
    monkeypatch.setenv("EXA_API_KEY_ID", "key_123")
    monkeypatch.setenv("EXA_SERVICE_API_KEY", "service-key")
    calls: list[dict[str, object]] = []

    def fake_get(url, *, headers, params, timeout):
        calls.append({"url": url, "headers": headers, "params": params, "timeout": timeout})
        return ExaUsageResponse()

    snapshot = exa_usage_snapshot(
        start_date="2026-06-01",
        end_date="2026-06-09",
        monthly_free_request_limit=1000,
        http_get=fake_get,
    )

    assert snapshot.available is True
    assert snapshot.status == "ok"
    assert snapshot.credits_used == 123
    assert snapshot.search_requests_used == 123
    assert snapshot.monthly_free_credit_limit == 1000
    assert snapshot.total_quantity_used == 133
    assert snapshot.estimated_free_credits_remaining == 877
    assert snapshot.estimated_free_requests_remaining == 877
    assert snapshot.monthly_free_request_limit == 1000
    assert calls[0]["url"] == "https://admin-api.exa.ai/team-management/api-keys/key_123/usage"
    assert calls[0]["headers"] == {"x-api-key": "service-key"}
    assert calls[0]["params"] == {
        "start_date": "2026-06-01",
        "end_date": "2026-06-09",
        "group_by": "day",
    }


def test_exa_usage_snapshot_resolves_default_key_name(monkeypatch) -> None:
    monkeypatch.delenv("EXA_API_KEY_ID", raising=False)
    monkeypatch.setenv("EXA_API_KEY_NAME", "default")
    monkeypatch.setenv("EXA_SERVICE_API_KEY", "service-key")
    calls: list[str] = []

    def fake_get(url, **_kwargs):
        calls.append(url)
        if url.endswith("/team-management/api-keys"):
            return ExaApiKeysResponse()
        return ExaUsageResponse()

    snapshot = exa_usage_snapshot(
        start_date="2026-06-01",
        end_date="2026-06-09",
        http_get=fake_get,
    )

    assert snapshot.available is True
    assert snapshot.api_key_id == "key_123"
    assert calls == [
        "https://admin-api.exa.ai/team-management/api-keys",
        "https://admin-api.exa.ai/team-management/api-keys/key_123/usage",
    ]
