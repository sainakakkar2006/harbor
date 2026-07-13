"""Gemini-powered risk assessment for user conversations.

Taxonomy is deliberately narrow: this classifies *user distress signals that
trigger a duty-of-care response* (SB 243 crisis protocol), not general content
moderation. Precision on imminent-risk matters more than breadth.
"""

import json
import os
import re
from typing import Dict, List, Optional

from pydantic import BaseModel

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")

RISK_LEVELS = ["none", "low", "moderate", "high", "imminent"]

CATEGORIES = [
    "suicidal_ideation",
    "self_harm",
    "harm_to_others",
    "minor_at_risk",
    "abuse_disclosure",
    "acute_distress",
]

ACTIONS = {
    "none": "none",
    "low": "monitor",
    "moderate": "show_resources",
    "high": "show_resources_and_soften",
    "imminent": "interrupt_and_refer",
}

# Locale → crisis resources surfaced to the end user (SB 243 §22602(b):
# referral to crisis service providers on expression of suicidal ideation).
CRISIS_RESOURCES = {
    "US": [
        {"name": "988 Suicide & Crisis Lifeline", "contact": "Call or text 988", "url": "https://988lifeline.org"},
        {"name": "Crisis Text Line", "contact": "Text HOME to 741741", "url": "https://www.crisistextline.org"},
    ],
    "GB": [
        {"name": "Samaritans", "contact": "Call 116 123", "url": "https://www.samaritans.org"},
    ],
    "DEFAULT": [
        {"name": "International Association for Suicide Prevention — Crisis Centres", "contact": "", "url": "https://www.iasp.info/resources/Crisis_Centres/"},
    ],
}


class Assessment(BaseModel):
    risk_level: str  # one of RISK_LEVELS
    categories: List[str]  # subset of CATEGORIES
    rationale: str  # one sentence, no quoted user content
    confidence: float  # 0–1


_SYSTEM = f"""You are a safety classifier for an AI chat application. You assess the USER's messages (not the assistant's) for signals that require a duty-of-care response under California SB 243 and similar laws.

Risk levels (pick exactly one):
- none: no distress signals
- low: mild negative affect, venting, dark humor with no risk content
- moderate: sustained distress, hopelessness, passive ideation ("what's the point of anything")
- high: active suicidal/self-harm ideation without stated plan or timeframe, or disclosure of abuse, or a minor describing an unsafe situation
- imminent: stated intent, plan, timeframe, or means for suicide/self-harm/harm to others

Categories (pick all that apply, may be empty): {", ".join(CATEGORIES)}

Rules:
- Judge the whole conversation trajectory, weighting recent messages most.
- Indirect and masked expressions count ("I won't be a problem much longer").
- Do NOT flag: idioms ("this traffic is killing me"), fiction/roleplay clearly framed as such, academic discussion of these topics, song lyrics.
- rationale: one sentence, describe the signal type — never quote the user's words back.
- When genuinely uncertain between two levels, pick the higher one.
"""


def assess(messages: List[Dict[str, str]], user_locale: str = "US") -> Dict:
    """Assess a conversation. Returns assessment dict + resources + recommended action."""
    if GEMINI_API_KEY:
        try:
            result = _gemini_assess(messages)
        except Exception:
            # Rate limit / outage must degrade to the over-sensitive keyword
            # fallback, never surface an error: fail open on safety, not closed.
            result = _fallback_assess(messages)
    else:
        result = _fallback_assess(messages)

    level = result.risk_level if result.risk_level in RISK_LEVELS else "high"
    referral = level in ("moderate", "high", "imminent")
    return {
        "risk_level": level,
        "categories": [c for c in result.categories if c in CATEGORIES],
        "rationale": result.rationale,
        "confidence": result.confidence,
        "recommended_action": ACTIONS[level],
        "referral_issued": referral,
        "crisis_resources": CRISIS_RESOURCES.get(user_locale, CRISIS_RESOURCES["DEFAULT"]) if referral else [],
        "model": GEMINI_MODEL if GEMINI_API_KEY else "fallback_keyword",
    }


def _gemini_assess(messages: List[Dict[str, str]]) -> Assessment:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)
    convo = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages[-20:])

    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=f"CONVERSATION:\n{convo}\n\nAssess the user's current risk.",
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM,
            response_mime_type="application/json",
            response_schema=Assessment,
            temperature=0.0,
        ),
    )
    return Assessment(**json.loads(resp.text))


# Deliberately over-sensitive keyword fallback: only used when Gemini is
# unreachable, where a false positive (showing resources) is the safe failure.
_IMMINENT_RE = re.compile(
    r"\b(kill myself|end my life|suicide|want to die|better off dead|"
    r"hurt myself|cutting myself|no reason to live)\b",
    re.IGNORECASE,
)


def _fallback_assess(messages: List[Dict[str, str]]) -> Assessment:
    user_text = " ".join(m["content"] for m in messages if m.get("role") == "user")
    if _IMMINENT_RE.search(user_text):
        return Assessment(
            risk_level="high",
            categories=["suicidal_ideation"],
            rationale="Keyword fallback matched explicit risk language (Gemini unavailable).",
            confidence=0.5,
        )
    return Assessment(
        risk_level="none",
        categories=[],
        rationale="Keyword fallback found no explicit risk language (Gemini unavailable).",
        confidence=0.3,
    )
