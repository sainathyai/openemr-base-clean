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
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app import trace
from app.agent import run as run_uc1
from app.chat import get_chat
from app.datasource import get_data_client
from app.llm import get_narrator
from app.order_safety import check_order
from app.reference_ranges import classify_all
from app.schemas import PatientContext
from server import metrics

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Clinical Co-Pilot", version="0.1.0")


@app.middleware("http")
async def _record_metrics(request: Request, call_next):
    """Time every request and record it under its route TEMPLATE (so
    /api/patients/{uuid}/briefing is one series, not one per patient)."""
    t0 = time.perf_counter()
    status = 500
    try:
        resp = await call_next(request)
        status = resp.status_code
        return resp
    finally:
        route = request.scope.get("route")
        label = getattr(route, "path", None) or request.url.path
        metrics.record_request(request.method, label, status,
                               (time.perf_counter() - t0) * 1000)


# ---------- patient-context cache (perf, not correctness) ----------

_CTX_TTL = 120.0  # seconds; a demo chat session is well within this
_ctx_cache: dict[str, tuple[float, PatientContext]] = {}


async def _get_context(uuid: str) -> PatientContext:
    now = time.time()
    hit = _ctx_cache.get(uuid)
    if hit and now - hit[0] < _CTX_TTL:
        return hit[1]
    client = get_data_client()
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
    """Liveness: the process is up and can answer. Cheap, no dependencies — this is
    what a load balancer polls to decide the container is alive."""
    return {
        "status": "ok",
        "narrator": get_narrator().name,
        "openemr_base": os.getenv("OPENEMR_BASE", "https://localhost:9300"),
    }


@app.get("/api/ready")
async def ready(response: Response) -> dict[str, Any]:
    """Readiness: can we actually SERVE right now? Distinct from liveness — the
    process can be up (healthy) yet unable to serve because the data source
    (OpenEMR FHIR, or the fixtures snapshot) is unreachable. Returns 503 when the
    gating dependency is down so orchestration stops routing traffic here."""
    checks: dict[str, Any] = {}
    gating_ok = True

    client = get_data_client()
    try:
        roster, ms = await client.patients()
        checks["data_source"] = {"ok": True, "patients": len(roster), "fetch_ms": ms}
    except Exception as e:  # readiness must report, not raise
        gating_ok = False
        checks["data_source"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        await client.aclose()

    # narrator is always constructible; Langfuse is optional and never gates serving
    checks["narrator"] = {"ok": True, "name": get_narrator().name}
    checks["langfuse"] = {"ok": True, "enabled": trace.enabled(), "required": False}

    if not gating_ok:
        response.status_code = 503
    return {"ready": gating_ok, "checks": checks}


@app.get("/api/metrics")
async def metrics_json() -> dict[str, Any]:
    """The in-app dashboard's data source: request count, error rate, p50/p95 latency
    per route, plus verification pass/fail, tool calls, retries. See /dashboard."""
    return metrics.snapshot()


@app.get("/metrics")
async def metrics_prometheus() -> Response:
    """Prometheus text-exposition, scraped by the side-car Prometheus and visualised
    in Grafana. This is the production monitoring surface; /api/metrics + /dashboard
    are the zero-infra in-app view."""
    body, content_type = metrics.render_prometheus()
    return Response(content=body, media_type=content_type)


@app.get("/api/patients")
async def patients() -> dict[str, Any]:
    client = get_data_client()
    try:
        roster, ms = await client.patients()
    except (httpx.HTTPError, RuntimeError) as e:
        raise _upstream_error(e)
    finally:
        await client.aclose()
    return {"patients": roster, "fetch_ms": ms}


# ---------- schedule: the "Today's Clinic" board ----------

def _age(birth: Optional[str], asof: Optional[date] = None) -> Optional[int]:
    if not birth:
        return None
    try:
        b = date.fromisoformat(str(birth)[:10])
    except ValueError:
        return None
    a = asof or date.today()
    return a.year - b.year - ((a.month, a.day) < (b.month, b.day))


@app.get("/api/schedule")
async def schedule() -> dict[str, Any]:
    """Today's appointments for the provider, joined to the patient roster so each
    slot carries name/age/sex. Appointments are real OpenEMR calendar records; the
    roster join gives the display fields without a per-patient fetch."""
    client = get_data_client()
    today = date.today().isoformat()
    try:
        appts, ms = await client.appointments(today)
        roster, _ = await client.patients()
    except (httpx.HTTPError, RuntimeError) as e:
        raise _upstream_error(e)
    finally:
        await client.aclose()

    by_uuid = {p["uuid"]: p for p in roster}
    slots = []
    for a in appts:
        p = by_uuid.get(a.get("patient_uuid"), {})
        slots.append({
            "source_id": a.get("source_id"),
            "start": a.get("start"),
            "end": a.get("end"),
            "minutes": a.get("minutes"),
            "status": a.get("status"),
            "reason": a.get("description"),
            "flags": a.get("flags"),
            "room": a.get("room"),
            "patient": {
                "uuid": a.get("patient_uuid"),
                "name": p.get("name"),
                "sex": p.get("sex"),
                "birth_date": p.get("birth_date"),
                "age": _age(p.get("birth_date")),
            },
        })
    return {
        "date": today,
        "provider": os.getenv("COPILOT_PROVIDER_NAME", "Dr. Gregory House"),
        "appointments": slots,
        "count": len(slots),
        "fetch_ms": ms,
    }


# ---------- chart: the original record (verify view) ----------

def _chart_payload(ctx: PatientContext) -> dict[str, Any]:
    """The full, source-attributed record the agent reasons over. This is the
    doctor's verify surface: every AI source-chip resolves to a row here. Labs are
    passed through the deterministic D-9 classifier so abnormal/critical values are
    flagged (the flag OpenEMR's data lacks, computed by us, not the LLM)."""
    demo = ctx.demographics
    labs = classify_all(ctx.labs, demo)

    def _iso(d: Any) -> Optional[str]:
        return d.isoformat() if d is not None else None

    def _fmt(v: Optional[str]) -> Optional[str]:
        """Round a numeric display value to 2 dp; pass through non-numerics (BP
        '154/88', qualitative results) untouched."""
        if v is None:
            return None
        try:
            f = float(v)
            return str(int(f)) if f.is_integer() else f"{f:.2f}"
        except (TypeError, ValueError):
            return v

    return {
        "demographics": {
            "source_id": demo.source_id if demo else None,
            "name": demo.name if demo else None,
            "sex": demo.sex if demo else None,
            "birth_date": _iso(demo.birth_date) if demo else None,
            "age": _age(str(demo.birth_date) if demo and demo.birth_date else None),
            "mrn": ctx.patient_id,
        },
        "problems": [{
            "source_id": p.source_id, "text": p.text, "code": p.code,
            "onset": _iso(p.onset), "status": p.clinical_status,
        } for p in ctx.problems],
        "medications": [{
            "source_id": m.source_id, "text": m.text, "code": m.code,
            "status": m.status, "authored_on": _iso(m.authored_on),
        } for m in ctx.medications],
        "allergies": [{
            "source_id": a.source_id, "text": a.text, "criticality": a.criticality,
        } for a in ctx.allergies],
        "labs": [{
            "source_id": la.source_id, "name": la.name, "loinc": la.loinc,
            "value": _fmt(la.value), "unit": la.unit,
            "effective": _iso(la.effective) if la.effective else None,
            "status": la.status, "abnormal": la.is_abnormal, "critical": la.is_critical,
            "reference": la.reference_display,
        } for la in labs],
        "vitals": [{
            "source_id": v.source_id, "name": v.name, "value": _fmt(v.value),
            "unit": v.unit, "effective": _iso(v.effective) if v.effective else None,
        } for v in ctx.vitals],
        "encounters": [{
            "source_id": e.source_id, "date": _iso(e.date) if e.date else None,
            "reason": e.reason, "type": e.type,
        } for e in ctx.encounters],
        "counts": {
            "problems": len(ctx.problems), "medications": len(ctx.medications),
            "allergies": len(ctx.allergies), "labs": len(labs),
            "vitals": len(ctx.vitals), "encounters": len(ctx.encounters),
            "abnormal_labs": sum(1 for la in labs if la.is_abnormal),
        },
        "warnings": ctx.warnings,
    }


@app.get("/api/patients/{uuid}/chart")
async def chart(uuid: str) -> dict[str, Any]:
    try:
        ctx = await _get_context(uuid)
    except (httpx.HTTPError, RuntimeError) as e:
        raise _upstream_error(e)
    return _chart_payload(ctx)


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

    # Structured evidence per source id so the UI can render a COMPACT tile
    # (value + date) instead of the long rendered evidence string. Dates come
    # straight from the source data, never the model.
    def _d10(d: Any) -> Optional[str]:
        return d.isoformat()[:10] if d is not None else None

    ev: dict[str, dict] = {}
    if cs:
        for t in cs.trends:
            ev.setdefault(t.latest_source_id, {
                "kind": "lab", "name": t.name,
                "value": round(t.latest_value, 2) if t.latest_value is not None else None,
                "unit": t.unit, "reference": t.reference_display,
                "status": t.latest_status, "date": _d10(t.latest_date),
            })
        for a in cs.current_abnormals:
            ev[a.source_id] = {
                "kind": "lab", "name": a.name,
                "value": round(a.numeric_value, 2) if a.numeric_value is not None else a.value,
                "unit": a.unit, "reference": a.reference_display,
                "status": a.status, "date": _d10(a.effective),
            }
        for m in cs.new_medications:
            ev[m.source_id] = {"kind": "medication", "name": m.text,
                               "date": _d10(m.authored_on)}
        for p in cs.new_problems:
            ev[p.source_id] = {"kind": "problem", "name": p.text,
                               "date": _d10(p.onset)}

    statements = [{
        "category": v.statement.category,
        "text": v.statement.text,           # short qualitative phrase, not the long render
        "rendered": v.rendered,             # kept for anyone who wants the full evidence
        "source_ids": v.statement.source_ids,
        "status": v.statement.asserted_status,
        "evidence": [ev[s] for s in v.statement.source_ids if s in ev],
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
            "trace_id": out.get("trace_id"),
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
        metrics.incr("retries")   # an upstream failure the client will likely retry
        raise _upstream_error(e)
    payload = _briefing_payload(out)
    v = payload["meta"]["verification"]
    metrics.incr("verification_claims", v["claims"])
    metrics.incr("verification_passed", v["passed"])
    metrics.incr("verification_dropped", v["dropped"])
    return payload


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
    metrics.incr("tool_calls", len(result.tool_calls))
    return {
        "question": result.question,
        "answer": result.answer,
        "citations": result.citations,
        "grounded": result.grounded,
        "violations": [{"rule": v.rule, "detail": v.detail} for v in result.violations],
        "tool_calls": result.tool_calls,
        "provider": result.usage.get("provider"),
        "latency_ms": result.latency_ms,
        "trace_id": result.trace_id,
    }


# ---------- UC-2: order safety ----------

class OrderIn(BaseModel):
    order_text: str
    kind: Optional[str] = "medication"


@app.post("/api/patients/{uuid}/order-check")
async def order_check(uuid: str, body: OrderIn) -> dict[str, Any]:
    if not body.order_text.strip():
        raise HTTPException(status_code=400, detail="empty order")
    try:
        ctx = await _get_context(uuid)
    except (httpx.HTTPError, RuntimeError) as e:
        raise _upstream_error(e)
    # deterministic engine (no model call): safe to run on the event loop
    trace_id = None
    with trace.observe("uc2_order_safety", "agent",
                       input={"order_text": body.order_text},
                       metadata={"patient_uuid": uuid}) as root:
        result = check_order(body.order_text, ctx)
        findings = result.findings
        trace.update(root, output={"recognized": result.recognized,
                                   "overall": result.overall, "findings": len(findings)})
        trace_id = trace.current_trace_id()
        # Recognition rate = knowledge-table coverage; a run of "not recognized" flags a
        # coverage gap, not patient safety. Every finding must cite a source.
        trace.score("order_recognized", 1.0 if result.recognized else 0.0,
                    comment=result.matched_drug or result.order_text)
        if findings:
            sourced = sum(1 for f in findings if f.source_ids)
            trace.score("finding_source_coverage", sourced / len(findings),
                        comment=f"{sourced}/{len(findings)} findings cite a source")
    trace.flush()
    payload = result.model_dump()
    payload["trace_id"] = trace_id
    return payload


# ---------- Layer C: user feedback -> Langfuse score ----------

class FeedbackIn(BaseModel):
    trace_id: str
    value: int                      # 1 = accurate/useful, 0 = wrong/unhelpful
    use_case: Optional[str] = None  # briefing | chat | order
    comment: Optional[str] = None


@app.post("/api/feedback")
async def feedback(body: FeedbackIn) -> dict[str, Any]:
    """Attach a clinician thumbs up/down to the exact interaction's Langfuse trace.
    No-op (recorded=false) when Langfuse is not configured."""
    if not body.trace_id:
        raise HTTPException(status_code=400, detail="missing trace_id")
    ok = trace.score_trace(
        body.trace_id, "user_feedback", float(1 if body.value else 0),
        comment=body.comment or body.use_case, data_type="NUMERIC")
    return {"recorded": ok}


# ---------- SPA ----------

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/dashboard")
async def dashboard() -> FileResponse:
    """Self-contained ops dashboard (polls /api/metrics)."""
    return FileResponse(str(STATIC_DIR / "metrics.html"))
