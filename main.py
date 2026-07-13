"""Harbor — drop-in safety & compliance layer for AI chat apps (SB 243 et al.).

Run:   uvicorn main:app --reload
Auth:  set HARBOR_API_KEYS="label1:key1,label2:key2" (unset = open dev mode).
Env:   GEMINI_API_KEY (classifier), HARBOR_ESCALATION_WEBHOOK (optional),
       HARBOR_DB, HARBOR_HASH_SALT.
"""

import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from harbor import escalation, safety, store

app = FastAPI(title="Harbor", version="0.2.0")

SITE = Path(__file__).parent / "site"
BREAK_REMINDER_SECONDS = 3 * 3600  # SB 243: minors get a break reminder at least every 3 hours


def _check_key(x_api_key: Optional[str]) -> str:
    raw = os.environ.get("HARBOR_API_KEYS", "")
    if not raw:
        return "dev"
    keys = dict(pair.split(":", 1) for pair in raw.split(",") if ":" in pair)
    for label, key in keys.items():
        if x_api_key == key:
            return label
    raise HTTPException(status_code=401, detail="invalid API key")


class Message(BaseModel):
    role: str
    content: str


class AssessRequest(BaseModel):
    conversation_id: str
    messages: List[Message] = Field(min_length=1)
    user_locale: str = "US"
    user_is_minor: bool = False
    session_started_at: Optional[float] = None  # unix ts
    last_break_reminder_at: Optional[float] = None  # unix ts
    escalation_webhook: Optional[str] = None  # overrides HARBOR_ESCALATION_WEBHOOK


def _minor_protections(req: AssessRequest) -> Dict:
    anchor = req.last_break_reminder_at or req.session_started_at
    due = bool(anchor and (time.time() - anchor) >= BREAK_REMINDER_SECONDS)
    return {
        "ai_disclosure_required": True,  # SB 243 §22602: must tell known minors they're talking to AI
        "break_reminder_due": due,
        "break_reminder_text": (
            "Just a reminder: you're chatting with an AI, not a person. "
            "It might be a good time to take a break."
        ) if due else None,
    }


@app.post("/v1/assess")
def assess(
    req: AssessRequest,
    background: BackgroundTasks,
    x_api_key: Optional[str] = Header(default=None),
) -> Dict:
    label = _check_key(x_api_key)
    t0 = time.time()
    result = safety.assess([m.model_dump() for m in req.messages], user_locale=req.user_locale)
    latency_ms = int((time.time() - t0) * 1000)
    store.log_event(req.conversation_id, result, api_key_label=label, latency_ms=latency_ms)

    if escalation.should_escalate(result):
        background.add_task(
            escalation.notify, store.conv_hash(req.conversation_id), result, req.escalation_webhook
        )
        result["escalation_triggered"] = True
    else:
        result["escalation_triggered"] = False

    if req.user_is_minor:
        result["minor_protections"] = _minor_protections(req)

    result["latency_ms"] = latency_ms
    return result


@app.get("/v1/compliance/report")
def report(year: int = 2026, x_api_key: Optional[str] = Header(default=None)) -> Dict:
    _check_key(x_api_key)
    return store.compliance_report(year)


@app.get("/v1/stats")
def stats(days: int = 14, x_api_key: Optional[str] = Header(default=None)) -> Dict:
    _check_key(x_api_key)
    return store.stats(days)


@app.get("/health")
def health() -> Dict:
    return {"ok": True, "gemini_configured": bool(os.environ.get("GEMINI_API_KEY"))}


# ---- Embedded site: landing page, live demo, dashboard ----

@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(SITE / "index.html")


@app.get("/demo", include_in_schema=False)
def demo() -> FileResponse:
    return FileResponse(SITE / "demo.html")


@app.get("/dashboard", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(SITE / "dashboard.html")
