"""In-process metrics for the serving layer (D-3 observability, request side).

Langfuse owns the LLM-path signal (tool sequence, token cost, verification/grounding
scores). This module owns the HTTP-path signal the Week-1 dashboard asks for that is
NOT model-specific: request count, error rate, p50/p95 latency per route, plus a few
clinical counters (verification pass/fail, tool calls, retries) mirrored here so the
dashboard renders without a round-trip to Langfuse.

Deliberately tiny and dependency-free: one lock, fixed-size latency rings per route,
so it fits the single t3.micro container. Not a replacement for Prometheus in a real
deployment; it is the self-contained demo dashboard source.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

_LOCK = threading.Lock()
_MAX_SAMPLES = 500          # per-route latency ring; bounds memory on a 1 GB box
_started = time.time()

# ---------- Prometheus exposition (scraped by the side-car Prometheus) ----------
# Same call sites as the in-process collector below, so the JSON dashboard and the
# Prometheus/Grafana stack read one source of truth. Histogram buckets tuned to this
# app: UC-1 briefing is the slow path (~hundreds of ms), everything else sub-100ms.
PROM_CONTENT_TYPE = CONTENT_TYPE_LATEST

_REQS = Counter("copilot_requests_total", "HTTP requests",
                ["method", "route", "status"])
_LAT = Histogram("copilot_request_duration_seconds", "HTTP request latency",
                 ["method", "route"],
                 buckets=(0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.8, 1.2, 2.0, 5.0))
_VERIF = Counter("copilot_verification_claims_total",
                 "UC-1 claims seen by the verification gate", ["outcome"])  # passed|dropped
_TOOLS = Counter("copilot_tool_calls_total", "UC-3 grounded tool calls")
_RETRIES = Counter("copilot_upstream_retries_total", "Upstream failures (retry-worthy)")

_COUNTER_MAP = {
    "tool_calls": lambda n: _TOOLS.inc(n),
    "retries": lambda n: _RETRIES.inc(n),
    "verification_passed": lambda n: _VERIF.labels(outcome="passed").inc(n),
    "verification_dropped": lambda n: _VERIF.labels(outcome="dropped").inc(n),
    # verification_claims is derivable as passed+dropped in PromQL; kept only in JSON
}


def render_prometheus() -> tuple[bytes, str]:
    """(-body, content_type) for the GET /metrics scrape endpoint."""
    return generate_latest(), PROM_CONTENT_TYPE


class _RouteStat:
    __slots__ = ("count", "errors", "samples")

    def __init__(self) -> None:
        self.count = 0
        self.errors = 0
        self.samples: deque[float] = deque(maxlen=_MAX_SAMPLES)


_routes: dict[str, _RouteStat] = defaultdict(_RouteStat)
_counters: dict[str, float] = defaultdict(float)


def record_request(method: str, route: str, status: int, ms: float) -> None:
    # Prometheus (time-series, scraped)
    _REQS.labels(method=method, route=route, status=str(status)).inc()
    _LAT.labels(method=method, route=route).observe(ms / 1000.0)
    # in-process collector (JSON dashboard)
    with _LOCK:
        s = _routes[f"{method} {route}"]
        s.count += 1
        if status >= 500:
            s.errors += 1
        s.samples.append(ms)


def incr(name: str, n: float = 1.0) -> None:
    fn = _COUNTER_MAP.get(name)
    if fn is not None:
        fn(n)
    with _LOCK:
        _counters[name] += n


def _pct(vals: list[float], p: float) -> float:
    """Linear-interpolation percentile over a sorted list."""
    if not vals:
        return 0.0
    k = (len(vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(vals) - 1)
    if f == c:
        return vals[f]
    return vals[f] + (vals[c] - vals[f]) * (k - f)


def snapshot() -> dict:
    with _LOCK:
        routes = {}
        total = errs = 0
        for r, s in _routes.items():
            vals = sorted(s.samples)
            routes[r] = {
                "count": s.count,
                "errors": s.errors,
                "p50_ms": round(_pct(vals, 0.50), 1),
                "p95_ms": round(_pct(vals, 0.95), 1),
                "max_ms": round(vals[-1], 1) if vals else 0.0,
            }
            total += s.count
            errs += s.errors
        c = dict(_counters)
        vc = c.get("verification_claims", 0.0)
        vp = c.get("verification_passed", 0.0)
        return {
            "uptime_s": round(time.time() - _started, 1),
            "requests": total,
            "errors": errs,
            "error_rate": round(errs / total, 4) if total else 0.0,
            "routes": dict(sorted(routes.items())),
            "verification": {
                "claims": int(vc),
                "passed": int(vp),
                "dropped": int(c.get("verification_dropped", 0.0)),
                "pass_rate": round(vp / vc, 4) if vc else None,
            },
            "tool_calls": int(c.get("tool_calls", 0.0)),
            "retries": int(c.get("retries", 0.0)),
        }
