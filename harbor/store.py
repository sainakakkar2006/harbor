"""Privacy-first audit log.

SB 243's annual report (§22603, due July 1, 2027) must contain referral counts
and protocol descriptions and must NOT contain user identifiers or personal
information. So we never store message content — only a salted hash of the
conversation id and the assessment metadata. That is also the sales pitch:
customers get compliance evidence without a new PII liability.
"""

import hashlib
import os
import sqlite3
import time
from typing import Dict

DB_PATH = os.environ.get("HARBOR_DB", os.path.join(os.path.dirname(__file__), "..", "harbor.db"))
_SALT = os.environ.get("HARBOR_HASH_SALT", "harbor-dev-salt")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            api_key_label TEXT,
            conversation_hash TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            categories TEXT NOT NULL,
            recommended_action TEXT NOT NULL,
            referral_issued INTEGER NOT NULL,
            model TEXT,
            latency_ms INTEGER
        )"""
    )
    return conn


def conv_hash(conversation_id: str) -> str:
    return hashlib.sha256((_SALT + conversation_id).encode()).hexdigest()[:16]


def log_event(conversation_id: str, assessment: Dict, api_key_label: str, latency_ms: int) -> None:
    h = conv_hash(conversation_id)
    conn = _conn()
    with conn:
        conn.execute(
            "INSERT INTO events (ts, api_key_label, conversation_hash, risk_level, categories, "
            "recommended_action, referral_issued, model, latency_ms) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                time.time(),
                api_key_label,
                h,
                assessment["risk_level"],
                ",".join(assessment["categories"]),
                assessment["recommended_action"],
                int(assessment["referral_issued"]),
                assessment.get("model", ""),
                latency_ms,
            ),
        )
    conn.close()


def monthly_count(label: str) -> int:
    """Assessments this calendar month for one tenant (metering)."""
    now = time.localtime()
    start = time.mktime((now.tm_year, now.tm_mon, 1, 0, 0, 0, 0, 0, -1))
    conn = _conn()
    (n,) = conn.execute(
        "SELECT COUNT(*) FROM events WHERE api_key_label = ? AND ts >= ?", (label, start)
    ).fetchone()
    conn.close()
    return n


def stats(days: int = 14, label: str = None) -> Dict:
    """Operational stats for the dashboard (last N days), scoped to one tenant."""
    since = time.time() - days * 86400
    where, params = "ts >= ?", [since]
    if label is not None:
        where += " AND api_key_label = ?"
        params.append(label)
    conn = _conn()
    total, referrals, avg_latency = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(referral_issued),0), COALESCE(AVG(latency_ms),0) "
        f"FROM events WHERE {where}",
        params,
    ).fetchone()
    by_level = dict(
        conn.execute(
            f"SELECT risk_level, COUNT(*) FROM events WHERE {where} GROUP BY risk_level",
            params,
        ).fetchall()
    )
    daily = [
        {"date": d, "assessments": n, "referrals": r}
        for d, n, r in conn.execute(
            f"SELECT date(ts, 'unixepoch'), COUNT(*), SUM(referral_issued) "
            f"FROM events WHERE {where} GROUP BY 1 ORDER BY 1",
            params,
        ).fetchall()
    ]
    conn.close()
    return {
        "tenant": label or "all",
        "window_days": days,
        "total_assessments": total,
        "referrals_issued": referrals,
        "avg_latency_ms": round(avg_latency),
        "by_risk_level": by_level,
        "daily": daily,
    }


def compliance_report(year: int, label: str = None) -> Dict:
    """Aggregates shaped to SB 243 §22603's annual reporting fields, per tenant."""
    start = time.mktime((year, 1, 1, 0, 0, 0, 0, 0, -1))
    end = time.mktime((year + 1, 1, 1, 0, 0, 0, 0, 0, -1))
    where, params = "ts >= ? AND ts < ?", [start, end]
    if label is not None:
        where += " AND api_key_label = ?"
        params.append(label)
    conn = _conn()
    cur = conn.execute(
        f"SELECT COUNT(*), SUM(referral_issued) FROM events WHERE {where}", params
    )
    total, referrals = cur.fetchone()
    by_level = dict(
        conn.execute(
            f"SELECT risk_level, COUNT(*) FROM events WHERE {where} GROUP BY risk_level",
            params,
        ).fetchall()
    )
    conn.close()
    return {
        "tenant": label or "all",
        "year": year,
        "total_assessments": total or 0,
        "crisis_referral_notifications_issued": referrals or 0,
        "assessments_by_risk_level": by_level,
        "detection_protocol": (
            "Every user turn is assessed by a Gemini-based classifier for suicidal ideation, "
            "self-harm, and related risk signals using a five-level evidence-informed taxonomy. "
            "At moderate risk and above, the user is shown crisis service provider referrals "
            "(e.g., 988 Suicide & Crisis Lifeline). No user message content or identifiers are retained."
        ),
    }
