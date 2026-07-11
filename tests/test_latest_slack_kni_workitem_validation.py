from __future__ import annotations

from pathlib import Path

from scripts.run_latest_slack_kni_workitem_validation import run_validation


class FakeSlack:
    def find_latest_kni_request(self, channel: str, *, scan_limit: int):
        assert (channel, scan_limit) == ("C123", 200)
        return {
            "channel": channel,
            "ts": "1.0",
            "text": "@KNI business research analyst research Example Health",
            "user_present": True,
        }


def test_latest_kni_request_creates_hashed_no_live_workitem_receipt(tmp_path: Path) -> None:
    payload = run_validation(
        channel="C123",
        database=tmp_path / "workitems.sqlite",
        slack=FakeSlack(),
    )

    assert payload["status"] == "pass"
    assert payload["stored_request_matches"] is True
    assert payload["work_item_id"]
    assert payload["route"] == "business_research_analyst"
    assert payload["openai_requests"] == 0
    assert payload["live_search"] is False
    assert payload["slack_posts"] == 0
    assert payload["external_writes"] == 0
    assert payload["send_enabled"] is False
    assert "channel" not in payload["slack_ref"]
    assert "ts" not in payload["slack_ref"]
