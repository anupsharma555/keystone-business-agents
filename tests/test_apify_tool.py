from __future__ import annotations

import json

import pytest

from keystone_agents.tools.apify_tool import (
    ApifyTool,
    fetch_linkedin_or_profile_placeholder,
    get_dataset_items,
    run_actor,
)


def test_run_actor_dry_run_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APIFY_API_TOKEN", raising=False)

    result = run_actor("dry/actor", {"query": "behavioral health"})

    assert result["mode"] == "dry_run"
    assert result["status"] == "dry-run"
    assert result["actor_id"] == "dry/actor"
    assert result["input_payload"] == {"query": "behavioral health"}


def test_get_dataset_items_dry_run_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APIFY_API_TOKEN", raising=False)

    items = get_dataset_items("dry-run-dataset")

    assert items == [
        {
            "mode": "dry_run",
            "dataset_id": "dry-run-dataset",
            "item_id": "dry-run-item-1",
            "title": "Dry-run Apify dataset item",
            "source": "apify:dry-run",
        }
    ]


def test_apify_missing_key_raises_only_in_live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APIFY_API_TOKEN", raising=False)

    run_actor("dry/actor", {}, live=False)
    get_dataset_items("dry-run-dataset", live=False)
    with pytest.raises(RuntimeError, match="APIFY_API_TOKEN"):
        run_actor("dry/actor", {}, live=True)
    with pytest.raises(RuntimeError, match="APIFY_API_TOKEN"):
        get_dataset_items("dry-run-dataset", live=True)


def test_apify_errors_do_not_print_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "apify-secret-test-value"
    monkeypatch.setenv("APIFY_API_TOKEN", secret)

    with pytest.raises(NotImplementedError) as actor_exc:
        run_actor("dry/actor", {}, live=True)
    with pytest.raises(NotImplementedError) as dataset_exc:
        get_dataset_items("dry-run-dataset", live=True)

    assert secret not in str(actor_exc.value)
    assert secret not in str(dataset_exc.value)


def test_apify_class_wrapper_uses_dry_run() -> None:
    result = ApifyTool().run_actor("dry/actor", {"q": "test"})

    assert result["mode"] == "dry_run"
    assert result["status"] == "dry-run"


def test_linkedin_profile_placeholder_extracts_fixture_claims() -> None:
    fixture = json.dumps(
        {
            "linkedin_url": "https://linkedin.example/company/curebase",
            "sources": [
                {
                    "source_type": "linkedin",
                    "supported_claims": ["Curebase lists decentralized trial operations."],
                }
            ],
        }
    )

    payload = json.loads(
        fetch_linkedin_or_profile_placeholder(
            "Curebase",
            fixture_json=fixture,
        )
    )

    assert payload["mode"] == "dry_run"
    assert payload["linkedin_url"] == "https://linkedin.example/company/curebase"
    assert payload["claims"] == ["Curebase lists decentralized trial operations."]
    assert payload["placeholder"] is False


def test_linkedin_profile_placeholder_flags_missing_claims_and_rejects_live() -> None:
    payload = json.loads(fetch_linkedin_or_profile_placeholder("Curebase"))

    assert payload["claims"] == []
    assert payload["placeholder"] is True
    with pytest.raises(RuntimeError, match="explicit integration implementation"):
        fetch_linkedin_or_profile_placeholder("Curebase", dry_run=False)
