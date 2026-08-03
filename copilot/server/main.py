"""FastAPI app: the D-3 serving layer.

Endpoints (all under /api), plus the SPA at /:
  GET  /api/health                      - liveness + which narrator is wired
  GET  /api/patients                    - roster for the picker
  GET  /api/patients/{uuid}/briefing    - UC-1 pre-visit synthesis (verified)
  POST /api/patients/{uuid}/chat        - UC-3 chart Q&A (single turn, grounded)
  POST /api/patients/{uuid}/order-check - UC-2 order safety (built next)

The clinical work is done by the existing agents; this module only orchestrates
and serialises. Patient context is briefly cached so chat turns do not each pay the
~4s parallel FHIR pull.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.agent import run as run_uc1
from app.chat import get_chat
from app.fhir_client import FhirClient
from app.llm import get_narrator
from app.schemas import PatientContext

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Clinical Co-Pilot", version="0.1.0")


# ---------- patient-context cache (perf, not correctness) ----------

_CTX_TTL = 120.0  # seconds; a demo chat session is well within this
_ctx_cache: dict[str, tuple[float, PatientContext]] = {}


async def _get_context(uuid: str) -> PatientContext:
    now = time.time()
    hit = _ctx_cache.get(uuid)
    if hit and now - hit[0] < _CTX_TTL:
        return hit[1]
    client = FhirClient()
    try:
        ctx = await client.get_context(uuid)
    finally:
        await client.aclose()
    _ctx_cache[uuid] = (now, ctx)
    return ctx


def _upstream_error(e: Exception) -> HTTPException:
    """OpenEMR unreachable or refusing: surface it as a 502 with a readable hint
    rather than a 500 stack trace."""
    return HTTPException(
        status_code=502,
        detail=f"OpenEMR FHIR call failed ({type(e).__name__}): {e}. "
               f"Is OpenEMR up and is the copilot OAuth client configured?",
    )


# ---------- health + roster ----------

@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "narrator": get_narrator().name,
        "openemr_base": os.getenv("OPENEMR_BASE", "https://localhost:9300"),
    }


@app.get("/api/patients")
async def patients() -> dict[str, Any]:
    client = FhirClient()
    try:
        roster, ms = await client.patients()
    except (httpx.HTTPError, RuntimeError) as e:
        raise _upstream_error(e)
    finally:
        await client.aclose()
    return {"patients": roster, "fetch_ms": ms}


# ---------- UC-1: pre-visit briefing ----------

def _briefing_payload(out: dict[str, Any]) -> dict[str, Any]:
    draft = out.get("draft")
    verified = out.get("verified", [])
    cs = out.get("changeset")

    order = ["critical", "abnormal_lab", "trend", "new_medication",
             "new_problem", "allergy", "context"]
    kept = [v for v in verified if v.ok]
    kept.sort(key=lambda v: order.index(v.statement.category)
              if v.statement.category in order else 99)

    statements = [{
        "category": v.statement.category,
        "text": v.rendered,
        "source_ids": v.statement.source_ids,
        "status": v.statement.asserted_status,
    } for v in kept]

    # The dropped claims are part of the story: they prove the gate is live.
    dropped = [{
        "category": v.statement.category,
        "text": v.statement.text,
        "violations": [{"rule": x.rule, "detail": x.detail} for x in v.violations],
    } for v in verified if not v.ok]

    usage = out.get("usage", {})
    return {
        "headline": draft.headline if draft else "",
        "patient_name": cs.patient_name if cs else None,
        "last_visit_date": str(cs.last_visit_date) if cs and cs.last_visit_date else None,
        "prior_visit_date": str(cs.prior_visit_date) if cs and cs.prior_visit_date else None,
        "statements": statements,
        "dropped": dropped,
        "meta": {
            "narrator": usage.get("provider"),
            "verification": {
                "claims": len(verified),
                "passed": len(kept),
                "dropped": len(verified) - len(kept),
            },
            "timings_ms": out.get("timings_ms", {}),
            "fetch_ms": out.get("fetch_ms", {}),
            "correlation_id": out.get("correlation_id"),
            "tokens": {k: usage[k] for k in ("input_tokens", "output_tokens")
                       if k in usage},
        },
        "warnings": out.get("warnings", []),
    }


@app.get("/api/patients/{uuid}/briefing")
async def briefing(uuid: str) -> dict[str, Any]:
    try:
        out = await run_uc1(uuid)
    except (httpx.HTTPError, RuntimeError) as e:
        raise _upstream_error(e)
    return _briefing_payload(out)


# ---------- UC-3: chart Q&A ----------

class ChatIn(BaseModel):
    question: str


@app.post("/api/patients/{uuid}/chat")
async def chat(uuid: str, body: ChatIn) -> dict[str, Any]:
    if not body.question.strip():
        raise HTTPException(status_code=400, detail="empty question")
    try:
        ctx = await _get_context(uuid)
    except (httpx.HTTPError, RuntimeError) as e:
        raise _upstream_error(e)
    agent = get_chat(ctx)
    # .ask is synchronous (and may call Claude): run it off the event loop.
    result = await run_in_threadpool(agent.ask, body.question)
    return {
        "question": result.question,
        "answer": result.answer,
        "citations": result.citations,
        "grounded": result.grounded,
        "violations": [{"rule": v.rule, "detail": v.detail} for v in result.violations],
        "tool_calls": result.tool_calls,
        "provider": result.usage.get("provider"),
        "latency_ms": result.latency_ms,
    }


# ---------- UC-2: order safety (endpoint stub; logic lands next) ----------

class OrderIn(BaseModel):
    order_text: str
    kind: Optional[str] = "medication"


@app.post("/api/patients/{uuid}/order-check")
async def order_check(uuid: str, body: OrderIn) -> dict[str, Any]:
    raise HTTPException(status_code=501, detail="order-check not implemented yet")


# ---------- SPA ----------

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))
