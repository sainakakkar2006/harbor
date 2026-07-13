# ⚓ Harbor

**The drop-in safety & compliance layer for AI chat apps.**

California SB 243 (effective Jan 1, 2026) requires every operator of a companion
chatbot — regardless of size — to maintain a crisis-detection protocol, refer
at-risk users to crisis services, publish the protocol, and (from July 2027)
file annual referral reports. Liability sits with the **app operator**, not the
model provider, and violations carry a private right of action at $1,000 each.
Most small AI apps have nothing in place. Harbor is one API call per user turn.

## What's inside

| Surface | Path |
|---|---|
| Landing page | `GET /` |
| Live demo playground | `GET /demo` |
| Compliance dashboard | `GET /dashboard` |
| API docs (auto) | `GET /docs` |
| Assess a turn | `POST /v1/assess` |
| SB 243 report | `GET /v1/compliance/report?year=2026` |
| Ops stats | `GET /v1/stats?days=14` |

**`POST /v1/assess`** — send the conversation, get back: five-level `risk_level`
(Gemini structured output, trajectory-aware, resists idiom/fiction false
positives), `recommended_action`, locale-appropriate `crisis_resources`,
`referral_issued`, `escalation_triggered` (fires your webhook at high/imminent —
metadata only, never content), and optional `minor_protections` (SB 243 AI
disclosure + 3-hour break-reminder timing).

**Privacy by design:** the audit log stores a salted conversation hash and
assessment metadata — never message content, never PII. That is what SB 243's
report format requires, and it means Harbor never becomes a data liability.

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export GEMINI_API_KEY=...            # from aistudio.google.com — falls back to
                                     # a conservative keyword mode if unset
.venv/bin/uvicorn main:app --reload  # → http://localhost:8000
```

Config (all optional): `GEMINI_MODEL` (default `gemini-2.5-flash`),
`HARBOR_API_KEYS="acme:sk_live_x,beta:sk_live_y"` (unset = open dev mode),
`HARBOR_ESCALATION_WEBHOOK`, `HARBOR_DB`, `HARBOR_HASH_SALT`.

## Test & evaluate

```bash
.venv/bin/python -m pytest tests/   # 13 tests incl. mocked Gemini path
.venv/bin/python redteam.py         # crisis-scenario eval suite (needs GEMINI_API_KEY)
```

## Deploy to Cloud Run

```bash
gcloud run deploy harbor --source . --region us-central1 --allow-unauthenticated \
  --set-env-vars GEMINI_API_KEY=$GEMINI_API_KEY,HARBOR_API_KEYS="you:$(openssl rand -hex 16)",HARBOR_HASH_SALT="$(openssl rand -hex 16)"
```

Note: Cloud Run's filesystem is ephemeral — for production, point `HARBOR_DB`
at a mounted volume or swap `store.py` to Cloud SQL before relying on the audit
log across restarts.

## Disclaimer

Harbor helps you implement and evidence a crisis-response protocol. It is a
safety tool, not legal advice, and does not by itself constitute compliance
with SB 243 or any other law. If you or someone you know is struggling, call
or text **988** (US).
