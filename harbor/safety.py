"""Gemini-powered risk assessment for user conversations.

Taxonomy is deliberately narrow: this classifies *user distress signals that
trigger a duty-of-care response* (SB 243 crisis protocol), not general content
moderation. Precision on imminent-risk matters more than breadth.

Degradation contract (see README): the primary model is tried first, then a
backup model. If both fail, the keyword screen runs and the result is marked
degraded=True with model="fallback_keyword_v2" — the response never claims a
Gemini model assessed a message it didn't. Whether a degraded no-risk result
becomes a 503 is the API layer's decision (HARBOR_FAIL_MODE).
"""

import json
import os
import re
from typing import Dict, List

from pydantic import BaseModel

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# Lite is primary: it scores identically on redteam.py (10/10) at ~1s/turn
# vs 7-13s for gemini-flash-latest. Re-verify with redteam.py if you reorder.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")
GEMINI_BACKUP_MODEL = os.environ.get("GEMINI_BACKUP_MODEL", "gemini-flash-latest")
# Each model has its own free-tier quota, so a longer chain survives
# single-model exhaustion. Overridable as a comma-separated list.
MODEL_CHAIN = [
    m.strip()
    for m in os.environ.get(
        "HARBOR_MODEL_CHAIN", f"{GEMINI_MODEL},{GEMINI_BACKUP_MODEL},gemini-2.0-flash"
    ).split(",")
    if m.strip()
]
# Gemini rejects deadlines under 10s ("Minimum allowed deadline is 10s"), so
# clamp to that floor no matter what the env says.
GEMINI_TIMEOUT_MS = max(int(os.environ.get("GEMINI_TIMEOUT_MS", "15000")), 10000)
MODEL_COOLDOWN_S = int(os.environ.get("HARBOR_MODEL_COOLDOWN_S", "60"))

# model -> unix ts of last failure; failed models are skipped for
# MODEL_COOLDOWN_S so an exhausted quota doesn't add timeout latency
# to every request during an outage.
_model_cooldown: Dict[str, float] = {}

FALLBACK_ENGINE = "fallback_keyword_v2"

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
    "MX": [
        {"name": "Línea de la Vida", "contact": "800 911 2000", "url": "https://www.gob.mx/salud/conadic"},
    ],
    "ES": [
        {"name": "Teléfono de la Esperanza / Línea 024", "contact": "Llama al 024", "url": "https://www.sanidad.gob.es/linea024/home.htm"},
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


_SYSTEM = f"""You are a safety classifier for an AI chat application. You assess the USER's messages (not the assistant's) for signals that require a duty-of-care response under California SB 243 and similar laws. Users may write in any language — assess meaning, not language.

Risk levels (pick exactly one):
- none: no distress signals
- low: mild negative affect, venting, dark humor with no risk content
- moderate: sustained distress, hopelessness, passive ideation ("what's the point of anything")
- high: active suicidal/self-harm ideation without stated plan or timeframe, or disclosure of abuse, or a minor describing an unsafe situation
- imminent: stated intent, plan, timeframe, or means for suicide/self-harm/harm to others

Categories (pick all that apply, may be empty): {", ".join(CATEGORIES)}

Rules:
- Judge the whole conversation trajectory, weighting recent messages most.
- Indirect and masked expressions count ("I won't be a problem much longer"), as do
  abbreviations and euphemisms in any language ("kms", "unalive").
- Do NOT flag: idioms ("this traffic is killing me"), fiction/roleplay clearly framed as such, academic discussion of these topics, song lyrics.
- rationale: one sentence, describe the signal type — never quote the user's words back.
- When genuinely uncertain between two levels, pick the higher one.
"""


def assess(messages: List[Dict[str, str]], user_locale: str = "US") -> Dict:
    """Assess a conversation.

    Returns the assessment plus resources, recommended action, and an honest
    account of which engine produced it:
      model     — engine that actually ran (never a Gemini name unless Gemini ran)
      degraded  — True when the keyword screen answered instead of a model
      degraded_reason — "model_error" (key set, calls failed) or
                        "no_model_configured" (no GEMINI_API_KEY)
    """
    result = None
    model_used = None
    degraded_reason = None

    if GEMINI_API_KEY:
        import time as _time

        now = _time.time()
        for model in MODEL_CHAIN:
            if now - _model_cooldown.get(model, 0) < MODEL_COOLDOWN_S:
                continue
            try:
                result = _gemini_assess(messages, model)
                model_used = model
                break
            except Exception:
                _model_cooldown[model] = now
                continue
        if result is None:
            result = _fallback_assess(messages)
            model_used = FALLBACK_ENGINE
            degraded_reason = "model_error"
    else:
        result = _fallback_assess(messages)
        model_used = FALLBACK_ENGINE
        degraded_reason = "no_model_configured"

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
        "model": model_used,
        "degraded": degraded_reason is not None,
        "degraded_reason": degraded_reason,
    }


def _gemini_assess(messages: List[Dict[str, str]], model: str) -> Assessment:
    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS),
    )
    convo = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages[-20:])

    resp = client.models.generate_content(
        model=model,
        contents=f"CONVERSATION:\n{convo}\n\nAssess the user's current risk.",
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM,
            response_mime_type="application/json",
            response_schema=Assessment,
            temperature=0.0,
        ),
    )
    return Assessment(**json.loads(resp.text))


# ---------------------------------------------------------------------------
# Degraded keyword screen (v2). This is a safety net, not a classifier: it is
# deliberately over-sensitive, covers common abbreviations/euphemisms, Spanish,
# and means+finality phrasing. It exists so an outage degrades to "too careful"
# instead of "silent none" — the API layer still surfaces degraded=True and,
# for no-risk results, fails closed by default.
# ---------------------------------------------------------------------------

_EXPLICIT_RE = re.compile(
    r"\b("
    # English — explicit and common shorthand
    r"kill(ing)? myself|kms|unalive( myself)?|suicid(e|al)|end(ing)? my life|"
    r"end it all|take my own life|want(s|ed)? to die|wanna die|"
    r"wish i (was|were) dead|better off dead|no reason to live|"
    r"(don'?t|do not) want to (live|be alive|wake up)|tired of being alive|"
    r"hurt(ing)? myself|cut(ting)? myself|self.?harm|"
    r"won'?t be (around|a problem|here) (much longer|anymore)|"
    r"not be here tomorrow|goodbye forever|"
    # Spanish — explicit
    r"quitar(me|se) la vida|no quiero (vivir|seguir viviendo)|"
    r"matar(me|se)|suicidar(me|se)|acabar con mi vida|quiero morir(me)?"
    r")\b",
    re.IGNORECASE,
)

# Means + finality within one message ("I have a gun and I'm done",
# "the rope is tied and I'm stepping off the chair now").
_MEANS = r"(gun|rope|pills?|bridge|blade|razor|knife|ledge|roof|pistola|cuerda|pastillas)"
_FINALITY = r"(i'?m done|done|end(ing)? (it|this)|ready|tonight|right now|goodbye|step(ping)? off|jump(ing)?|going to (do it|end)|se acab[oó]|esta noche)"
_MEANS_INTENT_RE = re.compile(
    rf"\b{_MEANS}\b.{{0,80}}\b{_FINALITY}\b|\b{_FINALITY}\b.{{0,80}}\b{_MEANS}\b",
    re.IGNORECASE,
)


def _fallback_assess(messages: List[Dict[str, str]]) -> Assessment:
    user_text = " ".join(m["content"] for m in messages if m.get("role") == "user")
    if _EXPLICIT_RE.search(user_text) or _MEANS_INTENT_RE.search(user_text):
        return Assessment(
            risk_level="high",
            categories=["suicidal_ideation"],
            rationale="Degraded keyword screen matched risk language; full-model assessment unavailable.",
            confidence=0.45,
        )
    return Assessment(
        risk_level="none",
        categories=[],
        rationale="Degraded keyword screen found no risk language; full-model assessment unavailable.",
        confidence=0.2,
    )
