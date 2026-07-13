"""Harbor — drop-in safety & compliance layer for AI chat apps (SB 243 et al.).

Run:   uvicorn main:app --reload
Auth:  HARBOR_API_KEYS="label1:key1,label2:key2". Requests without a valid key
       are served as the shared, rate-limited "demo" tenant (disable with
       HARBOR_ALLOW_DEMO=0). Unset HARBOR_API_KEYS entirely = open dev mode.
Env:   GEMINI_API_KEY, GEMINI_MODEL, GEMINI_BACKUP_MODEL, GEMINI_TIMEOUT_MS,
       HARBOR_FAIL_MODE=closed|open, HARBOR_MONTHLY_CAP, HARBOR_DEMO_RPH,
       HARBOR_ESCALATION_WEBHOOK, HARBOR_DB, HARBOR_HASH_SALT.

Error contract (see README):
  200 — assessment completed; check `degraded` (true = keyword screen, not a model)
  401 — HARBOR_API_KEYS is set, a key was supplied, and it is invalid
  422 — malformed request body
  429 — demo rate limit or tenant monthly cap exceeded
  503 — detection degraded AND no risk detected (fail-closed default): retry or queue
"""

import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from harbor import escalation, safety, store

DESCRIPTION = """Crisis-detection, referral, and compliance-evidence API for AI chat apps
(California SB 243 and similar duty-of-care laws).

Send each user turn to **POST /v1/assess**; render `crisis_resources` when
`referral_issued` is true. Responses marked `degraded: true` were screened by
the conservative keyword fallback, not a Gemini model. By default a degraded
assessment that finds no risk returns **503** rather than a false "none" —
retry with backoff or queue the turn.
"""

app = FastAPI(
    title="Harbor API",
    version="0.3.0",
    description=DESCRIPTION,
    openapi_tags=[
        {"name": "assess", "description": "Per-turn risk assessment"},
        {"name": "evidence", "description": "Tenant-scoped stats and SB 243 reporting"},
        {"name": "ops", "description": "Health and status"},
    ],
)

SITE = Path(__file__).parent / "site"
BREAK_REMINDER_SECONDS = 3 * 3600  # SB 243: minors get a break reminder at least every 3 hours

FAIL_MODE = os.environ.get("HARBOR_FAIL_MODE", "closed")
MONTHLY_CAP = int(os.environ.get("HARBOR_MONTHLY_CAP", "10000"))
DEMO_RPH = int(os.environ.get("HARBOR_DEMO_RPH", "30"))  # per-IP demo requests/hour

_demo_hits: Dict[str, Tuple[float, int]] = {}  # ip -> (window_start, count)


def _tenant(x_api_key: Optional[str], request: Optional[Request] = None) -> str:
    """Resolve the tenant label for this request.

    Valid key → that tenant. No key (or no keys configured) → "demo" / "dev".
    A *wrong* key is always a 401 — never silently downgraded to demo.
    """
    raw = os.environ.get("HARBOR_API_KEYS", "")
    if not raw:
        return "dev"
    keys = dict(pair.split(":", 1) for pair in raw.split(",") if ":" in pair)
    if x_api_key:
        for label, key in keys.items():
            if x_api_key == key:
                return label
        raise HTTPException(status_code=401, detail="invalid API key")
    if os.environ.get("HARBOR_ALLOW_DEMO", "1") == "0":
        raise HTTPException(status_code=401, detail="API key required")
    return "demo"


def _demo_rate_limit(request: Request) -> None:
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0].strip()
    now = time.time()
    start, count = _demo_hits.get(ip, (now, 0))
    if now - start > 3600:
        start, count = now, 0
    if count >= DEMO_RPH:
        raise HTTPException(
            status_code=429,
            detail={"error": "demo_rate_limited", "limit_per_hour": DEMO_RPH,
                    "hint": "Get an API key for production traffic."},
        )
    _demo_hits[ip] = (start, count + 1)


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


@app.post("/v1/assess", tags=["assess"])
def assess(
    req: AssessRequest,
    request: Request,
    background: BackgroundTasks,
    x_api_key: Optional[str] = Header(default=None),
) -> Dict:
    """Assess one conversation turn. Call this on every user message.

    Returns risk level, recommended action, crisis resources (when a referral
    is due), and honest engine/degradation metadata. 503 = detection is
    degraded and nothing conclusive was found — retry or queue, do not treat
    as "no risk".
    """
    label = _tenant(x_api_key, request)
    if label == "demo":
        _demo_rate_limit(request)
    elif label != "dev" and store.monthly_count(label) >= MONTHLY_CAP:
        raise HTTPException(
            status_code=429,
            detail={"error": "monthly_quota_exceeded", "cap": MONTHLY_CAP,
                    "hint": "Contact us to raise your plan's cap."},
        )

    t0 = time.time()
    result = safety.assess([m.model_dump() for m in req.messages], user_locale=req.user_locale)
    latency_ms = int((time.time() - t0) * 1000)

    # Fail closed: a degraded scan that found nothing is NOT evidence of safety.
    # (Degraded scans that DO find risk are returned — catching beats erroring.)
    if (
        result["degraded"]
        and result["degraded_reason"] == "model_error"
        and result["risk_level"] == "none"
        and FAIL_MODE == "closed"
    ):
        raise HTTPException(
            status_code=503,
            detail={"error": "detection_unavailable", "degraded": True,
                    "hint": "Primary and backup models unreachable; keyword screen found no "
                            "risk but cannot clear the message. Retry with backoff."},
            headers={"Retry-After": "30"},
        )

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
    result["tenant"] = label
    return result


@app.get("/v1/compliance/report", tags=["evidence"])
def report(year: int = 2026, x_api_key: Optional[str] = Header(default=None)) -> Dict:
    """SB 243 §22603-shaped annual aggregates, scoped to the calling tenant.

    Without an API key this returns the shared demo sandbox — never another
    tenant's data.
    """
    label = _tenant(x_api_key)
    return store.compliance_report(year, label=None if label == "dev" else label)


@app.get("/v1/stats", tags=["evidence"])
def stats(days: int = 14, x_api_key: Optional[str] = Header(default=None)) -> Dict:
    """Operational stats for the calling tenant (dashboard data source)."""
    label = _tenant(x_api_key)
    return store.stats(days, label=None if label == "dev" else label)


@app.get("/health", tags=["ops"])
def health() -> Dict:
    return {
        "ok": True,
        "gemini_configured": bool(os.environ.get("GEMINI_API_KEY")),
        "primary_model": safety.GEMINI_MODEL,
        "backup_model": safety.GEMINI_BACKUP_MODEL,
        "fail_mode": FAIL_MODE,
    }


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
