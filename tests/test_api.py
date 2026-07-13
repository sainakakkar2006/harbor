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
def clean_state():
    if os.path.exists("/tmp/harbor_test.db"):
        os.remove("/tmp/harbor_test.db")
    main._demo_hits.clear()
    yield


def _assess(content, key=None, conv="c1", **extra):
    headers = {"X-API-Key": key} if key else {}
    return client.post(
        "/v1/assess",
        json={"conversation_id": conv, "messages": [{"role": "user", "content": content}], **extra},
        headers=headers,
    )


def test_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"]
    assert "fail_mode" in r.json()


def test_crisis_message_issues_referral():
    body = _assess("I want to kill myself").json()
    assert body["risk_level"] in ("high", "imminent")
    assert body["referral_issued"] is True
    assert any("988" in res["name"] for res in body["crisis_resources"])
    assert body["escalation_triggered"] is True


def test_benign_message_no_referral():
    body = _assess("what a lovely day for a walk").json()
    assert body["risk_level"] == "none"
    assert body["referral_issued"] is False


def test_idiom_not_flagged():
    assert _assess("this homework is killing me lol").json()["risk_level"] in ("none", "low")


def test_degraded_flag_is_honest():
    # No GEMINI_API_KEY in tests: engine must be the fallback, marked degraded,
    # and must never claim a Gemini model name (review finding #1/#5).
    body = _assess("I want to kill myself").json()
    assert body["degraded"] is True
    assert body["model"] == safety.FALLBACK_ENGINE
    assert "gemini" not in body["model"]


# --- Hardened fallback (review finding #2) ---

def test_fallback_catches_kms():
    assert _assess("kms").json()["risk_level"] == "high"


def test_fallback_catches_means_plus_intent():
    assert _assess("I have a gun and I'm done").json()["risk_level"] == "high"
    assert _assess("the rope is tied and I'm stepping off the chair now").json()["risk_level"] == "high"


def test_fallback_catches_spanish():
    r = _assess("Ya no quiero vivir, esta noche me voy a quitar la vida", user_locale="MX")
    body = r.json()
    assert body["risk_level"] == "high"
    assert any("Vida" in res["name"] for res in body["crisis_resources"])


# --- Fail-closed contract (review finding #1/#6) ---

def _broken_gemini():
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = RuntimeError("429 quota")
    return patch.object(safety, "GEMINI_API_KEY", "test-key"), \
        patch("google.genai.Client", return_value=mock_client)


def test_model_outage_benign_returns_503_not_silent_none():
    p1, p2 = _broken_gemini()
    with p1, p2:
        r = _assess("what a lovely day")
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "detection_unavailable"
    assert r.headers.get("retry-after") == "30"


def test_model_outage_still_catches_explicit_risk():
    p1, p2 = _broken_gemini()
    with p1, p2:
        r = _assess("I want to kill myself")
    assert r.status_code == 200
    body = r.json()
    assert body["risk_level"] == "high" and body["degraded"] is True
    assert body["referral_issued"] is True


def test_backup_model_tried_before_degrading():
    calls = []

    def flaky(model, **kwargs):
        calls.append(model)
        if model == safety.GEMINI_MODEL:
            raise RuntimeError("primary down")
        fake = MagicMock()
        fake.text = json.dumps({"risk_level": "none", "categories": [],
                                "rationale": "ok", "confidence": 0.9})
        return fake

    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = flaky
    with patch.object(safety, "GEMINI_API_KEY", "test-key"), \
         patch("google.genai.Client", return_value=mock_client):
        r = _assess("hello there")
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == safety.GEMINI_BACKUP_MODEL and body["degraded"] is False
    assert calls == [safety.GEMINI_MODEL, safety.GEMINI_BACKUP_MODEL]


# --- Auth & tenancy (review findings #3/#4) ---

def test_wrong_key_is_401_never_demo(monkeypatch):
    monkeypatch.setenv("HARBOR_API_KEYS", "acme:sk_live_a")
    assert _assess("hello", key="totally-fake-key-123").status_code == 401


def test_no_key_serves_demo_tenant(monkeypatch):
    monkeypatch.setenv("HARBOR_API_KEYS", "acme:sk_live_a")
    body = _assess("hello").json()
    assert body["tenant"] == "demo"


def test_tenant_data_isolation(monkeypatch):
    monkeypatch.setenv("HARBOR_API_KEYS", "acme:sk_live_a,globex:sk_live_g")
    _assess("I want to kill myself", key="sk_live_a", conv="a1")
    _assess("nice weather", key="sk_live_g", conv="g1")
    _assess("demo pollution attempt: I want to kill myself", conv="d1")

    acme = client.get("/v1/stats", headers={"X-API-Key": "sk_live_a"}).json()
    globex = client.get("/v1/stats", headers={"X-API-Key": "sk_live_g"}).json()
    demo = client.get("/v1/stats").json()
    assert (acme["tenant"], acme["total_assessments"], acme["referrals_issued"]) == ("acme", 1, 1)
    assert (globex["tenant"], globex["total_assessments"], globex["referrals_issued"]) == ("globex", 1, 0)
    assert demo["tenant"] == "demo" and demo["total_assessments"] == 1

    acme_report = client.get("/v1/compliance/report?year=2026",
                             headers={"X-API-Key": "sk_live_a"}).json()
    assert acme_report["tenant"] == "acme"
    assert acme_report["crisis_referral_notifications_issued"] == 1


def test_demo_rate_limit(monkeypatch):
    monkeypatch.setenv("HARBOR_API_KEYS", "acme:sk_live_a")
    monkeypatch.setattr(main, "DEMO_RPH", 3)
    for _ in range(3):
        assert _assess("hello").status_code == 200
    r = _assess("hello")
    assert r.status_code == 429
    assert r.json()["detail"]["error"] == "demo_rate_limited"


def test_monthly_quota_enforced(monkeypatch):
    monkeypatch.setenv("HARBOR_API_KEYS", "acme:sk_live_a")
    monkeypatch.setattr(main, "MONTHLY_CAP", 2)
    assert _assess("one", key="sk_live_a").status_code == 200
    assert _assess("two", key="sk_live_a").status_code == 200
    r = _assess("three", key="sk_live_a")
    assert r.status_code == 429
    assert r.json()["detail"]["error"] == "monthly_quota_exceeded"


# --- Pre-existing behavior kept ---

def test_locale_resources():
    r = _assess("I want to kill myself", user_locale="GB")
    assert any("Samaritans" in res["name"] for res in r.json()["crisis_resources"])


def test_minor_break_reminder():
    r = _assess("hi there", user_is_minor=True, session_started_at=time.time() - 4 * 3600)
    mp = r.json()["minor_protections"]
    assert mp["ai_disclosure_required"] is True
    assert mp["break_reminder_due"] is True and mp["break_reminder_text"]


def test_compliance_report_counts_referrals():
    _assess("I want to kill myself")
    _assess("nice weather today")
    r = client.get("/v1/compliance/report?year=2026").json()
    assert r["total_assessments"] == 2
    assert r["crisis_referral_notifications_issued"] == 1


def test_gemini_path_parses_structured_output():
    fake = MagicMock()
    fake.text = json.dumps({"risk_level": "imminent", "categories": ["suicidal_ideation"],
                            "rationale": "User states a plan and timeframe.", "confidence": 0.97})
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = fake
    with patch.object(safety, "GEMINI_API_KEY", "test-key"), \
         patch("google.genai.Client", return_value=mock_client):
        result = safety.assess([{"role": "user", "content": "..."}])
    assert result["risk_level"] == "imminent"
    assert result["recommended_action"] == "interrupt_and_refer"
    assert result["degraded"] is False and result["model"] == safety.GEMINI_MODEL


def test_gemini_bad_level_coerced_safe():
    fake = MagicMock()
    fake.text = json.dumps({"risk_level": "banana", "categories": [], "rationale": "x", "confidence": 0.5})
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = fake
    with patch.object(safety, "GEMINI_API_KEY", "test-key"), \
         patch("google.genai.Client", return_value=mock_client):
        result = safety.assess([{"role": "user", "content": "hello"}])
    assert result["risk_level"] == "high"


def test_escalation_webhook_called():
    calls = []
    with patch("harbor.escalation.notify", side_effect=lambda *a, **k: calls.append(a)):
        _assess("I want to kill myself", escalation_webhook="https://example.com/hook")
    assert len(calls) == 1
    conv_hash, assessment = calls[0][0], calls[0][1]
    assert len(conv_hash) == 16 and "kill" not in json.dumps(assessment)
