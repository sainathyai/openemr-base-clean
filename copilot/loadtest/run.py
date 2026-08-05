"""Concurrency load test for the Co-Pilot serving layer (Week-1 requirement).

This is a CONCURRENCY / SLO test, not an infra-ceiling test: it holds a fixed number
of simulated clinicians (closed-loop workers) hammering a realistic endpoint mix and
reports p50/p90/p95/p99 latency, error rate, and throughput at each level. The point
is to show the request path stays within an acceptable envelope at 10 and 50
concurrent users, and that errors stay at zero, not to find where it falls over.

Run against a server with observability flushing OFF (blank LANGFUSE_* keys) so the
numbers reflect the app, not the Langfuse network sink:

    python -m loadtest.run --url http://127.0.0.1:8099 --users 10 --duration 15
    python -m loadtest.run --url http://127.0.0.1:8099 --users 50 --duration 15
    python -m loadtest.run --url http://127.0.0.1:8099 --levels 10,50   # both, back to back

Endpoint mix (weighted to the read-heavy pre-visit workflow):
    schedule 25% · briefing 35% (UC-1) · chat 20% (UC-3) · order-check 20% (UC-2)
"""
from __future__ import annotations

import argparse
import asyncio
import random
import time

import httpx

WORKLOAD = [
    ("GET", "/api/schedule", 0.25, None),
    ("GET", "/api/patients/{uuid}/briefing", 0.35, None),
    ("POST", "/api/patients/{uuid}/chat", 0.20, {"question": "What changed since the last visit?"}),
    ("POST", "/api/patients/{uuid}/order-check", 0.20, {"order_text": "Ibuprofen 600 mg"}),
]


def _pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)


async def _uuids(client: httpx.AsyncClient) -> list[str]:
    r = await client.get("/api/schedule")
    r.raise_for_status()
    return [a["patient"]["uuid"] for a in r.json().get("appointments", []) if a.get("patient")]


def _pick() -> tuple[str, str, dict | None]:
    x = random.random()
    acc = 0.0
    for method, path, w, body in WORKLOAD:
        acc += w
        if x <= acc:
            return method, path, body
    return WORKLOAD[-1][0], WORKLOAD[-1][1], WORKLOAD[-1][3]


class Rec:
    __slots__ = ("path", "ms", "ok")

    def __init__(self, path: str, ms: float, ok: bool) -> None:
        self.path, self.ms, self.ok = path, ms, ok


async def _worker(client: httpx.AsyncClient, uuids: list[str], deadline: float,
                  out: list[Rec]) -> None:
    while time.perf_counter() < deadline:
        method, path, body = _pick()
        url = path.replace("{uuid}", random.choice(uuids))
        t0 = time.perf_counter()
        ok = False
        try:
            if method == "GET":
                resp = await client.get(url)
            else:
                resp = await client.post(url, json=body)
            ok = resp.status_code < 500
        except Exception:
            ok = False
        out.append(Rec(path, (time.perf_counter() - t0) * 1000, ok))


async def run_level(base: str, users: int, duration: float) -> dict:
    limits = httpx.Limits(max_connections=users + 5, max_keepalive_connections=users + 5)
    async with httpx.AsyncClient(base_url=base, timeout=30.0, limits=limits) as client:
        uuids = await _uuids(client)
        if not uuids:
            raise SystemExit("no patients from /api/schedule; is the server in fixtures mode?")
        recs: list[Rec] = []
        deadline = time.perf_counter() + duration
        t0 = time.perf_counter()
        await asyncio.gather(*[_worker(client, uuids, deadline, recs) for _ in range(users)])
        wall = time.perf_counter() - t0

    lat = [r.ms for r in recs]
    errs = sum(1 for r in recs if not r.ok)
    by: dict[str, list[float]] = {}
    byerr: dict[str, int] = {}
    for r in recs:
        by.setdefault(r.path, []).append(r.ms)
        byerr[r.path] = byerr.get(r.path, 0) + (0 if r.ok else 1)
    return {
        "users": users, "duration_s": round(wall, 2), "requests": len(recs),
        "throughput_rps": round(len(recs) / wall, 1) if wall else 0.0,
        "errors": errs, "error_rate": round(errs / len(recs), 4) if recs else 0.0,
        "p50_ms": round(_pct(lat, 0.50), 1), "p90_ms": round(_pct(lat, 0.90), 1),
        "p95_ms": round(_pct(lat, 0.95), 1), "p99_ms": round(_pct(lat, 0.99), 1),
        "by_endpoint": {
            p: {"count": len(v), "errors": byerr.get(p, 0),
                "p50_ms": round(_pct(v, 0.50), 1), "p95_ms": round(_pct(v, 0.95), 1)}
            for p, v in sorted(by.items())
        },
    }


def _print(res: dict) -> None:
    print(f"\n=== {res['users']} concurrent users · {res['duration_s']}s ===")
    print(f"requests={res['requests']}  throughput={res['throughput_rps']} req/s  "
          f"errors={res['errors']} ({res['error_rate']*100:.2f}%)")
    print(f"latency ms  p50={res['p50_ms']}  p90={res['p90_ms']}  "
          f"p95={res['p95_ms']}  p99={res['p99_ms']}")
    print(f"{'endpoint':44s} {'n':>5} {'err':>4} {'p50':>8} {'p95':>8}")
    for p, s in res["by_endpoint"].items():
        print(f"{p:44s} {s['count']:>5} {s['errors']:>4} {s['p50_ms']:>8} {s['p95_ms']:>8}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Co-Pilot concurrency load test.")
    ap.add_argument("--url", default="http://127.0.0.1:8099")
    ap.add_argument("--users", type=int, default=10)
    ap.add_argument("--duration", type=float, default=15.0)
    ap.add_argument("--levels", default=None,
                    help="comma list of user counts to run back-to-back, e.g. 10,50")
    args = ap.parse_args(argv)

    levels = [int(x) for x in args.levels.split(",")] if args.levels else [args.users]
    for u in levels:
        res = asyncio.run(run_level(args.url, u, args.duration))
        _print(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
