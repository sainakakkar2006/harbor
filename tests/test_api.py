import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ["HARBOR_DB"] = "/tmp/harbor_test.db"
os.environ.pop("HARBOR_API_KEYS", None)
os.environ.pop("GEMINI_API_KEY", None)

import main  # noqa: E402
from harbor import safety  # noqa: E402

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def clean_db():
    if os.path.exists("/tmp/harbor_test.db"):
        os.remove("/tmp/harbor_test.db")
    yield


def _assess(content, **extra):
    return client.post(
        "/v1/assess",
        json={"conversation_id": "c1", "messages": [{"role": "user", "content": content}], **extra},
    )


def test_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"]


def test_crisis_message_issues_referral():
    r = _assess("I want to kill myself")
    body = r.json()
    assert body["risk_level"] in ("high", "imminent")
    assert body["referral_issued"] is True
    assert any("988" in res["name"] for res in body["crisis_resources"])
    assert body["escalation_triggered"] is True


def test_benign_message_no_referral():
    r = _assess("what a lovely day for a walk")
    body = r.json()
    assert body["risk_level"] == "none"
    assert body["referral_issued"] is False
    assert body["crisis_resources"] == []


def test_idiom_not_flagged():
    assert _assess("this homework is killing me lol").json()["risk_level"] in ("none", "low")


def test_locale_resources():
    r = _assess("I want to kill myself", user_locale="GB")
    assert any("Samaritans" in res["name"] for res in r.json()["crisis_resources"])


def test_minor_break_reminder():
    r = _assess("hi there", user_is_minor=True, session_started_at=time.time() - 4 * 3600)
    mp = r.json()["minor_protections"]
    assert mp["ai_disclosure_required"] is True
    assert mp["break_reminder_due"] is True and mp["break_reminder_text"]


def test_minor_no_reminder_yet():
    r = _assess("hi there", user_is_minor=True, session_started_at=time.time() - 600)
    assert r.json()["minor_protections"]["break_reminder_due"] is False


def test_auth_enforced(monkeypatch):
    monkeypatch.setenv("HARBOR_API_KEYS", "acme:sk_test_123")
    assert _assess("hello").status_code == 401
    r = client.post(
        "/v1/assess",
        json={"conversation_id": "c1", "messages": [{"role": "user", "content": "hello"}]},
        headers={"X-API-Key": "sk_test_123"},
    )
    assert r.status_code == 200


def test_compliance_report_counts_referrals():
    _assess("I want to kill myself")
    _assess("nice weather today")
    r = client.get("/v1/compliance/report?year=2026").json()
    assert r["total_assessments"] == 2
    assert r["crisis_referral_notifications_issued"] == 1
    assert "988" in r["detection_protocol"]


def test_stats_endpoint():
    _assess("I want to kill myself")
    s = client.get("/v1/stats").json()
    assert s["total_assessments"] == 1 and s["referrals_issued"] == 1
    assert len(s["daily"]) == 1


def test_gemini_path_parses_structured_output():
    fake = MagicMock()
    fake.text = json.dumps(
        {
            "risk_level": "imminent",
            "categories": ["suicidal_ideation"],
            "rationale": "User states a plan and timeframe.",
            "confidence": 0.97,
        }
    )
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = fake
    with patch.object(safety, "GEMINI_API_KEY", "test-key"), \
         patch("google.genai.Client", return_value=mock_client):
        result = safety.assess([{"role": "user", "content": "..."}])
    assert result["risk_level"] == "imminent"
    assert result["recommended_action"] == "interrupt_and_refer"
    assert result["referral_issued"] is True
    kwargs = mock_client.models.generate_content.call_args.kwargs
    assert kwargs["model"] == safety.GEMINI_MODEL


def test_gemini_bad_level_coerced_safe():
    fake = MagicMock()
    fake.text = json.dumps(
        {"risk_level": "banana", "categories": [], "rationale": "x", "confidence": 0.5}
    )
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = fake
    with patch.object(safety, "GEMINI_API_KEY", "test-key"), \
         patch("google.genai.Client", return_value=mock_client):
        result = safety.assess([{"role": "user", "content": "hello"}])
    # Unknown level from the model must fail SAFE (high), never silently pass
    assert result["risk_level"] == "high"


def test_escalation_webhook_called():
    calls = []
    with patch("harbor.escalation.notify", side_effect=lambda *a, **k: calls.append(a)):
        _assess("I want to kill myself", escalation_webhook="https://example.com/hook")
    assert len(calls) == 1
    conv_hash, assessment = calls[0][0], calls[0][1]
    assert len(conv_hash) == 16 and "kill" not in json.dumps(assessment)
