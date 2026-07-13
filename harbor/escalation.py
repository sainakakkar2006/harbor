"""Webhook escalation for high/imminent risk events.

Sends metadata only — never message content — so a customer's Slack channel or
on-call system can wake a human without Harbor becoming a PII processor.
Configure with HARBOR_ESCALATION_WEBHOOK (or per-request webhook_url override).
"""

import json
import os
import urllib.request
from typing import Dict, Optional

ESCALATE_LEVELS = ("high", "imminent")


def should_escalate(assessment: Dict) -> bool:
    return assessment["risk_level"] in ESCALATE_LEVELS


def notify(conversation_hash: str, assessment: Dict, webhook_url: Optional[str] = None) -> bool:
    url = webhook_url or os.environ.get("HARBOR_ESCALATION_WEBHOOK", "")
    if not url:
        return False
    payload = {
        "event": "harbor.risk_escalation",
        "conversation": conversation_hash,
        "risk_level": assessment["risk_level"],
        "categories": assessment["categories"],
        "recommended_action": assessment["recommended_action"],
        "referral_issued": assessment["referral_issued"],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "harbor/0.2"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False  # escalation is best-effort; the assess response already carries the referral
